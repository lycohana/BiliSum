import base64
import binascii
import importlib.resources
import io
import json
import logging
import os
from pathlib import Path
import struct
import wave
import zlib

import httpx
from fastapi import HTTPException

from video_sum_infra.config import ServiceSettings
from video_sum_infra.llm import (
    ANTHROPIC_API_VERSION,
    apply_openai_reasoning_settings,
    anthropic_messages_url,
    build_anthropic_messages_payload,
    extract_llm_message_content,
    is_anthropic_llm,
    normalize_openai_compatible_model_name,
    normalize_llm_provider,
    openai_chat_completions_url,
)
from video_sum_infra.runtime import service_log_path

from video_sum_service.context import COVER_CACHE_DIR, settings_manager
from video_sum_service.settings_manager import SettingsUpdatePayload, is_blank_or_masked_secret

logger = logging.getLogger(__name__)

MAX_LOG_CHARS = 20_000
MAX_LOG_LINE_CHARS = 1_000

VISUAL_TEST_FONT: dict[str, tuple[str, ...]] = {
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
}


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + chunk_type
        + data
        + struct.pack(">I", binascii.crc32(chunk_type + data) & 0xFFFFFFFF)
    )


def build_visual_test_image_data_url() -> str:
    width, height = 320, 160
    white = (255, 255, 255)
    ink = (22, 24, 28)
    pink = (251, 114, 153)
    pixels = bytearray(white * width * height)

    def set_pixel(x: int, y: int, color: tuple[int, int, int]) -> None:
        if 0 <= x < width and 0 <= y < height:
            offset = (y * width + x) * 3
            pixels[offset : offset + 3] = bytes(color)

    center_x, center_y, radius = 262, 80, 38
    for y in range(center_y - radius, center_y + radius + 1):
        for x in range(center_x - radius, center_x + radius + 1):
            if (x - center_x) ** 2 + (y - center_y) ** 2 <= radius**2:
                set_pixel(x, y, pink)

    scale = 11
    cursor_x = 26
    top = 48
    for char in "BILISUM":
        glyph = VISUAL_TEST_FONT[char]
        for row_index, row in enumerate(glyph):
            for col_index, value in enumerate(row):
                if value != "1":
                    continue
                for dy in range(scale):
                    for dx in range(scale):
                        set_pixel(cursor_x + col_index * scale + dx, top + row_index * scale + dy, ink)
        cursor_x += 6 * scale

    raw = bytearray()
    stride = width * 3
    for row in range(height):
        raw.append(0)
        start = row * stride
        raw.extend(pixels[start : start + stride])

    png = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), level=9))
        + _png_chunk(b"IEND", b"")
    )
    return f"data:image/png;base64,{base64.b64encode(png).decode('ascii')}"


def trim_log_text(content: str, *, max_chars: int = MAX_LOG_CHARS, max_line_chars: int = MAX_LOG_LINE_CHARS) -> str:
    lines = content.splitlines()
    trimmed_lines = [
        f"{line[:max_line_chars]}... [line truncated]"
        if len(line) > max_line_chars
        else line
        for line in lines
    ]
    trimmed = "\n".join(trimmed_lines)
    if len(trimmed) <= max_chars:
        return trimmed
    return f"... [log truncated, showing last {max_chars} chars]\n{trimmed[-max_chars:]}"


def read_log_tail(max_lines: int = 200) -> str:
    log_path = service_log_path()
    if not log_path.exists():
        return ""
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return trim_log_text("\n".join(lines[-max(1, max_lines) :]))


def cache_cover_image(source_url: str, canonical_id: str, referer_url: str | None = None) -> str:
    if not source_url:
        return ""
    normalized_source = source_url.replace("http://", "https://")

    # 验证 URL 安全性 - 防止 SSRF 攻击
    from urllib.parse import urlparse
    parsed = urlparse(normalized_source)

    # 视频封面 URL 白名单
    ALLOWED_COVER_DOMAINS = [
        "bilibili.com", "hdslb.com", "bilivideo.com", "biliimg.com",  # B站
        "ytimg.com", "ggpht.com", "googleusercontent.com",  # YouTube
        "twimg.com",  # Twitter
    ]

    if parsed.scheme != "https":
        logger.warning("cover image URL must use HTTPS: %s", normalized_source)
        return normalized_source

    if not any(parsed.netloc.endswith(domain) or parsed.netloc == domain for domain in ALLOWED_COVER_DOMAINS):
        logger.warning("cover image URL domain not in whitelist: %s", parsed.netloc)
        return normalized_source

    COVER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = COVER_CACHE_DIR / f"{canonical_id}.jpg"
    if target.exists():
        return f"/media/covers/{target.name}"
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36"
            ),
            "Referer": referer_url or "https://www.bilibili.com/",
        }
        with httpx.Client(timeout=30, follow_redirects=True, headers=headers) as client:
            response = client.get(normalized_source)
            response.raise_for_status()
        target.write_bytes(response.content)
        return f"/media/covers/{target.name}"
    except Exception:
        return normalized_source


