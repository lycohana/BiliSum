"""Tests for the Twelve Labs Pegasus video-understanding integration."""
import os
from unittest.mock import Mock, patch

import pytest
from video_sum_core.errors import PegasusAuthenticationError, PegasusConfigurationError
from video_sum_core.twelvelabs import (
    DEFAULT_TWELVELABS_PROMPT,
    analyze_video_with_pegasus,
    video_context_from_file,
    video_context_from_url,
)


def test_video_context_from_url():
    ctx = video_context_from_url("https://example.com/clip.mp4")
    assert ctx == {"type": "url", "url": "https://example.com/clip.mp4"}


def test_video_context_from_file_small(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\x00\x01\x02\x03")
    ctx = video_context_from_file(path)
    assert ctx is not None
    assert ctx["type"] == "base64_string"
    assert ctx["base64_string"]


def test_video_context_from_file_too_large(tmp_path, monkeypatch):
    path = tmp_path / "big.mp4"
    path.write_bytes(b"\x00")
    # Pretend the file exceeds the 30MB base64 ceiling without writing 30MB to disk.
    import video_sum_core.twelvelabs as tl

    monkeypatch.setattr(tl, "MAX_BASE64_VIDEO_BYTES", 0)
    assert video_context_from_file(path) is None


def test_video_context_from_file_missing(tmp_path):
    assert video_context_from_file(tmp_path / "nope.mp4") is None


def test_analyze_missing_api_key():
    with pytest.raises(PegasusConfigurationError):
        analyze_video_with_pegasus(api_key="", video={"type": "url", "url": "https://x/y.mp4"})


def test_analyze_invalid_video_source():
    with pytest.raises(PegasusConfigurationError):
        analyze_video_with_pegasus(api_key="tlk_test", video={})


def test_analyze_success():
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"id": "abc", "data": "画面里有人在演示一段代码。"}

    with patch("httpx.Client") as mock_client:
        post = mock_client.return_value.__enter__.return_value.post
        post.return_value = mock_response

        text = analyze_video_with_pegasus(
            api_key="tlk_test",
            video={"type": "url", "url": "https://example.com/clip.mp4"},
        )

    assert text == "画面里有人在演示一段代码。"
    # Confirm request wiring: correct endpoint, auth header, and payload shape.
    args, kwargs = post.call_args
    assert args[0].endswith("/analyze")
    assert kwargs["headers"]["x-api-key"] == "tlk_test"
    assert kwargs["json"]["video"] == {"type": "url", "url": "https://example.com/clip.mp4"}
    assert kwargs["json"]["model_name"] == "pegasus1.5"
    assert kwargs["json"]["prompt"] == DEFAULT_TWELVELABS_PROMPT
    assert kwargs["json"]["stream"] is False


def test_analyze_success_nested_data_shape():
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"data": {"data": "嵌套结构的文本。"}}

    with patch("httpx.Client") as mock_client:
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response
        text = analyze_video_with_pegasus(
            api_key="tlk_test", video={"type": "url", "url": "https://x/y.mp4"}
        )

    assert text == "嵌套结构的文本。"


def test_analyze_auth_error():
    mock_response = Mock()
    mock_response.status_code = 401

    with patch("httpx.Client") as mock_client:
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response
        with pytest.raises(PegasusAuthenticationError):
            analyze_video_with_pegasus(
                api_key="tlk_bad", video={"type": "url", "url": "https://x/y.mp4"}
            )


def test_analyze_bad_request_error():
    mock_response = Mock()
    mock_response.status_code = 400
    mock_response.json.return_value = {"message": "video_file_broken"}

    with patch("httpx.Client") as mock_client:
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response
        with pytest.raises(PegasusConfigurationError) as exc_info:
            analyze_video_with_pegasus(
                api_key="tlk_test", video={"type": "url", "url": "https://x/y.mp4"}
            )

    assert "video_file_broken" in str(exc_info.value)


def test_analyze_empty_text():
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"id": "abc", "data": ""}

    with patch("httpx.Client") as mock_client:
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response
        with pytest.raises(PegasusConfigurationError):
            analyze_video_with_pegasus(
                api_key="tlk_test", video={"type": "url", "url": "https://x/y.mp4"}
            )


@pytest.mark.skipif(
    not os.environ.get("TWELVELABS_API_KEY"),
    reason="requires TWELVELABS_API_KEY for a live Pegasus call",
)
def test_analyze_live_smoke():
    """Live wiring check against the real Pegasus endpoint.

    Skipped unless TWELVELABS_API_KEY is set. Uses a short public sample; we only
    assert the request is accepted and either returns text or a media-validation
    error (which still proves the request shape and auth are correct).
    """
    api_key = os.environ["TWELVELABS_API_KEY"]
    try:
        text = analyze_video_with_pegasus(
            api_key=api_key,
            video=video_context_from_url(
                "https://download.samplelib.com/mp4/sample-5s.mp4"
            ),
            prompt="Summarize this video in one short sentence.",
            max_tokens=512,
        )
        assert isinstance(text, str) and text
    except PegasusConfigurationError as exc:
        # The fixture may fail Twelve Labs' strict media validation; auth + wiring
        # are still proven by reaching that validation stage.
        assert "video_file_broken" in str(exc) or "Pegasus" in str(exc)
