from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re


# PEP 440 / npm-semver compatible: X.Y.Z optionally followed by a prerelease
# segment (e.g. 1.20.0-beta.1, 1.20.0-rc.2). Kept aligned with what
# pyproject.toml (PEP 440), package.json (semver) and electron-builder accept.
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
PRERELEASE_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)-"
    r"(?P<prefix>[0-9A-Za-z-]+)(?:\.(?P<number>[0-9]+))?$"
)
PYPROJECT_VERSION_RE = re.compile(r'(?m)^version = "[^"]+"$')

# Prerelease kinds selectable through the `release: <kind>` PR label.
PRERELEASE_KINDS: tuple[str, ...] = ("beta", "rc", "alpha")

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = REPO_ROOT / "VERSION"


@dataclass(frozen=True)
class VersionTargets:
    pyprojects: tuple[Path, ...]
    package_jsons: tuple[Path, ...]
    package_locks: tuple[Path, ...]


TARGETS = VersionTargets(
    pyprojects=(
        REPO_ROOT / "pyproject.toml",
        REPO_ROOT / "apps" / "service" / "pyproject.toml",
        REPO_ROOT / "packages" / "core" / "pyproject.toml",
        REPO_ROOT / "packages" / "infra" / "pyproject.toml",
    ),
    package_jsons=(
        REPO_ROOT / "apps" / "desktop" / "package.json",
        REPO_ROOT / "packages" / "npx" / "package.json",
    ),
    package_locks=(REPO_ROOT / "apps" / "desktop" / "package-lock.json",),
)


def normalize_version(raw: str) -> str:
    version = raw.strip()
    if not SEMVER_RE.fullmatch(version):
        raise ValueError(f"Invalid semantic version: {raw!r}")
    return version


def read_source_version() -> str:
    return normalize_version(VERSION_FILE.read_text(encoding="utf-8"))


def bump_version(current: str, level: str) -> str:
    normalized = normalize_version(current)
    base = normalized.split("-", 1)[0]
    major, minor, patch = [int(part) for part in base.split(".")]
    normalized_level = level.strip().lower()
    prerelease_match = PRERELEASE_RE.match(normalized)

    if normalized_level in {"beta", "rc", "alpha"}:
        if normalized_level not in PRERELEASE_KINDS:
            raise ValueError(f"Unsupported prerelease kind: {level!r}")
        if prerelease_match and prerelease_match.group("prefix") == normalized_level:
            number = int(prerelease_match.group("number") or 1)
            return f"{major}.{minor}.{patch}-{normalized_level}.{number + 1}"
        return f"{major}.{minor}.{patch}-{normalized_level}.1"

    # Finalizing an existing prerelease (e.g. 1.20.0-beta.1 + patch) releases
    # the final version instead of bumping past it.
    if prerelease_match is not None:
        if normalized_level == "major":
            return f"{major + 1}.0.0"
        if normalized_level == "minor":
            return f"{major}.{minor + 1}.0"
        if normalized_level == "patch":
            return f"{major}.{minor}.{patch}"

    if normalized_level == "major":
        return f"{major + 1}.0.0"
    if normalized_level == "minor":
        return f"{major}.{minor + 1}.0"
    if normalized_level == "patch":
        return f"{major}.{minor}.{patch + 1}"
    return normalize_version(level)


def bump_prerelease_kind(current: str, kind: str) -> str:
    """Return the prerelease version for *kind* based on the current version.

    ``1.20.0`` -> ``1.20.0-beta.1``; ``1.20.0-beta.1`` -> ``1.20.0-beta.2``.
    """
    return bump_version(current, kind)


def sync_version(version: str) -> list[Path]:
    normalized = normalize_version(version)
    changed_files: list[Path] = []

    current_source = VERSION_FILE.read_text(encoding="utf-8").strip() if VERSION_FILE.exists() else ""
    if current_source != normalized:
        VERSION_FILE.write_text(f"{normalized}\n", encoding="utf-8")
        changed_files.append(VERSION_FILE)

    for path in TARGETS.pyprojects:
        text = path.read_text(encoding="utf-8")
        updated, replacements = PYPROJECT_VERSION_RE.subn(f'version = "{normalized}"', text, count=1)
        if replacements != 1:
            raise ValueError(f"Expected one version field in {path}")
        if updated != text:
            path.write_text(updated, encoding="utf-8")
            changed_files.append(path)

    for path in TARGETS.package_jsons:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("version") != normalized:
            payload["version"] = normalized
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            changed_files.append(path)

    for path in TARGETS.package_locks:
        payload = json.loads(path.read_text(encoding="utf-8"))
        updated = False
        if payload.get("version") != normalized:
            payload["version"] = normalized
            updated = True
        packages = payload.get("packages")
        if isinstance(packages, dict):
            root_package = packages.get("")
            if isinstance(root_package, dict) and root_package.get("version") != normalized:
                root_package["version"] = normalized
                updated = True
        if updated:
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            changed_files.append(path)

    return changed_files


def collect_version_mismatches() -> list[str]:
    expected = read_source_version()
    mismatches: list[str] = []

    for path in TARGETS.pyprojects:
        text = path.read_text(encoding="utf-8")
        match = PYPROJECT_VERSION_RE.search(text)
        actual = ""
        if match is not None:
            actual = match.group(0).split('"')[1]
        if actual != expected:
            mismatches.append(f"{path.relative_to(REPO_ROOT)} -> {actual or 'missing'}")

    for path in TARGETS.package_jsons:
        actual = str(json.loads(path.read_text(encoding="utf-8")).get("version", ""))
        if actual != expected:
            mismatches.append(f"{path.relative_to(REPO_ROOT)} -> {actual or 'missing'}")

    for path in TARGETS.package_locks:
        payload = json.loads(path.read_text(encoding="utf-8"))
        top_level = str(payload.get("version", ""))
        if top_level != expected:
            mismatches.append(f"{path.relative_to(REPO_ROOT)} -> top-level {top_level or 'missing'}")
        packages = payload.get("packages")
        root_package = packages.get("") if isinstance(packages, dict) else None
        root_version = str(root_package.get("version", "")) if isinstance(root_package, dict) else ""
        if root_version != expected:
            mismatches.append(f"{path.relative_to(REPO_ROOT)} -> packages[''] {root_version or 'missing'}")

    return mismatches
