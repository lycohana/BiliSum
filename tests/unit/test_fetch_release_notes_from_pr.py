import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.append(str(REPO_ROOT / "scripts"))

from fetch_release_notes_from_pr import extract_after_marker, find_notes


def _comment(body: str) -> dict:
    return {"body": body, "user": {"login": "github-actions[bot]"}}


def test_extract_after_marker_returns_text_after_marker_line() -> None:
    body = (
        "<!-- release-notes:v1.20.0 -->\n"
        "\n"
        "## v1.20.0\n"
        "\n"
        "### 更新内容\n"
        "- 修复缓存问题\n"
    )

    assert extract_after_marker(body, "<!-- release-notes:v1.20.0 -->") == (
        "## v1.20.0\n"
        "\n"
        "### 更新内容\n"
        "- 修复缓存问题\n"
    )


def test_extract_after_marker_returns_none_when_marker_missing() -> None:
    notes = extract_after_marker(
        "## v1.20.0\n\n- 修复缓存问题\n",
        "<!-- release-notes:v1.20.0 -->",
    )
    assert notes is None


def test_extract_after_marker_returns_none_when_content_empty() -> None:
    notes = extract_after_marker(
        "<!-- release-notes:v1.20.0 -->\n\n",
        "<!-- release-notes:v1.20.0 -->",
    )
    assert notes is None


def test_find_notes_prefers_exact_version_marker() -> None:
    comments = [
        _comment("<!-- release-notes:v1.19.0 -->\n\n## v1.19.0\n- 旧版本\n"),
        _comment("<!-- release-notes:v1.20.0 -->\n\n## v1.20.0\n- 新版本\n"),
    ]

    assert find_notes(comments, "1.20.0") == "## v1.20.0\n- 新版本\n"


def test_find_notes_falls_back_to_any_marker_after_version_change() -> None:
    # Converting the PR to a prerelease changes the version, so the marker is no
    # longer an exact match; the edited notes must still be picked up.
    comments = [
        _comment("<!-- release-notes:v1.20.0 -->\n\n## v1.20.0\n- 用户改过的内容\n"),
    ]

    assert find_notes(comments, "1.20.0-beta.1") == "## v1.20.0\n- 用户改过的内容\n"


def test_find_notes_returns_none_when_no_marker_present() -> None:
    assert find_notes([_comment("普通评论")], "1.20.0") is None