def extract_http_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None

    if isinstance(payload, dict):
        for key in ("detail", "message", "error", "msg"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    text = (response.text or "").strip()
    return text or f"HTTP {response.status_code}"


def build_test_wav_bytes(duration_ms: int = 250, sample_rate: int = 16000) -> bytes:
    frame_count = max(1, int(sample_rate * duration_ms / 1000))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"\x00\x00" * frame_count)
    return buffer.getvalue()


def load_builtin_asr_test_audio() -> tuple[str, bytes, str]:
    audio = importlib.resources.files("video_sum_service.assets").joinpath("asr_test_zh.wav")
    return "bilisum-asr-test-zh.wav", audio.read_bytes(), "audio/wav"


def load_asr_test_audio() -> tuple[str, bytes, str]:
    audio_path = os.environ.get("VIDEO_SUM_ASR_TEST_AUDIO_FILE", "").strip()
    if audio_path:
        path = Path(audio_path).expanduser()
        if not path.is_file():
            raise HTTPException(status_code=400, detail=f"ASR 测试音频不存在：{path}")
        suffix = path.suffix.lower()
        content_type = {
            ".flac": "audio/flac",
            ".m4a": "audio/mp4",
            ".mp3": "audio/mpeg",
            ".ogg": "audio/ogg",
            ".wav": "audio/wav",
            ".webm": "audio/webm",
        }.get(suffix, "application/octet-stream")
        return path.name, path.read_bytes(), content_type

    try:
        return load_builtin_asr_test_audio()
    except (FileNotFoundError, ModuleNotFoundError):
        return "bilisum-asr-test.wav", build_test_wav_bytes(), "audio/wav"


def _extract_json_object(text: str) -> dict[str, object] | None:
    """Try to extract a JSON object from text that may have markdown fences or extra content."""
    import re
    t = text.strip()
    # Strip markdown code fences
    for fence in ("```json", "```"):
        if t.startswith(fence):
            t = t[len(fence):]
            if t.endswith("```"):
                t = t[:-3]
            t = t.strip()
            break
    # Try direct parse first
    try:
        result = json.loads(t)
        if isinstance(result, dict):
            return result
    except (json.JSONDecodeError, ValueError):
        pass
    # Try to find a JSON object with regex
    m = re.search(r'\{[^{}]*\{[^{}]*\}[^{}]*\}|\{[^{}]*\}', t)
    if m:
        try:
            result = json.loads(m.group())
            if isinstance(result, dict):
                return result
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def build_effective_llm_test_settings(payload: SettingsUpdatePayload | None = None) -> ServiceSettings:
    current_settings = settings_manager.current
    current_dump = current_settings.model_dump(mode="json")
    if payload is not None and payload.llm_test_scope == "knowledge":
        current_dump = {
            **current_dump,
            "llm_enabled": current_settings.knowledge_llm_enabled,
            "llm_provider": current_settings.knowledge_llm_provider,
            "llm_base_url": current_settings.knowledge_llm_base_url,
            "llm_api_key": current_settings.knowledge_llm_api_key,
            "llm_model": current_settings.knowledge_llm_model,
        }
    if payload is not None and payload.llm_test_scope == "visual":
        provider = current_settings.visual_vlm_provider or current_settings.llm_provider
        base_url = current_settings.visual_evidence_base_url or current_settings.llm_base_url
        current_dump = {
            **current_dump,
            "llm_enabled": current_settings.visual_multimodal_enabled,
            "llm_provider": provider,
            "llm_base_url": base_url,
            "llm_api_key": current_settings.visual_evidence_api_key or current_settings.llm_api_key,
            "llm_model": current_settings.visual_evidence_model or current_settings.llm_model,
        }
    updates = payload.model_dump(exclude_none=True, exclude={"llm_test_scope"}) if payload is not None else {}
    if "llm_api_key" in updates and is_blank_or_masked_secret(updates["llm_api_key"]) and current_dump.get("llm_api_key"):
        updates.pop("llm_api_key")
    return ServiceSettings.model_validate({**current_dump, **updates})


