from __future__ import annotations

import json

import httpx
import pytest
from video_sum_infra.model_client import (
    ModelClient,
    ModelRequest,
    ModelResponseError,
    ReasoningOptions,
    build_http_request,
)
from video_sum_infra.model_client_fake import FakeModelClient, RawBody


def _request(**overrides: object) -> ModelRequest:
    values: dict[str, object] = {
        "usage_stage": "summary_full",
        "base_url": "https://llm.example.com/v1",
        "api_key": "k",
        "payload": {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        "provider": "openai-compatible",
    }
    values.update(overrides)
    return ModelRequest(**values)  # type: ignore[arg-type]


def _client(handler) -> tuple[ModelClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return ModelClient(transport=httpx.MockTransport(record)), seen


def _chat_body(content: str, usage: dict[str, object] | None = None) -> dict[str, object]:
    return {"choices": [{"message": {"content": content}}], "usage": usage or {}}


def test_openai_request_returns_content_and_normalized_usage() -> None:
    client, seen = _client(
        lambda _r: httpx.Response(
            200, json=_chat_body("hello", {"prompt_tokens": 3, "completion_tokens": 2})
        )
    )

    response = client.chat(_request())

    assert seen[0].url == "https://llm.example.com/v1/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer k"
    assert response.content == "hello"
    assert response.usage_record == {
        "stage": "summary_full",
        "provider": "openai-compatible",
        "model": "m",
        "prompt_tokens": 3,
        "completion_tokens": 2,
        "total_tokens": 5,
        "cache_hit_tokens": None,
        "cache_miss_tokens": None,
        "cache_creation_tokens": None,
    }


def test_anthropic_request_converts_system_and_image_blocks() -> None:
    spec = build_http_request(
        _request(
            wire_format="anthropic",
            base_url="https://api.anthropic.com/v1",
            payload={
                "model": " claude ",
                "messages": [
                    {"role": "system", "content": "sys"},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "看图"},
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/jpeg;base64,QUJD"},
                            },
                        ],
                    },
                ],
            },
        )
    )

    assert spec.url == "https://api.anthropic.com/v1/messages"
    assert spec.headers["x-api-key"] == "k"
    assert spec.json["model"] == "claude"
    assert spec.json["system"] == "sys"
    assert spec.json["messages"][0]["content"][1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": "QUJD"},
    }


def test_reasoning_controls_apply_only_to_supported_models() -> None:
    reasoning = ReasoningOptions(thinking_type="disabled", reasoning_effort="high", provider="mimo")
    mimo = build_http_request(
        _request(payload={"model": "MiMo-X", "messages": []}, reasoning=reasoning)
    )
    other = build_http_request(
        _request(
            reasoning=ReasoningOptions(
                thinking_type="disabled", reasoning_effort="high", provider="openai"
            )
        )
    )

    assert mimo.json["model"] == "mimo-x"
    assert mimo.json["thinking"] == {"type": "disabled"}
    assert mimo.json["reasoning_effort"] == "high"
    assert "thinking" not in other.json


def test_endpoint_url_override_is_used_verbatim() -> None:
    spec = build_http_request(_request(endpoint_url="https://vlm.example.com/v1/chat/completions"))
    assert spec.url == "https://vlm.example.com/v1/chat/completions"


def test_transport_errors_retry_then_reraise_original(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _s: None)

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    client, seen = _client(fail)

    with pytest.raises(httpx.ConnectError):
        client.chat(_request(transport_retries=2))
    assert len(seen) == 3


def test_transport_retry_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _s: None)
    attempts = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json=_chat_body("ok"))

    client, _seen = _client(flaky)
    assert client.chat(_request(transport_retries=1)).content == "ok"


def test_http_status_errors_raise_with_response() -> None:
    client, _seen = _client(lambda _r: httpx.Response(401, json={"error": {"message": "bad key"}}))

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        client.chat(_request(transport_retries=3))
    assert excinfo.value.response.status_code == 401


def test_lenient_body_reads_sse_gateway_and_strict_body_does_not() -> None:
    sse = (
        'data: {"choices":[{"delta":{"content":"he"}}]}\n'
        'data: {"choices":[{"delta":{"content":"llo"}}]}\n'
        "data: [DONE]\n"
    )
    client, _seen = _client(lambda _r: httpx.Response(200, text=sse))

    assert client.chat(_request()).content == "hello"
    with pytest.raises(ValueError):
        client.chat(_request(lenient_body=False))


def test_lenient_body_empty_raises_model_response_error() -> None:
    client, _seen = _client(lambda _r: httpx.Response(200, text=""))
    with pytest.raises(ModelResponseError, match="no readable message content"):
        client.chat(_request())


def test_fake_records_requests_and_follows_script_per_stage() -> None:
    fake = FakeModelClient(
        ["shared"],
        by_stage={"mindmap": [{"a": 1}, ValueError("boom")]},
        usage={"total_tokens": 4},
    )

    assert fake.chat(_request(usage_stage="mindmap")).content == json.dumps({"a": 1})
    with pytest.raises(ValueError, match="boom"):
        fake.chat(_request(usage_stage="mindmap"))
    shared = fake.chat(_request(usage_stage="summary_full"))
    raw = FakeModelClient([RawBody({"content": [{"type": "text", "text": "anthropic"}]})]).chat(
        _request()
    )

    assert shared.content == "shared"
    assert shared.usage_record is not None and shared.usage_record["total_tokens"] == 4
    assert raw.content == "anthropic"
    assert fake.stages == ["mindmap", "mindmap", "summary_full"]
    with pytest.raises(AssertionError, match="no scripted reply"):
        fake.chat(_request())


def test_fake_guard_runs_before_recording() -> None:
    def guard(request: ModelRequest) -> None:
        raise RuntimeError(f"bypassed router: {request.usage_stage}")

    fake = FakeModelClient(default="x", guard=guard)
    with pytest.raises(RuntimeError, match="bypassed router"):
        fake.chat(_request())
    assert fake.requests == []
