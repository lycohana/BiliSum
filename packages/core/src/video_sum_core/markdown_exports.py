from __future__ import annotations

from datetime import datetime
import json
import re
from pathlib import Path

from video_sum_core.utils import sanitize_filename


def build_export_filename(title: str, export_time: datetime) -> str:
    safe_title = re.sub(r"\s+", " ", sanitize_filename(str(title or "").strip())).strip(" .")
    if not safe_title:
        safe_title = "VidMind Note"
    return f"{safe_title} {export_time.date().isoformat()}.md"


def build_task_markdown_export(
    *,
    title: str,
    overview: str,
    knowledge_note_markdown: str,
    key_points: list[str],
    timeline: list[dict[str, object]],
    source_url: str,
    platform: str,
    video_id: str | None,
    canonical_id: str | None,
    task_id: str,
    created_at: datetime,
    exported_at: datetime,
    tags: list[str],
    target: str = "obsidian",
    mindmap_path: str | None = None,
    transcript_text: str = "",
    include_transcript: bool = False,
) -> str:
    normalized_title = str(title or "").strip() or "VidMind 知识笔记"
    body = _build_markdown_body(
        title=normalized_title,
        overview=overview,
        knowledge_note_markdown=knowledge_note_markdown,
        key_points=key_points,
        timeline=timeline,
        mindmap_path=mindmap_path,
        transcript_text=transcript_text,
        include_transcript=include_transcript,
    )
    if str(target or "obsidian").strip().lower() != "obsidian":
        return body

    frontmatter = _build_frontmatter(
        {
            "title": normalized_title,
            "source_url": str(source_url or ""),
            "platform": str(platform or ""),
            "video_id": str(video_id or ""),
            "canonical_id": str(canonical_id or ""),
            "task_id": str(task_id or ""),
            "created_at": created_at.isoformat(),
            "exported_at": exported_at.isoformat(),
            "tags": [str(tag).strip() for tag in tags if str(tag).strip()],
        }
    )
    return f"{frontmatter}\n\n{body}".strip()


def _build_frontmatter(payload: dict[str, object]) -> str:
    lines = ["---"]
    for key, value in payload.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {json.dumps(str(item), ensure_ascii=False)}")
            continue
        lines.append(f"{key}: {json.dumps(str(value or ''), ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines)


def _build_markdown_body(
    *,
    title: str,
    overview: str,
    knowledge_note_markdown: str,
    key_points: list[str],
    timeline: list[dict[str, object]],
    mindmap_path: str | None,
    transcript_text: str,
    include_transcript: bool,
) -> str:
    sections = [
        f"# {title}",
        "## 核心概览",
        str(overview or "").strip() or "暂无核心概览。",
        "## 关键要点",
        _format_key_points(key_points),
        "## 章节时间线",
        _format_timeline(timeline),
        "## 知识笔记",
        _normalize_embedded_note(knowledge_note_markdown, title),
    ]
    if str(mindmap_path or "").strip():
        sections.extend(
            [
                "## 思维导图引用信息",
                f"- 导图文件：`{Path(str(mindmap_path)).as_posix()}`",
            ]
        )
    if include_transcript and str(transcript_text or "").strip():
        sections.extend(
            [
                "## 转写全文",
                f"```text\n{str(transcript_text).strip()}\n```",
            ]
        )
    return "\n\n".join(section.strip() for section in sections if str(section).strip())


def _format_key_points(key_points: list[str]) -> str:
    items = [str(item).strip() for item in key_points if str(item).strip()]
    if not items:
        return "- 暂无关键要点。"
    return "\n".join(f"- {item}" for item in items)


def _format_timeline(timeline: list[dict[str, object]]) -> str:
    items: list[str] = []
    for entry in timeline:
        title = str(entry.get("title") or "").strip() or "未命名章节"
        summary = str(entry.get("summary") or "").strip()
        start = _format_timestamp(entry.get("start"))
        line = f"- `{start}` **{title}**"
        if summary:
            line = f"{line}: {summary}"
        items.append(line)
    if not items:
        return "- 暂无章节时间线。"
    return "\n".join(items)


def _format_timestamp(value: object) -> str:
    try:
        total_seconds = max(0, int(float(value)))
    except (TypeError, ValueError):
        total_seconds = 0
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _normalize_embedded_note(markdown: str, title: str) -> str:
    text = str(markdown or "").strip()
    if not text:
        return "暂无知识笔记正文。"

    lines = text.splitlines()
    if lines and lines[0].lstrip().startswith("#"):
        heading_text = re.sub(r"^#+\s*", "", lines[0]).strip()
        if heading_text == title or heading_text:
            lines = lines[1:]
            while lines and not lines[0].strip():
                lines = lines[1:]
    normalized = "\n".join(lines).strip()
    return normalized or "暂无知识笔记正文。"
