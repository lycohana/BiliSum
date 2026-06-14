from __future__ import annotations

import ctypes
from contextlib import contextmanager
import json
import os
import shutil
import sys
import threading
import time

# Guard: torch's _load_dll_libraries calls os.add_dll_directory() for
# torch/lib, which can fail with WinError 206 on portable Python builds
# even though the path is well under MAX_PATH.  The directory is already
# on PATH (set by runtime_subprocess_env / activate_runtime_dll_directories),
# so swallowing this error is safe — DLL loading via PATH still works.
_original_add_dll_directory = getattr(os, "add_dll_directory", None)
if _original_add_dll_directory is not None:
    def _safe_add_dll_directory(path):
        try:
            return _original_add_dll_directory(path)
        except (FileNotFoundError, OSError):
            return None
    os.add_dll_directory = _safe_add_dll_directory
from pathlib import Path


APP_SLUG = "bilisum"
LEGACY_APP_SLUG = "briefvid"
DOCKER_DATA_ROOT = Path("/data")
_DLL_DIRECTORY_HANDLES: dict[str, object] = {}
_DLL_DIRECTORY_LOCK = threading.Lock()
_LEGACY_APP_DATA_MIGRATION_DONE = False
_RUNTIME_APP_PACKAGE_PREFIXES: tuple[str, ...] = (
    "video_sum_",
)


