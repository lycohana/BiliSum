"""Model step registry and :class:`StepRouter`.

Every pipeline model call goes through ``StepRouter``:

* routing (``agent`` / ``internal`` / ``local``) is decided per **step**
  (``summary``, ``mindmap`` ...). R2 only has ``internal`` and ``local``.
* parsing and retry policy are registered per **usage_stage** (``summary_chunk``,
  ``mindmap_repair`` ...), because the calls under one step differ. The
  policies copy the pre-R2 behaviour of ``real.py`` one to one; do not unify
  them (see ``tests/unit/test_model_step_policies.py``).
* ``AgentSignal`` always propagates: it is never retried and never triggers a
  fallback. Every broad ``except Exception`` that wraps a model step must be
  preceded by ``except AgentSignal: raise``.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import logging
import re
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal, Protocol, TypeVar

import httpx
from video_sum_infra.config import AGENT_DELEGATION_STEPS
from video_sum_infra.llm import extract_llm_message_content, is_anthropic_llm
from video_sum_infra.model_client import (
    ChatModelClient,
    ModelRequest,
    ModelResponse,
    ModelResponseError,
    ReasoningOptions,
    extract_response_error_detail,
)

from video_sum_core.errors import (
    LLMAuthenticationError,
    LLMConfigurationError,
    LLMTransientError,
    VideoSumError,
)
from video_sum_core.models.tasks import ModelRoute, ModelRouting, ModelSource

if TYPE_CHECKING:
    from video_sum_core.pipeline.real import PipelineSettings

logger = logging.getLogger("video_sum_core.pipeline.model_steps")

T = TypeVar("T")
Route = ModelRoute
Capability = Literal["text", "vision"]
ConfigSource = Literal["llm", "visual"]


# -- Agent signals -----------------------------------------------------------
class AgentSignal(Exception):
    """Base of the control-flow signals raised for Agent mode.

    Deliberately not a ``VideoSumError``: the pipeline's ``except VideoSumError``
    fallbacks must not see it. Broad ``except Exception`` blocks around model
    steps re-raise it explicitly.
    """


class AgentWorkRequired(AgentSignal):
    """Agent-routed calls have no result yet; ``items`` are this round's pending items."""

    def __init__(self, items: Sequence[PendingItem] | str = ()) -> None:
        if isinstance(items, str):  # plain message (tests, callers without items)
            self.items: list[PendingItem] = []
            super().__init__(items)
            return
        self.items = list(items)
        super().__init__(", ".join(item.item_id for item in self.items) or "agent work required")


class AgentContentRejected(AgentSignal):
    """Agent content for ``item_id`` failed the stage parser; its cache entry is gone.

    ``item`` is the pending item to hand out again.
    """

    def __init__(
        self, item_id: str = "", reason: str = "", item: PendingItem | None = None
    ) -> None:
        self.item_id = item_id
        self.reason = reason
        self.item = item
        super().__init__(f"{item_id}: {reason}" if reason else item_id)


# -- Agent execution mode: pending items, result cache, session ---------------
def result_key(usage_stage: str, index: int) -> str:
    """Cache key and pending ``item_id``: ``usage_stage#logical_index`` (design 3.3 premise 1)."""
    return f"{usage_stage}#{int(index)}"


def request_hash(request: ModelRequest) -> str:
    """Prompt fingerprint; only used to warn when a cached result was made for another prompt."""
    material = {"usage_stage": request.usage_stage, "messages": request.payload.get("messages")}
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PendingItem:
    """One model call the Agent has to answer. ``messages`` are exactly what the
    internal model would have received (OpenAI chat shape)."""

    item_id: str
    step: str
    usage_stage: str
    index: int
    kind: str  # capability: text | vision
    messages: list[object]
    response_format: Literal["json", "text"]
    request_hash: str
    images: tuple[
        str, ...
    ] = ()  # local image files; messages reference them by ``image_ref`` blocks

    def to_dict(self) -> dict[str, object]:
        """JSON shape of the item.

        ``images`` carries ``{"path"}`` only; the service adds ``url``.
        """
        return {
            "item_id": self.item_id,
            "step": self.step,
            "usage_stage": self.usage_stage,
            "index": self.index,
            "kind": self.kind,
            "messages": self.messages,
            "response_format": self.response_format,
            "request_hash": self.request_hash,
            "images": [{"path": path} for path in self.images],
        }


def _agent_messages(messages: object) -> list[object]:
    """``messages`` for the Agent.

    Inline (base64) images become ``{"type": "image_ref", "index": i}`` blocks pointing at
    ``PendingItem.images[i]``; everything else is kept as is.
    """
    if not isinstance(messages, list):
        return []
    image_count = 0
    result: list[object] = []
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            result.append(message)
            continue
        blocks: list[object] = []
        for block in content:
            image = (
                block.get("image_url")
                if isinstance(block, dict) and block.get("type") == "image_url"
                else None
            )
            url = image.get("url") if isinstance(image, dict) else None
            if isinstance(url, str) and url.startswith("data:"):
                blocks.append({"type": "image_ref", "index": image_count})
                image_count += 1
            else:
                blocks.append(block)
        result.append({**message, "content": blocks})
    return result


