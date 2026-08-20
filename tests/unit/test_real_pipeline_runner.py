import json
import os
from pathlib import Path

import httpx
import pytest
from yt_dlp.utils import DownloadError

from video_sum_core.errors import (
    LLMAuthenticationError,
    TranscriptionAuthenticationError,
    TranscriptionConfigurationError,
    UnsupportedInputError,
    VideoSumError,
)
from video_sum_core.models.tasks import InputType, TaskInput, TaskResult
from video_sum_core.pipeline.base import PipelineContext
from video_sum_core.pipeline.real import PipelineSettings, RealPipelineRunner


def _build_runner() -> RealPipelineRunner:
    return RealPipelineRunner(
        PipelineSettings(
            tasks_dir=Path("tests/tmp_tasks"),
            llm_enabled=True,
            llm_api_key="test-key",
            llm_base_url="https://example.com/v1",
            llm_model="test-model",
        )
    )


def test_summarize_falls_back_to_rules_when_llm_auth_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _build_runner()
    transcript = "[00:00] 第一条\n[00:10] 第二条\n[00:20] 第三条"
    segments = [
        {"start": 0, "text": "第一条"},
        {"start": 10, "text": "第二条"},
        {"start": 20, "text": "第三条"},
    ]
    events: list[tuple[str, int, str, dict[str, object] | None]] = []

    def fake_llm_summary(
        transcript: str,
        segments: list[dict[str, object]],
        title: str,
        emit,
        source_kind: str | None = None,
    ) -> dict[str, object]:
        raise LLMAuthenticationError("token 已失效")

    monkeypatch.setattr(runner, "_summarize_with_llm", fake_llm_summary)

    summary = runner._summarize(
        transcript=transcript,
        segments=segments,
        title="示例视频",
        emit=lambda stage, progress, message, payload=None: events.append((stage, progress, message, payload)),
    )

    assert summary["title"] == "示例视频"
    assert summary["overview"]
    assert summary["bulletPoints"]
    assert summary["chapters"]
    assert any("已切换为本地规则摘要" in message for _, _, message, _ in events)


def test_summarize_falls_back_to_rules_when_llm_config_is_incomplete() -> None:
    runner = RealPipelineRunner(
        PipelineSettings(
            tasks_dir=Path("tests/tmp_tasks"),
            llm_enabled=True,
            llm_api_key="test-key",
            llm_base_url="",
            llm_model="",
        )
    )
    transcript = "[00:00] 第一条\n[00:10] 第二条"
    segments = [
        {"start": 0, "text": "第一条"},
        {"start": 10, "text": "第二条"},
    ]

    summary = runner._summarize(
        transcript=transcript,
        segments=segments,
        title="配置缺失示例",
        emit=lambda *_args, **_kwargs: None,
    )

    assert summary["title"] == "配置缺失示例"
    assert summary["overview"]
    assert summary["bulletPoints"]


def test_summarize_emits_partial_result_payloads() -> None:
    runner = RealPipelineRunner(
        PipelineSettings(
            tasks_dir=Path("tests/tmp_tasks"),
            llm_enabled=True,
            llm_api_key="test-key",
            llm_base_url="https://example.com/v1",
            llm_model="test-model",
        )
    )
    transcript = "[00:00] 第一条\n[00:10] 第二条"
    segments = [
        {"start": 0, "text": "第一条"},
        {"start": 10, "text": "第二条"},
    ]
    events: list[tuple[str, int, str, dict[str, object] | None]] = []

    def fake_llm_summary(
        transcript: str,
        segments: list[dict[str, object]],
        title: str,
        emit,
        source_kind: str | None = None,
    ) -> dict[str, object]:
        return {
            "title": title,
            "overview": "整体概览",
            "bulletPoints": ["要点一", "要点二", "要点三", "要点四", "要点五"],
            "chapters": [{"title": "章节一", "start": 0, "summary": "章节摘要"}],
            "chapterGroups": [{"title": "主题一", "start": 0, "summary": "主题摘要", "children": [{"title": "章节一", "start": 0, "summary": "章节摘要"}]}],
        }

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(runner, "_summarize_with_llm", fake_llm_summary)
    monkeypatch.setattr(
        runner,
        "_generate_knowledge_note_with_llm",
        lambda **_kwargs: {
            "knowledgeNoteMarkdown": "# 知识笔记\n\n内容",
            "llm_prompt_tokens": 12,
            "llm_completion_tokens": 8,
            "llm_total_tokens": 20,
        },
    )

    try:
        summary = runner._summarize(
            transcript=transcript,
            segments=segments,
            title="阶段产出示例",
            emit=lambda stage, progress, message, payload=None: events.append((stage, progress, message, payload)),
        )
    finally:
        monkeypatch.undo()

    knowledge_cards_event = next(payload for _, _, message, payload in events if message == "知识卡片摘要生成完成")
    knowledge_note_event = next(payload for _, _, message, payload in events if message == "知识笔记生成完成")

    assert knowledge_cards_event is not None
    assert knowledge_cards_event["result_scope"] == "knowledge_cards"
    assert knowledge_cards_event["result"]["overview"] == "整体概览"
    assert knowledge_cards_event["result"]["knowledge_note_markdown"]
    assert knowledge_note_event is not None
    assert knowledge_note_event["result_scope"] == "knowledge_note"
    assert knowledge_note_event["result"]["knowledge_note_markdown"] == "# 知识笔记\n\n内容"
    assert summary["knowledgeNoteMarkdown"] == "# 知识笔记\n\n内容"


def test_build_fallback_segments_from_transcript_preserves_order() -> None:
    runner = RealPipelineRunner(PipelineSettings(tasks_dir=Path("tests/tmp_tasks")))

    segments = runner._build_fallback_segments_from_transcript(
        "第一句。第二句继续展开。第三句给出结论。",
        duration=30.0,
    )

    assert len(segments) >= 1
    assert segments[0]["start"] == 0.0
    assert float(segments[-1]["end"]) == 30.0
    assert "第一句" in "".join(str(segment["text"]) for segment in segments)


