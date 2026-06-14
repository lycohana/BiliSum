from __future__ import annotations

from email.parser import Parser
import importlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace

import time
import venv
from dataclasses import fields
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import HTTPException
from video_sum_infra.config import (
    DEFAULT_KNOWLEDGE_NOTE_SYSTEM_PROMPT,
    DEFAULT_KNOWLEDGE_NOTE_USER_PROMPT_TEMPLATE,
    DEFAULT_SUMMARY_SYSTEM_PROMPT,
    DEFAULT_SUMMARY_USER_PROMPT_TEMPLATE,
    DEFAULT_VISUAL_FRAME_PLANNING_PROMPT,
    DEFAULT_VISUAL_NOTE_SYSTEM_PROMPT,
    DEFAULT_VISUAL_NOTE_USER_PROMPT_TEMPLATE,
    DEFAULT_VISUAL_VLM_PROMPT,
    ServiceSettings,
)
from video_sum_infra.runtime import (
    _move_preserved_runtime_extensions,
    activate_runtime_pythonpath,
    bootstrap_managed_runtime,
    ffmpeg_location,
    is_frozen,
    managed_runtime_dir,
    managed_runtime_root,
    prepend_runtime_path,
    read_runtime_metadata,
    repo_root,
    runtime_library_dirs,
    runtime_python_candidates,
    runtime_python_executable,
    runtime_pythonpath_dirs,
    runtime_site_packages_dir,
    sanitized_subprocess_dll_search,
    write_runtime_metadata,
)

from video_sum_service.context import logger, settings_manager
from video_sum_service.repository import SqliteTaskRepository
from video_sum_service.settings_manager import SettingsUpdatePayload, is_blank_or_masked_secret

if TYPE_CHECKING:
    from video_sum_service.worker import TaskWorker

_environment_probe_cache: dict[str, dict[str, object]] = {}
_environment_probe_failures: dict[str, str] = {}
_ENVIRONMENT_PROBE_CACHE_FILE = "environment-probe-cache.json"
_INSPECT_CHANNELS_CACHE: dict[str, object] | None = None
_INSPECT_CHANNELS_CACHED_AT: float = 0.0
_INSPECT_CHANNELS_CACHE_TTL: float = 5.0  # seconds
_PIP_INDEX_CANDIDATES: tuple[tuple[str, str | None], ...] = (
    ("official", None),
    ("tsinghua", "https://pypi.tuna.tsinghua.edu.cn/simple"),
    ("aliyun", "https://mirrors.aliyun.com/pypi/simple"),
)
_KNOWN_RUNTIME_CHANNELS: tuple[str, ...] = ("base", "gpu-cu128", "gpu-cu126", "gpu-cu124")
_RUNTIME_EXTENSION_PACKAGE_KEYS: set[str] = {
    "torch",
    "torchvision",
    "torchaudio",
    "nvidia",
    "triton",
    "faster_whisper",
    "ctranslate2",
    "onnxruntime",
    "tokenizers",
    "huggingface_hub",
    "hf_xet",
    "chromadb",
    "sentence_transformers",
    "funasr",
    "modelscope",
}
_RUNTIME_ROOT_APP_DIRS: frozenset[str] = frozenset({"Lib", "lib", "Scripts", "bin", "DLLs", "stdlib"})
_RUNTIME_ROOT_APP_FILES: frozenset[str] = frozenset({"pythonpath.pth"})
_RUNTIME_ROOT_STALE_FILES: frozenset[str] = frozenset({"pyvenv.cfg"})
_RUNTIME_REQUIREMENT_NAME_PATTERN = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9_.-]*)")
_RUNTIME_CHANNEL_PATTERN = re.compile(r"^(base|gpu-cu\d+)$")
_TORCH_FAMILY_PACKAGES: tuple[str, ...] = ("torch", "torchvision", "torchaudio")


def _knowledge_dependency_probe_fields(package: str) -> tuple[str, str, str]:
    normalized = str(package or "").strip().lower()
    if normalized == "chromadb":
        return "chromadbInstalled", "chromadbBroken", "chromadbError"
    if normalized == "sentence-transformers":
        return "sentenceTransformersInstalled", "sentenceTransformersBroken", "sentenceTransformersError"
    if normalized == "modelscope":
        return "modelscopeInstalled", "modelscopeBroken", "modelscopeError"
    return "", "", ""


def apply_knowledge_dependency_policy(
    environment: dict[str, object],
    provider: str | None = None,
) -> dict[str, object]:
    """Adjust package probe results to the active embedding provider."""
    active_provider = str(
        provider or getattr(settings_manager.current, "knowledge_embedding_provider", "local_huggingface")
    ).strip().lower()
    requirements = get_knowledge_requirements(active_provider)
    required = [str(item) for item in requirements.get("required", [])]
    preinstalled = {str(item) for item in requirements.get("preinstalled", [])}
    missing: list[str] = []
    errors: list[str] = []

    for package in required:
        installed_field, broken_field, error_field = _knowledge_dependency_probe_fields(package)
        if not installed_field:
            continue
        if bool(environment.get(broken_field)):
            errors.append(str(environment.get(error_field) or f"{package} 已安装但导入失败。"))
            continue
        if not bool(environment.get(installed_field)) and package not in preinstalled:
            missing.append(package)

    environment["knowledgeDependenciesReady"] = not missing and not errors
    if errors:
        environment["knowledgeDependenciesError"] = "\n".join(errors)
    elif missing:
        environment["knowledgeDependenciesError"] = f"缺少依赖：{', '.join(missing)}"
    else:
        environment["knowledgeDependenciesError"] = ""
    environment["knowledgeRequiredPackages"] = required
    return environment


def _split_env_urls(raw_value: str | None) -> list[str]:
    if not raw_value:
        return []
    normalized = raw_value.replace("\r", "\n").replace(";", "\n").replace(",", "\n")
    return [item.strip() for item in normalized.splitlines() if item.strip()]


def _validate_index_url(url: str) -> bool:
    """验证 pip index URL 的安全性 - 防止命令注入"""
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
        # 只允许 HTTPS 协议，禁止 file://, ftp:// 等
        if parsed.scheme != "https":
            logger.warning("index URL must use HTTPS: %s", url)
            return False
        # 必须有有效的 netloc（域名）
        if not parsed.netloc:
            logger.warning("index URL missing domain: %s", url)
            return False
        return True
    except Exception as e:
        logger.warning("invalid index URL: %s, error: %s", url, e)
        return False


def _torch_index_candidates(cuda_variant: str) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = [("official", f"https://download.pytorch.org/whl/{cuda_variant}")]
    for index, url in enumerate(_split_env_urls(os.environ.get("VIDEO_SUM_TORCH_INDEX_URLS")), start=1):
        if _validate_index_url(url):
            candidates.append((f"custom-{index}", url))
        else:
            logger.warning("skipping invalid custom index URL: %s", url)
    return candidates


def _runtime_channel_cuda_variant(runtime_channel: str) -> str:
    normalized = normalize_runtime_channel(runtime_channel, allow_unknown_gpu=True)
    return normalized.removeprefix("gpu-") if normalized.startswith("gpu-") else ""


def normalize_runtime_channel(runtime_channel: str | None, *, allow_unknown_gpu: bool = False) -> str:
    normalized = str(runtime_channel or "base").strip().lower()
    if not normalized or normalized == "default":
        return "base"
    if normalized in _KNOWN_RUNTIME_CHANNELS:
        return normalized
    if allow_unknown_gpu and _RUNTIME_CHANNEL_PATTERN.fullmatch(normalized):
        return normalized
    raise HTTPException(status_code=400, detail="Unsupported runtime channel.")


def runtime_channel_is_discoverable(runtime_channel: str) -> bool:
    if runtime_channel.startswith("."):
        return False
    try:
        normalize_runtime_channel(runtime_channel, allow_unknown_gpu=True)
    except HTTPException:
        return False
    return True


def windows_hidden_subprocess_kwargs() -> dict[str, object]:
    if os.name != "nt":
        return {}

    kwargs: dict[str, object] = {}
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if creationflags:
        kwargs["creationflags"] = creationflags

    startupinfo_cls = getattr(subprocess, "STARTUPINFO", None)
    use_show_window = getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
    sw_hide = getattr(subprocess, "SW_HIDE", 0)
    if startupinfo_cls is not None:
        startupinfo = startupinfo_cls()
        startupinfo.dwFlags |= use_show_window
        startupinfo.wShowWindow = sw_hide
        kwargs["startupinfo"] = startupinfo

    return kwargs


def runtime_subprocess_env(runtime_channel: str) -> dict[str, str]:
    env = dict(os.environ)
    for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__"):
        env.pop(key, None)
    temp_dir = settings_manager.current.cache_dir / "runtime-temp" / runtime_channel
    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
        env["TMP"] = str(temp_dir)
        env["TEMP"] = str(temp_dir)
        env["TMPDIR"] = str(temp_dir)
    except OSError:
        logger.warning("failed to prepare runtime temp dir path=%s", temp_dir)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    # Propagate HuggingFace mirror to subprocess (funasr / sentence-transformers)
    hf_endpoint = (settings_manager.current.hf_endpoint or "").strip()
    if hf_endpoint:
        env["HF_ENDPOINT"] = hf_endpoint

    path_entries = [str(path) for path in runtime_library_dirs(runtime_channel)]
    pythonpath_entries = [str(path) for path in runtime_pythonpath_dirs(runtime_channel)]
    if pythonpath_entries:
        env["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)
    ffmpeg_exe = ffmpeg_location()
    if ffmpeg_exe is not None:
        path_entries.append(str(ffmpeg_exe.parent))

    current_path = env.get("PATH", "")
    inherited_entries: list[str] = []
    blocked_prefixes: list[Path] = []
    if is_frozen():
        blocked_prefixes.append(Path(sys.executable).resolve().parent)
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            blocked_prefixes.append(Path(meipass).resolve())

    for raw_entry in current_path.split(os.pathsep):
        entry = raw_entry.strip()
        if not entry:
            continue
        try:
            entry_path = Path(entry).resolve()
        except OSError:
            inherited_entries.append(entry)
            continue
        if any(str(entry_path).lower().startswith(str(prefix).lower()) for prefix in blocked_prefixes):
            continue
        inherited_entries.append(entry)

    merged: list[str] = []
    for entry in [*path_entries, *inherited_entries]:
        if entry and entry not in merged:
            merged.append(entry)
    env["PATH"] = os.pathsep.join(merged)
    return env


def run_command(command: list[str], runtime_channel: str, timeout: int = 3600) -> subprocess.CompletedProcess[str]:
    with sanitized_subprocess_dll_search():
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=True,
            env=runtime_subprocess_env(runtime_channel),
            cwd=managed_runtime_dir(runtime_channel),
            **windows_hidden_subprocess_kwargs(),
        )


def run_host_command(command: list[str], timeout: int = 3600) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__"):
        env.pop(key, None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    with sanitized_subprocess_dll_search():
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=True,
            env=env,
            cwd=repo_root(),
            **windows_hidden_subprocess_kwargs(),
        )


def _robust_rmtree(path: Path) -> None:
    """Remove a directory tree, retrying on Windows when files are locked."""
    if not path.exists():
        return

    def _on_error(func, p, exc_info):
        # WinError 145 "directory not empty" — wait and retry
        if isinstance(exc_info[1], OSError) and getattr(exc_info[1], "winerror", 0) == 145:
            time.sleep(0.5)
            try:
                func(p)
            except OSError:
                # Retry failed — let outer loop handle re-rmtree
                pass
        elif isinstance(exc_info[1], PermissionError):
            time.sleep(0.5)
            try:
                func(p)
            except OSError:
                pass
        else:
            raise

    max_retries = 3
    for attempt in range(max_retries):
        try:
            shutil.rmtree(str(path), onerror=_on_error if os.name == "nt" else None)
            return
        except OSError:
            if attempt + 1 == max_retries:
                # Last attempt: force with ignore_errors
                shutil.rmtree(str(path), ignore_errors=True)
            else:
                time.sleep(1.0)


# ---------------------------------------------------------------------------
# Per-channel mutual exclusion for runtime mutation operations
# ---------------------------------------------------------------------------

_runtime_channel_locks: dict[str, threading.Lock] = {}


def _acquire_channel_lock(runtime_channel: str, timeout: float = 0.5) -> threading.Lock | None:
    """Try to acquire the exclusive lock for *runtime_channel*.

    Returns the lock if acquired within *timeout* seconds, or ``None`` if
    another operation is currently in progress on this channel.
    """
    lock = _runtime_channel_locks.setdefault(runtime_channel, threading.Lock())
    if lock.acquire(timeout=timeout):
        return lock
    return None


def _release_channel_lock(lock: threading.Lock) -> None:
    lock.release()


def uses_current_service_python(runtime_channel: str) -> bool:
    return not is_frozen() and runtime_channel == "base"


# ---------------------------------------------------------------------------
# Install log streaming — real-time pip output visible to the UI
# ---------------------------------------------------------------------------

_install_log_dir = Path(tempfile.gettempdir()) / "bilisum_install_logs"
_install_sessions: dict[str, dict[str, object]] = {}


def _install_log_path(session_id: str) -> Path:
    return _install_log_dir / f"{session_id}.log"


def _ensure_install_log_dir() -> None:
    _install_log_dir.mkdir(parents=True, exist_ok=True)


def start_install_session(session_id: str, label: str) -> None:
    _ensure_install_log_dir()
    _install_sessions[session_id] = {
        "label": label,
        "started_at": __import__("time").time(),
        "done": False,
        "success": False,
        "progress": 0,
    }
    log = _install_log_path(session_id)
    log.write_text(f"[{label}] 开始安装...\n", encoding="utf-8")


def finish_install_session(session_id: str, success: bool) -> None:
    meta = _install_sessions.get(session_id)
    if meta is not None:
        meta["done"] = True
        meta["success"] = success
    log = _install_log_path(session_id)
    status = "完成" if success else "失败"
    with log.open("a", encoding="utf-8") as f:
        f.write(f"\n[{meta.get('label', '') if meta else ''}] 安装{status}。\n")


def append_install_log(session_id: str, line: str) -> None:
    log = _install_log_path(session_id)
    sanitized = line.rstrip("\n\r") + "\n"
    with log.open("a", encoding="utf-8") as f:
        f.write(sanitized)
    # Parse pip progress bar, e.g. " 45%|████     | 1.2G/2.7G [02:30<04:10, 11.0MB/s]"
    m = re.search(r"(\d{1,3})%\|", sanitized)
    if m:
        meta = _install_sessions.get(session_id)
        if meta is not None:
            meta["progress"] = int(m.group(1))


