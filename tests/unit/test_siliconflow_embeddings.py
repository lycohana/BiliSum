"""Tests for SiliconFlow embeddings integration."""
from unittest.mock import Mock, patch
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
        mock_response = Mock()
        mock_response.status_code = 401
        mock_client.return_value.__enter__.return_value.post.side_effect = Exception("HTTP error")

        with pytest.raises(HTTPException) as exc_info:
            service._embed_texts_siliconflow(["test"])

        assert exc_info.value.status_code == 500


def test_siliconflow_empty_response(mock_settings, mock_repository):
    """Test handling of empty API response."""
    service = KnowledgeIndexService(mock_repository, mock_settings)

    mock_response = Mock()
    mock_response.json.return_value = {"data": []}

    with patch("httpx.Client") as mock_client:
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response

        with pytest.raises(HTTPException) as exc_info:
            service._embed_texts_siliconflow(["test"])

        assert exc_info.value.status_code == 500
        assert "返回数据为空" in exc_info.value.detail
