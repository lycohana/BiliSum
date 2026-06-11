from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import venv


ROOT = Path(__file__).resolve().parents[2]
BUILD_ROOT = ROOT / "build" / "pyinstaller"
BUILD_VENV_DIR = BUILD_ROOT / "build-venv"
RUNTIME_DIR = BUILD_ROOT / "runtime" / "base"
BIN_DIR = BUILD_ROOT / "bin"
SPEC_PATH = ROOT / "packaging" / "pyinstaller" / "bilisum.spec"
VERSION_FILE = ROOT / "VERSION"
IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
PROJECT_FFMPEG_DIR = ROOT / "tools" / "ffmpeg" / ("win-x64" if IS_WINDOWS else "macos")


def executable_name(name: str) -> str:
    return f"{name}.exe" if IS_WINDOWS else name


def venv_python_candidates(venv_dir: Path) -> list[Path]:
    return [
        venv_dir / "Scripts" / "python.exe",
        venv_dir / "python.exe",
        venv_dir / "bin" / "python",
        venv_dir / "bin" / "python3",
        venv_dir / "python",
    ]


def site_packages_dir(python_exe: Path) -> Path:
    result = subprocess.run(
        [str(python_exe), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return Path(result.stdout.strip())


def run(command: list[str], cwd: Path | None = None) -> None:
    subprocess.run(command, cwd=cwd or ROOT, check=True)


def remove_tree(target: Path, retries: int = 6, delay: float = 0.5) -> None:
    if not target.exists():
        return
    last_error: OSError | None = None
    for _ in range(retries):
        try:
            shutil.rmtree(target)
            return
        except OSError as error:
            last_error = error
            time.sleep(delay)
    if last_error is not None:
        raise last_error


def ensure_python_version() -> None:
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("PyInstaller onedir build must run with Python 3.12.")


def runtime_python() -> Path:
    for candidate in venv_python_candidates(RUNTIME_DIR):
        if candidate.exists():
            return candidate
    raise FileNotFoundError("Managed runtime python executable was not created.")


def build_python() -> Path:
    for candidate in venv_python_candidates(BUILD_VENV_DIR):
        if candidate.exists():
            return candidate
    raise FileNotFoundError("Build virtualenv python executable was not created.")


def create_build_venv() -> None:
    if BUILD_VENV_DIR.exists():
        remove_tree(BUILD_VENV_DIR)
    builder = venv.EnvBuilder(with_pip=True, clear=True, symlinks=False)
    builder.create(BUILD_VENV_DIR)


def create_runtime_seed() -> None:
    if RUNTIME_DIR.exists():
        remove_tree(RUNTIME_DIR)
    RUNTIME_DIR.parent.mkdir(parents=True, exist_ok=True)

    builder = venv.EnvBuilder(with_pip=True, clear=True, symlinks=False)
    builder.create(RUNTIME_DIR)
    python_exe = runtime_python()
    run([str(python_exe), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"])
    run(
        [
            str(python_exe),
            "-m",
            "pip",
            "install",
            str(ROOT / "packages" / "infra"),
            str(ROOT / "packages" / "core"),
            str(ROOT / "apps" / "service"),
            "chromadb>=1.0.0",
        ]
    )
    # 清理运行时环境中不必要的包，减小打包体积
    cleanup_runtime_site_packages(python_exe)
    # 清理 direct_url.json 元数据，避免生产环境路径无效
    cleanup_direct_url_metadata(site_packages_dir(python_exe))
    if IS_WINDOWS:
        make_portable_python_runtime(RUNTIME_DIR)
    elif IS_MACOS:
        make_portable_macos_runtime(RUNTIME_DIR)
    write_runtime_seed_metadata(RUNTIME_DIR)


def base_python_root() -> Path:
    """返回构建所依赖的基础 Python 安装目录，而不是 venv 启动器目录。"""
    candidates = [
        Path(sys.base_prefix),
        Path(getattr(sys, "_base_executable", "")).resolve().parent if getattr(sys, "_base_executable", "") else None,
        Path(sys.executable).resolve().parent,
    ]
    for candidate in candidates:
        if candidate is not None and (candidate / "python.exe").exists():
            return candidate
    raise FileNotFoundError("Unable to locate the base Python installation for portable runtime packaging.")


def macos_base_python_root() -> Path:
    """Return the uv-managed CPython install root used to build the venv."""
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, pathlib, sysconfig; "
                "print(json.dumps({'base': sysconfig.get_config_var('base'), "
                "'stdlib': sysconfig.get_path('stdlib'), "
                "'platstdlib': sysconfig.get_path('platstdlib')}))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    payload = json.loads(probe.stdout.strip() or "{}")
    for key in ("base", "platstdlib", "stdlib"):
        value = str(payload.get(key) or "")
        if not value:
            continue
        candidate = Path(value).resolve()
        if key != "base":
            candidate = candidate.parents[1] if candidate.name.startswith("python") else candidate.parent
        if (candidate / "bin" / f"python{sys.version_info.major}.{sys.version_info.minor}").exists():
            return candidate
    raise FileNotFoundError("Unable to locate the uv-managed macOS Python runtime root.")


def make_portable_macos_runtime(runtime_dir: Path) -> None:
    """Bundle uv-managed macOS CPython pieces needed after the runtime is relocated."""
    python_root = macos_base_python_root()
    portable_stdlib_dir = runtime_dir / "stdlib"
    portable_lib_dir = runtime_dir / "lib"
    python_version = f"python{sys.version_info.major}.{sys.version_info.minor}"

    source_stdlib = python_root / "lib" / python_version
    if not source_stdlib.exists():
        raise FileNotFoundError(f"Base Python stdlib not found: {source_stdlib}")
    shutil.copytree(
        source_stdlib,
        portable_stdlib_dir,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("site-packages", "__pycache__"),
    )

    source_lib = python_root / "lib"
    portable_lib_dir.mkdir(parents=True, exist_ok=True)
    for pattern in ("libpython*.dylib", "libpython*.a"):
        for source in source_lib.glob(pattern):
            shutil.copy2(source, portable_lib_dir / source.name)

    site_packages = site_packages_dir(runtime_python())
    pyvenv_cfg = runtime_dir / "pyvenv.cfg"
    if pyvenv_cfg.exists():
        pyvenv_cfg.unlink()

    (runtime_dir / "pythonpath.pth").write_text(
        "\n".join(
            [
                str(portable_stdlib_dir.relative_to(runtime_dir)),
                str(site_packages.relative_to(runtime_dir)),
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(f"Prepared portable macOS Python runtime from {python_root} -> {runtime_dir}")
    verify_portable_macos_runtime(runtime_dir)


def make_portable_python_runtime(runtime_dir: Path) -> None:
    """把 venv seed 补齐成可重定位的 Windows CPython runtime。

    Windows 上的 venv 启动器依赖 pyvenv.cfg 中记录的基础解释器位置，
    直接复制到其他机器后容易指回构建机路径。这里改为把基础 CPython
    可执行文件、运行时 DLL 和标准库一起复制到 runtime 根目录，并用
    ``python312._pth`` 显式指定 stdlib 与 site-packages，避免再依赖
    ``Scripts\\python.exe`` 或 ``pyvenv.cfg``。
    """
    python_root = base_python_root()
    portable_stdlib_dir = runtime_dir / "stdlib"
    portable_dlls_dir = runtime_dir / "DLLs"

    binaries = [
        "python.exe",
        "pythonw.exe",
        "python3.dll",
        f"python{sys.version_info.major}{sys.version_info.minor}.dll",
        "vcruntime140.dll",
        "vcruntime140_1.dll",
    ]
    for name in binaries:
        source = python_root / name
        if source.exists():
            shutil.copy2(source, runtime_dir / name)

    source_lib = python_root / "Lib"
    if not source_lib.exists():
        raise FileNotFoundError(f"Base Python stdlib not found: {source_lib}")
    shutil.copytree(source_lib, portable_stdlib_dir, dirs_exist_ok=True)

    source_dlls = python_root / "DLLs"
    if source_dlls.exists():
        shutil.copytree(source_dlls, portable_dlls_dir, dirs_exist_ok=True)

    pyvenv_cfg = runtime_dir / "pyvenv.cfg"
    if pyvenv_cfg.exists():
        pyvenv_cfg.unlink()

    pth_name = f"python{sys.version_info.major}{sys.version_info.minor}._pth"
    (runtime_dir / pth_name).write_text(
        "stdlib\n"
        "DLLs\n"
        "Lib\\site-packages\n"
        "import site\n",
        encoding="utf-8",
    )
    print(f"Prepared portable Python runtime from {python_root} -> {runtime_dir}")
    verify_portable_python_runtime(runtime_dir)


def verify_portable_python_runtime(runtime_dir: Path) -> None:
    python_exe = runtime_dir / "python.exe"
    if not python_exe.exists():
        raise FileNotFoundError(f"Portable runtime python.exe missing: {python_exe}")

    probe = [
        str(python_exe),
        "-c",
        (
            "import ctypes, encodings, json, sqlite3, ssl, sys, video_sum_core; "
            "print(json.dumps({'exe': sys.executable, 'prefix': sys.prefix, 'base_prefix': sys.base_prefix}))"
        ),
    ]
    subprocess.run(probe, cwd=runtime_dir, check=True)


def portable_runtime_python(runtime_dir: Path) -> Path:
    for candidate in venv_python_candidates(runtime_dir):
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"Portable runtime Python missing: {runtime_dir}")


def verify_portable_macos_runtime(runtime_dir: Path) -> None:
    python_exe = portable_runtime_python(runtime_dir)
    env = dict(os.environ)
    pythonpath = runtime_dir / "pythonpath.pth"
    entries = [
        str((runtime_dir / line.strip()).resolve())
        for line in pythonpath.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    env.pop("PYTHONHOME", None)
    env.pop("__PYVENV_LAUNCHER__", None)
    env["PYTHONPATH"] = os.pathsep.join(entries)
    probe = [
        str(python_exe),
        "-c",
        (
            "import encodings, json, sqlite3, ssl, sys, video_sum_core; "
            "print(json.dumps({'exe': sys.executable, 'prefix': sys.prefix, 'path': sys.path[:3]}))"
        ),
    ]
    subprocess.run(probe, cwd=runtime_dir, env=env, check=True)


def write_runtime_seed_metadata(runtime_dir: Path) -> None:
    version = VERSION_FILE.read_text(encoding="utf-8").strip() if VERSION_FILE.exists() else "0.0.0"
    payload = {
        "runtimeChannel": "base",
        "runtimeLayout": "portable-cpython" if (IS_WINDOWS or IS_MACOS) else "venv",
        "appVersion": version,
        "pythonVersion": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    }
    target = runtime_dir / "video_sum_runtime.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def cleanup_direct_url_metadata(site_packages: Path) -> None:
    """删除 direct_url.json 元数据，避免生产环境路径无效。

    当使用 `pip install <local_path>` 安装本地包时，pip 会在
    direct_url.json 中记录原始路径。在生产环境中，这些路径
    不存在，导致 pip 操作失败。

    这只移除来源信息记录，不影响包的功能。
    """
    if not site_packages.exists():
        return

    cleaned_count = 0
    for dist_info in site_packages.glob("*.dist-info"):
        direct_url = dist_info / "direct_url.json"
        if direct_url.exists():
            try:
                # 只清理本地路径的 direct_url，保留 VCS/URL 来源
                content = json.loads(direct_url.read_text(encoding="utf-8"))
                url = content.get("url", "")
                if url.startswith("file://"):
                    direct_url.unlink()
                    cleaned_count += 1
                    print(f"Cleaned direct_url: {dist_info.name}")
            except (json.JSONDecodeError, OSError) as e:
                print(f"Warning: Could not process {direct_url}: {e}")

    print(f"Direct URL cleanup complete: removed {cleaned_count} file(s)")


def cleanup_runtime_site_packages(python_exe: Path) -> None:
    """删除运行时环境中不必要的包，减小打包体积。"""
    site_packages = site_packages_dir(python_exe)
    if not site_packages.exists():
        return

    # 不需要的包列表（名称：是否在 pip freeze 中检查）
    unnecessary_packages = {
        "sympy": True,       # 符号计算库，74MB，与视频摘要无关
        "mpmath": True,      # sympy 的依赖
        # pip/setuptools/wheel/ensurepip must stay in the managed runtime because
        # optional features install and repair their own dependencies after app updates.
        "rich": True,        # 终端美化，运行时不需要
        "typer": True,       # CLI 框架，运行时不需要
        "pygments": True,    # 语法高亮，rich/typer 的依赖
        "markdown_it_py": True,  # rich 的依赖
        # 本地 ASR 改为按需安装，不再进入基础运行时
        "faster_whisper": True,
        "ctranslate2": True,
        "onnxruntime": True,
        "tokenizers": True,
        "huggingface_hub": True,
        "hf_xet": True,
    }

    removed_count = 0
    removed_size = 0

    for package_name, check_freeze in unnecessary_packages.items():
        package_dir = site_packages / package_name
        package_dir_dist = site_packages / f"{package_name}-{package_name}.dist-info"

        # 查找实际的 dist-info 目录
        if not package_dir_dist.exists():
            for dist_info in site_packages.glob(f"{package_name}-*.dist-info"):
                if dist_info.is_dir():
                    package_dir_dist = dist_info
                    break

        if package_dir.exists():
            try:
                # 计算大小
                pkg_size = sum(f.stat().st_size for f in package_dir.rglob("*") if f.is_file())
                shutil.rmtree(package_dir)
                removed_count += 1
                removed_size += pkg_size
                print(f"Removed: {package_dir} ({pkg_size / 1024 / 1024:.1f} MB)")
            except OSError as e:
                print(f"Warning: Could not remove {package_dir}: {e}")

        if package_dir_dist.exists():
            try:
                shutil.rmtree(package_dir_dist)
                print(f"Removed: {package_dir_dist}")
            except OSError as e:
                print(f"Warning: Could not remove {package_dir_dist}: {e}")

    print(f"Cleanup complete: removed {removed_count} packages, freed {removed_size / 1024 / 1024:.1f} MB")


def copy_ffmpeg_binaries() -> None:
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    source_dir = resolve_ffmpeg_source_dir()
    if source_dir is None:
        raise SystemExit(
            "Bundled ffmpeg not found. "
            f"Expected repo-managed binaries in {PROJECT_FFMPEG_DIR} "
            "or a usable VIDEO_SUM_FFMPEG_DIR / system ffmpeg installation."
        )

    for name in (executable_name("ffmpeg"), executable_name("ffprobe")):
        source = source_dir / name
        if source.exists():
            shutil.copy2(source, BIN_DIR / name)
    copy_macos_ffmpeg_libraries(source_dir)


def copy_macos_ffmpeg_libraries(source_dir: Path) -> None:
    if not IS_MACOS:
        return
    required_libraries = resolve_macos_ffmpeg_libraries(
        [source_dir / executable_name("ffmpeg"), source_dir / executable_name("ffprobe")]
    )
    if not required_libraries:
        return
    lib_dir = BIN_DIR / "lib"
    lib_dir.mkdir(parents=True, exist_ok=True)
    for library in required_libraries:
        shutil.copy2(library, lib_dir / library.name)
    for executable in (BIN_DIR / executable_name("ffmpeg"), BIN_DIR / executable_name("ffprobe")):
        rewrite_macos_binary_library_paths(executable, lib_dir, "@executable_path/lib")
    for library in lib_dir.glob("*.dylib"):
        run(["install_name_tool", "-id", f"@rpath/{library.name}", str(library)])
        rewrite_macos_binary_library_paths(library, lib_dir, "@loader_path")


def resolve_macos_ffmpeg_libraries(seed_binaries: list[Path]) -> list[Path]:
    resolved: list[Path] = []
    queue = [binary for binary in seed_binaries if binary.exists()]
    seen: set[Path] = set()
    while queue:
        binary = queue.pop(0).resolve()
        if binary in seen:
            continue
        seen.add(binary)
        for library in macos_linked_libraries(binary):
            if library in resolved:
                continue
            resolved.append(library)
            queue.append(library)
    return resolved


def macos_linked_libraries(binary: Path) -> list[Path]:
    result = subprocess.run(
        ["otool", "-L", str(binary)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    libraries: list[Path] = []
    for raw_line in result.stdout.splitlines()[1:]:
        library_ref = raw_line.strip().split(" ", 1)[0]
        if not library_ref.startswith(("/opt/homebrew/", "/usr/local/")):
            continue
        library = Path(library_ref)
        if library.exists():
            libraries.append(library.resolve())
    return libraries


def rewrite_macos_binary_library_paths(binary: Path, lib_dir: Path, rpath: str) -> None:
    if not binary.exists():
        return
    add_macos_rpath(binary, rpath)
    for library in macos_linked_libraries(binary):
        bundled_library = lib_dir / library.name
        if bundled_library.exists():
            run(["install_name_tool", "-change", str(library), f"@rpath/{library.name}", str(binary)])


def add_macos_rpath(binary: Path, rpath: str) -> None:
    if rpath in macos_rpaths(binary):
        return
    run(["install_name_tool", "-add_rpath", rpath, str(binary)])


def macos_rpaths(binary: Path) -> set[str]:
    result = subprocess.run(
        ["otool", "-l", str(binary)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    rpaths: set[str] = set()
    lines = result.stdout.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != "cmd LC_RPATH":
            continue
        for detail in lines[index + 1 : index + 6]:
            detail = detail.strip()
            if detail.startswith("path "):
                rpaths.add(detail.split(" ", 2)[1])
                break
    return rpaths


def resolve_ffmpeg_source_dir() -> Path | None:
    if ffmpeg_dir_is_usable(PROJECT_FFMPEG_DIR):
        return PROJECT_FFMPEG_DIR

    env_dir = os.environ.get("VIDEO_SUM_FFMPEG_DIR")
    if env_dir:
        candidate = Path(env_dir).resolve()
        if ffmpeg_dir_is_usable(candidate):
            return candidate
        print(f"warning: VIDEO_SUM_FFMPEG_DIR is not usable: {candidate}")

    raw_candidates: list[Path] = []
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        raw_candidates.append(Path(ffmpeg_path).resolve())

    where_exe = shutil.which("where.exe")
    if where_exe:
        try:
            output = subprocess.run(
                [where_exe, "ffmpeg"],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            ).stdout
            for line in output.splitlines():
                line = line.strip()
                if line:
                    raw_candidates.append(Path(line).resolve())
        except subprocess.CalledProcessError:
            pass

    checked_dirs: list[Path] = []
    for ffmpeg_executable in raw_candidates:
        for candidate_dir in ffmpeg_dir_candidates(ffmpeg_executable):
            if candidate_dir in checked_dirs:
                continue
            checked_dirs.append(candidate_dir)
            if ffmpeg_dir_is_usable(candidate_dir):
                return candidate_dir
    return None


def ffmpeg_dir_candidates(ffmpeg_executable: Path) -> list[Path]:
    candidates = [ffmpeg_executable.parent]
    path_text = str(ffmpeg_executable).lower()
    if IS_WINDOWS and "chocolatey\\bin\\ffmpeg.exe" in path_text:
        candidates.insert(0, ffmpeg_executable.parent.parent / "lib" / "ffmpeg" / "tools" / "ffmpeg" / "bin")
    return [candidate.resolve() for candidate in candidates]


def ffmpeg_dir_is_usable(directory: Path) -> bool:
    ffmpeg_executable = directory / executable_name("ffmpeg")
    ffprobe_executable = directory / executable_name("ffprobe")
    if not ffmpeg_executable.exists() or not ffprobe_executable.exists():
        return False
    return executable_runs(ffmpeg_executable) and executable_runs(ffprobe_executable)


def executable_runs(executable: Path) -> bool:
    try:
        subprocess.run(
            [str(executable), "-version"],
            cwd=executable.parent,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def install_build_dependencies() -> None:
    python_exe = build_python()
    run([str(python_exe), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"])
    run([str(python_exe), "-m", "pip", "install", "--upgrade", "pyinstaller"])
    run(
        [
            str(python_exe),
            "-m",
            "pip",
            "install",
            str(ROOT / "packages" / "infra"),
            str(ROOT / "packages" / "core"),
            str(ROOT / "apps" / "service"),
        ]
    )
    # 清理 build 环境中不需要的包，减少 PyInstaller 分析时的干扰
    cleanup_build_site_packages(python_exe)


def cleanup_build_site_packages(python_exe: Path) -> None:
    """删除 build 环境中不必要的包，减少 PyInstaller 分析时的干扰。
    
    注意：这不会减小最终安装包体积（因为 PyInstaller 只收集依赖的模块），
    但可以减少警告和避免意外收集。
    """
    site_packages = site_packages_dir(python_exe)
    if not site_packages.exists():
        return

    # 不需要的包列表
    unnecessary_packages = {
        "sympy": True,       # 符号计算库，与视频摘要无关
        "mpmath": True,      # sympy 的依赖
        "rich": True,        # 终端美化，运行时不需要
        "typer": True,       # CLI 框架，运行时不需要
        "pygments": True,    # 语法高亮，rich/typer 的依赖
        "markdown_it_py": True,  # rich 的依赖
        # 本地 ASR 相关重依赖不参与基础包分析
        "faster_whisper": True,
        "ctranslate2": True,
        "onnxruntime": True,
        "tokenizers": True,
        "huggingface_hub": True,
        "hf_xet": True,
    }

    removed_count = 0

    for package_name in unnecessary_packages.keys():
        package_dir = site_packages / package_name
        
        # 查找 dist-info 目录
        package_dir_dist = None
        for dist_info in site_packages.glob(f"{package_name}-*.dist-info"):
            if dist_info.is_dir():
                package_dir_dist = dist_info
                break

        if package_dir.exists():
            try:
                import shutil
                shutil.rmtree(package_dir)
                removed_count += 1
                print(f"Removed from build env: {package_dir}")
            except OSError as e:
                print(f"Warning: Could not remove {package_dir}: {e}")

        if package_dir_dist and package_dir_dist.exists():
            try:
                import shutil
                shutil.rmtree(package_dir_dist)
                print(f"Removed from build env: {package_dir_dist}")
            except OSError as e:
                print(f"Warning: Could not remove {package_dir_dist}: {e}")

    print(f"Build env cleanup complete: removed {removed_count} packages")


def main() -> int:
    ensure_python_version()
    BUILD_ROOT.mkdir(parents=True, exist_ok=True)
    create_build_venv()
    install_build_dependencies()
    create_runtime_seed()
    copy_ffmpeg_binaries()
    run([str(build_python()), "-m", "PyInstaller", "--noconfirm", "--clean", str(SPEC_PATH)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
