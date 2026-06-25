"""Twelve Labs Pegasus 视频理解集成。

Pegasus 直接“看”视频本身（画面 + 语音），生成一段视频理解摘要。它与现有
基于转写文本的 LLM 摘要互补：当视频包含演示、图表、操作步骤等画面信息，而
字幕/转写无法覆盖时，Pegasus 摘要可以补足这部分内容，再随知识笔记一起写入
知识库。

本模块刻意只用 ``httpx`` 直连 Twelve Labs REST API（``/v1.3/analyze``），与仓库
内其它外部服务（SiliconFlow embeddings、Anthropic、OpenAI 兼容接口）的调用方式
保持一致，不引入额外 SDK 依赖。

接口文档：https://docs.twelvelabs.io/
免费 API Key：https://twelvelabs.io/ （有较慷慨的免费额度）
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path

import httpx

from video_sum_core.errors import PegasusAuthenticationError, PegasusConfigurationError

logger = logging.getLogger("video_sum_core.twelvelabs")

DEFAULT_TWELVELABS_BASE_URL = "https://api.twelvelabs.io/v1.3"
DEFAULT_TWELVELABS_MODEL = "pegasus1.5"
DEFAULT_TWELVELABS_PROMPT = (
    "请用简体中文总结这段视频。重点描述画面中出现、但仅凭语音/字幕无法获知的信息："
    "演示步骤、界面操作、图表数据、代码、实验结果和关键画面。"
    "输出 3 到 6 条要点，忠实于画面，不要编造。"
)

# Pegasus 直传 base64 视频上限为 30MB；超过则需走 asset/url 来源。
MAX_BASE64_VIDEO_BYTES = 30 * 1024 * 1024


def _analyze_url(base_url: str) -> str:
    normalized = str(base_url or "").strip().rstrip("/") or DEFAULT_TWELVELABS_BASE_URL
    if normalized.endswith("/analyze"):
        return normalized
    return f"{normalized}/analyze"


def video_context_from_url(url: str) -> dict[str, str]:
    """根据可直连的原始媒体地址构造 Pegasus video 来源。"""
    return {"type": "url", "url": str(url or "").strip()}


def video_context_from_file(path: str | Path) -> dict[str, str] | None:
    """把本地视频文件编码为 Pegasus base64 来源。

    超过 30MB 的文件返回 ``None``（调用方应跳过 Pegasus，而不是报错），
    避免把大文件一次性塞进请求体。
    """
    file_path = Path(path)
    try:
        size = file_path.stat().st_size
    except OSError:
        return None
    if size <= 0 or size > MAX_BASE64_VIDEO_BYTES:
        return None
    encoded = base64.b64encode(file_path.read_bytes()).decode("ascii")
    return {"type": "base64_string", "base64_string": encoded}


def analyze_video_with_pegasus(
    *,
    api_key: str,
    video: dict[str, str],
    prompt: str = DEFAULT_TWELVELABS_PROMPT,
    model_name: str = DEFAULT_TWELVELABS_MODEL,
    base_url: str = DEFAULT_TWELVELABS_BASE_URL,
    max_tokens: int = 2048,
    temperature: float = 0.2,
    timeout_seconds: float = 300.0,
) -> str:
    """调用 Pegasus 同步分析接口，返回生成的视频理解摘要文本。

    Args:
        api_key: Twelve Labs API Key。
        video: Pegasus video 来源对象，必须恰好包含一种来源，例如
            ``{"type": "url", "url": ...}``、``{"type": "asset_id", "asset_id": ...}``
            或 ``{"type": "base64_string", "base64_string": ...}``。可用
            :func:`video_context_from_url` / :func:`video_context_from_file` 构造。
        prompt: 引导生成的提示词（≤ 2000 tokens）。
        model_name: Pegasus 模型名（``pegasus1.5`` / ``pegasus1.2``）。
        base_url: API 基础地址，默认指向 v1.3。
        max_tokens: 最大输出 token 数。
        temperature: 采样温度（0-1）。
        timeout_seconds: 单次请求超时时间。

    Returns:
        Pegasus 生成的摘要文本（已 strip）。

    Raises:
        PegasusConfigurationError: API Key 或 video 来源缺失/非法。
        PegasusAuthenticationError: Twelve Labs 拒绝了 API Key（401/403）。
        VideoSumError: 其它上游错误（通过基类传播）。
    """
    if not str(api_key or "").strip():
        raise PegasusConfigurationError("Twelve Labs API Key 未配置。")
    if not isinstance(video, dict) or not video.get("type"):
        raise PegasusConfigurationError("Pegasus video 来源缺失或非法。")

    payload: dict[str, object] = {
        "model_name": model_name or DEFAULT_TWELVELABS_MODEL,
        "video": video,
        "prompt": prompt or DEFAULT_TWELVELABS_PROMPT,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    logger.info(
        "pegasus analyze request model=%s source=%s prompt_chars=%d",
        payload["model_name"],
        video.get("type"),
        len(str(payload["prompt"])),
    )
    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            response = client.post(
                _analyze_url(base_url),
                headers={"x-api-key": str(api_key).strip(), "Content-Type": "application/json"},
                json=payload,
            )
    except httpx.HTTPError as exc:  # 网络层错误
        raise PegasusConfigurationError(f"Pegasus 请求失败：{exc}") from exc

    if response.status_code in (401, 403):
        raise PegasusAuthenticationError("Twelve Labs 拒绝了 API Key，请检查密钥是否有效。")
    if response.status_code >= 400:
        detail = ""
        try:
            detail = str(response.json().get("message") or "")
        except (ValueError, AttributeError):
            detail = response.text[:200]
        raise PegasusConfigurationError(f"Pegasus 返回错误（{response.status_code}）：{detail}")

    body = response.json()
    text = _extract_pegasus_text(body)
    if not text:
        raise PegasusConfigurationError("Pegasus 未返回任何摘要文本。")
    logger.info("pegasus analyze success chars=%d", len(text))
    return text


def _extract_pegasus_text(body: object) -> str:
    """从 ``/analyze`` 响应中提取生成文本。

    v1.3 同步响应形如 ``{"id": ..., "data": "<text>", ...}``；历史/SDK 形态可能把
    文本包在 ``data.data`` 里，这里都做兼容。
    """
    if not isinstance(body, dict):
        return ""
    data = body.get("data")
    if isinstance(data, str):
        return data.strip()
    if isinstance(data, dict):
        nested = data.get("data") or data.get("text")
        if isinstance(nested, str):
            return nested.strip()
    text = body.get("text")
    if isinstance(text, str):
        return text.strip()
    return ""