class AgentResultStore(Protocol):
    """Per-task cache of every step result of an Agent task (service: ``agent_work``).

    Entries are JSON objects keyed by :func:`result_key`:

    * ``{"source": "agent", "content": str}``  (submitted by the Agent)
    * ``{"source": "agent", "declined": True, "reason": str}``  (Agent declined: default chain)
    * ``{"source": "internal", "response": {...}}``  (builtin model reply)
    * ``{"source": "internal" | "local", "error": str, "error_type": str}``  (failure; ``local``
      = the caller's fallback replaced it)

    Every entry may carry ``request_hash``.
    """

    def load(self, key: str) -> dict[str, object] | None: ...

    def save(self, key: str, entry: dict[str, object]) -> None: ...

    def discard(self, key: str) -> None: ...


@dataclass(frozen=True)
class AgentSession:
    routing: ModelRouting
    store: AgentResultStore


_AGENT_SESSION: contextvars.ContextVar[AgentSession | None] = contextvars.ContextVar(
    "bilisum_agent_session", default=None
)


@contextlib.contextmanager
def agent_session(routing: ModelRouting, store: AgentResultStore) -> Iterator[AgentSession]:
    """Run an Agent task's summary unit: route by ``routing``, cache every result in ``store``.

    Outside a session the router behaves exactly as for builtin tasks (no cache).
    Worker threads see it when started with :func:`submit_in_context`.
    """
    session = AgentSession(routing, store)
    token = _AGENT_SESSION.set(session)
    try:
        yield session
    finally:
        _AGENT_SESSION.reset(token)


_ERROR_TYPES: dict[str, type[Exception]] = {
    "LLMAuthenticationError": LLMAuthenticationError,
    "LLMConfigurationError": LLMConfigurationError,
}


def _cached_error(entry: dict[str, object]) -> Exception:
    error_type = _ERROR_TYPES.get(str(entry.get("error_type") or ""), VideoSumError)
    return error_type(str(entry.get("error") or "cached failure"))


def _response_to_entry(response: ModelResponse) -> dict[str, object]:
    return {
        "content": response.content,
        "body": response.body,
        "usage": response.usage,
        "usage_record": response.usage_record,
        "model": response.model,
        "status_code": response.status_code,
    }


def _response_from_entry(data: dict[str, object]) -> ModelResponse:
    usage = data.get("usage")
    usage_record = data.get("usage_record")
    return ModelResponse(
        content=str(data.get("content") or ""),
        body=data.get("body"),
        usage=dict(usage) if isinstance(usage, dict) else {},
        usage_record=dict(usage_record) if isinstance(usage_record, dict) else None,
        model=str(data.get("model") or ""),
        status_code=int(data.get("status_code") or 200),
    )


def agent_response(content: str) -> ModelResponse:
    """An Agent's raw output wrapped as an OpenAI chat response, so parsing stays unchanged."""
    body = {"choices": [{"message": {"role": "assistant", "content": content}}]}
    return ModelResponse(content=extract_llm_message_content(body) or "", body=body)


# -- content parsing helpers (moved from real.py) -----------------------------
def extract_json_object_text(value: str) -> str:
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


def _truncate(value: str, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit]


def parse_llm_json_content(content: str) -> dict[str, object]:
    # 移除可能的 think 标签（某些模型如 MiniMax-M2.5 可能返回）
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)

    candidates = [str(content or "").strip()]
    extracted = extract_json_object_text(content)
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

    preview = _truncate(str(content or "").replace("\r", "\\r").replace("\n", "\\n"), 600)
    logging.getLogger("video_sum_core.pipeline.real").error(
        "llm json returned invalid content preview=%s", preview
    )
    raise VideoSumError("LLM returned invalid JSON content.")


def extract_llm_response_content(response_json: object) -> str:
    if not isinstance(response_json, dict):
        raise VideoSumError("LLM returned an unexpected response payload.")
    content = extract_llm_message_content(response_json)
    if content:
        return content
    raise VideoSumError("LLM returned no readable message content.")