def test_build_fallback_segments_from_long_comma_only_transcript_splits_into_multiple_timestamps() -> None:
    runner = RealPipelineRunner(PipelineSettings(tasks_dir=Path("tests/tmp_tasks")))

    transcript = (
        "你的ai正在变得越来越会舔了，那么早期ai最大的问题是幻觉啊，"
        "如果你两三年前用过ai1定会对它的胡言乱语印象深刻，"
        "怕你说啊，这ai加1它等于3，那ai都可能一本正经的回答，"
        "当然，在如今的大模型里，这种离谱的情况已经越来越少了，"
        "但是取而代之的是一种更隐蔽的问题。"
    )

    segments = runner._build_fallback_segments_from_transcript(transcript, duration=75.0)
    rendered = runner._render_transcript_from_segments(segments)

    assert len(segments) >= 3
    assert rendered.count("[") >= 3
    assert "[00:00]" in rendered
    assert "[01:" not in rendered or float(segments[-1]["end"]) <= 75.0


def test_mindmap_uses_local_fallback_when_repair_request_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _build_runner()
    calls: list[str] = []

    def fake_request(*, usage_stage: str, **_kwargs):
        calls.append(usage_stage)
        if usage_stage == "mindmap":
            return {
                "title": "示例导图",
                "root": "root",
                "nodes": [
                    {
                        "id": "root",
                        "label": "示例导图",
                        "type": "root",
                        "summary": "概览",
                        "children": [
                            {"id": "theme-1", "label": "单一分支", "type": "theme", "summary": "内容"}
                        ],
                    }
                ],
            }
        raise RuntimeError("repair timeout")

    monkeypatch.setattr(runner, "_request_llm_json", fake_request)
    mindmap, usage = runner._generate_mindmap_with_llm(
        title="示例视频",
        result=TaskResult(
            overview="视频概览",
            key_points=["关键点一", "关键点二"],
            timeline=[
                {"title": "概念", "start": 0, "summary": "概念说明"},
                {"title": "方法", "start": 10, "summary": "方法说明"},
                {"title": "应用", "start": 20, "summary": "应用说明"},
            ],
        ),
    )

    root = next(node for node in mindmap.nodes if node.id == mindmap.root)
    assert len(root.children) >= 3
    assert calls == ["mindmap", "mindmap_repair"]
    assert usage == []


def test_transcribe_uses_siliconflow_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = RealPipelineRunner(
        PipelineSettings(
            tasks_dir=Path("tests/tmp_tasks"),
            transcription_provider="siliconflow",
            siliconflow_asr_api_key="test-key",
        )
    )

    called: dict[str, object] = {}

    def fake_siliconflow(
        audio_path: Path,
        duration: float | None,
        emit,
    ) -> tuple[str, list[dict[str, object]]]:
        called["audio_path"] = audio_path
        called["duration"] = duration
        return "mock transcript", [{"start": 0.0, "end": 5.0, "text": "mock"}]

    monkeypatch.setattr(runner, "_transcribe_with_siliconflow", fake_siliconflow)

    transcript, segments = runner._transcribe(Path("sample.mp3"), 12.0, lambda *_args, **_kwargs: None)

    assert transcript == "mock transcript"
    assert segments[0]["text"] == "mock"
    assert called["audio_path"] == Path("sample.mp3")
    assert called["duration"] == 12.0


def test_transcribe_uses_multimodal_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = RealPipelineRunner(
        PipelineSettings(
            tasks_dir=Path("tests/tmp_tasks"),
            transcription_provider="multimodal",
            multimodal_asr_base_url="https://api.example.com/v1",
            multimodal_asr_model="test-model",
        )
    )

    called: dict[str, object] = {}

    def fake_multimodal(
        audio_path: Path,
        duration: float | None,
        emit,
    ) -> tuple[str, list[dict[str, object]]]:
        called["audio_path"] = audio_path
        called["duration"] = duration
        return "mock transcript", [{"start": 0.0, "end": 5.0, "text": "mock"}]

    monkeypatch.setattr(runner, "_transcribe_with_multimodal", fake_multimodal)

    transcript, segments = runner._transcribe(Path("sample.mp3"), 12.0, lambda *_args, **_kwargs: None)

    assert transcript == "mock transcript"
    assert segments[0]["text"] == "mock"
    assert called["audio_path"] == Path("sample.mp3")
    assert called["duration"] == 12.0


