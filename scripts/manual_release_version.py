from __future__ import annotations

import os
import sys


def main() -> int:
    """Resolve the release version for a manual (workflow_dispatch) bump.

    Reads ``MANUAL_BUMP`` (patch / minor / major / an explicit version like
    ``1.20.0`` or ``1.20.0-beta.1``) and writes ``bump=`` / ``next_version=``
    into ``GITHUB_OUTPUT`` when present, mirroring detect_bump.py's contract.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from versioning import bump_version, read_source_version

    level = os.environ.get("MANUAL_BUMP", "").strip()
    if not level:
        print("MANUAL_BUMP is empty; nothing to do.", file=sys.stderr)
        return 1

    try:
        next_version = bump_version(read_source_version(), level)
    except ValueError as exc:
        print(f"Invalid bump target: {level} ({exc})", file=sys.stderr)
        return 1

    bump = level if level in {"patch", "minor", "major"} else next_version
    output_path = os.environ.get("GITHUB_OUTPUT", "")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as handle:
            handle.write(f"bump={bump}\n")
            handle.write(f"next_version={next_version}\n")

    print(f"bump={bump}")
    print(f"next_version={next_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