# -- stage parsers: "is this content usable?" ----------------------------------
def parse_llm_json_response(response: ModelResponse) -> dict[str, object]:
    """Text stages: JSON object plus the normalized usage fields real.py expects."""
    parsed = parse_llm_json_content(extract_llm_response_content(response.body))
    parsed.setdefault("title", "")
    parsed.setdefault("overview", "")
    parsed.setdefault("bulletPoints", [])
    parsed.setdefault("chapters", [])
    usage_record = response.usage_record
    parsed["llm_prompt_tokens"] = usage_record.get("prompt_tokens") if usage_record else None
    parsed["llm_completion_tokens"] = (
        usage_record.get("completion_tokens") if usage_record else None
    )
    parsed["llm_total_tokens"] = usage_record.get("total_tokens") if usage_record else None
    parsed["llm_prompt_cache_hit_tokens"] = (
        usage_record.get("cache_hit_tokens") if usage_record else None
    )
    parsed["llm_prompt_cache_miss_tokens"] = (
        usage_record.get("cache_miss_tokens") if usage_record else None
    )
    parsed["llm_prompt_cache_creation_tokens"] = (
        usage_record.get("cache_creation_tokens") if usage_record else None
    )
    if usage_record:
        parsed["_llm_usage_record"] = usage_record
    return parsed


def parse_knowledge_note_response(response: ModelResponse) -> dict[str, object]:
    parsed = parse_llm_json_response(response)
    parsed.setdefault("knowledgeNoteMarkdown", "")
    if not str(parsed.get("knowledgeNoteMarkdown") or "").strip():
        raise VideoSumError("LLM returned empty knowledge note markdown.")
    return parsed


def parse_visual_frame_response(response: ModelResponse) -> dict[str, object]:
    parsed = json.loads(extract_json_object_text(response.content))
    if not isinstance(parsed, dict):
        raise ValueError("visual frame reply is not a JSON object")
    return parsed


def parse_visual_compose_response(response: ModelResponse) -> str:
    content = response.content
    if not content:
        raise VideoSumError("图文笔记模型返回空内容。")
    return content


# -- retry policies (copied from pre-R2 real.py, one per family) ---------------
Attempt = Callable[[], T]


def _retry_count(settings: PipelineSettings) -> int:
    return max(0, int(settings.summary_chunk_retry_count))


@dataclass(frozen=True)
class RetryPolicy:
    """How one ``usage_stage`` reacts to failures.

    ``transport_retries`` is the per-call budget handed to ``ModelClient``;
    ``execute`` is the response-level loop around the call+parse attempt.
    """

    name: str

    def transport_retries(self, settings: PipelineSettings) -> int:
        return 0

    def execute(self, attempt: Attempt[T], settings: PipelineSettings, stage: str) -> T:
        return attempt()


@dataclass(frozen=True)
class ResponseRetry(RetryPolicy):
    """Old ``_request_llm_json_with_retries``: retry timeouts, transport errors,
    429/5xx (``LLMTransientError``) and 200-with-empty-content with exponential
    backoff; auth/config and other ``VideoSumError`` raise at once."""

    def execute(self, attempt: Attempt[T], settings: PipelineSettings, stage: str) -> T:
        retry_count = _retry_count(settings)
        last_error: Exception | None = None
        for index in range(retry_count + 1):
            try:
                return attempt()
            except (LLMAuthenticationError, LLMConfigurationError):
                raise
            except httpx.TimeoutException as exc:
                last_error = exc
            except httpx.TransportError as exc:
                last_error = exc
            except LLMTransientError as exc:
                last_error = exc
            except VideoSumError as exc:
                if "no readable message content" not in str(exc).lower():
                    raise
                last_error = exc
            if index >= retry_count:
                break
            logger.warning(
                "llm response retry model=%s stage=%s attempt=%d/%d error=%s",
                settings.llm_model,
                stage,
                index + 1,
                retry_count,
                last_error,
            )
            time.sleep(min(30.0, 1.5 * (2**index)))
        if last_error is not None:
            raise last_error
        raise VideoSumError("LLM request failed before receiving a response.")


@dataclass(frozen=True)
class ChunkRetry(RetryPolicy):
    """Old ``_request_llm_summary_chunk``: any error retried (no backoff), the
    client also retries transport errors; exhausted -> ``VideoSumError``."""

    def transport_retries(self, settings: PipelineSettings) -> int:
        return _retry_count(settings)

    def execute(self, attempt: Attempt[T], settings: PipelineSettings, stage: str) -> T:
        last_error: Exception | None = None
        for index in range(_retry_count(settings) + 1):
            try:
                return attempt()
            except AgentSignal:
                raise
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "llm summary chunk request failed model=%s attempt=%d error=%s",
                    settings.llm_model,
                    index + 1,
                    exc,
                )
        raise VideoSumError(str(last_error) if last_error else "Chunk failed.")


@dataclass(frozen=True)
class SingleAttempt(RetryPolicy):
    """One response-level attempt; only the client's transport retries."""

    client_transport_retries: int = 0

    def transport_retries(self, settings: PipelineSettings) -> int:
        return self.client_transport_retries


