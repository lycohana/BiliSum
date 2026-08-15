from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class InputType(str, Enum):
    URL = "url"
    VIDEO_FILE = "video_file"
    AUDIO_FILE = "audio_file"
    TRANSCRIPT_TEXT = "transcript_text"


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskOptions(BaseModel):
    language: str = "zh"
    summary_mode: str = "auto"
    summary_scope: str = "knowledge_note"
    prompt_preset_id: str | None = None
    prefer_subtitles: bool = True
    export_formats: list[str] = Field(default_factory=lambda: ["md", "json"])
    visual_note_mode: str | None = None


class TaskInput(BaseModel):
    input_type: InputType
    source: str
    title: str | None = None
    platform_hint: str | None = None
    options: TaskOptions = Field(default_factory=TaskOptions)


class TaskResult(BaseModel):
    overview: str = ""
    knowledge_note_markdown: str = ""
    transcript_text: str = ""
    segments: list[dict[str, object]] = Field(default_factory=list)
    segment_summaries: list[str] = Field(default_factory=list)
    key_points: list[str] = Field(default_factory=list)
    timeline: list[dict[str, object]] = Field(default_factory=list)
    chapter_groups: list[dict[str, object]] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict)
    llm_prompt_tokens: int | None = None
    llm_completion_tokens: int | None = None
    llm_total_tokens: int | None = None
    mindmap_status: str = "idle"
    mindmap_error_message: str | None = None
    mindmap_artifact_path: str | None = None
    mindmap_updated_at: datetime | None = None
    visual_note_status: str = "idle"
    visual_note_error_message: str | None = None
    visual_note_artifact_path: str | None = None
    visual_enhanced_note_artifact_path: str | None = None
    visual_note_updated_at: datetime | None = None
    visual_note_mode: str = "text"
    visual_frame_count: int = 0
    visual_insert_count: int = 0


class MindMapNode(BaseModel):
    id: str
    label: str
    type: str
    summary: str = ""
    children: list["MindMapNode"] = Field(default_factory=list)
    time_anchor: float | None = None
    source_chapter_titles: list[str] = Field(default_factory=list)
    source_chapter_starts: list[float] = Field(default_factory=list)


class TaskMindMap(BaseModel):
    version: int = 1
    title: str
    root: str
    nodes: list[MindMapNode] = Field(default_factory=list)