def test_transcribe_with_multimodal_uses_configured_ffmpeg_for_chunking(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = RealPipelineRunner(
        PipelineSettings(
            tasks_dir=tmp_path,
            transcription_provider="multimodal",
            multimodal_asr_base_url="https://api.example.com/v1",
            multimodal_asr_model="test-model",
        )
    )
    audio_path = tmp_path / "sample.mp3"
    audio_path.write_bytes(b"fake audio" * 1024)
    ffmpeg_exe = tmp_path / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    ffprobe_exe = tmp_path / ("ffprobe.exe" if os.name == "nt" else "ffprobe")
    ffmpeg_exe.write_text("", encoding="utf-8")
    ffprobe_exe.write_text("", encoding="utf-8")
    commands: list[list[str]] = []

    class FakeCompletedProcess:
        def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = returncode

    def fake_run(command, *args, **kwargs):
        command = [str(item) for item in command]
        commands.append(command)
        if command[0] == str(ffprobe_exe):
            return FakeCompletedProcess(stdout="370.0\n")
        chunk_path = Path(command[-1])
        chunk_path.write_bytes(b"chunk")
        return FakeCompletedProcess()

    transcripts = iter(["第一段", "第二段", "第三段"])

    monkeypatch.setattr("video_sum_core.pipeline.real.ffmpeg_location", lambda: ffmpeg_exe)
    monkeypatch.setattr("video_sum_core.pipeline.real.subprocess.run", fake_run)
    monkeypatch.setattr(
        runner,
        "_build_fallback_segments_from_transcript",
        lambda text, duration: [{"start": 0.0, "end": 1.0, "text": text}],
    )

    def fake_send(*args, **kwargs):
        return next(transcripts)

    # Replace the network-facing nested behavior by making every generated chunk readable
    # through a small fake httpx client.
    class FakeResponse:
        status_code = 200
        text = "{}"

        def json(self) -> dict[str, object]:
            return {"choices": [{"message": {"content": fake_send()}}]}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def post(self, *args, **kwargs) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr("video_sum_core.pipeline.real.httpx.Client", FakeClient)

    transcript, segments = runner._transcribe_with_multimodal(
        audio_path,
        None,
        lambda *_args, **_kwargs: None,
    )

    assert commands[0][0] == str(ffprobe_exe)
    assert all(command[0] == str(ffmpeg_exe) for command in commands[1:])
    assert len(commands[1:]) == 3
    assert "第一段" in transcript
    assert [segment["text"] for segment in segments] == ["第一段", "第二段", "第三段"]


def test_transcribe_with_multimodal_fails_when_large_audio_duration_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = RealPipelineRunner(
        PipelineSettings(
            tasks_dir=tmp_path,
            transcription_provider="multimodal",
            multimodal_asr_base_url="https://api.example.com/v1",
            multimodal_asr_model="test-model",
        )
    )
    audio_path = tmp_path / "large.mp3"
    audio_path.write_bytes(b"0" * (16 * 1024 * 1024))
    ffmpeg_exe = tmp_path / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    ffprobe_exe = tmp_path / ("ffprobe.exe" if os.name == "nt" else "ffprobe")
    ffmpeg_exe.write_text("", encoding="utf-8")
    ffprobe_exe.write_text("", encoding="utf-8")

    class FakeCompletedProcess:
        stdout = ""
        stderr = ""
        returncode = 1

    monkeypatch.setattr("video_sum_core.pipeline.real.ffmpeg_location", lambda: ffmpeg_exe)
    monkeypatch.setattr(
        "video_sum_core.pipeline.real.subprocess.run",
        lambda *args, **kwargs: FakeCompletedProcess(),
    )

    with pytest.raises(VideoSumError, match="duration"):
        runner._transcribe_with_multimodal(audio_path, None, lambda *_args, **_kwargs: None)


def test_transcribe_with_siliconflow_requires_api_key() -> None:
    runner = RealPipelineRunner(
        PipelineSettings(
            tasks_dir=Path("tests/tmp_tasks"),
            transcription_provider="siliconflow",
            siliconflow_asr_api_key="",
        )
    )

    with pytest.raises(TranscriptionConfigurationError, match="API key"):
        runner._transcribe_with_siliconflow(Path("sample.mp3"), 10.0, lambda *_args, **_kwargs: None)


def test_transcribe_with_siliconflow_maps_auth_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = RealPipelineRunner(
        PipelineSettings(
            tasks_dir=Path("tests/tmp_tasks"),
            transcription_provider="siliconflow",
            siliconflow_asr_api_key="test-key",
        )
    )
    audio_path = tmp_path / "sample.mp3"
    audio_path.write_bytes(b"fake audio")

    class FakeResponse:
        status_code = 401
        text = '{"error":{"message":"invalid key"}}'

        def json(self) -> dict[str, object]:
            return {"error": {"message": "invalid key"}}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def post(self, *args, **kwargs) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr("video_sum_core.pipeline.real.httpx.Client", FakeClient)

    with pytest.raises(TranscriptionAuthenticationError, match="auth failed"):
        runner._transcribe_with_siliconflow(audio_path, 10.0, lambda *_args, **_kwargs: None)


def test_transcribe_with_local_asr_requires_installed_runtime() -> None:
    runner = RealPipelineRunner(
        PipelineSettings(
            tasks_dir=Path("tests/tmp_tasks"),
            transcription_provider="local",
            local_asr_available=False,
        )
    )

    with pytest.raises(TranscriptionConfigurationError, match="Install local ASR from Settings"):
        runner._transcribe(Path("sample.mp3"), 10.0, lambda *_args, **_kwargs: None)


def test_preflight_checks_llm_before_full_run(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _build_runner()
    events: list[tuple[str, int, str, dict[str, object] | None]] = []

    monkeypatch.setattr(runner, "_preflight_llm_availability", lambda: None)

    runner.preflight(
        PipelineContext(
            task_id="task-1",
            task_input={
                "input_type": InputType.URL,
                "source": "https://www.bilibili.com/video/BV1xx411c7mD",
                "title": "测试视频",
            },
        ),
        on_event=lambda event: events.append((event.stage, event.progress, event.message, event.payload)),
    )

    assert events[0][0] == "preflight"
    assert "正在检查 LLM API 是否可用" in events[0][2]
    assert events[-1][0] == "preflight"
    assert "LLM API 检查通过" in events[-1][2]


def test_run_from_url_accepts_youtube_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = RealPipelineRunner(PipelineSettings(tasks_dir=tmp_path))
    emitted: list[tuple[str, int, str, dict[str, object] | None]] = []

    monkeypatch.setattr(runner, "_probe_video", lambda _url: {"title": "YouTube 示例", "duration": 30.0})
    monkeypatch.setattr(runner, "_download_audio", lambda *_args, **_kwargs: tmp_path / "sample.mp3")
    monkeypatch.setattr(
        runner,
        "_transcribe",
        lambda *_args, **_kwargs: ("[00:00] 示例", [{"start": 0.0, "end": 3.0, "text": "示例"}]),
    )
    monkeypatch.setattr(
        runner,
        "_export_transcript_snapshot",
        lambda *_args, **_kwargs: type(
            "SnapshotResult",
            (),
            {
                "artifacts": {
                    "transcript_path": str(tmp_path / "transcript.txt"),
                    "summary_path": str(tmp_path / "summary.json"),
                },
                "model_dump": lambda self, mode="json": self.artifacts,
            },
        )(),
    )
    monkeypatch.setattr(
        runner,
        "_summarize",
        lambda *_args, **_kwargs: {
            "overview": "概览",
            "knowledgeNoteMarkdown": "# Note",
            "bulletPoints": [],
            "chapters": [],
            "chapterGroups": [],
        },
    )
    monkeypatch.setattr(
        runner,
        "_export_result",
        lambda *_args, **_kwargs: runner._build_task_result("transcript", {"overview": "概览", "knowledgeNoteMarkdown": "# Note"}),
    )

    result_events, result = runner.run(
        PipelineContext(
            task_id="task-youtube",
            task_input={
                "input_type": InputType.URL,
                "source": "https://youtu.be/dQw4w9WgXcQ",
                "title": None,
            },
        ),
        on_event=lambda event: emitted.append((event.stage, event.progress, event.message, event.payload)),
    )

    assert result.overview == "概览"
    assert emitted[0][0] == "preparing" or result_events[0].stage == "preparing"


def test_run_from_local_video_file_uses_local_media_pipeline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = RealPipelineRunner(PipelineSettings(tasks_dir=tmp_path))
    local_video = tmp_path / "sample.mp4"
    local_video.write_bytes(b"fake-video")
    emitted: list[tuple[str, int, str, dict[str, object] | None]] = []

    monkeypatch.setattr(runner, "_prepare_local_audio_source", lambda *_args, **_kwargs: tmp_path / "sample.mp3")
    monkeypatch.setattr(
        runner,
        "_transcribe",
        lambda *_args, **_kwargs: ("[00:00] 本地示例", [{"start": 0.0, "end": 3.0, "text": "本地示例"}]),
    )
    monkeypatch.setattr(
        runner,
        "_export_transcript_snapshot",
        lambda *_args, **_kwargs: type(
            "SnapshotResult",
            (),
            {
                "artifacts": {
                    "transcript_path": str(tmp_path / "transcript.txt"),
                    "summary_path": str(tmp_path / "summary.json"),
                },
                "model_dump": lambda self, mode="json": self.artifacts,
            },
        )(),
    )
    monkeypatch.setattr(
        runner,
        "_summarize",
        lambda *_args, **_kwargs: {
            "overview": "本地概览",
            "knowledgeNoteMarkdown": "# 本地笔记",
            "bulletPoints": [],
            "chapters": [],
            "chapterGroups": [],
        },
    )
    monkeypatch.setattr(
        runner,
        "_export_result",
        lambda *_args, **_kwargs: runner._build_task_result("transcript", {"overview": "本地概览", "knowledgeNoteMarkdown": "# 本地笔记"}),
    )

    result_events, result = runner.run(
        PipelineContext(
            task_id="task-local-video",
            task_input={
                "input_type": InputType.VIDEO_FILE,
                "source": str(local_video),
                "title": "本地示例视频",
            },
        ),
        on_event=lambda event: emitted.append((event.stage, event.progress, event.message, event.payload)),
    )

    assert result.overview == "本地概览"
    assert emitted[0][0] == "preparing" or result_events[0].stage == "preparing"
    assert any("本地视频文件" in message for _, _, message, _ in emitted)


def test_run_from_url_rejects_unsupported_url(tmp_path: Path) -> None:
    runner = RealPipelineRunner(PipelineSettings(tasks_dir=tmp_path))

    with pytest.raises(UnsupportedInputError, match="supports Bilibili and YouTube"):
        runner.run(
            PipelineContext(
                task_id="task-unsupported",
                task_input={
                    "input_type": InputType.URL,
                    "source": "https://example.com/video/123",
                    "title": None,
                },
            )
        )


def test_probe_video_uses_cookies_file_and_browser_headers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    captured_options: dict[str, object] = {}

    class FakeYoutubeDL:
        def __init__(self, options: dict[str, object]) -> None:
            captured_options.update(options)

        def __enter__(self) -> "FakeYoutubeDL":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def extract_info(self, url: str, download: bool = False) -> dict[str, object]:
            return {"title": "测试视频"}

    monkeypatch.setattr("video_sum_core.pipeline.real.YoutubeDL", FakeYoutubeDL)
    runner = RealPipelineRunner(PipelineSettings(tasks_dir=tmp_path, ytdlp_cookies_file=str(cookie_file)))

    assert runner._probe_video("https://www.bilibili.com/video/BV1xx411c7mD")["title"] == "测试视频"
    assert captured_options["cookiefile"] == str(cookie_file)
    assert "Mozilla/5.0" in captured_options["http_headers"]["User-Agent"]
    assert captured_options["http_headers"]["Referer"] == "https://www.bilibili.com/"


def test_probe_video_maps_bilibili_412_to_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FakeYoutubeDL:
        def __init__(self, options: dict[str, object]) -> None:
            pass

        def __enter__(self) -> "FakeYoutubeDL":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def extract_info(self, url: str, download: bool = False) -> dict[str, object]:
            raise DownloadError("ERROR: [BiliBili] BV1xx: HTTP Error 412: Precondition Failed")

    monkeypatch.setattr("video_sum_core.pipeline.real.YoutubeDL", FakeYoutubeDL)
    runner = RealPipelineRunner(PipelineSettings(tasks_dir=tmp_path))

    with pytest.raises(VideoSumError, match="VIDEO_SUM_YTDLP_COOKIES_FILE"):
        runner._probe_video("https://www.bilibili.com/video/BV1xx411c7mD")


def test_probe_video_maps_chrome_cookie_database_failure_to_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FakeYoutubeDL:
        def __init__(self, options: dict[str, object]) -> None:
            pass

        def __enter__(self) -> "FakeYoutubeDL":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def extract_info(self, url: str, download: bool = False) -> dict[str, object]:
            raise DownloadError("ERROR: Could not copy Chrome cookie database")

    monkeypatch.setattr("video_sum_core.pipeline.real.YoutubeDL", FakeYoutubeDL)
    runner = RealPipelineRunner(PipelineSettings(tasks_dir=tmp_path, ytdlp_cookies_browser="chrome"))

    with pytest.raises(VideoSumError, match="登录窗口"):
        runner._probe_video("https://www.bilibili.com/video/BV1xx411c7mD")


def test_probe_video_maps_browser_dpapi_failure_to_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FakeYoutubeDL:
        def __init__(self, options: dict[str, object]) -> None:
            pass

        def __enter__(self) -> "FakeYoutubeDL":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def extract_info(self, url: str, download: bool = False) -> dict[str, object]:
            raise DownloadError("ERROR: Failed to decrypt with DPAPI")

    monkeypatch.setattr("video_sum_core.pipeline.real.YoutubeDL", FakeYoutubeDL)
    runner = RealPipelineRunner(PipelineSettings(tasks_dir=tmp_path, ytdlp_cookies_browser="chrome"))

    with pytest.raises(VideoSumError, match="登录窗口"):
        runner._probe_video("https://www.bilibili.com/video/BV1xx411c7mD")


def test_preflight_llm_timeout_fails_quickly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = RealPipelineRunner(
        PipelineSettings(
            tasks_dir=tmp_path,
            llm_enabled=True,
            llm_api_key="test-key",
            llm_base_url="https://api.example.com/v1",
            llm_model="test-model",
        )
    )

    def fake_request(**kwargs: object) -> dict[str, object]:
        assert kwargs["timeout"] == 20
        assert kwargs["retry_count"] == 0
        raise httpx.ReadTimeout("slow preflight")

    monkeypatch.setattr(runner, "_request_llm_json", fake_request)

    with pytest.raises(VideoSumError, match="LLM API 检查超时"):
        runner._preflight_llm_availability()


def test_llm_json_request_normalizes_mimo_model(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = RealPipelineRunner(
        PipelineSettings(
            tasks_dir=tmp_path,
            llm_enabled=True,
            llm_api_key="test-key",
            llm_base_url="https://api.example.com/v1",
            llm_model="MiMo-V2.5-Pro",
        )
    )
    calls: list[dict[str, object]] = []

    class FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "choices": [{"message": {"content": '{"overview":"ok"}'}}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "total_tokens": 12,
                    "prompt_cache_hit_tokens": 6,
                    "prompt_cache_miss_tokens": 4,
                },
            }

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def post(self, url: str, headers: dict[str, str], json: dict[str, object]) -> FakeResponse:
            calls.append({"url": url, "headers": headers, "json": json})
            return FakeResponse()

    monkeypatch.setattr("video_sum_core.pipeline.real.httpx.Client", FakeClient)

    result = runner._request_llm_json(
        base_url="https://api.example.com/v1",
        payload={"model": "MiMo-V2.5-Pro", "messages": []},
    )

    assert result["overview"] == "ok"
    assert calls[0]["json"]["model"] == "mimo-v2.5-pro"
    assert result["llm_prompt_cache_hit_tokens"] == 6
    assert result["llm_prompt_cache_miss_tokens"] == 4


def test_llm_json_request_accepts_choice_text_response(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = RealPipelineRunner(
        PipelineSettings(
            tasks_dir=tmp_path,
            llm_enabled=True,
            llm_api_key="test-key",
            llm_base_url="https://api.example.com/v1",
            llm_model="test-model",
        )
    )

    class FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"choices": [{"text": '{"overview":"ok from text"}'}]}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def post(self, url: str, headers: dict[str, str], json: dict[str, object]) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr("video_sum_core.pipeline.real.httpx.Client", FakeClient)

    result = runner._request_llm_json(
        base_url="https://api.example.com/v1",
        payload={"model": "test-model", "messages": []},
    )

    assert result["overview"] == "ok from text"


def test_llm_json_request_reads_openai_nested_cached_tokens(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = RealPipelineRunner(
        PipelineSettings(
            tasks_dir=tmp_path,
            llm_enabled=True,
            llm_api_key="test-key",
            llm_base_url="https://api.example.com/v1",
            llm_model="test-model",
        )
    )

    class FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "choices": [{"message": {"content": '{"overview":"ok"}'}}],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 2,
                    "total_tokens": 22,
                    "prompt_tokens_details": {"cached_tokens": 12, "cache_creation_input_tokens": 5},
                },
            }

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def post(self, *args, **kwargs) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr("video_sum_core.pipeline.real.httpx.Client", FakeClient)

    result = runner._request_llm_json(
        base_url="https://api.example.com/v1",
        payload={"model": "test-model", "messages": []},
    )

    assert result["llm_prompt_tokens"] == 20
    assert result["llm_prompt_cache_hit_tokens"] == 12
    assert result["llm_prompt_cache_miss_tokens"] == 8
    assert result["llm_prompt_cache_creation_tokens"] == 5


def test_llm_summary_payload_disables_thinking_in_chat_template(tmp_path: Path) -> None:
    runner = RealPipelineRunner(PipelineSettings(tasks_dir=tmp_path, llm_model="test-model"))

    payload = runner._build_llm_summary_payload(
        title="标题",
        transcript_excerpt="[00:00] 内容",
        segments_excerpt='[{"start":0,"text":"内容"}]',
    )

    assert payload["response_format"] == {"type": "json_object"}
    assert payload["enable_thinking"] is False
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}


