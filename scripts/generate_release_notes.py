from __future__ import annotations

import argparse
import re
import subprocess
from collections import defaultdict
from pathlib import Path


COMMIT_SUBJECT_RE = re.compile(
    r"^(?P<type>[A-Za-z][\w/-]*)(?P<pre_markers>[!*]*)(?:\((?P<scope>[^)]*)\))?"
    r"(?P<post_markers>[!*]*)(?P<separator>:|：)\s*(?P<description>.+)$"
)
RELEASE_RE = re.compile(r"^chore\(release\):\s+v\d+\.\d+\.\d+")
SECTION_ORDER = ("feat", "fix", "refactor")
SECTION_TITLES = {
    "feat": "Features",
    "fix": "Fixes",
    "refactor": "Refactors",
}
COMMIT_SEPARATOR = "\x1e"
FIELD_SEPARATOR = "\x1f"


def run_git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout.strip()


def list_version_tags() -> list[str]:
    raw = run_git("tag", "--list", "v*", "--sort=-version:refname")
    return [line.strip() for line in raw.splitlines() if line.strip()]


def previous_version_tag(current_tag: str) -> str:
    for tag in list_version_tags():
        if tag != current_tag:
            return tag
    return ""


def commits_between(base_ref: str, target_ref: str) -> list[tuple[str, str, str]]:
    revision_range = f"{base_ref}..{target_ref}" if base_ref else target_ref
    raw = run_git("log", "--format=%H%x1f%s%x1f%b%x1e", revision_range)
    commits: list[tuple[str, str, str]] = []
    for entry in raw.split(COMMIT_SEPARATOR):
        item = entry.strip()
        if not item:
            continue
        commit_sha, _, remainder = item.partition(FIELD_SEPARATOR)
        subject, _, body = remainder.partition(FIELD_SEPARATOR)
        commits.append((commit_sha.strip(), subject.strip(), body.strip()))
    return commits


def parse_commit(subject: str) -> dict[str, str] | None:
    match = COMMIT_SUBJECT_RE.match(subject.strip())
    if not match:
        return None
    markers = (match.group("pre_markers") or "") + (match.group("post_markers") or "")
    return {
        "type": match.group("type").lower(),
        "scope": (match.group("scope") or "").strip(),
        "markers": markers,
        "description": match.group("description").strip(),
    }


def normalize_commit_text(commit_sha: str, subject: str, repo_slug: str) -> dict[str, str] | None:
    if not subject or RELEASE_RE.match(subject):
        return None
    parsed = parse_commit(subject)
    if parsed is None:
        return None
    if parsed["type"] not in SECTION_ORDER:
        return None
    scope_prefix = f"{parsed['scope']}: " if parsed["scope"] else ""
    short_sha = commit_sha[:7]
    commit_link = f"https://github.com/{repo_slug}/commit/{commit_sha}" if repo_slug and commit_sha else ""
    suffix = f" ([{short_sha}]({commit_link}))" if commit_link else ""
    return {
        "type": parsed["type"],
        "text": f"{scope_prefix}{parsed['description']}{suffix}",
        "is_release_highlight": "*" in parsed["markers"] or "!" in parsed["markers"],
    }


def build_release_notes(version: str, commits: list[tuple[str, str, str]], compare_ref: str, repo_slug: str) -> str:
    grouped: dict[str, list[str]] = defaultdict(list)
    highlights: list[str] = []

    for commit_sha, subject, _body in commits:
        normalized = normalize_commit_text(commit_sha, subject, repo_slug)
        if normalized is None:
            continue
        grouped[normalized["type"]].append(normalized["text"])
        if normalized["is_release_highlight"]:
            highlights.append(normalized["text"])

    if not highlights:
        for commit_type in SECTION_ORDER:
            if grouped[commit_type]:
                highlights.append(grouped[commit_type][0])
                break

    lines: list[str] = [f"## v{version}"]

    if highlights:
        lines.extend(["", "### 主要版本信息"])
        lines.extend([f"- {item}" for item in highlights])

    has_updates = any(grouped[commit_type] for commit_type in SECTION_ORDER)
    if has_updates:
        lines.extend(["", "### 更新内容"])
        for commit_type in SECTION_ORDER:
            items = grouped[commit_type]
            if not items:
                continue
            lines.extend(["", f"#### {SECTION_TITLES[commit_type]}"])
            lines.extend([f"- {item}" for item in items])

    if compare_ref and repo_slug:
        lines.extend(
            [
                "",
                f"Full Changelog: https://github.com/{repo_slug}/compare/{compare_ref}...v{version}",
            ]
        )

    return "\n".join(lines).strip() + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate GitHub release notes from conventional commits.")
    parser.add_argument("--version", required=True, help="Release version without the leading v.")
    parser.add_argument("--repo", required=True, help="GitHub repository slug, e.g. owner/name.")
    parser.add_argument("--output", required=True, help="Output markdown file path.")
    parser.add_argument("--target-ref", default="HEAD", help="Git ref to inspect. Defaults to HEAD.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    current_tag = f"v{args.version}"
    compare_ref = previous_version_tag(current_tag)
    commits = commits_between(compare_ref, args.target_ref)
    content = build_release_notes(args.version, commits, compare_ref, args.repo)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
