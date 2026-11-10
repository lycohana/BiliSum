"""Normalize JSON and forced-SSE OpenAI-compatible responses.

The parsing lives in ``video_sum_infra.model_client.parse_response_body`` so the
pipeline, knowledge base and probes share one implementation; this keeps the
core-level entry point that raises ``VideoSumError``.
"""

from video_sum_infra.model_client import ModelResponseError, parse_response_body

from video_sum_core.errors import VideoSumError


def parse_llm_response_json(response) -> dict:
    try:
        return parse_response_body(response)
    except ModelResponseError as exc:
        raise VideoSumError(str(exc)) from exc