def test_full_summary_context_uses_canonical_transcript_and_anchor_only_segments(
    tmp_path: Path,
) -> None:
    runner = RealPipelineRunner(
        PipelineSettings(
            tasks_dir=tmp_path,
            llm_enabled=True,
            llm_api_key="test-key",
            llm_base_url="https://example.com/v1",
            llm_model="test-model",
            summary_context_mode="full",
        )
    )
    captured: dict[str, object] = {}

    def fake_request(**kwargs: object) -> dict[str, object]:
        captured["payload"] = kwargs["payload"]
        return {"title": "标题", "overview": "概览", "bulletPoints": [], "chapters": []}

    runner._request_llm_json = fake_request  # type: ignore[method-assign]
    summary = runner._summarize_with_llm(
        transcript="旧转写不应作为第二份正文发送",
        segments=[
            {"start": 0, "end": 2, "text": "第一段正文"},
            {"start": 2, "end": 4, "text": "第二段正文"},
        ],
        title="标题",
        emit=lambda *_args, **_kwargs: None,
    )

    assert summary["overview"] == "概览"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    user_message = next(message for message in payload["messages"] if message["role"] == "user")
    assert "[00:00] 第一段正文" in user_message["content"]
    assert "[00:02] 第二段正文" in user_message["content"]
    assert user_message["content"].count("第二段正文") == 1
    assert '"text"' not in user_message["content"].split("分段数据节选：", 1)[-1]


