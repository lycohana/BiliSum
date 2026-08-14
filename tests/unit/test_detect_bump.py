from pathlib import Path
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.append(str(REPO_ROOT / "scripts"))

from detect_bump import detect_bump


@pytest.mark.parametrize(
    ("commits", "expected"),
    [
        ([("fix(ui): 调整颜色", "")], ""),
        ([("fix(ui)*: 调整颜色", "")], "patch"),
        ([("fix*(knowledge): 修复知识库索引 (#98)", "")], "patch"),
        ([("feat(desktop)*: 新增更新入口", "")], "minor"),
        ([("feat!: 切换配置格式", "")], "major"),
        ([("fix(core)*!: 重构存储协议", "")], "major"),
        ([("refactor(core): 重构存储协议", "BREAKING CHANGE: storage layout changed")], "major"),
        ([("docs(readme)*: 更新说明", "")], ""),
        # 只在正文里提到 "BREAKING CHANGE" 字样不算 breaking（解释性文字误判修复）
        ([("feat(ci): 重构发布流程", "触发条件为带 * 标记（或 BREAKING CHANGE）的提交")], ""),
        ([("fix(ui): 调整颜色", "说明中提到 BREAKING CHANGE 但无 footer")], ""),
        # 规范 footer 才算 breaking
        ([("feat(core): 新增功能", "BREAKING CHANGE: 存储格式变更")], "major"),
        ([("feat(core): 新增功能", "BREAKING-CHANGE: 存储格式变更")], "major"),
    ],
)
def test_detect_bump_requires_release_marker_or_breaking_change(
    commits: list[tuple[str, str]],
    expected: str,
) -> None:
    assert detect_bump(commits) == expected