def probe_llm_connection(payload: SettingsUpdatePayload | None = None) -> dict[str, object]:
    effective_settings = build_effective_llm_test_settings(payload)
    test_scope = payload.llm_test_scope if payload and payload.llm_test_scope else "main"

    base_url = str(effective_settings.llm_base_url or "").strip().rstrip("/")
    api_key = str(effective_settings.llm_api_key or "").strip()
    model = str(effective_settings.llm_model or "").strip()
    provider = normalize_llm_provider(effective_settings.llm_provider)
    use_anthropic = is_anthropic_llm(provider, base_url)
    request_model = model if use_anthropic else normalize_openai_compatible_model_name(model)

    if not base_url:
        raise HTTPException(status_code=400, detail="请先填写 API Base URL。")
    if not api_key:
        raise HTTPException(status_code=400, detail="请先填写 API Key。")
    if not model:
        raise HTTPException(status_code=400, detail="请先填写模型名称。")

    if test_scope == "visual":
        request_payload = {
            "model": request_model,
            "messages": [
                {
                    "role": "system",
                    "content": "你是一个视觉 JSON 测试助手。你必须观察图片，只返回合法 JSON。",
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "请识别图片中的大字文本和主要形状。只返回 JSON，不要解释。"
                                '格式：{"ok":true,"text":"BILISUM","shape":"pink circle"}'
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": build_visual_test_image_data_url(),
                                "detail": "low",
                            },
                        },
                    ],
                },
            ],
            "temperature": 0,
            "max_tokens": 256,
        }
    else:
        request_payload = {
            "model": request_model,
            "messages": [
                {
                    "role": "system",
                    "content": "你只输出一行合法 JSON，不要 markdown 代码块，不要解释文字。",
                },
                {
                    "role": "user",
                    "content": '输出 {"ok":true,"message":"test"}',
                },
            ],
            "temperature": 0,
            "max_tokens": 512,
        }
    # For visual tests with images, only the real Anthropic API supports image
    # blocks via /messages. Third-party Anthropic-compatible endpoints
    # (SiliconFlow etc.) need OpenAI format for image requests.
    fallback_to_openai_for_images = (
        use_anthropic and test_scope == "visual" and "api.anthropic.com" not in base_url.lower()
    )
    if fallback_to_openai_for_images:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        request_url = openai_chat_completions_url(base_url)
        request_json = apply_openai_reasoning_settings(
            request_payload,
            thinking_type=effective_settings.llm_thinking_type,
            reasoning_effort=effective_settings.llm_reasoning_effort,
            provider=effective_settings.llm_provider,
            model=model,
        )
        request_json["model"] = normalize_openai_compatible_model_name(model)
    elif use_anthropic:
        headers = {
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
            "Content-Type": "application/json",
        }
        request_url = anthropic_messages_url(base_url)
        request_json = build_anthropic_messages_payload(request_payload)
    else:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        request_url = openai_chat_completions_url(base_url)
        request_json = apply_openai_reasoning_settings(
            request_payload,
            thinking_type=effective_settings.llm_thinking_type,
            reasoning_effort=effective_settings.llm_reasoning_effort,
            provider=effective_settings.llm_provider,
            model=model,
        )

    probe_timeout = 120 if test_scope == "visual" else 30
    try:
        with httpx.Client(timeout=probe_timeout, follow_redirects=True) as client:
            response = client.post(
                request_url,
                headers=headers,
                json=request_json,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"LLM 连接失败：{exc}") from exc

    if response.status_code >= 400:
        detail = extract_http_error_detail(response)
        raise HTTPException(
            status_code=response.status_code,
            detail=f"LLM 测试失败：{detail}",
        )

    try:
        body = response.json()
    except ValueError:
        body = None

    preview = extract_llm_message_content(body)
    if not preview:
        raise HTTPException(status_code=502, detail="LLM 测试失败：接口已连通，但没有返回可读取的消息内容。")

    try:
        parsed_json = json.loads(preview)
    except json.JSONDecodeError:
        parsed_json = _extract_json_object(preview)
    if parsed_json is None:
        raise HTTPException(
            status_code=502,
            detail=f"LLM 测试失败：未返回合法 JSON。响应预览：{preview[:200]}",
        )

    if test_scope == "visual":
        parsed_text = json.dumps(parsed_json, ensure_ascii=False).lower()
        if "bilisum" not in parsed_text and "bili" not in parsed_text:
            raise HTTPException(
                status_code=502,
                detail="视觉模型测试失败：接口返回了 JSON，但未识别出测试图片中的 BiliSum 文本。",
            )

    preview = preview[:200]
    return {
        "ok": True,
        "message": (
            f"视觉模型图片识别测试成功：{model}"
            if test_scope == "visual"
            else f"LLM 连接与 JSON 输出测试成功：{model}"
        ),
        "model": model,
        "baseUrl": base_url,
        "responsePreview": preview,
        "jsonOutputAvailable": True,
        "visualImageRecognitionAvailable": test_scope == "visual",
        "jsonPreview": json.dumps(parsed_json, ensure_ascii=False)[:200],
    }