def _env_flag(name: str) -> bool:
    value = os.environ.get(name, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def is_running_in_docker() -> bool:
    return _env_flag("VIDEO_SUM_DOCKER") or Path("/.dockerenv").exists()


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def bundled_root() -> Path:
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            return Path(meipass).resolve()
        return Path(sys.executable).resolve().parent
    return repo_root()


def web_static_dir() -> Path:
    override = os.environ.get("VIDEO_SUM_WEB_STATIC_DIR", "").strip()
    if override:
        return Path(override).resolve()
    if is_frozen():
        return bundled_root() / "web" / "static"
    return repo_root() / "apps" / "web" / "static"


def bundled_bin_dir() -> Path:
    if is_frozen():
        return bundled_root() / "bin"
    return repo_root() / "bin"


def local_appdata_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        return Path(local_appdata)
    if os.name != "nt":
        xdg_data_home = os.environ.get("XDG_DATA_HOME")
        if xdg_data_home:
            return Path(xdg_data_home)
        return Path.home() / ".local" / "share"
    return Path.home() / "AppData" / "Local"


def _copy_missing_tree(source: Path, destination: Path, *, merge_depth: int = 0) -> None:
    if not source.exists():
        return
    if source.is_dir():
        destination.mkdir(parents=True, exist_ok=True)
        for child in source.iterdir():
            child_destination = destination / child.name
            if child_destination.exists():
                if child.is_dir() and merge_depth < 1:
                    _copy_missing_tree(child, child_destination, merge_depth=merge_depth + 1)
                continue
            _copy_missing_tree(child, child_destination)
        return
    if source.is_file() and not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def migrate_legacy_app_data_root() -> None:
    global _LEGACY_APP_DATA_MIGRATION_DONE
    if _LEGACY_APP_DATA_MIGRATION_DONE:
        return
    _LEGACY_APP_DATA_MIGRATION_DONE = True
    if is_running_in_docker() or os.environ.get("VIDEO_SUM_APP_DATA_ROOT", "").strip():
        return

    source = local_appdata_dir() / LEGACY_APP_SLUG
    destination = local_appdata_dir() / APP_SLUG
    if not source.exists() or source == destination:
        return
    try:
        _copy_missing_tree(source, destination)
    except OSError:
        return


def app_data_root() -> Path:
    override = os.environ.get("VIDEO_SUM_APP_DATA_ROOT", "").strip()
    if override:
        return Path(override).resolve()
    if is_running_in_docker():
        return DOCKER_DATA_ROOT
    migrate_legacy_app_data_root()
    return local_appdata_dir() / APP_SLUG


def default_data_dir() -> Path:
    if is_running_in_docker():
        return app_data_root()
    return app_data_root() / "data"


def default_cache_dir() -> Path:
    return default_data_dir() / "cache"


def default_tasks_dir() -> Path:
    return default_data_dir() / "tasks"


def default_database_url() -> str:
    return f"sqlite:///{(default_data_dir() / 'video_sum.db').as_posix()}"


def default_host() -> str:
    if is_running_in_docker():
        return "0.0.0.0"
    return "127.0.0.1"


def managed_runtime_root() -> Path:
    return app_data_root() / "runtime"


def managed_runtime_dir(runtime_channel: str) -> Path:
    """Get managed runtime directory for a channel, with path traversal protection."""
    root = managed_runtime_root().resolve()
    candidate = (root / runtime_channel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Invalid runtime channel path: {runtime_channel!r}") from exc
    return candidate


def log_dir() -> Path:
    return app_data_root() / "logs"


def service_log_path() -> Path:
    return log_dir() / "service.log"


def bundled_runtime_seed_dir() -> Path:
    return bundled_root() / "runtime" / "base"


def runtime_seed_available() -> bool:
    return bundled_runtime_seed_dir().exists()


def read_runtime_metadata(target_dir: Path) -> dict[str, object]:
    metadata_path = target_dir / "video_sum_runtime.json"
    if not metadata_path.exists():
        return {}
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def bundled_runtime_seed_metadata() -> dict[str, object]:
    return read_runtime_metadata(bundled_runtime_seed_dir())


def runtime_python_candidates(runtime_dir: Path) -> list[Path]:
    """Return possible managed runtime Python interpreters for each platform."""
    return [
        runtime_dir / "python.exe",
        runtime_dir / "Scripts" / "python.exe",
        runtime_dir / "bin" / "python",
        runtime_dir / "bin" / "python3",
        runtime_dir / "python",
    ]


def runtime_scripts_dir(runtime_dir: Path) -> Path:
    candidates = [runtime_dir / "Scripts", runtime_dir / "bin"]
    for scripts_dir in candidates:
        if scripts_dir.exists():
            return scripts_dir
    return runtime_dir


def runtime_site_packages_dir(runtime_channel: str) -> Path:
    return _runtime_site_packages_dir_for(managed_runtime_dir(runtime_channel))


def _runtime_site_packages_dir_for(runtime_dir: Path) -> Path:
    legacy_dir = runtime_dir / "Lib" / "site-packages"
    if legacy_dir.exists():
        return legacy_dir
    lib_dir = runtime_dir / "lib"
    if lib_dir.exists():
        versioned_dirs = sorted(lib_dir.glob("python*/site-packages"))
        if versioned_dirs:
            return versioned_dirs[-1]
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    return lib_dir / version / "site-packages"


def _runtime_site_package_key(item: Path) -> str:
    name = item.name
    for suffix in (".dist-info", ".egg-info"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    if "-" in name:
        parts = name.split("-")
        version_index = next((index for index, part in enumerate(parts) if part[:1].isdigit()), len(parts))
        name = "-".join(parts[:version_index]) or parts[0]
    return name.replace("-", "_").lower()


def _runtime_extension_should_preserve(item: Path) -> bool:
    key = _runtime_site_package_key(item)
    return not any(key.startswith(prefix) for prefix in _RUNTIME_APP_PACKAGE_PREFIXES)


def _restore_runtime_extensions(runtime_dir: Path, backup_dir: Path) -> None:
    source = backup_dir / "site-packages"
    if not source.exists():
        return
    target = _runtime_site_packages_dir_for(runtime_dir)
    target.mkdir(parents=True, exist_ok=True)
    target_package_keys = {_runtime_site_package_key(item) for item in target.iterdir()}
    for item in source.iterdir():
        if not _runtime_extension_should_preserve(item):
            continue
        item_key = _runtime_site_package_key(item)
        if item_key in target_package_keys:
            continue
        destination = target / item.name
        if destination.exists():
            continue
        shutil.move(str(item), str(destination))


def _move_preserved_runtime_extensions(source_runtime_dir: Path, target_runtime_dir: Path) -> list[str]:
    source = _runtime_site_packages_dir_for(source_runtime_dir)
    if not source.exists():
        return []
    target = _runtime_site_packages_dir_for(target_runtime_dir)
    target.mkdir(parents=True, exist_ok=True)
    target_package_keys = {_runtime_site_package_key(item) for item in target.iterdir()}
    moved_names: list[str] = []
    for item in source.iterdir():
        if not _runtime_extension_should_preserve(item):
            continue
        if _runtime_site_package_key(item) in target_package_keys:
            continue
        destination = target / item.name
        if destination.exists():
            continue
        shutil.move(str(item), str(destination))
        moved_names.append(item.name)
    return moved_names


def _move_runtime_site_package_items(source_runtime_dir: Path, target_runtime_dir: Path, item_names: list[str]) -> None:
    source = _runtime_site_packages_dir_for(source_runtime_dir)
    target = _runtime_site_packages_dir_for(target_runtime_dir)
    target.mkdir(parents=True, exist_ok=True)
    for item_name in reversed(item_names):
        source_item = source / item_name
        target_item = target / item_name
        if source_item.exists() and not target_item.exists():
            shutil.move(str(source_item), str(target_item))


def _restore_interrupted_runtime_refresh(runtime_dir: Path, backup_dir: Path) -> None:
    if not backup_dir.exists():
        return
    if runtime_dir.exists():
        shutil.rmtree(runtime_dir)
    shutil.move(str(backup_dir), str(runtime_dir))


def runtime_stdlib_dir(runtime_channel: str) -> Path:
    return managed_runtime_dir(runtime_channel) / "stdlib"


def runtime_pythonpath_dirs(runtime_channel: str) -> list[Path]:
    runtime_dir = managed_runtime_dir(runtime_channel)
    candidates: list[Path] = []
    pythonpath_file = runtime_dir / "pythonpath.pth"
    if pythonpath_file.exists():
        try:
            for raw_line in pythonpath_file.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                entry = Path(line)
                candidates.append(entry if entry.is_absolute() else runtime_dir / entry)
        except OSError:
            pass
    candidates.extend(
        [
            runtime_stdlib_dir(runtime_channel),
            runtime_dir / "DLLs",
            runtime_site_packages_dir(runtime_channel),
            runtime_dir,
        ]
    )
    unique_dirs: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        key = str(resolved).lower()
        if key in seen:
            continue
        seen.add(key)
        unique_dirs.append(resolved)
    return unique_dirs


def runtime_library_dirs(runtime_channel: str) -> list[Path]:
    runtime_dir = managed_runtime_dir(runtime_channel)
    candidates: list[Path] = []

    python_executable = runtime_python_executable(runtime_channel)
    if python_executable is not None:
        candidates.append(python_executable.parent)
    portable_dlls_dir = runtime_dir / "DLLs"
    if portable_dlls_dir.exists():
        candidates.append(portable_dlls_dir)
    runtime_lib_dir = runtime_dir / "lib"
    if runtime_lib_dir.exists():
        candidates.append(runtime_lib_dir)
    candidates.append(runtime_scripts_dir(runtime_dir))

    torch_lib_dir = runtime_site_packages_dir(runtime_channel) / "torch" / "lib"
    if torch_lib_dir.exists():
        candidates.append(torch_lib_dir)
    torchaudio_lib_dir = runtime_site_packages_dir(runtime_channel) / "torchaudio" / "lib"
    if torchaudio_lib_dir.exists():
        candidates.append(torchaudio_lib_dir)

    nvidia_root = runtime_site_packages_dir(runtime_channel) / "nvidia"
    if nvidia_root.exists():
        for bin_dir in sorted(nvidia_root.rglob("bin")):
            if bin_dir.is_dir():
                candidates.append(bin_dir)

    cuda_path = os.environ.get("CUDA_PATH")
    if cuda_path:
        cuda_bin_dir = Path(cuda_path) / "bin"
        if cuda_bin_dir.exists():
            candidates.append(cuda_bin_dir)

    unique_dirs: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        key = str(resolved).lower()
        if not candidate.exists() or key in seen:
            continue
        seen.add(key)
        unique_dirs.append(resolved)
    return unique_dirs


def runtime_worker_executable(runtime_channel: str) -> Path | None:
    runtime_dir = managed_runtime_dir(runtime_channel)
    scripts_dir = runtime_scripts_dir(runtime_dir)
    candidates = [
        scripts_dir / "video-sum-transcribe-worker.exe",
        scripts_dir / "video-sum-transcribe-worker",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def runtime_python_executable(runtime_channel: str) -> Path | None:
    """获取托管运行时 Python 解释器路径。
    
    优先查找根目录的 python.exe（可重定位的解释器），
    其次才是 Scripts 目录的 venv 启动器。
    
    注意：不再回退到 sys.executable，因为在生产环境中这可能导致
    使用开发环境的 Python 路径。
    """
    runtime_dir = managed_runtime_dir(runtime_channel)
    for candidate in runtime_python_candidates(runtime_dir):
        if candidate.exists():
            return candidate
    return None


def ffmpeg_location() -> Path | None:
    """返回 ffmpeg 可执行文件的路径（不是目录）。"""
    ffmpeg_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    ffprobe_name = "ffprobe.exe" if os.name == "nt" else "ffprobe"

    def usable_ffmpeg_from_dir(directory: Path) -> Path | None:
        ffmpeg_exe = directory / ffmpeg_name
        ffprobe_exe = directory / ffprobe_name
        if ffmpeg_exe.exists() and ffprobe_exe.exists():
            return ffmpeg_exe
        return None

    # 1. 环境变量指定的目录
    env_dir = os.environ.get("VIDEO_SUM_FFMPEG_DIR")
    if env_dir:
        candidate = Path(env_dir)
        if candidate.exists():
            ffmpeg_exe = usable_ffmpeg_from_dir(candidate)
            if ffmpeg_exe is not None:
                return ffmpeg_exe

    # 2. 打包后的 bin 目录
    bundled_dir = bundled_bin_dir()
    if bundled_dir.exists():
        ffmpeg_exe = usable_ffmpeg_from_dir(bundled_dir)
        if ffmpeg_exe is not None:
            return ffmpeg_exe

    # 3. 系统 PATH 中的 ffmpeg
    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    if ffmpeg_path and ffprobe_path and Path(ffmpeg_path).resolve().parent == Path(ffprobe_path).resolve().parent:
        return Path(ffmpeg_path).resolve()

    return None


def prepend_runtime_path(runtime_channel: str) -> None:
    paths = [str(path) for path in runtime_library_dirs(runtime_channel)]
    ffmpeg_exe = ffmpeg_location()
    if ffmpeg_exe is not None:
        # 添加 ffmpeg 所在目录到 PATH
        paths.append(str(ffmpeg_exe.parent))

    current_path = os.environ.get("PATH", "")
    for value in reversed(paths):
        if value and value not in current_path:
            current_path = f"{value}{os.pathsep}{current_path}" if current_path else value
    os.environ["PATH"] = current_path
    activate_runtime_dll_directories(runtime_channel)


def activate_runtime_pythonpath(runtime_channel: str) -> None:
    runtime_paths = runtime_pythonpath_dirs(runtime_channel)
    if not runtime_paths:
        return

    runtime_root = managed_runtime_root()
    try:
        resolved_runtime_root = runtime_root.resolve()
    except OSError:
        resolved_runtime_root = runtime_root

    filtered: list[str] = []
    for entry in sys.path:
        if not entry:
            filtered.append(entry)
            continue
        try:
            entry_path = Path(entry).resolve()
        except OSError:
            filtered.append(entry)
            continue
        try:
            inside_runtime_root = entry_path.is_relative_to(resolved_runtime_root)
        except ValueError:
            inside_runtime_root = False
        is_managed_runtime_path = inside_runtime_root and (
            entry_path.name in {"stdlib", "DLLs"}
            or entry_path.parent == resolved_runtime_root
            or entry_path.name == "site-packages"
        )
        if not is_managed_runtime_path:
            filtered.append(entry)

    for runtime_path in runtime_paths:
        value = str(runtime_path)
        if value not in filtered:
            filtered.append(value)
    sys.path[:] = filtered


def activate_runtime_dll_directories(runtime_channel: str) -> None:
    add_dll_directory = getattr(os, "add_dll_directory", None)
    if add_dll_directory is None:
        return

    try:
        resolved_runtime_root = managed_runtime_root().resolve()
    except OSError:
        resolved_runtime_root = managed_runtime_root()

    desired_dirs: list[Path] = runtime_library_dirs(runtime_channel)
    desired_keys: set[str] = set()
    for directory in desired_dirs:
        try:
            resolved = directory.resolve()
        except OSError:
            resolved = directory
        desired_keys.add(str(resolved).lower())

    with _DLL_DIRECTORY_LOCK:
        for key, handle in list(_DLL_DIRECTORY_HANDLES.items()):
            path = Path(key)
            try:
                inside_runtime_root = path.is_relative_to(resolved_runtime_root)
            except ValueError:
                inside_runtime_root = False
            if inside_runtime_root and key not in desired_keys:
                close = getattr(handle, "close", None)
                if close is not None:
                    close()
                _DLL_DIRECTORY_HANDLES.pop(key, None)

        for directory in desired_dirs:
            if not directory.exists():
                continue
            try:
                resolved = directory.resolve()
            except OSError:
                resolved = directory
            key = str(resolved).lower()
            if key in _DLL_DIRECTORY_HANDLES:
                continue
            try:
                _DLL_DIRECTORY_HANDLES[key] = add_dll_directory(str(resolved))
            except OSError:
                continue


def _robust_shutil_move(src: str, dst: str) -> None:
    for attempt in range(3):
        try:
            shutil.move(src, dst)
            return
        except OSError:
            if attempt == 2:
                raise
            time.sleep(1)


def _robust_shutil_copytree(src: Path, dst: Path, **kwargs: object) -> None:
    for attempt in range(3):
        try:
            shutil.copytree(src, dst, **kwargs)
            return
        except OSError:
            if attempt == 2:
                raise
            time.sleep(1)


def bootstrap_managed_runtime(runtime_channel: str = "base") -> Path | None:
    runtime_dir = managed_runtime_dir(runtime_channel)
    if runtime_channel != "base" or not is_frozen() or not runtime_seed_available():
        if runtime_python_executable(runtime_channel) is not None:
            return runtime_dir
        return None

    seed_dir = bundled_runtime_seed_dir()
    runtime_dir.parent.mkdir(parents=True, exist_ok=True)
    refresh_backup_dir = runtime_dir.parent / f".{runtime_dir.name}-refresh-backup"
    legacy_extension_backup_dir = runtime_dir.parent / f".{runtime_dir.name}-extension-backup"
    _restore_interrupted_runtime_refresh(runtime_dir, refresh_backup_dir)
    if runtime_dir.exists() and legacy_extension_backup_dir.exists():
        _restore_runtime_extensions(runtime_dir, legacy_extension_backup_dir)
        shutil.rmtree(legacy_extension_backup_dir)

    seed_metadata = bundled_runtime_seed_metadata()
    runtime_metadata = read_runtime_metadata(runtime_dir)
    runtime_ready = runtime_python_executable(runtime_channel) is not None
    seed_version = str(seed_metadata.get("appVersion") or "")
    runtime_version = str(runtime_metadata.get("appVersion") or "")
    can_preserve_extensions = (
        runtime_metadata.get("runtimeLayout") == seed_metadata.get("runtimeLayout")
        and runtime_metadata.get("pythonVersion") == seed_metadata.get("pythonVersion")
    )
    requires_refresh = (
        not runtime_ready
        or runtime_version != seed_version
        or runtime_metadata.get("runtimeLayout") != seed_metadata.get("runtimeLayout")
        or runtime_metadata.get("pythonVersion") != seed_metadata.get("pythonVersion")
    )
    if not requires_refresh:
        return runtime_dir

    # Always try to preserve extensions across refreshes, even when Python
    # version or layout differs.  Silently deleting user-installed packages
    # (chromadb, sentence-transformers, etc.) is worse than preserving
    # potentially incompatible ones — pip can repair them afterwards.
    should_preserve_extensions = (
        runtime_ready and runtime_dir.exists() and (runtime_metadata or seed_metadata)
    )

    moved_extension_names: list[str] = []
    if runtime_dir.exists():
        _robust_shutil_move(str(runtime_dir), str(refresh_backup_dir))
    try:
        _robust_shutil_copytree(seed_dir, runtime_dir, dirs_exist_ok=True)
        if should_preserve_extensions and refresh_backup_dir.exists():
            moved_extension_names = _move_preserved_runtime_extensions(refresh_backup_dir, runtime_dir)
        if legacy_extension_backup_dir.exists():
            _restore_runtime_extensions(runtime_dir, legacy_extension_backup_dir)
    except Exception:
        if refresh_backup_dir.exists():
            if runtime_dir.exists() and moved_extension_names:
                _move_runtime_site_package_items(runtime_dir, refresh_backup_dir, moved_extension_names)
            if runtime_dir.exists():
                shutil.rmtree(runtime_dir)
            _robust_shutil_move(str(refresh_backup_dir), str(runtime_dir))
        raise
    finally:
        if refresh_backup_dir.exists():
            shutil.rmtree(refresh_backup_dir)
        if legacy_extension_backup_dir.exists() and runtime_dir.exists():
            shutil.rmtree(legacy_extension_backup_dir)
    return runtime_dir


def runtime_metadata_path(runtime_channel: str) -> Path:
    return managed_runtime_dir(runtime_channel) / "video_sum_runtime.json"


def write_runtime_metadata(runtime_channel: str, payload: dict[str, object]) -> None:
    target = runtime_metadata_path(runtime_channel)
    target.parent.mkdir(parents=True, exist_ok=True)
    # target.parent = managed_runtime_dir(channel); read_runtime_metadata appends
    # "video_sum_runtime.json" to the directory path, so passing target.parent
    # correctly reads the existing metadata from the same file being written.
    current = read_runtime_metadata(target.parent)
    target.write_text(
        json.dumps({**current, **payload}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _get_windows_dll_directory() -> str | None:
    if os.name != "nt":
        return None
    kernel32 = ctypes.windll.kernel32
    kernel32.GetDllDirectoryW.restype = ctypes.c_uint32
    needed = kernel32.GetDllDirectoryW(0, None)
    if needed == 0:
        return None
    buffer = ctypes.create_unicode_buffer(needed)
    kernel32.GetDllDirectoryW(needed, buffer)
    return buffer.value


def _set_windows_dll_directory(path: str | None) -> None:
    if os.name != "nt":
        return
    kernel32 = ctypes.windll.kernel32
    kernel32.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
    kernel32.SetDllDirectoryW.restype = ctypes.c_int
    if not kernel32.SetDllDirectoryW(path):
        raise OSError(ctypes.get_last_error(), "SetDllDirectoryW failed")


@contextmanager
def sanitized_subprocess_dll_search():
    if os.name != "nt" or not is_frozen():
        yield
        return

    previous = _get_windows_dll_directory()
    _set_windows_dll_directory(None)
    try:
        yield
    finally:
        _set_windows_dll_directory(previous)