@dataclass(frozen=True)
class FrameRetry(RetryPolicy):
    """Old per-frame loop in ``_describe_visual_frames``: any error retried
    ``visual_evidence_retry_count`` (0 counts as 1, max 3) times with backoff."""

    def execute(self, attempt: Attempt[T], settings: PipelineSettings, stage: str) -> T:
        retry_count = max(0, min(int(settings.visual_evidence_retry_count or 1), 3))
        last_error: Exception | None = None
        for index in range(retry_count + 1):
            try:
                return attempt()
            except AgentSignal:
                raise
            except Exception as exc:
                last_error = exc
                if index < retry_count:
                    time.sleep(min(4.0, 1.2 * (index + 1)))
        assert last_error is not None
        raise last_error


@dataclass(frozen=True)
class ProbeRetry(RetryPolicy):
    """Old ``_preflight_llm_availability`` loop: every ``VideoSumError`` retried,
    timeouts/transport errors reworded, auth/config raise at once."""

    def execute(self, attempt: Attempt[T], settings: PipelineSettings, stage: str) -> T:
        retry_count = _retry_count(settings)
        last_error: VideoSumError | None = None
        for index in range(retry_count + 1):
            try:
                return attempt()
            except (LLMAuthenticationError, LLMConfigurationError):
                raise
            except httpx.TimeoutException as exc:
                last_error = VideoSumError(
                    "LLM API 检查超时，请稍后重试或检查 Base URL / 网络代理。"
                )
                last_error.__cause__ = exc
            except httpx.TransportError as exc:
                last_error = VideoSumError(f"LLM API 检查失败，请检查 Base URL / 网络代理：{exc}")
                last_error.__cause__ = exc
            except VideoSumError as exc:
                last_error = exc
            if index >= retry_count:
                break
            logger.warning(
                "llm preflight failed, retrying attempt=%d/%d error=%s",
                index + 1,
                retry_count,
                last_error,
            )
            time.sleep(min(1.5, 0.25 * (2**index)))
        if last_error is not None:
            raise last_error
        raise VideoSumError("LLM request failed before receiving a response.")


RESPONSE_RETRY = ResponseRetry("response_retry")
CHUNK_RETRY = ChunkRetry("chunk_retry")
FRAME_RETRY = FrameRetry("frame_retry")
PROBE_RETRY = ProbeRetry("probe_retry")


# -- registry ----------------------------------------------------------------
def _llm_enabled(settings: PipelineSettings) -> bool:
    return bool(settings.llm_enabled and settings.llm_api_key)


def _keyframe_llm_enabled(settings: PipelineSettings) -> bool:
    return bool(_llm_enabled(settings) and settings.llm_base_url and settings.llm_model)


def visual_config(settings: PipelineSettings) -> tuple[str, str, str, str]:
    provider = (settings.visual_vlm_provider or "openai-compatible").strip()
    base_url = (settings.visual_evidence_base_url or settings.llm_base_url or "").rstrip("/")
    model = settings.visual_evidence_model or settings.llm_model
    api_key = settings.visual_evidence_api_key or settings.llm_api_key
    return provider, base_url, model, api_key


def _visual_enabled(settings: PipelineSettings) -> bool:
    if not settings.visual_evidence_use_llm:
        return False
    if not settings.visual_multimodal_enabled:
        return False
    _provider, base_url, model, api_key = visual_config(settings)
    return bool(base_url and model and api_key)


@dataclass(frozen=True)
class ModelStep:
    name: str
    capability: Capability
    config_source: ConfigSource
    local_fallback: str
    usage_stages: tuple[str, ...]
    available: Callable[[PipelineSettings], bool]


@dataclass(frozen=True)
class StageSpec:
    usage_stage: str
    step: str
    parse: Callable[[ModelResponse], object]
    retry: RetryPolicy
    map_errors: bool = True  # text stages map HTTP errors to core errors


# Note: visual_compose reads the *visual* model config (as real.py always did),
# although the design table lists LLM; its usage_stage keeps the historical
# label ``visual_note_compose`` (desktop shows it in the usage breakdown).
STEPS: dict[str, ModelStep] = {
    step.name: step
    for step in (
        ModelStep(
            "summary",
            "text",
            "llm",
            "_summarize_with_rules",
            ("summary_full", "summary_chunk", "summary_merge", "aggregate_summary"),
            _llm_enabled,
        ),
        ModelStep(
            "knowledge_note",
            "text",
            "llm",
            "rule-built note (summary kept)",
            ("knowledge_note",),
            _llm_enabled,
        ),
        ModelStep(
            "keyframe_plan",
            "text",
            "llm",
            "_build_visual_keyframe_plan_locally",
            ("visual_keyframe_plan",),
            _keyframe_llm_enabled,
        ),
        ModelStep(
            "visual_frame",
            "vision",
            "visual",
            "placeholder observation (text mode)",
            ("visual_frame",),
            _visual_enabled,
        ),
        ModelStep(
            "visual_compose",
            "text",
            "visual",
            "_compose_visual_note_locally",
            ("visual_note_compose",),
            _visual_enabled,
        ),
        ModelStep(
            "mindmap",
            "text",
            "llm",
            "_build_mindmap_fallback",
            ("mindmap", "mindmap_repair"),
            _llm_enabled,
        ),
    )
}

