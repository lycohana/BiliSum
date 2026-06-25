from __future__ import annotations

import base64
import hashlib
import glob
import json
import logging
import math
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, NoReturn

import httpx
from video_sum_infra.config import (
    DEFAULT_KNOWLEDGE_NOTE_SYSTEM_PROMPT,
    DEFAULT_KNOWLEDGE_NOTE_USER_PROMPT_TEMPLATE,
    DEFAULT_SUMMARY_SYSTEM_PROMPT,
    DEFAULT_SUMMARY_USER_PROMPT_TEMPLATE,
    normalize_visual_note_mode,
)
from video_sum_infra.llm import (
    ANTHROPIC_API_VERSION,
    anthropic_messages_url,
    build_anthropic_messages_payload,
    extract_llm_message_content,
    is_anthropic_llm,
    normalize_openai_compatible_model_name,
    openai_chat_completions_url,
)
from video_sum_infra.prompt_library import load_presets
from video_sum_infra.runtime import (
    ffmpeg_location,
    runtime_library_dirs,
    runtime_python_executable,
    runtime_pythonpath_dirs,
    sanitized_subprocess_dll_search,
)

from video_sum_core.errors import (
    LLMAuthenticationError,
    LLMConfigurationError,
    PegasusAuthenticationError,
    PegasusConfigurationError,
    TranscriptionAuthenticationError,
    TranscriptionConfigurationError,
    UnsupportedInputError,
    VideoSumError,
)
from video_sum_core.twelvelabs import (
    DEFAULT_TWELVELABS_PROMPT,
    analyze_video_with_pegasus,
    video_context_from_file,
)
from video_sum_core.models.tasks import InputType, MindMapNode, TaskInput, TaskMindMap, TaskResult
from video_sum_core.pipeline.base import (
    PipelineContext,
    PipelineEvent,
    PipelineEventReporter,
    PipelineRunner,
)
from video_sum_core.utils import ensure_directory, normalize_video_url, sanitize_filename

logger = logging.getLogger("video_sum_core.pipeline.real")
YoutubeDL = None
DownloadError = None

_BILIBILI_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.bilibili.com/",
}


def _get_ytdlp_classes():
    global DownloadError, YoutubeDL
    if YoutubeDL is None:
        from yt_dlp import YoutubeDL as ImportedYoutubeDL

        YoutubeDL = ImportedYoutubeDL
    if DownloadError is None:
        from yt_dlp.utils import DownloadError as ImportedDownloadError

        DownloadError = ImportedDownloadError
    return YoutubeDL, DownloadError


def _windows_hidden_subprocess_kwargs() -> dict[str, object]:
    if os.name != "nt":
        return {}

    kwargs: dict[str, object] = {}
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if creationflags:
        kwargs["creationflags"] = creationflags

    startupinfo_cls = getattr(subprocess, "STARTUPINFO", None)
    use_show_window = getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
    sw_hide = getattr(subprocess, "SW_HIDE", 0)
    if startupinfo_cls is not None:
        startupinfo = startupinfo_cls()
        startupinfo.dwFlags |= use_show_window
        startupinfo.wShowWindow = sw_hide
        kwargs["startupinfo"] = startupinfo

    return kwargs


def _safe_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _truncate_text(value: str, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit]


def _extract_response_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return _truncate_text(response.text.strip(), 400)

    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            code = error.get("code")
            if message and code:
                return f"{code}: {message}"
            if message:
                return str(message)
        message = payload.get("message")
        if message:
            return str(message)
    return _truncate_text(json.dumps(payload, ensure_ascii=False), 400)


