from __future__ import annotations

import argparse
import re
import subprocess
import sys

from versioning import bump_version, read_source_version


COMMIT_SEPARATOR = "\x1e"
FIELD_SEPARATOR = "\x1f"
# Markers (`!` / `*`) may appear before and/or after the scope:
#   fix*: ...          fix(scope)*: ...        fix*(scope): ...
COMMIT_HEADER_RE = re.compile(
    r"^(?P<type>[A-Za-z][\w/-]*)(?P<pre_markers>[!*]*)(?:\([^)]*\))?(?P<post_markers>[!*]*):"
)
RELEASE_RE = re.compile(r"^chore\(release\):\s+v\d+\.\d+\.\d+$")
# A breaking change is only recognized from the conventional-commit footer
# (`BREAKING CHANGE:` / `BREAKING-CHANGE:` at line start) or the `!` marker.
# Plain mentions of the phrase in a commit body must not count as breaking.
BREAKING_FOOTER_RE = re.compile(r"^BREAKING[ -]CHANGE:", re.MULTILINE)
MINOR_TYPES = {"feat"}
PATCH_TYPES = {"fix", "perf", "refactor"}
RELEASABLE_TYPES = MINOR_TYPES | PATCH_TYPES


def run_git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout.strip()


def latest_version_tag() -> str:
    return run_git("tag", "--list", "v*", "--sort=-version:refname").splitlines()[0] if run_git(
        "tag", "--list", "v*", "--sort=-version:refname"
    ) else ""


def latest_release_commit() -> str:
    return run_git("log", "--fixed-strings", "--grep", "chore(release): v", "--format=%H", "-n", "1")


def release_base_ref() -> tuple[str, str]:
    tag = latest_version_tag()
    if tag:
        return tag, "tag"

    commit = latest_release_commit()
    if commit:
        return commit, "release-commit"

    return "", "root"


def commits_since(ref: str) -> list[tuple[str, str]]:
    revision_range = f"{ref}..HEAD" if ref else "HEAD"
    raw = run_git("log", "--format=%s%x1f%b%x1e", revision_range)
    commits: list[tuple[str, str]] = []
    for entry in raw.split(COMMIT_SEPARATOR):
        item = entry.strip()
        if not item:
            continue
        subject, _, body = item.partition(FIELD_SEPARATOR)
        commits.append((subject.strip(), body.strip()))
    return commits


def parse_commit_subject(subject: str) -> tuple[str, str] | None:
    match = COMMIT_HEADER_RE.match(subject)
    if not match:
        return None
    markers = (match.group("pre_markers") or "") + (match.group("post_markers") or "")
    return match.group("type"), markers


def is_breaking_change(markers: str, body: str) -> bool:
    return "!" in markers or bool(BREAKING_FOOTER_RE.search(body or ""))


def should_release_commit(commit_type: str, markers: str, body: str) -> bool:
    if commit_type not in RELEASABLE_TYPES:
        return False
    return "*" in markers or is_breaking_change(markers, body)


def detect_bump(commits: list[tuple[str, str]]) -> str:
    filtered = [(subject, body) for subject, body in commits if subject and not RELEASE_RE.match(subject)]
    if not filtered:
        return ""

    parsed_commits: list[tuple[str, str, str]] = []
    for subject, body in filtered:
        parsed = parse_commit_subject(subject)
        if parsed is None:
            continue
        commit_type, markers = parsed
        if should_release_commit(commit_type, markers, body):
            parsed_commits.append((commit_type, markers, body))

    if not parsed_commits:
        return ""

    for commit_type, markers, body in parsed_commits:
        if commit_type in RELEASABLE_TYPES and is_breaking_change(markers, body):
            return "major"

    for commit_type, _markers, _body in parsed_commits:
        if commit_type in MINOR_TYPES:
            return "minor"

    for commit_type, _markers, _body in parsed_commits:
        if commit_type in PATCH_TYPES:
            return "patch"

    return ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Detect the next semantic version bump from git history.")
    parser.add_argument(
        "--github-output",
        help="Optional path to GITHUB_OUTPUT for workflow step outputs.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    current_version = read_source_version()
    base_ref, base_kind = release_base_ref()
    commits = commits_since(base_ref)
    bump = detect_bump(commits)
    next_version = bump_version(current_version, bump) if bump else current_version

    print(f"current_version={current_version}")
    print(f"release_base={base_ref or 'none'}")
    print(f"release_base_kind={base_kind}")
    print(f"bump={bump or 'none'}")
    print(f"next_version={next_version}")

    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as handle:
            handle.write(f"current_version={current_version}\n")
            handle.write(f"release_base={base_ref}\n")
            handle.write(f"release_base_kind={base_kind}\n")
            handle.write(f"bump={bump}\n")
            handle.write(f"next_version={next_version}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
