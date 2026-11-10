"""Single entry point for non-streaming chat calls to a model provider.

``ModelClient.chat(ModelRequest) -> ModelResponse`` owns everything between
"here is an OpenAI-shaped chat payload" and "here is the reply text plus a
normalized usage record": OpenAI/Anthropic wire formats, reasoning controls,
image content blocks, timeouts, transport retries, usage normalization and
tolerant body parsing (SSE/NDJSON gateways).

Callers only assemble requests and interpret the reply. Error contract:

* transport failures re-raise the original ``httpx`` exception once the
  request's ``transport_retries`` are used up (only retryable transport errors
  are retried, see :func:`is_retryable_transport_error`); HTTP 429/5xx are
  retried within the same budget with exponential backoff;
* HTTP status >= 400 raises ``httpx.HTTPStatusError`` (``exc.response`` is
  available; :func:`extract_response_error_detail` renders it);
* an unreadable body raises :class:`ModelResponseError` when
  ``lenient_body`` is true, or the ``ValueError`` from ``response.json()``
  otherwise.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Literal, Protocol

import httpx

from video_sum_infra.llm import (
    ANTHROPIC_API_VERSION,
    anthropic_messages_url,
    apply_openai_reasoning_settings,
    build_anthropic_messages_payload,
    extract_llm_message_content,
    normalize_openai_compatible_model_name,
    openai_chat_completions_url,
)

logger = logging.getLogger("video_sum_infra.model_client")

WireFormat = Literal["openai", "anthropic"]


class ModelResponseError(Exception):
    """The provider answered 2xx but the body could not be read."""


@dataclass(frozen=True, slots=True)
class ReasoningOptions:
    """Reasoning controls applied to OpenAI-format requests.

    See ``apply_openai_reasoning_settings``.
    """

    thinking_type: str | None
    reasoning_effort: str | None
    provider: str | None = None


@dataclass(slots=True)
class ModelRequest:
    """One chat call. ``payload`` is always OpenAI chat-completions shaped.

    ``payload`` holds ``model``, ``messages`` (string or content-block lists,
    including ``image_url`` blocks) and any extra fields (``temperature``,
    ``max_tokens``, ``enable_thinking`` ...). For ``wire_format="anthropic"`` it
    is converted with ``build_anthropic_messages_payload``.
    """

    usage_stage: str
    base_url: str
    api_key: str
    payload: dict[str, object]
    wire_format: WireFormat = "openai"
    provider: str = ""
    reasoning: ReasoningOptions | None = None
    timeout: float | httpx.Timeout = 180
    transport_retries: int = 0
    lenient_body: bool = True
    endpoint_url: str | None = None
    usage_model: str | None = None
    images: tuple[str, ...] = ()
    """Local files behind the inline ``image_url`` blocks of ``payload``, in order.
    Not sent on the wire; the Agent gets these instead of the inline data."""


@dataclass(slots=True)
class ModelResponse:
    content: str
    body: object = None
    usage: dict[str, object] = field(default_factory=dict)
    usage_record: dict[str, object] | None = None
    model: str = ""
    status_code: int = 200


@dataclass(frozen=True, slots=True)
class HttpRequestSpec:
    """The exact wire request a :class:`ModelRequest` turns into."""

    url: str
    headers: dict[str, str]
    json: dict[str, object]
    timeout: float | httpx.Timeout
    model: str


class ChatModelClient(Protocol):
    def chat(self, request: ModelRequest) -> ModelResponse: ...


def build_http_request(request: ModelRequest) -> HttpRequestSpec:
    """Pure: render a request to URL, headers and JSON body without sending it."""
    payload = dict(request.payload)
    if request.wire_format == "anthropic":
        payload["model"] = str(payload.get("model") or "").strip()
        body = build_anthropic_messages_payload(payload)
        headers = {
            "x-api-key": request.api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
            "Content-Type": "application/json",
        }
        url = request.endpoint_url or anthropic_messages_url(request.base_url)
        model = str(body.get("model") or "")
    else:
        if request.reasoning is not None:
            payload = apply_openai_reasoning_settings(
                payload,
                thinking_type=request.reasoning.thinking_type,
                reasoning_effort=request.reasoning.reasoning_effort,
                provider=request.reasoning.provider,
                model=str(payload.get("model") or ""),
                base_url=request.base_url,
            )
        payload["model"] = normalize_openai_compatible_model_name(str(payload.get("model") or ""))
        body = payload
        headers = {
            "Authorization": f"Bearer {request.api_key}",
            "Content-Type": "application/json",
        }
        url = request.endpoint_url or openai_chat_completions_url(request.base_url)
        model = str(payload["model"])
    return HttpRequestSpec(
        url=url, headers=headers, json=body, timeout=request.timeout, model=model
    )


def is_retryable_transport_error(error: Exception) -> bool:
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


def extract_response_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip()[:400]

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
    return json.dumps(payload, ensure_ascii=False)[:400]


def parse_response_body(response: httpx.Response) -> dict[str, object]:
    """Parse a chat response body: plain JSON, JSON + ``data: [DONE]``, SSE or NDJSON.

    Some OpenAI-compatible gateways always stream (``data: {...}`` events plus
    ``data: [DONE]``) even when the client did not ask for it, or append an SSE
    terminator to a normal JSON body. Chunks are combined into one
    chat-completion-shaped body; ``usage`` from any chunk is kept. Anything
    unreadable raises :class:`ModelResponseError`.
    """
    try:
        body = response.json()
    except ValueError:
        try:
            body = _parse_framed_body(response.text)
        except ModelResponseError:
            raise
        except ValueError as exc:
            raise ModelResponseError(f"LLM response is not valid JSON or SSE: {exc}") from exc
    if not isinstance(body, dict):
        raise ModelResponseError("LLM response must be a JSON object.")
    return body


def _parse_framed_body(text: str) -> object:
    raw = text.lstrip("﻿").strip()
    decoder = json.JSONDecoder()
    chunks: list[object] = []
    if raw.startswith("{"):
        body, end = decoder.raw_decode(raw)
        if raw[end:].strip() in {"", "data: [DONE]"}:
            return body
        # NDJSON gateways emit one chat-completion chunk per line. Reject
        # unrelated trailing objects instead of silently dropping data.
        try:
            chunks = [
                json.loads(line)
                for line in raw.splitlines()
                if line.strip() and line.strip() != "[DONE]"
            ]
        except ValueError as exc:
            raise ModelResponseError("LLM response contains unexpected data after JSON.") from exc
        if not all(
            isinstance(chunk, dict) and any(key in chunk for key in ("choices", "usage", "error"))
            for chunk in chunks
        ):
            raise ModelResponseError("LLM response contains unexpected data after JSON.")
        return _combine_chunks(chunks)
    # OpenAI chunks normally use one data line per event; joining data lines
    # also supports the multiline form defined by SSE.
    events = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n\n")
    for event in events:
        lines = [line[5:].lstrip() for line in event.splitlines() if line.startswith("data:")]
        if not lines:
            continue
        try:
            event_chunks = [json.loads("\n".join(lines))] if lines != ["[DONE]"] else []
        except ValueError:
            # Accept gateways that omit blank lines between chunks.
            event_chunks = [json.loads(line) for line in lines if line != "[DONE]"]
        chunks.extend(event_chunks)
    return _combine_chunks(chunks)


def _combine_chunks(chunks: list[object]) -> dict[str, object]:
    parts: list[str] = []
    usage: dict[str, object] = {}
    for chunk in chunks:
        if not isinstance(chunk, dict):
            raise ModelResponseError("LLM SSE chunk must be a JSON object.")
        if chunk.get("error"):
            raise ModelResponseError(f"LLM stream error: {chunk['error']}")
        if isinstance(chunk.get("usage"), dict):
            usage.update(chunk["usage"])
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                continue
            message = choice.get("delta") or choice.get("message") or {}
            content = message.get("content") if isinstance(message, dict) else None
            content = content or choice.get("text")
            if isinstance(content, str):
                parts.append(content)
    if not "".join(parts):
        raise ModelResponseError("LLM returned a stream with no readable message content.")
    return {
        "choices": [{"message": {"role": "assistant", "content": "".join(parts)}}],
        "usage": usage,
    }


def is_retryable_status(status_code: int) -> bool:
    """Rate limits and server errors are worth another attempt; other 4xx are not."""
    return status_code == 429 or status_code >= 500


def _safe_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def normalize_usage_record(
    usage_payload: object,
    *,
    stage: str,
    model: str | None,
    provider: str | None,
) -> dict[str, object] | None:
    """Normalize provider token usage (OpenAI, Anthropic, DeepSeek-style cache fields)."""
    usage = usage_payload if isinstance(usage_payload, dict) else {}
    raw_prompt_tokens = _safe_int(usage.get("prompt_tokens"))
    input_tokens = _safe_int(usage.get("input_tokens"))
    prompt_tokens = raw_prompt_tokens if raw_prompt_tokens is not None else input_tokens
    completion_tokens = _safe_int(usage.get("completion_tokens") or usage.get("output_tokens"))
    total_tokens = _safe_int(usage.get("total_tokens"))
    prompt_cache_hit_tokens = _safe_int(usage.get("prompt_cache_hit_tokens"))
    prompt_cache_miss_tokens = _safe_int(usage.get("prompt_cache_miss_tokens"))
    prompt_cache_creation_tokens = _safe_int(usage.get("cache_creation_input_tokens"))
    prompt_token_details = usage.get("prompt_tokens_details")
    input_token_details = usage.get("input_tokens_details")
    anthropic_cache_read_tokens = _safe_int(usage.get("cache_read_input_tokens"))
    anthropic_cache_creation_tokens: int | None = None
    if prompt_cache_hit_tokens is None and isinstance(prompt_token_details, dict):
        prompt_cache_hit_tokens = _safe_int(prompt_token_details.get("cached_tokens"))
    if prompt_cache_hit_tokens is None and isinstance(input_token_details, dict):
        prompt_cache_hit_tokens = _safe_int(input_token_details.get("cached_tokens"))
    if prompt_cache_hit_tokens is None:
        prompt_cache_hit_tokens = _safe_int(usage.get("cached_tokens"))
    if prompt_cache_hit_tokens is None:
        prompt_cache_hit_tokens = _safe_int(usage.get("input_cached_tokens"))
    if prompt_cache_creation_tokens is None and isinstance(prompt_token_details, dict):
        prompt_cache_creation_tokens = _safe_int(
            prompt_token_details.get("cache_creation_input_tokens")
        )
    if prompt_cache_creation_tokens is None and isinstance(input_token_details, dict):
        prompt_cache_creation_tokens = _safe_int(
            input_token_details.get("cache_creation_input_tokens")
        )
    if prompt_cache_creation_tokens is None:
        prompt_cache_creation_tokens = _safe_int(usage.get("cache_write_tokens"))
    if provider == "anthropic" or anthropic_cache_read_tokens is not None:
        anthropic_cache_creation_tokens = prompt_cache_creation_tokens
    if anthropic_cache_read_tokens is not None or anthropic_cache_creation_tokens is not None:
        prompt_cache_hit_tokens = anthropic_cache_read_tokens or 0
        if input_tokens is not None:
            prompt_cache_miss_tokens = input_tokens + (anthropic_cache_creation_tokens or 0)
            prompt_tokens = (
                input_tokens + prompt_cache_hit_tokens + (anthropic_cache_creation_tokens or 0)
            )
    elif (
        prompt_cache_miss_tokens is None
        and prompt_tokens is not None
        and prompt_cache_hit_tokens is not None
    ):
        prompt_cache_miss_tokens = max(0, prompt_tokens - prompt_cache_hit_tokens)
    if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
        total_tokens = prompt_tokens + completion_tokens
    values = (
        prompt_tokens,
        completion_tokens,
        total_tokens,
        prompt_cache_hit_tokens,
        prompt_cache_miss_tokens,
        prompt_cache_creation_tokens,
    )
    if not any(value is not None for value in values):
        return None
    return {
        "stage": stage,
        "provider": str(provider or "").strip() or None,
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cache_hit_tokens": prompt_cache_hit_tokens,
        "cache_miss_tokens": prompt_cache_miss_tokens,
        "cache_creation_tokens": prompt_cache_creation_tokens,
    }


def build_model_response(
    request: ModelRequest,
    body: object,
    *,
    model: str,
    status_code: int = 200,
) -> ModelResponse:
    """Turn a decoded body into a :class:`ModelResponse` (shared by real and fake clients)."""
    usage_payload = body.get("usage") if isinstance(body, dict) else None
    usage = usage_payload if isinstance(usage_payload, dict) else {}
    record_model = (
        request.usage_model if request.usage_model is not None else (model.strip() or None)
    )
    return ModelResponse(
        content=extract_llm_message_content(body),
        body=body,
        usage=usage,
        usage_record=normalize_usage_record(
            usage,
            stage=request.usage_stage,
            model=record_model,
            provider=request.provider,
        ),
        model=model,
        status_code=status_code,
    )


class ModelClient:
    """Real client: one short-lived ``httpx.Client`` per call, no streaming."""

    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        self._transport = transport

    def chat(self, request: ModelRequest) -> ModelResponse:
        spec = build_http_request(request)
        retries = max(0, int(request.transport_retries))
        client_kwargs: dict[str, object] = {"timeout": spec.timeout, "follow_redirects": True}
        if self._transport is not None:
            client_kwargs["transport"] = self._transport
        response: httpx.Response | None = None
        for attempt in range(retries + 1):
            try:
                with httpx.Client(**client_kwargs) as client:
                    response = client.post(spec.url, headers=spec.headers, json=spec.json)
                if is_retryable_status(response.status_code) and attempt < retries:
                    backoff_seconds = min(30.0, 1.5 * (2**attempt))
                    logger.warning(
                        "model status retry stage=%s model=%s status=%s attempt=%d/%d "
                        "backoff=%.1fs",
                        request.usage_stage,
                        spec.model,
                        response.status_code,
                        attempt + 1,
                        retries + 1,
                        backoff_seconds,
                    )
                    time.sleep(backoff_seconds)
                    continue
                break
            except Exception as exc:
                if attempt >= retries or not is_retryable_transport_error(exc):
                    raise
                backoff_seconds = min(6.0, 1.5 * (attempt + 1))
                logger.warning(
                    "model transport retry stage=%s model=%s attempt=%d/%d error=%s backoff=%.1fs",
                    request.usage_stage,
                    spec.model,
                    attempt + 1,
                    retries + 1,
                    exc,
                    backoff_seconds,
                )
                time.sleep(backoff_seconds)
        assert response is not None
        response.raise_for_status()
        body = parse_response_body(response) if request.lenient_body else response.json()
        return build_model_response(
            request, body, model=spec.model, status_code=response.status_code
        )
