from pathlib import Path
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.append(str(REPO_ROOT / "scripts"))

from versioning import bump_prerelease_kind, bump_version, normalize_version  # noqa: E402


def test_normalize_version_accepts_prerelease() -> None:
    assert normalize_version("1.20.0-beta.1") == "1.20.0-beta.1"
    assert normalize_version("1.20.0-rc.2") == "1.20.0-rc.2"
    assert normalize_version("1.20.0-alpha.1") == "1.20.0-alpha.1"


def test_normalize_version_rejects_invalid() -> None:
    for raw in ("1.20", "v1.20.0", "1.20.0-", "1.20.0-beta."):
        with pytest.raises(ValueError):
            normalize_version(raw)


@pytest.mark.parametrize(
    ("current", "level", "expected"),
    [
        ("1.19.2", "patch", "1.19.3"),
        ("1.19.2", "minor", "1.20.0"),
        ("1.19.2", "major", "2.0.0"),
        ("1.19.2", "1.20.0", "1.20.0"),
        # Creating / incrementing prereleases
        ("1.20.0", "beta", "1.20.0-beta.1"),
        ("1.20.0-beta.1", "beta", "1.20.0-beta.2"),
        ("1.20.0-rc.1", "rc", "1.20.0-rc.2"),
        ("1.20.0-beta.1", "alpha", "1.20.0-alpha.1"),
        # Finalizing a prerelease
        ("1.20.0-beta.1", "patch", "1.20.0"),
        ("1.20.0-beta.1", "minor", "1.21.0"),
        ("1.20.0-beta.1", "major", "2.0.0"),
    ],
)
def test_bump_version(current: str, level: str, expected: str) -> None:
    assert bump_version(current, level) == expected


def test_bump_prerelease_kind() -> None:
    assert bump_prerelease_kind("1.20.0", "beta") == "1.20.0-beta.1"
    assert bump_prerelease_kind("1.20.0-beta.1", "beta") == "1.20.0-beta.2"