def test_auto_summary_context_falls_back_to_chunks_when_transcript_is_long(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = RealPipelineRunner(
        PipelineSettings(
            tasks_dir=tmp_path,
            llm_enabled=True,
            llm_api_key="test-key",
            llm_base_url="https://example.com/v1",
            llm_model="test-model",
            summary_context_mode="auto",
            summary_full_context_max_chars=800,
            summary_chunk_target_chars=800,
        )
    )
    calls: list[str] = []

    def fake_chunk(*args: object, **kwargs: object) -> dict[str, object]:
        calls.append("chunk")
        chunk = args[2]
        assert isinstance(chunk, dict)
        return {"chunk_index": chunk["index"], "overview": "分块"}

    monkeypatch.setattr(runner, "_request_llm_summary_chunk", fake_chunk)
    monkeypatch.setattr(
        runner,
        "_request_llm_json",
        lambda **kwargs: (calls.append("aggregate") or {"overview": "合并"}),
    )
    segments = [{"start": index, "end": index + 1, "text": "长字幕内容" * 100} for index in range(3)]

    result = runner._summarize_with_llm(
        transcript="",
        segments=segments,
        title="长视频",
        emit=lambda *_args, **_kwargs: None,
    )

    assert result["overview"] == "合并"
    assert "chunk" in calls
    assert "aggregate" in calls


def test_llm_json_request_accepts_anthropic_messages_response(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = RealPipelineRunner(
        PipelineSettings(
            tasks_dir=tmp_path,
            llm_enabled=True,
            llm_provider="anthropic",
            llm_api_key="test-key",
            llm_base_url="https://api.anthropic.com/v1",
            llm_model="claude-3-5-haiku-latest",
        )
    )
    calls: list[dict[str, object]] = []

    class FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "content": [{"type": "text", "text": '{"overview":"ok"}'}],
                "usage": {
                    "input_tokens": 6,
                    "output_tokens": 2,
                    "cache_read_input_tokens": 4,
                    "cache_creation_input_tokens": 3,
                },
            }

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def post(self, url: str, headers: dict[str, str], json: dict[str, object]) -> FakeResponse:
            calls.append({"url": url, "headers": headers, "json": json})
            return FakeResponse()

    monkeypatch.setattr("video_sum_core.pipeline.real.httpx.Client", FakeClient)

    result = runner._request_llm_json(
        base_url="https://api.anthropic.com/v1",
        payload={
            "model": "claude-3-5-haiku-latest",
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "user"},
            ],
            "response_format": {"type": "json_object"},
        },
    )

    assert result["overview"] == "ok"
    assert calls[0]["url"] == "https://api.anthropic.com/v1/messages"
    assert calls[0]["headers"]["x-api-key"] == "test-key"
    assert calls[0]["headers"]["anthropic-version"] == "2023-06-01"
    assert calls[0]["json"]["system"] == "system"
    assert result["llm_prompt_tokens"] == 13
    assert result["llm_prompt_cache_hit_tokens"] == 4
    assert result["llm_prompt_cache_miss_tokens"] == 9
    assert result["llm_prompt_cache_creation_tokens"] == 3
    assert "response_format" not in calls[0]["json"]


