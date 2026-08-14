"""Tests for dependency management functions."""
from pathlib import Path
from unittest.mock import Mock

import pytest
from video_sum_infra.config import ServiceSettings
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
    monkeypatch.setattr(runtime_support.settings_manager, "_settings", mock_settings)

    result = runtime_support.check_package_dependencies("chromadb")

    assert "知识库" in result[0]


def test_check_package_dependencies_sentence_transformers(monkeypatch) -> None:
    """Test check_package_dependencies for sentence-transformers."""
    mock_settings = Mock()
    mock_settings.knowledge_embedding_provider = "local_huggingface"
    monkeypatch.setattr(runtime_support.settings_manager, "_settings", mock_settings)

    result = runtime_support.check_package_dependencies("sentence-transformers")

    assert len(result) > 0
    assert "知识库" in result[0]


def test_check_package_dependencies_modelscope(monkeypatch) -> None:
    """Test check_package_dependencies for modelscope."""
    mock_settings = Mock()
    mock_settings.knowledge_embedding_provider = "local_modelscope"
    mock_settings.funasr_hub = "ms"
    monkeypatch.setattr(runtime_support.settings_manager, "_settings", mock_settings)

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


# ---------------------------------------------------------------------------
# Directory-level knowledge dependency verification (issue #97)
# ---------------------------------------------------------------------------


def test_packages_missing_from_site_packages_detects_absent(tmp_path: Path) -> None:
    """Missing import dirs are reported as missing."""
    site_packages = tmp_path / "site-packages"
    site_packages.mkdir(parents=True)

    missing = runtime_support.packages_missing_from_site_packages(
        site_packages,
        ["chromadb", "sentence-transformers", "modelscope"],
    )

    assert missing == ["chromadb", "sentence-transformers", "modelscope"]


def test_packages_missing_from_site_packages_accepts_present(tmp_path: Path) -> None:
    """Existing import dirs are not reported as missing."""
    site_packages = tmp_path / "site-packages"
    site_packages.mkdir(parents=True)
    (site_packages / "chromadb").mkdir()
    (site_packages / "sentence_transformers").mkdir()

    missing = runtime_support.packages_missing_from_site_packages(
        site_packages,
        ["chromadb", "sentence-transformers"],
    )

    assert missing == []


def test_packages_missing_from_site_packages_ignores_unknown_mapping(tmp_path: Path) -> None:
    """Packages without a known dir mapping never block the install."""
    site_packages = tmp_path / "site-packages"
    site_packages.mkdir(parents=True)

    missing = runtime_support.packages_missing_from_site_packages(
        site_packages, ["some-unknown-pkg"]
    )

    assert missing == []


def test_runtime_subprocess_env_disables_user_site(monkeypatch, tmp_path: Path) -> None:
    """Runtime subprocesses must not see macOS user site-packages (issue #97)."""
    monkeypatch.setattr(
        runtime_support.settings_manager,
        "_settings",
        ServiceSettings(cache_dir=tmp_path, hf_endpoint=""),
    )
    monkeypatch.setattr(runtime_support, "runtime_library_dirs", lambda channel: [])
    monkeypatch.setattr(runtime_support, "runtime_pythonpath_dirs", lambda channel: [])
    monkeypatch.setattr(runtime_support, "ffmpeg_location", lambda: None)
    monkeypatch.setattr(runtime_support, "is_frozen", lambda: False)

    env = runtime_support.runtime_subprocess_env("base")

    assert env.get("PYTHONNOUSERSITE") == "1"


def test_pip_install_with_fallbacks_supports_install_target(monkeypatch, tmp_path: Path) -> None:
    """--target installs must be forwarded to the pip command."""
    install_target = tmp_path / "runtime" / "base" / "lib" / "python3.12" / "site-packages"
    captured: list[list[str]] = []
    monkeypatch.setattr(
        runtime_support, "runtime_site_packages_dir", lambda channel: tmp_path / "sp"
    )

    def fake_runner(command, runtime_channel, timeout=1800):
        captured.append(command)
        return Mock(returncode=0, stdout="ok", stderr="")

    runtime_support.pip_install_with_fallbacks(
        tmp_path / "python",
        "base",
        ["chromadb>=1.0.0"],
        package_label="知识库依赖",
        runner=fake_runner,
        install_target=install_target,
    )

    assert captured
    assert "--target" in captured[0]
    assert str(install_target) in captured[0]
    assert "chromadb>=1.0.0" in captured[0]


