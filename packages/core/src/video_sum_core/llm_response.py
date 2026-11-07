"""Normalize JSON and forced-SSE OpenAI-compatible responses."""

import json

from video_sum_core.errors import VideoSumError


def parse_llm_response_json(response) -> dict:
    try:
        body = response.json()
    except ValueError:
        raw = response.text.lstrip("\ufeff").strip()
        # Some gateways append an SSE terminator to an otherwise normal JSON body.
        decoder = json.JSONDecoder()
        chunks = []
        if raw.startswith("{"):
            body, end = decoder.raw_decode(raw)
            if raw[end:].strip() not in {"", "data: [DONE]"}:
                # NDJSON gateways emit one chat-completion chunk per line.
                # Reject unrelated trailing objects instead of dropping data.
                try:
                    chunks = [json.loads(line) for line in raw.splitlines() if line.strip() and line.strip() != "[DONE]"]
                except ValueError as exc:
                    raise VideoSumError("LLM response contains unexpected data after JSON.") from exc
                if not all(isinstance(chunk, dict) and any(key in chunk for key in ("choices", "usage", "error")) for chunk in chunks):
                    raise VideoSumError("LLM response contains unexpected data after JSON.")
                body = _combine_chunks(chunks)
        else:
            # OpenAI chunks normally use one data line per event. Joining data
            # lines also supports the multiline form defined by SSE.
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
            body = _combine_chunks(chunks)
    if not isinstance(body, dict):
        raise VideoSumError("LLM response must be a JSON object.")
    return body


def _combine_chunks(chunks: list) -> dict:
    parts: list[str] = []
    usage: dict = {}
    for chunk in chunks:
        if not isinstance(chunk, dict):
            raise VideoSumError("LLM SSE chunk must be a JSON object.")
        if chunk.get("error"):
            raise VideoSumError(f"LLM stream error: {chunk['error']}")
        if isinstance(chunk.get("usage"), dict):
            usage.update(chunk["usage"])
        for choice in chunk.get("choices", []):
            if choice.get("index", 0) != 0:
                continue
            message = choice.get("delta") or choice.get("message") or {}
            content = message.get("content") or choice.get("text")
            if isinstance(content, str):
                parts.append(content)
    if not "".join(parts):
        raise VideoSumError("LLM returned a stream with no readable message content.")
    return {"choices": [{"message": {"role": "assistant", "content": "".join(parts)}}], "usage": usage}