# Steps the agent engine may take over (F2: text steps; F3: the visual note steps).
AGENT_DELEGABLE_STEPS: tuple[str, ...] = tuple(STEPS)
# The settings vocabulary (infra) must name exactly these steps.
assert set(AGENT_DELEGATION_STEPS) == set(STEPS), "AGENT_DELEGATION_STEPS out of sync with STEPS"


def agent_steps_for(capabilities: Sequence[str], delegation: Sequence[str]) -> list[str]:
    """Steps handed to the Agent: needed capability declared AND user allows it (STEPS order)."""
    return [
        name
        for name, step in STEPS.items()
        if name in AGENT_DELEGABLE_STEPS and step.capability in capabilities and name in delegation
    ]

PROBE_STAGE = "llm"
STAGES: dict[str, StageSpec] = {
    spec.usage_stage: spec
    for spec in (
        StageSpec("summary_full", "summary", parse_llm_json_response, RESPONSE_RETRY),
        StageSpec("summary_chunk", "summary", parse_llm_json_response, CHUNK_RETRY),
        StageSpec("summary_merge", "summary", parse_llm_json_response, RESPONSE_RETRY),
        StageSpec("aggregate_summary", "summary", parse_llm_json_response, RESPONSE_RETRY),
        StageSpec(
            "knowledge_note", "knowledge_note", parse_knowledge_note_response, RESPONSE_RETRY
        ),
        StageSpec(
            "visual_keyframe_plan",
            "keyframe_plan",
            parse_llm_json_response,
            SingleAttempt("single", 1),
        ),
        StageSpec(
            "visual_frame",
            "visual_frame",
            parse_visual_frame_response,
            FRAME_RETRY,
            map_errors=False,
        ),
        StageSpec(
            "visual_note_compose",
            "visual_compose",
            parse_visual_compose_response,
            SingleAttempt("single", 0),
            map_errors=False,
        ),
        StageSpec("mindmap", "mindmap", parse_llm_json_response, RESPONSE_RETRY),
        StageSpec("mindmap_repair", "mindmap", parse_llm_json_response, RESPONSE_RETRY),
    )
}
PROBE_SPEC = StageSpec(PROBE_STAGE, "probe", parse_llm_json_response, PROBE_RETRY)


VISUAL_STEPS: tuple[str, ...] = ("keyframe_plan", "visual_frame", "visual_compose")
"""Steps of the 图文笔记 flow (regenerating the visual note re-reports exactly these)."""


def step_for_stage(usage_stage: str) -> str:
    return STAGES[usage_stage].step


# -- route records -------------------------------------------------------------
_ROUTE_STRENGTH: dict[str, int] = {"local": 0, "internal": 1, "agent": 2}


def _weakest(first: Route, second: Route) -> Route:
    return first if _ROUTE_STRENGTH[first] <= _ROUTE_STRENGTH[second] else second


@dataclass(frozen=True)
class RouteRecord:
    step: str
    usage_stage: str
    index: int
    source: Route
    error: str | None = None


@dataclass
class RouteLog:
    records: list[RouteRecord] = field(default_factory=list)
    marks: dict[str, Route] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, record: RouteRecord) -> None:
        with self._lock:
            self.records.append(record)

    def mark(self, step: str, source: Route) -> None:
        """Caller's final word on ``step`` (e.g. it ran locally without calling the router)."""
        with self._lock:
            self.marks[step] = source

    def sources(self) -> dict[str, Route]:
        """Per step that left a trace: its mark if any; else the weakest source
        among its items, independent of record order.

        An item (``usage_stage#index``) counts as ``local`` when it fell back
        locally or every call for it failed (the caller recovered without the
        model); otherwise it counts as the source that answered it. A step is
        ``local`` if any item is local, else ``internal`` if any item is
        internal, else ``agent``: a partly-fallback step is never shown as
        fully Agent / model made.
        """
        with self._lock:
            records = list(self.records)
            marks = dict(self.marks)
        answered: dict[tuple[str, str, int], Route] = {}
        failed: set[tuple[str, str, int]] = set()
        for record in records:
            key = (record.step, record.usage_stage, record.index)
            if record.source == "local" or record.error is None:
                # Answered: by the Agent / internal model, or by the local fallback.
                answered[key] = (
                    _weakest(answered[key], record.source) if key in answered else record.source
                )
            else:
                failed.add(key)
        items = {key: "local" for key in failed if key not in answered}
        items.update(answered)
        result: dict[str, Route] = {}
        for (step, _, _), source in items.items():
            result[step] = _weakest(result[step], source) if step in result else source
        result.update(marks)
        return result

    def model_sources(self, steps: Sequence[str] | None = None) -> dict[str, ModelSource]:
        """``TaskResult.model_sources``.

        Every step in ``steps`` (default all); ``none`` if it did not run.
        """
        sources = self.sources()
        return {step: sources.get(step, "none") for step in (STEPS if steps is None else steps)}