def _extract_json_object_text(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text


def _should_retry_llm_transport_error(error: Exception) -> bool:
    return isinstance(
        error,
        (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.ReadError,
            httpx.WriteError,
            httpx.CloseError,
            httpx.ProxyError,
            httpx.RemoteProtocolError,
            httpx.NetworkError,
            httpx.ProtocolError,
            httpx.TransportError,
        ),
    )


@dataclass(slots=True)
class PipelineSettings:
    tasks_dir: Path
    data_dir: Path | str | None = None
    runtime_channel: str = "base"
    transcription_provider: str = "siliconflow"
    prefer_bilibili_subtitle: bool = True
    whisper_model: str = "tiny"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    local_asr_available: bool = False
    siliconflow_asr_base_url: str = "https://api.siliconflow.cn/v1"
    siliconflow_asr_model: str = "TeleAI/TeleSpeechASR"
    siliconflow_asr_api_key: str = ""
    siliconflow_asr_chunk_duration_seconds: int = 1800
    siliconflow_asr_concurrency: int = 2
    multimodal_asr_base_url: str = ""
    multimodal_asr_model: str = "mimo-v2-omni"
    multimodal_asr_api_key: str = ""
    multimodal_asr_chunk_duration_seconds: int = 180
    multimodal_asr_max_retries: int = 5
    funasr_model: str = "paraformer-zh"
    funasr_device: str = "cpu"
    funasr_vad_model: str = "fsmn-vad"
    funasr_punc_model: str = "ct-punc"
    funasr_spk_model: str = ""
    funasr_hub: str = "ms"
    funasr_hotword: str = ""
    funasr_available: bool = False
    llm_enabled: bool = False
    llm_provider: str = "openai-compatible"
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""
    visual_evidence_enabled: bool = False
    visual_note_mode: str = "text"
    visual_multimodal_enabled: bool = False
    visual_download_resolution: str = "720p"
    visual_evidence_use_llm: bool = True
    visual_vlm_provider: str = "openai-compatible"
    visual_evidence_base_url: str = ""
    visual_evidence_model: str = ""
    visual_evidence_api_key: str = ""
    visual_evidence_max_frames: int = 12
    visual_evidence_frame_interval_seconds: int = 10
    visual_evidence_frame_width: int = 960
    visual_evidence_vlm_image_width: int = 768
    visual_evidence_image_quality: int = 85
    visual_evidence_timeout_seconds: int = 120
    visual_evidence_retry_count: int = 1
    twelvelabs_summary_enabled: bool = False
    twelvelabs_api_key: str = ""
    twelvelabs_model: str = "pegasus1.5"
    twelvelabs_base_url: str = "https://api.twelvelabs.io/v1.3"
    twelvelabs_prompt: str = ""
    visual_note_system_prompt: str = ""
    visual_note_user_prompt_template: str = ""
    visual_frame_planning_prompt: str = ""
    visual_vlm_prompt: str = ""
    summary_system_prompt: str = ""
    summary_user_prompt_template: str = ""
    prompt_presets_path: str = ""
    knowledge_note_system_prompt: str = ""
    knowledge_note_user_prompt_template: str = ""
    mindmap_system_prompt: str = ""
    mindmap_user_prompt_template: str = ""
    summary_chunk_target_chars: int = 2200
    summary_chunk_overlap_segments: int = 2
    summary_chunk_concurrency: int = 2
    summary_chunk_retry_count: int = 2
    ytdlp_cookies_file: str = ""
    ytdlp_cookies_browser: str = ""


class RealPipelineRunner(PipelineRunner):
    def __init__(self, settings: PipelineSettings) -> None:
        self._settings = settings
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def preflight(
        self,
        context: PipelineContext,
        on_event: PipelineEventReporter | None = None,
    ) -> None:
        task_input = context.task_input

        def emit(stage: str, progress: int, message: str, payload: dict[str, object] | None = None) -> None:
            if on_event is None:
                return
            on_event(PipelineEvent(stage=stage, progress=progress, message=message, payload=payload or {}))

        if not (self._settings.llm_enabled and str(self._settings.llm_api_key or "").strip()):
            return

        if task_input.input_type not in {
            InputType.URL,
            InputType.VIDEO_FILE,
            InputType.AUDIO_FILE,
            InputType.TRANSCRIPT_TEXT,
        }:
            return

        emit("preflight", 2, "正在检查 LLM API 是否可用")
        self._preflight_llm_availability()
        emit("preflight", 4, "LLM API 检查通过")

    def run(
        self,
        context: PipelineContext,
        on_event: PipelineEventReporter | None = None,
    ) -> tuple[list[PipelineEvent], TaskResult]:
        task_input = context.task_input

        events: list[PipelineEvent] = []

        def emit(stage: str, progress: int, message: str, payload: dict[str, object] | None = None) -> None:
            event = PipelineEvent(stage=stage, progress=progress, message=message, payload=payload or {})
            events.append(event)
            logger.info(
                "pipeline event task_id=%s stage=%s progress=%s message=%s payload=%s",
                context.task_id,
                stage,
                progress,
                message,
                event.payload,
            )
            if on_event is not None:
                on_event(event)

        logger.info(
            "pipeline run start task_id=%s input_type=%s source=%s",
            context.task_id,
            task_input.input_type.value,
            task_input.source,
        )

        if task_input.input_type is InputType.URL:
            result = self._run_from_url(context, emit)
        elif task_input.input_type in {InputType.VIDEO_FILE, InputType.AUDIO_FILE}:
            result = self._run_from_local_media(context, emit)
        elif task_input.input_type is InputType.TRANSCRIPT_TEXT:
            result = self._run_from_transcript_text(context, emit)
        else:
            raise UnsupportedInputError(
                f"Current runner does not support input type '{task_input.input_type.value}'."
            )
        return events, result

    def _run_from_url(
        self,
        context: PipelineContext,
        emit: Callable[[str, int, str, dict[str, object] | None], None],
    ) -> TaskResult:
        task_input = context.task_input
        emit("preparing", 8, "正在规范化视频链接")
        normalized = normalize_video_url(task_input.source)
        normalized_url = normalized.normalized_url
        canonical_id = normalized.canonical_id
        if normalized.platform not in {"bilibili", "youtube"}:
            raise UnsupportedInputError("Current runner only supports Bilibili and YouTube video URLs.")

        task_dir = ensure_directory(self._settings.tasks_dir / context.task_id)
        emit("probing", 12, "正在读取视频信息", {"url": normalized_url})
        metadata = self._probe_video(normalized_url)
        title = task_input.title or metadata.get("title") or canonical_id or "video"
        safe_title = sanitize_filename(title)
        emit(
            "probing",
            16,
            "视频信息读取完成",
            {"title": title, "duration": metadata.get("duration")},
        )

        # 尝试获取 Bilibili 字幕
        transcript, segments = None, []
        logger.info(
            "bilibili subtitle check: prefer=%s platform=%s",
            self._settings.prefer_bilibili_subtitle,
            normalized.platform,
        )
        if self._settings.prefer_bilibili_subtitle and normalized.platform == "bilibili":
            emit("fetching_subtitle", 18, "正在尝试获取字幕")
            try:
                from video_sum_service.integrations import fetch_bilibili_subtitle

                # yt-dlp 返回的 B 站 metadata：
                # - id: BV 号（不是 aid）
                # - cid: 可能在 formats 或其他地方
                bvid = metadata.get("id")  # 这是 BV 号
                aid = None
                cid = None

                # 从 formats 中提取 cid（B 站特有）
                formats = metadata.get("formats", [])
                if formats and isinstance(formats, list):
                    for fmt in formats:
                        if isinstance(fmt, dict):
                            # B 站的 format URL 通常包含 cid 参数
                            fmt_url = fmt.get("url", "")
                            if "cid=" in fmt_url:
                                import re
                                match = re.search(r'cid=(\d+)', fmt_url)
                                if match:
                                    cid = int(match.group(1))
                                    break

                # 如果有 BV 号但没有 aid，从 API 获取
                if bvid and not aid:
                    try:
                        import httpx
                        with httpx.Client(timeout=5.0) as client:
                            resp = client.get(
                                f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
                                headers={"User-Agent": "Mozilla/5.0"}
                            )
                        if resp.status_code == 200:
                            data = resp.json()
                            if data.get("code") == 0:
                                aid = data.get("data", {}).get("aid")
                                if not cid:
                                    cid = data.get("data", {}).get("cid")
                    except Exception as e:
                        logger.debug("Failed to get aid/cid from API: %s", e)

                logger.info("Extracted bvid=%s aid=%s cid=%s", bvid, aid, cid)

                if aid and cid:
                    cookie = self._extract_cookie_for_bilibili()
                    subtitle_result = fetch_bilibili_subtitle(aid, cid, cookie, bvid=bvid)
                    if subtitle_result:
                        transcript = subtitle_result["transcript"]
                        segments = subtitle_result["segments"]
                        emit("fetching_subtitle", 52, f"已获取字幕，共 {len(segments)} 段，跳过转写")
                        logger.info(
                            "bilibili subtitle used task_id=%s segments=%d chars=%d",
                            context.task_id,
                            len(segments),
                            len(transcript),
                        )
                else:
                    logger.warning("bilibili metadata missing aid=%s cid=%s, fallback to ASR", aid, cid)
            except Exception as exc:
                logger.warning("bilibili subtitle fetch failed, fallback to ASR: %s", exc, exc_info=True)
                emit("fetching_subtitle", 20, "字幕获取失败，使用 ASR 转写")

        # 如果字幕未获取，使用原 ASR 流程
        if not transcript:
            audio_path = self._download_audio(normalized_url, task_dir, safe_title, emit)
            transcript, segments = self._transcribe(audio_path, metadata.get("duration"), emit)

        transcript_result = self._export_transcript_snapshot(task_dir, title, transcript, segments)
        emit(
            "transcribing",
            86,
            "转写完成，已保存可复用文本",
            {
                "result": transcript_result.model_dump(mode="json"),
                "result_scope": "transcript",
            },
        )
        summary = self._summarize(
            transcript,
            segments,
            title,
            emit,
            prompt_preset_id=task_input.options.prompt_preset_id,
        )
        emit("exporting", 97, "正在导出任务结果")
        result = self._export_result(task_dir, title, transcript, segments, summary)
        emit(
            "exporting",
            98,
            "纯文本知识笔记已写入，正在生成图文笔记",
            {
                "result": result.model_dump(mode="json"),
                "result_scope": "knowledge_note",
            },
        )
        result = self._build_inline_visual_note(context.task_id, task_input, task_dir, title, result, emit)
        emit("exporting", 99, "结果文件已写入本地目录")
        logger.info(
            "pipeline url run finish task_id=%s segments=%d transcript_chars=%d output_dir=%s",
            context.task_id,
            len(segments),
            len(transcript),
            task_dir,
        )
        return result

    def _run_from_transcript_text(
        self,
        context: PipelineContext,
        emit: Callable[[str, int, str, dict[str, object] | None], None],
    ) -> TaskResult:
        task_dir = ensure_directory(self._settings.tasks_dir / context.task_id)
        title, transcript, segments, source_kind = self._parse_transcript_payload(
            context.task_input.source,
            context.task_input.title,
        )
        is_aggregate_series = source_kind == "aggregate_series"
        emit(
            "preparing",
            12,
            "正在整理全集摘要素材" if is_aggregate_series else "正在复用已有转写内容",
            {"segment_count": len(segments), "source_kind": source_kind or "transcript"},
        )
        emit(
            "transcribing",
            32,
            "已跳过重新转写，直接复用分 P 摘要素材" if is_aggregate_series else "已跳过重新转写，直接复用当前版本文本",
            {"transcript_chars": len(transcript), "segment_count": len(segments)},
        )
        summary = self._summarize(
            transcript,
            segments,
            title,
            emit,
            source_kind=source_kind,
            prompt_preset_id=context.task_input.options.prompt_preset_id,
        )
        emit("exporting", 97, "正在导出新的摘要结果")
        result = self._export_result(task_dir, title, transcript, segments, summary)
        emit(
            "exporting",
            98,
            "纯文本知识笔记已写入，正在生成图文笔记",
            {
                "result": result.model_dump(mode="json"),
                "result_scope": "knowledge_note",
            },
        )
        result = self._build_inline_visual_note(context.task_id, context.task_input, task_dir, title, result, emit)
        emit("exporting", 99, "新的摘要结果已写入本地目录")
        logger.info(
            "pipeline transcript rerun finish task_id=%s segments=%d transcript_chars=%d output_dir=%s",
            context.task_id,
            len(segments),
            len(transcript),
            task_dir,
        )
        return result

    def _run_from_local_media(
        self,
        context: PipelineContext,
        emit: Callable[[str, int, str, dict[str, object] | None], None],
    ) -> TaskResult:
        task_input = context.task_input
        source_path = Path(str(task_input.source or "")).expanduser()
        if not source_path.exists() or not source_path.is_file():
            raise VideoSumError(f"Local media file does not exist: {source_path}")

        task_dir = ensure_directory(self._settings.tasks_dir / context.task_id)
        title = task_input.title or source_path.stem or source_path.name or "local_media"
        safe_title = sanitize_filename(title)
        is_video_file = task_input.input_type is InputType.VIDEO_FILE
        emit(
            "preparing",
            8,
            "正在读取本地视频文件" if is_video_file else "正在读取本地音频文件",
            {"path": str(source_path)},
        )
        audio_path = self._prepare_local_audio_source(
            source_path=source_path,
            task_dir=task_dir,
            safe_title=safe_title,
            input_type=task_input.input_type,
            emit=emit,
        )
        transcript, segments = self._transcribe(audio_path, None, emit)
        transcript_result = self._export_transcript_snapshot(task_dir, title, transcript, segments)
        emit(
            "transcribing",
            86,
            "转写完成，已保存可复用文本",
            {
                "result": transcript_result.model_dump(mode="json"),
                "result_scope": "transcript",
            },
        )
        pegasus_video = None
        if is_video_file and self._settings.twelvelabs_summary_enabled:
            pegasus_video = video_context_from_file(source_path)
        summary = self._summarize(
            transcript,
            segments,
            title,
            emit,
            prompt_preset_id=task_input.options.prompt_preset_id,
            pegasus_video=pegasus_video,
        )
        emit("exporting", 97, "正在导出任务结果")
        result = self._export_result(task_dir, title, transcript, segments, summary)
        emit(
            "exporting",
            98,
            "纯文本知识笔记已写入，正在生成图文笔记",
            {
                "result": result.model_dump(mode="json"),
                "result_scope": "knowledge_note",
            },
        )
        result = self._build_inline_visual_note(context.task_id, task_input, task_dir, title, result, emit)
        emit("exporting", 99, "结果文件已写入本地目录")
        logger.info(
            "pipeline local media run finish task_id=%s input_type=%s source=%s segments=%d transcript_chars=%d output_dir=%s",
            context.task_id,
            task_input.input_type.value,
            source_path,
            len(segments),
            len(transcript),
            task_dir,
        )
        return result

    def _prepare_local_audio_source(
        self,
        source_path: Path,
        task_dir: Path,
        safe_title: str,
        input_type: InputType,
        emit: Callable[[str, int, str, dict[str, object] | None], None],
    ) -> Path:
        target_path = task_dir / f"{safe_title}.mp3"
        if input_type is InputType.AUDIO_FILE and source_path.suffix.lower() == ".mp3":
            emit("downloading", 20, "正在复制本地音频文件")
            shutil.copy2(source_path, target_path)
            emit("downloading", 48, "音频文件已就绪")
            return target_path

        ffmpeg_exe = ffmpeg_location()
        if ffmpeg_exe is None:
            raise VideoSumError("FFmpeg is unavailable, cannot process local media files.")

        emit(
            "downloading",
            20,
            "正在提取本地视频音轨" if input_type is InputType.VIDEO_FILE else "正在转换本地音频格式",
            {"path": str(source_path)},
        )
        emit("downloading", 34, "正在写入可转写音频文件")
        try:
            result = subprocess.run(
                [
                    str(ffmpeg_exe),
                    "-y",
                    "-i",
                    str(source_path),
                    "-vn",
                    "-acodec",
                    "libmp3lame",
                    "-ab",
                    "192k",
                    str(target_path),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15 * 60,
                check=False,
                **_windows_hidden_subprocess_kwargs(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise VideoSumError("Failed to extract audio from local media file.") from exc
        if result.returncode != 0 or not target_path.exists():
            logger.error(
                "local media audio extraction failed source=%s returncode=%s stdout=%s stderr=%s",
                source_path,
                result.returncode,
                result.stdout.strip(),
                result.stderr.strip(),
            )
            raise VideoSumError("Failed to extract audio from local media file.")

        emit("downloading", 48, "音频文件已就绪")
        return target_path

    def _parse_transcript_payload(
        self,
        source: str,
        title_hint: str | None,
    ) -> tuple[str, str, list[dict[str, object]], str | None]:
        try:
            payload = json.loads(source)
        except json.JSONDecodeError as exc:
            raise VideoSumError("Transcript task payload is invalid JSON.") from exc

        if not isinstance(payload, dict):
            raise VideoSumError("Transcript task payload must be an object.")

        transcript = str(payload.get("transcript") or "").strip()
        if not transcript:
            raise VideoSumError("Transcript task payload is missing transcript content.")

        raw_segments = payload.get("segments") or []
        segments = self._coerce_transcript_segments(raw_segments)
        if not segments:
            raise VideoSumError("Transcript task payload is missing valid segments.")

        title = str(payload.get("title") or title_hint or "视频摘要").strip() or "视频摘要"
        source_kind = str(payload.get("source_kind") or "").strip() or None
        return title, transcript, segments, source_kind

    def _coerce_transcript_segments(self, value: object) -> list[dict[str, object]]:
        if not isinstance(value, list):
            return []
        segments: list[dict[str, object]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            start = item.get("start")
            end = item.get("end")
            try:
                start_value = float(start) if start is not None else 0.0
            except (TypeError, ValueError):
                start_value = 0.0
            try:
                end_value = float(end) if end is not None else start_value
            except (TypeError, ValueError):
                end_value = start_value
            segments.append({"start": start_value, "end": end_value, "text": text})
        return segments

    def _base_ydl_options(self) -> dict[str, object]:
        options: dict[str, object] = {
            "quiet": True,
            "no_warnings": True,
            "http_headers": _BILIBILI_HTTP_HEADERS,
            "retries": 3,
            "extractor_retries": 3,
            "fragment_retries": 3,
            "sleep_interval_requests": 1,
            "socket_timeout": 30,
        }
        cookie_file = str(
            self._settings.ytdlp_cookies_file
            or os.environ.get("VIDEO_SUM_YTDLP_COOKIES_FILE")
            or os.environ.get("YTDLP_COOKIES_FILE")
            or ""
        ).strip()
        if cookie_file:
            cookie_path = Path(cookie_file).expanduser()
            if cookie_path.exists() and cookie_path.is_file():
                options["cookiefile"] = str(cookie_path)
            else:
                logger.warning("yt-dlp cookie file does not exist: %s", cookie_path)
        return options

    def _raise_ydl_error(self, error: Exception) -> NoReturn:
        message = str(error)
        if "Could not copy Chrome cookie database" in message:
            raise VideoSumError(
                "无法读取 Chrome 登录态：yt-dlp 不能复制 Chrome Cookie 数据库。"
                "请通过 BiliSum 的 B 站登录窗口重新捕获登录态，或按教程导出 cookies.txt 并填写 yt-dlp Cookies 文件。"
            ) from error
        if "Failed to decrypt with DPAPI" in message:
            raise VideoSumError(
                "无法读取浏览器登录态：yt-dlp 解密 Windows 浏览器 Cookie 失败（DPAPI）。"
                "请通过 BiliSum 的 B 站登录窗口重新捕获登录态，或按教程导出 cookies.txt 并填写 yt-dlp Cookies 文件。"
            ) from error
        if "HTTP Error 412" in message and "BiliBili" in message:
            raise VideoSumError(
                "B 站返回 HTTP 412，当前请求可能被风控拦截。请稍后重试、切换网络/IP，"
                "或通过 BiliSum 的 B 站登录窗口捕获登录态；也可以填写 yt-dlp Cookies 文件 "
                "/ VIDEO_SUM_YTDLP_COOKIES_FILE。"
            ) from error
        raise VideoSumError(f"Failed to read or download video with yt-dlp: {message}") from error

    def _probe_video(self, url: str) -> dict:
        YoutubeDL, DownloadError = _get_ytdlp_classes()
        try:
            with YoutubeDL(self._base_ydl_options()) as ydl:
                info = ydl.extract_info(url, download=False)
        except DownloadError as exc:
            self._raise_ydl_error(exc)
        if not isinstance(info, dict):
            raise VideoSumError("Failed to probe video metadata.")
        return info

    def _download_audio(
        self,
        url: str,
        task_dir: Path,
        safe_title: str,
        emit: Callable[[str, int, str, dict[str, object] | None], None],
    ) -> Path:
        emit("downloading", 20, "正在连接音频源")
        output_template = str(task_dir / f"{safe_title}.%(ext)s")
        download_progress = {
            "value": 20,
            "last_emit_time": 0.0,
            "last_downloaded_bytes": 0,
            "last_message": "",
        }

        def progress_hook(data: dict[str, object]) -> None:
            status = str(data.get("status") or "")
            if status == "downloading":
                now = time.monotonic()
                total = data.get("total_bytes") or data.get("total_bytes_estimate")
                downloaded = int(data.get("downloaded_bytes") or 0)
                speed = data.get("speed")
                eta = data.get("eta")
                fragment_index = data.get("fragment_index")
                fragment_count = data.get("fragment_count")

                if total and downloaded:
                    ratio = max(0.0, min(1.0, float(downloaded) / float(total)))
                    progress = min(42, 22 + int(ratio * 20))
                    message = self._build_download_message(
                        downloaded=downloaded,
                        total=int(total),
                        speed=speed,
                        eta=eta,
                        fragment_index=fragment_index,
                        fragment_count=fragment_count,
                    )
                    should_emit = (
                        progress > download_progress["value"]
                        or now - float(download_progress["last_emit_time"]) >= 0.8
                        or abs(downloaded - int(download_progress["last_downloaded_bytes"])) >= 2 * 1024 * 1024
                    )
                    if should_emit:
                        download_progress["value"] = progress
                        download_progress["last_emit_time"] = now
                        download_progress["last_downloaded_bytes"] = downloaded
                        download_progress["last_message"] = message
                        emit(
                            "downloading",
                            progress,
                            message,
                            {
                                "downloaded_bytes": downloaded,
                                "total_bytes": int(total),
                                "speed": int(speed) if speed else None,
                                "eta": eta,
                            },
                        )
                elif downloaded:
                    progress = min(38, 22 + min(14, int(math.log10(max(downloaded, 1))) * 2))
                    message = self._build_download_message(
                        downloaded=downloaded,
                        total=None,
                        speed=speed,
                        eta=eta,
                        fragment_index=fragment_index,
                        fragment_count=fragment_count,
                    )
                    should_emit = (
                        now - float(download_progress["last_emit_time"]) >= 0.8
                        or abs(downloaded - int(download_progress["last_downloaded_bytes"])) >= 2 * 1024 * 1024
                    )
                    if should_emit:
                        download_progress["value"] = max(int(download_progress["value"]), progress)
                        download_progress["last_emit_time"] = now
                        download_progress["last_downloaded_bytes"] = downloaded
                        download_progress["last_message"] = message
                        emit(
                            "downloading",
                            int(download_progress["value"]),
                            message,
                            {
                                "downloaded_bytes": downloaded,
                                "speed": int(speed) if speed else None,
                                "eta": eta,
                            },
                        )
            elif status == "finished":
                emit("downloading", 44, "音频下载完成，正在提取音轨")

        def postprocessor_hook(data: dict[str, object]) -> None:
            status = str(data.get("status") or "")
            postprocessor = str(data.get("postprocessor") or "")
            if status == "started":
                emit("downloading", 46, f"正在执行 {postprocessor or '后处理'}")
            elif status == "processing":
                emit("downloading", 47, f"正在提取 MP3 音频")
            elif status == "finished":
                emit("downloading", 48, "音频提取完成")

        options = {
            **self._base_ydl_options(),
            "format": "bestaudio/best",
            "outtmpl": output_template,
            "noplaylist": True,
            "progress_hooks": [progress_hook],
            "postprocessor_hooks": [postprocessor_hook],
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        }
        ffmpeg_exe = ffmpeg_location()
        if ffmpeg_exe is not None:
            options["ffmpeg_location"] = str(ffmpeg_exe)
            logger.info("using ffmpeg location: %s", ffmpeg_exe)
        else:
            logger.warning("ffmpeg not found, yt_dlp will use system PATH")
        YoutubeDL, DownloadError = _get_ytdlp_classes()
        try:
            with YoutubeDL(options) as ydl:
                ydl.download([url])
        except DownloadError as exc:
            self._raise_ydl_error(exc)
        candidates = sorted(
            task_dir / p
            for p in glob.glob(
                f"{glob.escape(str(task_dir / safe_title))}.*"
            )
        )
        if not candidates:
            raise VideoSumError("Audio download failed.")
        emit("downloading", 48, "音频文件已就绪")
        return candidates[0]

    def _extract_cookie_for_bilibili(self) -> str | None:
        """从 yt-dlp cookie 配置中提取 Bilibili cookie"""
        try:
            options = self._base_ydl_options()
            cookiefile = options.get("cookiefile")
            if cookiefile:
                from pathlib import Path

                cookie_path = Path(str(cookiefile))
                if cookie_path.exists():
                    # 读取 Netscape cookie 格式，提取 bilibili.com 的 cookie
                    cookie_parts = []
                    with open(cookie_path, encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line or line.startswith("#"):
                                continue
                            parts = line.split("\t")
                            if len(parts) >= 7 and "bilibili.com" in parts[0]:
                                name, value = parts[5], parts[6]
                                cookie_parts.append(f"{name}={value}")
                    if cookie_parts:
                        return "; ".join(cookie_parts)
            # Fallback: 尝试从 cookiesfrombrowser 获取（但这需要浏览器环境，暂不实现）
            return None
        except Exception as exc:
            logger.warning("failed to extract bilibili cookie: %s", exc)
            return None

    def _build_download_message(
        self,
        downloaded: int,
        total: int | None,
        speed: float | None,
        eta: int | None,
        fragment_index: object | None,
        fragment_count: object | None,
    ) -> str:
        parts = [f"已下载 {self._format_bytes(downloaded)}"]
        if total:
            parts.append(f"/ {self._format_bytes(total)}")
        if speed:
            parts.append(f"速度 {self._format_bytes(int(speed))}/s")
        if eta is not None:
            parts.append(f"剩余约 {int(eta)} 秒")
        if fragment_index and fragment_count:
            parts.append(f"分片 {fragment_index}/{fragment_count}")
        return " ".join(parts)

    def _format_bytes(self, value: int) -> str:
        units = ["B", "KB", "MB", "GB"]
        size = float(max(value, 0))
        unit_index = 0
        while size >= 1024 and unit_index < len(units) - 1:
            size /= 1024
            unit_index += 1
        if unit_index == 0:
            return f"{int(size)}{units[unit_index]}"
        return f"{size:.1f}{units[unit_index]}"

    def _transcribe(
        self,
        audio_path: Path,
        duration: float | None,
        emit: Callable[[str, int, str, dict[str, object] | None], None],
    ) -> tuple[str, list[dict[str, object]]]:
        provider = self._settings.transcription_provider
        if provider == "siliconflow":
            return self._transcribe_with_siliconflow(audio_path, duration, emit)
        if provider == "multimodal":
            return self._transcribe_with_multimodal(audio_path, duration, emit)
        if provider == "funasr":
            return self._transcribe_with_funasr(audio_path, duration, emit)
        return self._transcribe_with_local_whisper(audio_path, duration, emit)

    def _transcribe_with_local_whisper(
        self,
        audio_path: Path,
        duration: float | None,
        emit: Callable[[str, int, str, dict[str, object] | None], None],
    ) -> tuple[str, list[dict[str, object]]]:
        if not self._settings.local_asr_available:
            raise TranscriptionConfigurationError(
                "Local ASR is not installed in the current runtime. Install local ASR from Settings or switch to SiliconFlow."
            )
        attempts = [
            {
                "model": self._settings.whisper_model,
                "device": self._settings.whisper_device,
                "compute_type": self._settings.whisper_compute_type,
                "message": f"正在加载转写模型 {self._settings.whisper_model}",
            }
        ]
        if self._settings.whisper_device == "cpu" and self._settings.whisper_compute_type != "float32":
            attempts.append(
                {
                    "model": self._settings.whisper_model,
                    "device": "cpu",
                    "compute_type": "float32",
                    "message": f"兼容模式重试，正在加载转写模型 {self._settings.whisper_model}",
                }
            )

        last_error: VideoSumError | None = None
        for index, attempt in enumerate(attempts):
            emit(
                "transcribing",
                52,
                str(attempt["message"]),
                {
                    "model": attempt["model"],
                    "device": attempt["device"],
                    "compute_type": attempt["compute_type"],
                    "attempt": index + 1,
                },
            )
            logger.info(
                "launch transcription subprocess attempt=%s model=%s device=%s compute_type=%s audio=%s",
                index + 1,
                attempt["model"],
                attempt["device"],
                attempt["compute_type"],
                audio_path,
            )
            emit("transcribing", 58, "开始转写音频内容")
            try:
                transcript, segments = self._run_transcription_subprocess(
                    audio_path=audio_path,
                    duration=duration,
                    emit=emit,
                    model_name=str(attempt["model"]),
                    device=str(attempt["device"]),
                    compute_type=str(attempt["compute_type"]),
                )
                logger.info(
                    "transcription finished audio=%s segments=%d transcript_chars=%d attempt=%s",
                    audio_path,
                    len(segments),
                    len(transcript),
                    index + 1,
                )
                return transcript, segments
            except VideoSumError as exc:
                last_error = exc
                is_last_attempt = index == len(attempts) - 1
                if is_last_attempt or not self._should_retry_transcription(exc):
                    raise
                emit("transcribing", 56, "转写引擎异常，正在切换兼容模式重试")

        raise last_error or VideoSumError("Transcription failed.")

    def _transcribe_with_siliconflow(
        self,
        audio_path: Path,
        duration: float | None,
        emit: Callable[[str, int, str, dict[str, object] | None], None],
    ) -> tuple[str, list[dict[str, object]]]:
        import subprocess
        import tempfile

        base_url = (self._settings.siliconflow_asr_base_url or "").rstrip("/")
        model_name = (self._settings.siliconflow_asr_model or "").strip()
        api_key = (self._settings.siliconflow_asr_api_key or "").strip()
        if not base_url or not model_name:
            raise TranscriptionConfigurationError("SiliconFlow ASR settings are incomplete.")
        if not api_key:
            raise TranscriptionConfigurationError("SiliconFlow ASR API key is not configured.")

        request_url = f"{base_url}/audio/transcriptions"
        emit(
            "transcribing",
            52,
            f"正在连接语音识别服务 {model_name}",
            {"provider": "siliconflow", "model": model_name},
        )

        chunk_duration = max(60, int(self._settings.siliconflow_asr_chunk_duration_seconds or 1800))
        concurrency = max(1, min(int(self._settings.siliconflow_asr_concurrency or 2), 8))
        total_duration = float(duration or 0)

        # Determine total duration via ffprobe if unknown
        if total_duration <= 0:
            try:
                result = subprocess.run(
                    ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", str(audio_path)],
                    capture_output=True, text=True, timeout=30,
                )
                total_duration = float(result.stdout.strip())
            except Exception:
                total_duration = 0

        file_size = audio_path.stat().st_size
        needs_chunking = total_duration > (chunk_duration * 1.1) or file_size > 500 * 1024 * 1024

        if not needs_chunking:
            emit("transcribing", 58, "正在上传音频到语音识别服务", {"provider": "siliconflow"})
            transcript = self._siliconflow_transcribe_chunk(
                audio_path, audio_path.name, model_name, api_key, request_url, 0, 1, emit,
            )
            if not transcript:
                raise VideoSumError("SiliconFlow ASR returned empty transcript text.")
            emit("transcribing", 78, "语音识别完成，正在整理文本", {"provider": "siliconflow"})
            segments = self._build_fallback_segments_from_transcript(transcript, duration)
            emit("transcribing", 84, f"转写完成，共 {len(segments)} 段", {"provider": "siliconflow"})
            return self._render_transcript_from_segments(segments), segments

        num_chunks = max(1, int(-(-total_duration // chunk_duration)))
        emit("transcribing", 55, f"音频 {int(total_duration)}s，分 {num_chunks} 段并发识别", {"provider": "siliconflow", "chunks": num_chunks, "concurrency": concurrency})

        chunk_jobs: list[tuple[int, float, Path]] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            for i in range(num_chunks):
                chunk_start = i * chunk_duration
                chunk_path = Path(tmpdir) / f"sf_chunk_{i:03d}.mp3"
                ffmpeg_cmd = [
                    "ffmpeg", "-y", "-i", str(audio_path),
                    "-ss", str(chunk_start), "-t", str(chunk_duration),
                    "-acodec", "libmp3lame", "-ab", "64k", "-ar", "16000", "-ac", "1",
                    str(chunk_path),
                ]
                subprocess.run(ffmpeg_cmd, capture_output=True, text=True, timeout=120)
                if chunk_path.exists() and chunk_path.stat().st_size > 0:
                    chunk_jobs.append((i, chunk_start, chunk_path))
                else:
                    logger.warning("siliconflow asr chunk %d/%d extraction failed", i + 1, num_chunks)

            results: dict[int, str] = {}
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = {
                    pool.submit(
                        self._siliconflow_transcribe_chunk,
                        job[2], f"chunk_{job[0]:03d}.mp3", model_name, api_key, request_url,
                        job[1], num_chunks, emit,
                    ): job[0]
                    for job in chunk_jobs
                }
                for future in as_completed(futures):
                    chunk_idx = futures[future]
                    try:
                        results[chunk_idx] = future.result() or ""
                    except Exception as exc:
                        logger.warning("siliconflow asr chunk %d failed: %s", chunk_idx, exc)

        if not results:
            raise VideoSumError("SiliconFlow ASR returned empty transcript for all chunks.")

        all_transcripts = [(k * chunk_duration, results[k]) for k in sorted(results) if results[k]]
        full_transcript = "\n".join(text for _, text in all_transcripts)
        emit("transcribing", 78, f"并发识别完成，{len(results)}/{len(chunk_jobs)} 段成功", {"provider": "siliconflow"})

        segments: list[dict[str, object]] = []
        for chunk_offset, chunk_text in all_transcripts:
            actual_dur = min(chunk_duration, max(1.0, total_duration - chunk_offset))
            chunk_segs = self._build_fallback_segments_from_transcript(chunk_text, actual_dur)
            for seg in chunk_segs:
                segments.append({
                    "start": round(seg["start"] + chunk_offset, 3),
                    "end": round(seg["end"] + chunk_offset, 3),
                    "text": seg["text"],
                })
        emit("transcribing", 84, f"转写完成，共 {len(segments)} 段", {"provider": "siliconflow"})
        return self._render_transcript_from_segments(segments), segments

    def _siliconflow_transcribe_chunk(
        self,
        chunk_path: Path,
        file_name: str,
        model_name: str,
        api_key: str,
        request_url: str,
        chunk_offset: float,
        total_chunks: int,
        emit: Callable[[str, int, str, dict[str, object] | None], None],
    ) -> str:
        headers = {"Authorization": f"Bearer {api_key}"}
        timeout = httpx.Timeout(connect=30.0, read=600.0, write=600.0, pool=30.0)
        max_retries = 3
        for attempt in range(max_retries):
            if attempt > 0:
                time.sleep(3 * (attempt + 1))
            try:
                with chunk_path.open("rb") as f, httpx.Client(timeout=timeout) as client:
                    response = client.post(
                        request_url,
                        headers=headers,
                        data={"model": model_name},
                        files={"file": (file_name, f, "audio/mpeg")},
                    )
                if response.status_code in {401, 403}:
                    detail = _extract_response_error_detail(response)
                    raise TranscriptionAuthenticationError(f"SiliconFlow ASR auth failed: {detail}")
                if response.status_code >= 400:
                    detail = _extract_response_error_detail(response)
                    logger.warning("siliconflow asr chunk error (offset=%.0fs): %s", chunk_offset, detail)
                    continue
                payload = response.json()
                text = str(payload.get("text") or "").strip() if isinstance(payload, dict) else ""
                if text:
                    return text
                logger.warning("siliconflow asr chunk empty (offset=%.0fs, attempt=%d)", chunk_offset, attempt + 1)
            except httpx.HTTPError as exc:
                logger.warning("siliconflow asr chunk request failed (offset=%.0fs): %s", chunk_offset, exc)
        return ""

    def _transcribe_with_multimodal(
        self,
        audio_path: Path,
        duration: float | None,
        emit: Callable[[str, int, str, dict[str, object] | None], None],
    ) -> tuple[str, list[dict[str, object]]]:
        import base64
        import subprocess
        import tempfile

        base_url = (self._settings.multimodal_asr_base_url or "").rstrip("/")
        model_name = (self._settings.multimodal_asr_model or "").strip()
        api_key = (self._settings.multimodal_asr_api_key or "").strip()
        if not base_url:
            raise TranscriptionConfigurationError("Multimodal ASR base URL is not configured.")
        if not model_name:
            raise TranscriptionConfigurationError("Multimodal ASR model is not configured.")

        request_url = f"{base_url}/chat/completions"
        emit(
            "transcribing",
            52,
            f"正在连接多模态语音识别服务 {model_name}",
            {"provider": "multimodal", "model": model_name},
        )

        # Audio chunking config
        CHUNK_DURATION_SECONDS = max(30, int(self._settings.multimodal_asr_chunk_duration_seconds or 180))
        total_duration = float(duration or 0)
        ffmpeg_exe = ffmpeg_location()
        ffprobe_exe = None
        if ffmpeg_exe is not None:
            ffprobe_name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
            ffprobe_exe = ffmpeg_exe.parent / ffprobe_name
        if ffprobe_exe is None or not ffprobe_exe.is_file():
            ffprobe_which = shutil.which("ffprobe")
            if ffprobe_which:
                ffprobe_exe = Path(ffprobe_which)

        # If duration is unknown, try to get it from ffprobe
        if total_duration <= 0 and ffprobe_exe is not None:
            try:
                result = subprocess.run(
                    [
                        str(ffprobe_exe),
                        "-v",
                        "quiet",
                        "-show_entries",
                        "format=duration",
                        "-of",
                        "csv=p=0",
                        str(audio_path),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                    **_windows_hidden_subprocess_kwargs(),
                )
                if result.returncode == 0:
                    total_duration = float(result.stdout.strip())
            except Exception:
                total_duration = 0

        # Determine if chunking is needed (> 6 minutes or file > 15MB)
        file_size = audio_path.stat().st_size
        needs_chunking = (total_duration > 360) or (file_size > 15 * 1024 * 1024)
        if needs_chunking and ffmpeg_exe is None:
            raise VideoSumError("FFmpeg is unavailable, cannot split audio for multimodal ASR.")
        if needs_chunking and total_duration <= 0:
            raise VideoSumError("Unable to determine audio duration for multimodal ASR chunking.")

        mime_map = {".wav": "audio/wav", ".mp3": "audio/mp3", ".m4a": "audio/mp4", ".ogg": "audio/ogg", ".flac": "audio/flac"}

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        def _send_audio_chunk(chunk_path: Path, chunk_offset: float, chunk_index: int, total_chunks: int) -> str:
            """Send a single audio chunk to the multimodal ASR API with retry on empty response."""
            with chunk_path.open("rb") as f:
                chunk_data = f.read()
            chunk_b64 = base64.b64encode(chunk_data).decode("utf-8")

            emit(
                "transcribing",
                55 + int(20 * chunk_index / max(1, total_chunks)),
                f"正在识别音频片段 {chunk_index + 1}/{total_chunks}",
                {"provider": "multimodal", "chunk": chunk_index + 1, "total": total_chunks, "offset": chunk_offset},
            )

            chunk_ext = chunk_path.suffix.lower()
            chunk_mime = mime_map.get(chunk_ext, "audio/wav")

            payload = {
                "model": model_name,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {
                                    "data": f"data:{chunk_mime};base64,{chunk_b64}",
                                },
                            },
                            {
                                "type": "text",
                                "text": "请转录这段音频的全部文字内容，保持原文，不要修改",
                            },
                        ],
                    }
                ],
                "max_completion_tokens": 4096,
            }

            max_retries = max(0, int(self._settings.multimodal_asr_max_retries or 5))
            for attempt in range(max_retries):
                if attempt > 0:
                    wait_sec = 3 * (attempt + 1)
                    logger.info("multimodal chunk %d/%d retry %d/%d (offset=%.0fs, size=%d bytes, wait=%ds)", chunk_index + 1, total_chunks, attempt + 1, max_retries, chunk_offset, len(chunk_data), wait_sec)
                    time.sleep(wait_sec)

                timeout = httpx.Timeout(connect=30.0, read=600.0, write=600.0, pool=30.0)
                with httpx.Client(timeout=timeout) as client:
                    response = client.post(request_url, headers=headers, json=payload)

                if response.status_code in {401, 403}:
                    detail = _extract_response_error_detail(response)
                    raise TranscriptionAuthenticationError(f"Multimodal ASR authentication failed: {detail}")
                if response.status_code >= 400:
                    detail = _extract_response_error_detail(response)
                    raise VideoSumError(f"Multimodal ASR request failed: {detail}")

                result = response.json()
                transcript = ""
                choices = result.get("choices") or []
                if choices:
                    message = choices[0].get("message") or {}
                    transcript = str(message.get("content") or "").strip()
                    if not transcript:
                        transcript = str(message.get("reasoning_content") or "").strip()

                if transcript:
                    if attempt > 0:
                        logger.info("multimodal chunk %d/%d succeeded on retry %d (offset=%.0fs)", chunk_index + 1, total_chunks, attempt + 1, chunk_offset)
                    return transcript

                finish_reason = choices[0].get("finish_reason", "unknown") if choices else "no_choices"
                logger.warning("multimodal chunk %d/%d empty response (attempt %d/%d, offset=%.0fs, finish_reason=%s)", chunk_index + 1, total_chunks, attempt + 1, max_retries, chunk_offset, finish_reason)

            return ""

        # Reject oversized single-chunk payloads (> 12MB raw → ~16MB base64)
        MAX_SINGLE_CHUNK_BYTES = 12 * 1024 * 1024
        all_transcripts: list[tuple[float, float, str]] = []  # (offset, duration, text)

        if not needs_chunking:
            if file_size > MAX_SINGLE_CHUNK_BYTES:
                raise VideoSumError(
                    f"Audio file too large for single-chunk multimodal ASR "
                    f"({file_size / 1024 / 1024:.1f}MB > {MAX_SINGLE_CHUNK_BYTES / 1024 / 1024:.0f}MB). "
                    f"Please enable chunking by ensuring audio duration is known."
                )
            # Single chunk - send entire audio
            emit("transcribing", 58, "正在发送音频到多模态识别服务", {"provider": "multimodal", "audio_size": file_size})
            transcript = _send_audio_chunk(audio_path, 0.0, 0, 1)
            if not transcript:
                raise VideoSumError("Multimodal ASR returned empty transcript text.")
            all_transcripts.append((0.0, total_duration or 0, transcript))
        else:
            # Multiple chunks - split and process with overlap to reduce
            # missed words at boundaries.  Each chunk (except the first)
            # starts 2s earlier so the model hears boundary audio in both
            # adjacent chunks.  This may produce slight duplication in the
            # merged transcript, which the LLM summariser can handle.
            CHUNK_OVERLAP_SECONDS = 2
            num_chunks = max(1, int(-(-total_duration // CHUNK_DURATION_SECONDS)))  # ceil division
            emit(
                "transcribing",
                55,
                f"音频较长({int(total_duration)}秒)，将分 {num_chunks} 段识别",
                {"provider": "multimodal", "total_duration": total_duration, "chunks": num_chunks},
            )

            with tempfile.TemporaryDirectory() as tmpdir:
                for i in range(num_chunks):
                    chunk_start = i * CHUNK_DURATION_SECONDS
                    chunk_duration = CHUNK_DURATION_SECONDS
                    if i > 0:
                        chunk_start -= CHUNK_OVERLAP_SECONDS
                        chunk_duration += CHUNK_OVERLAP_SECONDS
                    chunk_path = Path(tmpdir) / f"chunk_{i:03d}.mp3"

                    # Re-encode to MP3 for reliable splitting and smaller file size
                    ffmpeg_cmd = [
                        str(ffmpeg_exe), "-y",
                        "-i", str(audio_path),
                        "-ss", str(chunk_start),
                        "-t", str(chunk_duration),
                        "-acodec", "libmp3lame",
                        "-ab", "64k",
                        "-ar", "16000",
                        "-ac", "1",
                        str(chunk_path),
                    ]
                    result = subprocess.run(
                        ffmpeg_cmd,
                        capture_output=True,
                        text=True,
                        timeout=120,
                        check=False,
                        **_windows_hidden_subprocess_kwargs(),
                    )

                    if not chunk_path.exists() or chunk_path.stat().st_size == 0:
                        logger.warning("multimodal chunk %d/%d extraction failed (offset=%.0fs): %s", i + 1, num_chunks, chunk_start, result.stderr[-200:] if result.stderr else "empty output")
                        continue

                    chunk_size_kb = chunk_path.stat().st_size / 1024
                    logger.info("multimodal chunk %d/%d extracted: offset=%.0fs, size=%.0fKB", i + 1, num_chunks, chunk_start, chunk_size_kb)

                    transcript = _send_audio_chunk(chunk_path, chunk_start, i, num_chunks)
                    if transcript:
                        all_transcripts.append((chunk_start, chunk_duration, transcript))
                    else:
                        logger.warning("multimodal chunk %d/%d returned empty transcript after all retries (offset=%.0fs, size=%.0fKB)", i + 1, num_chunks, chunk_start, chunk_size_kb)

        if not all_transcripts:
            raise VideoSumError("Multimodal ASR returned empty transcript text.")

        # Merge all chunk transcripts
        full_transcript = "\n".join(text for _, _, text in all_transcripts)

        emit(
            "transcribing",
            78,
            "多模态语音识别完成，正在整理文本结构",
            {"provider": "multimodal", "transcript_chars": len(full_transcript), "chunks_processed": len(all_transcripts)},
        )

        # Build segments with proper timestamps
        segments: list[dict[str, object]] = []
        for chunk_offset, chunk_dur, chunk_text in all_transcripts:
            actual_dur = min(chunk_dur, max(1.0, total_duration - chunk_offset)) if total_duration > 0 else chunk_dur
            chunk_segments = self._build_fallback_segments_from_transcript(chunk_text, actual_dur)
            for seg in chunk_segments:
                segments.append({
                    "start": round(seg["start"] + chunk_offset, 3),
                    "end": round(seg["end"] + chunk_offset, 3),
                    "text": seg["text"],
                })

        emit(
            "transcribing",
            84,
            f"转写完成，共整理 {len(segments)} 段",
            {"provider": "multimodal", "segment_count": len(segments)},
        )
        return self._render_transcript_from_segments(segments), segments

    def _transcribe_with_funasr(
        self,
        audio_path: Path,
        duration: float | None,
        emit: Callable[[str, int, str, dict[str, object] | None], None],
    ) -> tuple[str, list[dict[str, object]]]:
        if not self._settings.funasr_available:
            raise TranscriptionConfigurationError(
                "FunASR is not installed in the current runtime. Install FunASR from Settings or switch to another provider."
            )

        attempts = [
            {
                "model": self._settings.funasr_model,
                "device": self._settings.funasr_device,
                "vad_model": self._settings.funasr_vad_model,
                "punc_model": self._settings.funasr_punc_model,
                "spk_model": self._settings.funasr_spk_model,
                "hub": self._settings.funasr_hub,
                "hotword": self._settings.funasr_hotword,
                "message": f"正在加载 FunASR 模型 {self._settings.funasr_model}",
            }
        ]

        if self._settings.funasr_device == "cpu":
            attempts.append(
                {
                    "model": self._settings.funasr_model,
                    "device": "cpu",
                    "vad_model": "",
                    "punc_model": "",
                    "spk_model": "",
                    "hub": self._settings.funasr_hub,
                    "hotword": self._settings.funasr_hotword,
                    "message": f"兼容模式重试，正在加载 FunASR 模型 {self._settings.funasr_model}（禁用 VAD/PUNC）",
                }
            )
        elif self._settings.funasr_device == "cuda":
            attempts.append(
                {
                    "model": self._settings.funasr_model,
                    "device": "cpu",
                    "vad_model": "",
                    "punc_model": "",
                    "spk_model": "",
                    "hub": self._settings.funasr_hub,
                    "hotword": self._settings.funasr_hotword,
                    "message": f"GPU 模式异常，自动回退 CPU 重试...",
                }
            )

        last_error: VideoSumError | None = None
        for index, attempt in enumerate(attempts):
            emit(
                "transcribing",
                52,
                str(attempt["message"]),
                {
                    "model": attempt["model"],
                    "device": attempt["device"],
                    "vad_model": attempt.get("vad_model", ""),
                    "punc_model": attempt.get("punc_model", ""),
                    "attempt": index + 1,
                },
            )
            logger.info(
                "launch funasr subprocess attempt=%s model=%s device=%s vad=%s punc=%s audio=%s",
                index + 1,
                attempt["model"],
                attempt["device"],
                attempt.get("vad_model", ""),
                attempt.get("punc_model", ""),
                audio_path,
            )
            emit("transcribing", 58, "开始转写音频内容")
            try:
                transcript, segments = self._run_funasr_subprocess(
                    audio_path=audio_path,
                    duration=duration,
                    emit=emit,
                    model_name=str(attempt["model"]),
                    device=str(attempt["device"]),
                    vad_model=str(attempt.get("vad_model", "")),
                    punc_model=str(attempt.get("punc_model", "")),
                    spk_model=str(attempt.get("spk_model", "")),
                    hub=str(attempt.get("hub", "ms")),
                    hotword=str(attempt.get("hotword", "")),
                )
                logger.info(
                    "funasr transcription finished audio=%s segments=%d transcript_chars=%d attempt=%s",
                    audio_path,
                    len(segments),
                    len(transcript),
                    index + 1,
                )
                return transcript, segments
            except VideoSumError as exc:
                last_error = exc
                is_last_attempt = index == len(attempts) - 1
                if is_last_attempt or not self._should_retry_transcription(exc):
                    raise
                emit("transcribing", 56, "转写引擎异常，正在切换兼容模式重试")
            except RuntimeError as exc:
                msg = str(exc).lower()
                if "lock" in msg or "waiting" in msg:
                    raise VideoSumError(
                        f"可能由于多个任务同时下载模型导致锁竞争，请稍后重试: {exc}"
                    ) from exc
                raise

        raise last_error or VideoSumError("FunASR transcription failed.")

    def _build_fallback_segments_from_transcript(
        self,
        transcript: str,
        duration: float | None,
    ) -> list[dict[str, object]]:
        normalized = re.sub(r"\s+", " ", str(transcript or "")).strip()
        if not normalized:
            return []

        chunks = self._split_transcript_into_timed_chunks(normalized)

        total_chars = sum(len(chunk) for chunk in chunks) or len(normalized)
        effective_duration = max(float(duration or 0.0), float(len(chunks) * 6))
        current = 0.0
        segments: list[dict[str, object]] = []
        for index, chunk in enumerate(chunks):
            weight = len(chunk) / total_chars if total_chars > 0 else 1 / len(chunks)
            start = current
            if index == len(chunks) - 1:
                end = effective_duration
            else:
                end = min(effective_duration, current + max(2.0, effective_duration * weight))
            segments.append({"start": round(start, 3), "end": round(end, 3), "text": chunk})
            current = end
        return segments

    def _split_transcript_into_timed_chunks(self, transcript: str) -> list[str]:
        text = str(transcript or "").strip()
        if not text:
            return []

        sentence_parts = [
            part.strip()
            for part in re.split(r"(?<=[。！？!?；;：:，,、])", text)
            if part and part.strip()
        ]
        if not sentence_parts:
            sentence_parts = [text]

        soft_target_chars = 56
        hard_limit_chars = 88
        chunks: list[str] = []
        buffer = ""

        def flush_buffer() -> None:
            nonlocal buffer
            if buffer.strip():
                chunks.append(buffer.strip())
                buffer = ""

        for part in sentence_parts:
            candidate = f"{buffer}{part}".strip()
            if buffer and len(candidate) > soft_target_chars:
                flush_buffer()
                candidate = part.strip()

            while len(candidate) > hard_limit_chars:
                split_index = max(
                    candidate.rfind(marker, 0, hard_limit_chars)
                    for marker in ("，", "、", ",", "；", ";", "：", ":", " ")
                )
                if split_index < 24:
                    split_index = hard_limit_chars
                head = candidate[:split_index].strip(" ，、；;：:")
                if head:
                    chunks.append(head)
                candidate = candidate[split_index:].strip()

            buffer = candidate

        flush_buffer()
        return chunks or [text]

    def _render_transcript_from_segments(self, segments: list[dict[str, object]]) -> str:
        lines = [
            f"[{self._format_seconds(float(segment.get('start') or 0))}] {str(segment.get('text') or '').strip()}"
            for segment in segments
            if str(segment.get("text") or "").strip()
        ]
        transcript = "\n".join(lines).strip()
        if transcript:
            return transcript
        return "\n".join(str(segment.get("text") or "").strip() for segment in segments if str(segment.get("text") or "").strip())

    def _run_transcription_subprocess(
        self,
        audio_path: Path,
        duration: float | None,
        emit: Callable[[str, int, str, dict[str, object] | None], None],
        model_name: str,
        device: str,
        compute_type: str,
    ) -> tuple[str, list[dict[str, object]]]:
        progress_path = audio_path.with_name("transcription_worker_progress.jsonl")
        output_path = audio_path.with_name("transcription_worker_result.json")
        if progress_path.exists():
            progress_path.unlink()
        if output_path.exists():
            output_path.unlink()

        command = self._build_transcription_command(
            audio_path=audio_path,
            model_name=model_name,
            device=device,
            compute_type=compute_type,
            progress_path=progress_path,
            output_path=output_path,
        )
        if duration is not None:
            command.extend(["--duration", str(duration)])

        env = os.environ.copy()
        for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__"):
            env.pop(key, None)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        pythonpath_entries = [str(path) for path in runtime_pythonpath_dirs(self._settings.runtime_channel)]
        if pythonpath_entries:
            env["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)
        runtime_paths = [str(path) for path in runtime_library_dirs(self._settings.runtime_channel)]
        ffmpeg_exe = ffmpeg_location()
        if ffmpeg_exe is not None:
            runtime_paths.append(str(ffmpeg_exe.parent))
        env["VIDEO_SUM_DLL_PATHS"] = os.pathsep.join(runtime_paths)
        merged_path: list[str] = []
        for entry in [*runtime_paths, *(env.get("PATH", "").split(os.pathsep))]:
            item = entry.strip()
            if item and item not in merged_path:
                merged_path.append(item)
        env["PATH"] = os.pathsep.join(merged_path)
        with sanitized_subprocess_dll_search():
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                cwd=str(audio_path.parent),
                **_windows_hidden_subprocess_kwargs(),
            )

        # Drain stdout/stderr in background threads to prevent pipe buffer deadlock.
        _whisper_stdout_lines: list[str] = []
        _whisper_stderr_lines: list[str] = []

        def _drain_pipe(pipe, sink: list[str]) -> None:
            if pipe is None:
                return
            try:
                for line in pipe:
                    sink.append(line)
            except (ValueError, OSError):
                pass

        _drain_stdout_thread = threading.Thread(target=_drain_pipe, args=(process.stdout, _whisper_stdout_lines), daemon=True)
        _drain_stderr_thread = threading.Thread(target=_drain_pipe, args=(process.stderr, _whisper_stderr_lines), daemon=True)
        _drain_stdout_thread.start()
        _drain_stderr_thread.start()

        _whisper_last_reported_stderr = -1

        def _emit_whisper_stderr_progress() -> None:
            nonlocal _whisper_last_reported_stderr
            total = len(_whisper_stderr_lines)
            if total <= _whisper_last_reported_stderr:
                return
            last = _whisper_last_reported_stderr
            _whisper_last_reported_stderr = total
            fresh = [line.strip() for line in _whisper_stderr_lines[last:] if line.strip()]
            if not fresh:
                return
            text = fresh[-1]
            if len(text) > 200:
                text = text[:200] + "..."
            emit("transcribing", 59, f"Whisper: {text}")

        emit("transcribing", 58, "正在启动转写子进程...")

        progress_offset = 0
        timeout_seconds = max(30 * 60, int((duration or 0) * 8) + 10 * 60)
        deadline = time.monotonic() + timeout_seconds
        while process.poll() is None:
            prev_offset = progress_offset
            progress_offset = self._replay_transcription_progress(progress_path, progress_offset, emit)
            if progress_offset == prev_offset:
                _emit_whisper_stderr_progress()
            else:
                _whisper_last_reported_stderr = len(_whisper_stderr_lines)
            if time.monotonic() > deadline:
                process.kill()
                _drain_stdout_thread.join(timeout=2)
                _drain_stderr_thread.join(timeout=2)
                stdout = "".join(_whisper_stdout_lines).strip()
                stderr = "".join(_whisper_stderr_lines).strip()
                logger.error(
                    "transcription subprocess timeout audio=%s model=%s device=%s compute_type=%s stdout=%s stderr=%s",
                    audio_path,
                    model_name,
                    device,
                    compute_type,
                    stdout[-1500:],
                    stderr[-1500:],
                )
                raise VideoSumError("Transcription subprocess timed out.")
            time.sleep(0.2)

        progress_offset = self._replay_transcription_progress(progress_path, progress_offset, emit)
        _drain_stdout_thread.join(timeout=5)
        _drain_stderr_thread.join(timeout=5)
        stdout = "".join(_whisper_stdout_lines).strip()
        stderr = "".join(_whisper_stderr_lines).strip()
        if process.returncode != 0:
            # 检查是否是 CUDA 清理时的访问冲突，但输出文件已成功写入
            is_native_crash = self._is_native_crash_returncode(process.returncode)
            output_valid = output_path.exists()
            if output_valid:
                try:
                    payload = json.loads(output_path.read_text(encoding="utf-8"))
                    transcript = str(payload.get("transcript") or "")
                    segments = list(payload.get("segments") or [])
                    if transcript.strip() and segments:
                        # 输出有效，接受结果，记录警告但不抛出异常
                        logger.warning(
                            "transcription subprocess crashed during cleanup but output is valid "
                            "audio=%s model=%s device=%s compute_type=%s returncode=%s segments=%d transcript_chars=%d",
                            audio_path,
                            model_name,
                            device,
                            compute_type,
                            process.returncode,
                            len(segments),
                            len(transcript),
                        )
                        return transcript, segments
                except (json.JSONDecodeError, OSError):
                    pass  # 输出文件损坏，继续抛出异常

            logger.error(
                "transcription subprocess failed audio=%s model=%s device=%s compute_type=%s returncode=%s stdout=%s stderr=%s",
                audio_path,
                model_name,
                device,
                compute_type,
                process.returncode,
                stdout.strip(),
                stderr.strip(),
            )
            native_hint = ""
            if is_native_crash:
                native_hint = " The transcription runtime crashed at the native library level."
            raise VideoSumError(
                f"Transcription subprocess failed with exit code {process.returncode}."
                f"{native_hint} Runtime={device}/{compute_type} model={model_name}."
            )
        if stderr.strip():
            logger.info("transcription subprocess stderr audio=%s stderr=%s", audio_path, stderr.strip())
        if not output_path.exists():
            raise VideoSumError("Transcription subprocess completed without output.")

        payload = json.loads(output_path.read_text(encoding="utf-8"))
        transcript = str(payload.get("transcript") or "")
        segments = list(payload.get("segments") or [])
        if not transcript.strip():
            raise VideoSumError("Transcription produced empty output.")
        return transcript, segments

    def _build_transcription_command(
        self,
        audio_path: Path,
        model_name: str,
        device: str,
        compute_type: str,
        progress_path: Path,
        output_path: Path,
    ) -> list[str]:
        runtime_python = runtime_python_executable(self._settings.runtime_channel)
        if runtime_python is None:
            raise VideoSumError(
                f"Managed runtime python is unavailable for channel '{self._settings.runtime_channel}'."
            )
        command = [str(runtime_python), "-m", "video_sum_core.transcribe_subprocess"]

        command.extend(
            [
                "--audio-path",
                str(audio_path),
                "--model",
                model_name,
                "--device",
                device,
                "--compute-type",
                compute_type,
                "--progress-path",
                str(progress_path),
                "--output-path",
                str(output_path),
            ]
        )
        return command

    def _run_funasr_subprocess(
        self,
        audio_path: Path,
        duration: float | None,
        emit: Callable[[str, int, str, dict[str, object] | None], None],
        model_name: str,
        device: str,
        vad_model: str,
        punc_model: str,
        spk_model: str,
        hub: str,
        hotword: str,
    ) -> tuple[str, list[dict[str, object]]]:
        progress_path = audio_path.with_name("funasr_worker_progress.jsonl")
        output_path = audio_path.with_name("funasr_worker_result.json")
        if progress_path.exists():
            progress_path.unlink()
        if output_path.exists():
            output_path.unlink()

        command = self._build_funasr_command(
            audio_path=audio_path,
            model_name=model_name,
            device=device,
            vad_model=vad_model,
            punc_model=punc_model,
            spk_model=spk_model,
            hub=hub,
            hotword=hotword,
            progress_path=progress_path,
            output_path=output_path,
        )
        if duration is not None:
            command.extend(["--duration", str(duration)])

        env = os.environ.copy()
        for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__"):
            env.pop(key, None)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        pythonpath_entries = [str(path) for path in runtime_pythonpath_dirs(self._settings.runtime_channel)]
        if pythonpath_entries:
            env["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)
        runtime_paths = [str(path) for path in runtime_library_dirs(self._settings.runtime_channel)]
        ffmpeg_exe = ffmpeg_location()
        if ffmpeg_exe is not None:
            runtime_paths.append(str(ffmpeg_exe.parent))
        env["VIDEO_SUM_DLL_PATHS"] = os.pathsep.join(runtime_paths)
        merged_path: list[str] = []
        for entry in [*runtime_paths, *(env.get("PATH", "").split(os.pathsep))]:
            item = entry.strip()
            if item and item not in merged_path:
                merged_path.append(item)
        env["PATH"] = os.pathsep.join(merged_path)
        with sanitized_subprocess_dll_search():
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                cwd=str(audio_path.parent),
                **_windows_hidden_subprocess_kwargs(),
            )

        # Drain stdout/stderr in background threads to prevent pipe buffer deadlock.
        # FunASR / PyTorch emit large volumes of log to stderr; if the pipe fills
        # up the child process blocks on write() and appears to hang forever.
        _funasr_stdout_lines: list[str] = []
        _funasr_stderr_lines: list[str] = []

        def _drain_pipe(pipe, sink: list[str]) -> None:
            if pipe is None:
                return
            try:
                for line in pipe:
                    sink.append(line)
            except (ValueError, OSError):
                pass

        _drain_stdout_thread = threading.Thread(target=_drain_pipe, args=(process.stdout, _funasr_stdout_lines), daemon=True)
        _drain_stderr_thread = threading.Thread(target=_drain_pipe, args=(process.stderr, _funasr_stderr_lines), daemon=True)
        _drain_stdout_thread.start()
        _drain_stderr_thread.start()

        # Report subprocess stderr as progress while waiting for the child
        # to write to the structured progress file.  FunASR / PyTorch emit
        # model-download percentage and loading stages on stderr long before
        # any progress JSONL line appears.
        _funasr_last_reported_stderr = -1

        def _emit_stderr_progress() -> None:
            nonlocal _funasr_last_reported_stderr
            total = len(_funasr_stderr_lines)
            if total <= _funasr_last_reported_stderr:
                return
            last = _funasr_last_reported_stderr
            _funasr_last_reported_stderr = total
            fresh = [line.strip() for line in _funasr_stderr_lines[last:] if line.strip()]
            if not fresh:
                return
            text = fresh[-1]
            if len(text) > 200:
                text = text[:200] + "..."
            emit("transcribing", 59, f"FunASR: {text}")

        # Immediate feedback — do NOT silently wait on a cold subprocess
        emit("transcribing", 58, "正在启动 FunASR 子进程，首次运行需下载模型（约 500MB），请耐心等待...")

        progress_offset = 0
        timeout_seconds = max(30 * 60, int((duration or 0) * 8) + 10 * 60)
        deadline = time.monotonic() + timeout_seconds
        stall_start = time.monotonic()
        stall_timeout = 600  # 10 minutes with no progress -> abort
        while process.poll() is None:
            if self._cancelled:
                process.kill()
                process.wait(timeout=5)
                raise VideoSumError("Task cancelled by user.")
            prev_offset = progress_offset
            progress_offset = self._replay_transcription_progress(progress_path, progress_offset, emit)
            if progress_offset == prev_offset:
                _emit_stderr_progress()
            else:
                _funasr_last_reported_stderr = len(_funasr_stderr_lines)
            # Stall detection: if the subprocess produces zero progress
            # for too long it is probably stuck (waiting for a lock, failed
            # download, etc.) — abort early instead of waiting the full 30 min.
            if progress_offset == 0:
                if time.monotonic() - stall_start > stall_timeout:
                    process.kill()
                    raise VideoSumError(
                        "FunASR subprocess produced no progress after 10 minutes. "
                        "Check network or ModelScope availability."
                    )
            else:
                stall_start = time.monotonic()
            if time.monotonic() > deadline:
                process.kill()
                _drain_stdout_thread.join(timeout=2)
                _drain_stderr_thread.join(timeout=2)
                stdout = "".join(_funasr_stdout_lines).strip()
                stderr = "".join(_funasr_stderr_lines).strip()
                logger.error(
                    "funasr subprocess timeout audio=%s model=%s device=%s stdout=%s stderr=%s",
                    audio_path,
                    model_name,
                    device,
                    stdout[-1500:],
                    stderr[-1500:],
                )
                raise VideoSumError("FunASR subprocess timed out.")
            time.sleep(0.2)

        progress_offset = self._replay_transcription_progress(progress_path, progress_offset, emit)
        _drain_stdout_thread.join(timeout=5)
        _drain_stderr_thread.join(timeout=5)
        stdout = "".join(_funasr_stdout_lines).strip()
        stderr = "".join(_funasr_stderr_lines).strip()
        if process.returncode != 0:
            is_native_crash = self._is_native_crash_returncode(process.returncode)
            output_valid = output_path.exists()
            if output_valid:
                try:
                    payload = json.loads(output_path.read_text(encoding="utf-8"))
                    transcript = str(payload.get("transcript") or "")
                    segments = list(payload.get("segments") or [])
                    if transcript.strip() and segments:
                        logger.warning(
                            "funasr subprocess crashed during cleanup but output is valid "
                            "audio=%s model=%s device=%s returncode=%s segments=%d transcript_chars=%d",
                            audio_path,
                            model_name,
                            device,
                            process.returncode,
                            len(segments),
                            len(transcript),
                        )
                        return transcript, segments
                except (json.JSONDecodeError, OSError):
                    pass

            logger.error(
                "funasr subprocess failed audio=%s model=%s device=%s returncode=%s stdout=%s stderr=%s",
                audio_path,
                model_name,
                device,
                process.returncode,
                stdout.strip(),
                stderr.strip(),
            )
            native_hint = ""
            if is_native_crash:
                native_hint = " The FunASR runtime crashed at the native library level."
            raise VideoSumError(
                f"FunASR subprocess failed with exit code {process.returncode}."
                f"{native_hint} Runtime={device} model={model_name}."
            )
        if stderr.strip():
            logger.info("funasr subprocess stderr audio=%s stderr=%s", audio_path, stderr.strip())
        if not output_path.exists():
            raise VideoSumError("FunASR subprocess completed without output.")

        payload = json.loads(output_path.read_text(encoding="utf-8"))
        transcript = str(payload.get("transcript") or "")
        segments = list(payload.get("segments") or [])
        if not transcript.strip():
            raise VideoSumError("FunASR transcription produced empty output.")
        return transcript, segments

    def _build_funasr_command(
        self,
        audio_path: Path,
        model_name: str,
        device: str,
        vad_model: str,
        punc_model: str,
        spk_model: str,
        hub: str,
        hotword: str,
        progress_path: Path,
        output_path: Path,
    ) -> list[str]:
        runtime_python = runtime_python_executable(self._settings.runtime_channel)
        if runtime_python is None:
            raise VideoSumError(
                f"Managed runtime python is unavailable for channel '{self._settings.runtime_channel}'."
            )
        command = [str(runtime_python), "-m", "video_sum_core.transcribe_funasr_subprocess"]

        command.extend(
            [
                "--audio-path",
                str(audio_path),
                "--model",
                model_name,
                "--device",
                device,
                "--vad-model",
                vad_model,
                "--punc-model",
                punc_model,
                "--spk-model",
                spk_model,
                "--hub",
                hub,
                "--hotword",
                hotword,
                "--progress-path",
                str(progress_path),
                "--output-path",
                str(output_path),
            ]
        )
        return command

    def _should_retry_transcription(self, error: VideoSumError) -> bool:
        message = str(error)
        return "native library level" in message or "exit code 3221226505" in message

    def _is_native_crash_returncode(self, returncode: int) -> bool:
        return returncode in {3221226505, -1073740791}

    def _replay_transcription_progress(
        self,
        progress_path: Path,
        offset: int,
        emit: Callable[[str, int, str, dict[str, object] | None], None],
    ) -> int:
        if not progress_path.exists():
            return offset
        with progress_path.open("r", encoding="utf-8") as handle:
            handle.seek(offset)
            for line in handle:
                raw = line.strip()
                if not raw:
                    continue
                event = json.loads(raw)
                emit(
                    str(event.get("stage") or "transcribing"),
                    int(event.get("progress") or 0),
                    str(event.get("message") or ""),
                    event.get("payload") or {},
                )
            return handle.tell()

    def _summarize(
        self,
        transcript: str,
        segments: list[dict[str, object]],
        title: str,
        emit: Callable[[str, int, str, dict[str, object] | None], None],
        source_kind: str | None = None,
        prompt_preset_id: str | None = None,
        pegasus_video: dict[str, str] | None = None,
    ) -> dict[str, object]:
        emit(
            "summarizing",
            88,
            self._build_summary_start_message(),
            {"llm_enabled": self._settings.llm_enabled and bool(self._settings.llm_api_key)},
        )
        used_llm_summary = False
        if self._settings.llm_enabled and self._settings.llm_api_key:
            emit("summarizing", 91, f"正在请求 LLM：{self._settings.llm_model or '未命名模型'}")
            logger.info(
                "llm summary request model=%s base_url=%s transcript_chars=%d segments=%d",
                self._settings.llm_model,
                self._settings.llm_base_url,
                len(transcript),
                len(segments),
            )
            try:
                if str(prompt_preset_id or "").strip():
                    summary = self._summarize_with_llm(
                        transcript,
                        segments,
                        title,
                        emit,
                        source_kind=source_kind,
                        prompt_preset_id=prompt_preset_id,
                    )
                else:
                    summary = self._summarize_with_llm(
                        transcript,
                        segments,
                        title,
                        emit,
                        source_kind=source_kind,
                    )
                used_llm_summary = True
            except (LLMAuthenticationError, LLMConfigurationError) as exc:
                logger.warning("llm unavailable, fallback to rule summary reason=%s", exc)
                emit(
                    "summarizing",
                    91,
                    f"LLM 不可用，已切换为本地规则摘要：{exc}",
                    {"fallback": "rules", "reason": str(exc)},
                )
                summary = self._summarize_with_rules(transcript, segments, title)
        else:
            emit("summarizing", 91, "未启用 LLM，使用本地规则摘要")
            logger.info("rule summary start transcript_chars=%d segments=%d", len(transcript), len(segments))
            summary = self._summarize_with_rules(transcript, segments, title)
        summary = self._normalize_summary(summary, transcript, segments, title)
        emit(
            "summarizing",
            95,
            "知识卡片摘要生成完成",
            {
                "result": self._build_task_result(transcript, summary, segments).model_dump(mode="json"),
                "result_scope": "knowledge_cards",
            },
        )
        if used_llm_summary:
            emit("summarizing", 96, "正在生成知识笔记")
            try:
                note_payload = self._generate_knowledge_note_with_llm(
                    transcript=transcript,
                    segments=segments,
                    title=title,
                    summary=summary,
                    source_kind=source_kind,
                )
                knowledge_note_markdown = str(note_payload.get("knowledgeNoteMarkdown") or "").strip()
                if knowledge_note_markdown:
                    summary["knowledgeNoteMarkdown"] = knowledge_note_markdown
                summary["llm_prompt_tokens"] = (_safe_int(summary.get("llm_prompt_tokens")) or 0) + (
                    _safe_int(note_payload.get("llm_prompt_tokens")) or 0
                )
                summary["llm_completion_tokens"] = (_safe_int(summary.get("llm_completion_tokens")) or 0) + (
                    _safe_int(note_payload.get("llm_completion_tokens")) or 0
                )
                summary["llm_total_tokens"] = (_safe_int(summary.get("llm_total_tokens")) or 0) + (
                    _safe_int(note_payload.get("llm_total_tokens")) or 0
                )
            except VideoSumError as exc:
                logger.warning("knowledge note llm generation failed, fallback to local note builder error=%s", exc)
                emit(
                    "summarizing",
                    97,
                    f"知识笔记生成失败，已回退为本地笔记：{exc}",
                    {"fallback": "knowledge_note_rules", "reason": str(exc)},
                )
            else:
                emit(
                    "summarizing",
                    98,
                    "知识笔记生成完成",
                    {
                        "result": self._build_task_result(transcript, summary, segments).model_dump(mode="json"),
                        "result_scope": "knowledge_note",
                    },
                )
        self._maybe_append_pegasus_summary(summary, title, pegasus_video, emit)
        emit("summarizing", 99, "结果整理完成")
        logger.info(
            "summary finished title=%s bullet_points=%d chapters=%d overview_chars=%d",
            title,
            len(summary.get("bulletPoints", [])),
            len(summary.get("chapters", [])),
            len(str(summary.get("overview") or "")),
        )
        return summary

    def _maybe_append_pegasus_summary(
        self,
        summary: dict[str, object],
        title: str,
        pegasus_video: dict[str, str] | None,
        emit: Callable[[str, int, str, dict[str, object] | None], None],
    ) -> None:
        """可选：用 Twelve Labs Pegasus 直接理解视频画面，把生成的视频理解摘要
        追加到知识笔记末尾，随知识笔记一起进入知识库。

        这是纯增量的可选步骤：未开启、缺少可直连的视频来源或调用失败时都会安静跳过，
        绝不影响已生成的文本摘要与知识笔记。
        """
        if not self._settings.twelvelabs_summary_enabled:
            return
        if not pegasus_video:
            logger.info("pegasus summary skipped: no direct video source available")
            return
        api_key = str(self._settings.twelvelabs_api_key or "").strip()
        if not api_key:
            logger.info("pegasus summary skipped: api key not configured")
            return
        emit("summarizing", 98, "正在调用 Twelve Labs Pegasus 理解视频画面")
        try:
            pegasus_text = analyze_video_with_pegasus(
                api_key=api_key,
                video=pegasus_video,
                prompt=self._settings.twelvelabs_prompt or DEFAULT_TWELVELABS_PROMPT,
                model_name=self._settings.twelvelabs_model or "pegasus1.5",
                base_url=self._settings.twelvelabs_base_url or "https://api.twelvelabs.io/v1.3",
            )
        except (PegasusConfigurationError, PegasusAuthenticationError) as exc:
            logger.warning("pegasus summary failed, skipping enrichment reason=%s", exc)
            emit("summarizing", 98, f"Pegasus 视频理解跳过：{exc}", {"pegasus": "skipped", "reason": str(exc)})
            return
        section = f"\n\n## Pegasus 视频理解摘要\n\n{pegasus_text.strip()}\n"
        existing_note = str(summary.get("knowledgeNoteMarkdown") or "").strip()
        summary["knowledgeNoteMarkdown"] = (existing_note + section) if existing_note else section.strip()
        summary["pegasus_summary"] = pegasus_text.strip()
        emit("summarizing", 98, "Pegasus 视频理解摘要已并入知识笔记", {"pegasus": "ok"})

    def _build_summary_start_message(self) -> str:
        if self._settings.llm_enabled and self._settings.llm_api_key:
            return "开始生成 LLM 摘要"
        return "开始生成本地规则摘要"

    def _summarize_with_llm(
        self,
        transcript: str,
        segments: list[dict[str, object]],
        title: str,
        emit: Callable[[str, int, str, dict[str, object] | None], None],
        source_kind: str | None = None,
        prompt_preset_id: str | None = None,
    ) -> dict[str, object]:
        base_url = (self._settings.llm_base_url or "").rstrip("/")
        if not base_url or not self._settings.llm_model:
            raise LLMConfigurationError("LLM 配置不完整，请检查 Base URL 和模型名。")
        if source_kind == "aggregate_series":
            return self._summarize_aggregate_series_with_llm(base_url, transcript, segments, title, emit)
        system_prompt_override, user_prompt_override = self._resolve_prompt(prompt_preset_id)
        chunks = self._build_summary_chunks(segments)
        if not chunks:
            chunks = [
                {
                    "index": 1,
                    "transcript": transcript,
                    "segments_json": json.dumps(segments, ensure_ascii=False),
                }
            ]
        logger.info(
            "llm summary chunk plan model=%s chunks=%d target_chars=%d overlap_segments=%d",
            self._settings.llm_model,
            len(chunks),
            self._settings.summary_chunk_target_chars,
            self._settings.summary_chunk_overlap_segments,
        )

        partial_summaries: list[dict[str, object]] = []
        chunk_count = len(chunks)
        concurrency = max(1, int(self._settings.summary_chunk_concurrency))
        retry_count = max(0, int(self._settings.summary_chunk_retry_count))
        emit(
            "summarizing",
            91,
            f"正在并发汇总 {chunk_count} 个内容块",
            {"chunk_count": chunk_count, "concurrency": concurrency},
        )
        completed = 0
        failures: list[str] = []
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            future_map = {
                executor.submit(
                    self._request_llm_summary_chunk,
                    base_url,
                    title,
                    chunk,
                    chunk_count,
                    retry_count,
                    system_prompt_override,
                    user_prompt_override,
                ): chunk
                for chunk in chunks
            }
            for future in as_completed(future_map):
                chunk = future_map[future]
                chunk_index = int(chunk["index"])
                try:
                    partial = future.result()
                    partial_summaries.append(partial)
                    completed += 1
                    progress = min(94, 91 + max(0, math.floor(completed * 3 / max(1, chunk_count))))
                    emit(
                        "summarizing",
                        progress,
                        f"已完成第 {chunk_index}/{chunk_count} 个内容块",
                        {"chunk_index": chunk_index, "chunk_count": chunk_count, "completed_chunks": completed},
                    )
                except VideoSumError as exc:
                    failures.append(f"chunk={chunk_index}: {exc}")
                    completed += 1
                    logger.warning(
                        "llm summary chunk skipped after retries model=%s chunk=%d/%d error=%s",
                        self._settings.llm_model,
                        chunk_index,
                        chunk_count,
                        exc,
                    )
                    emit(
                        "summarizing",
                        min(94, 91 + max(0, math.floor(completed * 3 / max(1, chunk_count)))),
                        f"第 {chunk_index}/{chunk_count} 个内容块失败，已跳过继续",
                        {"chunk_index": chunk_index, "chunk_count": chunk_count, "error": str(exc)},
                    )

        partial_summaries.sort(key=lambda item: int(item.get("chunk_index") or 0))
        if not partial_summaries:
            raise VideoSumError("All LLM summary chunks failed.")

        total_prompt_tokens = sum((_safe_int(item.get("llm_prompt_tokens")) or 0) for item in partial_summaries)
        total_completion_tokens = sum((_safe_int(item.get("llm_completion_tokens")) or 0) for item in partial_summaries)
        total_tokens = sum((_safe_int(item.get("llm_total_tokens")) or 0) for item in partial_summaries)

        emit(
            "summarizing",
            94,
            "正在合并分块摘要",
            {"chunk_count": chunk_count},
        )
        merged_chapters = self._merge_partial_chapters(partial_summaries, segments)
        aggregate_transcript, aggregate_segments = self._build_aggregate_summary_inputs(partial_summaries, merged_chapters)
        logger.info(
            "llm summary aggregate request model=%s chunk_count=%d aggregate_transcript_chars=%d aggregate_segments_chars=%d merged_chapters=%d",
            self._settings.llm_model,
            chunk_count,
            len(aggregate_transcript),
            len(aggregate_segments),
            len(merged_chapters),
        )
        merged = self._request_llm_json(
            base_url=base_url,
            payload=self._build_llm_summary_payload(
                title=title,
                transcript_excerpt=aggregate_transcript,
                segments_excerpt=aggregate_segments,
                system_prompt_override=system_prompt_override,
                user_prompt_override=user_prompt_override,
            ),
        )
        merged = self._merge_structured_summary(
            merged=merged,
            partial_summaries=partial_summaries,
            merged_chapters=merged_chapters,
            transcript=transcript,
            segments=segments,
            title=title,
        )
        if failures:
            merged.setdefault("overview", "")
            merged["overview"] = f"{merged['overview']}\n\n注意：有 {len(failures)} 个分块摘要失败，最终结果基于成功分块汇总。".strip()
        merged["llm_prompt_tokens"] = total_prompt_tokens + (_safe_int(merged.get("llm_prompt_tokens")) or 0)
        merged["llm_completion_tokens"] = total_completion_tokens + (_safe_int(merged.get("llm_completion_tokens")) or 0)
        merged["llm_total_tokens"] = total_tokens + (_safe_int(merged.get("llm_total_tokens")) or 0)
        return merged

    def _summarize_aggregate_series_with_llm(
        self,
        base_url: str,
        transcript: str,
        segments: list[dict[str, object]],
        title: str,
        emit: Callable[[str, int, str, dict[str, object] | None], None],
    ) -> dict[str, object]:
        page_count = len({int(float(item.get("start") or 0)) for item in segments if item.get("start") is not None})
        transcript_excerpt = self._build_aggregate_series_excerpt(transcript)
        segments_excerpt = self._build_aggregate_series_segments_excerpt(segments)
        emit(
            "summarizing",
            93,
            "正在生成全集总结",
            {"page_count": page_count, "input_chars": len(transcript_excerpt)},
        )
        logger.info(
            "llm aggregate series request model=%s pages=%d transcript_chars=%d segments_chars=%d",
            self._settings.llm_model,
            page_count,
            len(transcript_excerpt),
            len(segments_excerpt),
        )
        return self._request_llm_json(
            base_url=base_url,
            payload=self._build_aggregate_series_summary_payload(
                title=title,
                transcript_excerpt=transcript_excerpt,
                segments_excerpt=segments_excerpt,
            ),
        )

    def _request_llm_summary_chunk(
        self,
        base_url: str,
        title: str,
        chunk: dict[str, object],
        chunk_count: int,
        retry_count: int,
        system_prompt_override: str | None = None,
        user_prompt_override: str | None = None,
    ) -> dict[str, object]:
        chunk_index = int(chunk["index"])
        last_error: Exception | None = None
        for attempt in range(retry_count + 1):
            logger.info(
                "llm summary chunk request model=%s chunk=%d/%d attempt=%d transcript_chars=%d segments_json_chars=%d",
                self._settings.llm_model,
                chunk_index,
                chunk_count,
                attempt + 1,
                len(str(chunk["transcript"])),
                len(str(chunk["segments_json"])),
            )
            try:
                partial = self._request_llm_json(
                    base_url=base_url,
                    payload=self._build_llm_summary_payload(
                        title=f"{title} - 分块 {chunk_index}",
                        transcript_excerpt=str(chunk["transcript"]),
                        segments_excerpt=str(chunk["segments_json"]),
                        system_prompt_override=system_prompt_override,
                        user_prompt_override=user_prompt_override,
                    ),
                )
                partial["chunk_index"] = chunk_index
                partial["chunk_start"] = float(chunk.get("chunk_start") or 0)
                partial["chunk_end"] = float(chunk.get("chunk_end") or partial["chunk_start"])
                partial["source_segment_count"] = int(chunk.get("source_segment_count") or 0)
                return partial
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "llm summary chunk request failed model=%s chunk=%d/%d attempt=%d error=%s",
                    self._settings.llm_model,
                    chunk_index,
                    chunk_count,
                    attempt + 1,
                    exc,
                )
        raise VideoSumError(str(last_error) if last_error else f"Chunk {chunk_index} failed.")

    def _request_llm_json(
        self,
        base_url: str,
        payload: dict[str, object],
        *,
        timeout: float = 180,
        retry_count: int | None = None,
    ) -> dict[str, object]:
        payload = dict(payload)
        use_anthropic = is_anthropic_llm(self._settings.llm_provider, base_url)
        payload["model"] = (
            str(payload.get("model") or "").strip()
            if use_anthropic
            else normalize_openai_compatible_model_name(str(payload.get("model") or ""))
        )
        if use_anthropic:
            headers = {
                "x-api-key": self._settings.llm_api_key,
                "anthropic-version": ANTHROPIC_API_VERSION,
                "Content-Type": "application/json",
            }
            request_url = anthropic_messages_url(base_url)
            request_payload = build_anthropic_messages_payload(payload)
        else:
            headers = {
                "Authorization": f"Bearer {self._settings.llm_api_key}",
                "Content-Type": "application/json",
            }
            request_url = openai_chat_completions_url(base_url)
            request_payload = payload
        transport_retry_count = max(0, int(self._settings.summary_chunk_retry_count if retry_count is None else retry_count))
        last_error: Exception | None = None
        response: httpx.Response | None = None
        for attempt in range(transport_retry_count + 1):
            try:
                with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                    response = client.post(request_url, headers=headers, json=request_payload)
                break
            except Exception as exc:
                last_error = exc
                if attempt >= transport_retry_count or not _should_retry_llm_transport_error(exc):
                    raise
                backoff_seconds = min(6.0, 1.5 * (attempt + 1))
                logger.warning(
                    "llm json transport retry model=%s attempt=%d/%d error=%s backoff=%.1fs",
                    self._settings.llm_model,
                    attempt + 1,
                    transport_retry_count + 1,
                    exc,
                    backoff_seconds,
                )
                time.sleep(backoff_seconds)
        if response is None:
            raise VideoSumError(str(last_error) if last_error else "LLM request failed before receiving response.")
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = _extract_response_error_detail(exc.response)
            logger.error(
                "llm json request failed status=%s model=%s detail=%s",
                exc.response.status_code,
                self._settings.llm_model,
                detail,
            )
            status_code = exc.response.status_code
            if status_code in (401, 403):
                raise LLMAuthenticationError(
                    f"LLM API Key 无效、已过期，或当前模型/接口无权限访问（HTTP {status_code}: {detail}）。"
                ) from exc
            raise VideoSumError(f"LLM request failed with status {status_code}: {detail}") from exc
        logger.info("llm json response status=%s model=%s", response.status_code, self._settings.llm_model)
        response_json = response.json()
        content = self._extract_llm_response_content(response_json)
        parsed = self._parse_llm_json_content(content)
        usage = response_json.get("usage") or {}
        parsed.setdefault("title", "")
        parsed.setdefault("overview", "")
        parsed.setdefault("bulletPoints", [])
        parsed.setdefault("chapters", [])
        prompt_tokens = _safe_int(usage.get("prompt_tokens") or usage.get("input_tokens"))
        completion_tokens = _safe_int(usage.get("completion_tokens") or usage.get("output_tokens"))
        total_tokens = _safe_int(usage.get("total_tokens"))
        if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
            total_tokens = prompt_tokens + completion_tokens
        parsed["llm_prompt_tokens"] = prompt_tokens
        parsed["llm_completion_tokens"] = completion_tokens
        parsed["llm_total_tokens"] = total_tokens
        return parsed

    def _extract_llm_response_content(self, response_json: object) -> str:
        if not isinstance(response_json, dict):
            raise VideoSumError("LLM returned an unexpected response payload.")
        content = extract_llm_message_content(response_json)
        if content:
            return content
        raise VideoSumError("LLM returned no readable message content.")

    def _preflight_llm_availability(self) -> None:
        base_url = (self._settings.llm_base_url or "").rstrip("/")
        if not base_url or not self._settings.llm_model:
            raise LLMConfigurationError("LLM 配置不完整，请检查 Base URL 和模型名。")

        payload = {
            "model": self._settings.llm_model,
            "messages": [
                {
                    "role": "system",
                    "content": "你是一个 JSON 测试助手。你只能返回合法 JSON，不能输出任何额外文字。",
                },
                {
                    "role": "user",
                    "content": '请只返回：{"ok":true}',
                },
            ],
            "temperature": 0,
            "max_tokens": 32,
            "response_format": {"type": "json_object"},
            "enable_thinking": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        try:
            self._request_llm_json(base_url=base_url, payload=payload, timeout=20, retry_count=0)
        except httpx.TimeoutException as exc:
            raise VideoSumError("LLM API 检查超时，请稍后重试或检查 Base URL / 网络代理。") from exc
        except httpx.TransportError as exc:
            raise VideoSumError(f"LLM API 检查失败，请检查 Base URL / 网络代理：{exc}") from exc

    def _parse_llm_json_content(self, content: str) -> dict[str, object]:
        # 移除可能的 think 标签（某些模型如 MiniMax-M2.5 可能返回）
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)

        candidates = [str(content or "").strip()]
        extracted = _extract_json_object_text(content)
        if extracted not in candidates:
            candidates.append(extracted)

        for candidate in candidates:
            if not candidate:
                continue
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                try:
                    return json.loads(candidate, strict=False)
                except json.JSONDecodeError:
                    continue

        preview = _truncate_text(str(content or "").replace("\r", "\\r").replace("\n", "\\n"), 600)
        logger.error("llm json returned invalid content preview=%s", preview)
        raise VideoSumError("LLM returned invalid JSON content.")

    def _build_llm_summary_payload(
        self,
        title: str,
        transcript_excerpt: str,
        segments_excerpt: str,
        system_prompt_override: str | None = None,
        user_prompt_override: str | None = None,
    ) -> dict[str, object]:
        messages = self._build_summary_messages(
            title,
            transcript_excerpt,
            segments_excerpt,
            system_prompt_override=system_prompt_override,
            user_prompt_override=user_prompt_override,
        )
        messages = self._ensure_json_keyword_in_messages(messages)
        # Qwen mixed-thinking models may reject json_object mode when thinking is enabled.
        return {
            "model": self._settings.llm_model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "enable_thinking": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }

    def _build_aggregate_series_summary_payload(
        self,
        title: str,
        transcript_excerpt: str,
        segments_excerpt: str,
    ) -> dict[str, object]:
        messages = self._build_aggregate_series_summary_messages(title, transcript_excerpt, segments_excerpt)
        messages = self._ensure_json_keyword_in_messages(messages)
        return {
            "model": self._settings.llm_model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "enable_thinking": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }

    def _build_aggregate_series_summary_messages(
        self,
        title: str,
        transcript_excerpt: str,
        segments_excerpt: str,
    ) -> list[dict[str, str]]:
        return [
            {
                "role": "system",
                "content": (
                    "你是一名中文长内容总编辑，正在把一个多 P 视频的分 P 摘要整合为全集总结。"
                    "输入不是完整转写，而是每个分 P 已生成的概览、要点、章节和笔记摘录。"
                    "你的任务是做二次归纳：先逐 P 扫描，再跨 P 合并去重，最后输出能直接展示的结构化 JSON。"
                    "所有结论必须来自输入，不得补充外部资料，不得编造，不得输出 JSON 以外的文字。"
                    "You must return valid json only."
                ),
            },
            {
                "role": "user",
                "content": self._render_user_prompt_template(
                    """请基于下面的多 P 摘要素材生成「全集总结」JSON。

工作步骤必须隐式完成，不要把步骤写进输出：
1. 逐 P 扫描：每个 `## P数字 标题` 是一个分 P，优先读取其中 `[重点-概览]`、`[重点-要点]`、`[重点-章节]`、`[重点-笔记]`。
2. 每个分 P 至少保留一个高价值信息点，避免只关注开头或结尾导致中间分 P 丢失。
3. 跨 P 合并：把重复观点合并，把递进关系、因果关系、对比关系、结论与限制提炼出来。
4. 输出面向全集，而不是逐 P 流水账；但所有章节都必须保留可跳转的 P 数定位。

强约束：
1. 只返回合法 JSON，对象顶层只允许包含 title、overview、bulletPoints、chapters、chapterGroups 五个字段。
2. title 使用简洁中文标题，体现全集主题，不要机械重复原题。
3. overview 写 3 到 5 句，覆盖全集主线、关键展开、最终结论或价值。
4. bulletPoints 写 5 到 8 条，每条 28 到 88 个字，优先写跨 P 的综合结论、关键方法、限制、争议或行动建议。
5. chapters 表示全集的主题段落或内容推进，不要等同于每个分 P 一条；每项必须包含 title、start、summary：
   - start 必须是该主题最适合跳转的分 P 数字，例如 1、2、3；禁止使用时间戳、秒数、00:00、分钟。
   - title 要像真实小标题，不能写“章节 1”“P1 总结”这类占位标题。
   - summary 写 40 到 120 个字，说明这个主题覆盖哪些 P、核心信息和为什么重要。
6. chapterGroups 是大主题分组，每项包含 title、start、summary、children；children 必须来自 chapters。
   - group.start 同样是分 P 数字。
   - 分组按语义组织，不要机械平均分配。
7. 如果某个 P 的材料很短，只做保守归纳；不要补充没有出现的信息。
8. 不要写”视频主要讲了””本合集介绍了”这类低信息密度开头，直接进入信息本体。
9. 当分 P 数量较多（超过 20P）时，chapters 应该按主题合并多个相邻分 P，而不是每个分 P 一条。
10. 当分 P 数量较多时，bulletPoints 应该聚焦跨分 P 的综合结论，而不是单个分 P 的内容。

输出格式示例：
{{"title":"","overview":"","bulletPoints":["", "", "", "", ""],"chapters":[{{"title":"","start":1,"summary":""}}],"chapterGroups":[{{"title":"","start":1,"summary":"","children":[{{"title":"","start":1,"summary":""}}]}}]}}

全集标题：
{title}

逐 P 摘要素材：
{transcript_excerpt}

P 数索引：
{segments_excerpt}""",
                    title=title,
                    transcript_excerpt=transcript_excerpt,
                    segments_excerpt=segments_excerpt,
                ),
            },
        ]

    def _build_summary_messages(
        self,
        title: str,
        transcript_excerpt: str,
        segments_excerpt: str,
        system_prompt_override: str | None = None,
        user_prompt_override: str | None = None,
    ) -> list[dict[str, str]]:
        system_prompt = (
            str(system_prompt_override or "").strip()
            or self._settings.summary_system_prompt.strip()
            or DEFAULT_SUMMARY_SYSTEM_PROMPT
        )
        user_template = (
            str(user_prompt_override or "").strip()
            or self._settings.summary_user_prompt_template.strip()
            or DEFAULT_SUMMARY_USER_PROMPT_TEMPLATE
        )
        if not user_template:
            user_template = """请阅读下面的视频资料，并输出一个 JSON 对象。
注意：你必须返回合法的 json 对象，且只返回 json。

目标：
生成一个适合详情页展示的结构化摘要，让用户在不看完整视频的情况下，也能快速理解：
1. 这支视频核心在讲什么；
2. 有哪些关键观点、论据、案例、争议和结论；
3. 内容是如何逐步展开的。

强约束：
1. 必须输出合法 JSON，对象顶层只允许包含 title、overview、bulletPoints、chapters、chapterGroups 五个字段。
2. title 必须是简洁、准确的中文标题，避免口号式空话。
3. overview 必须写成 3 到 5 句中文，整体形成一段完整概述：
   - 第 1 句交代主题或讨论对象；
   - 中间句交代关键论点、论据、背景、冲突或方法；
   - 最后 1 句交代结论、判断、影响或最终落点；
   - 总体要具体、完整，适合单独作为“核心概览”展示。
4. bulletPoints 必须是 5 到 8 条中文要点，每条 28 到 88 个字：
   - 每条都要能单独成为一张知识卡片；
   - 优先提炼事实、观点、因果、对比、条件、风险、建议、争议；
   - 不要把同一件事拆成多条近义重复表达；
   - 不要写“作者认为”“视频提到”这类低信息密度前缀，直接写结论。
5. chapters 必须按内容自然分布生成，每个章节必须包含 title、start、summary：
   - chapter.title 要像小标题，短而具体，能体现这一段的主题推进；
   - chapter.summary 必须比普通概述更详细，写成 2 到 3 个短句或 40 到 120 个字，说明这一段具体讲了什么、举了什么例子、得出了什么判断；
   - chapters 应体现内容推进关系，而不是机械平均切分；
   - 章节数量不要预设固定值，应根据内容转折、主题切换、论证层次和视频时长自适应决定；
   - 短视频可以较少章节，长视频或知识密度高的视频应适当增加章节，必要时可达到 9 到 12 个；
   - 只有在确实进入新主题、新问题、新案例或新结论时才切出新章节，不要为了凑数量硬拆。
6. chapterGroups 用来表示“大章节 / 小章节”层级，按真实结构归纳大章节；每个大章节必须包含 title、start、summary、children：
   - children 必须是从 chapters 中归并出来的小章节数组，每项仍然包含 title、start、summary；
   - chapterGroups.summary 要概括该大章节的主题推进，20 到 60 个字；
   - chapterGroups.title 必须是有内容的主题名，禁止使用“大章节1”“第一部分”“Part 1”“Section 1”这类占位标题；
   - 当原文层级明显时，大章节数量也应随内容自适应，不要固定成 2 到 4 组；
   - 如果层级不明显，可以少量归并；如果层级明显，可以返回更多组，但不要机械平均分配。
7. start 必须使用视频里真实出现的时间点，单位为秒，按升序排列。
8. 如果原文信息有限，也必须尽量提炼出非空 bulletPoints 和 chapters，不能返回空数组。
9. 不要写“视频主要讲了”“本视频介绍了”“作者首先”这类模板化空话，直接进入信息本体。
10. 不要引用不存在的数据，不要补充外部背景，不要猜测说话者未明确表达的动机。

写作要求：
- 保持中文自然、清楚、具体，避免官话和营销口吻。
- 优先保留高价值信息：定义、判断、证据、例子、条件、限制、影响、结论。
- 如果视频包含多个层次，overview 负责总览，bulletPoints 负责拆出关键结论，chapters 负责还原内容推进，chapterGroups 负责归纳章节层级。
- 标题必须像真实目录项，而不是编号占位符；宁可少而准，也不要为了数量固定而硬拆。

输出格式示例：
{{"title":"","overview":"","bulletPoints":["", "", "", "", ""],"chapters":[{{"title":"","start":0,"summary":""}}],"chapterGroups":[{{"title":"","start":0,"summary":"","children":[{{"title":"","start":0,"summary":""}}]}}]}}

视频标题：{title}

转写节选：
{transcript}

分段数据节选：
{segments_json}"""
        return [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": self._render_user_prompt_template(
                    user_template,
                    title=title,
                    transcript_excerpt=transcript_excerpt,
                    segments_excerpt=segments_excerpt,
                ),
            },
        ]

    def _resolve_prompt(self, preset_id: str | None) -> tuple[str | None, str | None]:
        normalized_id = str(preset_id or "").strip()
        if not normalized_id:
            return None, None
        presets = load_presets(
            data_dir=self._settings.data_dir,
            prompt_presets_path=self._settings.prompt_presets_path,
        )
        for preset in presets:
            if preset.id == normalized_id:
                return preset.system_prompt, preset.user_prompt_template
        logger.warning("prompt preset not found, fallback to settings prompt preset_id=%s", normalized_id)
        return None, None

    def _build_knowledge_note_messages(
        self,
        title: str,
        transcript_excerpt: str,
        segments_excerpt: str,
        summary_json: str,
    ) -> list[dict[str, str]]:
        system_prompt = (
            self._settings.knowledge_note_system_prompt.strip()
            if self._settings.knowledge_note_system_prompt.strip()
            else DEFAULT_KNOWLEDGE_NOTE_SYSTEM_PROMPT
        )
        user_template = (
            self._settings.knowledge_note_user_prompt_template.strip()
            if self._settings.knowledge_note_user_prompt_template.strip()
            else DEFAULT_KNOWLEDGE_NOTE_USER_PROMPT_TEMPLATE
        )
        return [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": self._render_user_prompt_template(
                    user_template,
                    title=title,
                    transcript_excerpt=transcript_excerpt,
                    segments_excerpt=segments_excerpt,
                    summary_json=summary_json,
                ),
            },
        ]

    def _build_aggregate_series_note_messages(
        self,
        title: str,
        transcript_excerpt: str,
        segments_excerpt: str,
        summary_json: str,
    ) -> list[dict[str, str]]:
        return [
            {
                "role": "system",
                "content": (
                    "你是一名中文知识编辑，正在把多 P 视频的结构化全集总结整理成阅读笔记。"
                    "输入是分 P 摘要素材和已生成的全集结构化摘要，不是完整转写。"
                    "你需要做主题化整理，保留 P 数定位，不能补充外部资料，不能输出 JSON 以外的文字。"
                    "You must return valid json only."
                ),
            },
            {
                "role": "user",
                "content": self._render_user_prompt_template(
                    """请输出一个 JSON 对象，顶层只包含 knowledgeNoteMarkdown。

写作目标：
- 生成一篇「全集总结」阅读笔记，面向复盘整套多 P 视频。
- 不要把分 P 摘要逐条搬运成流水账；要按主题、方法、结论、限制进行重组。
- 关键段落中保留 P 数定位，例如“P2-P4 说明...”“这一点从 P5 开始展开”。

强约束：
1. knowledgeNoteMarkdown 必须是完整 Markdown，不要代码围栏。
2. 不要写时间戳，不要写 00:00；所有定位都使用 P 数。
3. 不要照抄逐 P 素材；要提炼主线、重点和跨 P 关系。
4. 不要编造输入中没有的信息。
5. 建议结构：核心结论、全集主线、分主题重点、关键转折/对比、回看路径。

全集标题：
{title}

已有全集结构化摘要：
{summary_json}

逐 P 摘要素材：
{transcript_excerpt}

P 数索引：
{segments_excerpt}""",
                    title=title,
                    transcript_excerpt=transcript_excerpt,
                    segments_excerpt=segments_excerpt,
                    summary_json=summary_json,
                ),
            },
        ]

    def _ensure_json_keyword_in_messages(self, messages: list[dict[str, str]]) -> list[dict[str, str]]:
        if any("json" in str(message.get("content") or "").lower() for message in messages):
            return messages

        patched = [dict(message) for message in messages]
        if patched:
            patched[0]["content"] = f"{patched[0].get('content', '').rstrip()}\nReturn valid json only."
        else:
            patched = [{"role": "system", "content": "Return valid json only."}]
        return patched

    def _render_user_prompt_template(
        self,
        template: str,
        title: str,
        transcript_excerpt: str,
        segments_excerpt: str,
        summary_json: str = "",
        knowledge_note_markdown: str = "",
    ) -> str:
        rendered = template
        replacements = {
            "title": title,
            "transcript": transcript_excerpt,
            "transcript_excerpt": transcript_excerpt,
            "segments_json": segments_excerpt,
            "segments_excerpt": segments_excerpt,
            "summary_json": summary_json,
            "knowledge_note_markdown": knowledge_note_markdown,
        }
        for key, value in replacements.items():
            rendered = rendered.replace(f"{{{key}}}", value)
        return rendered.replace("{{", "{").replace("}}", "}")

    def _build_llm_knowledge_note_payload(
        self,
        title: str,
        transcript_excerpt: str,
        segments_excerpt: str,
        summary_json: str,
        source_kind: str | None = None,
    ) -> dict[str, object]:
        if source_kind == "aggregate_series":
            messages = self._build_aggregate_series_note_messages(
                title,
                transcript_excerpt,
                segments_excerpt,
                summary_json,
            )
        else:
            messages = self._build_knowledge_note_messages(title, transcript_excerpt, segments_excerpt, summary_json)
        messages = self._ensure_json_keyword_in_messages(messages)
        return {
            "model": self._settings.llm_model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "enable_thinking": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }

    def _generate_knowledge_note_with_llm(
        self,
        transcript: str,
        segments: list[dict[str, object]],
        title: str,
        summary: dict[str, object],
        source_kind: str | None = None,
    ) -> dict[str, object]:
        base_url = (self._settings.llm_base_url or "").rstrip("/")
        if not base_url or not self._settings.llm_model:
            raise LLMConfigurationError("LLM 配置不完整，请检查 Base URL 和模型名。")
        if source_kind == "aggregate_series":
            transcript_excerpt = self._build_aggregate_series_excerpt(transcript)
            segments_excerpt = self._build_aggregate_series_segments_excerpt(segments)
        else:
            transcript_excerpt = self._build_transcript_excerpt(transcript)
            segments_excerpt = self._build_segments_excerpt(segments)
        summary_json = _truncate_text(
            json.dumps(
                {
                    "title": summary.get("title"),
                    "overview": summary.get("overview"),
                    "bulletPoints": summary.get("bulletPoints"),
                    "chapters": summary.get("chapters"),
                },
                ensure_ascii=False,
            ),
            2400,
        )
        logger.info(
            "llm knowledge note request model=%s transcript_chars=%d segments=%d summary_chars=%d",
            self._settings.llm_model,
            len(transcript_excerpt),
            len(segments),
            len(summary_json),
        )
        payload = self._build_llm_knowledge_note_payload(
            title=title,
            transcript_excerpt=transcript_excerpt,
            segments_excerpt=segments_excerpt,
            summary_json=summary_json,
            source_kind=source_kind,
        )
        result = self._request_llm_json(base_url=base_url, payload=payload)
        result.setdefault("knowledgeNoteMarkdown", "")
        if not str(result.get("knowledgeNoteMarkdown") or "").strip():
            raise VideoSumError("LLM returned empty knowledge note markdown.")
        return result

    def build_and_export_mindmap(
        self,
        task_id: str,
        title: str,
        result: TaskResult,
    ) -> tuple[TaskMindMap, Path]:
        mindmap = self._generate_mindmap_with_llm(title=title, result=result)
        task_dir = ensure_directory(self._settings.tasks_dir / task_id)
        mindmap_path = task_dir / "mindmap.json"
        mindmap_path.write_text(
            json.dumps(mindmap.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return mindmap, mindmap_path

    def build_and_export_visual_evidence(
        self,
        task_id: str,
        task_input: TaskInput,
        title: str,
        result: TaskResult,
        mode: str | None = None,
        force: bool = False,
        on_event: Callable[[PipelineEvent], None] | None = None,
    ) -> tuple[dict[str, object], Path, Path]:
        task_dir = ensure_directory(self._settings.tasks_dir / task_id)
        visual_dir = task_dir / "visual_evidence"
        if force and visual_dir.exists():
            shutil.rmtree(visual_dir)
        visual_dir = ensure_directory(visual_dir)
        frames_dir = ensure_directory(visual_dir / "frames")
        frame_index_path = visual_dir / "frame_index.json"
        context_path = visual_dir / "visual_context.json"
        note_path = visual_dir / "visual_note.md"
        enhanced_note_path = visual_dir / "visual_enhanced_note.md"
        keyframe_plan_path = visual_dir / "visual_keyframe_plan.json"
        insert_plan_path = visual_dir / "visual_insert_plan.json"
        mode = self._visual_note_mode(mode)

        def report(stage: str, progress: int, message: str, payload: dict[str, object] | None = None) -> None:
            if on_event is not None:
                on_event(PipelineEvent(stage=stage, progress=progress, message=message, payload=payload or {}))

        if mode == "text":
            context = self._build_visual_context_payload(
                task_id=task_id,
                status="skipped",
                source_kind="text",
                frames=[],
                observations=[],
                warnings=["当前知识笔记形式为纯文本，未生成图文增强笔记。"],
                note_path=note_path,
                enhanced_note_path=enhanced_note_path,
                frame_index_path=frame_index_path,
                keyframe_plan_path=keyframe_plan_path,
                insert_plan_path=insert_plan_path,
                mode=mode,
                insert_count=0,
            )
            self._write_json_atomic(context_path, context)
            note_path.write_text("", encoding="utf-8")
            enhanced_note_path.write_text("", encoding="utf-8")
            self._write_json_atomic(keyframe_plan_path, {"schema_version": 1, "mode": mode, "keyframes": []})
            self._write_json_atomic(insert_plan_path, {"schema_version": 1, "mode": mode, "insertions": []})
            return context, enhanced_note_path, context_path

        report("visual_keyinfo_planning", 24, "正在提取适合截图的关键信息点", {"mode": mode})
        report("visual_source_preparing", 34, "正在并发准备视频画面来源", {"mode": mode})
        with ThreadPoolExecutor(max_workers=2) as executor:
            source_future = executor.submit(self._prepare_visual_source, task_input, task_dir, title)
            plan_future = executor.submit(self._build_visual_keyframe_plan, title, result, mode)
            source_path, source_kind, warnings = source_future.result()
            keyframe_plan = plan_future.result()
        self._write_json_atomic(keyframe_plan_path, keyframe_plan)
        keyframes = [item for item in keyframe_plan.get("keyframes", []) if isinstance(item, dict)]
        report(
            "visual_source_ready",
            40,
            "视频画面来源和截图规划已就绪",
            {"mode": mode, "keyframe_count": len(keyframes), "source_kind": source_kind},
        )

        if source_path is None:
            context = self._build_visual_context_payload(
                task_id=task_id,
                status="unsupported",
                source_kind=source_kind,
                frames=[],
                observations=[],
                warnings=warnings,
                note_path=note_path,
                enhanced_note_path=enhanced_note_path,
                frame_index_path=frame_index_path,
                keyframe_plan_path=keyframe_plan_path,
                insert_plan_path=insert_plan_path,
                mode=mode,
                insert_count=0,
            )
            self._write_json_atomic(context_path, context)
            note_path.write_text("", encoding="utf-8")
            enhanced_note_path.write_text("", encoding="utf-8")
            self._write_json_atomic(insert_plan_path, {"schema_version": 1, "mode": mode, "insertions": []})
            return context, enhanced_note_path, context_path

        timestamps = self._visual_timestamps_from_keyframe_plan(keyframe_plan, result)
        report("visual_frame_extracting", 48, "正在按关键信息点抽取对应画面", {"mode": mode, "timestamp_count": len(timestamps)})
        frames, extract_warnings = self._extract_visual_frames(source_path, timestamps, frames_dir)
        warnings.extend(extract_warnings)
        self._attach_visual_keyframe_plan(frames, keyframes)
        report("visual_frame_extracted", 58, "关键画面抽取完成，正在整理图片索引", {"mode": mode, "frame_count": len(frames)})
        observations: list[dict[str, object]] = []
        if frames and mode == "vlm_integrated" and self._visual_llm_available():
            try:
                report("visual_frame_analyzing", 70, "正在调用 VLM 解析画面内容", {"mode": mode, "frame_count": len(frames)})
                observations = self._describe_visual_frames(frames, title, result)
            except Exception as exc:
                logger.warning("visual frame description failed task_id=%s error=%s", task_id, exc)
                warnings.append(f"视觉模型描述失败：{exc}")
        elif frames:
            observations = [
                {
                    "frame_id": str(frame["frame_id"]),
                    "timestamp_seconds": float(frame["timestamp_seconds"]),
                    "caption": str(frame.get("planned_caption") or frame.get("planned_concept") or "关键画面信息"),
                    "ocr_text": "",
                    "semantic_summary": str(frame.get("planned_note_hint") or frame.get("planned_reason") or ""),
                    "key_facts": [],
                    "suggested_anchor": str(frame.get("anchor_heading") or ""),
                    "scene": "unknown",
                    "confidence": None,
                }
                for frame in frames
            ]
        else:
            warnings.append("未能抽取到可用关键帧。")

        public_frames = [self._public_visual_frame(frame) for frame in frames]
        frame_index = {
            "schema_version": 1,
            "task_id": task_id,
            "source_kind": source_kind,
            "source_media_name": source_path.name,
            "frames_dir": "frames",
            "frames": public_frames,
        }
        self._write_json_atomic(frame_index_path, frame_index)

        note_markdown = self._render_visual_note_markdown(observations)
        note_path.write_text(note_markdown, encoding="utf-8")
        report("visual_insert_planning", 82, "正在规划图片插入到笔记正文的位置", {"mode": mode})
        insert_plan = self._build_visual_insert_plan(result, public_frames, observations, mode)
        report("visual_note_composing", 92, "正在整合图文笔记正文", {"mode": mode, "insert_count": len(insert_plan.get("insertions", [])) if isinstance(insert_plan.get("insertions"), list) else 0})
        enhanced_note_markdown = self._compose_visual_enhanced_note(
            title=title,
            result=result,
            observations=observations,
            insert_plan=insert_plan,
            mode=mode,
        )
        enhanced_note_path.write_text(enhanced_note_markdown, encoding="utf-8")
        self._write_json_atomic(insert_plan_path, insert_plan)
        status = "ready" if enhanced_note_markdown.strip() and insert_plan.get("insertions") else "partial" if frames else "failed"
        context = self._build_visual_context_payload(
            task_id=task_id,
            status=status,
            source_kind=source_kind,
            frames=public_frames,
            observations=observations,
            warnings=warnings,
            note_path=note_path,
            enhanced_note_path=enhanced_note_path,
            frame_index_path=frame_index_path,
            keyframe_plan_path=keyframe_plan_path,
            insert_plan_path=insert_plan_path,
            mode=mode,
            insert_count=len(insert_plan.get("insertions", [])) if isinstance(insert_plan.get("insertions"), list) else 0,
        )
        self._write_json_atomic(context_path, context)
        return context, enhanced_note_path, context_path

    def _build_inline_visual_note(
        self,
        task_id: str,
        task_input: TaskInput,
        task_dir: Path,
        title: str,
        result: TaskResult,
        emit: Callable[[str, int, str, dict[str, object] | None], None],
    ) -> TaskResult:
        mode = self._visual_note_mode()
        task_mode = getattr(task_input.options, "visual_note_mode", None)
        task_explicitly_requests_visual = False
        if task_mode is not None:
            mode = normalize_visual_note_mode(task_mode)
            task_explicitly_requests_visual = mode != "text"
        visual_enabled = self._settings.visual_evidence_enabled or task_explicitly_requests_visual
        if not visual_enabled or mode == "text":
            return result.model_copy(update={"visual_note_mode": mode})
        if not result.knowledge_note_markdown.strip():
            return result.model_copy(
                update={
                    "visual_note_mode": mode,
                    "visual_note_status": "skipped",
                    "visual_note_error_message": "纯文本知识笔记为空，已跳过图文笔记。",
                }
            )

        started_result = result.model_copy(
            update={
                "visual_note_mode": mode,
                "visual_note_status": "generating",
                "visual_note_error_message": None,
            }
        )
        emit(
            "visual_generating",
            20,
            "正在生成图文笔记",
            {"mode": mode, "result": started_result.model_dump(mode="json"), "result_scope": "visual_note"},
        )

        try:
            def handle_visual_event(event: PipelineEvent) -> None:
                emit(event.stage, event.progress, event.message, dict(event.payload or {}))

            context, note_path, context_path = self.build_and_export_visual_evidence(
                task_id=task_id,
                task_input=task_input,
                title=title,
                result=started_result,
                mode=mode,
                on_event=handle_visual_event,
            )
        except Exception as exc:
            logger.exception("inline visual note generation failed task_id=%s error=%s", task_id, exc)
            failed_result = started_result.model_copy(
                update={
                    "visual_note_status": "failed",
                    "visual_note_error_message": str(exc),
                    "visual_note_updated_at": datetime.now(timezone.utc),
                }
            )
            emit(
                "visual_failed",
                100,
                "图文笔记生成失败，纯文本笔记已保留",
                {"mode": mode, "error": str(exc), "result": failed_result.model_dump(mode="json"), "result_scope": "visual_note"},
            )
            return failed_result

        status_value = str(context.get("status") or "partial")
        warnings = [str(item) for item in context.get("warnings", []) if str(item).strip()]
        artifacts = {
            **started_result.artifacts,
            "visual_enhanced_note_path": str(Path(note_path)),
            "visual_note_path": str(Path(note_path).parent / "visual_note.md"),
            "visual_context_path": str(Path(context_path)),
            "visual_frame_index_path": str(Path(note_path).parent / "frame_index.json"),
            "visual_insert_plan_path": str(Path(note_path).parent / "visual_insert_plan.json"),
        }
        final_result = started_result.model_copy(
            update={
                "artifacts": artifacts,
                "visual_note_mode": str(context.get("mode") or mode),
                "visual_note_status": status_value,
                "visual_note_error_message": "\n".join(warnings) if status_value in {"failed", "partial", "unsupported"} and warnings else None,
                "visual_note_artifact_path": str(Path(note_path).parent / "visual_note.md"),
                "visual_enhanced_note_artifact_path": str(Path(note_path)),
                "visual_note_updated_at": datetime.now(timezone.utc),
                "visual_frame_count": int(context.get("frame_count") or 0),
                "visual_insert_count": int(context.get("insert_count") or 0),
            }
        )
        emit(
            "visual_completed" if status_value == "ready" else "visual_partial",
            100,
            "图文笔记生成完成" if status_value == "ready" else "图文笔记已降级完成",
            {
                "mode": final_result.visual_note_mode,
                "status": status_value,
                "frame_count": final_result.visual_frame_count,
                "insert_count": final_result.visual_insert_count,
                "warnings": warnings,
                "result": final_result.model_dump(mode="json"),
                "result_scope": "visual_note",
            },
        )
        return final_result

    def _visual_note_mode(self, mode_override: str | None = None) -> str:
        return normalize_visual_note_mode(mode_override or self._settings.visual_note_mode or "text")

    def _build_visual_keyframe_plan(self, title: str, result: TaskResult, mode: str) -> dict[str, object]:
        fallback = self._build_visual_keyframe_plan_locally(result, mode)
        if not self._settings.llm_enabled or not self._settings.llm_api_key or not self._settings.llm_base_url or not self._settings.llm_model:
            return fallback
        try:
            return self._build_visual_keyframe_plan_with_llm(title, result, mode)
        except Exception as exc:
            logger.warning("visual keyframe planning llm failed error=%s", exc)
            return fallback

    def _build_visual_keyframe_plan_locally(self, result: TaskResult, mode: str) -> dict[str, object]:
        keyframes: list[dict[str, object]] = []
        max_frames = max(1, min(int(self._settings.visual_evidence_max_frames or 12), 30))
        for item in result.timeline or []:
            if not isinstance(item, dict):
                continue
            try:
                timestamp = float(item.get("start") or 0)
            except (TypeError, ValueError):
                timestamp = 0.0
            title = str(item.get("title") or "").strip() or "关键章节"
            summary = str(item.get("summary") or "").strip()
            keyframes.append(
                {
                    "timestamp_seconds": max(0.0, timestamp),
                    "anchor_heading": title,
                    "concept": title,
                    "reason": summary or "该时间点对应知识笔记中的关键段落，适合截图辅助理解。",
                    "caption_hint": title,
                    "note_hint": summary,
                    "priority": max(0.1, 1.0 - len(keyframes) * 0.04),
                    "mode": mode,
                }
            )
            if len(keyframes) >= max_frames:
                break
        if not keyframes:
            keyframes = [
                {
                    "timestamp_seconds": timestamp,
                    "anchor_heading": "",
                    "concept": f"关键画面 {index + 1}",
                    "reason": "未找到章节时间线，按固定间隔抽取画面用于辅助复盘。",
                    "caption_hint": "关键画面",
                    "note_hint": "",
                    "priority": max(0.1, 1.0 - index * 0.05),
                    "mode": mode,
                }
                for index, timestamp in enumerate(self._choose_visual_timestamps(result))
            ]
        return {"schema_version": 1, "mode": mode, "planner": "local", "keyframes": keyframes[:max_frames]}

    def _build_visual_keyframe_plan_with_llm(self, title: str, result: TaskResult, mode: str) -> dict[str, object]:
        base_url = (self._settings.llm_base_url or "").rstrip("/")
        max_frames = max(1, min(int(self._settings.visual_evidence_max_frames or 12), 30))
        summary_payload = {
            "overview": result.overview,
            "keyPoints": result.key_points,
            "chapters": result.timeline,
            "chapterGroups": result.chapter_groups,
        }
        segments_excerpt = _truncate_text(json.dumps(result.timeline, ensure_ascii=False), 6000)
        user_template = self._settings.visual_frame_planning_prompt.strip() or (
            "请根据视频摘要和知识笔记，选择最值得截图辅助理解的关键画面。\n"
            "目标链路是：先找知识点 -> 按时间点截图 -> 让 VLM 理解图片 -> 整合成图文笔记。\n"
            "只返回 JSON 对象，格式：{\"keyframes\":[{\"timestamp_seconds\":数字,\"anchor_heading\":\"应插入到的笔记标题\","
            "\"concept\":\"被图片支撑的知识点\",\"reason\":\"为什么需要截图\",\"caption_hint\":\"图片标题建议\","
            "\"note_hint\":\"插入笔记时应补充的说明\",\"priority\":0到1}]}。\n"
            f"最多选择 {max_frames} 张；不要平均抽帧，只选能解释界面、代码、图表、演示、对比或步骤的画面。\n"
            "视频标题：{title}\n模式：{mode}\n摘要 JSON：\n{summary_json}\n\n知识笔记：\n{knowledge_note_markdown}"
        )
        try:
            user_prompt = user_template.format(
                title=title,
                mode=mode,
                max_frames=max_frames,
                summary_json=_truncate_text(json.dumps(summary_payload, ensure_ascii=False), 9000),
                knowledge_note_markdown=_truncate_text(result.knowledge_note_markdown or "", 12000),
                segments_excerpt=segments_excerpt,
            )
        except (KeyError, IndexError, ValueError):
            user_prompt = (
                f"{user_template}\n\n视频标题：{title}\n模式：{mode}\n摘要 JSON：\n"
                f"{_truncate_text(json.dumps(summary_payload, ensure_ascii=False), 9000)}\n\n"
                f"知识笔记：\n{_truncate_text(result.knowledge_note_markdown or '', 12000)}\n\n"
                f"分段数据：\n{segments_excerpt}"
            )
        payload = {
            "model": self._settings.llm_model,
            "messages": [
                {"role": "system", "content": "你是视频学习笔记的信息架构师。你只输出合法 JSON。"},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
            "enable_thinking": False,
        }
        parsed = self._request_llm_json(base_url=base_url, payload=payload, timeout=90, retry_count=1)
        raw_items = parsed.get("keyframes")
        keyframes: list[dict[str, object]] = []
        if isinstance(raw_items, list):
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                try:
                    timestamp = float(item.get("timestamp_seconds") or item.get("timestamp") or 0)
                except (TypeError, ValueError):
                    continue
                if timestamp < 0:
                    continue
                try:
                    priority = float(item.get("priority") if item.get("priority") is not None else 0.75)
                except (TypeError, ValueError):
                    priority = 0.75
                keyframes.append(
                    {
                        "timestamp_seconds": timestamp,
                        "anchor_heading": str(item.get("anchor_heading") or "").strip()[:160],
                        "concept": str(item.get("concept") or item.get("caption_hint") or "关键画面").strip()[:160],
                        "reason": str(item.get("reason") or "").strip()[:600],
                        "caption_hint": str(item.get("caption_hint") or item.get("concept") or "关键画面").strip()[:180],
                        "note_hint": str(item.get("note_hint") or item.get("reason") or "").strip()[:800],
                        "priority": max(0.0, min(1.0, priority)),
                        "mode": mode,
                    }
                )
        raw_frames = parsed.get("frames")
        if isinstance(raw_frames, list):
            for item in raw_frames:
                if not isinstance(item, dict):
                    continue
                try:
                    timestamp = float(item.get("timestamp_seconds") or item.get("seconds") or item.get("timestamp") or 0)
                except (TypeError, ValueError):
                    continue
                if timestamp < 0:
                    continue
                try:
                    raw_importance = item.get("priority") if item.get("priority") is not None else item.get("importance")
                    priority = float(raw_importance if raw_importance is not None else 0.75)
                    if priority > 1:
                        priority = priority / 5
                except (TypeError, ValueError):
                    priority = 0.75
                visual_type = str(item.get("expected_visual_type") or item.get("visual_type") or "").strip()
                reason = str(item.get("reason") or "").strip()
                keyframes.append(
                    {
                        "timestamp_seconds": timestamp,
                        "anchor_heading": str(item.get("anchor_heading") or item.get("heading") or "").strip()[:160],
                        "concept": str(item.get("concept") or item.get("caption_hint") or visual_type or "关键画面").strip()[:160],
                        "reason": reason[:600],
                        "caption_hint": str(item.get("caption_hint") or visual_type or item.get("concept") or "关键画面").strip()[:180],
                        "note_hint": str(item.get("note_hint") or reason).strip()[:800],
                        "priority": max(0.0, min(1.0, priority)),
                        "mode": mode,
                    }
                )
        if not keyframes:
            return self._build_visual_keyframe_plan_locally(result, mode)
        keyframes.sort(key=lambda item: float(item.get("priority") or 0), reverse=True)
        return {"schema_version": 1, "mode": mode, "planner": "llm", "keyframes": keyframes[:max_frames]}

    def _visual_timestamps_from_keyframe_plan(self, keyframe_plan: dict[str, object], result: TaskResult) -> list[float]:
        min_interval = max(5, min(int(self._settings.visual_evidence_frame_interval_seconds or 60), 60))
        timestamps: list[float] = []
        for item in keyframe_plan.get("keyframes", []):
            if not isinstance(item, dict):
                continue
            try:
                timestamp = max(0.0, float(item.get("timestamp_seconds") or 0))
            except (TypeError, ValueError):
                continue
            if any(abs(timestamp - existing) < min_interval for existing in timestamps):
                continue
            timestamps.append(timestamp)
        return timestamps or self._choose_visual_timestamps(result)

    def _attach_visual_keyframe_plan(self, frames: list[dict[str, object]], keyframes: list[dict[str, object]]) -> None:
        for frame, planned in zip(frames, keyframes):
            frame["anchor_heading"] = str(planned.get("anchor_heading") or "").strip()
            frame["planned_concept"] = str(planned.get("concept") or "").strip()
            frame["planned_reason"] = str(planned.get("reason") or "").strip()
            frame["planned_caption"] = str(planned.get("caption_hint") or planned.get("concept") or "").strip()
            frame["planned_note_hint"] = str(planned.get("note_hint") or planned.get("reason") or "").strip()
            frame["selection_source"] = str(planned.get("planner") or "keyframe_plan")

    def _prepare_visual_source(
        self,
        task_input: TaskInput,
        task_dir: Path,
        title: str,
    ) -> tuple[Path | None, str, list[str]]:
        warnings: list[str] = []
        if task_input.input_type is InputType.VIDEO_FILE:
            source_path = Path(str(task_input.source or "")).expanduser()
            if source_path.exists() and source_path.is_file():
                return source_path, "local_video", warnings
            return None, "local_video", [f"本地视频文件不存在：{source_path}"]
        if task_input.input_type is InputType.URL:
            try:
                video_path = self._download_video_for_visuals(
                    task_input.source,
                    task_dir / "visual_evidence",
                    sanitize_filename(title or "video"),
                )
                return video_path, "url_video", warnings
            except Exception as exc:
                logger.warning("visual video download skipped source=%s error=%s", task_input.source, exc)
                return None, "url_video", [f"图文笔记视频源准备失败：{exc}"]
        if task_input.input_type is InputType.AUDIO_FILE:
            return None, "audio_file", ["音频任务没有可截图的视频画面。"]
        return None, "transcript_text", ["纯转写任务没有可截图的视频画面。"]

    def _download_video_for_visuals(self, url: str, output_dir: Path, safe_title: str) -> Path:
        ensure_directory(output_dir)
        output_template = str(output_dir / f"{safe_title}.visual.%(ext)s")
        height = self._visual_download_height()
        options = {
            **self._base_ydl_options(),
            "format": (
                f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
                f"bestvideo[height<={height}]+bestaudio/"
                f"best[height<={height}][ext=mp4]/best[height<={height}]/best"
            ),
            "outtmpl": output_template,
            "noplaylist": True,
        }
        ffmpeg_exe = ffmpeg_location()
        if ffmpeg_exe is not None:
            options["ffmpeg_location"] = str(ffmpeg_exe)
        YoutubeDL, DownloadError = _get_ytdlp_classes()
        try:
            with YoutubeDL(options) as ydl:
                ydl.download([url])
        except DownloadError as exc:
            self._raise_ydl_error(exc)
        candidates = sorted(
            (Path(p) for p in glob.glob(
                f"{glob.escape(str(output_dir / safe_title))}.visual.*"
            )),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            raise VideoSumError("图文笔记视频源下载失败。")
        return candidates[0]

    def _visual_download_height(self) -> int:
        value = str(self._settings.visual_download_resolution or "720p").strip().lower()
        if value in {"auto", "best", "source", "original"}:
            return 2160
        aliases = {
            "360p": 360,
            "480p": 480,
            "720p": 720,
            "1080p": 1080,
            "1440p": 1440,
            "2k": 1440,
            "2160p": 2160,
            "4k": 2160,
        }
        if value in aliases:
            return aliases[value]
        match = re.search(r"(\d{3,4})", value)
        if match:
            return max(360, min(int(match.group(1)), 2160))
        return 720

    def _choose_visual_timestamps(self, result: TaskResult) -> list[float]:
        max_frames = max(1, min(int(self._settings.visual_evidence_max_frames or 12), 30))
        min_interval = max(10, int(self._settings.visual_evidence_frame_interval_seconds or 10))
        candidates: list[float] = []
        for item in result.timeline or []:
            if not isinstance(item, dict):
                continue
            start = item.get("start")
            try:
                timestamp = float(start) if start is not None else None
            except (TypeError, ValueError):
                timestamp = None
            if timestamp is not None and timestamp >= 0:
                candidates.append(timestamp)
        if not candidates:
            candidates = [float(index * min_interval) for index in range(max_frames)]
        ordered: list[float] = []
        for timestamp in sorted(set(round(item, 1) for item in candidates)):
            if ordered and timestamp - ordered[-1] < min_interval:
                continue
            ordered.append(timestamp)
            if len(ordered) >= max_frames:
                break
        if not ordered and candidates:
            ordered = [max(0.0, float(candidates[0]))]
        return ordered[:max_frames]

    def _extract_visual_frames(
        self,
        source_path: Path,
        timestamps: list[float],
        frames_dir: Path,
    ) -> tuple[list[dict[str, object]], list[str]]:
        warnings: list[str] = []
        ffmpeg_exe = ffmpeg_location()
        if ffmpeg_exe is None:
            return [], ["FFmpeg 不可用，已跳过截图证据。"]
        frames: list[dict[str, object]] = []
        seen_hashes: set[str] = set()
        width = max(320, min(int(self._settings.visual_evidence_frame_width or 960), 1600))
        analysis_width = max(320, min(int(self._settings.visual_evidence_vlm_image_width or 768), width))
        for index, timestamp in enumerate(timestamps, start=1):
            frame_id = f"f{index:04d}"
            target = frames_dir / f"{frame_id}.jpg"
            analysis_target = frames_dir / f"{frame_id}.analysis.jpg"
            command = [
                str(ffmpeg_exe),
                "-y",
                "-ss",
                f"{max(0.0, float(timestamp)):.3f}",
                "-i",
                str(source_path),
                "-frames:v",
                "1",
                "-vf",
                f"scale='min({width},iw)':-2",
                "-q:v",
                "3",
                str(target),
            ]
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=45,
                    check=False,
                    **_windows_hidden_subprocess_kwargs(),
                )
            except (OSError, subprocess.SubprocessError) as exc:
                warnings.append(f"{self._format_seconds(timestamp)} 截图失败：{exc}")
                continue
            if result.returncode != 0 or not target.exists():
                warnings.append(f"{self._format_seconds(timestamp)} 截图失败。")
                continue
            if analysis_width < width:
                analysis_command = [
                    str(ffmpeg_exe),
                    "-y",
                    "-i",
                    str(target),
                    "-vf",
                    f"scale='min({analysis_width},iw)':-2",
                    "-q:v",
                    str(self._ffmpeg_jpeg_quality_for_visual_analysis()),
                    str(analysis_target),
                ]
                try:
                    analysis_result = subprocess.run(
                        analysis_command,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=30,
                        check=False,
                        **_windows_hidden_subprocess_kwargs(),
                    )
                    if analysis_result.returncode != 0 or not analysis_target.exists():
                        analysis_target = target
                except (OSError, subprocess.SubprocessError):
                    analysis_target = target
            else:
                analysis_target = target
            digest = self._sha256_file(target)
            if digest in seen_hashes:
                try:
                    target.unlink()
                except OSError:
                    pass
                if analysis_target != target:
                    try:
                        analysis_target.unlink()
                    except OSError:
                        pass
                continue
            seen_hashes.add(digest)
            frames.append(
                {
                    "frame_id": frame_id,
                    "timestamp_seconds": float(timestamp),
                    "timestamp": self._format_seconds(float(timestamp)),
                    "file_name": target.name,
                    "image_path": f"frames/{target.name}",
                    "analysis_image_path": f"frames/{analysis_target.name}",
                    "_absolute_path": str(target),
                    "_analysis_absolute_path": str(analysis_target),
                    "sha256": digest,
                    "selected": True,
                    "reason": "chapter_anchor",
                }
            )
        return frames, warnings

    def _ffmpeg_jpeg_quality_for_visual_analysis(self) -> str:
        try:
            quality = int(self._settings.visual_evidence_image_quality or 85)
        except (TypeError, ValueError):
            quality = 85
        quality = max(1, min(quality, 100))
        qscale = round(31 - ((quality - 1) * 29 / 99))
        return str(max(2, min(qscale, 31)))

    def _visual_llm_available(self) -> bool:
        if not self._settings.visual_evidence_use_llm:
            return False
        if hasattr(self._settings, "visual_multimodal_enabled") and not self._settings.visual_multimodal_enabled:
            return False
        _provider, base_url, model, api_key = self._visual_llm_config()
        return bool(base_url and model and api_key)

    def _visual_llm_config(self) -> tuple[str, str, str, str]:
        provider = (self._settings.visual_vlm_provider or "openai-compatible").strip()
        base_url = (self._settings.visual_evidence_base_url or self._settings.llm_base_url or "").rstrip("/")
        model = self._settings.visual_evidence_model or self._settings.llm_model
        api_key = self._settings.visual_evidence_api_key or self._settings.llm_api_key
        return provider, base_url, model, api_key

    def _describe_visual_frames(
        self,
        frames: list[dict[str, object]],
        title: str,
        result: TaskResult,
    ) -> list[dict[str, object]]:
        provider, base_url, model, api_key = self._visual_llm_config()
        observations: list[dict[str, object]] = []
        timeline_hint = "\n".join(
            f"- {self._format_seconds(float(item.get('start') or 0))} {str(item.get('title') or '').strip()}：{str(item.get('summary') or '').strip()}"
            for item in (result.timeline or [])[:20]
            if isinstance(item, dict)
        )
        is_anthropic = provider == "anthropic"
        timeout = max(15, int(self._settings.visual_evidence_timeout_seconds or 120))
        retry_count = max(0, min(int(self._settings.visual_evidence_retry_count or 1), 3))
        for frame in frames:
            image_path = frame.get("_analysis_absolute_path") or frame.get("_absolute_path")
            if not image_path:
                image_path = str(frame.get("analysis_image_path") or frame.get("image_path") or "")
            image_file = Path(str(image_path))
            if not image_file.exists():
                continue
            image_data = base64.b64encode(image_file.read_bytes()).decode("ascii")
            prompt = self._settings.visual_vlm_prompt.strip() or (
                "提取画面中的客观信息输出 JSON。不要叙述、不要评价、不要写「该画面」。"
                "字段：visual_type, caption, ocr_text, key_facts, semantic_summary, importance, should_insert, "
                "suggested_anchor, scene, confidence。"
                "\n视频标题：{title}\n时间点：{timestamp}\n章节线索：\n{timeline_hint}"
            )
            try:
                prompt = prompt.format(
                    title=title,
                    timestamp=frame.get("timestamp"),
                    context=timeline_hint,
                    timeline_hint=timeline_hint,
                    knowledge_note_markdown=_truncate_text(result.knowledge_note_markdown or "", 4000),
                )
            except (KeyError, IndexError, ValueError):
                prompt = f"{prompt}\n视频标题：{title}\n时间点：{frame.get('timestamp')}\n章节线索：\n{timeline_hint}"
            payload: dict[str, object] = {
                "model": model,
                "max_tokens": 2048,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{image_data}",
                                    "detail": "low",
                                },
                            },
                        ],
                    }
                ],
                "response_format": {"type": "json_object"},
                "enable_thinking": False,
            }
            # Only use Anthropic image format for the real Anthropic API.
            # Third-party Anthropic-compatible endpoints (e.g. SiliconFlow) do not
            # support images via /messages — fall back to OpenAI format for those.
            use_anthropic_images = is_anthropic and "api.anthropic.com" in base_url.lower()
            if use_anthropic_images:
                request_url = anthropic_messages_url(base_url)
                headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
                request_payload = build_anthropic_messages_payload(payload)
            else:
                request_url = f"{base_url}/chat/completions"
                headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
                payload["model"] = normalize_openai_compatible_model_name(model)
                request_payload = payload
            last_error: Exception | None = None
            for attempt in range(retry_count + 1):
                try:
                    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                        response = client.post(request_url, headers=headers, json=request_payload)
                    response.raise_for_status()
                    body = response.json()
                    content = extract_llm_message_content(body)
                    parsed = json.loads(_extract_json_object_text(content))
                    key_facts_raw = parsed.get("key_facts")
                    key_facts: list[str] = []
                    if isinstance(key_facts_raw, list):
                        key_facts = [str(f).strip()[:200] for f in key_facts_raw if str(f).strip()]
                    observations.append(
                        {
                            "frame_id": str(frame["frame_id"]),
                            "timestamp_seconds": float(frame["timestamp_seconds"]),
                            "visual_type": str(parsed.get("visual_type") or parsed.get("scene") or "unknown").strip()[:64],
                            "caption": str(parsed.get("caption") or "关键画面信息").strip()[:240],
                            "ocr_text": str(parsed.get("ocr_text") or "").strip()[:600],
                            "semantic_summary": str(parsed.get("semantic_summary") or "").strip()[:600],
                            "key_facts": key_facts,
                            "importance": parsed.get("importance"),
                            "should_insert": parsed.get("should_insert", True),
                            "suggested_anchor": str(parsed.get("suggested_anchor") or "").strip()[:120],
                            "scene": str(parsed.get("scene") or "unknown").strip()[:64],
                            "confidence": parsed.get("confidence"),
                        }
                    )
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt >= retry_count:
                        logger.warning("visual frame llm request failed frame_id=%s error=%s", frame.get("frame_id"), exc)
                    else:
                        time.sleep(min(4.0, 1.2 * (attempt + 1)))
            if last_error and not any(item.get("frame_id") == frame.get("frame_id") for item in observations):
                observations.append(
                    {
                        "frame_id": str(frame["frame_id"]),
                        "timestamp_seconds": float(frame["timestamp_seconds"]),
                        "visual_type": "unknown",
                        "caption": "关键画面信息",
                        "ocr_text": "",
                        "semantic_summary": "",
                        "key_facts": [],
                        "importance": None,
                        "should_insert": True,
                        "suggested_anchor": "",
                        "scene": "unknown",
                        "confidence": None,
                    }
                )
        return observations

    def _build_visual_insert_plan(
        self,
        result: TaskResult,
        frames: list[dict[str, object]],
        observations: list[dict[str, object]],
        mode: str,
    ) -> dict[str, object]:
        observations_by_id = {str(item.get("frame_id") or ""): item for item in observations}
        max_insertions = max(1, min(int(self._settings.visual_evidence_max_frames or 12), 30))
        insertions: list[dict[str, object]] = []
        for frame in frames:
            frame_id = str(frame.get("frame_id") or "")
            observation = observations_by_id.get(frame_id, {})
            if mode == "vlm_integrated" and observation:
                should_insert = bool(observation.get("should_insert", True))
                try:
                    importance = float(observation.get("importance") if observation.get("importance") is not None else 0.75)
                except (TypeError, ValueError):
                    importance = 0.75
                if not should_insert and importance < 0.72:
                    continue
            chapter = self._nearest_visual_chapter(result, float(frame.get("timestamp_seconds") or 0))
            timestamp = str(frame.get("timestamp") or self._format_seconds(float(frame.get("timestamp_seconds") or 0)))
            anchor_heading = str(
                observation.get("suggested_anchor")
                or frame.get("anchor_heading")
                or chapter.get("title")
                or ""
            ).strip()
            caption = str(
                observation.get("caption")
                or frame.get("planned_caption")
                or frame.get("planned_concept")
                or chapter.get("title")
                or "关键画面"
            ).strip()
            key_facts_list = observation.get("key_facts") if isinstance(observation.get("key_facts"), list) else []
            explanation = str(
                (". ".join(str(f) for f in key_facts_list) if key_facts_list else "")
                or observation.get("semantic_summary")
                or frame.get("planned_note_hint")
                or frame.get("planned_reason")
                or chapter.get("summary")
                or ""
            ).strip()
            insertions.append(
                {
                    "frame_id": frame_id,
                    "timestamp_seconds": float(frame.get("timestamp_seconds") or 0),
                    "anchor_heading": anchor_heading,
                    "chapter_title": str(chapter.get("title") or "").strip(),
                    "markdown_image": f"visual://{frame_id}",
                    "alt": f"{timestamp} {caption}",
                    "caption": caption,
                    "explanation": explanation[:500],
                    "concept": str(frame.get("planned_concept") or "").strip(),
                    "selection_reason": str(frame.get("planned_reason") or "").strip(),
                    "mode": mode,
                }
            )
            if len(insertions) >= max_insertions:
                break
        return {"schema_version": 1, "mode": mode, "insertions": insertions}

    def _nearest_visual_chapter(self, result: TaskResult, timestamp: float) -> dict[str, object]:
        chapters = [item for item in (result.timeline or []) if isinstance(item, dict)]
        if not chapters:
            return {}
        best = chapters[0]
        best_delta = float("inf")
        for chapter in chapters:
            try:
                start = float(chapter.get("start") or 0)
            except (TypeError, ValueError):
                start = 0.0
            delta = abs(timestamp - start)
            if delta < best_delta:
                best = chapter
                best_delta = delta
        return best

    def _compose_visual_enhanced_note(
        self,
        *,
        title: str,
        result: TaskResult,
        observations: list[dict[str, object]],
        insert_plan: dict[str, object],
        mode: str,
    ) -> str:
        base_note = str(result.knowledge_note_markdown or "").strip()
        if not base_note:
            return ""
        if mode == "vlm_integrated" and observations and self._visual_llm_available():
            try:
                model_note = self._compose_visual_note_with_llm(title, base_note, observations, insert_plan)
                if self._visual_note_preserves_text_subject(model_note):
                    return model_note
                logger.warning("visual enhanced note llm composition rejected because text subject was not preserved")
            except Exception as exc:
                logger.warning("visual enhanced note llm composition failed error=%s", exc)
        return self._compose_visual_note_locally(base_note, insert_plan)

    def _visual_note_preserves_text_subject(self, markdown: str) -> bool:
        text = str(markdown or "").strip()
        if "visual://" not in text:
            return False
        image_count = len(re.findall(r"!\[[^\]]*\]\(visual://[^)]+\)", text))
        if image_count <= 0:
            return False
        text_without_images = re.sub(r"!\[[^\]]*\]\(visual://[^)]+\)", "", text)
        text_chars = len(re.sub(r"\s+", "", text_without_images))
        return text_chars >= max(180, image_count * 80)

    def _compose_visual_note_with_llm(
        self,
        title: str,
        knowledge_note_markdown: str,
        observations: list[dict[str, object]],
        insert_plan: dict[str, object] | None = None,
    ) -> str:
        provider, base_url, model, api_key = self._visual_llm_config()
        is_anthropic = provider == "anthropic"
        system_prompt = self._settings.visual_note_system_prompt.strip() or (
            "你是一名擅长将截图与文字深度整合的中文技术编辑。只输出 Markdown 正文。"
            "输出必须是段落→图片→段落的交替结构，只选最重要的 3-6 张图。"
            "绝对禁止图片堆积在末尾，禁止使用「画面呈现」「该画面」「上图」等流水账句式。"
        )
        payload_observations = []
        for item in observations:
            frame_id = str(item.get("frame_id") or "")
            if not frame_id:
                continue
            # Pre-filter: only send frames that are likely to be inserted.
            # Low-importance frames bloat the prompt and confuse the model.
            should_insert = item.get("should_insert")
            if should_insert is not None and not should_insert:
                continue
            try:
                importance = float(item.get("importance") if item.get("importance") is not None else 3)
            except (TypeError, ValueError):
                importance = 3
            if importance < 2.5:
                continue
            payload_observations.append(
                {
                    **{key: value for key, value in item.items() if not str(key).startswith("_")},
                    "markdown_image": f"visual://{frame_id}",
                }
            )
        user_template = self._settings.visual_note_user_prompt_template.strip() or (
            "请以画面客观信息为参考重新整合知识笔记，图片链接使用 observations 中的 markdown_image。\n"
            "核心规则：输出必须是 段落1→图1→段落2→图2 交替模式，只选 3-6 张最重要的图，禁止图片堆在末尾。\n"
            "要求：以知识点为叙事主线；精简合并重复内容；禁止「画面呈现」「该画面」「上图」等句式；"
            "图片插入后用 1-2 句自然过渡；key_facts/semantic_summary 转化为自己的语言。\n"
            "标题：{title}\n原始知识笔记：\n{knowledge_note_markdown}\n视觉解析 JSON：\n{visual_observations_json}"
        )
        if insert_plan:
            insertions_by_id = {
                str(item.get("frame_id") or ""): item
                for item in insert_plan.get("insertions", [])
                if isinstance(item, dict)
            }
            for item in payload_observations:
                plan_item = insertions_by_id.get(str(item.get("frame_id") or ""))
                if plan_item:
                    item["insert_plan"] = plan_item
        visual_observations_json = json.dumps(payload_observations, ensure_ascii=False, indent=2)
        # Truncate large inputs to keep compose prompt under reasonable token limits.
        # Long knowledge notes and many observations cause timeouts on reasoning models.
        truncated_note = _truncate_text(knowledge_note_markdown, 8000)
        truncated_obs = _truncate_text(visual_observations_json, 12000)
        try:
            user_prompt = user_template.format(
                title=title,
                knowledge_note_markdown=truncated_note,
                visual_observations_json=truncated_obs,
            )
        except (KeyError, IndexError, ValueError) as exc:
            logger.warning("visual note prompt template format failed error=%s", exc)
            user_prompt = (
                f"{user_template}\n\n"
                f"标题：{title}\n"
                f"原始知识笔记：\n{knowledge_note_markdown}\n\n"
                f"视觉解析 JSON：\n{visual_observations_json}"
            )
        openai_payload: dict[str, object] = {
            "model": normalize_openai_compatible_model_name(model),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.5,
            "max_tokens": 65536,
            "enable_thinking": True,
        }
        if is_anthropic:
            headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
            request_url = anthropic_messages_url(base_url)
            request_payload = build_anthropic_messages_payload(openai_payload)
        else:
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            request_url = f"{base_url}/chat/completions"
            request_payload = openai_payload
        # Compose step processes the full knowledge note + all observations — can take a while.
        compose_timeout = max(60, int(self._settings.visual_evidence_timeout_seconds or 300))
        with httpx.Client(timeout=compose_timeout, follow_redirects=True) as client:
            response = client.post(request_url, headers=headers, json=request_payload)
        response.raise_for_status()
        body = response.json()
        content = extract_llm_message_content(body)
        if not content:
            raise VideoSumError("图文笔记模型返回空内容。")
        return content

    def _compose_visual_note_locally(self, knowledge_note_markdown: str, insert_plan: dict[str, object]) -> str:
        insertions = [item for item in insert_plan.get("insertions", []) if isinstance(item, dict)]
        if not insertions:
            return knowledge_note_markdown
        lines = knowledge_note_markdown.splitlines()
        output: list[str] = []
        insertion_index = 0
        for line in lines:
            stripped = line.strip()
            is_heading = stripped.startswith("#")
            output.append(line)
            if insertion_index >= len(insertions):
                continue
            current = insertions[insertion_index]
            anchor = str(current.get("anchor_heading") or current.get("chapter_title") or "").strip()
            if not is_heading or not anchor:
                continue
            heading_text = re.sub(r"^#+\s*", "", stripped).strip()
            shorter, longer = sorted((len(anchor), len(heading_text)))
            if (anchor in heading_text or heading_text in anchor) and shorter >= longer * 0.6:
                output.extend(self._format_visual_insertion_markdown(current))
                insertion_index += 1
        return "\n".join(output).strip()

    def _format_visual_insertion_markdown(self, insertion: dict[str, object]) -> list[str]:
        image = str(insertion.get("markdown_image") or "").strip()
        alt = str(insertion.get("alt") or insertion.get("caption") or "关键画面").strip()
        explanation = str(insertion.get("explanation") or "").strip()
        if not image:
            return []
        lines = ["", f"![{alt}]({image})"]
        if explanation:
            lines.extend(["", f"> {explanation}"])
        lines.append("")
        return lines

    def _render_visual_note_markdown(self, observations: list[dict[str, object]]) -> str:
        if not observations:
            return ""
        sections = ["## 图文笔记素材"]
        for item in observations:
            timestamp = self._format_seconds(float(item.get("timestamp_seconds") or 0))
            frame_id = str(item.get("frame_id") or "")
            caption = str(item.get("caption") or "关键画面截图").strip()
            ocr_text = str(item.get("ocr_text") or "").strip()
            image_name = f"{frame_id}.jpg" if frame_id else ""
            sections.append("")
            sections.append(f"### {timestamp} {caption}")
            if image_name:
                sections.append(f"![{timestamp} {caption}](frames/{image_name})")
            if ocr_text:
                sections.append("")
                sections.append(f"- 画面文字：{ocr_text}")
        return "\n".join(sections).strip()

    def _public_visual_frame(self, frame: dict[str, object]) -> dict[str, object]:
        return {key: value for key, value in frame.items() if not str(key).startswith("_")}

    def _build_visual_context_payload(
        self,
        *,
        task_id: str,
        status: str,
        source_kind: str,
        frames: list[dict[str, object]],
        observations: list[dict[str, object]],
        warnings: list[str],
        note_path: Path,
        enhanced_note_path: Path,
        frame_index_path: Path,
        keyframe_plan_path: Path,
        insert_plan_path: Path,
        mode: str,
        insert_count: int,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "task_id": task_id,
            "status": status,
            "source_kind": source_kind,
            "provider": "openai_compatible" if self._visual_llm_available() else "none",
            "model": self._visual_llm_config()[2] if self._visual_llm_available() else "",
            "visual_note_path": note_path.name,
            "visual_enhanced_note_path": enhanced_note_path.name,
            "frame_index_path": frame_index_path.name,
            "visual_keyframe_plan_path": keyframe_plan_path.name,
            "visual_insert_plan_path": insert_plan_path.name,
            "frame_count": len(observations),
            "insert_count": insert_count,
            "mode": mode,
            "frames": frames,
            "observations": observations,
            "warnings": warnings,
        }

    def _write_json_atomic(self, path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)

    def _sha256_file(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _build_mindmap_messages(
        self,
        title: str,
        summary_json: str,
        knowledge_note_markdown: str,
    ) -> list[dict[str, str]]:
        system_prompt = (
            self._settings.mindmap_system_prompt.strip()
            if self._settings.mindmap_system_prompt.strip()
            else (
                "你是一名擅长把学习内容重新组织为知识导图的中文内容编辑。"
                "你的任务是基于已有结构化摘要和知识笔记，输出一个适合思维导图展示、信息密度充足、覆盖完整的 JSON 树。"
                "所有内容都必须忠实原文，不得编造，不得补充外部资料，不得输出 JSON 以外的任何文字。"
                "You must return valid json only."
            )
        )
        user_template = (
            self._settings.mindmap_user_prompt_template.strip()
            if self._settings.mindmap_user_prompt_template.strip()
            else """请阅读下面的视频资料，并输出一个 JSON 对象。
注意：你必须返回合法的 json 对象，且只返回 json。

目标：
把当前视频内容组织成一棵真正“像思维导图”的知识树。它必须以概念、主题、方法、结论之间的关系为核心，而不是把章节标题换个层级重新排列。最末层节点仍然必须能回到原视频片段。

强约束：
1. 顶层只允许包含 title、root、nodes 三个字段。
2. root 必须是整棵导图的根节点 id。
3. nodes 必须是数组，其中包含唯一的根节点；每个节点必须包含 id、label、type、summary、children、time_anchor、source_chapter_titles、source_chapter_starts。
4. type 只能是 root、theme、topic、leaf 之一。
5. 整体结构必须是树，最大深度为 root -> theme -> topic/leaf -> leaf。
6. 顶层 theme 数量应为 4 到 8 个，每个 theme 下应有 3 到 6 个 topic 或 leaf；除非原内容本身很短，否则不要生成过于稀疏的导图。
7. leaf 节点必须能映射到原章节，并带真实时间点；time_anchor 必须取自 source_chapter_starts 中最早的时间点。
8. source_chapter_titles 和 source_chapter_starts 只保留最相关的 1 到 3 项，且数量一致。
9. label 必须是有内容的主题名，禁止“主题1”“Part 1”“Section 1”等占位标题。
10. summary 要适合学习复盘，直接写信息本体，不要重复整段知识笔记；theme/topic 的 summary 尽量写成 2 到 4 句，leaf 的 summary 至少要交代“结论 / 方法 / 条件 / 例子”中的两项。
11. label 和 summary 内如果出现数学内容，优先使用 KaTeX 兼容的 LaTeX 写法，例如 `$\\frac{1}{n}$`、`$(-1)^n$`、`$\\varepsilon$-$N$`；不要输出无法解析的伪公式。
12. 只允许输出 JSON；但 JSON 字符串内部允许包含少量 Markdown 和 `$...$` / `$$...$$` 数学公式。
13. 不要把 chapters 或 chapterGroups 直接一一平移成 theme/topic；必须先做语义归纳，再组织层级。
14. 如果多个章节都在讲同一个概念、同一种方法、同一类例子，应该聚合成一个主题，而不是拆成多个并列节点。
15. 导图的每一层都应体现“父主题如何拆成子主题”，而不是简单的时间顺序。

写作要求：
- 优先按“概念定义 / 推导方法 / 典型例子 / 易错点 / 结论判断 / 应用条件”这类知识结构重组。
- 根节点应该是整支视频真正的学习主题，不要只是视频标题原样重复，除非标题本身已经是明确概念。
- theme 层应该是 4 到 8 个最核心的大主题，彼此之间要有明显区分。
- topic 层应承担细化作用，只有当某个 theme 下确实存在两到三类不同子议题时才保留 topic；否则可直接挂 leaf。
- leaf 节点要具体、短促、可点击后立刻看懂，不要写成长句，也不要只是“第X部分”。
- 允许把多个来源章节压缩成一个更抽象、更像脑图节点的表达。
- 如果原文本身是教程或知识讲解，优先提炼“知识结构”；如果原文是评论或资讯，优先提炼“观点结构”和“因果关系”。
- 若视频包含公式、定义、判别条件、证明步骤、典型例题，不要省略它们；应把它们拆成独立主题或叶子节点，而不是只保留一个笼统标题。
- 不要怕信息多，只要层级清楚即可；优先保证“覆盖完整”和“节点可学”，不要只给每个主题一个空泛标签。
- 对于数学/理工类内容，优先把“定义、命题、判别条件、证明思路、典型例题、易错点”拆成不同节点；不要把整段证明压成一句泛泛描述。
- 如果一个 theme 下只生成了 1 个叶子节点，优先继续细化，除非原文确实只讲了这一点。
- 如果知识笔记已经给出分点、例题或条件，你应该把这些信息展开到对应节点，而不是只复述 theme 名称。
- 最终观感要像学习者自己整理出来的脑图，而不是讲稿目录。

输出格式示例：
{{"title":"","root":"root","nodes":[{{"id":"root","label":"","type":"root","summary":"","children":[{{"id":"theme-1","label":"","type":"theme","summary":"","children":[{{"id":"leaf-1","label":"","type":"leaf","summary":"","children":[],"time_anchor":0,"source_chapter_titles":[""],"source_chapter_starts":[0]}}],"source_chapter_titles":[],"source_chapter_starts":[]}}],"source_chapter_titles":[],"source_chapter_starts":[]}}]}}

视频标题：
{title}

已有结构化摘要：
{summary_json}

知识笔记：
{knowledge_note_markdown}"""
        )
        return [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": self._render_user_prompt_template(
                    user_template,
                    title=title,
                    transcript_excerpt="",
                    segments_excerpt="",
                    summary_json=summary_json,
                    knowledge_note_markdown=knowledge_note_markdown,
                ),
            },
        ]

    def _build_llm_mindmap_payload(
        self,
        title: str,
        summary_json: str,
        knowledge_note_markdown: str,
    ) -> dict[str, object]:
        messages = self._build_mindmap_messages(title, summary_json, knowledge_note_markdown)
        messages = self._ensure_json_keyword_in_messages(messages)
        return {
            "model": self._settings.llm_model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "enable_thinking": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }

    def _generate_mindmap_with_llm(
        self,
        title: str,
        result: TaskResult,
    ) -> TaskMindMap:
        base_url = (self._settings.llm_base_url or "").rstrip("/")
        if not base_url or not self._settings.llm_model:
            raise LLMConfigurationError("LLM 配置不完整，请检查 Base URL 和模型名。")

        summary_json = _truncate_text(
            json.dumps(
                {
                    "title": title,
                    "overview": result.overview,
                    "keyPoints": result.key_points,
                    "chapters": result.timeline,
                    "chapterGroups": result.chapter_groups,
                },
                ensure_ascii=False,
            ),
            9000,
        )
        knowledge_note_markdown = _truncate_text(result.knowledge_note_markdown or "", 12000)
        logger.info(
            "llm mindmap request model=%s summary_chars=%d knowledge_note_chars=%d key_points=%d chapters=%d chapter_groups=%d",
            self._settings.llm_model,
            len(summary_json),
            len(knowledge_note_markdown),
            len(result.key_points or []),
            len(result.timeline or []),
            len(result.chapter_groups or []),
        )
        payload = self._build_llm_mindmap_payload(title, summary_json, knowledge_note_markdown)
        llm_result = self._request_llm_json(base_url=base_url, payload=payload)
        return self._normalize_mindmap_payload(llm_result, title=title, result=result)

    def _normalize_mindmap_payload(
        self,
        payload: dict[str, object],
        *,
        title: str,
        result: TaskResult,
    ) -> TaskMindMap:
        chapters = [
            {
                "title": str(item.get("title") or "").strip(),
                "start": float(item.get("start") or 0),
                "summary": str(item.get("summary") or "").strip(),
            }
            for item in result.timeline
            if isinstance(item, dict)
        ]
        root_id = str(payload.get("root") or "root").strip() or "root"
        raw_nodes = payload.get("nodes")
        root_node_payload: dict[str, object] | None = None
        if isinstance(raw_nodes, list):
            for item in raw_nodes:
                if isinstance(item, dict) and str(item.get("id") or "").strip() == root_id:
                    root_node_payload = item
                    break
            if root_node_payload is None:
                root_node_payload = next((item for item in raw_nodes if isinstance(item, dict)), None)
        elif isinstance(raw_nodes, dict):
            root_node_payload = raw_nodes
        if root_node_payload is None:
            root_node_payload = {"id": root_id, "label": title, "type": "root", "summary": result.overview, "children": []}

        used_ids: set[str] = set()
        root_node = self._normalize_mindmap_node(
            root_node_payload,
            depth=0,
            title=title,
            chapters=chapters,
            used_ids=used_ids,
        )
        root_node.id = root_id
        root_node.type = "root"
        if not root_node.label.strip():
            root_node.label = title

        if len(root_node.children) > 8:
            root_node.children = root_node.children[:8]

        return TaskMindMap(version=1, title=str(payload.get("title") or title).strip() or title, root=root_node.id, nodes=[root_node])

    def _normalize_mindmap_node(
        self,
        payload: dict[str, object],
        *,
        depth: int,
        title: str,
        chapters: list[dict[str, object]],
        used_ids: set[str],
    ) -> MindMapNode:
        raw_children = payload.get("children")
        child_payloads = [item for item in raw_children if isinstance(item, dict)] if isinstance(raw_children, list) else []
        normalized_children: list[MindMapNode] = []
        if depth < 3:
            max_children = 8 if depth == 0 else 6
            for child in child_payloads[:max_children]:
                normalized_children.append(
                    self._normalize_mindmap_node(
                        child,
                        depth=depth + 1,
                        title=title,
                        chapters=chapters,
                        used_ids=used_ids,
                    )
                )

        label = self._normalize_content_title(
            str(payload.get("label") or payload.get("title") or "").strip(),
            fallback_text=str(payload.get("summary") or "").strip(),
            fallback_prefix="主题" if depth <= 1 else "节点",
            fallback_index=max(1, len(used_ids) + 1),
        )
        summary = _truncate_text(str(payload.get("summary") or "").strip(), 360)
        source_titles = [str(item).strip() for item in (payload.get("source_chapter_titles") or []) if str(item).strip()]
        source_starts: list[float] = []
        for item in (payload.get("source_chapter_starts") or []):
            if item is None or str(item).strip() == "":
                continue
            try:
                source_starts.append(float(item))
            except (TypeError, ValueError):
                continue

        if not source_titles and not source_starts:
            inferred_titles, inferred_starts = self._infer_mindmap_sources(label, summary, chapters)
            source_titles = inferred_titles
            source_starts = inferred_starts

        if len(source_titles) > 3:
            source_titles = source_titles[:3]
        if len(source_starts) > 3:
            source_starts = source_starts[:3]
        pair_count = min(len(source_titles), len(source_starts))
        if pair_count:
            source_titles = source_titles[:pair_count]
            source_starts = source_starts[:pair_count]

        raw_time_anchor = payload.get("time_anchor")
        time_anchor = float(raw_time_anchor) if raw_time_anchor is not None and str(raw_time_anchor).strip() != "" else None
        if time_anchor is None and source_starts:
            time_anchor = min(source_starts)
        if time_anchor is None and normalized_children:
            child_anchors = [child.time_anchor for child in normalized_children if child.time_anchor is not None]
            if child_anchors:
                time_anchor = min(child_anchors)

        node_type = str(payload.get("type") or "").strip().lower()
        if depth == 0:
            node_type = "root"
        elif depth == 1:
            node_type = "theme"
        elif normalized_children and depth < 3:
            node_type = "topic"
        else:
            node_type = "leaf"
            normalized_children = []

        if node_type == "leaf" and time_anchor is None:
            inferred_titles, inferred_starts = self._infer_mindmap_sources(label, summary, chapters)
            if inferred_starts:
                source_titles = inferred_titles
                source_starts = inferred_starts
                time_anchor = min(inferred_starts)

        node_id = self._normalize_mindmap_node_id(str(payload.get("id") or "").strip(), label, node_type, used_ids)

        return MindMapNode(
            id=node_id,
            label=label or ("根节点" if depth == 0 else "主题"),
            type=node_type,
            summary=summary,
            children=normalized_children,
            time_anchor=time_anchor if node_type == "leaf" else None,
            source_chapter_titles=source_titles,
            source_chapter_starts=source_starts,
        )

    def _normalize_mindmap_node_id(
        self,
        raw_id: str,
        label: str,
        node_type: str,
        used_ids: set[str],
    ) -> str:
        base = raw_id or re.sub(r"[^a-z0-9]+", "-", self._dedupe_text_key(label or node_type)).strip("-")
        if not base:
            base = node_type
        candidate = base
        suffix = 2
        while candidate in used_ids:
            candidate = f"{base}-{suffix}"
            suffix += 1
        used_ids.add(candidate)
        return candidate

    def _infer_mindmap_sources(
        self,
        label: str,
        summary: str,
        chapters: list[dict[str, object]],
    ) -> tuple[list[str], list[float]]:
        if not chapters:
            return [], []
        query = self._dedupe_text_key(f"{label} {summary}")
        ranked: list[tuple[int, dict[str, object]]] = []
        for chapter in chapters:
            title = str(chapter.get("title") or "").strip()
            chapter_summary = str(chapter.get("summary") or "").strip()
            haystack = self._dedupe_text_key(f"{title} {chapter_summary}")
            score = 0
            if label and self._dedupe_text_key(label) and self._dedupe_text_key(label) in haystack:
                score += 5
            if summary and self._dedupe_text_key(summary) and self._dedupe_text_key(summary)[:12] in haystack:
                score += 4
            if query and haystack:
                score += len(set(query[:24]) & set(haystack[:24]))
            ranked.append((score, chapter))
        ranked.sort(key=lambda item: (-item[0], float(item[1].get("start") or 0)))
        selected = [item[1] for item in ranked[:3] if item[0] > 0] or [ranked[0][1]]
        titles = [str(item.get("title") or "").strip() for item in selected if str(item.get("title") or "").strip()]
        starts = [float(item.get("start") or 0) for item in selected]
        return titles[:3], starts[:3]

    def _build_summary_chunks(self, segments: list[dict[str, object]]) -> list[dict[str, object]]:
        if not segments:
            return []

        chunks: list[dict[str, object]] = []
        target_chars = max(800, int(self._settings.summary_chunk_target_chars))
        overlap = max(0, int(self._settings.summary_chunk_overlap_segments))
        start = 0
        index = 1
        total = len(segments)

        while start < total:
            current: list[dict[str, object]] = []
            current_chars = 0
            cursor = start
            while cursor < total:
                segment = segments[cursor]
                text = str(segment.get("text") or "").strip()
                estimated = len(text) + 24
                if current and current_chars + estimated > target_chars:
                    break
                current.append(segment)
                current_chars += estimated
                cursor += 1

            if not current:
                current = [segments[start]]
                cursor = start + 1

            chunk_lines = [
                f"[{self._format_seconds(float(item.get('start') or 0))}] {str(item.get('text') or '').strip()}"
                for item in current
                if str(item.get("text") or "").strip()
            ]
            compact_segments = [
                {
                    "start": float(item.get("start") or 0),
                    "text": _truncate_text(str(item.get("text") or "").strip(), 120),
                }
                for item in current
                if str(item.get("text") or "").strip()
            ]
            chunks.append(
                {
                    "index": index,
                    "transcript": _truncate_text("\n".join(chunk_lines), target_chars + 400),
                    "segments_json": _truncate_text(json.dumps(compact_segments, ensure_ascii=False), target_chars + 400),
                    "chunk_start": float(current[0].get("start") or 0),
                    "chunk_end": float(current[-1].get("end") or current[-1].get("start") or 0),
                    "source_segment_count": len(current),
                }
            )
            index += 1
            if cursor >= total:
                break
            start = max(cursor - overlap, start + 1)
        return chunks

    def _build_aggregate_summary_inputs(
        self,
        partial_summaries: list[dict[str, object]],
        merged_chapters: list[dict[str, object]] | None = None,
    ) -> tuple[str, str]:
        lines: list[str] = []
        segments: list[dict[str, object]] = []
        final_chapters = merged_chapters or []

        if final_chapters:
            lines.append("## 合并后的全局章节")
            for chapter in final_chapters:
                start = float(chapter.get("start") or 0)
                chapter_title = str(chapter.get("title") or "").strip()
                summary = str(chapter.get("summary") or "").strip()
                if not summary:
                    continue
                lines.append(f"[{self._format_seconds(start)}] {chapter_title}：{summary}")
                segments.append(
                    {
                        "start": start,
                        "text": _truncate_text(f"{chapter_title}：{summary}", 180),
                    }
                )
            lines.append("")

        for item in partial_summaries:
            chunk_index = int(item.get("chunk_index") or 0)
            title = str(item.get("title") or f"分块 {chunk_index}")
            overview = str(item.get("overview") or "").strip()
            bullet_points = [str(point).strip() for point in item.get("bulletPoints") or [] if str(point).strip()]
            chapters = [chapter for chapter in item.get("chapters") or [] if isinstance(chapter, dict)]

            lines.append(f"### 分块 {chunk_index}: {title}")
            if overview:
                lines.append(f"概览：{overview}")
            for point in bullet_points[:6]:
                lines.append(f"- {point}")
            for chapter in chapters[:6]:
                start = float(chapter.get("start") or 0)
                summary = str(chapter.get("summary") or "").strip()
                chapter_title = str(chapter.get("title") or f"章节 {chunk_index}")
                if summary:
                    lines.append(f"[{self._format_seconds(start)}] {chapter_title}：{summary}")
            lines.append("")
        return _truncate_text("\n".join(lines).strip(), 5200), _truncate_text(json.dumps(segments, ensure_ascii=False), 2600)

    def _format_seconds(self, value: float) -> str:
        total = max(0, int(value))
        minutes = total // 60
        seconds = total % 60
        return f"{minutes:02d}:{seconds:02d}"

    def _build_transcript_excerpt(self, transcript: str) -> str:
        lines = [line.strip() for line in transcript.splitlines() if line.strip()]
        if len(lines) <= 120:
            return _truncate_text("\n".join(lines), 3600)
        head = lines[:70]
        tail = lines[-35:]
        return _truncate_text("\n".join(head + ["[...中间转写已省略...]"] + tail), 3600)

    def _build_aggregate_series_excerpt(self, transcript: str) -> str:
        blocks = [block.strip() for block in re.split(r"\n(?=##\s*P\d+\s)", transcript) if block.strip()]
        if not blocks:
            return _truncate_text(transcript, 10_000)

        # 根据总 P 数动态调整压缩策略
        page_count = len(blocks)

        # 动态计算每 P 的字符预算，保证所有 P 都能被包含
        if page_count <= 20:
            per_page_budget = 500
            total_budget = 15_000
        elif page_count <= 40:
            per_page_budget = 350
            total_budget = 18_000
        elif page_count <= 60:
            per_page_budget = 250
            total_budget = 20_000
        else:
            per_page_budget = 180
            total_budget = 24_000

        # 优先级：概览 > 要点 > 章节 > 笔记
        compact_blocks: list[str] = []
        for block in blocks:
            lines = [line.strip() for line in block.splitlines() if line.strip()]
            if not lines:
                continue

            head = lines[0]  # ## P 数 标题
            remaining_budget = per_page_budget - len(head) - 2

            # 分类不同优先级的行
            overview_lines = []
            keypoint_lines = []
            chapter_lines = []
            note_lines = []

            for line in lines[1:]:
                if not line.startswith("[重点-"):
                    continue
                if line.startswith("[重点 - 概览]"):
                    overview_lines.append(line)
                elif line.startswith("[重点 - 要点]"):
                    keypoint_lines.append(line)
                elif line.startswith("[重点 - 章节]"):
                    chapter_lines.append(line)
                elif line.startswith("[重点 - 笔记]"):
                    note_lines.append(line)

            # 按优先级选择行，确保高优先级内容优先保留
            selected_lines = []

            # 1. 概览优先级最高，最多保留 1 条
            for line in overview_lines[:1]:
                truncated = _truncate_text(line, min(remaining_budget - 2, 200))
                if truncated:
                    selected_lines.append(truncated)
                    remaining_budget -= len(truncated) + 2

            # 2. 要点优先级次之，最多保留 2 条
            for line in keypoint_lines[:2]:
                if remaining_budget <= 20:
                    break
                truncated = _truncate_text(line, min(remaining_budget - 2, 150))
                if truncated:
                    selected_lines.append(truncated)
                    remaining_budget -= len(truncated) + 2

            # 3. 章节和笔记根据剩余预算决定
            if remaining_budget > 40:
                # 章节和笔记合并考虑，优先章节
                other_lines = chapter_lines[:2] + note_lines[:1]
                for line in other_lines:
                    if remaining_budget <= 30:
                        break
                    truncated = _truncate_text(line, min(remaining_budget - 2, 100))
                    if truncated:
                        selected_lines.append(truncated)
                        remaining_budget -= len(truncated) + 2

            compact_blocks.append("\n".join([head, *selected_lines]))

        return _truncate_text("\n\n".join(compact_blocks), total_budget)

    def _build_aggregate_series_segments_excerpt(self, segments: list[dict[str, object]]) -> str:
        seen_pages: set[int] = set()
        compact_segments: list[dict[str, object]] = []

        # 根据总 P 数动态调整每 P 的字符预算
        total_pages = len({int(float(item.get("start") or 0)) for item in segments if item.get("start") is not None})

        if total_pages <= 20:
            per_page_label_limit = 150
            total_limit = 4800
        elif total_pages <= 40:
            per_page_label_limit = 120
            total_limit = 6000
        elif total_pages <= 60:
            per_page_label_limit = 100
            total_limit = 7200
        else:
            per_page_label_limit = 80
            total_limit = 9600

        for item in segments:
            try:
                page_number = int(float(item.get("start") or 0))
            except (TypeError, ValueError):
                continue
            if page_number <= 0 or page_number in seen_pages:
                continue
            seen_pages.add(page_number)
            compact_segments.append(
                {
                    "page": page_number,
                    "label": _truncate_text(str(item.get("text") or "").strip(), per_page_label_limit),
                }
            )
        return _truncate_text(json.dumps(compact_segments, ensure_ascii=False), total_limit)

    def _build_segments_excerpt(self, segments: list[dict[str, object]]) -> str:
        if len(segments) <= 32:
            selected = segments
        else:
            head = segments[:12]
            middle_start = max(12, len(segments) // 2 - 4)
            middle = segments[middle_start : middle_start + 8]
            tail = segments[-12:]
            selected = [*head, {"start": "...", "text": "中间分段已省略"}, *middle, *tail]

        compact_segments: list[dict[str, object]] = []
        for item in selected:
            compact_segments.append(
                {
                    "start": item.get("start"),
                    "text": _truncate_text(str(item.get("text") or ""), 72),
                }
            )
        return _truncate_text(json.dumps(compact_segments, ensure_ascii=False), 1800)

    def _summarize_with_rules(
        self,
        transcript: str,
        segments: list[dict[str, object]],
        title: str,
    ) -> dict[str, object]:
        lines = [line.strip() for line in transcript.splitlines() if line.strip()]
        overview = "\n".join(lines[:3])[:400]
        bullet_points = [line.split("] ", 1)[-1][:80] for line in lines[:5]]
        chapters = []
        if segments:
            step = max(1, len(segments) // 4)
            for index in range(0, len(segments), step):
                group = segments[index : index + step]
                chapters.append(
                    {
                        "title": f"章节 {len(chapters) + 1}",
                        "start": group[0]["start"],
                        "summary": " ".join(str(item["text"]) for item in group)[:120],
                    }
                )
                if len(chapters) >= 4:
                    break
        return {
            "title": title or "视频摘要",
            "overview": overview,
            "bulletPoints": bullet_points,
            "chapters": chapters,
            "chapterGroups": self._build_chapter_groups_from_chapters(chapters),
            "knowledgeNoteMarkdown": self._build_knowledge_note_markdown(
                title=title or "视频摘要",
                overview=overview,
                bullet_points=bullet_points,
                chapters=chapters,
                transcript=transcript,
            ),
        }

    def _normalize_summary(
        self,
        summary: dict[str, object],
        transcript: str,
        segments: list[dict[str, object]],
        title: str,
    ) -> dict[str, object]:
        normalized = dict(summary)
        normalized["title"] = str(normalized.get("title") or title or "视频摘要")

        overview = str(normalized.get("overview") or "").strip()
        if not overview:
            overview = self._build_overview_fallback(transcript)
        normalized["overview"] = overview

        bullet_points = self._coerce_bullet_points(normalized.get("bulletPoints"))
        if not bullet_points:
            bullet_points = self._build_bullet_points_fallback(overview, transcript, segments)
        normalized["bulletPoints"] = bullet_points

        chapters = self._coerce_chapters(normalized.get("chapters"), segments)
        if not chapters:
            chapters = self._build_chapters_fallback(segments)
        normalized["chapters"] = chapters
        chapter_groups = self._coerce_chapter_groups(normalized.get("chapterGroups"), chapters)
        if not chapter_groups:
            chapter_groups = self._build_chapter_groups_from_chapters(chapters)
        normalized["chapterGroups"] = chapter_groups

        knowledge_note_markdown = str(normalized.get("knowledgeNoteMarkdown") or "").strip()
        if not knowledge_note_markdown:
            knowledge_note_markdown = self._build_knowledge_note_markdown(
                title=str(normalized["title"]),
                overview=overview,
                bullet_points=bullet_points,
                chapters=chapters,
                transcript=transcript,
            )
        normalized["knowledgeNoteMarkdown"] = knowledge_note_markdown
        return normalized

    def _build_knowledge_note_markdown(
        self,
        title: str,
        overview: str,
        bullet_points: list[str],
        chapters: list[dict[str, object]],
        transcript: str,
    ) -> str:
        sections: list[str] = [f"# {title or '知识笔记'}"]

        if overview:
            sections.extend(["", "## 核心概览", "", overview.strip()])

        if bullet_points:
            sections.extend(["", "## 关键要点", ""])
            sections.extend(f"- {point.strip()}" for point in bullet_points if point.strip())

        if chapters:
            sections.extend(["", "## 内容展开"])
            for index, chapter in enumerate(chapters, start=1):
                chapter_title = str(chapter.get("title") or f"章节 {index}").strip()
                chapter_summary = str(chapter.get("summary") or "").strip()
                start = float(chapter.get("start") or 0)
                sections.extend(
                    [
                        "",
                        f"### {chapter_title}",
                        "",
                        f"- 时间点：{self._format_seconds(start)}",
                    ]
                )
                if chapter_summary:
                    sections.extend(["", chapter_summary])

        transcript_lines = [line.strip() for line in transcript.splitlines() if line.strip()]
        if transcript_lines:
            sections.extend(["", "## 原文摘录", ""])
            excerpt = transcript_lines[:6]
            sections.extend(f"> {line}" for line in excerpt)
            if len(transcript_lines) > len(excerpt):
                sections.extend(["", "> ..."])

        return "\n".join(sections).strip()

    def _build_overview_fallback(self, transcript: str) -> str:
        lines = [line.strip() for line in transcript.splitlines() if line.strip()]
        return "\n".join(lines[:3])[:400]

    def _coerce_bullet_points(self, value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        items = [str(item).strip() for item in value if str(item).strip()]
        return items[:6]

    def _build_bullet_points_fallback(
        self,
        overview: str,
        transcript: str,
        segments: list[dict[str, object]],
    ) -> list[str]:
        points: list[str] = []
        if overview:
            chunks = [part.strip(" \n\r\t-•") for part in overview.replace("。", "。\n").splitlines()]
            for chunk in chunks:
                if chunk:
                    points.append(chunk[:80])
                if len(points) >= 4:
                    break

        if len(points) < 3:
            transcript_lines = [line.strip() for line in transcript.splitlines() if line.strip()]
            for line in transcript_lines:
                text = line.split("] ", 1)[-1].strip()
                if text and text not in points:
                    points.append(text[:80])
                if len(points) >= 5:
                    break

        if len(points) < 3 and segments:
            step = max(1, len(segments) // 5)
            for index in range(0, len(segments), step):
                text = str(segments[index].get("text") or "").strip()
                if text and text not in points:
                    points.append(text[:80])
                if len(points) >= 5:
                    break
        return points[:5]

    def _merge_structured_summary(
        self,
        merged: dict[str, object],
        partial_summaries: list[dict[str, object]],
        merged_chapters: list[dict[str, object]],
        transcript: str,
        segments: list[dict[str, object]],
        title: str,
    ) -> dict[str, object]:
        result = dict(merged)
        structural_chapters = merged_chapters or self._merge_partial_chapters(partial_summaries, segments)
        target_count = self._suggest_chapter_count(segments)
        llm_chapters = self._coerce_chapters(result.get("chapters"), segments)

        if len(llm_chapters) >= max(3, min(target_count, len(structural_chapters))) and len(llm_chapters) >= len(structural_chapters):
            final_chapters = llm_chapters
        else:
            final_chapters = structural_chapters or llm_chapters or self._build_chapters_fallback(segments)
        final_chapters = self._rebalance_chapters_for_coverage(final_chapters, segments)

        result["title"] = str(result.get("title") or title or "视频摘要").strip()
        result["overview"] = str(result.get("overview") or "").strip() or self._build_overview_from_partials(partial_summaries, transcript)
        bullet_points = self._coerce_bullet_points(result.get("bulletPoints"))
        if len(bullet_points) < min(5, max(3, math.ceil(target_count / 2))):
            bullet_points = self._build_bullet_points_from_partials(partial_summaries, final_chapters, result["overview"], transcript, segments)
        result["bulletPoints"] = bullet_points
        result["chapters"] = final_chapters
        result["chapterGroups"] = self._build_chapter_groups_from_chapters(final_chapters)
        return result

    def _merge_partial_chapters(
        self,
        partial_summaries: list[dict[str, object]],
        segments: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        merged: list[dict[str, object]] = []
        seen_keys: set[tuple[int, str]] = set()

        for partial in partial_summaries:
            chunk_start = float(partial.get("chunk_start") or 0)
            chunk_end = float(partial.get("chunk_end") or chunk_start)
            chapters = self._coerce_chapters(partial.get("chapters"), segments)
            for chapter in chapters:
                start = self._align_chapter_start(float(chapter.get("start") or chunk_start), segments, chunk_start, chunk_end)
                title = str(chapter.get("title") or "").strip()
                summary = str(chapter.get("summary") or "").strip()
                if not summary:
                    continue
                dedupe_key = (int(start), self._dedupe_text_key(title or summary))
                if dedupe_key in seen_keys:
                    continue
                seen_keys.add(dedupe_key)
                self._append_or_merge_chapter(
                    merged,
                    {
                        "title": title,
                        "start": start,
                        "summary": summary,
                    },
                )

        merged.sort(key=lambda item: float(item.get("start") or 0))
        normalized: list[dict[str, object]] = []
        for index, chapter in enumerate(merged, start=1):
            title = self._normalize_content_title(
                str(chapter.get("title") or "").strip(),
                fallback_text=str(chapter.get("summary") or "").strip(),
                fallback_prefix="章节",
                fallback_index=index,
            )
            normalized.append(
                {
                    "title": title,
                    "start": float(chapter.get("start") or 0),
                    "summary": str(chapter.get("summary") or "").strip()[:160],
                }
            )
        return self._rebalance_chapters_for_coverage(normalized, segments)

    def _append_or_merge_chapter(
        self,
        chapters: list[dict[str, object]],
        chapter: dict[str, object],
    ) -> None:
        start = float(chapter.get("start") or 0)
        title_key = self._dedupe_text_key(str(chapter.get("title") or ""))
        summary = str(chapter.get("summary") or "").strip()

        for existing in chapters:
            existing_start = float(existing.get("start") or 0)
            existing_title_key = self._dedupe_text_key(str(existing.get("title") or ""))
            if abs(existing_start - start) <= 45 or (title_key and title_key == existing_title_key):
                existing_summary = str(existing.get("summary") or "").strip()
                if len(summary) > len(existing_summary):
                    existing["summary"] = summary
                if len(str(chapter.get("title") or "")) > len(str(existing.get("title") or "")):
                    existing["title"] = str(chapter.get("title") or "")
                existing["start"] = min(existing_start, start)
                return
        chapters.append(dict(chapter))

    def _align_chapter_start(
        self,
        start: float,
        segments: list[dict[str, object]],
        chunk_start: float,
        chunk_end: float,
    ) -> float:
        if not segments:
            return max(0.0, start)
        candidates = [
            float(segment.get("start") or 0)
            for segment in segments
            if chunk_start - 1e-6 <= float(segment.get("start") or 0) <= max(chunk_end, chunk_start)
        ]
        if not candidates:
            candidates = [float(segment.get("start") or 0) for segment in segments]
        if not candidates:
            return max(0.0, start)
        return min(candidates, key=lambda item: abs(item - start))

    def _build_overview_from_partials(
        self,
        partial_summaries: list[dict[str, object]],
        transcript: str,
    ) -> str:
        sentences: list[str] = []
        for partial in partial_summaries:
            overview = str(partial.get("overview") or "").strip()
            if not overview:
                continue
            for piece in re.split(r"(?<=[。！？!?])", overview):
                clean_piece = piece.strip()
                if clean_piece and clean_piece not in sentences:
                    sentences.append(clean_piece)
                if len(sentences) >= 5:
                    return "".join(sentences)[:400]
        return self._build_overview_fallback(transcript)

    def _build_bullet_points_from_partials(
        self,
        partial_summaries: list[dict[str, object]],
        chapters: list[dict[str, object]],
        overview: str,
        transcript: str,
        segments: list[dict[str, object]],
    ) -> list[str]:
        points: list[str] = []
        for partial in partial_summaries:
            for point in partial.get("bulletPoints") or []:
                clean_point = str(point).strip()
                if not clean_point:
                    continue
                key = self._dedupe_text_key(clean_point)
                if any(self._dedupe_text_key(existing) == key for existing in points):
                    continue
                points.append(clean_point[:88])
                if len(points) >= 8:
                    return points

        for chapter in chapters:
            summary = str(chapter.get("summary") or "").strip()
            if not summary:
                continue
            candidate = summary[:88]
            key = self._dedupe_text_key(candidate)
            if any(self._dedupe_text_key(existing) == key for existing in points):
                continue
            points.append(candidate)
            if len(points) >= 8:
                return points

        fallback = self._build_bullet_points_fallback(overview, transcript, segments)
        for point in fallback:
            clean_point = str(point).strip()
            key = self._dedupe_text_key(clean_point)
            if clean_point and not any(self._dedupe_text_key(existing) == key for existing in points):
                points.append(clean_point)
            if len(points) >= 8:
                break
        return points[:8]

    def _dedupe_text_key(self, value: str) -> str:
        normalized = re.sub(r"[\W_]+", "", str(value or "").lower())
        return normalized[:24]

    def _coerce_chapters(
        self,
        value: object,
        segments: list[dict[str, object]] | None = None,
    ) -> list[dict[str, object]]:
        if not isinstance(value, list):
            return []
        chapters: list[dict[str, object]] = []
        limit = self._chapter_limit(segments or [])
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                continue
            summary = str(item.get("summary") or "").strip()
            if not summary:
                continue
            title = self._normalize_content_title(
                str(item.get("title") or "").strip(),
                fallback_text=summary,
                fallback_prefix="章节",
                fallback_index=index + 1,
            )
            chapters.append(
                {
                    "title": title,
                    "start": float(item.get("start") or 0),
                    "summary": summary[:160],
                }
            )
        return chapters[:limit]

    def _build_chapters_fallback(self, segments: list[dict[str, object]]) -> list[dict[str, object]]:
        if not segments:
            return []
        chapters: list[dict[str, object]] = []
        target_count = self._suggest_chapter_count(segments)
        step = max(1, math.ceil(len(segments) / max(1, target_count)))
        for index in range(0, len(segments), step):
            group = segments[index : index + step]
            if not group:
                continue
            summary = " ".join(str(item.get("text") or "").strip() for item in group if str(item.get("text") or "").strip())
            if not summary:
                continue
            chapters.append(
                {
                    "title": self._normalize_content_title(
                        "",
                        fallback_text=summary,
                        fallback_prefix="章节",
                        fallback_index=len(chapters) + 1,
                    ),
                    "start": float(group[0].get("start") or 0),
                    "summary": summary[:160],
                }
            )
            if len(chapters) >= self._chapter_limit(segments):
                break
        return self._rebalance_chapters_for_coverage(chapters, segments)

    def _coerce_chapter_groups(
        self,
        value: object,
        chapters: list[dict[str, object]] | None = None,
    ) -> list[dict[str, object]]:
        if not isinstance(value, list):
            return []
        groups: list[dict[str, object]] = []
        limit = self._chapter_group_limit(chapters or [])
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                continue
            children = self._coerce_chapters(item.get("children"), chapters)
            if not children:
                continue
            summary = str(item.get("summary") or "").strip()[:120]
            title = self._normalize_content_title(
                str(item.get("title") or "").strip(),
                fallback_text=summary,
                fallback_prefix="主题",
                fallback_index=index + 1,
                child_titles=[str(child.get("title") or "").strip() for child in children],
            )
            groups.append(
                {
                    "title": title,
                    "start": float(item.get("start") or children[0].get("start") or 0),
                    "summary": summary,
                    "children": children,
                }
            )
        return groups[:limit]

    def _build_chapter_groups_from_chapters(self, chapters: list[dict[str, object]]) -> list[dict[str, object]]:
        if not chapters:
            return []
        desired_groups = self._suggest_chapter_group_count(chapters)
        group_size = max(1, math.ceil(len(chapters) / max(1, desired_groups)))
        groups: list[dict[str, object]] = []
        for index in range(0, len(chapters), group_size):
            items = chapters[index : index + group_size]
            if not items:
                continue
            first = items[0]
            last = items[-1]
            summary = "；".join(str(item.get("title") or "").strip() for item in items if str(item.get("title") or "").strip())
            groups.append(
                {
                    "title": self._normalize_content_title(
                        "",
                        fallback_text=summary,
                        fallback_prefix="主题",
                        fallback_index=len(groups) + 1,
                        child_titles=[str(item.get("title") or "").strip() for item in items],
                    ),
                    "start": float(first.get("start") or 0),
                    "summary": summary[:120] or f"{self._format_seconds(float(first.get('start') or 0))} - {self._format_seconds(float(last.get('start') or 0))}",
                    "children": items,
                }
            )
        return groups[: self._chapter_group_limit(chapters)]

    def _chapter_limit(self, segments: list[dict[str, object]]) -> int:
        if not segments:
            return 12

        # 检测是否为全集总结场景：segments 的 start 值是连续整数（分 P 号），且范围较大
        is_aggregate_series = self._is_aggregate_series_segments(segments)

        if is_aggregate_series:
            # 全集总结场景：根据 P 数决定限制，最多允许 100 个章节
            page_count = len({int(float(item.get("start") or 0)) for item in segments if item.get("start") is not None})
            return max(20, min(100, page_count))

        # 普通单 P 视频场景：基于时长计算
        target = self._suggest_chapter_count(segments)
        return max(8, min(48, target * 2))

    def _is_aggregate_series_segments(self, segments: list[dict[str, object]]) -> bool:
        """检测 segments 是否属于全集总结场景（start 值为分 P 号而非时间戳）"""
        if len(segments) < 5:
            return False

        # 提取所有 start 值并转换为整数
        start_values = []
        for item in segments:
            start = item.get("start")
            if start is not None:
                try:
                    start_values.append(int(float(start)))
                except (TypeError, ValueError):
                    continue

        if len(start_values) < 5:
            return False

        # 检查是否为连续或接近连续的整数序列（分 P 号）
        # 全集总结的 segments 通常 start 值范围较大且为整数
        min_start = min(start_values)
        max_start = max(start_values)
        range_span = max_start - min_start

        # 如果 start 值范围超过 10 且平均间隔接近 1，判断为全集总结
        if range_span > 10:
            unique_starts = sorted(set(start_values))
            if len(unique_starts) >= 5:
                # 计算平均间隔
                avg_gap = (unique_starts[-1] - unique_starts[0]) / (len(unique_starts) - 1) if len(unique_starts) > 1 else 1
                # 如果平均间隔在 0.8-1.5 之间，很可能是分 P 号
                return 0.8 <= avg_gap <= 1.5

        return False

    def _chapter_group_limit(self, chapters: list[dict[str, object]]) -> int:
        if not chapters:
            return 5
        return max(2, min(8, self._suggest_chapter_group_count(chapters) + 1))

    def _suggest_chapter_count(self, segments: list[dict[str, object]]) -> int:
        if not segments:
            return 4

        # 检测是否为全集总结场景
        is_aggregate_series = self._is_aggregate_series_segments(segments)

        if is_aggregate_series:
            # 全集总结场景：根据 P 数决定章节数，每 3-5P 一个章节
            page_count = len({int(float(item.get("start") or 0)) for item in segments if item.get("start") is not None})
            # 目标章节数：每 4P 一个章节，最少 8 个，最多 50 个
            return max(8, min(50, math.ceil(page_count / 4)))

        # 普通单 P 视频场景：基于时长计算
        first_start = float(segments[0].get("start") or 0)
        last_end = float(segments[-1].get("end") or segments[-1].get("start") or first_start)
        duration = max(0.0, last_end - first_start)
        segment_count = len(segments)
        duration_minutes = duration / 60.0 if duration > 0 else 0.0
        duration_target = math.ceil(duration_minutes / 3.5) if duration_minutes > 0 else 4
        density_target = math.ceil(segment_count / 120) if segment_count > 0 else 0
        return max(4, min(28, max(duration_target, density_target, 4)))

    def _suggest_chapter_group_count(self, chapters: list[dict[str, object]]) -> int:
        chapter_count = len(chapters)
        return max(1, min(8, math.ceil(chapter_count / 4)))

    def _rebalance_chapters_for_coverage(
        self,
        chapters: list[dict[str, object]],
        segments: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        if not chapters:
            return []

        # 检测是否为全集总结场景
        is_aggregate_series = self._is_aggregate_series_segments(segments)

        ordered = sorted(chapters, key=lambda item: float(item.get("start") or 0))
        target_count = self._suggest_chapter_count(segments)

        if len(ordered) <= target_count:
            return ordered

        # 全集总结场景：使用基于 P 数的窗口划分
        if is_aggregate_series:
            page_count = len({int(float(item.get("start") or 0)) for item in segments if item.get("start") is not None})
            window_size = max(3.0, page_count / max(1, target_count))  # 每窗口约 3-5P

            buckets: list[list[dict[str, object]]] = []
            for chapter in ordered:
                start = float(chapter.get("start") or 0)
                bucket_index = min(target_count - 1, max(0, int((start - 1) / window_size)))
                while len(buckets) <= bucket_index:
                    buckets.append([])
                buckets[bucket_index].append(chapter)

            merged_buckets: list[dict[str, object]] = []
            for bucket in buckets:
                if not bucket:
                    continue
                merged_buckets.append(self._merge_chapter_bucket(bucket))

            if len(merged_buckets) > target_count:
                sampled: list[dict[str, object]] = []
                for index in range(target_count):
                    source_index = min(len(merged_buckets) - 1, round(index * (len(merged_buckets) - 1) / max(1, target_count - 1)))
                    sampled.append(merged_buckets[source_index])
                deduped: list[dict[str, object]] = []
                seen: set[tuple[int, str]] = set()
                for chapter in sampled:
                    key = (int(float(chapter.get("start") or 0)), self._dedupe_text_key(str(chapter.get("title") or "")))
                    if key in seen:
                        continue
                    seen.add(key)
                    deduped.append(chapter)
                merged_buckets = deduped

            return merged_buckets

        # 普通单 P 视频场景：基于时长的窗口划分
        first_start = float(segments[0].get("start") or ordered[0].get("start") or 0) if segments else float(ordered[0].get("start") or 0)
        last_end = (
            float(segments[-1].get("end") or segments[-1].get("start") or ordered[-1].get("start") or first_start)
            if segments
            else float(ordered[-1].get("start") or first_start)
        )
        duration = max(1.0, last_end - first_start)
        window_size = max(45.0, duration / max(1, target_count))

        buckets: list[list[dict[str, object]]] = []
        for chapter in ordered:
            start = float(chapter.get("start") or 0)
            bucket_index = min(target_count - 1, max(0, int((start - first_start) / window_size)))
            while len(buckets) <= bucket_index:
                buckets.append([])
            buckets[bucket_index].append(chapter)

        merged_buckets: list[dict[str, object]] = []
        for bucket in buckets:
            if not bucket:
                continue
            merged_buckets.append(self._merge_chapter_bucket(bucket))

        if len(merged_buckets) > target_count:
            sampled: list[dict[str, object]] = []
            for index in range(target_count):
                source_index = min(len(merged_buckets) - 1, round(index * (len(merged_buckets) - 1) / max(1, target_count - 1)))
                sampled.append(merged_buckets[source_index])
            deduped: list[dict[str, object]] = []
            seen: set[tuple[int, str]] = set()
            for chapter in sampled:
                key = (int(float(chapter.get("start") or 0)), self._dedupe_text_key(str(chapter.get("title") or "")))
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(chapter)
            merged_buckets = deduped

        return merged_buckets

    def _merge_chapter_bucket(self, bucket: list[dict[str, object]]) -> dict[str, object]:
        first = bucket[0]
        titles = [str(item.get("title") or "").strip() for item in bucket if str(item.get("title") or "").strip()]
        summaries = [str(item.get("summary") or "").strip() for item in bucket if str(item.get("summary") or "").strip()]
        title = self._normalize_content_title(
            titles[0] if titles else "",
            fallback_text="；".join(summaries),
            fallback_prefix="章节",
            fallback_index=1,
            child_titles=titles[1:],
        )
        summary_parts: list[str] = []
        seen_summary_keys: set[str] = set()
        for summary in summaries:
            key = self._dedupe_text_key(summary)
            if not key or key in seen_summary_keys:
                continue
            seen_summary_keys.add(key)
            summary_parts.append(summary)
            if len("；".join(summary_parts)) >= 150:
                break
        return {
            "title": title,
            "start": float(first.get("start") or 0),
            "summary": "；".join(summary_parts)[:160],
        }

    def _normalize_content_title(
        self,
        title: str,
        fallback_text: str = "",
        fallback_prefix: str = "章节",
        fallback_index: int = 1,
        child_titles: list[str] | None = None,
    ) -> str:
        clean_title = self._clean_title(title)
        if clean_title and not self._is_placeholder_title(clean_title):
            return clean_title

        for child_title in child_titles or []:
            normalized = self._clean_title(child_title)
            if normalized and not self._is_placeholder_title(normalized):
                return normalized

        derived_from_text = self._derive_title_from_text(fallback_text)
        if derived_from_text:
            return derived_from_text

        return f"{fallback_prefix} {fallback_index}"

    def _clean_title(self, value: str) -> str:
        title = str(value or "").strip()
        title = re.sub(r"^[\-•\d\.\)\(、\s]+", "", title)
        if not title:
            return ""
        if self._looks_like_rich_title(title):
            return self._truncate_rich_title(title, max_length=52, extension=24)
        return title[:24]

    def _looks_like_rich_title(self, value: str) -> bool:
        title = str(value or "")
        return bool("$" in title or re.search(r"\\[A-Za-z]+|[_^{}]", title))

    def _truncate_rich_title(self, value: str, *, max_length: int, extension: int = 0) -> str:
        title = str(value or "").strip()
        if len(title) <= max_length:
            return title

        in_math = False
        last_safe_break = None
        last_safe_end = None
        hard_limit = min(len(title), max_length + max(0, extension))

        for index, char in enumerate(title[:hard_limit]):
            if char == "$" and (index == 0 or title[index - 1] != "\\"):
                in_math = not in_math
            if not in_math:
                last_safe_end = index + 1
                if char.isspace() or char in "，,。；;：:、)]）】":
                    last_safe_break = index + 1
                if index + 1 >= max_length:
                    break

        if in_math:
            opening_index = title.rfind("$", 0, hard_limit)
            if opening_index >= 8:
                return title[:opening_index].rstrip("，,。；;：:、 ")

        safe_cut = last_safe_break or last_safe_end
        if safe_cut is None or safe_cut <= 0:
            return title[:hard_limit].rstrip("，,。；;：:、 ")
        return title[:safe_cut].rstrip("，,。；;：:、 ")

    def _is_placeholder_title(self, value: str) -> bool:
        title = str(value or "").strip()
        if not title:
            return True
        normalized = title.lower()
        return bool(
            re.fullmatch(r"(大)?章节\s*\d+", title)
            or re.fullmatch(r"主题\s*\d+", title)
            or re.fullmatch(r"第?\s*\d+\s*(章|节|部分)", title)
            or re.fullmatch(r"(part|section|chapter)\s*[-:：]?\s*\d+", normalized)
        )

    def _derive_title_from_text(self, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        text = re.sub(r"^(这一部分|本部分|这里|该部分|这一章|本章|本节|这一节)(主要)?(讲|介绍|讨论|说明|分析|围绕)?", "", text)
        text = re.split(r"[。；!！?？\n]", text, maxsplit=1)[0].strip(" ：:-")
        text = re.sub(r"\s+", "", text)
        if not text:
            return ""
        if len(text) > 24:
            text = text[:24].rstrip("，,、；;：:")
        return text

    def _export_result(
        self,
        task_dir: Path,
        title: str,
        transcript: str,
        segments: list[dict[str, object]],
        summary: dict[str, object],
    ) -> TaskResult:
        snapshot_result = self._export_transcript_snapshot(task_dir, title, transcript, segments)
        transcript_path = Path(snapshot_result.artifacts["transcript_path"])
        summary_path = Path(snapshot_result.artifacts["summary_path"])
        knowledge_note_path = task_dir / "knowledge_note.md"
        knowledge_note_markdown = str(summary.get("knowledgeNoteMarkdown") or "").strip()
        summary_path.write_text(
            json.dumps({"title": title, "summary": summary, "segments": segments}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        knowledge_note_path.write_text(knowledge_note_markdown, encoding="utf-8")
        logger.info(
            "result exported transcript_path=%s summary_path=%s knowledge_note_path=%s",
            transcript_path,
            summary_path,
            knowledge_note_path,
        )
        return self._build_task_result(
            transcript,
            summary,
            segments,
            artifacts={
                "transcript_path": str(transcript_path),
                "summary_path": str(summary_path),
                "knowledge_note_path": str(knowledge_note_path),
            },
        )

    def _export_transcript_snapshot(
        self,
        task_dir: Path,
        title: str,
        transcript: str,
        segments: list[dict[str, object]],
    ) -> TaskResult:
        transcript_path = task_dir / "transcript.txt"
        summary_path = task_dir / "summary.json"
        transcript_path.write_text(transcript, encoding="utf-8")
        summary_path.write_text(
            json.dumps({"title": title, "summary": {}, "segments": segments}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return self._build_task_result(
            transcript,
            {},
            segments,
            artifacts={
                "transcript_path": str(transcript_path),
                "summary_path": str(summary_path),
            },
        )

    def _build_task_result(
        self,
        transcript: str,
        summary: dict[str, object],
        segments: list[dict[str, object]] | None = None,
        artifacts: dict[str, str] | None = None,
    ) -> TaskResult:
        knowledge_note_markdown = str(summary.get("knowledgeNoteMarkdown") or "").strip()
        return TaskResult(
            overview=str(summary.get("overview") or ""),
            knowledge_note_markdown=knowledge_note_markdown,
            transcript_text=transcript,
            segments=segments or [],
            segment_summaries=[str(item["summary"]) for item in summary.get("chapters", [])],
            key_points=[str(item) for item in summary.get("bulletPoints", [])],
            timeline=[
                {
                    "title": str(item["title"]),
                    "start": item["start"],
                    "summary": str(item["summary"]),
                }
                for item in summary.get("chapters", [])
            ],
            chapter_groups=[
                {
                    "title": str(group.get("title") or ""),
                    "start": group.get("start"),
                    "summary": str(group.get("summary") or ""),
                    "children": [
                        {
                            "title": str(item.get("title") or ""),
                            "start": item.get("start"),
                            "summary": str(item.get("summary") or ""),
                        }
                        for item in (group.get("children") or [])
                        if isinstance(item, dict)
                    ],
                }
                for group in summary.get("chapterGroups", [])
                if isinstance(group, dict)
            ],
            artifacts=artifacts or {},
            llm_prompt_tokens=_safe_int(summary.get("llm_prompt_tokens")),
            llm_completion_tokens=_safe_int(summary.get("llm_completion_tokens")),
            llm_total_tokens=_safe_int(summary.get("llm_total_tokens")),
        )
