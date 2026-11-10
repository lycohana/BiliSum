"""Scripted, recording stand-in for :class:`~video_sum_infra.model_client.ModelClient`.

Inject it wherever a ``ModelClient`` is accepted (e.g.
``RealPipelineRunner(settings, model_client=fake)``). Every call is recorded
in ``fake.requests`` and answered from a script.

Script items (``script=[...]`` in call order, or ``by_stage={"mindmap": [...]}``
per ``usage_stage``, which wins over the shared script):

* ``str``            -> reply content
* ``dict``/``list``  -> reply content ``json.dumps``-ed (ensure_ascii=False)
* ``RawBody(body)``  -> ``body`` treated as the decoded provider JSON body
                        (any shape: OpenAI choices, Anthropic content blocks ...)
* ``ModelResponse``  -> returned as is
* ``BaseException``  -> raised (instance or class)
* callable           -> called with the ``ModelRequest``; its return value is
                        interpreted with the same rules

When both queues are empty, ``default`` is used if given, else the call fails
with ``AssertionError``. ``guard`` (if set) runs before anything else on each
call and may raise, e.g. to reject calls made outside ``StepRouter``.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from collections.abc import Callable, Iterable, Mapping

from video_sum_infra.model_client import (
    HttpRequestSpec,
    ModelRequest,
    ModelResponse,
    build_http_request,
    build_model_response,
)

ScriptItem = object


class RawBody:
    """Script item: reply with this decoded provider body instead of plain content."""

    def __init__(self, body: object) -> None:
        self.body = body


class FakeModelClient:
    def __init__(
        self,
        script: Iterable[ScriptItem] | None = None,
        *,
        by_stage: Mapping[str, Iterable[ScriptItem]] | None = None,
        default: ScriptItem | None = None,
        usage: Mapping[str, object] | None = None,
        guard: Callable[[ModelRequest], None] | None = None,
    ) -> None:
        self._script: deque[ScriptItem] = deque(script or ())
        self._by_stage: dict[str, deque[ScriptItem]] = {
            stage: deque(items) for stage, items in (by_stage or {}).items()
        }
        self._default = default
        self._usage = dict(usage or {})
        self.guard = guard
        self.requests: list[ModelRequest] = []
        self._lock = threading.Lock()

    # -- scripting -----------------------------------------------------
    def push(self, *items: ScriptItem, stage: str | None = None) -> None:
        with self._lock:
            if stage is None:
                self._script.extend(items)
            else:
                self._by_stage.setdefault(stage, deque()).extend(items)

    # -- inspection ----------------------------------------------------
    @property
    def stages(self) -> list[str]:
        return [request.usage_stage for request in self.requests]

    def calls_for(self, stage: str) -> list[ModelRequest]:
        return [request for request in self.requests if request.usage_stage == stage]

    def wire_requests(self) -> list[HttpRequestSpec]:
        """What the real client would have sent for each recorded call."""
        return [build_http_request(request) for request in self.requests]

    # -- ModelClient protocol ------------------------------------------
    def chat(self, request: ModelRequest) -> ModelResponse:
        if self.guard is not None:
            self.guard(request)
        with self._lock:
            self.requests.append(request)
            item = self._next_item(request.usage_stage)
        return self._resolve(item, request)

    def _next_item(self, stage: str) -> ScriptItem:
        queue = self._by_stage.get(stage)
        if queue:
            return queue.popleft()
        if self._script:
            return self._script.popleft()
        if self._default is not None:
            return self._default
        raise AssertionError(f"FakeModelClient has no scripted reply for usage_stage={stage!r}")

    def _resolve(self, item: ScriptItem, request: ModelRequest) -> ModelResponse:
        if isinstance(item, ModelResponse):
            return item
        if isinstance(item, RawBody):
            return build_model_response(request, item.body, model=build_http_request(request).model)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, type) and issubclass(item, BaseException):
            raise item()
        if callable(item):
            return self._resolve(item(request), request)
        if isinstance(item, (dict, list)):
            item = json.dumps(item, ensure_ascii=False)
        content = str(item)
        body = {
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": dict(self._usage),
        }
        return build_model_response(request, body, model=build_http_request(request).model)
