from datetime import datetime, timezone

from video_sum_core.markdown_exports import build_export_filename, build_task_markdown_export


def test_build_task_markdown_export_for_obsidian_includes_frontmatter_and_sections() -> None:
    content = build_task_markdown_export(
        title="函数专题",
        overview="讲解函数的定义与例子。",
        knowledge_note_markdown="# 函数专题\n\n## 核心概览\n\n函数是映射关系。",
        key_points=["定义域和值域", "二次函数例子"],
        timeline=[{"title": "函数定义", "start": 12.0, "summary": "介绍定义域和值域。"}],
        source_url="https://www.bilibili.com/video/BV1test",
        platform="bilibili",
        video_id="video-1",
        canonical_id="BV1test",
        task_id="task-1",
        created_at=datetime(2026, 4, 22, 1, 0, tzinfo=timezone.utc),
        exported_at=datetime(2026, 4, 22, 9, 30, tzinfo=timezone.utc),
        tags=["vidmind", "bilibili", "video-summary"],
        target="obsidian",
        mindmap_path="C:/vault/mindmaps/task-1.json",
    )

    assert content.startswith("---\n")
    assert 'title: "函数专题"' in content
    assert 'canonical_id: "BV1test"' in content
    assert '  - "vidmind"' in content
    assert "\n# 函数专题\n" in content
    assert "## 核心概览" in content
    assert "## 关键要点" in content
    assert "## 章节时间线" in content
    assert "## 知识笔记" in content
    assert "## 思维导图引用信息" in content
    assert "- 导图文件：" in content


def test_build_task_markdown_export_for_markdown_omits_frontmatter_and_tolerates_missing_mindmap() -> None:
    content = build_task_markdown_export(
        title="线性代数",
        overview="概览",
        knowledge_note_markdown="## 知识笔记\n\n矩阵与向量。",
        key_points=[],
        timeline=[],
        source_url="https://example.com/video",
        platform="unknown",
        video_id=None,
        canonical_id=None,
        task_id="task-2",
        created_at=datetime(2026, 4, 22, 1, 0, tzinfo=timezone.utc),
        exported_at=datetime(2026, 4, 22, 9, 30, tzinfo=timezone.utc),
        tags=["vidmind"],
        target="markdown",
        mindmap_path=None,
    )

    assert not content.startswith("---\n")
    assert "## 思维导图引用信息" not in content
    assert "- 暂无关键要点。" in content
    assert "- 暂无章节时间线。" in content


def test_build_task_markdown_export_can_include_transcript() -> None:
    content = build_task_markdown_export(
        title="转写导出",
        overview="概览",
        knowledge_note_markdown="## 知识笔记\n\n内容。",
        key_points=[],
        timeline=[],
        source_url="https://example.com/video",
        platform="unknown",
        video_id=None,
        canonical_id=None,
        task_id="task-3",
        created_at=datetime(2026, 4, 22, 1, 0, tzinfo=timezone.utc),
        exported_at=datetime(2026, 4, 22, 9, 30, tzinfo=timezone.utc),
        tags=["vidmind"],
        target="markdown",
        transcript_text="[00:00] 第一条",
        include_transcript=True,
    )

    assert "## 转写全文" in content
    assert "```text\n[00:00] 第一条\n```" in content


def test_build_export_filename_sanitizes_invalid_characters() -> None:
    file_name = build_export_filename('函数/映射: 入门?', datetime(2026, 4, 22, 9, 30, tzinfo=timezone.utc))

    assert file_name.endswith("2026-04-22.md")
    assert "/" not in file_name
    assert ":" not in file_name