def test_export_transcript_snapshot_creates_resummary_artifacts(tmp_path: Path) -> None:
    runner = RealPipelineRunner(PipelineSettings(tasks_dir=tmp_path))

    result = runner._export_transcript_snapshot(
        tmp_path,
        "示例标题",
        "[00:00] 第一条",
        [{"start": 0, "end": 1, "text": "第一条"}],
    )

    assert result.transcript_text == "[00:00] 第一条"
    assert result.artifacts["transcript_path"].endswith("transcript.txt")
    summary_payload = json.loads(Path(result.artifacts["summary_path"]).read_text(encoding="utf-8"))
    assert summary_payload["title"] == "示例标题"
    assert summary_payload["summary"] == {}
    assert summary_payload["segments"][0]["text"] == "第一条"


def test_visual_frame_insert_mode_composes_note_without_vlm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = RealPipelineRunner(PipelineSettings(tasks_dir=tmp_path, visual_note_mode="frame_insert"))
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    describe_called = False

    monkeypatch.setattr(runner, "_prepare_visual_source", lambda task_input, task_dir, title: (source, "local_video", []))
    monkeypatch.setattr(
        runner,
        "_build_visual_keyframe_plan",
        lambda title, result, mode: {
            "schema_version": 1,
            "mode": mode,
            "planner": "test",
            "keyframes": [
                {
                    "timestamp_seconds": 12.0,
                    "anchor_heading": "主题",
                    "concept": "主题",
                    "reason": "截图能解释主题。",
                    "caption_hint": "主题截图",
                    "note_hint": "结合截图理解主题。",
                    "priority": 0.9,
                }
            ],
        },
    )
    monkeypatch.setattr(
        runner,
        "_extract_visual_frames",
        lambda source_path, timestamps, frames_dir: (
            [
                {
                    "frame_id": "f0001",
                    "timestamp_seconds": 12.0,
                    "timestamp": "00:12",
                    "file_name": "f0001.jpg",
                    "image_path": "frames/f0001.jpg",
                    "_absolute_path": str(frames_dir / "f0001.jpg"),
                    "sha256": "abc",
                }
            ],
            [],
        ),
    )

    def fail_describe(*args, **kwargs):
        nonlocal describe_called
        describe_called = True
        raise AssertionError("frame_insert mode must not call VLM description")

    monkeypatch.setattr(runner, "_visual_llm_available", lambda: True)
    monkeypatch.setattr(runner, "_describe_visual_frames", fail_describe)

    context, note_path, _ = runner.build_and_export_visual_evidence(
        task_id="task-frame",
        task_input=TaskInput(input_type=InputType.VIDEO_FILE, source=str(source), title="视频"),
        title="视频",
        result=TaskResult(
            knowledge_note_markdown="# 主题\n\n正文段落。",
            timeline=[{"title": "主题", "start": 12.0, "summary": "章节摘要"}],
        ),
        mode="frame_insert",
    )

    markdown = note_path.read_text(encoding="utf-8")
    assert context["mode"] == "frame_insert"
    assert context["insert_count"] == 1
    assert "visual://f0001" in markdown
    assert "结合截图理解主题。" in markdown
    assert "> 结合截图理解主题。" in markdown
    assert "## 图文笔记素材" not in markdown
    assert not describe_called