def read_install_log(session_id: str, tail_bytes: int = 8192) -> dict[str, object]:
    meta = _install_sessions.get(session_id, {})
    log = _install_log_path(session_id)

    # 验证路径安全性 - 防止路径遍历攻击
    try:
        resolved_log = log.resolve()
        resolved_dir = _install_log_dir.resolve()
        if not resolved_log.is_relative_to(resolved_dir):
            logger.warning("path traversal attempt detected: session_id=%s", session_id)
            return {
                "sessionId": session_id,
                "label": meta.get("label", ""),
                "done": meta.get("done", False),
                "success": meta.get("success", False),
                "progress": meta.get("progress", 0),
                "log": "",
            }
    except (ValueError, OSError) as e:
        logger.warning("invalid log path: session_id=%s, error=%s", session_id, e)
        return {
            "sessionId": session_id,
            "label": meta.get("label", ""),
            "done": meta.get("done", False),
            "success": meta.get("success", False),
            "progress": meta.get("progress", 0),
            "log": "",
        }

    content = ""
    if log.exists():
        raw = log.read_bytes()
        content = raw[-tail_bytes:].decode("utf-8", errors="replace")
    return {
        "sessionId": session_id,
        "label": meta.get("label", ""),
        "done": meta.get("done", False),
        "success": meta.get("success", False),
        "progress": meta.get("progress", 0),
        "log": content,
    }


class _StreamingRunner:
    """Callable runner that streams subprocess output to an install log.

    Stores a reference to the underlying :class:`subprocess.Popen` so that
    :meth:`cancel` can kill the subprocess if an exception occurs mid-install.
    """

    def __init__(self, session_id: str, *, env: dict[str, str] | None = None, cwd: Path | None = None):
        self.session_id = session_id
        self._env = env
        self._cwd = cwd
        self._proc: subprocess.Popen[str] | None = None

    def __call__(self, command: list[str], runtime_channel: str, timeout: int = 3600) -> subprocess.CompletedProcess[str]:
        env = self._env if self._env is not None else runtime_subprocess_env(runtime_channel)
        cwd = self._cwd if self._cwd is not None else managed_runtime_dir(runtime_channel)
        append_install_log(self.session_id, f"$ {' '.join(command)}\n")

        with sanitized_subprocess_dll_search():
            self._proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                cwd=cwd,
                **windows_hidden_subprocess_kwargs(),
            )

        stdout_lines: list[str] = []
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            stdout_lines.append(line)
            append_install_log(self.session_id, line)

        self._proc.wait(timeout=timeout)
        combined = "".join(stdout_lines)
        if self._proc.returncode != 0:
            raise subprocess.CalledProcessError(self._proc.returncode, command, output=combined, stderr="")
        return subprocess.CompletedProcess(command, self._proc.returncode, stdout=combined, stderr="")

    def cancel(self):
        """Kill the underlying subprocess, including its child process tree."""
        if self._proc is not None and self._proc.poll() is None:
            append_install_log(self.session_id, "\n[安装已取消]\n")
            if os.name == "nt":
                # On Windows, TerminateProcess does not kill children.
                # taskkill /T kills the entire process tree.
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(self._proc.pid)],
                        capture_output=True,
                        timeout=10,
                    )
                except subprocess.SubprocessError:
                    self._proc.kill()
            else:
                self._proc.kill()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


def _environment_probe_cache_path() -> Path:
    return settings_manager.current.cache_dir / _ENVIRONMENT_PROBE_CACHE_FILE


def _read_environment_probe_cache_file() -> dict[str, object]:
    path = _environment_probe_cache_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("ignore invalid environment probe cache path=%s error=%s", path, exc)
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_environment_probe_cache_file(payload: dict[str, object]) -> None:
    path = _environment_probe_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("failed to write environment probe cache path=%s error=%s", path, exc)


def _load_cached_environment_probe(runtime_channel: str) -> dict[str, object] | None:
    cached = _environment_probe_cache.get(runtime_channel)
    if cached is not None:
        if _cached_environment_probe_usable(runtime_channel, cached):
            return apply_knowledge_dependency_policy(dict(cached))
        _environment_probe_cache.pop(runtime_channel, None)
        return None

    cache_file = _read_environment_probe_cache_file()
    entry = cache_file.get(runtime_channel)
    if not isinstance(entry, dict):
        return None
    if str(entry.get("runtimeChannel") or runtime_channel) != runtime_channel:
        return None
    if not _cached_environment_probe_usable(runtime_channel, entry):
        return None
    normalized_entry = apply_knowledge_dependency_policy(dict(entry))
    _environment_probe_cache[runtime_channel] = dict(normalized_entry)
    return normalized_entry


def _cached_environment_probe_usable(runtime_channel: str, entry: dict[str, object]) -> bool:
    if str(entry.get("runtimeChannel") or runtime_channel) != runtime_channel:
        return False
    if uses_current_service_python(runtime_channel):
        return True
    python_executable = runtime_python_executable(runtime_channel)
    if not entry.get("runtimeReady"):
        return python_executable is None
    if python_executable is None:
        return False
    cached_python = str(entry.get("runtimePython") or "")
    if cached_python and not runtime_paths_match(Path(cached_python), python_executable):
        return False
    metadata = read_runtime_metadata(managed_runtime_dir(runtime_channel))
    if not runtime_probe_cache_metadata_matches(entry, metadata):
        return False
    cached_runtime_path = str(entry.get("runtimePath") or "")
    return not cached_runtime_path or runtime_paths_match(Path(cached_runtime_path), managed_runtime_dir(runtime_channel))


def _load_cached_environment_probe_for_runtime_status(
    runtime_channel: str,
    runtime_dir: Path,
    python_executable: Path | None,
    metadata: dict[str, object],
) -> dict[str, object] | None:
    entry = _load_cached_environment_probe(runtime_channel)
    if entry is None:
        return None
    cached_python = str(entry.get("runtimePython") or "")
    if cached_python and python_executable is not None:
        if not runtime_paths_match(Path(cached_python), python_executable):
            return None
    cached_app_version = str(entry.get("appVersion") or "")
    if cached_app_version and cached_app_version != str(metadata.get("appVersion") or ""):
        return None
    cached_python_version = str(entry.get("runtimePythonVersion") or entry.get("pythonVersion") or "")
    metadata_python_version = str(metadata.get("pythonVersion") or "")
    if cached_python_version and metadata_python_version and cached_python_version != metadata_python_version:
        return None
    cached_runtime_path = str(entry.get("runtimePath") or "")
    if cached_runtime_path:
        if not runtime_paths_match(Path(cached_runtime_path), runtime_dir):
            return None
    return entry


def runtime_probe_cache_metadata_matches(entry: dict[str, object], metadata: dict[str, object]) -> bool:
    if not metadata:
        return True
    for field in ("appVersion", "runtimeLayout", "pythonVersion"):
        cached_value = str(entry.get(field) or "")
        metadata_value = str(metadata.get(field) or "")
        if not cached_value:
            return False
        if metadata_value and cached_value != metadata_value:
            return False
    return True


def runtime_cache_metadata_fields(runtime_channel: str, runtime_dir: Path | None = None) -> dict[str, str]:
    if runtime_channel == "base" and uses_current_service_python(runtime_channel):
        return {}
    metadata = read_runtime_metadata(runtime_dir or managed_runtime_dir(runtime_channel))
    return {
        "runtimePath": str(runtime_dir or managed_runtime_dir(runtime_channel)),
        "appVersion": str(metadata.get("appVersion") or ""),
        "runtimeLayout": str(metadata.get("runtimeLayout") or ""),
        "pythonVersion": str(metadata.get("pythonVersion") or ""),
    }


