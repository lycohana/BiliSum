"""Environment probe script — executed via subprocess in detect_environment().

Read by runtime_support.py and passed to ``python -c`` to inspect the
runtime Python's installed packages and capabilities.

NOTE: Do NOT add ``from __future__ import annotations`` here — this script
is executed via ``python -c`` by concatenating test shims, and __future__
imports must appear at the very start of the file.
"""

# Guard: torch's _load_dll_libraries calls os.add_dll_directory()
# for torch/lib, which can fail with WinError 206 on portable Python
# builds even though the path is well under MAX_PATH.  The directory
# is already on PATH (set by runtime_subprocess_env), so swallowing
# this error is safe — DLL loading via PATH still works.
import os as _os
_original_add_dll_directory = getattr(_os, "add_dll_directory", None)
if _original_add_dll_directory is not None:
    def _safe_add_dll_directory(path):
        try:
            return _original_add_dll_directory(path)
        except (FileNotFoundError, OSError):
            return None
    _os.add_dll_directory = _safe_add_dll_directory
# NOTE: Do NOT del _os / _original_add_dll_directory — in -c exec mode
# the compiler may use LOAD_GLOBAL for the free variable reference in
# the closure, and del removes the name from __main__, causing NameError
# when the guard is later invoked via torch._load_dll_libraries.

import importlib
import importlib.metadata
import json
import sys


def importable_distribution(
    distribution_name: str, import_name: str
) -> tuple[bool, str, str]:
    """Check whether *distribution_name* is installed and *import_name* is importable.

    Returns ``(importable, version, error)``.
    """
    try:
        version = importlib.metadata.version(distribution_name)
    except importlib.metadata.PackageNotFoundError:
        return False, "", ""
    try:
        importlib.import_module(import_name)
    except Exception as exc:
        return False, version, f"{type(exc).__name__}: {exc}"
    return True, version, ""


def probe() -> dict:
    """Run the environment probe and return a JSON-serializable payload."""
    torch_error = ""
    try:
        import torch  # noqa: F811
    except Exception as exc:
        torch = None
        torch_error = f"{type(exc).__name__}: {exc}"

    cuda_available = bool(torch is not None and torch.cuda.is_available())
    gpu_name = torch.cuda.get_device_name(0) if cuda_available else ""

    # Register torchaudio/lib DLL directory so that libtorchaudio.pyd
    # can resolve its transitive dependencies.  Guards handle WinError 206.
    if torch is not None:
        _sp = sys.modules["os"].path.dirname(torch.__file__)  # site-packages/torch
        _tal = sys.modules["os"].path.join(sys.modules["os"].path.dirname(_sp), "torchaudio", "lib")
        if sys.modules["os"].path.isdir(_tal):
            sys.modules["os"].add_dll_directory(_tal)

    payload = {
        "pythonVersion": sys.version.split()[0],
        "torchInstalled": torch is not None,
        "torchVersion": torch.__version__ if torch is not None else "",
        "torchError": torch_error,
        "torchvisionInstalled": False,
        "torchvisionVersion": "",
        "torchvisionBroken": False,
        "torchvisionError": "",
        "torchaudioInstalled": False,
        "torchaudioVersion": "",
        "torchaudioBroken": False,
        "torchaudioError": "",
        "cudaAvailable": cuda_available,
        "gpuName": gpu_name,
        "ytDlpVersion": importlib.metadata.version("yt-dlp"),
        "localAsrVersion": "",
        "localAsrInstalled": False,
        "localAsrAvailable": False,
        "chromadbVersion": "",
        "chromadbInstalled": False,
        "chromadbBroken": False,
        "chromadbError": "",
        "sentenceTransformersVersion": "",
        "sentenceTransformersInstalled": False,
        "sentenceTransformersBroken": False,
        "sentenceTransformersError": "",
        "modelscopeVersion": "",
        "modelscopeInstalled": False,
        "modelscopeBroken": False,
        "modelscopeError": "",
        "knowledgeDependenciesReady": False,
        "knowledgeDependenciesError": "",
        "ffmpegLocation": "",
        "recommendedModel": "large-v3-turbo" if cuda_available else "base",
        "recommendedDevice": "cuda" if cuda_available else "cpu",
    }

    torchvision_installed, torchvision_version, torchvision_error = importable_distribution(
        "torchvision", "torchvision"
    )
    torchvision_broken = bool(torchvision_version and torchvision_error)
    payload["torchvisionVersion"] = torchvision_version
    payload["torchvisionInstalled"] = torchvision_installed
    payload["torchvisionBroken"] = torchvision_broken
    payload["torchvisionError"] = torchvision_error

    torchaudio_installed, torchaudio_version, torchaudio_error = importable_distribution(
        "torchaudio", "torchaudio"
    )
    torchaudio_broken = bool(torchaudio_version and torchaudio_error)
    payload["torchaudioVersion"] = torchaudio_version
    payload["torchaudioInstalled"] = torchaudio_installed
    payload["torchaudioBroken"] = torchaudio_broken
    payload["torchaudioError"] = torchaudio_error

    try:
        payload["localAsrVersion"] = importlib.metadata.version("faster-whisper")
        payload["localAsrInstalled"] = True
        payload["localAsrAvailable"] = True
    except importlib.metadata.PackageNotFoundError:
        pass

    funasr_installed, funasr_version, funasr_error = importable_distribution(
        "funasr", "funasr"
    )
    payload["funasrVersion"] = funasr_version
    payload["funasrInstalled"] = funasr_installed
    payload["funasrAvailable"] = funasr_installed
    payload["funasrBroken"] = bool(funasr_version and funasr_error)
    payload["funasrError"] = funasr_error

    chromadb_installed, chromadb_version, chromadb_error = importable_distribution(
        "chromadb", "chromadb"
    )
    chromadb_broken = bool(chromadb_version and chromadb_error)
    payload["chromadbVersion"] = chromadb_version
    payload["chromadbInstalled"] = chromadb_installed
    payload["chromadbBroken"] = chromadb_broken
    payload["chromadbError"] = chromadb_error

    st_installed, st_version, st_error = importable_distribution(
        "sentence-transformers", "sentence_transformers"
    )
    st_broken = bool(st_version and st_error)
    payload["sentenceTransformersVersion"] = st_version
    payload["sentenceTransformersInstalled"] = st_installed
    payload["sentenceTransformersBroken"] = st_broken
    payload["sentenceTransformersError"] = st_error

    modelscope_installed, modelscope_version, modelscope_error = importable_distribution(
        "modelscope", "modelscope"
    )
    modelscope_broken = bool(modelscope_version and modelscope_error)
    payload["modelscopeVersion"] = modelscope_version
    payload["modelscopeInstalled"] = modelscope_installed
    payload["modelscopeBroken"] = modelscope_broken
    payload["modelscopeError"] = modelscope_error

    payload["knowledgeDependenciesReady"] = bool(
        payload.get("chromadbInstalled") and payload.get("sentenceTransformersInstalled")
    )
    if not payload["knowledgeDependenciesReady"]:
        errors = [
            value
            for value in [
                payload.get("chromadbError"),
                payload.get("sentenceTransformersError"),
            ]
            if value
        ]
        payload["knowledgeDependenciesError"] = "\n".join(errors)

    # Debug: confirm sitecustomize guard was loaded at interpreter startup
    payload["sitecustomizeActive"] = sys.modules["os"].environ.get("BILISUM_SITECUSTOMIZE") == "1"

    return payload


def main() -> None:
    """Entry point: print the probe payload as JSON to stdout."""
    print(json.dumps(probe(), ensure_ascii=False))


if __name__ == "__main__":
    main()
