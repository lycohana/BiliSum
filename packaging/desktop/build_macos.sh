#!/usr/bin/env bash
set -euo pipefail

skip_prebuild=0
for arg in "$@"; do
  case "$arg" in
    --skip-prebuild)
      skip_prebuild=1
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      echo "Usage: build_macos.sh [--skip-prebuild]" >&2
      exit 2
      ;;
  esac
done

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "macOS packaging must run on macOS." >&2
  exit 1
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
desktop_dir="$repo_root/apps/desktop"
web_static_validation_script="$repo_root/scripts/validate_web_static_assets.js"

machine_arch="$(uname -m)"
case "$machine_arch" in
  arm64)
    electron_builder_arch="--arm64"
    ;;
  x86_64)
    electron_builder_arch="--x64"
    ;;
  *)
    echo "Unsupported macOS build architecture: $machine_arch" >&2
    exit 1
    ;;
esac

uv python install 3.12
python312="$(uv python find --managed-python 3.12 | tr -d '\r')"
if [[ -z "$python312" ]]; then
  echo "No uv-managed Python 3.12 interpreter was found." >&2
  exit 1
fi

echo "Using Python 3.12: $python312"

icon_script="$repo_root/apps/desktop/build/generate_icon.py"
if [[ ! -f "$icon_script" ]]; then
  echo "Icon generator script was not found: $icon_script" >&2
  exit 1
fi

icon_python="$python312"
if ! "$icon_python" -c "from PIL import Image" >/dev/null 2>&1; then
  icon_venv_dir="$repo_root/build/icon-python"
  icon_python="$icon_venv_dir/bin/python"

  if [[ ! -x "$icon_python" ]]; then
    echo "Creating isolated Python environment for icon generation: $icon_venv_dir"
    "$python312" -m venv "$icon_venv_dir"
  fi

  if ! "$icon_python" -m pip --version >/dev/null 2>&1; then
    echo "pip is missing in the icon generation environment; bootstrapping with ensurepip..."
    "$icon_python" -m ensurepip --upgrade
  fi

  if ! "$icon_python" -c "from PIL import Image" >/dev/null 2>&1; then
    echo "Pillow is missing; installing Pillow into the isolated icon generation environment..."
    "$icon_python" -m pip install --disable-pip-version-check Pillow
  fi
fi

echo "Generating application icons..."
"$icon_python" "$icon_script"

pushd "$desktop_dir" >/dev/null
trap 'popd >/dev/null' EXIT

if [[ "$skip_prebuild" -eq 0 ]]; then
  npm run build:renderer
  "$python312" "$repo_root/packaging/pyinstaller/build_onedir.py"

  backend_executable="$repo_root/dist/VidMind/VidMind"
  if [[ ! -f "$backend_executable" ]]; then
    echo "Packaged backend was not produced: $backend_executable" >&2
    exit 1
  fi
  chmod +x "$backend_executable"
  for executable in \
    "$repo_root/dist/VidMind/_internal/runtime/base/bin/python" \
    "$repo_root/dist/VidMind/_internal/runtime/base/bin/python3" \
    "$repo_root/dist/VidMind/_internal/bin/ffmpeg" \
    "$repo_root/dist/VidMind/_internal/bin/ffprobe"; do
    if [[ -f "$executable" && ! -x "$executable" ]]; then
      echo "Packaged executable is missing execute permission: $executable" >&2
      exit 1
    fi
  done

  runtime_seed="$repo_root/build/pyinstaller/runtime/base"
  runtime_probe_root="$(mktemp -d)"
  trap 'rm -rf "$runtime_probe_root"; popd >/dev/null' EXIT
  cp -R "$runtime_seed" "$runtime_probe_root/base"
  runtime_probe_python="$runtime_probe_root/base/bin/python"
  if [[ ! -x "$runtime_probe_python" ]]; then
    runtime_probe_python="$runtime_probe_root/base/bin/python3"
  fi
  if [[ ! -x "$runtime_probe_python" ]]; then
    echo "Relocated managed runtime Python was not found under $runtime_probe_root/base/bin" >&2
    exit 1
  fi
  runtime_probe_pythonpath="$(
    cd "$runtime_probe_root/base"
    while IFS= read -r entry; do
      [[ -z "$entry" || "$entry" == \#* ]] && continue
      printf '%s:' "$runtime_probe_root/base/$entry"
    done < pythonpath.pth
  )"
  env -u PYTHONHOME -u PYTHONEXECUTABLE -u __PYVENV_LAUNCHER__ PYTHONPATH="${runtime_probe_pythonpath%:}" \
    "$runtime_probe_python" -c "import encodings, sqlite3, ssl, sys, video_sum_core; print(sys.executable)"
else
  echo "SkipPrebuild enabled: reusing existing renderer and backend artifacts."
fi

if [[ ! -f "$web_static_validation_script" ]]; then
  echo "Web static asset validation script was not found: $web_static_validation_script" >&2
  exit 1
fi

echo "Validating web static asset references..."
node "$web_static_validation_script"

npm run build:electron

export CSC_IDENTITY_AUTO_DISCOVERY=false
npx electron-builder --config electron-builder.config.js --mac "$electron_builder_arch" --publish=never