def runtime_paths_match(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return str(left) == str(right)


def _store_cached_environment_probe(runtime_channel: str, payload: dict[str, object]) -> None:
    _environment_probe_cache[runtime_channel] = dict(payload)
    cache_file = _read_environment_probe_cache_file()
    cache_file[runtime_channel] = dict(payload)
    _write_environment_probe_cache_file(cache_file)


def command_error_detail(exc: subprocess.CalledProcessError, fallback: str) -> str:
    parts = [str(exc.stdout or "").strip(), str(exc.stderr or "").strip()]
    merged = "\n".join(part for part in parts if part).strip()
    if not merged:
        merged = str(exc)
    merged = merged[-1500:]
    return f"{fallback}\n\n{merged}".strip()


def pip_install_error_detail(
    package_label: str,
    attempts: list[tuple[str, subprocess.CalledProcessError]],
) -> str:
    if not attempts:
        return f"安装 {package_label} 失败。"

    last_label, last_exc = attempts[-1]
    combined = "\n".join(
        (str(exc.stdout or "").strip() + "\n" + str(exc.stderr or "").strip()).strip()
        for _, exc in attempts
    )
    normalized = combined.lower()
    hints = [
        f"安装 {package_label} 失败。已尝试官方 PyPI 与国内镜像（清华、阿里云）。",
    ]
    if "ssl" in normalized or "unexpected_eof_while_reading" in normalized:
        hints.append("检测到 SSL 握手异常，通常是当前网络到包索引的连接不稳定或被代理拦截。")
    if "no matching distribution found" in normalized and "from versions: none" in normalized:
        hints.append("当前输出更像是索引访问失败，而不是包本身不存在。")

    last_detail = command_error_detail(last_exc, f"最后一次尝试使用 {last_label} 源仍然失败。")
    return "\n\n".join([*hints, last_detail])


def _runner_install_session_id(runner: object) -> str | None:
    session_id = getattr(runner, "session_id", None)
    return str(session_id) if session_id else None


def _sanitize_for_log(value: object) -> str:
    """Sanitize user input for logging to prevent log injection attacks."""
    text = str(value)
    return text.replace("\r", "\\r").replace("\n", "\\n")


def cleanup_invalid_runtime_distributions(runtime_channel: str, *, session_id: str | None = None) -> list[str]:
    """Remove pip leftovers that appear as "Ignoring invalid distribution ~..."."""
    try:
        site_packages = runtime_site_packages_dir(runtime_channel)
    except Exception as exc:
        logger.debug(
            "skip invalid distribution cleanup runtime_channel=%s error=%s",
            _sanitize_for_log(runtime_channel),
            exc,
        )
        return []
    if not site_packages.exists():
        return []

    removed: list[str] = []
    for item in site_packages.iterdir():
        if not item.name.startswith("~"):
            continue
        try:
            if item.is_dir():
                _robust_rmtree(item)
            else:
                item.unlink(missing_ok=True)
            removed.append(item.name)
        except OSError as exc:
            logger.warning("failed to remove invalid distribution leftover path=%s error=%s", item, exc)

    if removed and session_id:
        append_install_log(session_id, f"[运行环境] 已清理 pip 残留分发目录：{', '.join(removed)}")
    return removed


def _probe_torch_family(
    python_executable: Path,
    runtime_channel: str,
    *,
    runner=run_command,
) -> dict[str, dict[str, object]]:
    script = r"""
import importlib
import importlib.metadata
import json

payload = {}
for name in ("torch", "torchvision", "torchaudio"):
    try:
        distribution_version = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        distribution_version = ""
    error = ""
    import_version = ""
    try:
        module = importlib.import_module(name)
        import_version = str(getattr(module, "__version__", "") or "")
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    payload[name] = {
        "distributionVersion": distribution_version,
        "version": import_version or distribution_version,
        "importable": not error,
        "error": error,
    }
print(json.dumps(payload, ensure_ascii=False))
"""
    try:
        result = runner(
            [str(python_executable), "-c", script],
            runtime_channel=runtime_channel,
            timeout=120,
        )
        lines = [line.strip() for line in str(result.stdout or "").splitlines() if line.strip()]
        return json.loads(lines[-1] if lines else "{}")
    except Exception as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        return {
            name: {
                "distributionVersion": "",
                "version": "",
                "importable": False,
                "error": detail[-1000:],
            }
            for name in _TORCH_FAMILY_PACKAGES
        }


def _torch_family_needs_cuda_repair(
    probe: dict[str, dict[str, object]],
    cuda_variant: str,
) -> tuple[bool, list[str]]:
    expected_suffix = f"+{cuda_variant}".lower()
    reasons: list[str] = []
    for package_name in _TORCH_FAMILY_PACKAGES:
        item = probe.get(package_name, {})
        version = str(item.get("version") or item.get("distributionVersion") or "")
        error = str(item.get("error") or "")
        if error:
            reasons.append(f"{package_name} 导入失败：{error[-240:]}")
            continue
        if not version:
            reasons.append(f"{package_name} 未安装")
            continue
        if expected_suffix not in version.lower():
            reasons.append(f"{package_name}={version} 不是 {cuda_variant} 版本")
    return bool(reasons), reasons


def ensure_torch_family_compatible(
    python_executable: Path,
    runtime_channel: str,
    *,
    package_label: str,
    runner=run_command,
    install_if_missing: bool = False,
) -> subprocess.CompletedProcess[str] | None:
    cuda_variant = _runtime_channel_cuda_variant(runtime_channel)
    if not cuda_variant:
        return None

    session_id = _runner_install_session_id(runner)
    cleanup_invalid_runtime_distributions(runtime_channel, session_id=session_id)
    probe = _probe_torch_family(python_executable, runtime_channel, runner=runner)
    needs_repair, reasons = _torch_family_needs_cuda_repair(probe, cuda_variant)
    if not needs_repair:
        return None
    any_torch_distribution = any(
        str(probe.get(package_name, {}).get("distributionVersion") or probe.get(package_name, {}).get("version") or "")
        for package_name in _TORCH_FAMILY_PACKAGES
    )
    if not any_torch_distribution and not install_if_missing:
        return None

    if session_id:
        append_install_log(
            session_id,
            "[PyTorch] 检测到 GPU 运行环境的 torch / torchvision / torchaudio 不一致，"
            f"将从 PyTorch {cuda_variant} 源重新安装三件套。\n"
            + "\n".join(f"  - {reason}" for reason in reasons),
        )
    logger.warning(
        "repairing torch family runtime_channel=%s package_label=%s reasons=%s",
        _sanitize_for_log(runtime_channel),
        _sanitize_for_log(package_label),
        _sanitize_for_log("; ".join(reasons)),
    )
    result = torch_install_with_fallbacks(
        python_executable,
        runtime_channel,
        cuda_variant,
        timeout=3600,
        runner=runner,
        reinstall=True,
    )
    cleanup_invalid_runtime_distributions(runtime_channel, session_id=session_id)
    repaired_probe = _probe_torch_family(python_executable, runtime_channel, runner=runner)
    still_broken, repair_reasons = _torch_family_needs_cuda_repair(repaired_probe, cuda_variant)
    if still_broken:
        detail = "\n".join(f"- {reason}" for reason in repair_reasons)
        if session_id:
            append_install_log(session_id, f"[PyTorch] 修复后检测仍未通过：\n{detail}")
        raise HTTPException(
            status_code=500,
            detail=f"{package_label} 安装后 PyTorch GPU 运行环境仍不一致：\n{detail}",
        )
    if session_id:
        append_install_log(session_id, "[PyTorch] GPU 运行环境三件套检测通过。")
    return result


def pip_install_with_fallbacks(
    python_executable: Path,
    runtime_channel: str,
    packages: list[str],
    *,
    package_label: str = "本地 ASR 依赖",
    reinstall: bool = False,
    timeout: int = 1800,
    runner=run_command,
) -> subprocess.CompletedProcess[str]:
    attempts: list[tuple[str, subprocess.CalledProcessError]] = []
    session_id = _runner_install_session_id(runner)
    cleanup_invalid_runtime_distributions(runtime_channel, session_id=session_id)

    for label, index_url in _PIP_INDEX_CANDIDATES:
        command = [
            str(python_executable),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--upgrade",
            "--upgrade-strategy",
            "only-if-needed",
        ]
        if reinstall:
            command.append("--force-reinstall")
        if index_url:
            command.extend(["--index-url", index_url])
        command.extend(packages)

        try:
            result = runner(command, runtime_channel=runtime_channel, timeout=timeout)
            cleanup_invalid_runtime_distributions(runtime_channel, session_id=session_id)
            repair_result = ensure_torch_family_compatible(
                python_executable,
                runtime_channel,
                package_label=package_label,
                runner=runner,
            )
            if repair_result is not None:
                return subprocess.CompletedProcess(
                    result.args,
                    result.returncode,
                    stdout="\n".join(
                        part
                        for part in [str(result.stdout or ""), str(repair_result.stdout or "")]
                        if part
                    ),
                    stderr="\n".join(
                        part
                        for part in [str(result.stderr or ""), str(repair_result.stderr or "")]
                        if part
                    ),
                )
            return result
        except subprocess.CalledProcessError as exc:
            attempts.append((label, exc))

    raise HTTPException(status_code=500, detail=pip_install_error_detail(package_label, attempts))


def _run_pip_install(
    python_executable: Path,
    runtime_channel: str,
    packages: list[str],
    *,
    package_label: str,
    reinstall: bool = False,
    timeout: int = 1800,
    runner=run_command,
) -> subprocess.CompletedProcess[str]:
    """Shared pip install wrapper for install_* functions.

    Calls ``pip_install_with_fallbacks`` with the given parameters and returns
    the completed process result.  Install functions use this to avoid
    duplicating the subprocess call + stdout collection pattern.
    """
    return pip_install_with_fallbacks(
        python_executable,
        runtime_channel,
        packages,
        package_label=package_label,
        reinstall=reinstall,
        timeout=timeout,
        runner=runner,
    )


def pip_index_options() -> list[dict[str, str]]:
    return [
        {"label": label, "url": index_url or "https://pypi.org/simple"}
        for label, index_url in _PIP_INDEX_CANDIDATES
    ]


def torch_install_with_fallbacks(
    python_executable: Path,
    runtime_channel: str,
    cuda_variant: str,
    *,
    timeout: int = 1800,
    runner=run_command,
    reinstall: bool = False,
) -> subprocess.CompletedProcess[str]:
    attempts: list[tuple[str, subprocess.CalledProcessError]] = []
    session_id = _runner_install_session_id(runner)
    cleanup_invalid_runtime_distributions(runtime_channel, session_id=session_id)

    for label, index_url in _torch_index_candidates(cuda_variant):
        command = [
            str(python_executable),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--upgrade",
            "--upgrade-strategy",
            "only-if-needed",
        ]
        if reinstall:
            command.append("--force-reinstall")
        command.extend([
            "torch",
            "torchvision",
            "torchaudio",
            "--index-url",
            index_url,
        ])
        try:
            result = runner(command, runtime_channel=runtime_channel, timeout=timeout)
            cleanup_invalid_runtime_distributions(runtime_channel, session_id=session_id)
            return result
        except subprocess.CalledProcessError as exc:
            attempts.append((label, exc))

    raise HTTPException(status_code=500, detail=pip_install_error_detail("CUDA 运行环境依赖", attempts))


def ensure_python_pip(python_executable: Path, runtime_channel: str, runner=run_command) -> None:
    pip_check_error: subprocess.CalledProcessError | None = None
    try:
        runner([str(python_executable), "-m", "pip", "--version"], runtime_channel=runtime_channel, timeout=120)
        return
    except subprocess.CalledProcessError as exc:
        pip_check_error = exc

    try:
        runner(
            [str(python_executable), "-m", "ensurepip", "--upgrade", "--default-pip"],
            runtime_channel=runtime_channel,
            timeout=300,
        )
    except subprocess.CalledProcessError as exc:
        original_detail = (
            command_error_detail(pip_check_error, "pip 启动失败。")
            if pip_check_error is not None
            else ""
        )
        repair_detail = command_error_detail(
            exc,
            "运行环境里的 pip 不可用，且 ensurepip 自动修复失败。"
            "这通常是旧版本运行时在应用更新后缺少 pip 工具链或 pip 已损坏，请先同步/刷新运行环境后重试。",
        )
        raise HTTPException(
            status_code=500,
            detail="\n\n".join(part for part in [repair_detail, original_detail] if part).strip(),
        ) from exc

    try:
        runner([str(python_executable), "-m", "pip", "--version"], runtime_channel=runtime_channel, timeout=120)
    except subprocess.CalledProcessError as exc:
        raise HTTPException(
            status_code=500,
            detail=command_error_detail(exc, "pip 修复完成后仍无法启动。"),
        ) from exc


def ensure_runtime_pip(python_executable: Path, runtime_channel: str) -> None:
    ensure_python_pip(python_executable, runtime_channel, runner=run_command)


def install_workspace_packages(python_executable: Path, runtime_channel: str) -> None:
    if is_frozen():
        return

    ensure_runtime_pip(python_executable, runtime_channel)
    root = repo_root()
    run_command(
        [
            str(python_executable),
            "-m",
            "pip",
            "install",
            "--upgrade",
            "pip",
            "setuptools",
            "wheel",
            "hatchling>=1.27.0",
        ],
        runtime_channel=runtime_channel,
        timeout=900,
    )
    command = [
        str(python_executable),
        "-m",
        "pip",
        "install",
        "--no-build-isolation",
    ]
    if runtime_channel != "base":
        command.append("--no-deps")
    command.extend(
        [
            str(root / "packages" / "infra"),
            str(root / "packages" / "core"),
            str(root / "apps" / "service"),
        ]
    )
    run_command(command, runtime_channel=runtime_channel, timeout=1800)


def create_source_runtime(runtime_channel: str) -> Path:
    runtime_channel = normalize_runtime_channel(runtime_channel, allow_unknown_gpu=True)
    runtime_dir = managed_runtime_dir(runtime_channel)
    venv.EnvBuilder(with_pip=True, clear=True).create(runtime_dir)
    python_executable = next((candidate for candidate in runtime_python_candidates(runtime_dir) if candidate.exists()), None)
    if python_executable is None:
        raise HTTPException(status_code=500, detail="Managed runtime creation failed: python.exe missing.")
    install_workspace_packages(python_executable, runtime_channel=runtime_channel)
    return runtime_dir


def _ensure_runtime_sitecustomize(runtime_channel: str) -> None:
    """Write ``sitecustomize.py`` to the managed runtime so that *every*
    subprocess (probe, pip, transcription worker) loads the
    ``os.add_dll_directory`` guard before any other import runs.

    Torch's module-level ``_load_dll_libraries`` calls
    ``os.add_dll_directory(torch/lib)`` which throws
    ``FileNotFoundError: WinError 206`` on portable CPython builds.
    Swallowing this error is safe — the directory is already on PATH.

    Path validation is performed via runtime_site_packages_dir() →
    managed_runtime_dir() which includes path traversal protection.
    """
    runtime_channel = normalize_runtime_channel(runtime_channel, allow_unknown_gpu=True)
    if not is_frozen() and uses_current_service_python(runtime_channel):
        # In dev mode with "base" channel, everything runs in the host
        # Python — no managed runtime subprocess, no portable Python issue.
        return
    site_packages = runtime_site_packages_dir(runtime_channel)
    site_packages.mkdir(parents=True, exist_ok=True)
    runtime_root = managed_runtime_root().resolve()
    resolved_site_packages = site_packages.resolve()
    try:
        resolved_site_packages.relative_to(runtime_root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid runtime channel path.") from exc
    target = resolved_site_packages / "sitecustomize.py"
    content = '''"""Auto-generated by BiliSum — do not edit.

Guards os.add_dll_directory against WinError 206 on portable CPython,
so that torch / funasr imports succeed in every subprocess.
"""
import os as _bs_os
_bs_orig = getattr(_bs_os, "add_dll_directory", None)
if _bs_orig is not None:
    def _bs_safe(path):
        try:
            return _bs_orig(path)
        except (FileNotFoundError, OSError):
            return None
    _bs_os.add_dll_directory = _bs_safe
    _bs_os.environ["BILISUM_SITECUSTOMIZE"] = "1"
'''
    if target.exists() and target.read_text(encoding="utf-8") == content:
        return
    target.write_text(content, encoding="utf-8")
    logger.info(
        "sitecustomize guard written runtime_channel=%s path=%s",
        _sanitize_for_log(runtime_channel),
        _sanitize_for_log(str(target)),
    )
    logger.info("installed sitecustomize guard runtime_channel=%s", _sanitize_for_log(runtime_channel))


def ensure_runtime_channel(runtime_channel: str) -> Path | None:
    runtime_channel = normalize_runtime_channel(runtime_channel, allow_unknown_gpu=True)
    if runtime_channel == "base":
        bootstrap_managed_runtime("base")
        python_executable = runtime_python_executable("base")
        if python_executable is not None:
            _ensure_runtime_sitecustomize("base")
            return managed_runtime_dir("base")
        if not is_frozen():
            _ensure_runtime_sitecustomize("base")
            return create_source_runtime("base")
        raise HTTPException(status_code=500, detail="Bundled base runtime is missing.")

    target_dir = managed_runtime_dir(runtime_channel)
    backup_dir = runtime_refresh_backup_dir(runtime_channel)
    restore_interrupted_runtime_refresh(target_dir, backup_dir)

    base_dir = ensure_runtime_channel("base")
    if base_dir is None or not base_dir.exists():
        raise HTTPException(status_code=500, detail="Base runtime is unavailable.")

    base_metadata = read_runtime_metadata(base_dir)
    target_metadata = read_runtime_metadata(target_dir)
    target_ready = runtime_python_executable(runtime_channel) is not None
    target_matches_base = runtime_metadata_matches_base(target_metadata, base_metadata)
    if target_ready and target_matches_base:
        _ensure_runtime_sitecustomize(runtime_channel)
        return target_dir

    if target_ready and target_dir.exists():
        run_runtime_refresh_with_backup(
            target_dir,
            backup_dir,
            lambda: sync_runtime_base(target_dir, base_dir, runtime_channel),
        )
        _ensure_runtime_sitecustomize(runtime_channel)
        return target_dir

    replace_runtime_with_base_copy(target_dir, base_dir, runtime_channel, backup_dir)
    _ensure_runtime_sitecustomize(runtime_channel)
    return target_dir


def runtime_refresh_backup_dir(runtime_channel: str) -> Path:
    runtime_channel = normalize_runtime_channel(runtime_channel, allow_unknown_gpu=True)
    return managed_runtime_dir(runtime_channel).parent / f".{runtime_channel}-refresh-backup"


def _assert_path_within_managed_runtime_root(path: Path, *, field_name: str = "path") -> Path:
    """Validate that a path is within the managed runtime root to prevent path traversal."""
    root = managed_runtime_root().resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}.") from exc
    return resolved


def restore_interrupted_runtime_refresh(runtime_dir: Path, backup_dir: Path) -> None:
    """Restore runtime from backup if interrupted refresh left it in a bad state.

    Path validation is performed by the caller via managed_runtime_dir().
    """
    if not backup_dir.exists():
        return
    if runtime_dir.exists():
        shutil.rmtree(runtime_dir)
    shutil.move(str(backup_dir), str(runtime_dir))


def run_runtime_refresh_with_backup(runtime_dir: Path, backup_dir: Path, refresh) -> None:
    """Run refresh operation with backup/restore safety net.

    Path validation is performed by the caller via managed_runtime_dir().
    """
    prepare_runtime_refresh_backup(runtime_dir, backup_dir)
    try:
        refresh()
    except Exception:
        if runtime_dir.exists():
            shutil.rmtree(runtime_dir)
        shutil.move(str(backup_dir), str(runtime_dir))
        raise
    else:
        shutil.rmtree(backup_dir, ignore_errors=True)


def prepare_runtime_refresh_backup(runtime_dir: Path, backup_dir: Path) -> None:
    """Create backup of runtime before refresh.

    Path validation is performed by the caller via managed_runtime_dir().
    """
    backup_temp_dir = backup_dir.parent / f".{backup_dir.name}-temp"
    if backup_dir.exists():
        _robust_rmtree(backup_dir)
    if backup_temp_dir.exists():
        _robust_rmtree(backup_temp_dir)
    try:
        shutil.copytree(runtime_dir, backup_temp_dir)
        shutil.move(str(backup_temp_dir), str(backup_dir))
    except Exception:
        if backup_temp_dir.exists():
            shutil.rmtree(backup_temp_dir, ignore_errors=True)
        raise


def replace_runtime_with_base_copy(
    target_dir: Path,
    base_dir: Path,
    runtime_channel: str,
    backup_dir: Path,
) -> None:
    """Replace target runtime with a fresh copy from base.

    Path validation is performed by the caller (ensure_runtime_channel)
    via managed_runtime_dir() which includes path traversal protection.
    """
    temp_dir = target_dir.parent / f".{runtime_channel}-refresh-temp"
    if temp_dir.exists():
        _robust_rmtree(temp_dir)
    if target_dir.exists():
        prepare_runtime_refresh_backup(target_dir, backup_dir)
    try:
        shutil.copytree(base_dir, temp_dir)
        if target_dir.exists():
            _robust_rmtree(target_dir)
        shutil.move(str(temp_dir), str(target_dir))
    except Exception:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        if backup_dir.exists():
            if target_dir.exists():
                _robust_rmtree(target_dir)
            shutil.move(str(backup_dir), str(target_dir))
        elif target_dir.exists() and runtime_python_executable(runtime_channel) is None:
            shutil.rmtree(target_dir, ignore_errors=True)
        raise
    else:
        if backup_dir.exists():
            # Restore user-installed extension packages (funasr, chromadb, etc.)
            # that would otherwise be silently lost when the GPU channel is
            # rebuilt from a clean base copy.
            _move_preserved_runtime_extensions(backup_dir, target_dir)
            shutil.rmtree(backup_dir, ignore_errors=True)


def inspect_runtime_channels() -> dict[str, object]:
    global _INSPECT_CHANNELS_CACHE, _INSPECT_CHANNELS_CACHED_AT
    # Return cached result if fresh — avoids repeated disk I/O on every UI poll
    now = time.time()
    if _INSPECT_CHANNELS_CACHE is not None and (now - _INSPECT_CHANNELS_CACHED_AT) < _INSPECT_CHANNELS_CACHE_TTL:
        return dict(_INSPECT_CHANNELS_CACHE)

    root = managed_runtime_root()
    discovered = set(_KNOWN_RUNTIME_CHANNELS)
    if root.exists():
        discovered.update(
            item.name
            for item in root.iterdir()
            if item.is_dir() and runtime_channel_is_discoverable(item.name)
        )

    base_dir = managed_runtime_dir("base")
    base_metadata = read_runtime_metadata(base_dir)
    base_app_version = str(base_metadata.get("appVersion") or "")
    base_layout = str(base_metadata.get("runtimeLayout") or "")
    base_python_version = str(base_metadata.get("pythonVersion") or "")
    channels: list[dict[str, object]] = []

    for runtime_channel in sorted(discovered, key=lambda item: (item != "base", item)):
        runtime_dir = managed_runtime_dir(runtime_channel)
        metadata = read_runtime_metadata(runtime_dir)
        python_executable = runtime_python_executable(runtime_channel)
        exists = runtime_dir.exists()
        ready = python_executable is not None
        app_version = str(metadata.get("appVersion") or "")
        layout = str(metadata.get("runtimeLayout") or "")
        cached_environment = (
            _load_cached_environment_probe_for_runtime_status(
                runtime_channel,
                runtime_dir,
                python_executable,
                metadata,
            )
            if exists
            else None
        )
        environment_status_source = "probe-cache" if cached_environment is not None else "metadata"
        local_asr_installed = bool(
            cached_environment.get("localAsrInstalled")
            if cached_environment is not None
            else metadata.get("localAsrInstalled")
        )
        knowledge_dependencies_ready = bool(
            cached_environment.get("knowledgeDependenciesReady")
            if cached_environment is not None
            else metadata.get("knowledgeDependenciesReady")
        )
        needs_update = bool(
            exists
            and runtime_channel != "base"
            and ready
            and not runtime_metadata_matches_base(metadata, base_metadata)
        )
        channels.append(
            {
                "runtimeChannel": runtime_channel,
                "path": str(runtime_dir),
                "exists": exists,
                "ready": ready,
                "python": str(python_executable or ""),
                "appVersion": app_version,
                "runtimeLayout": layout,
                "targetAppVersion": base_app_version,
                "targetRuntimeLayout": base_layout,
                "pythonVersion": str(metadata.get("pythonVersion") or ""),
                "targetPythonVersion": base_python_version,
                "needsUpdate": needs_update,
                "cudaVariant": str(metadata.get("cudaVariant") or ""),
                "localAsrInstalled": local_asr_installed,
                "knowledgeDependenciesReady": knowledge_dependencies_ready,
                "environmentStatusSource": environment_status_source,
            }
        )

    result = {
        "baseAppVersion": base_app_version,
        "baseRuntimeLayout": base_layout,
        "basePythonVersion": base_python_version,
        "pipIndexes": pip_index_options(),
        "channels": channels,
    }
    _INSPECT_CHANNELS_CACHE = result
    _INSPECT_CHANNELS_CACHED_AT = now
    return result


def _invalidate_inspect_channels_cache() -> None:
    global _INSPECT_CHANNELS_CACHE, _INSPECT_CHANNELS_CACHED_AT
    _INSPECT_CHANNELS_CACHE = None
    _INSPECT_CHANNELS_CACHED_AT = 0.0


def sync_runtime_channel(runtime_channel: str) -> dict[str, object]:
    runtime_channel = normalize_runtime_channel(runtime_channel, allow_unknown_gpu=True)
    if runtime_channel == "base":
        runtime_dir = ensure_runtime_channel("base")
    else:
        runtime_dir = ensure_runtime_channel(runtime_channel)
    clear_environment_probe_cache(runtime_channel)
    environment = detect_environment(runtime_channel)
    return {
        "synced": runtime_dir is not None,
        "runtimeChannel": runtime_channel,
        "path": str(runtime_dir or ""),
        "environment": environment,
    }


def sync_all_runtime_channels() -> dict[str, object]:
    status = inspect_runtime_channels()
    channels = [
        str(channel["runtimeChannel"])
        for channel in status["channels"]
        if channel.get("exists") and channel.get("runtimeChannel") != "base"
        and (channel.get("needsUpdate") or not channel.get("ready"))
    ]
    results = [sync_runtime_channel(runtime_channel) for runtime_channel in channels]
    return {
        "synced": True,
        "channels": results,
        "runtimeStatus": inspect_runtime_channels(),
    }


def sync_runtime_base(target_dir: Path, base_dir: Path, runtime_channel: str) -> None:
    target_metadata = read_runtime_metadata(target_dir)

    for item in base_dir.iterdir():
        if item.name == "video_sum_runtime.json":
            continue
        if not runtime_root_item_should_sync(item):
            continue
        if item.name in {"Lib", "lib"}:
            sync_runtime_lib(target_dir / item.name, item)
            continue
        if item.name in {"Scripts", "bin"}:
            sync_runtime_scripts(target_dir / item.name, item)
            continue
        copy_runtime_item(item, target_dir / item.name)

    remove_stale_runtime_root_items(target_dir, base_dir)

    base_metadata = read_runtime_metadata(base_dir)
    (target_dir / "video_sum_runtime.json").write_text(
        json.dumps(
            {
                **target_metadata,
                "runtimeChannel": runtime_channel,
                "runtimeLayout": base_metadata.get("runtimeLayout"),
                "appVersion": base_metadata.get("appVersion"),
                "pythonVersion": base_metadata.get("pythonVersion"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def runtime_metadata_matches_base(target_metadata: dict[str, object], base_metadata: dict[str, object]) -> bool:
    return (
        target_metadata.get("appVersion") == base_metadata.get("appVersion")
        and target_metadata.get("runtimeLayout") == base_metadata.get("runtimeLayout")
        and target_metadata.get("pythonVersion") == base_metadata.get("pythonVersion")
    )


def runtime_root_item_should_sync(item: Path) -> bool:
    name = item.name
    lower_name = name.lower()
    if name in _RUNTIME_ROOT_APP_DIRS or name in _RUNTIME_ROOT_APP_FILES:
        return True
    if lower_name in {"python.exe", "pythonw.exe", "python3.dll"}:
        return True
    if lower_name.startswith("python") and (lower_name.endswith(".dll") or lower_name.endswith("._pth")):
        return True
    if lower_name.startswith("vcruntime") and lower_name.endswith(".dll"):
        return True
    return False


def remove_stale_runtime_root_items(target_dir: Path, base_dir: Path) -> None:
    for name in _RUNTIME_ROOT_STALE_FILES:
        target = target_dir / name
        if target.exists():
            target.unlink()


def sync_runtime_scripts(target_scripts_dir: Path, base_scripts_dir: Path) -> None:
    if not base_scripts_dir.exists():
        return
    target_scripts_dir.mkdir(parents=True, exist_ok=True)
    for pattern in (
        "python",
        "python3",
        "python3.*",
        "video-sum-service*",
        "video-sum-transcribe-worker*",
    ):
        for item in base_scripts_dir.glob(pattern):
            copy_runtime_item(item, target_scripts_dir / item.name)


def sync_runtime_lib(target_lib_dir: Path, base_lib_dir: Path) -> None:
    if not base_lib_dir.exists():
        return
    target_lib_dir.mkdir(parents=True, exist_ok=True)
    for item in base_lib_dir.iterdir():
        if item.name == "site-packages":
            sync_runtime_site_packages(target_lib_dir / "site-packages", item)
            continue
        if item.is_dir() and (item / "site-packages").exists():
            sync_runtime_lib(target_lib_dir / item.name, item)
            continue
        copy_runtime_item(item, target_lib_dir / item.name)


def sync_runtime_site_packages(
    target_site_packages: Path,
    base_site_packages: Path,
) -> None:
    target_site_packages.mkdir(parents=True, exist_ok=True)
    protected_package_keys = runtime_protected_site_package_keys(target_site_packages)
    for item in base_site_packages.iterdir():
        if runtime_site_package_item_protected(item, protected_package_keys):
            continue
        remove_matching_dist_info(target_site_packages, item)
        copy_runtime_item(item, target_site_packages / item.name)


def runtime_site_package_item_protected(
    item: Path,
    protected_package_keys: set[str] | None = None,
) -> bool:
    package_key = runtime_site_package_key(item)
    if protected_package_keys is not None:
        return package_key in protected_package_keys
    return runtime_site_package_key_is_extension(package_key)


def runtime_site_package_key_is_extension(package_key: str) -> bool:
    return package_key in _RUNTIME_EXTENSION_PACKAGE_KEYS or package_key.startswith("nvidia_")


def runtime_protected_site_package_keys(target_site_packages: Path) -> set[str]:
    if not target_site_packages.exists():
        return set()
    installed_package_keys = {
        runtime_site_package_key(item)
        for item in target_site_packages.iterdir()
        if item.name != "__pycache__"
    }
    protected_package_keys = {
        key for key in installed_package_keys if runtime_site_package_key_is_extension(key)
    }
    if not protected_package_keys:
        return set()

    requirements_by_package, package_aliases = runtime_distribution_dependency_graph(target_site_packages)
    pending = list(protected_package_keys)
    while pending:
        package_key = pending.pop()
        related_keys = package_aliases.get(package_key, {package_key})
        dependency_keys = set().union(
            *(requirements_by_package.get(related_key, set()) for related_key in related_keys)
        )
        for dependency_key in set().union(*(package_aliases.get(key, {key}) for key in dependency_keys)):
            if dependency_key not in installed_package_keys or dependency_key in protected_package_keys:
                continue
            protected_package_keys.add(dependency_key)
            pending.append(dependency_key)
    return protected_package_keys


def runtime_distribution_dependency_graph(site_packages: Path) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    requirements_by_package: dict[str, set[str]] = {}
    package_aliases: dict[str, set[str]] = {}
    for item in list(site_packages.glob("*.dist-info")) + list(site_packages.glob("*.egg-info")):
        metadata_path = runtime_distribution_metadata_path(item)
        if metadata_path is None:
            continue
        try:
            metadata = Parser().parsestr(metadata_path.read_text(encoding="utf-8"))
        except OSError:
            continue
        package_keys = {runtime_site_package_key(item)}
        metadata_name = metadata.get("Name")
        if metadata_name:
            package_keys.add(runtime_normalize_package_key(metadata_name))
        package_keys.update(runtime_distribution_top_level_keys(item))
        for package_key in package_keys:
            package_aliases.setdefault(package_key, set()).update(package_keys)
        dependencies = {
            dependency_key
            for raw_requirement in metadata.get_all("Requires-Dist", [])
            if (dependency_key := runtime_requirement_package_key(raw_requirement))
        }
        for package_key in package_keys:
            requirements_by_package.setdefault(package_key, set()).update(dependencies)
    return requirements_by_package, package_aliases


def runtime_distribution_top_level_keys(distribution_path: Path) -> set[str]:
    keys: set[str] = set()
    if not distribution_path.is_dir():
        return keys
    top_level_path = distribution_path / "top_level.txt"
    if top_level_path.exists():
        try:
            keys.update(
                runtime_normalize_package_key(line.strip())
                for line in top_level_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
        except OSError:
            pass
    record_path = distribution_path / "RECORD"
    if record_path.exists():
        try:
            for line in record_path.read_text(encoding="utf-8").splitlines():
                top_level = line.split(",", 1)[0].replace("\\", "/").split("/", 1)[0].strip()
                if top_level and not top_level.endswith((".dist-info", ".egg-info")):
                    keys.add(runtime_site_package_key(Path(top_level)))
        except OSError:
            pass
    return keys


def runtime_distribution_metadata_path(distribution_path: Path) -> Path | None:
    if distribution_path.is_file():
        return distribution_path
    for filename in ("METADATA", "PKG-INFO"):
        candidate = distribution_path / filename
        if candidate.exists():
            return candidate
    return None


def runtime_requirement_package_key(raw_requirement: str) -> str:
    match = _RUNTIME_REQUIREMENT_NAME_PATTERN.match(raw_requirement)
    if match is None:
        return ""
    return runtime_normalize_package_key(match.group(1))


def runtime_normalize_package_key(package_name: str) -> str:
    return re.sub(r"[-_.]+", "_", package_name).lower()


def runtime_site_package_key(item: Path) -> str:
    name = item.name
    for suffix in (".dist-info", ".egg-info"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    if "-" in name:
        parts = name.split("-")
        version_index = next((index for index, part in enumerate(parts) if part[:1].isdigit()), len(parts))
        name = "-".join(parts[:version_index]) or parts[0]
    return runtime_normalize_package_key(name)


def remove_matching_dist_info(target_site_packages: Path, source: Path) -> None:
    if not (source.name.endswith(".dist-info") or source.name.endswith(".egg-info")):
        return
    source_key = runtime_site_package_key(source)
    for target in list(target_site_packages.glob("*.dist-info")) + list(target_site_packages.glob("*.egg-info")):
        if runtime_site_package_key(target) == source_key and target.name != source.name:
            shutil.rmtree(target) if target.is_dir() else target.unlink()


def copy_runtime_item(source: Path, target: Path) -> None:
    if not source.exists():
        return
    if source.is_dir():
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target, dirs_exist_ok=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def detect_environment(runtime_channel: str | None = None) -> dict[str, object]:
    active_channel = normalize_runtime_channel(
        runtime_channel or settings_manager.current.runtime_channel,
        allow_unknown_gpu=True,
    )
    # Ensure the DLL guard exists before running the probe subprocess.
    # Must be BEFORE the cache check so that stale cached results don't
    # prevent the guard from being written on first access.
    _ensure_runtime_sitecustomize(active_channel)

    cached = _load_cached_environment_probe(active_channel)
    if cached is not None:
        return apply_knowledge_dependency_policy(dict(cached))

    if uses_current_service_python(active_channel):
        # Do NOT .resolve() sys.executable — in uv venvs the symlink
        # target is the bare CPython interpreter without site-packages.
        python_executable = Path(sys.executable)
        probe_runner = lambda command, timeout=120: run_host_command(command, timeout=timeout)
    else:
        python_executable = runtime_python_executable(active_channel)
        probe_runner = lambda command, timeout=120: run_command(command, runtime_channel=active_channel, timeout=timeout)
        if python_executable is None:
            payload = {
                "pythonVersion": "",
                "torchInstalled": False,
                "torchVersion": "",
                "torchError": "Runtime Python executable is missing.",
                "torchvisionInstalled": False,
                "torchvisionVersion": "",
                "torchvisionBroken": False,
                "torchvisionError": "",
                "torchaudioInstalled": False,
                "torchaudioVersion": "",
                "torchaudioBroken": False,
                "torchaudioError": "",
                "cudaAvailable": False,
                "gpuName": "",
                "ytDlpVersion": "",
                "localAsrInstalled": False,
                "localAsrAvailable": False,
                "localAsrVersion": "",
                "funasrInstalled": False,
                "funasrAvailable": False,
                "funasrVersion": "",
                "funasrBroken": False,
                "funasrError": "",
                "chromadbInstalled": False,
                "chromadbVersion": "",
                "chromadbBroken": False,
                "chromadbError": "",
                "sentenceTransformersInstalled": False,
                "sentenceTransformersVersion": "",
                "sentenceTransformersBroken": False,
                "sentenceTransformersError": "",
                "modelscopeInstalled": False,
                "modelscopeVersion": "",
                "modelscopeBroken": False,
                "modelscopeError": "",
                "knowledgeDependenciesReady": False,
                "knowledgeDependenciesError": "Runtime Python executable is missing.",
                "ffmpegLocation": "",
                "recommendedModel": "base",
                "recommendedDevice": "cpu",
                "runtimeChannel": active_channel,
                "runtimeReady": False,
                "runtimePython": "",
                "runtimeError": "Runtime Python executable is missing.",
            }
            _store_cached_environment_probe(active_channel, payload)
            return payload

    probe_script_path = Path(__file__).parent / "probe_script.py"
    script = probe_script_path.read_text(encoding="utf-8")

    probe_failed = False
    try:
        result = probe_runner([str(python_executable), "-c", script], timeout=120)
        payload = json.loads(result.stdout.strip() or "{}")
        payload["ffmpegLocation"] = str(ffmpeg_location() or "")
        _environment_probe_failures.pop(active_channel, None)
        # Debug: log probe result when torch/funasr are unexpectedly missing
        if not payload.get("torchInstalled") or not payload.get("funasrInstalled"):
            logger.info(
                "probe debug channel=%s torch=%s funasr=%s sitecustomize=%s torchErr=%s funasrErr=%s",
                active_channel,
                payload.get("torchInstalled"),
                payload.get("funasrInstalled"),
                payload.get("sitecustomizeActive"),
                payload.get("torchError", "")[-200:],
                payload.get("funasrError", "")[-200:],
            )
    except Exception as exc:
        probe_failed = True
        failure_detail = (exc.stderr or exc.stdout or str(exc)).strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        if _environment_probe_failures.get(active_channel) != failure_detail:
            logger.warning(
                "detect environment failed runtime_channel=%s error=%s detail=%s",
                active_channel,
                exc,
                failure_detail[-1200:],
            )
            if len(_environment_probe_failures) >= 100:
                _environment_probe_failures.pop(next(iter(_environment_probe_failures)))
            _environment_probe_failures[active_channel] = failure_detail
        payload = {
            "pythonVersion": "",
            "torchInstalled": False,
            "torchVersion": "",
            "torchError": failure_detail[-1200:],
            "torchvisionInstalled": False,
            "torchvisionVersion": "",
            "torchvisionBroken": False,
            "torchvisionError": failure_detail[-1200:],
            "torchaudioInstalled": False,
            "torchaudioVersion": "",
            "torchaudioBroken": False,
            "torchaudioError": failure_detail[-1200:],
            "cudaAvailable": False,
            "gpuName": "",
            "ytDlpVersion": "",
            "localAsrInstalled": False,
            "localAsrAvailable": False,
            "localAsrVersion": "",
            "funasrInstalled": False,
            "funasrAvailable": False,
            "funasrVersion": "",
            "funasrBroken": False,
            "funasrError": failure_detail[-1200:],
            "chromadbInstalled": False,
            "chromadbVersion": "",
            "chromadbBroken": False,
            "chromadbError": "",
            "sentenceTransformersInstalled": False,
            "sentenceTransformersVersion": "",
            "sentenceTransformersBroken": False,
            "sentenceTransformersError": "",
            "modelscopeInstalled": False,
            "modelscopeVersion": "",
            "modelscopeBroken": False,
            "modelscopeError": "",
            "knowledgeDependenciesReady": False,
            "knowledgeDependenciesError": failure_detail[-1200:],
            "ffmpegLocation": "",
            "recommendedModel": "base",
            "recommendedDevice": "cpu",
            "runtimeError": failure_detail[-1200:],
        }

    payload.update(
        {
            "runtimeChannel": active_channel,
            "runtimeReady": (
                not probe_failed
                and (uses_current_service_python(active_channel) or runtime_python_executable(active_channel) is not None)
            ),
            "runtimePython": str(python_executable),
            "ffmpegLocation": str(ffmpeg_location() or ""),
            **runtime_cache_metadata_fields(active_channel, managed_runtime_dir(active_channel)),
        }
    )
    payload["localAsrInstalled"] = bool(payload.get("localAsrInstalled"))
    payload["localAsrAvailable"] = bool(payload.get("localAsrAvailable"))
    payload["localAsrVersion"] = str(payload.get("localAsrVersion") or "")
    payload["torchError"] = str(payload.get("torchError") or "")
    payload["torchvisionInstalled"] = bool(payload.get("torchvisionInstalled"))
    payload["torchvisionVersion"] = str(payload.get("torchvisionVersion") or "")
    payload["torchvisionBroken"] = bool(payload.get("torchvisionBroken"))
    payload["torchvisionError"] = str(payload.get("torchvisionError") or "")
    payload["torchaudioInstalled"] = bool(payload.get("torchaudioInstalled"))
    payload["torchaudioVersion"] = str(payload.get("torchaudioVersion") or "")
    payload["torchaudioBroken"] = bool(payload.get("torchaudioBroken"))
    payload["torchaudioError"] = str(payload.get("torchaudioError") or "")
    payload["funasrInstalled"] = bool(payload.get("funasrInstalled"))
    payload["funasrAvailable"] = bool(payload.get("funasrAvailable"))
    payload["funasrVersion"] = str(payload.get("funasrVersion") or "")
    payload["funasrBroken"] = bool(payload.get("funasrBroken"))
    payload["funasrError"] = str(payload.get("funasrError") or "")
    payload["chromadbInstalled"] = bool(payload.get("chromadbInstalled"))
    payload["chromadbVersion"] = str(payload.get("chromadbVersion") or "")
    payload["chromadbBroken"] = bool(payload.get("chromadbBroken"))
    payload["chromadbError"] = str(payload.get("chromadbError") or "")
    payload["sentenceTransformersInstalled"] = bool(payload.get("sentenceTransformersInstalled"))
    payload["sentenceTransformersVersion"] = str(payload.get("sentenceTransformersVersion") or "")
    payload["sentenceTransformersBroken"] = bool(payload.get("sentenceTransformersBroken"))
    payload["sentenceTransformersError"] = str(payload.get("sentenceTransformersError") or "")
    payload["modelscopeInstalled"] = bool(payload.get("modelscopeInstalled"))
    payload["modelscopeVersion"] = str(payload.get("modelscopeVersion") or "")
    payload["modelscopeBroken"] = bool(payload.get("modelscopeBroken"))
    payload["modelscopeError"] = str(payload.get("modelscopeError") or "")
    payload["knowledgeDependenciesReady"] = bool(payload.get("knowledgeDependenciesReady"))
    payload["knowledgeDependenciesError"] = str(payload.get("knowledgeDependenciesError") or "")
    apply_knowledge_dependency_policy(payload)
    payload["runtimeError"] = str(payload.get("runtimeError") or "")
    _store_cached_environment_probe(active_channel, payload)
    return payload


def clear_environment_probe_cache(runtime_channel: str | None = None) -> None:
    _invalidate_inspect_channels_cache()
    if runtime_channel is None:
        _environment_probe_cache.clear()
        _environment_probe_failures.clear()
        path = _environment_probe_cache_path()
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("failed to remove environment probe cache path=%s", path)
        return
    runtime_channel = normalize_runtime_channel(runtime_channel, allow_unknown_gpu=True)
    _environment_probe_cache.pop(runtime_channel, None)
    _environment_probe_failures.pop(runtime_channel, None)
    cache_file = _read_environment_probe_cache_file()
    if runtime_channel in cache_file:
        cache_file.pop(runtime_channel, None)
        _write_environment_probe_cache_file(cache_file)


def build_worker(
    repository: SqliteTaskRepository,
    current_settings: ServiceSettings,
    environment_info: dict[str, object] | None = None,
) -> TaskWorker:
    from video_sum_core.pipeline.real import PipelineSettings, RealPipelineRunner

    from video_sum_service.worker import TaskWorker

    selected_runtime_channel = normalize_runtime_channel(current_settings.runtime_channel, allow_unknown_gpu=True)
    if selected_runtime_channel != "base" and runtime_python_executable(selected_runtime_channel) is None:
        logger.warning("runtime channel %s is not ready, falling back to base", selected_runtime_channel)
        selected_runtime_channel = "base"

    bootstrap_managed_runtime(selected_runtime_channel)
    prepend_runtime_path(selected_runtime_channel)
    activate_runtime_pythonpath(selected_runtime_channel)
    environment = environment_info or detect_environment(selected_runtime_channel)
    runtime_settings = current_settings.with_resolved_runtime(cuda_available=bool(environment.get("cudaAvailable")))
    pipeline_settings_payload = {
        "tasks_dir": runtime_settings.tasks_dir,
        "runtime_channel": selected_runtime_channel,
        "transcription_provider": runtime_settings.transcription_provider,
        "whisper_model": runtime_settings.whisper_model,
        "whisper_device": runtime_settings.whisper_device,
        "whisper_compute_type": runtime_settings.whisper_compute_type,
        "local_asr_available": bool(environment.get("localAsrAvailable")),
        "siliconflow_asr_base_url": runtime_settings.siliconflow_asr_base_url,
        "siliconflow_asr_model": runtime_settings.siliconflow_asr_model,
        "siliconflow_asr_api_key": runtime_settings.siliconflow_asr_api_key,
        "siliconflow_asr_chunk_duration_seconds": runtime_settings.siliconflow_asr_chunk_duration_seconds,
        "siliconflow_asr_concurrency": runtime_settings.siliconflow_asr_concurrency,
        "multimodal_asr_base_url": runtime_settings.multimodal_asr_base_url,
        "multimodal_asr_model": runtime_settings.multimodal_asr_model,
        "multimodal_asr_api_key": runtime_settings.multimodal_asr_api_key,
        "multimodal_asr_chunk_duration_seconds": runtime_settings.multimodal_asr_chunk_duration_seconds,
        "multimodal_asr_max_retries": runtime_settings.multimodal_asr_max_retries,
        "funasr_available": bool(environment.get("funasrAvailable")),
        "funasr_model": runtime_settings.funasr_model,
        "funasr_device": runtime_settings.funasr_device,
        "funasr_vad_model": runtime_settings.funasr_vad_model,
        "funasr_punc_model": runtime_settings.funasr_punc_model,
        "funasr_spk_model": runtime_settings.funasr_spk_model,
        "funasr_hub": runtime_settings.funasr_hub,
        "funasr_hotword": runtime_settings.funasr_hotword,
        "llm_enabled": runtime_settings.llm_enabled,
        "llm_provider": runtime_settings.llm_provider,
        "llm_api_key": runtime_settings.llm_api_key,
        "llm_base_url": runtime_settings.llm_base_url,
        "llm_model": runtime_settings.llm_model,
        "visual_evidence_enabled": runtime_settings.visual_evidence_enabled,
        "visual_note_mode": runtime_settings.visual_note_mode,
        "visual_multimodal_enabled": runtime_settings.visual_multimodal_enabled,
        "visual_download_resolution": runtime_settings.visual_download_resolution,
        "visual_evidence_use_llm": runtime_settings.visual_evidence_use_llm and runtime_settings.visual_multimodal_enabled,
        "visual_vlm_provider": runtime_settings.visual_vlm_provider,
        "visual_evidence_base_url": runtime_settings.visual_evidence_base_url,
        "visual_evidence_model": runtime_settings.visual_evidence_model,
        "visual_evidence_api_key": runtime_settings.visual_evidence_api_key,
        "visual_evidence_max_frames": runtime_settings.visual_evidence_max_frames,
        "visual_evidence_frame_interval_seconds": runtime_settings.visual_evidence_frame_interval_seconds,
        "visual_evidence_frame_width": runtime_settings.visual_evidence_frame_width,
        "visual_evidence_image_quality": runtime_settings.visual_evidence_image_quality,
        "visual_evidence_timeout_seconds": runtime_settings.visual_evidence_timeout_seconds,
        "visual_evidence_retry_count": runtime_settings.visual_evidence_retry_count,
        "visual_note_system_prompt": runtime_settings.visual_note_system_prompt,
        "visual_note_user_prompt_template": runtime_settings.visual_note_user_prompt_template,
        "visual_frame_planning_prompt": runtime_settings.visual_frame_planning_prompt,
        "visual_vlm_prompt": runtime_settings.visual_vlm_prompt,
        "summary_system_prompt": runtime_settings.summary_system_prompt,
        "summary_user_prompt_template": runtime_settings.summary_user_prompt_template,
        "knowledge_note_system_prompt": runtime_settings.knowledge_note_system_prompt,
        "knowledge_note_user_prompt_template": runtime_settings.knowledge_note_user_prompt_template,
        "mindmap_system_prompt": runtime_settings.mindmap_system_prompt,
        "mindmap_user_prompt_template": runtime_settings.mindmap_user_prompt_template,
        "summary_chunk_target_chars": runtime_settings.summary_chunk_target_chars,
        "summary_chunk_overlap_segments": runtime_settings.summary_chunk_overlap_segments,
        "summary_chunk_concurrency": runtime_settings.summary_chunk_concurrency,
        "summary_chunk_retry_count": runtime_settings.summary_chunk_retry_count,
        "ytdlp_cookies_file": runtime_settings.ytdlp_cookies_file,
        "ytdlp_cookies_browser": runtime_settings.ytdlp_cookies_browser,
    }
    supported_pipeline_fields = {field.name for field in fields(PipelineSettings)}
    pipeline_settings = PipelineSettings(
        **{
            key: value
            for key, value in pipeline_settings_payload.items()
            if key in supported_pipeline_fields
        }
    )
    return TaskWorker(
        repository=repository,
        pipeline_runner=RealPipelineRunner(pipeline_settings),
        auto_generate_mindmap=current_settings.auto_generate_mindmap,
        auto_generate_visual_evidence=current_settings.visual_evidence_enabled and current_settings.visual_note_mode != "text",
        knowledge_index_auto_rebuild=(
            current_settings.knowledge_index_auto_rebuild
            if current_settings.knowledge_enabled
            else "disabled"
        ),
        knowledge_index_settings=current_settings,
        task_concurrency=current_settings.task_concurrency,
        mindmap_concurrency=current_settings.mindmap_concurrency,
    )


def replace_task_worker(app_state, next_worker: TaskWorker) -> TaskWorker:
    from video_sum_service.worker import TaskWorker

    previous_worker = getattr(app_state, "task_worker", None)
    app_state.task_worker = next_worker
    if isinstance(previous_worker, TaskWorker):
        previous_worker.shutdown(wait=False, cancel_pending=True)
    return next_worker


def serialize_settings(
    current_settings: ServiceSettings,
    environment_info: dict[str, object] | None = None,
) -> dict[str, object]:
    environment = environment_info or detect_environment(current_settings.runtime_channel)
    runtime_settings = current_settings.with_resolved_runtime(cuda_available=bool(environment.get("cudaAvailable")))
    return {
        "host": current_settings.host,
        "port": current_settings.port,
        "data_dir": str(current_settings.data_dir),
        "cache_dir": str(current_settings.cache_dir),
        "tasks_dir": str(current_settings.tasks_dir),
        "database_url": current_settings.database_url,
        "transcription_provider": current_settings.transcription_provider,
        "prefer_bilibili_subtitle": current_settings.prefer_bilibili_subtitle,
        "whisper_model": runtime_settings.whisper_model,
        "whisper_device": runtime_settings.whisper_device,
        "whisper_compute_type": runtime_settings.whisper_compute_type,
        "device_preference": current_settings.device_preference,
        "compute_type": current_settings.compute_type,
        "model_mode": current_settings.model_mode,
        "fixed_model": current_settings.fixed_model,
        "siliconflow_asr_base_url": current_settings.siliconflow_asr_base_url,
        "siliconflow_asr_model": current_settings.siliconflow_asr_model,
        "siliconflow_asr_api_key": "",
        "siliconflow_asr_api_key_configured": bool(current_settings.siliconflow_asr_api_key),
        "multimodal_asr_base_url": current_settings.multimodal_asr_base_url,
        "multimodal_asr_model": current_settings.multimodal_asr_model,
        "multimodal_asr_api_key": "",
        "multimodal_asr_api_key_configured": bool(current_settings.multimodal_asr_api_key),
        "multimodal_asr_chunk_duration_seconds": current_settings.multimodal_asr_chunk_duration_seconds,
        "multimodal_asr_max_retries": current_settings.multimodal_asr_max_retries,
        "funasr_model": current_settings.funasr_model,
        "funasr_device": current_settings.funasr_device,
        "funasr_vad_model": current_settings.funasr_vad_model,
        "funasr_punc_model": current_settings.funasr_punc_model,
        "funasr_spk_model": current_settings.funasr_spk_model,
        "funasr_hub": current_settings.funasr_hub,
        "funasr_hotword": current_settings.funasr_hotword,
        "funasr_available": environment.get("funasrAvailable", False),
        "cuda_variant": current_settings.cuda_variant,
        "runtime_channel": current_settings.runtime_channel,
        "output_dir": str(current_settings.output_dir),
        "preserve_temp_audio": current_settings.preserve_temp_audio,
        "enable_cache": current_settings.enable_cache,
        "language": current_settings.language,
        "summary_mode": current_settings.summary_mode,
        "prompt_router_mode": current_settings.prompt_router_mode,
        "prompt_presets_path": current_settings.prompt_presets_path,
        "llm_enabled": current_settings.llm_enabled,
        "auto_generate_mindmap": current_settings.auto_generate_mindmap,
        "visual_note_mode": current_settings.visual_note_mode,
        "visual_evidence_enabled": current_settings.visual_evidence_enabled,
        "visual_multimodal_enabled": current_settings.visual_multimodal_enabled,
        "visual_download_resolution": current_settings.visual_download_resolution,
        "visual_evidence_use_llm": current_settings.visual_evidence_use_llm,
        "visual_vlm_provider": current_settings.visual_vlm_provider,
        "visual_evidence_base_url": current_settings.visual_evidence_base_url,
        "visual_evidence_model": current_settings.visual_evidence_model,
        "visual_evidence_api_key": "",
        "visual_evidence_api_key_configured": bool(current_settings.visual_evidence_api_key),
        "visual_evidence_max_frames": current_settings.visual_evidence_max_frames,
        "visual_evidence_frame_interval_seconds": current_settings.visual_evidence_frame_interval_seconds,
        "visual_evidence_frame_width": current_settings.visual_evidence_frame_width,
        "visual_evidence_image_quality": current_settings.visual_evidence_image_quality,
        "visual_evidence_timeout_seconds": current_settings.visual_evidence_timeout_seconds,
        "visual_evidence_retry_count": current_settings.visual_evidence_retry_count,
        "llm_provider": current_settings.llm_provider,
        "llm_base_url": current_settings.llm_base_url,
        "llm_model": current_settings.llm_model,
        "llm_api_key": "",
        "llm_api_key_configured": bool(current_settings.llm_api_key),
        "knowledge_llm_mode": current_settings.knowledge_llm_mode,
        "knowledge_llm_enabled": current_settings.knowledge_llm_enabled,
        "knowledge_llm_provider": current_settings.knowledge_llm_provider,
        "knowledge_llm_base_url": current_settings.knowledge_llm_base_url,
        "knowledge_llm_model": current_settings.knowledge_llm_model,
        "knowledge_llm_api_key": "",
        "knowledge_llm_api_key_configured": bool(current_settings.knowledge_llm_api_key),
        "knowledge_enabled": current_settings.knowledge_enabled,
        "knowledge_embedding_provider": current_settings.knowledge_embedding_provider,
        "knowledge_embedding_model": current_settings.knowledge_embedding_model,
        "hf_endpoint": current_settings.hf_endpoint,
        "siliconflow_embedding_api_key": "",
        "siliconflow_embedding_api_key_configured": bool(current_settings.siliconflow_embedding_api_key),
        "siliconflow_embedding_base_url": current_settings.siliconflow_embedding_base_url,
        "siliconflow_embedding_model": current_settings.siliconflow_embedding_model,
        "knowledge_index_auto_rebuild": current_settings.knowledge_index_auto_rebuild,
        "summary_system_prompt": current_settings.summary_system_prompt,
        "summary_user_prompt_template": current_settings.summary_user_prompt_template,
        "knowledge_note_system_prompt": current_settings.knowledge_note_system_prompt,
        "knowledge_note_user_prompt_template": current_settings.knowledge_note_user_prompt_template,
        "visual_note_system_prompt": current_settings.visual_note_system_prompt,
        "visual_note_user_prompt_template": current_settings.visual_note_user_prompt_template,
        "visual_frame_planning_prompt": current_settings.visual_frame_planning_prompt,
        "visual_vlm_prompt": current_settings.visual_vlm_prompt,
        "summary_chunk_target_chars": current_settings.summary_chunk_target_chars,
        "summary_chunk_overlap_segments": current_settings.summary_chunk_overlap_segments,
        "task_concurrency": current_settings.task_concurrency,
        "mindmap_concurrency": current_settings.mindmap_concurrency,
        "summary_chunk_concurrency": current_settings.summary_chunk_concurrency,
        "summary_chunk_retry_count": current_settings.summary_chunk_retry_count,
        "ytdlp_cookies_file": current_settings.ytdlp_cookies_file,
        "ytdlp_cookies_browser": current_settings.ytdlp_cookies_browser,
        "settings_file_exists": settings_manager.has_persisted_settings,
        "defaults": {
            "knowledge_note_system_prompt": DEFAULT_KNOWLEDGE_NOTE_SYSTEM_PROMPT,
            "knowledge_note_user_prompt_template": DEFAULT_KNOWLEDGE_NOTE_USER_PROMPT_TEMPLATE,
            "visual_note_system_prompt": DEFAULT_VISUAL_NOTE_SYSTEM_PROMPT,
            "visual_note_user_prompt_template": DEFAULT_VISUAL_NOTE_USER_PROMPT_TEMPLATE,
            "visual_frame_planning_prompt": DEFAULT_VISUAL_FRAME_PLANNING_PROMPT,
            "visual_vlm_prompt": DEFAULT_VISUAL_VLM_PROMPT,
            "summary_system_prompt": DEFAULT_SUMMARY_SYSTEM_PROMPT,
            "summary_user_prompt_template": DEFAULT_SUMMARY_USER_PROMPT_TEMPLATE,
        },
    }


def install_cuda_support(cuda_variant: str, repository: SqliteTaskRepository, *, session_id: str | None = None) -> tuple[dict[str, object], TaskWorker]:
    if cuda_variant not in {"cu124", "cu126", "cu128"}:
        raise HTTPException(status_code=400, detail="Unsupported CUDA variant.")

    runtime_channel = f"gpu-{cuda_variant}"
    runtime_dir = ensure_runtime_channel(runtime_channel)
    python_executable = runtime_python_executable(runtime_channel)
    if runtime_dir is None or python_executable is None:
        raise HTTPException(status_code=500, detail="Managed runtime is unavailable.")

    use_streaming = session_id is not None
    if use_streaming:
        start_install_session(session_id, "CUDA")
    runner = _StreamingRunner(session_id) if use_streaming else run_command

    try:
        install_workspace_packages(python_executable, runtime_channel=runtime_channel)
        ensure_runtime_pip(python_executable, runtime_channel)
        result = torch_install_with_fallbacks(
            python_executable,
            runtime_channel,
            cuda_variant,
            timeout=1800,
            runner=runner,
        )
    except subprocess.CalledProcessError as exc:
        if isinstance(runner, _StreamingRunner):
            runner.cancel()
            finish_install_session(session_id, success=False)
        clear_environment_probe_cache(runtime_channel)
        raise HTTPException(status_code=500, detail=command_error_detail(exc, "安装 CUDA 依赖失败。")) from exc
    except HTTPException:
        if isinstance(runner, _StreamingRunner):
            runner.cancel()
            finish_install_session(session_id, success=False)
        clear_environment_probe_cache(runtime_channel)
        raise

    if use_streaming:
        finish_install_session(session_id, success=True)

    current_settings = settings_manager.save(SettingsUpdatePayload(cuda_variant=cuda_variant, runtime_channel=runtime_channel))
    clear_environment_probe_cache(runtime_channel)
    clear_environment_probe_cache("base")
    # Inherit version metadata from base so the GPU runtime reports the correct appVersion
    base_metadata = read_runtime_metadata(managed_runtime_dir("base"))
    write_runtime_metadata(
        runtime_channel,
        {
            "runtimeChannel": runtime_channel,
            "cudaVariant": cuda_variant,
            "python": str(python_executable),
            "appVersion": base_metadata.get("appVersion") or "",
            "runtimeLayout": base_metadata.get("runtimeLayout") or "",
            "pythonVersion": base_metadata.get("pythonVersion") or "",
        },
    )
    environment = detect_environment(runtime_channel)
    worker = build_worker(repository, current_settings, environment_info=environment)
    return {
        "installed": True,
        "cudaVariant": cuda_variant,
        "runtimeChannel": runtime_channel,
        "restartRequired": True,
        "stdoutTail": (result.stdout or "")[-1500:],
        "environment": environment,
        "installSessionId": session_id,
    }, worker


def install_local_asr(reinstall: bool, repository: SqliteTaskRepository, *, session_id: str | None = None) -> tuple[dict[str, object], TaskWorker]:
    current_settings = settings_manager.current
    runtime_channel = normalize_runtime_channel(current_settings.runtime_channel, allow_unknown_gpu=True)
    lock = _acquire_channel_lock(runtime_channel)
    if lock is None:
        raise HTTPException(status_code=409, detail="另一个安装或同步操作正在进行中，请稍后重试。")

    use_current_python = uses_current_service_python(runtime_channel)
    if use_current_python:
        runtime_dir = repo_root()
        python_executable = Path(sys.executable)
        runner = lambda command, runtime_channel, timeout=1800: run_host_command(command, timeout=timeout)
    else:
        runtime_dir = ensure_runtime_channel(runtime_channel)
        python_executable = runtime_python_executable(runtime_channel)
        runner = run_command
    if runtime_dir is None or python_executable is None:
        raise HTTPException(status_code=500, detail="Managed runtime is unavailable.")

    # Pre-flight: disk space check for model download (~3 GB for large-v3)
    cache_parent = Path.home() / ".cache"
    if cache_parent.exists():
        try:
            free = shutil.disk_usage(cache_parent).free
            if free < 3 * 1024 * 1024 * 1024:  # < 3 GB
                raise HTTPException(
                    status_code=507,
                    detail=f"磁盘空间不足，本地 ASR 模型下载需要至少 3 GB。当前可用: {free / (1024 ** 3):.1f} GB",
                )
        except HTTPException:
            raise
        except OSError:
            pass

    use_streaming = session_id is not None
    if use_streaming:
        start_install_session(session_id, "本地 ASR")
    if use_streaming and use_current_python:
        host_env = dict(os.environ)
        for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__"):
            host_env.pop(key, None)
        host_env["PYTHONIOENCODING"] = "utf-8"
        host_env["PYTHONUTF8"] = "1"
        pip_runner: _StreamingRunner | object = _StreamingRunner(session_id, env=host_env, cwd=repo_root())
    elif use_streaming:
        pip_runner = _StreamingRunner(session_id)
    else:
        pip_runner = runner

    try:
        if not use_current_python:
            install_workspace_packages(python_executable, runtime_channel=runtime_channel)
            ensure_runtime_pip(python_executable, runtime_channel)
        else:
            ensure_python_pip(python_executable, runtime_channel, runner=runner)
        result = _run_pip_install(
            python_executable,
            runtime_channel,
            ["faster-whisper>=1.1.1"],
            package_label="本地 ASR 依赖",
            reinstall=reinstall,
            timeout=1800,
            runner=pip_runner,
        )
    except subprocess.CalledProcessError as exc:
        if isinstance(pip_runner, _StreamingRunner):
            pip_runner.cancel()
            finish_install_session(session_id, success=False)
        clear_environment_probe_cache(runtime_channel)
        _release_channel_lock(lock)
        raise HTTPException(status_code=500, detail=((exc.stderr or exc.stdout or str(exc))[-1500:])) from exc
    except HTTPException:
        if isinstance(pip_runner, _StreamingRunner):
            pip_runner.cancel()
            finish_install_session(session_id, success=False)
        clear_environment_probe_cache(runtime_channel)
        _release_channel_lock(lock)
        raise

    clear_environment_probe_cache(runtime_channel)
    environment = detect_environment(runtime_channel)
    worker = build_worker(repository, current_settings, environment_info=environment)
    write_runtime_metadata(
        runtime_channel,
        {
            "runtimeChannel": runtime_channel,
            "python": str(python_executable),
            "localAsrInstalled": bool(environment.get("localAsrInstalled")),
            "localAsrVersion": str(environment.get("localAsrVersion") or ""),
        },
    )
    installed = bool(environment.get("localAsrInstalled"))
    if use_streaming:
        finish_install_session(session_id, success=installed)
    _release_channel_lock(lock)
    return {
        "installed": installed,
        "runtimeChannel": runtime_channel,
        "installSessionId": session_id,
        "stdoutTail": ((result.stdout or "") + "\n" + (result.stderr or "")).strip()[-1500:],
        "environment": environment,
    }, worker


def install_funasr(reinstall: bool, repository: SqliteTaskRepository, *, session_id: str | None = None) -> tuple[dict[str, object], TaskWorker]:
    current_settings = settings_manager.current
    runtime_channel = normalize_runtime_channel(current_settings.runtime_channel, allow_unknown_gpu=True)

    # W2: prevent concurrent install/sync on the same channel
    lock = _acquire_channel_lock(runtime_channel)
    if lock is None:
        raise HTTPException(status_code=409, detail="另一个安装或同步操作正在进行中，请稍后重试。")

    use_current_python = uses_current_service_python(runtime_channel)
    if use_current_python:
        runtime_dir = repo_root()
        python_executable = Path(sys.executable)
        runner = lambda command, runtime_channel, timeout=1800: run_host_command(command, timeout=timeout)
    else:
        # Pre-flight: if the runtime channel is broken (e.g. Python binary missing
        # after a partial upgrade or corrupted pip/setuptools), force-rebuild it
        # from scratch before attempting any pip work.
        python_executable = runtime_python_executable(runtime_channel)
        if python_executable is None:
            logger.warning("runtime channel %s has no python — forcing full rebuild", runtime_channel)
            target_dir = managed_runtime_dir(runtime_channel)
            if target_dir.exists():
                _robust_rmtree(target_dir)
            # Also clean stale backup/temp dirs
            for stale in [
                runtime_refresh_backup_dir(runtime_channel),
                target_dir.parent / f".{runtime_channel}-refresh-backup-temp",
                target_dir.parent / f".{runtime_channel}-refresh-temp",
            ]:
                if stale.exists():
                    _robust_rmtree(stale)

        runtime_dir = ensure_runtime_channel(runtime_channel)
        python_executable = runtime_python_executable(runtime_channel)
        if runtime_dir is None or python_executable is None:
            raise HTTPException(status_code=500, detail=(
                "运行环境创建失败。请尝试：1) 重启应用 2) 设置 → 运行环境 → 同步需要更新的 runtime "
                "3) 手动删除 %s 后重试" % managed_runtime_dir(runtime_channel)
            ))
        runner = run_command

    # W10: disk space pre-check
    cache_dir = Path.home() / ".cache" / "modelscope"
    if cache_dir.parent.exists():
        free = shutil.disk_usage(cache_dir.parent).free
        if free < 2 * 1024 * 1024 * 1024:  # < 2GB
            raise HTTPException(
                status_code=507,
                detail="磁盘空间不足（需要至少 2GB）。当前可用: {:.1f}GB".format(free / (1024**3)),
            )

    use_streaming = session_id is not None
    if use_streaming:
        start_install_session(session_id, "FunASR")
    if use_streaming and use_current_python:
        host_env = dict(os.environ)
        for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__"):
            host_env.pop(key, None)
        host_env["PYTHONIOENCODING"] = "utf-8"
        host_env["PYTHONUTF8"] = "1"
        pip_runner: _StreamingRunner | object = _StreamingRunner(session_id, env=host_env, cwd=repo_root())
    elif use_streaming:
        pip_runner = _StreamingRunner(session_id)
    else:
        pip_runner = runner

    try:
        if not use_current_python:
            # install_workspace_packages bootstraps pip + workspace packages.
            # GPU runtimes already use --no-deps, so workspace reinstall is safe.
            install_workspace_packages(python_executable, runtime_channel=runtime_channel)
            ensure_runtime_pip(python_executable, runtime_channel)
        else:
            ensure_python_pip(python_executable, runtime_channel, runner=runner)

        clear_environment_probe_cache(runtime_channel)
        pre_install_environment = detect_environment(runtime_channel)
        repair_reinstall = bool(
            pre_install_environment.get("funasrVersion")
            and (
                pre_install_environment.get("funasrBroken")
                or pre_install_environment.get("funasrError")
                or not pre_install_environment.get("funasrInstalled")
            )
        )
        if repair_reinstall and isinstance(pip_runner, _StreamingRunner):
            append_install_log(session_id, "[FunASR] 检测到已安装包导入异常，将执行强制重装。")

        torch_repair_result = ensure_torch_family_compatible(
            python_executable,
            runtime_channel,
            package_label="FunASR 依赖",
            runner=pip_runner,
            install_if_missing=True,
        )

        # C1: Probe installed torch before deciding what to install.
        # On GPU channels, torch / torchvision / torchaudio must come from
        # the PyTorch CUDA index.  The generic PyPI index can overwrite them
        # with CPU wheels, so only let PyPI install torch on base/CPU.
        funasr_packages = ["transformers>=4.40,<4.50", "funasr>=1.1.0"]
        if not _runtime_channel_cuda_variant(runtime_channel):
            try:
                torch_probe = runner(
                    [str(python_executable), "-c", "import torch; print(torch.__version__)"],
                    runtime_channel=runtime_channel,
                    timeout=30,
                )
                logger.info("torch %s already installed — skipping torch/torchaudio install", torch_probe.stdout.strip())
            except subprocess.CalledProcessError:
                logger.info("torch not found — will install torch + torchaudio with funasr")
                funasr_packages = ["torch", "torchaudio"] + funasr_packages

        result = _run_pip_install(
            python_executable,
            runtime_channel,
            funasr_packages,
            package_label="FunASR 依赖",
            reinstall=reinstall or repair_reinstall,
            timeout=3600,
            runner=pip_runner,
        )
        if torch_repair_result is not None:
            result = subprocess.CompletedProcess(
                result.args,
                result.returncode,
                stdout="\n".join(
                    part
                    for part in [str(torch_repair_result.stdout or ""), str(result.stdout or "")]
                    if part
                ),
                stderr="\n".join(
                    part
                    for part in [str(torch_repair_result.stderr or ""), str(result.stderr or "")]
                    if part
                ),
            )
    except subprocess.CalledProcessError as exc:
        if isinstance(pip_runner, _StreamingRunner):
            pip_runner.cancel()
            finish_install_session(session_id, success=False)
        clear_environment_probe_cache(runtime_channel)
        _release_channel_lock(lock)
        raise HTTPException(status_code=500, detail=((exc.stderr or exc.stdout or str(exc))[-1500:])) from exc
    except HTTPException:
        if isinstance(pip_runner, _StreamingRunner):
            pip_runner.cancel()
            finish_install_session(session_id, success=False)
        clear_environment_probe_cache(runtime_channel)
        _release_channel_lock(lock)
        raise

    clear_environment_probe_cache(runtime_channel)
    environment = detect_environment(runtime_channel)
    worker = build_worker(repository, current_settings, environment_info=environment)
    write_runtime_metadata(
        runtime_channel,
        {
            "runtimeChannel": runtime_channel,
            "python": str(python_executable),
            "funasrInstalled": bool(environment.get("funasrInstalled")),
            "funasrVersion": str(environment.get("funasrVersion") or ""),
        },
    )
    installed = bool(environment.get("funasrInstalled"))
    if use_streaming:
        finish_install_session(session_id, success=installed)
    _release_channel_lock(lock)
    result_payload: dict[str, object] = {
        "installed": installed,
        "runtimeChannel": runtime_channel,
        "installSessionId": session_id,
        "stdoutTail": ((result.stdout or "") + "\n" + (result.stderr or "")).strip()[-1500:],
        "environment": environment,
    }
    # Surface probe error details when installation appears to fail so
    # the UI (and user) can see the actual import error.
    if not installed:
        result_payload["funasrError"] = str(environment.get("funasrError") or "")
        result_payload["torchError"] = str(environment.get("torchError") or "")
    return result_payload, worker


def install_knowledge_dependencies(
    reinstall: bool,
    repository: SqliteTaskRepository,
    runtime_channel: str | None = None,
    provider: str | None = None,
    session_id: str | None = None,
) -> tuple[dict[str, object], TaskWorker | None]:
    current_settings = settings_manager.current

    # Determine provider
    if provider is None:
        provider = str(getattr(current_settings, "knowledge_embedding_provider", "local_huggingface"))
    provider = provider.strip().lower()

    # Get required packages based on provider
    requirements = get_knowledge_requirements(provider)
    required_packages = requirements["required"]

    runtime_channel = normalize_runtime_channel(runtime_channel or current_settings.runtime_channel, allow_unknown_gpu=True)
    current_runtime_channel = normalize_runtime_channel(current_settings.runtime_channel, allow_unknown_gpu=True)
    should_refresh_worker = runtime_channel == current_runtime_channel
    use_current_python = uses_current_service_python(runtime_channel)
    if use_current_python:
        runtime_dir = repo_root()
        python_executable = Path(sys.executable)
        runner = lambda command, runtime_channel, timeout=1800: run_host_command(command, timeout=timeout)
    else:
        runtime_dir = ensure_runtime_channel(runtime_channel)
        python_executable = runtime_python_executable(runtime_channel)
        runner = run_command
    if runtime_dir is None or python_executable is None:
        raise HTTPException(status_code=500, detail="Managed runtime is unavailable.")

    use_streaming = session_id is not None
    if use_streaming:
        start_install_session(session_id, "知识库依赖")
    if use_streaming and use_current_python:
        host_env = dict(os.environ)
        for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__"):
            host_env.pop(key, None)
        host_env["PYTHONIOENCODING"] = "utf-8"
        host_env["PYTHONUTF8"] = "1"
        pip_runner: _StreamingRunner | object = _StreamingRunner(session_id, env=host_env, cwd=repo_root())
    elif use_streaming:
        pip_runner = _StreamingRunner(session_id)
    else:
        pip_runner = runner

    repair_reinstall = False
    clear_environment_probe_cache(runtime_channel)
    environment = detect_environment(runtime_channel)
    if not reinstall:
        environment = apply_knowledge_dependency_policy(environment, provider)
        if environment.get("knowledgeDependenciesReady"):
            if use_streaming:
                append_install_log(session_id, "知识库依赖已在当前运行环境可用，无需重复安装。")
                finish_install_session(session_id, success=True)
            worker = (
                build_worker(repository, current_settings, environment_info=environment)
                if should_refresh_worker
                else None
            )
            write_runtime_metadata(
                runtime_channel,
                {
                    "runtimeChannel": runtime_channel,
                    "python": str(python_executable),
                    "chromadbInstalled": bool(environment.get("chromadbInstalled")),
                    "chromadbVersion": str(environment.get("chromadbVersion") or ""),
                    "chromadbBroken": bool(environment.get("chromadbBroken")),
                    "sentenceTransformersInstalled": bool(environment.get("sentenceTransformersInstalled")),
                    "sentenceTransformersVersion": str(environment.get("sentenceTransformersVersion") or ""),
                    "sentenceTransformersBroken": bool(environment.get("sentenceTransformersBroken")),
                    "modelscopeInstalled": bool(environment.get("modelscopeInstalled")),
                    "modelscopeVersion": str(environment.get("modelscopeVersion") or ""),
                    "modelscopeBroken": bool(environment.get("modelscopeBroken")),
                    "knowledgeDependenciesReady": True,
                },
            )
            return {
                "installed": True,
                "runtimeChannel": runtime_channel,
                "stdoutTail": "知识库依赖已在当前运行环境可用，无需重复安装。",
                "environment": environment,
                "installSessionId": session_id,
            }, worker
        repair_reinstall = bool(
            environment.get("chromadbVersion")
            or environment.get("sentenceTransformersVersion")
            or environment.get("modelscopeVersion")
            or environment.get("chromadbError")
            or environment.get("sentenceTransformersError")
            or environment.get("modelscopeError")
            or (
                environment.get("knowledgeDependenciesError")
                and not str(environment.get("knowledgeDependenciesError") or "").startswith("缺少依赖")
            )
        )

    packages = ["chromadb>=1.0.0"]
    if "sentence-transformers" in required_packages:
        packages.extend(["transformers>=4.40,<4.50", "sentence-transformers>=3.0"])
    if "modelscope" in required_packages:
        packages.append("modelscope")

    # Auto-uninstall broken residual packages that are NOT required by current provider.
    # E.g. when switching from local_huggingface to siliconflow, leftover broken
    # sentence-transformers should be cleaned up to avoid confusing UI state.
    residual_packages_to_uninstall = []
    if "sentence-transformers" not in required_packages and environment.get("sentenceTransformersBroken"):
        residual_packages_to_uninstall.append("sentence-transformers")
    if "modelscope" not in required_packages and environment.get("modelscopeBroken"):
        residual_packages_to_uninstall.append("modelscope")

    if residual_packages_to_uninstall:
        try:
            append_install_log(
                session_id,
                f"[知识库] 检测到损坏的残留依赖（{', '.join(residual_packages_to_uninstall)}），将自动卸载\n",
            )
            runner(
                [str(python_executable), "-m", "pip", "uninstall", "-y", *residual_packages_to_uninstall],
                runtime_channel=runtime_channel,
                timeout=300,
            )
        except subprocess.CalledProcessError as exc:
            # Non-fatal: log and continue
            logger.warning(
                "failed to uninstall residual broken packages packages=%s error=%s",
                residual_packages_to_uninstall,
                exc,
            )

    try:
        if not use_current_python:
            install_workspace_packages(python_executable, runtime_channel=runtime_channel)
            ensure_runtime_pip(python_executable, runtime_channel)
        else:
            ensure_python_pip(python_executable, runtime_channel, runner=runner)
        result = pip_install_with_fallbacks(
            python_executable,
            runtime_channel,
            packages,
            package_label="知识库依赖",
            reinstall=reinstall or repair_reinstall,
            timeout=1800,
            runner=pip_runner,
        )
    except subprocess.CalledProcessError as exc:
        if isinstance(pip_runner, _StreamingRunner):
            pip_runner.cancel()
            finish_install_session(session_id, success=False)
        clear_environment_probe_cache(runtime_channel)
        raise HTTPException(status_code=500, detail=((exc.stderr or exc.stdout or str(exc))[-1500:])) from exc
    except HTTPException:
        if isinstance(pip_runner, _StreamingRunner):
            pip_runner.cancel()
            finish_install_session(session_id, success=False)
        clear_environment_probe_cache(runtime_channel)
        raise

    importlib.invalidate_caches()
    activate_runtime_pythonpath(runtime_channel)
    clear_environment_probe_cache(runtime_channel)
    environment = apply_knowledge_dependency_policy(detect_environment(runtime_channel), provider)
    worker = (
        build_worker(repository, current_settings, environment_info=environment)
        if should_refresh_worker
        else None
    )
    write_runtime_metadata(
        runtime_channel,
        {
            "runtimeChannel": runtime_channel,
            "python": str(python_executable),
            "chromadbInstalled": bool(environment.get("chromadbInstalled")),
            "chromadbVersion": str(environment.get("chromadbVersion") or ""),
            "chromadbBroken": bool(environment.get("chromadbBroken")),
            "sentenceTransformersInstalled": bool(environment.get("sentenceTransformersInstalled")),
            "sentenceTransformersVersion": str(environment.get("sentenceTransformersVersion") or ""),
            "sentenceTransformersBroken": bool(environment.get("sentenceTransformersBroken")),
            "modelscopeInstalled": bool(environment.get("modelscopeInstalled")),
            "modelscopeVersion": str(environment.get("modelscopeVersion") or ""),
            "modelscopeBroken": bool(environment.get("modelscopeBroken")),
            "knowledgeDependenciesReady": bool(environment.get("knowledgeDependenciesReady")),
        },
    )
    installed = bool(environment.get("knowledgeDependenciesReady"))
    if use_streaming:
        finish_install_session(session_id, success=installed)
    return {
        "installed": installed,
        "runtimeChannel": runtime_channel,
        "installSessionId": session_id,
        "stdoutTail": ((result.stdout or "") + "\n" + (result.stderr or "")).strip()[-1500:],
        "repairReinstall": repair_reinstall,
        "environment": environment,
    }, worker


# -- embedding model presets -------------------------------------------------
_EMBEDDING_MODEL_PRESETS: dict[str, str] = {
    # HuggingFace presets
    "BAAI/bge-small-zh-v1.5": "BAAI bge-small-zh 中文轻量 (HF)",
    "BAAI/bge-base-zh-v1.5": "BAAI bge-base-zh 中文通用 (HF)",
    "BAAI/bge-large-zh-v1.5": "BAAI bge-large-zh 中文最强 (HF)",
    "BAAI/bge-small-en-v1.5": "BAAI bge-small-en 英文轻量 (HF)",
    "moka-ai/m3e-base": "M3E 中文 Embedding (HF)",
    # ModelScope presets (same model IDs, loaded via MS SDK)
}

def _build_embedding_download_script(provider: str, model_name: str, hf_endpoint: str = "") -> str:
    if provider == "local_modelscope":
        return (
            "import sys, json, traceback\n"
            "try:\n"
            " from modelscope.hub.snapshot_download import snapshot_download\n"
            f" print(f'ModelScope downloading {model_name}...', file=sys.stderr)\n"
            f" path = snapshot_download('{model_name}')\n"
            " print(f'[OK] path={path}', file=sys.stderr)\n"
            " print(json.dumps({'ok': True, 'path': path}))\n"
            "except Exception as e:\n"
            " traceback.print_exc(file=sys.stderr)\n"
            " print(json.dumps({'ok': False, 'error': str(e)}))"
        )
    else:
        hf_mirror_newline = f"import os\nos.environ['HF_ENDPOINT']='{hf_endpoint}'\n" if hf_endpoint else ""
        return (
            f"{hf_mirror_newline}"
            "import sys, json, traceback\n"
            "try:\n"
            " from huggingface_hub import snapshot_download\n"
            f" print(f'HF downloading {model_name}...', file=sys.stderr)\n"
            f" path = snapshot_download('{model_name}', resume_download=True)\n"
            " print(f'[OK] path={path}', file=sys.stderr)\n"
            " print(json.dumps({'ok': True, 'path': path}))\n"
            "except Exception as e:\n"
            " traceback.print_exc(file=sys.stderr)\n"
            " print(json.dumps({'ok': False, 'error': str(e)}))"
        )


def _build_embedding_verify_script(provider: str, model_name: str, hf_endpoint: str = "") -> str:
    if provider == "local_modelscope":
        return (
            "import sys, json, traceback\n"
            "try:\n"
            " from modelscope.hub.snapshot_download import snapshot_download\n"
            " from sentence_transformers import SentenceTransformer\n"
            f" print(f'ModelScope loading {model_name}...', file=sys.stderr)\n"
            f" path = snapshot_download('{model_name}')\n"
            " m = SentenceTransformer(path, local_files_only=True)\n"
            " print('Running test encode...', file=sys.stderr)\n"
            " v = m.encode(['hello world'])\n"
            " print(f'[OK] dim={v.shape[1]}', file=sys.stderr)\n"
            " print(json.dumps({'ok': True, 'dim': int(v.shape[1])}))\n"
            "except Exception as e:\n"
            " traceback.print_exc(file=sys.stderr)\n"
            " print(json.dumps({'ok': False, 'error': str(e)}))"
        )
    else:
        hf_mirror_newline = f"import os\nos.environ['HF_ENDPOINT']='{hf_endpoint}'\n" if hf_endpoint else ""
        return (
            f"{hf_mirror_newline}"
            "import sys, json, traceback\n"
            "try:\n"
            " from sentence_transformers import SentenceTransformer\n"
            f" print(f'SentenceTransformer loading {model_name}...', file=sys.stderr)\n"
            f" m = SentenceTransformer('{model_name}')\n"
            " print('Running test encode...', file=sys.stderr)\n"
            " v = m.encode(['hello world'])\n"
            " print(f'[OK] dim={v.shape[1]}', file=sys.stderr)\n"
            " print(json.dumps({'ok': True, 'dim': int(v.shape[1])}))\n"
            "except Exception as e:\n"
            " traceback.print_exc(file=sys.stderr)\n"
            " print(json.dumps({'ok': False, 'error': str(e)}))"
        )


def download_embedding_model(
    repository: SqliteTaskRepository,
    *,
    provider: str = "local_huggingface",
    model_name: str = "BAAI/bge-small-zh-v1.5",
    hf_endpoint: str = "",
    session_id: str | None = None,
) -> dict[str, object]:
    current_settings = settings_manager.current
    runtime_channel = normalize_runtime_channel(current_settings.runtime_channel, allow_unknown_gpu=True)

    if provider == "online":
        raise HTTPException(status_code=501, detail="在线 Embedding API 暂不支持。")

    use_current_python = uses_current_service_python(runtime_channel)
    if use_current_python:
        python_executable = Path(sys.executable)
        runner = lambda command, runtime_channel, timeout=1800: run_host_command(command, timeout=timeout)
        pip_runner = runner
    else:
        runtime_dir = ensure_runtime_channel(runtime_channel)
        python_executable = runtime_python_executable(runtime_channel)
        runner = run_command
        pip_runner = _StreamingRunner(session_id) if session_id else runner
    if python_executable is None:
        raise HTTPException(status_code=500, detail="Managed runtime Python is unavailable.")

    if session_id:
        start_install_session(session_id, "Embedding 模型下载")

    try:
        if not use_current_python:
            ensure_runtime_pip(python_executable, runtime_channel)

        # Install prerequisite packages with streaming
        if provider == "local_modelscope":
            _run_pip_install(python_executable, runtime_channel, ["modelscope"], package_label="modelscope", timeout=600, runner=pip_runner)
        else:
            _run_pip_install(python_executable, runtime_channel, ["sentence-transformers>=3.0"], package_label="sentence-transformers", timeout=600, runner=pip_runner)

        download_script = _build_embedding_download_script(provider, model_name, hf_endpoint)
        result = runner(
            [str(python_executable), "-c", download_script],
            runtime_channel=runtime_channel,
            timeout=3600,
        )
        # Extract JSON from last line of stdout in case stderr mixed in
        stdout = (result.stdout or "").strip()
        data: dict[str, object] = {}
        for line in reversed(stdout.splitlines()):
            try:
                data = json.loads(line.strip())
                break
            except (json.JSONDecodeError, ValueError):
                continue
        downloaded = bool(data.get("ok"))
        if session_id:
            finish_install_session(session_id, success=downloaded)
        return {
            "downloaded": downloaded,
            "modelPath": str(data.get("path") or ""),
            "detail": "" if downloaded else (result.stderr or result.stdout or "")[-2000:],
            "stdoutTail": stdout[-3000:],
            "installSessionId": session_id,
        }
    except Exception as exc:
        if session_id:
            finish_install_session(session_id, success=False)
        detail = (getattr(exc, "stderr", "") or getattr(exc, "stdout", "") or str(exc))[-2000:]
        return {
            "downloaded": False,
            "modelPath": "",
            "detail": detail,
            "stdoutTail": detail,
            "installSessionId": session_id,
        }


def verify_embedding_model(
    repository: SqliteTaskRepository,
    *,
    provider: str = "local_huggingface",
    model_name: str = "BAAI/bge-small-zh-v1.5",
    hf_endpoint: str = "",
    api_key: str = "",
    base_url: str = "",
) -> dict[str, object]:
    current_settings = settings_manager.current
    runtime_channel = normalize_runtime_channel(current_settings.runtime_channel, allow_unknown_gpu=True)

    provider = str(provider or "local_huggingface").strip().lower()
    if provider == "siliconflow":
        from video_sum_service.knowledge.index_service import KnowledgeIndexService

        effective_api_key = str(api_key or "").strip()
        if is_blank_or_masked_secret(effective_api_key):
            effective_api_key = str(current_settings.siliconflow_embedding_api_key or "").strip()
        if not effective_api_key:
            return {
                "verified": False,
                "dimension": 0,
                "detail": "硅基流动 Embedding API Key 未配置。",
                "stdoutTail": "",
            }
        effective_base_url = str(base_url or current_settings.siliconflow_embedding_base_url or "").strip()
        effective_model = str(model_name or current_settings.siliconflow_embedding_model or "").strip()
        if not effective_base_url or not effective_model:
            return {
                "verified": False,
                "dimension": 0,
                "detail": "硅基流动 Embedding Base URL 或模型名称未配置。",
                "stdoutTail": "",
            }
        probe_settings = current_settings.model_copy(
            update={
                "knowledge_embedding_provider": "siliconflow",
                "siliconflow_embedding_api_key": effective_api_key,
                "siliconflow_embedding_base_url": effective_base_url,
                "siliconflow_embedding_model": effective_model,
                "knowledge_embedding_model": effective_model,
            }
        )
        try:
            service = KnowledgeIndexService(repository or SimpleNamespace(), probe_settings, model_name=effective_model)
            vectors = service._embed_texts_siliconflow(["BiliSum embedding connectivity test"])
            dimension = len(vectors[0]) if vectors else 0
            if dimension <= 0:
                return {
                    "verified": False,
                    "dimension": 0,
                    "detail": "硅基流动 Embedding API 返回向量为空。",
                    "stdoutTail": "",
                }
            return {
                "verified": True,
                "dimension": dimension,
                "detail": f"硅基流动向量模型可用，向量维度 {dimension}",
                "stdoutTail": f"[OK] SiliconFlow embeddings model={effective_model} dim={dimension}",
            }
        except HTTPException as exc:
            return {
                "verified": False,
                "dimension": 0,
                "detail": str(exc.detail),
                "stdoutTail": str(exc.detail),
            }

    if provider == "online":
        raise HTTPException(status_code=501, detail="在线 Embedding API 暂不支持。")

    use_current_python = uses_current_service_python(runtime_channel)
    if use_current_python:
        python_executable = Path(sys.executable)
        runner = lambda command, runtime_channel, timeout=1800: run_host_command(command, timeout=timeout)
    else:
        python_executable = runtime_python_executable(runtime_channel)
        runner = run_command
    if python_executable is None:
        raise HTTPException(status_code=500, detail="Managed runtime Python is unavailable.")

    verify_script = _build_embedding_verify_script(provider, model_name, hf_endpoint)
    try:
        result = runner(
            [str(python_executable), "-c", verify_script],
            runtime_channel=runtime_channel,
            timeout=600,
        )
        stdout = (result.stdout or "").strip()
        data: dict[str, object] = {}
        for line in reversed(stdout.splitlines()):
            try:
                data = json.loads(line.strip())
                break
            except (json.JSONDecodeError, ValueError):
                continue
        if data.get("ok") and data.get("dim"):
            return {"verified": True, "dimension": data.get("dim"), "detail": f"模型可用，向量维度 {data['dim']}", "stdoutTail": stdout[-3000:]}
        return {"verified": False, "dimension": 0, "detail": (result.stderr or stdout or "unknown error")[-2000:], "stdoutTail": stdout[-3000:]}
    except Exception as exc:
        detail = (getattr(exc, "stderr", "") or getattr(exc, "stdout", "") or str(exc))[-2000:]
        return {"verified": False, "dimension": 0, "detail": detail, "stdoutTail": detail}


def get_embedding_model_presets() -> dict[str, str]:
    """Return the available embedding model presets."""
    return dict(_EMBEDDING_MODEL_PRESETS)


# -- Smart knowledge dependency management -----------------------------------

def get_knowledge_requirements(provider: str) -> dict[str, object]:
    """Get required packages based on embedding provider."""
    provider = str(provider or "").strip().lower()

    if provider == "siliconflow":
        return {"required": ["chromadb"], "optional": [], "preinstalled": []}
    elif provider == "local_modelscope":
        return {"required": ["chromadb", "sentence-transformers", "modelscope"], "optional": [], "preinstalled": []}
    else:
        return {"required": ["chromadb", "sentence-transformers"], "optional": [], "preinstalled": []}


def check_package_dependencies(package: str) -> list[str]:
    """Check what features depend on this package."""
    dependencies = []
    settings = settings_manager.current
    
    if package == "sentence-transformers":
        provider = str(getattr(settings, "knowledge_embedding_provider", "")).lower()
        if provider in ("local_huggingface", "local_modelscope"):
            dependencies.append("知识库（本地向量模型）")
    
    if package == "modelscope":
        provider = str(getattr(settings, "knowledge_embedding_provider", "")).lower()
        if provider == "local_modelscope":
            dependencies.append("知识库（ModelScope 向量模型）")
        funasr_hub = str(getattr(settings, "funasr_hub", "")).lower()
        if funasr_hub == "ms":
            dependencies.append("FunASR（ModelScope Hub）")
    
    if package == "chromadb":
        if getattr(settings, "knowledge_enabled", False):
            dependencies.append("知识库（向量数据库）")
    
    return dependencies


def uninstall_packages(packages: list[str], runtime_channel: str | None = None) -> dict[str, object]:
    """Uninstall specified packages from runtime."""
    runtime_channel = normalize_runtime_channel(runtime_channel or settings_manager.current.runtime_channel, allow_unknown_gpu=True)
    
    use_current_python = uses_current_service_python(runtime_channel)
    if use_current_python:
        python_executable = Path(sys.executable)
        runner = lambda command, runtime_channel, timeout=1800: run_host_command(command, timeout=timeout)
    else:
        ensure_runtime_channel(runtime_channel)
        python_executable = runtime_python_executable(runtime_channel)
        runner = run_command
    
    if python_executable is None:
        raise HTTPException(status_code=500, detail="Runtime unavailable.")
    
    try:
        removed_before = cleanup_invalid_runtime_distributions(runtime_channel)
        result = runner([str(python_executable), "-m", "pip", "uninstall", "-y", *packages], runtime_channel, timeout=600)
        removed_after = cleanup_invalid_runtime_distributions(runtime_channel)
        clear_environment_probe_cache(runtime_channel)
        cleanup_lines = []
        if removed_before:
            cleanup_lines.append(f"卸载前已清理 pip 残留：{', '.join(removed_before)}")
        if removed_after:
            cleanup_lines.append(f"卸载后已清理 pip 残留：{', '.join(removed_after)}")
        stdout = "\n".join(part for part in [*cleanup_lines, result.stdout or ""] if part)
        return {"success": True, "packages": packages, "stdout": stdout, "stderr": result.stderr or ""}
    except subprocess.CalledProcessError as exc:
        cleanup_invalid_runtime_distributions(runtime_channel)
        raise HTTPException(status_code=500, detail=f"卸载失败：{(exc.stderr or exc.stdout or str(exc))[-1000:]}") from exc
