from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field, field_validator
from video_sum_infra.config import ENGINES


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
    PAUSED = "paused"
    CANCELLED = "cancelled"
    # Waiting for an Agent to deliver model-step results (F2). Lives only in
    # the database: it holds no worker slot and is never resubmitted on restart.
    AGENT_SUMMARIZING = "agent_summarizing"


# Task status sets. Everything that asks "is this task still alive / finished /
# safe to resubmit after a restart" must use these instead of listing statuses.
# Active: the task still occupies the system (keeps a CLI-managed service alive).
# ``agent_summarizing`` is not active: a task waiting for an Agent holds no
# worker and must not stop idle shutdown; the CLI/MCP start the service again
# when the Agent comes back.
ACTIVE_STATUSES: frozenset[TaskStatus] = frozenset({TaskStatus.QUEUED, TaskStatus.RUNNING})
# Terminal: the task has ended and will not change on its own. ``paused``
# (transcript kept, LLM failed) waits for the user to re-run the LLM step.
TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.PAUSED, TaskStatus.CANCELLED}
)
# Unfinished: not ended yet, including tasks waiting for an Agent. Used to join
# an existing task (reuse) and to count work still in progress for an Agent.
UNFINISHED_STATUSES: frozenset[TaskStatus] = ACTIVE_STATUSES | {TaskStatus.AGENT_SUMMARIZING}
# Recoverable: on service start these are resubmitted to the worker.
RECOVERABLE_STATUSES: frozenset[TaskStatus] = frozenset({TaskStatus.QUEUED, TaskStatus.RUNNING})

TRANSCRIPT_ONLY_SCOPE = "transcript_only"
"""``summary_scope`` value: finish with the transcript snapshot, no summary or model calls."""

ModelRoute = Literal["agent", "internal", "local"]
"""Who produced a model step that ran (执行来源 without "none")."""
ModelSource = ModelRoute | Literal["none"]
"""执行来源 of one model step: Agent, the 内置模型, 本地兜底 (rules), or the step did not run."""

EXTRA_INSTRUCTIONS_MAX_CHARS = 1000
"""Upper bound for ``TaskOptions.extra_instructions`` (补充要求), in characters."""


def blank_to_none(value: object) -> object:
    """Strip text; blank text means "not specified" (None)."""
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def check_extra_instructions_length(value: str | None) -> str | None:
    if value is not None and len(value) > EXTRA_INSTRUCTIONS_MAX_CHARS:
        raise ValueError(
            f"补充要求不能超过 {EXTRA_INSTRUCTIONS_MAX_CHARS} 字（当前 {len(value)} 字）。"
        )
    return value


BATCH_ID_MAX_CHARS = 128


class ModelRouting(BaseModel):
    """Per-task route table, fixed when the task is created (F2).

    ``agent_steps`` lists the model steps (``model_steps.STEPS`` names) handed
    to the Agent; every other step follows the default chain (internal model ->
    local fallback). A builtin task has no capabilities and no agent steps.
    """

    engine: Literal["builtin", "agent"] = "builtin"
    agent_capabilities: list[str] = Field(default_factory=list)
    agent_steps: list[str] = Field(default_factory=list)

    @property
    def is_agent(self) -> bool:
        return self.engine == "agent"

    def delegates(self, step: str) -> bool:
        return self.is_agent and step in self.agent_steps


BUILTIN_ROUTING = ModelRouting()
"""Route of a task created without routing (older tasks, internal callers)."""


class TaskOptions(BaseModel):
    scheduled_at: AwareDatetime | None = None
    # None = not specified: prompts stay exactly as before (no language section).
    language: str | None = Field(default=None, max_length=32)
    summary_mode: str = "auto"
    summary_scope: str = "knowledge_note"
    prompt_preset_id: str | None = None
    prefer_subtitles: bool = True
    export_formats: list[str] = Field(default_factory=lambda: ["md", "json"])
    visual_note_mode: str | None = None
    # Explicit engine (``builtin`` / ``agent``); None = follow ``default_engine``.
    engine: str | None = None
    # 补充要求: user wording added to the prompt in its own labelled section.
    extra_instructions: str | None = None
    # Shared by every task one create request produced (Agent claims by batch).
    batch_id: str | None = Field(default=None, max_length=BATCH_ID_MAX_CHARS)
    # Fixed at creation; None = created before routing existed -> builtin.
    model_routing: ModelRouting | None = None

    @field_validator("language", "extra_instructions", "engine", "batch_id", mode="before")
    @classmethod
    def _normalize_optional_text(cls, value: object) -> object:
        return blank_to_none(value)

    @field_validator("extra_instructions")
    @classmethod
    def _limit_extra_instructions(cls, value: str | None) -> str | None:
        return check_extra_instructions_length(value)

    @field_validator("engine")
    @classmethod
    def _check_engine(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.lower()
        if value not in ENGINES:
            raise ValueError(f"不支持的引擎：{value}（可选：{', '.join(ENGINES)}）。")
        return value

    @property
    def transcript_only(self) -> bool:
        return self.summary_scope == TRANSCRIPT_ONLY_SCOPE

    @property
    def routing(self) -> ModelRouting:
        return self.model_routing or BUILTIN_ROUTING


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


class AgentIdentity(BaseModel):
    """Who answered an Agent step, as the Agent reported it (BiliSum cannot verify it)."""

    name: str
    model: str | None = None
    self_reported: bool = True


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
    # Which 补充要求 / output language produced this result (None = not specified).
    extra_instructions: str | None = None
    language: str | None = None
    # 执行来源 per model step (summary, knowledge_note, keyframe_plan, visual_frame,
    # visual_compose, mindmap). Empty for results stored before F1b.
    model_sources: dict[str, ModelSource] = Field(default_factory=dict)
    # Agent self-reported name/model per item the Agent answered, keyed by item key
    # ``usage_stage#index`` (F2). A task taken over by another Agent keeps both names.
    # Results stored before this change are keyed by plain ``usage_stage``.
    agent_identities: dict[str, AgentIdentity] = Field(default_factory=dict)


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