def test_visual_evidence_force_rebuild_clears_previous_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = RealPipelineRunner(PipelineSettings(tasks_dir=tmp_path, visual_note_mode="frame_insert"))
    visual_dir = tmp_path / "task-force" / "visual_evidence"
    frames_dir = visual_dir / "frames"
    frames_dir.mkdir(parents=True)
    stale_frame = frames_dir / "f9999.jpg"
    stale_frame.write_bytes(b"stale")

    monkeypatch.setattr(runner, "_prepare_visual_source", lambda task_input, task_dir, title: (None, "url_video", ["missing source"]))
    monkeypatch.setattr(
        runner,
        "_build_visual_keyframe_plan",
        lambda title, result, mode: {"schema_version": 1, "mode": mode, "keyframes": []},
    )

    context, _note_path, _ = runner.build_and_export_visual_evidence(
        task_id="task-force",
        task_input=TaskInput(input_type=InputType.URL, source="https://example.com/video", title="视频"),
        title="视频",
        result=TaskResult(knowledge_note_markdown="# 主题", timeline=[{"title": "主题", "start": 0.0}]),
        mode="frame_insert",
        force=True,
    )

    assert context["status"] == "unsupported"
    assert not stale_frame.exists()


def test_visual_vlm_integrated_mode_uses_observations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = RealPipelineRunner(PipelineSettings(tasks_dir=tmp_path, visual_note_mode="vlm_integrated"))
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    describe_called = False

    monkeypatch.setattr(runner, "_prepare_visual_source", lambda task_input, task_dir, title: (source, "local_video", []))
    monkeypatch.setattr(
        runner,
        "_build_visual_keyframe_plan",
        lambda title, result, mode: {
            "schema_version": 1,
            "mode": mode,
            "planner": "test",
            "keyframes": [
                {
                    "timestamp_seconds": 42.0,
                    "anchor_heading": "任务链",
                    "concept": "三层任务链",
                    "reason": "截图能说明任务链结构。",
                    "caption_hint": "任务链结构图",
                    "note_hint": "结合结构图理解任务链。",
                    "priority": 0.95,
                }
            ],
        },
    )
    monkeypatch.setattr(
        runner,
        "_extract_visual_frames",
        lambda source_path, timestamps, frames_dir: (
            [
                {
                    "frame_id": "f0001",
                    "timestamp_seconds": 42.0,
                    "timestamp": "00:42",
                    "file_name": "f0001.jpg",
                    "image_path": "frames/f0001.jpg",
                    "_absolute_path": str(frames_dir / "f0001.jpg"),
                    "sha256": "abc",
                }
            ],
            [],
        ),
    )
    monkeypatch.setattr(runner, "_visual_llm_available", lambda: True)

    def fake_describe(*args, **kwargs):
        nonlocal describe_called
        describe_called = True
        return [
            {
                "frame_id": "f0001",
                "timestamp_seconds": 42.0,
                "caption": "模型解析的结构图",
                "semantic_summary": "图中展示三层任务链。",
                "key_facts": ["这张结构图说明任务链如何串联。"],
                "should_insert": True,
                "importance": 0.9,
                "suggested_anchor": "任务链",
            }
        ]

    monkeypatch.setattr(runner, "_describe_visual_frames", fake_describe)

    context, note_path, _ = runner.build_and_export_visual_evidence(
        task_id="task-vlm",
        task_input=TaskInput(input_type=InputType.VIDEO_FILE, source=str(source), title="视频"),
        title="视频",
        result=TaskResult(
            knowledge_note_markdown="# 任务链\n\n正文段落。",
            timeline=[{"title": "任务链", "start": 42.0, "summary": "章节摘要"}],
        ),
        mode="vlm_integrated",
    )

    markdown = note_path.read_text(encoding="utf-8")
    assert describe_called
    assert context["mode"] == "vlm_integrated"
    assert context["insert_count"] == 1
    assert "模型解析的结构图" in markdown
    assert "这张结构图说明任务链如何串联。" in markdown
    assert "visual://f0001" in markdown


