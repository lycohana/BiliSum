"""Unit tests for _parse_llm_response_json SSE tolerance."""

import json

import httpx
import pytest

from video_sum_core.pipeline.real import _parse_llm_response_json


def _fake_response(text: str) -> httpx.Response:
    return httpx.Response(200, text=text, request=httpx.Request("POST", "http://test"))


def test_plain_json_passthrough():
    body = {"choices": [{"message": {"role": "assistant", "content": '{"ok":true}'}}], "usage": {"total_tokens": 5}}
    resp = _fake_response(json.dumps(body))
    assert _parse_llm_response_json(resp) == body


def test_plain_json_with_done_tail():
    """Some gateways append data: [DONE] to a plain JSON body (single object)."""
    body = {"choices": [{"message": {"role": "assistant", "content": '{"ok":true}'}}], "usage": {"total_tokens": 5}}
    text = json.dumps(body) + "\ndata: [DONE]\n"
    parsed = _parse_llm_response_json(_fake_response(text))
    assert parsed["choices"][0]["message"]["content"] == '{"ok":true}'
    assert parsed["usage"]["total_tokens"] == 5


def test_sse_chunks_concatenated():
    chunks = [
        {"choices": [{"delta": {"content": '{"ok"'}}]},
        {"choices": [{"delta": {"content": ":true}"}}]},
        {"choices": [{"delta": {}}], "usage": {"total_tokens": 7}},
    ]
    text = "".join(f"data: {json.dumps(c)}\n" for c in chunks) + "data: [DONE]\n"
    parsed = _parse_llm_response_json(_fake_response(text))
    assert parsed["choices"][0]["message"]["content"] == '{"ok":true}'
    assert parsed["usage"]["total_tokens"] == 7


def test_sse_with_reasoning_only_chunks():
    chunks = [
        {"choices": [{"delta": {"reasoning_content": "thinking..."}}]},
        {"choices": [{"delta": {"content": '{"ok":true}'}}]},
    ]
    text = "".join(f"data: {json.dumps(c)}\n" for c in chunks) + "data: [DONE]\n"
    parsed = _parse_llm_response_json(_fake_response(text))
    assert parsed["choices"][0]["message"]["content"] == '{"ok":true}'


def test_empty_body_raises():
    with pytest.raises(Exception):
        _parse_llm_response_json(_fake_response(""))


def test_sse_no_content_raises():
    text = "data: {\"choices\":[{\"delta\":{\"reasoning_content\":\"x\"}}]}\ndata: [DONE]\n"
    with pytest.raises(Exception):
        _parse_llm_response_json(_fake_response(text))