_ACTIVE_LOG: contextvars.ContextVar[RouteLog | None] = contextvars.ContextVar(
    "bilisum_route_log", default=None
)
_IN_ROUTER: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "bilisum_in_step_router", default=False
)


def require_step_router(request: ModelRequest) -> None:
    """``FakeModelClient(guard=...)`` hook: fail any model call made outside ``StepRouter``."""
    if not _IN_ROUTER.get():
        raise AssertionError(
            f"model call bypassed StepRouter (usage_stage={request.usage_stage!r})"
        )


@contextlib.contextmanager
def record_routes() -> Iterator[RouteLog]:
    """Collect route records of every routed call made in this context (F1b ``model_sources``).

    Worker threads only see it when started with :func:`submit_in_context`.
    """
    log = RouteLog()
    token = _ACTIVE_LOG.set(log)
    try:
        yield log
    finally:
        _ACTIVE_LOG.reset(token)


def mark_route(step: str, source: Route) -> None:
    """Record the final source of ``step`` in the active route log (no-op outside one)."""
    log = _ACTIVE_LOG.get()
    if log is not None:
        log.mark(step, source)


def submit_in_context[R](executor, fn: Callable[..., R], /, *args: object, **kwargs: object):
    """``executor.submit`` that carries the caller's context (route log) into the worker thread."""
    return executor.submit(contextvars.copy_context().run, fn, *args, **kwargs)