def probe_asr_connection(payload: SettingsUpdatePayload | None = None) -> dict[str, object]:
    current_settings = settings_manager.current
    updates = payload.model_dump(exclude_none=True) if payload is not None else {}
    if (
        "siliconflow_asr_api_key" in updates
        and is_blank_or_masked_secret(updates["siliconflow_asr_api_key"])
        and current_settings.siliconflow_asr_api_key
    ):
        updates.pop("siliconflow_asr_api_key")
    if (
        "multimodal_asr_api_key" in updates
        and is_blank_or_masked_secret(updates["multimodal_asr_api_key"])
        and current_settings.multimodal_asr_api_key
    ):
        updates.pop("multimodal_asr_api_key")
    effective_settings = ServiceSettings.model_validate(
        {**current_settings.model_dump(mode="json"), **updates}
    )

    provider = str(effective_settings.transcription_provider or "").strip().lower()

    if provider == "multimodal":
        return _probe_multimodal_asr(effective_settings)

    base_url = str(effective_settings.siliconflow_asr_base_url or "").strip().rstrip("/")
    api_key = str(effective_settings.siliconflow_asr_api_key or "").strip()
    model = str(effective_settings.siliconflow_asr_model or "").strip()

    if not base_url:
        raise HTTPException(status_code=400, detail="请先填写 SiliconFlow Base URL。")
    if not api_key:
        raise HTTPException(status_code=400, detail="请先填写 SiliconFlow API Key。")
    if not model:
        raise HTTPException(status_code=400, detail="请先填写 ASR 模型名称。")

    headers = {"Authorization": f"Bearer {api_key}"}
    request_url = f"{base_url}/audio/transcriptions"
    audio_name, audio_bytes, audio_content_type = load_asr_test_audio()

    try:
        timeout = httpx.Timeout(connect=20.0, read=90.0, write=90.0, pool=20.0)
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                request_url,
                headers=headers,
                data={"model": model},
                files={"file": (audio_name, audio_bytes, audio_content_type)},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"ASR 连接失败：{exc}") from exc

    if response.status_code in {401, 403}:
        detail = extract_http_error_detail(response)
        raise HTTPException(status_code=response.status_code, detail=f"ASR 测试失败：认证失败，{detail}")
    if response.status_code >= 400:
        detail = extract_http_error_detail(response)
        raise HTTPException(status_code=response.status_code, detail=f"ASR 测试失败：{detail}")

    try:
        body = response.json()
    except ValueError:
        body = None

    transcript = str(body.get("text") or "").strip() if isinstance(body, dict) else ""
    return {
        "ok": True,
        "message": (
            f"ASR 连接测试成功：{model}"
            if transcript
            else f"ASR 连接测试成功：{model}（接口已响应，但测试音频未返回文本）"
        ),
        "model": model,
        "baseUrl": base_url,
        "responsePreview": transcript[:120],
    }


