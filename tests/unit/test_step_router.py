"""StepRouter behaviour: routing gate, AgentSignal pass-through, batch order, guard, seams."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
import video_sum_service.runtime_support as runtime_support
from service_seam import routed_fake, service_seam
from video_sum_core.models.tasks import InputType, TaskInput, TaskOptions, TaskResult
from video_sum_core.pipeline.base import PipelineContext, PipelineTranscription
from video_sum_core.pipeline.model_steps import (
    STAGES,
    AgentContentRejected,
    AgentSignal,
    AgentWorkRequired,
    StepRouter,
)
from video_sum_core.pipeline.real import PipelineSettings, RealPipelineRunner
from video_sum_infra.config import ServiceSettings
from video_sum_infra.model_client import ModelRequest

SUMMARY = {
    "title": "标题",
    "overview": "概览",
    "bulletPoints": ["要点"],
    "chapters": [{"title": "开场", "start": 0, "summary": "开场介绍"}],
}
THIN_MINDMAP = {
    "title": "导图",
    "root": "root",
    "nodes": [
        {
            "id": "root",
            "label": "导图",
            "type": "root",
            "children": [{"id": "t1", "label": "唯一", "type": "theme"}],
        }
    ],
}
GOOD_REPLIES: dict[str, object] = {
    "llm": {"ok": True},
    "summary_full": SUMMARY,
    "summary_chunk": SUMMARY,
    "summary_merge": SUMMARY,
    "aggregate_summary": SUMMARY,
    "knowledge_note": {"knowledgeNoteMarkdown": "# 笔记\n\n## 开场\n\n内容"},
    "mindmap": THIN_MINDMAP,
    "mindmap_repair": THIN_MINDMAP,
    "visual_keyframe_plan": {"keyframes": [{"timestamp_seconds": 3, "concept": "界面"}]},
    "visual_frame": {"caption": "画面"},
    "visual_note_compose": "# 图文\n\n![图](visual://f0001)\n" + "正文" * 200,
}
SEGMENTS = [{"start": 0, "end": 5, "text": "第一段"}, {"start": 5, "end": 9, "text": "第二段"}]


def _settings(tmp_path: Path, **overrides: object) -> PipelineSettings:
    values: dict[str, object] = {
        "tasks_dir": tmp_path / "tasks",
        "llm_enabled": True,
        "llm_api_key": "test-key",
        "llm_base_url": "https://llm.example.com/v1",
        "llm_model": "test-model",
        "summary_context_mode": "full",
        "visual_evidence_use_llm": True,
        "visual_multimodal_enabled": True,
        "visual_evidence_base_url": "https://vlm.example.com/v1",
        "visual_evidence_model": "vision-model",
        "visual_evidence_api_key": "vision-key",
    }
    values.update(overrides)
    return PipelineSettings(**values)  # type: ignore[arg-type]


def _reply_by_stage(failing_stage: str | None = None, error: BaseException | None = None):
    def reply(request: ModelRequest):
        if request.usage_stage == failing_stage:
            raise error  # type: ignore[misc]
        return GOOD_REPLIES[request.usage_stage]

    return reply


def _visual_runner(
    tmp_path: Path, fake, monkeypatch: pytest.MonkeyPatch, **overrides: object
) -> RealPipelineRunner:
    runner = RealPipelineRunner(_settings(tmp_path, **overrides), model_client=fake)
    image = tmp_path / "f0001.jpg"
    image.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")

    def frames(_source, _timestamps, _frames_dir):
        frame = {
            "frame_id": "f0001",
            "timestamp_seconds": 3.0,
            "timestamp": "00:03",
            "_absolute_path": str(image),
        }
        return [frame], []

    monkeypatch.setattr(runner, "_extract_visual_frames", frames)
    return runner


def _run_summary(runner: RealPipelineRunner, tmp_path: Path) -> TaskResult:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    task_dir = tmp_path / "tasks" / "t1"
    task_dir.mkdir(parents=True, exist_ok=True)
    context = PipelineContext(
        task_id="t1",
        task_input=TaskInput(
            input_type=InputType.VIDEO_FILE,
            source=str(video),
            options=TaskOptions(visual_note_mode="vlm_integrated"),
        ),
    )
    transcription = PipelineTranscription(
        title="标题",
        transcript="第一段\n第二段",
        segments=SEGMENTS,
        task_dir=task_dir,
        source_path=None,
    )
    return runner.run_summary(context, transcription)


# -- AgentSignal is never swallowed ------------------------------------------------
def test_default_run_summary_reaches_every_summary_and_visual_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = routed_fake(default=_reply_by_stage())
    result = _run_summary(_visual_runner(tmp_path, fake, monkeypatch), tmp_path)

    assert result.visual_note_status == "ready"
    assert fake.stages == [
        "summary_full",
        "knowledge_note",
        "visual_keyframe_plan",
        "visual_frame",
        "visual_note_compose",
    ]


SUMMARY_UNIT_STAGES = [
    "summary_full",
    "knowledge_note",
    "visual_keyframe_plan",
    "visual_frame",
    "visual_note_compose",
]


@pytest.mark.parametrize("signal", [AgentWorkRequired, AgentContentRejected])
@pytest.mark.parametrize("stage", SUMMARY_UNIT_STAGES)
def test_agent_signal_escapes_summary_and_inline_visual_note(
    stage: str, signal: type[AgentSignal], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = routed_fake(default=_reply_by_stage(stage, signal(stage)))
    runner = _visual_runner(
        tmp_path, fake, monkeypatch, summary_chunk_retry_count=3, visual_evidence_retry_count=2
    )

    with pytest.raises(signal):
        _run_summary(runner, tmp_path)

    assert len(fake.calls_for(stage)) == 1  # never retried


@pytest.mark.parametrize("stage", ["summary_chunk", "summary_merge"])
def test_agent_signal_escapes_chunked_summary(stage: str, tmp_path: Path) -> None:
    fake = routed_fake(default=_reply_by_stage(stage, AgentWorkRequired(stage)))
    runner = RealPipelineRunner(
        _settings(tmp_path, summary_context_mode="chunked"), model_client=fake
    )

    with pytest.raises(AgentWorkRequired):
        runner._summarize(
            "第一段\n第二段", SEGMENTS, "标题", lambda *_a, **_k: None, summary_scope="summary"
        )

    assert len(fake.calls_for(stage)) == 1


@pytest.mark.parametrize("stage", ["mindmap", "mindmap_repair"])
def test_agent_signal_escapes_mindmap(stage: str, tmp_path: Path) -> None:
    fake = routed_fake(default=_reply_by_stage(stage, AgentWorkRequired(stage)))
    runner = RealPipelineRunner(_settings(tmp_path), model_client=fake)
    result = TaskResult(overview="概览", knowledge_note_markdown="# 笔记")

    with pytest.raises(AgentWorkRequired):
        runner.build_and_export_mindmap("t1", "标题", result)

    assert len(fake.calls_for(stage)) == 1


def test_agent_signals_are_not_video_sum_errors() -> None:
    from video_sum_core.errors import VideoSumError

    assert not issubclass(AgentSignal, VideoSumError)
    assert issubclass(AgentWorkRequired, AgentSignal) and issubclass(
        AgentContentRejected, AgentSignal
    )


# -- guard: model calls only through the router ------------------------------------
def test_routed_fake_rejects_calls_that_bypass_the_router() -> None:
    fake = routed_fake(default="{}")
    request = ModelRequest(
        usage_stage="summary_full", base_url="https://x", api_key="k", payload={"messages": []}
    )

    with pytest.raises(AssertionError, match="bypassed StepRouter"):
        fake.chat(request)
    assert fake.requests == []


def test_every_registered_stage_belongs_to_a_registered_step() -> None:
    router = StepRouter(PipelineSettings(tasks_dir=Path(".")), routed_fake())
    for spec in STAGES.values():
        assert router.resolve(spec.step) == "local"  # nothing configured -> local everywhere


# -- run_many and chunk ordering -----------------------------------------------------
def _frame_request(label: str) -> ModelRequest:
    return ModelRequest(
        usage_stage="visual_frame",
        base_url="https://vlm.example.com/v1",
        api_key="k",
        payload={"model": "m", "messages": [{"role": "user", "content": label}]},
    )


def test_run_many_returns_results_in_input_order_with_logical_indices(tmp_path: Path) -> None:
    def reply(request: ModelRequest):
        label = request.payload["messages"][0]["content"]
        if label == "broken":
            raise ValueError("bad frame")
        return {"caption": label}

    fake = routed_fake(default=reply)
    router = StepRouter(_settings(tmp_path, visual_evidence_retry_count=1), fake)
    labels = ["c", "broken", "a"]

    with router.recording() as log:
        results = router.run_many(
            "visual_frame",
            [_frame_request(label) for label in labels],
            indices=[7, 3, 5],
            fallback=lambda exc, index: f"fallback#{index}",
        )

    singles = [{"caption": "c"}, "fallback#3", {"caption": "a"}]
    assert results == singles
    assert [(r.index, r.source) for r in log.records] == [
        (7, "internal"),
        (3, "local"),
        (5, "internal"),
    ]


def test_chunk_results_keep_their_logical_index_when_completion_order_is_shuffled(
    tmp_path: Path,
) -> None:
    started = threading.Barrier(3, timeout=5)

    def reply(request: ModelRequest):
        user = next(m["content"] for m in request.payload["messages"] if m["role"] == "user")
        chunk = int(user.split("分块 ")[1].split()[0][0])
        started.wait()
        time.sleep(0.05 * (4 - chunk))  # chunk 3 finishes first, chunk 1 last
        return {
            "overview": f"chunk-{chunk}",
            "chapters": [{"title": f"章{chunk}", "start": chunk * 30, "summary": "s"}],
        }

    fake = routed_fake(by_stage={"summary_merge": [SUMMARY]}, default=reply)
    runner = RealPipelineRunner(
        _settings(
            tmp_path,
            summary_context_mode="chunked",
            summary_chunk_target_chars=800,
            summary_chunk_concurrency=3,
        ),
        model_client=fake,
    )
    segments = [
        {"start": i * 30, "end": i * 30 + 30, "text": f"第{i}段" + "内容" * 300} for i in range(3)
    ]

    with runner._router.recording() as log:
        runner._summarize("", segments, "长视频", lambda *_a, **_k: None, summary_scope="summary")

    merge_user = next(
        m["content"]
        for m in fake.calls_for("summary_merge")[0].payload["messages"]
        if m["role"] == "user"
    )
    assert merge_user.index("chunk-1") < merge_user.index("chunk-2") < merge_user.index("chunk-3")
    chunk_records = sorted(r.index for r in log.records if r.usage_stage == "summary_chunk")
    assert chunk_records == [1, 2, 3]
    assert log.sources() == {"summary": "internal"}


# -- preflight probes only when a step routes to the internal model ----------------
def test_preflight_does_not_probe_when_no_step_routes_to_internal_model(
    tmp_path: Path, monkeypatch
) -> None:
    fake = routed_fake()  # no script: any call fails the test
    with service_seam(monkeypatch, tmp_path, fake, llm_enabled=False) as seam:
        detail = seam.wait_for_terminal(seam.create_transcript_task())

    assert detail["status"] == "completed"
    assert fake.requests == []


def test_preflight_probes_through_the_router_when_llm_is_configured(
    tmp_path: Path, monkeypatch
) -> None:
    fake = routed_fake(default=_reply_by_stage())
    with service_seam(monkeypatch, tmp_path, fake) as seam:
        task_id = seam.create_transcript_task()
        detail = seam.wait_for_terminal(task_id)

    assert detail["status"] == "completed", detail
    assert fake.stages[:3] == ["llm", "summary_full", "knowledge_note"]
    assert fake.requests[0].timeout == 20


# -- build_worker injection seam ----------------------------------------------------
def test_build_worker_hands_the_injected_model_client_to_the_pipeline(
    tmp_path: Path, monkeypatch
) -> None:
    for name in (
        "bootstrap_managed_runtime",
        "prepend_runtime_path",
        "activate_runtime_pythonpath",
    ):
        monkeypatch.setattr(runtime_support, name, lambda *_a, **_k: None)
    fake = routed_fake(default=_reply_by_stage())
    monkeypatch.setattr(runtime_support, "PIPELINE_MODEL_CLIENT", fake)
    settings = ServiceSettings(
        data_dir=tmp_path,
        tasks_dir=tmp_path / "tasks",
        llm_enabled=True,
        llm_api_key="sk-test",
        llm_base_url="http://llm.invalid/v1",
        llm_model="test-model",
        summary_context_mode="full",
        auto_generate_mindmap=False,
        knowledge_enabled=False,
    )

    with service_seam(monkeypatch, tmp_path, fake) as seam:
        worker = runtime_support.build_worker(seam.repository, settings, environment_info={})
        seam.worker.shutdown(wait=True, timeout=5)
        seam.client.app.state.task_worker = worker
        try:
            detail = seam.wait_for_terminal(seam.create_transcript_task())
        finally:
            worker.shutdown(wait=True, timeout=10)

    assert detail["status"] == "completed", detail
    assert "summary_full" in fake.stages
