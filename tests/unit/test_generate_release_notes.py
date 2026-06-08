from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.append(str(REPO_ROOT / "scripts"))

from generate_release_notes import build_release_notes


def test_build_release_notes_uses_release_marked_commits_as_highlights() -> None:
    notes = build_release_notes(
        "1.8.0",
        [
            ("abcdef1234567890", "feat(desktop)*: 接入硅基流动 TeleSpeechASR 并优化设置页保存交互", ""),
            ("1234567890abcdef", "fix(core): 修复本地 ASR 安装后回退问题", ""),
            ("fedcba0987654321", "refactor(ui): 调整运行状态展示结构", ""),
        ],
        "v1.7.0",
        "qianshengli/vidmind",
    )

    assert "### 主要版本信息" in notes
    assert "- desktop: 接入硅基流动 TeleSpeechASR 并优化设置页保存交互 ([abcdef1](https://github.com/qianshengli/vidmind/commit/abcdef1234567890))" in notes
    assert "- core: 修复本地 ASR 安装后回退问题 ([1234567](https://github.com/qianshengli/vidmind/commit/1234567890abcdef))" in notes
    assert "#### Features" in notes
    assert "#### Fixes" in notes
    assert "#### Refactors" in notes
    assert "Full Changelog: https://github.com/qianshengli/vidmind/compare/v1.7.0...v1.8.0" in notes


def test_build_release_notes_falls_back_to_first_releasable_commit_when_no_marker_exists() -> None:
    notes = build_release_notes(
        "1.8.1",
        [
            ("11111112222223333333", "fix: 修复缓存问题", ""),
            ("9999999aaaaaaabbbbbbb", "refactor(desktop): 清理更新状态逻辑", ""),
        ],
        "v1.8.0",
        "qianshengli/vidmind",
    )

    assert "- 修复缓存问题 ([1111111](https://github.com/qianshengli/vidmind/commit/11111112222223333333))" in notes
    assert "#### Fixes" in notes
    assert "#### Refactors" in notes