def _probe_multimodal_asr(settings: ServiceSettings) -> dict[str, object]:
    import base64

    base_url = str(settings.multimodal_asr_base_url or "").strip().rstrip("/")
    api_key = str(settings.multimodal_asr_api_key or "").strip()
    model = str(settings.multimodal_asr_model or "").strip()

    if not base_url:
        raise HTTPException(status_code=400, detail="请先填写多模态 ASR Base URL。")
    if not model:
        raise HTTPException(status_code=400, detail="请先填写多模态 ASR 模型名称。")

    request_url = f"{base_url}/chat/completions"
    audio_bytes = build_test_wav_bytes()
    audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": f"data:audio/wav;base64,{audio_b64}",
                        },
                    },
                    {
                        "type": "text",
                        "text": "请转录这段音频的全部文字内容，保持原文，不要修改",
                    },
                ],
            }
        ],
        "max_completion_tokens": 1024,
    }

    try:
        timeout = httpx.Timeout(connect=20.0, read=90.0, write=90.0, pool=20.0)
        with httpx.Client(timeout=timeout) as client:
            response = client.post(request_url, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"多模态 ASR 连接失败：{exc}") from exc

    if response.status_code in {401, 403}:
        detail = extract_http_error_detail(response)
        raise HTTPException(status_code=response.status_code, detail=f"多模态 ASR 测试失败：认证失败，{detail}")
    if response.status_code >= 400:
        detail = extract_http_error_detail(response)
        raise HTTPException(status_code=response.status_code, detail=f"多模态 ASR 测试失败：{detail}")

    try:
        body = response.json()
    except ValueError:
        body = None

    transcript = ""
    if isinstance(body, dict):
        choices = body.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            transcript = str(message.get("content") or "").strip()
            if not transcript:
                transcript = str(message.get("reasoning_content") or "").strip()

    return {
        "ok": True,
        "message": (
            f"多模态 ASR 连接测试成功：{model}"
            if transcript
            else f"多模态 ASR 连接测试成功：{model}（接口已响应，但测试音频未返回文本）"
        ),
        "model": model,
        "baseUrl": base_url,
        "responsePreview": transcript[:120],
    }


