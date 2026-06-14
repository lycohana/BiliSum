"""Tests for dependency management functions."""
from pathlib import Path
from unittest.mock import Mock

import pytest

from video_sum_service import runtime_support


def test_get_knowledge_requirements_local_huggingface() -> None:
    """Test get_knowledge_requirements for local HuggingFace provider."""
    result = runtime_support.get_knowledge_requirements("local_huggingface")

    assert "required" in result
    assert "preinstalled" in result
    assert "chromadb" in result["required"]
    assert "sentence-transformers" in result["required"]
    assert len(result["preinstalled"]) == 0


def test_get_knowledge_requirements_local_modelscope() -> None:
    """Test get_knowledge_requirements for local ModelScope provider."""
    result = runtime_support.get_knowledge_requirements("local_modelscope")

    assert "required" in result
    assert "preinstalled" in result
    assert "chromadb" in result["required"]
    assert "sentence-transformers" in result["required"]
    assert "modelscope" in result["required"]
    assert len(result["preinstalled"]) == 0


def test_get_knowledge_requirements_siliconflow() -> None:
    """Test get_knowledge_requirements for SiliconFlow provider."""
    result = runtime_support.get_knowledge_requirements("siliconflow")

    assert "required" in result
    assert "preinstalled" in result
    # SiliconFlow only needs chromadb for index storage
    assert "chromadb" in result["required"]
    assert "sentence-transformers" not in result["required"]
    assert "modelscope" not in result["required"]
    assert len(result["preinstalled"]) == 0


def test_check_package_dependencies_chromadb(monkeypatch) -> None:
    """Test check_package_dependencies for chromadb when knowledge is enabled."""
    mock_settings = Mock()
    mock_settings.knowledge_enabled = True
    runtime_support.settings_manager._settings = mock_settings

    result = runtime_support.check_package_dependencies("chromadb")

    assert "知识库" in result[0]


def test_check_package_dependencies_sentence_transformers(monkeypatch) -> None:
    """Test check_package_dependencies for sentence-transformers."""
    mock_settings = Mock()
    mock_settings.knowledge_embedding_provider = "local_huggingface"
    runtime_support.settings_manager._settings = mock_settings

    result = runtime_support.check_package_dependencies("sentence-transformers")

    assert len(result) > 0
    assert "知识库" in result[0]


def test_check_package_dependencies_modelscope(monkeypatch) -> None:
    """Test check_package_dependencies for modelscope."""
    mock_settings = Mock()
    mock_settings.knowledge_embedding_provider = "local_modelscope"
    mock_settings.funasr_hub = "ms"
    runtime_support.settings_manager._settings = mock_settings

    result = runtime_support.check_package_dependencies("modelscope")

    assert len(result) == 2
    assert any("知识库" in dep for dep in result)
    assert any("FunASR" in dep for dep in result)


def test_uninstall_packages_basic(monkeypatch, tmp_path: Path) -> None:
    """Test uninstall_packages removes specified packages."""
    runtime_root = tmp_path / "runtime"
    base_dir = runtime_root / "base"
    base_dir.mkdir(parents=True)
    (base_dir / "python.exe").write_text("python", encoding="utf-8")

    monkeypatch.setattr(runtime_support, "managed_runtime_dir", lambda channel: base_dir)
    monkeypatch.setattr(
        runtime_support,
        "runtime_python_executable",
        lambda channel: base_dir / "python.exe",
    )
    monkeypatch.setattr(
        runtime_support,
        "uses_current_service_python",
        lambda channel: False,
    )

    uninstall_commands = []

    def mock_run_command(command, runtime_channel, timeout=300):
        uninstall_commands.append(command)
        return Mock(returncode=0, stdout="Successfully uninstalled chromadb\n")

    monkeypatch.setattr(runtime_support, "run_command", mock_run_command)
    monkeypatch.setattr(runtime_support, "clear_environment_probe_cache", lambda channel: None)

    result = runtime_support.uninstall_packages(["chromadb"], "base")

    assert result["success"] is True
    assert len(uninstall_commands) == 1
    assert "pip" in uninstall_commands[0]
    assert "uninstall" in uninstall_commands[0]
    assert "chromadb" in uninstall_commands[0]
    assert "-y" in uninstall_commands[0]  # non-interactive


def test_uninstall_packages_multiple(monkeypatch, tmp_path: Path) -> None:
    """Test uninstall_packages with multiple packages."""
    runtime_root = tmp_path / "runtime"
    base_dir = runtime_root / "base"
    base_dir.mkdir(parents=True)
    (base_dir / "python.exe").write_text("python", encoding="utf-8")

    monkeypatch.setattr(runtime_support, "managed_runtime_dir", lambda channel: base_dir)
    monkeypatch.setattr(
        runtime_support,
        "runtime_python_executable",
        lambda channel: base_dir / "python.exe",
    )
    monkeypatch.setattr(
        runtime_support,
        "uses_current_service_python",
        lambda channel: False,
    )

    uninstall_commands = []

    def mock_run_command(command, runtime_channel, timeout=300):
        uninstall_commands.append(command)
        return Mock(returncode=0, stdout="Successfully uninstalled packages\n")

    monkeypatch.setattr(runtime_support, "run_command", mock_run_command)
    monkeypatch.setattr(runtime_support, "clear_environment_probe_cache", lambda channel: None)

    result = runtime_support.uninstall_packages(["chromadb", "sentence-transformers"], "base")

    assert result["success"] is True
    assert len(uninstall_commands) == 1
    assert "chromadb" in uninstall_commands[0]
    assert "sentence-transformers" in uninstall_commands[0]


def test_normalize_runtime_channel_basic() -> None:
    """Test normalize_runtime_channel with valid inputs."""
    assert runtime_support.normalize_runtime_channel("base") == "base"
    assert runtime_support.normalize_runtime_channel("gpu-cu128") == "gpu-cu128"
    assert runtime_support.normalize_runtime_channel("gpu-cu126") == "gpu-cu126"
    assert runtime_support.normalize_runtime_channel("gpu-cu124") == "gpu-cu124"


def test_normalize_runtime_channel_whitespace() -> None:
    """Test normalize_runtime_channel strips whitespace."""
    assert runtime_support.normalize_runtime_channel("  base  ") == "base"
    assert runtime_support.normalize_runtime_channel("\tgpu-cu128\n") == "gpu-cu128"


def test_normalize_runtime_channel_unknown_gpu() -> None:
    """Test normalize_runtime_channel with allow_unknown_gpu."""
    # Should raise without allow_unknown_gpu
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        runtime_support.normalize_runtime_channel("gpu-cu999")
    assert exc_info.value.status_code == 400

    # Should succeed with allow_unknown_gpu
    result = runtime_support.normalize_runtime_channel("gpu-cu999", allow_unknown_gpu=True)
    assert result == "gpu-cu999"


def test_sanitize_for_log() -> None:
    """Test _sanitize_for_log prevents log injection."""
    # Newlines and carriage returns should be escaped
    result = runtime_support._sanitize_for_log("test\ninjection\rhere")
    assert "\n" not in result
    assert "\r" not in result
    assert "\\n" in result
    assert "\\r" in result


