"""Start the distributed backend outside the checkout before publishing it."""

from __future__ import annotations

import argparse
from email.parser import Parser
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import build_opener, ProxyHandler


def validate_backend(backend: Path, version: str, timeout: float) -> dict:
    backend = backend.resolve(strict=True)
    distributions = list((backend.parent / "_internal").glob("video_sum_service-*.dist-info/METADATA"))
    if len(distributions) != 1:
        raise RuntimeError("Packaged backend must contain exactly one application distribution metadata file.")
    distribution = Parser().parsestr(distributions[0].read_text(encoding="utf-8"))
    if distribution["Name"] != "video-sum-service" or distribution["Version"] != version:
        raise RuntimeError("Packaged application distribution metadata does not match the release version.")
    with tempfile.TemporaryDirectory(prefix="bilisum-release-check-") as temporary:
        # Keep the cwd, settings and managed runtime outside the checkout.
        test_root = Path(temporary).resolve()
        app_root = test_root / "application-data"
        (app_root / "data").mkdir(parents=True)
        (app_root / "data" / "settings.json").write_text(
            json.dumps({"runtime_channel": "base", "llm_enabled": False, "knowledge_enabled": False}),
            encoding="utf-8",
        )
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("VIDEO_SUM_", "BILISUM_", "BRIEFVID_"))
        }
        env.update({
            "VIDEO_SUM_APP_DATA_ROOT": str(app_root),
            "VIDEO_SUM_HOST": "127.0.0.1",
            "VIDEO_SUM_PORT": str(port),
            "VIDEO_SUM_ACCESS_TOKEN": "isolated-release-validation",
        })
        opener = build_opener(ProxyHandler({}))
        with (test_root / "process.log").open("wb") as output:
            process = subprocess.Popen(
                [str(backend)], cwd=test_root, env=env,
                stdout=output, stderr=output,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            try:
                deadline = time.monotonic() + timeout
                last_health: dict = {}
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError(f"Packaged backend exited with {process.returncode}.")
                    try:
                        with opener.open(f"http://127.0.0.1:{port}/health", timeout=3) as response:
                            last_health = json.load(response)
                    except (OSError, ValueError):
                        time.sleep(1)
                        continue
                    if last_health.get("version") != version:
                        raise RuntimeError(
                            f"Packaged backend version {last_health.get('version')!r} does not match {version!r}."
                        )
                    startup = last_health.get("runtime_startup", {})
                    if startup.get("status") == "error":
                        raise RuntimeError(f"Managed runtime startup failed: {startup.get('message')}")
                    if last_health.get("status") == "ok" and startup.get("status") == "ready":
                        runtime_python = Path(startup["environment"]["runtimePython"]).resolve()
                        if not runtime_python.is_relative_to(app_root):
                            raise RuntimeError("Backend did not use its isolated managed runtime.")
                        return {"version": version, "service": "ok", "runtime": "ready", "isolated": True}
                    time.sleep(1)
                raise RuntimeError(f"Packaged backend startup timed out. Last health: {last_health}")
            except Exception:
                for log in (app_root / "logs" / "service.log", test_root / "process.log"):
                    if log.exists():
                        print(log.read_text(encoding="utf-8", errors="replace")[-6000:], file=sys.stderr)
                raise
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    print(json.dumps(validate_backend(args.backend, args.version, args.timeout)))


if __name__ == "__main__":
    main()
