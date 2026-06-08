# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import os
import sys

from PyInstaller.utils.hooks import collect_submodules, copy_metadata


ROOT = Path.cwd().resolve()
BUILD_ROOT = ROOT / "build" / "pyinstaller"
WEB_STATIC_DIR = ROOT / "apps" / "web" / "static"
ICON_PATH = ROOT / "apps" / "desktop" / "build" / ("icon.ico" if sys.platform == "win32" else "icon.icns")
RUNTIME_SEED_DIR = BUILD_ROOT / "runtime" / "base"
BIN_DIR = BUILD_ROOT / "bin"

# 运行时环境中不需要的包（用于过滤 runtime/base 目录）
RUNTIME_EXCLUDE_PACKAGES = [
    'sympy',
    'mpmath',
    'pip',
    'setuptools',
    'wheel',
    'distutils',
    'ensurepip',
    'rich',
    'typer',
    'pygments',
    'markdown_it_py',
    # 本地 ASR 相关重依赖不再随基础包分发
    'faster_whisper',
    'ctranslate2',
    'onnxruntime',
    'tokenizers',
    'huggingface_hub',
    'hf_xet',
]

def collect_runtime_files(source_dir: Path, dest_dir: str, exclude_packages: list[str]) -> list[tuple[str, str]]:
    """收集运行时目录中的文件，排除指定的包。"""
    if not source_dir.exists():
        return []
    
    site_packages = source_dir / "Lib" / "site-packages"
    if not site_packages.exists():
        # 如果没有 site-packages，复制整个目录
        return [(str(source_dir), dest_dir)]
    
    # 收集需要排除的包目录
    exclude_dirs: set[Path] = set()
    for pkg_name in exclude_packages:
        pkg_dir = site_packages / pkg_name
        if pkg_dir.exists():
            exclude_dirs.add(pkg_dir)
        # 也排除 dist-info
        for dist_info in site_packages.glob(f"{pkg_name}-*.dist-info"):
            if dist_info.is_dir():
                exclude_dirs.add(dist_info)
    
    if not exclude_dirs:
        # 没有需要排除的，复制整个目录
        return [(str(source_dir), dest_dir)]
    
    # 递归收集文件，排除指定的包
    result: list[tuple[str, str]] = []
    
    def is_excluded(path: Path) -> bool:
        """检查路径是否在被排除的目录中。"""
        path_str = str(path).lower()
        for exclude_dir in exclude_dirs:
            if path_str.startswith(str(exclude_dir).lower()):
                return True
        return False
    
    for root, dirs, files in os.walk(source_dir):
        root_path = Path(root)
        
        # 检查当前目录是否在被排除的目录中
        if is_excluded(root_path):
            dirs.clear()  # 不遍历子目录
            continue
        
        # 过滤掉被排除的子目录
        dirs[:] = [d for d in dirs if not is_excluded(root_path / d)]
        
        # 添加文件
        for file in files:
            file_path = root_path / file
            if not is_excluded(file_path):
                file_rel = file_path.relative_to(source_dir)
                result.append((str(file_path), str(Path(dest_dir) / file_rel)))
    
    return result

# FFmpeg 统一只通过 datas/bin 分发一份。
# 运行时会从 bundled_root()/bin 查找 ffmpeg.exe，避免同时在
# PyInstaller _internal 根目录和 bin/ 目录各保留一份。
binaries = []

datas = []
datas += [(str(WEB_STATIC_DIR), "web/static")]
# 使用过滤函数收集 runtime 文件，排除不需要的包
datas += collect_runtime_files(RUNTIME_SEED_DIR, "runtime/base", RUNTIME_EXCLUDE_PACKAGES)
if BIN_DIR.exists():
    datas += [(str(BIN_DIR), "bin")]

# runtime/base 会作为完整 seed runtime 原样随安装包分发，
# 主服务 EXE 不再重复携带 faster-whisper 的数据和元数据，
# 否则会在 _internal 与 runtime/base 中各保留一份重依赖。
# 简化元数据复制，移除非必要的包元数据
datas += copy_metadata("pydantic")
datas += copy_metadata("pydantic-settings")

hiddenimports = []
hiddenimports += collect_submodules("video_sum_service")
hiddenimports += collect_submodules("video_sum_core")
hiddenimports += collect_submodules("video_sum_infra")
hiddenimports += collect_submodules("yt_dlp")
# 时区数据 - Windows 上 zoneinfo 需要
hiddenimports += ["tzdata"]

a = Analysis(
    [str(ROOT / "apps" / "service" / "src" / "video_sum_service" / "__main__.py")],
    pathex=[
        str(ROOT / "apps" / "service" / "src"),
        str(ROOT / "packages" / "core" / "src"),
        str(ROOT / "packages" / "infra" / "src"),
    ],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 大型数学符号计算库 - 运行时推理不需要
        'sympy',
        'mpmath',
        # 开发/构建工具 - 运行时不需要
        'pip',
        'setuptools',
        'wheel',
        'distutils',
        'ensurepip',
        # 测试框架
        'pytest',
        'nose',
        'unittest',
        'doctest',
        'pdb',
        # 文档工具
        'pydoc',
        'sphinx',
        # 不需要的 ML 后端
        'tensorflow',
        'jax',
        'theano',
        'keras',
        # 本地 ASR 默认改为按需安装
        'faster_whisper',
        'ctranslate2',
        'onnxruntime',
        'tokenizers',
        'huggingface_hub',
        'hf_xet',
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VidMind",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[
        # 排除不支持的 ARM64 架构文件
        'w64-arm',
        'arm64',
        # 排除使用 SIMD 指令集的关键 DLL - UPX 压缩会导致崩溃
        'ctranslate2',
        'ctranslate2.dll',
        'libiomp5md',
        'libiomp5md.dll',
        'onnxruntime',
        'onnxruntime.dll',
        # PyAV 相关
        'av',
        'av.dll',
        'av-*.dll',
        # NumPy 相关
        'numpy',
        'numpy.dll',
        'numpy\\.libs',
        # Python 核心 DLL
        'python3.dll',
        'python312.dll',
        # 排除已经压缩或无法压缩的文件
        '.txt',
        '.json',
        '.md',
    ],
    console=False,
    icon=str(ICON_PATH) if ICON_PATH.exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[
        # 排除不支持的 ARM64 架构文件
        'w64-arm',
        'arm64',
        # 排除使用 SIMD 指令集的关键 DLL - UPX 压缩会导致崩溃
        'ctranslate2',
        'ctranslate2.dll',
        'libiomp5md',
        'libiomp5md.dll',
        'onnxruntime',
        'onnxruntime.dll',
        # PyAV 相关
        'av',
        'av.dll',
        'av-*.dll',
        # NumPy 相关
        'numpy',
        'numpy.dll',
        'numpy\\.libs',
        # Python 核心 DLL
        'python3.dll',
        'python312.dll',
        # 排除已经压缩或无法压缩的文件
        '.txt',
        '.json',
        '.md',
    ],
    name="VidMind",
)