# -- router --------------------------------------------------------------------
class StepRouter:
    def __init__(self, settings: PipelineSettings, model_client: ChatModelClient) -> None:
        self._settings = settings
        self._client = model_client

    # routing
    def resolve(self, step: str) -> Route:
        """``agent`` when the running Agent task's route table delegates ``step``
        (fixed at creation, never recomputed from settings), else the default chain."""
        session = _AGENT_SESSION.get()
        if session is not None and session.routing.delegates(step):
            return "agent"
        return self._default_route(step)

    def _default_route(self, step: str) -> Route:
        return "internal" if STEPS[step].available(self._settings) else "local"

    def available(self, step: str) -> bool:
        """The caller should build model requests for ``step`` (internal model or Agent)."""
        return self.resolve(step) != "local"

    def needs_probe(self) -> bool:
        """Preflight probes the LLM only when some LLM-backed step routes to it."""
        if not str(self._settings.llm_api_key or "").strip():
            return False
        return any(
            self.resolve(step.name) == "internal"
            for step in STEPS.values()
            if step.config_source == "llm"
        )

    # configuration
    def llm_base_url(self) -> str:
        return (self._settings.llm_base_url or "").rstrip("/")

    def require_llm_config(self, step: str | None = None) -> str:
        """Base URL of the internal LLM; raises when it is incomplete, unless
        ``step`` is routed to the Agent (no internal model needed)."""
        base_url = self.llm_base_url()
        if step is not None and self.resolve(step) == "agent":
            return base_url
        if not base_url or not self._settings.llm_model:
            raise LLMConfigurationError("LLM 配置不完整，请检查 Base URL 和模型名。")
        return base_url

    def visual_config(self) -> tuple[str, str, str, str]:
        return visual_config(self._settings)

    def llm_request(
        self,
        usage_stage: str,
        payload: dict[str, object],
        *,
        base_url: str | None = None,
        timeout: float = 180,
    ) -> ModelRequest:
        settings = self._settings
        base_url = self.llm_base_url() if base_url is None else base_url
        payload = dict(payload)
        use_anthropic = is_anthropic_llm(settings.llm_provider, base_url)
        if not use_anthropic:
            payload["model"] = str(payload.get("model") or settings.llm_model or "")
        return ModelRequest(
            usage_stage=usage_stage,
            base_url=base_url,
            api_key=settings.llm_api_key,
            payload=payload,
            wire_format="anthropic" if use_anthropic else "openai",
            provider=settings.llm_provider,
            reasoning=self.reasoning(),
            timeout=timeout,
            usage_model=(
                None
                if str(payload.get("model") or "").strip()
                else (str(settings.llm_model or "").strip() or None)
            ),
        )

    def reasoning(self) -> ReasoningOptions:
        return ReasoningOptions(
            thinking_type=self._settings.llm_thinking_type,
            reasoning_effort=self._settings.llm_reasoning_effort,
            provider=self._settings.llm_provider,
        )

    # execution
    def recording(self) -> contextlib.AbstractContextManager[RouteLog]:
        """See :func:`record_routes`."""
        return record_routes()

    def mark_local(self, step: str) -> None:
        """``step`` was produced by its local fallback without (successfully) asking a model."""
        mark_route(step, "local")

    def run(
        self,
        step: str,
        request: ModelRequest,
        *,
        index: int = 0,
        fallback: Callable[[Exception], T] | None = None,
        on_response: Callable[[ModelResponse], None] | None = None,
    ) -> object:
        """Execute one call of ``step`` with its stage policy; return the parsed result.

        ``fallback(exc)`` (if given) replaces the result on any failure except
        ``AgentSignal``. ``on_response`` sees every successful raw response
        before parsing (usage accounting). Callers gate with :meth:`available`.
        """
        spec = self._spec(step, request)
        session = _AGENT_SESSION.get()
        if session is not None:
            return self._run_in_session(session, spec, request, index, fallback, on_response)
        return self._run_default(spec, request, index, fallback, on_response)

    def run_many(
        self,
        step: str,
        requests: Sequence[ModelRequest],
        *,
        indices: Sequence[int],
        fallback: Callable[[Exception, int], T] | None = None,
        on_response: Callable[[ModelResponse], None] | None = None,
    ) -> list[object]:
        """Run several calls of one step; results come back in input order.

        For an Agent-routed step, every call still lacking a result is collected
        first and raised as one :class:`AgentWorkRequired` (one round, whole batch).
        """
        if len(requests) != len(indices):
            raise ValueError("requests and indices must have the same length")
        session = _AGENT_SESSION.get()
        if session is not None and self.resolve(step) == "agent":
            missing = [
                self._pending_item(self._spec(step, request), request, index)
                for request, index in zip(requests, indices, strict=True)
                if session.store.load(result_key(request.usage_stage, index)) is None
            ]
            if missing:
                raise AgentWorkRequired(missing)
        results: list[object] = []
        for request, index in zip(requests, indices, strict=True):
            item_fallback = None if fallback is None else (lambda exc, _i=index: fallback(exc, _i))
            results.append(
                self.run(
                    step, request, index=index, fallback=item_fallback, on_response=on_response
                )
            )
        return results

    def probe(self) -> None:
        """Preflight connectivity check of the internal LLM.

        Formerly ``_preflight_llm_availability``.
        """
        base_url = self.require_llm_config()
        # 说明：有意不传 response_format（json_object）
        # 本地 LLM 后端（Ollama/llama.cpp/vLLM 等）不支持该参数，会返回 400
        # 输出格式靠 system prompt 中"只输出合法 JSON"约束
        payload = {
            "model": self._settings.llm_model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是一个 JSON 测试助手。你只能返回合法 JSON，不能输出任何额外文字。"
                    ),
                },
                {"role": "user", "content": '请只返回：{"ok":true}'},
            ],
            "temperature": 0,
            "max_tokens": 32,
            "enable_thinking": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        request = self.llm_request(PROBE_STAGE, payload, base_url=base_url, timeout=20)
        self._execute(PROBE_SPEC, request, None)

    # internals
    def _run_default(
        self,
        spec: StageSpec,
        request: ModelRequest,
        index: int,
        fallback: Callable[[Exception], T] | None,
        on_response: Callable[[ModelResponse], None] | None,
        *,
        cache: Callable[[dict[str, object]], None] | None = None,
    ) -> object:
        """Default chain: internal model with the stage policy, ``fallback`` on failure.

        ``cache`` (Agent tasks only) receives the outcome entry, failures included.
        """
        responses: list[ModelResponse] = []
        try:
            result = self._execute(spec, request, on_response, responses)
        except AgentSignal:
            raise
        except Exception as exc:
            if cache is not None:
                source = "internal" if fallback is None else "local"
                cache({"source": source, "error": str(exc), "error_type": type(exc).__name__})
            if fallback is None:
                self._record(spec, index, "internal", exc)
                raise
            logger.warning(
                "model step fallback step=%s stage=%s index=%d error=%s",
                spec.step,
                spec.usage_stage,
                index,
                exc,
            )
            self._record(spec, index, "local", exc)
            return fallback(exc)
        if cache is not None and responses:
            cache({"source": "internal", "response": _response_to_entry(responses[-1])})
        self._record(spec, index, "internal")
        return result

    def _run_in_session(
        self,
        session: AgentSession,
        spec: StageSpec,
        request: ModelRequest,
        index: int,
        fallback: Callable[[Exception], T] | None,
        on_response: Callable[[ModelResponse], None] | None,
    ) -> object:
        """Agent task: every outcome is cached under ``usage_stage#index``, replayed from there."""
        key = result_key(spec.usage_stage, index)
        fingerprint = request_hash(request)
        entry = session.store.load(key)
        if entry is not None and not entry.get("declined"):
            cached_hash = entry.get("request_hash")
            if cached_hash and cached_hash != fingerprint:
                logger.warning(
                    "cached result was made for another prompt key=%s "
                    "(service or settings changed?)",
                    key,
                )
            return self._replay(session, spec, request, index, key, entry, fallback, on_response)
        if entry is None and self.resolve(spec.step) == "agent":
            raise AgentWorkRequired([self._pending_item(spec, request, index)])

        def cache(outcome: dict[str, object]) -> None:
            session.store.save(key, {**outcome, "request_hash": fingerprint})

        if entry is not None and self._default_route(spec.step) == "local":
            # Declined by the Agent and no internal model: same as an unconfigured LLM.
            exc = LLMConfigurationError(f"Agent 拒绝了 {key}，且未配置内置模型。")
            source = "internal" if fallback is None else "local"
            cache({"source": source, "error": str(exc), "error_type": type(exc).__name__})
            self._record(spec, index, source, exc)
            if fallback is None:
                raise exc
            return fallback(exc)
        return self._run_default(spec, request, index, fallback, on_response, cache=cache)

    def _replay(
        self,
        session: AgentSession,
        spec: StageSpec,
        request: ModelRequest,
        index: int,
        key: str,
        entry: dict[str, object],
        fallback: Callable[[Exception], T] | None,
        on_response: Callable[[ModelResponse], None] | None,
    ) -> object:
        source = str(entry.get("source") or "internal")
        if "error" in entry:
            exc = _cached_error(entry)
            if fallback is None or source != "local":
                self._record(spec, index, "internal", exc)
                raise exc
            self._record(spec, index, "local", exc)
            return fallback(exc)
        if source == "agent":
            response = agent_response(str(entry.get("content") or ""))
            if on_response is not None:
                on_response(response)
            try:
                # No parse retry: every attempt would read the same cached content.
                result = spec.parse(response)
            except Exception as exc:
                session.store.discard(key)
                reason = str(exc) or type(exc).__name__
                raise AgentContentRejected(
                    key, reason, self._pending_item(spec, request, index)
                ) from exc
            self._record(spec, index, "agent")
            return result
        data = entry.get("response")
        response = _response_from_entry(data if isinstance(data, dict) else {})
        if on_response is not None:
            on_response(response)
        result = spec.parse(response)
        self._record(spec, index, "internal")
        return result

    def _pending_item(self, spec: StageSpec, request: ModelRequest, index: int) -> PendingItem:
        return PendingItem(
            item_id=result_key(spec.usage_stage, index),
            step=spec.step,
            usage_stage=spec.usage_stage,
            index=int(index),
            kind=STEPS[spec.step].capability,
            messages=_agent_messages(request.payload.get("messages")),
            response_format="text" if spec.parse is parse_visual_compose_response else "json",
            request_hash=request_hash(request),
            images=tuple(str(path) for path in request.images),
        )

    def _spec(self, step: str, request: ModelRequest) -> StageSpec:
        spec = STAGES.get(request.usage_stage)
        if spec is None or spec.step != step:
            raise ValueError(
                f"usage_stage {request.usage_stage!r} is not registered under step {step!r}"
            )
        return spec

    def _execute(
        self,
        spec: StageSpec,
        request: ModelRequest,
        on_response: Callable[[ModelResponse], None] | None,
        responses: list[ModelResponse] | None = None,
    ) -> object:
        request = replace(request, transport_retries=spec.retry.transport_retries(self._settings))

        def attempt() -> object:
            response = self._call(spec, request)
            if responses is not None:
                responses.append(response)
            if on_response is not None:
                on_response(response)
            return spec.parse(response)

        return spec.retry.execute(attempt, self._settings, spec.usage_stage)

    def _call(self, spec: StageSpec, request: ModelRequest) -> ModelResponse:
        token = _IN_ROUTER.set(True)
        try:
            if not spec.map_errors:
                return self._client.chat(request)
            try:
                response = self._client.chat(request)
            except httpx.HTTPStatusError as exc:
                detail = extract_response_error_detail(exc.response)
                logger.error(
                    "llm json request failed status=%s model=%s detail=%s",
                    exc.response.status_code,
                    self._settings.llm_model,
                    detail,
                )
                status_code = exc.response.status_code
                if status_code in (401, 403):
                    raise LLMAuthenticationError(
                        "LLM API Key 无效、已过期，或当前模型/接口无权限访问"
                        f"（HTTP {status_code}: {detail}）。"
                    ) from exc
                if status_code == 429 or status_code >= 500:
                    raise LLMTransientError(
                        f"LLM request failed with status {status_code}: {detail}"
                    ) from exc
                raise VideoSumError(
                    f"LLM request failed with status {status_code}: {detail}"
                ) from exc
            except ModelResponseError as exc:
                raise VideoSumError(str(exc)) from exc
            logger.info(
                "llm json response status=%s model=%s",
                response.status_code,
                self._settings.llm_model,
            )
            return response
        finally:
            _IN_ROUTER.reset(token)

    def _record(
        self, spec: StageSpec, index: int, source: Route, error: Exception | None = None
    ) -> None:
        log = _ACTIVE_LOG.get()
        if log is not None:
            log.add(
                RouteRecord(
                    spec.step,
                    spec.usage_stage,
                    index,
                    source,
                    None if error is None else str(error),
                )
            )
