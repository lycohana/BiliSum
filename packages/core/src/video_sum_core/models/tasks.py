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


class LLMUsageRecord(BaseModel):
    """Usage returned by one logical LLM/VLM request.

    The provider-specific response fields are normalized here so the UI can
    explain which independent step consumed tokens without storing prompts,
    model input, or credentials.
    """

    stage: str = "llm"
    provider: str | None = None
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cache_hit_tokens: int | None = None
    cache_miss_tokens: int | None = None
    cache_creation_tokens: int | None = None


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
    llm_prompt_cache_hit_tokens: int | None = None
    llm_prompt_cache_miss_tokens: int | None = None
    llm_prompt_cache_creation_tokens: int | None = None
    llm_usage_breakdown: list[LLMUsageRecord] = Field(default_factory=list)
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


_LLM_USAGE_TOTAL_FIELDS: tuple[tuple[str, str], ...] = (
    ("llm_prompt_tokens", "prompt_tokens"),
    ("llm_completion_tokens", "completion_tokens"),
    ("llm_total_tokens", "total_tokens"),
    ("llm_prompt_cache_hit_tokens", "cache_hit_tokens"),
    ("llm_prompt_cache_miss_tokens", "cache_miss_tokens"),
    ("llm_prompt_cache_creation_tokens", "cache_creation_tokens"),
)


def _coerce_usage_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return number if number is not None and number >= 0 else None


def merge_llm_usage_records(
    result: TaskResult,
    records: list[LLMUsageRecord | dict[str, object]] | None,
) -> TaskResult:
    """Append independent request usage and keep legacy totals compatible."""

    normalized_records: list[LLMUsageRecord] = []
    for record in records or []:
        try:
            normalized_records.append(
                record if isinstance(record, LLMUsageRecord) else LLMUsageRecord.model_validate(record)
            )
        except (TypeError, ValueError):
            continue
    if not normalized_records:
        return result

    existing = list(result.llm_usage_breakdown or [])
    combined = existing + normalized_records
    update: dict[str, object] = {"llm_usage_breakdown": combined}
    for result_field, record_field in _LLM_USAGE_TOTAL_FIELDS:
        values = [
            value
            for record in combined
            if (value := _coerce_usage_int(getattr(record, record_field, None))) is not None
        ]
        if not values:
            continue
        if existing:
            update[result_field] = sum(values)
        else:
            update[result_field] = (_coerce_usage_int(getattr(result, result_field, None)) or 0) + sum(values)
    return result.model_copy(update=update)


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
