"""Tests for SiliconFlow embeddings integration."""
from unittest.mock import Mock, patch
import httpx
import pytest
from fastapi import HTTPException

from video_sum_infra.config import ServiceSettings
from video_sum_service.knowledge.index_service import KnowledgeIndexService
from video_sum_service.repository import SqliteTaskRepository


@pytest.fixture
def mock_settings():
    """Create mock settings for SiliconFlow."""
    settings = Mock(spec=ServiceSettings)
    settings.knowledge_embedding_provider = "siliconflow"
    settings.knowledge_embedding_model = "BAAI/bge-large-zh-v1.5"
    settings.siliconflow_embedding_api_key = "sk-test-key"
    settings.siliconflow_embedding_base_url = "https://api.siliconflow.cn/v1"
    settings.siliconflow_embedding_model = "BAAI/bge-large-zh-v1.5"
    settings.runtime_channel = "base"
    return settings


@pytest.fixture
def mock_repository():
    """Create mock repository."""
    return Mock(spec=SqliteTaskRepository)


def test_siliconflow_embedder_initialization(mock_settings, mock_repository):
    """Test SiliconFlow embedder initialization."""
    service = KnowledgeIndexService(mock_repository, mock_settings)
    embedder = service._get_embedder()
    assert embedder == "siliconflow"


def test_siliconflow_embed_texts_success(mock_settings, mock_repository):
    """Test successful SiliconFlow API call."""
    service = KnowledgeIndexService(mock_repository, mock_settings)

    mock_response = Mock()
    mock_response.json.return_value = {
        "data": [
            {"index": 0, "embedding": [0.1, 0.2, 0.3]},
            {"index": 1, "embedding": [0.4, 0.5, 0.6]},
        ]
    }

    with patch("httpx.Client") as mock_client:
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response

        embeddings = service._embed_texts_siliconflow(["text1", "text2"])

        assert len(embeddings) == 2
        assert embeddings[0] == [0.1, 0.2, 0.3]
        assert embeddings[1] == [0.4, 0.5, 0.6]


def test_siliconflow_missing_api_key(mock_settings, mock_repository):
    """Test error when API key is missing."""
    mock_settings.siliconflow_embedding_api_key = ""
    service = KnowledgeIndexService(mock_repository, mock_settings)

    with pytest.raises(HTTPException) as exc_info:
        service._embed_texts_siliconflow(["test"])

    assert exc_info.value.status_code == 500
    assert "API Key 未配置" in exc_info.value.detail


def test_siliconflow_http_error(mock_settings, mock_repository):
    """Test handling of HTTP errors."""
    service = KnowledgeIndexService(mock_repository, mock_settings)

    with patch("httpx.Client") as mock_client:
        request = httpx.Request("POST", "https://api.siliconflow.cn/v1/embeddings")
        mock_response = httpx.Response(
            400,
            request=request,
            json={"error": {"message": "Input length 681 exceeds 512 tokens"}},
        )
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response

        with pytest.raises(HTTPException) as exc_info:
            service._embed_texts_siliconflow(["test"])

        assert exc_info.value.status_code == 502
        assert "Input length 681 exceeds 512 tokens" in exc_info.value.detail


def test_siliconflow_empty_response(mock_settings, mock_repository):
    """Test handling of empty API response."""
    service = KnowledgeIndexService(mock_repository, mock_settings)

    mock_response = Mock()
    mock_response.json.return_value = {"data": []}

    with patch("httpx.Client") as mock_client:
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response

        with pytest.raises(HTTPException) as exc_info:
            service._embed_texts_siliconflow(["test"])

        assert exc_info.value.status_code == 502
        assert "返回向量数量不匹配" in exc_info.value.detail


def test_siliconflow_shortens_bge_inputs_and_batches(mock_settings, mock_repository):
    service = KnowledgeIndexService(mock_repository, mock_settings)
    posted_batches: list[list[str]] = []

    def fake_post(*_args, **kwargs):
        batch = kwargs["json"]["input"]
        posted_batches.append(batch)
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "data": [
                {"index": index, "embedding": [float(index), 1.0]}
                for index, _text in enumerate(batch)
            ]
        }
        return response

    with patch("httpx.Client") as mock_client:
        mock_client.return_value.__enter__.return_value.post.side_effect = fake_post
        embeddings = service._embed_texts_siliconflow(["中" * 900, *[f"text-{index}" for index in range(16)]])

    assert len(posted_batches) == 2
    assert len(posted_batches[0]) == 16
    assert len(posted_batches[0][0]) == 400
    assert len(embeddings) == 17