def test_visual_keyframe_plan_prefers_llm_selected_timestamps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = RealPipelineRunner(
        PipelineSettings(
            tasks_dir=tmp_path,
            visual_note_mode="frame_insert",
            llm_enabled=True,
            llm_api_key="key",
            llm_base_url="https://llm.example/v1",
            llm_model="test-model",
        )
    )
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    captured_timestamps: list[float] = []

    monkeypatch.setattr(runner, "_prepare_visual_source", lambda task_input, task_dir, title: (source, "local_video", []))
    monkeypatch.setattr(
        runner,
        "_request_llm_json",
        lambda *args, **kwargs: {
            "keyframes": [
                {
                    "timestamp_seconds": 88.0,
                    "anchor_heading": "关键设置",
                    "concept": "插件配置界面",
                    "reason": "这里出现了需要截图理解的设置界面。",
                    "caption_hint": "插件配置界面",
                    "note_hint": "截图展示了配置入口。",
                    "priority": 0.91,
                }
            ]
        },
    )

    def fake_extract(source_path, timestamps, frames_dir):
        captured_timestamps.extend(timestamps)
        return (
            [
                {
                    "frame_id": "f0001",
                    "timestamp_seconds": float(timestamps[0]),
                    "timestamp": "01:28",
                    "file_name": "f0001.jpg",
                    "image_path": "frames/f0001.jpg",
                    "_absolute_path": str(frames_dir / "f0001.jpg"),
                    "sha256": "abc",
                }
            ],
            [],
        )

    monkeypatch.setattr(runner, "_extract_visual_frames", fake_extract)

    context, note_path, _ = runner.build_and_export_visual_evidence(
        task_id="task-plan",
        task_input=TaskInput(input_type=InputType.VIDEO_FILE, source=str(source), title="视频"),
        title="视频",
        result=TaskResult(
            knowledge_note_markdown="# 关键设置\n\n正文段落。",
            timeline=[{"title": "开头", "start": 0.0, "summary": "不是关键设置"}],
        ),
        mode="frame_insert",
    )

    assert captured_timestamps == [88.0]
    assert context["insert_count"] == 1
    assert "截图展示了配置入口。" in note_path.read_text(encoding="utf-8")
    plan_path = tmp_path / "task-plan" / "visual_evidence" / "visual_keyframe_plan.json"
    assert json.loads(plan_path.read_text(encoding="utf-8"))["planner"] == "llm"


def test_visual_frame_planning_prompt_renders_default_variables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = RealPipelineRunner(
        PipelineSettings(
            tasks_dir=tmp_path,
            visual_note_mode="frame_insert",
            llm_enabled=True,
            llm_api_key="key",
            llm_base_url="https://llm.example/v1",
            llm_model="test-model",
            visual_evidence_max_frames=4,
        )
    )
    captured_prompt = ""

    def fake_request(*, base_url, payload, timeout, retry_count, **_kwargs):
        nonlocal captured_prompt
        captured_prompt = str(payload["messages"][1]["content"])
        return {
            "keyframes": [
                {
                    "timestamp_seconds": 12.0,
                    "anchor_heading": "关键设置",
                    "concept": "配置界面",
                    "reason": "解释设置入口",
                    "caption_hint": "设置入口",
                    "note_hint": "截图展示入口",
                    "priority": 0.9,
                }
            ]
        }

    monkeypatch.setattr(runner, "_request_llm_json", fake_request)

    plan = runner._build_visual_keyframe_plan_with_llm(
        "视频",
        TaskResult(
            overview="概览",
            knowledge_note_markdown="# 关键设置\n\n正文",
            timeline=[{"title": "关键设置", "start": 12.0, "summary": "章节摘要"}],
        ),
        "frame_insert",
    )

    assert plan["planner"] == "llm"
    assert "{max_frames}" not in captured_prompt
    assert "最多选择 4 张" in captured_prompt
    assert '"start": 12.0' in captured_prompt


def test_visual_keyframe_plan_accepts_legacy_frames_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = RealPipelineRunner(
        PipelineSettings(
            tasks_dir=tmp_path,
            visual_note_mode="frame_insert",
            llm_enabled=True,
            llm_api_key="key",
            llm_base_url="https://llm.example/v1",
            llm_model="test-model",
        )
    )
    monkeypatch.setattr(
        runner,
        "_request_llm_json",
        lambda *args, **kwargs: {
            "frames": [
                {
                    "seconds": 18.0,
                    "reason": "出现配置画面",
                    "expected_visual_type": "settings",
                    "importance": 4,
                }
            ]
        },
    )

    plan = runner._build_visual_keyframe_plan_with_llm(
        "视频",
        TaskResult(knowledge_note_markdown="# 配置\n\n正文", timeline=[{"title": "配置", "start": 18.0}]),
        "frame_insert",
    )

    assert plan["planner"] == "llm"
    assert plan["keyframes"][0]["timestamp_seconds"] == 18.0
    assert plan["keyframes"][0]["priority"] == 0.8


def test_visual_note_rejects_image_gallery_model_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = RealPipelineRunner(PipelineSettings(tasks_dir=tmp_path, visual_note_mode="vlm_integrated"))
    monkeypatch.setattr(runner, "_visual_llm_available", lambda: True)
    monkeypatch.setattr(runner, "_compose_visual_note_with_llm", lambda *args, **kwargs: "![图](visual://f0001)")

    note = runner._compose_visual_enhanced_note(
        title="视频",
        result=TaskResult(knowledge_note_markdown="# 文本主体\n\n这里有一段完整的文字说明，图片应该只是补充。"),
        observations=[{"frame_id": "f0001", "timestamp_seconds": 1, "caption": "图"}],
        insert_plan={
            "insertions": [
                {
                    "frame_id": "f0001",
                    "markdown_image": "visual://f0001",
                    "alt": "图",
                    "anchor_heading": "文本主体",
                    "explanation": "图片只补充说明这段文字。",
                }
            ]
        },
        mode="vlm_integrated",
    )

    assert note.startswith("# 文本主体")
    assert "![图](visual://f0001)" in note
    assert "> 图片只补充说明这段文字。" in note