def test_install_knowledge_dependencies_does_not_trust_probe_alone(
    monkeypatch, tmp_path: Path
) -> None:
    """When the probe reports ready but the package is absent from the runtime
    site-packages, the install must proceed and force the package into the
    directory the index service actually imports from (issue #97)."""
    runtime_dir = tmp_path / "runtime" / "base"
    runtime_dir.mkdir(parents=True)
    python_exe = runtime_dir / "python.exe"
    python_exe.write_text("python", encoding="utf-8")
    site_packages = runtime_dir / "lib" / "python3.12" / "site-packages"
    site_packages.mkdir(parents=True)

    ready_env = {
        "runtimeChannel": "base",
        "runtimeReady": True,
        "chromadbInstalled": True,
        "chromadbVersion": "",
        "chromadbBroken": False,
        "chromadbError": "",
        "sentenceTransformersInstalled": True,
        "sentenceTransformersVersion": "",
        "sentenceTransformersBroken": False,
        "sentenceTransformersError": "",
        "modelscopeInstalled": False,
        "modelscopeVersion": "",
        "modelscopeBroken": False,
        "modelscopeError": "",
        "knowledgeDependenciesReady": True,
        "knowledgeDependenciesError": "",
    }

    monkeypatch.setattr(
        runtime_support.settings_manager,
        "_settings",
        ServiceSettings(
            knowledge_embedding_provider="local_huggingface",
            runtime_channel="base",
            cache_dir=tmp_path / "cache",
            hf_endpoint="",
        ),
    )
    monkeypatch.setattr(runtime_support, "uses_current_service_python", lambda channel: False)
    monkeypatch.setattr(runtime_support, "ensure_runtime_channel", lambda channel: runtime_dir)
    monkeypatch.setattr(runtime_support, "runtime_python_executable", lambda channel: python_exe)
    monkeypatch.setattr(runtime_support, "runtime_site_packages_dir", lambda channel: site_packages)
    monkeypatch.setattr(runtime_support, "detect_environment", lambda channel: dict(ready_env))
    monkeypatch.setattr(
        runtime_support,
        "apply_knowledge_dependency_policy",
        lambda env, provider=None: env,
    )
    # First probe-time check and post-install check report chromadb missing from
    # the runtime site-packages; the --target fallback then puts it there.
    monkeypatch.setattr(
        runtime_support,
        "packages_missing_from_site_packages",
        lambda sp, packages: ["chromadb"]
        if not (sp / "chromadb").is_dir()
        else [],
    )
    monkeypatch.setattr(
        runtime_support,
        "install_workspace_packages",
        lambda python, runtime_channel=None: None,
    )
    monkeypatch.setattr(runtime_support, "ensure_runtime_pip", lambda python, runtime_channel: None)
    monkeypatch.setattr(runtime_support, "build_worker", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime_support, "write_runtime_metadata", lambda channel, payload: None)
    monkeypatch.setattr(runtime_support, "clear_environment_probe_cache", lambda channel: None)
    monkeypatch.setattr(runtime_support, "activate_runtime_pythonpath", lambda channel: None)

    pip_calls: list[dict[str, object]] = []

    def fake_pip_install(*args, **kwargs):
        pip_calls.append({"args": list(args), **kwargs})
        if kwargs.get("install_target") is not None:
            # Simulate the --target install actually placing the package.
            (kwargs["install_target"] / "chromadb").mkdir(exist_ok=True)
        return Mock(returncode=0, stdout="ok", stderr="", args=[])

    monkeypatch.setattr(runtime_support, "pip_install_with_fallbacks", fake_pip_install)

    repository = Mock()
    result, worker = runtime_support.install_knowledge_dependencies(
        reinstall=False, repository=repository
    )

    # Install must NOT early-return just because the probe said "ready".
    assert len(pip_calls) == 2
    assert pip_calls[0].get("install_target") is None  # regular pip install first
    assert pip_calls[1].get("install_target") == site_packages  # --target fallback
    assert result["installed"] is True
    assert worker is None


