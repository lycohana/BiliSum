"""Fetch the (possibly hand-edited) release notes from a merged Release PR.

``prepare-release.yml`` posts the auto-generated release notes as a comment on
the ``Release vX.Y.Z`` PR. The user may edit that comment directly. When the PR
is merged, ``release.yml`` calls this script to find the merged PR by its title
and extract the (edited) notes for use as the GitHub Release body.

The comment keeps a marker as its first line::

    <!-- release-notes:v1.20.0 -->

    ## v1.20.0
    ...

Everything after the marker line is treated as the release notes. The user
should keep that first line intact while editing the rest.

Exit code 0 means notes were found and written to ``--output``; any non-zero
exit code means "not found" and the caller falls back to auto-generation.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

MARKER_PREFIX = "<!-- release-notes:"
# A prerelease suffix (e.g. -beta.1) never appears in the release branch name:
# the head branch is always ``release/v<stable>``.
PRERELEASE_SUFFIX_RE = re.compile(r"-(alpha|beta|rc)(\.\d+)?$")


def run_gh(*args: str) -> str:
    proc = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        message = proc.stderr.strip() or proc.stdout.strip()
        raise SystemExit(f"gh {' '.join(args)} failed: {message}")
    return proc.stdout.strip()


def find_merged_pr(repo: str, version: str) -> str | None:
    """Return the number of the merged PR titled ``Release v{version}``."""
    target_title = f"Release v{version}"

    # 1) Exact-title match over the most recently merged PRs (avoids relying on
    #    GitHub search indexing right after a merge).
    raw = run_gh(
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "merged",
        "--json",
        "number,title",
        "--limit",
        "200",
        "--jq",
        ".[] | [.number, .title] | @tsv",
    )
    for line in raw.splitlines():
        number, _, title = line.partition("\t")
        if title == target_title:
            return number

    # 2) Fallback: look up by head branch ``release/v<stable>``, which covers
    #    prerelease versions whose title changed after a label was applied.
    stable = PRERELEASE_SUFFIX_RE.sub("", version)
    number = run_gh(
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "merged",
        "--head",
        f"release/v{stable}",
        "--json",
        "number",
        "--jq",
        ".[0].number // empty",
    )
    return number or None


def list_issue_comments(repo: str, pr_number: str) -> list[dict]:
    raw = run_gh(
        "api",
        f"repos/{repo}/issues/{pr_number}/comments",
        "--paginate",
    )
    if not raw:
        return []
    return json.loads(raw)


def extract_after_marker(body: str, marker: str) -> str | None:
    """Return the text after *marker*'s line, or None when *marker* is absent."""
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if line.strip().startswith(marker):
            content = lines[index + 1 :]
            # Drop the blank separator between the marker line and the notes.
            while content and not content[0].strip():
                content.pop(0)
            notes = "\n".join(content).rstrip() + "\n"
            return notes if notes.strip() else None
    return None


def find_notes(comments: list[dict], version: str) -> str | None:
    """Extract release notes from a list of PR comments.

    Prefer the exact version marker; fall back to any release-notes marker so a
    version change (e.g. converting to ``-beta.1``) still picks up the edited
    comment.
    """
    exact_marker = f"<!-- release-notes:v{version} -->"
    for comment in comments:
        notes = extract_after_marker(comment.get("body", ""), exact_marker)
        if notes:
            return notes
    for comment in comments:
        notes = extract_after_marker(comment.get("body", ""), MARKER_PREFIX)
        if notes:
            return notes
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="GitHub repository slug, e.g. owner/name.")
    parser.add_argument("--version", required=True, help="Release version without the leading v.")
    parser.add_argument("--output", required=True, help="Output markdown file path.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    pr_number = find_merged_pr(args.repo, args.version)
    if pr_number is None:
        print(f"No merged Release PR found for v{args.version}.", file=sys.stderr)
        return 1
    notes = find_notes(list_issue_comments(args.repo, pr_number), args.version)
    if notes is None:
        print(f"PR #{pr_number} has no release-notes comment.", file=sys.stderr)
        return 1
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(notes, encoding="utf-8")
    print(f"Wrote release notes from PR #{pr_number} comment to {output}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