def fetch_bilibili_subtitle(
    aid: int | str,
    cid: int | str,
    cookie: str | None = None,
    bvid: str | None = None,
) -> dict[str, object] | None:
    """
    从 Bilibili API 获取视频字幕（包括 AI 字幕）。

    Args:
        aid: Bilibili 视频 AV 号
        cid: Bilibili 视频分 P 的 CID
        cookie: Bilibili Cookie（可选）
        bvid: Bilibili 视频 BV 号（优先使用）

    Returns:
        成功返回 {"transcript": "全文", "segments": [{"start": 0.0, "end": 1.5, "text": "..."}]}
        失败或无字幕返回 None
    """
    import logging

    logger = logging.getLogger(__name__)

    try:
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}
        if cookie:
            headers["Cookie"] = cookie

        # 如果没有 bvid，尝试从 aid 获取
        if not bvid:
            try:
                with httpx.Client(timeout=10.0) as client:
                    resp = client.get(
                        f"https://api.bilibili.com/x/web-interface/view?aid={aid}",
                        headers=headers
                    )
                if resp.status_code == 200:
                    data = resp.json()
                    bvid = data.get("data", {}).get("bvid")
            except Exception:
                pass

        subtitles = []

        # 方法1：WBI API（获取 UP 主上传的字幕）
        if bvid:
            try:
                wbi_url = f"https://api.bilibili.com/x/player/wbi/v2?bvid={bvid}&cid={cid}"
                with httpx.Client(timeout=15.0, follow_redirects=True) as client:
                    wbi_resp = client.get(wbi_url, headers=headers)

                if wbi_resp.status_code == 200:
                    wbi_data = wbi_resp.json()
                    if wbi_data.get("code") == 0:
                        subtitles = wbi_data.get("data", {}).get("subtitle", {}).get("subtitles", [])
                        if subtitles:
                            logger.info("bilibili subtitle fetched via WBI API (UP主字幕)")
            except Exception as e:
                logger.debug("WBI API failed: %s", e)

        # 方法2：弹幕 API（获取 AI 字幕）
        if not subtitles:
            try:
                dm_url = f"https://api.bilibili.com/x/v2/dm/view?type=1&oid={cid}&pid={aid}"
                with httpx.Client(timeout=15.0, follow_redirects=True) as client:
                    dm_resp = client.get(dm_url, headers=headers)

                if dm_resp.status_code == 200:
                    dm_data = dm_resp.json()
                    if dm_data.get("code") == 0:
                        subtitles = dm_data.get("data", {}).get("subtitle", {}).get("subtitles", [])
                        if subtitles:
                            logger.info("bilibili subtitle fetched via dm/view API (AI字幕)")
            except Exception as e:
                logger.debug("dm/view API failed: %s", e)

        if not subtitles:
            logger.info("bilibili video has no subtitles aid=%s cid=%s", aid, cid)
            return None

        # 智能选择字幕：优先非 AI、非摘要的中文字幕
        selected_subtitle = None
        logger.debug("Available subtitles: %s", [(s.get("lan_doc"), s.get("lan")) for s in subtitles])

        # 优先级1：非 AI、非摘要的中文字幕（UP主上传）
        for sub in subtitles:
            lan_doc = sub.get("lan_doc", "")
            lan = sub.get("lan", "")
            if "摘要" not in lan_doc and "ai-" not in lan and ("中文" in lan_doc or "zh-" in lan or lan == "zh"):
                selected_subtitle = sub
                logger.info("Selected subtitle: %s (priority: non-AI Chinese)", lan_doc)
                break

        # 优先级2：AI 中文字幕
        if not selected_subtitle:
            for sub in subtitles:
                lan = sub.get("lan", "")
                lan_doc = sub.get("lan_doc", "")
                if lan == "ai-zh" or ("中文" in lan_doc and "ai-" in lan):
                    selected_subtitle = sub
                    logger.info("Selected subtitle: %s (priority: AI Chinese)", lan_doc)
                    break

        # 优先级3：任意中文字幕
        if not selected_subtitle:
            for sub in subtitles:
                lan_doc = sub.get("lan_doc", "")
                lan = sub.get("lan", "")
                if "中文" in lan_doc or "zh" in lan:
                    selected_subtitle = sub
                    logger.info("Selected subtitle: %s (priority: any Chinese)", lan_doc)
                    break

        # 优先级4：第一个可用字幕
        if not selected_subtitle:
            selected_subtitle = subtitles[0]
            logger.info("Selected subtitle: %s (fallback: first available)", selected_subtitle.get("lan_doc"))

        subtitle_url = selected_subtitle.get("subtitle_url")
        if not subtitle_url:
            logger.warning("bilibili subtitle entry has no url")
            return None

        # 确保 URL 包含协议
        if subtitle_url.startswith("//"):
            subtitle_url = "https:" + subtitle_url
        elif subtitle_url.startswith("http://"):
            subtitle_url = subtitle_url.replace("http://", "https://")

        # 验证 URL 安全性 - 防止 SSRF 攻击
        from urllib.parse import urlparse
        parsed = urlparse(subtitle_url)
        ALLOWED_DOMAINS = ["bilibili.com", "hdslb.com", "bilivideo.com"]

        if parsed.scheme != "https":
            logger.warning("bilibili subtitle URL must use HTTPS: %s", subtitle_url)
            return None

        if not any(parsed.netloc.endswith(domain) or parsed.netloc == domain for domain in ALLOWED_DOMAINS):
            logger.warning("bilibili subtitle URL domain not in whitelist: %s", parsed.netloc)
            return None

        # 下载字幕 JSON
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            sub_resp = client.get(subtitle_url, headers=headers)

        if sub_resp.status_code != 200:
            logger.warning("bilibili subtitle download failed status=%s", sub_resp.status_code)
            return None

        subtitle_data = sub_resp.json()

        # 支持多种字幕 JSON 格式
        body_list = (
            subtitle_data.get("body") if isinstance(subtitle_data.get("body"), list) else None
        ) or (
            subtitle_data.get("data", {}).get("body") if isinstance(subtitle_data.get("data", {}).get("body"), list) else None
        ) or (
            subtitle_data.get("result", {}).get("body") if isinstance(subtitle_data.get("result", {}).get("body"), list) else None
        ) or (
            subtitle_data.get("subtitle", {}).get("body") if isinstance(subtitle_data.get("subtitle", {}).get("body"), list) else None
        ) or []

        if not body_list:
            logger.warning("bilibili subtitle body is empty or invalid, tried all known formats")
            return None

        # 转换为 transcript 格式
        segments = []
        transcript_parts = []
        for item in body_list:
            start = float(item.get("from", 0.0))
            end = float(item.get("to", 0.0))
            text = str(item.get("content", "")).strip()
            if text:
                segments.append({"start": start, "end": end, "text": text})
                transcript_parts.append(text)

        if not segments:
            logger.warning("bilibili subtitle has no valid segments")
            return None

        transcript = "\n".join(transcript_parts)
        logger.info("bilibili subtitle fetched successfully segments=%d chars=%d", len(segments), len(transcript))
        return {"transcript": transcript, "segments": segments}

    except Exception as exc:
        logger.warning("bilibili subtitle fetch failed: %s", exc, exc_info=True)
        return None
