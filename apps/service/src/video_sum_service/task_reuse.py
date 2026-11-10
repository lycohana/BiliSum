"""Reuse rules: find an existing task that already answers a new request.

Rules:
- same video, same page (missing page = page 1);
- same summary scope, except a full summary (``knowledge_note``) also answers a
  quick-summary (``summary``) request, never the other way round;
- same prompt preset, output language and supplementary instructions;
- engine and visual-note mode only take part when the request names them; the
  engine is compared with the one the task was routed to (``model_routing``),
  whether it was named or came from ``default_engine``;
- a completed task with a result is returned as is; otherwise a task with the
  same options that is still active (queued / running / agent_summarizing) is
  attached to instead of creating a new one. Failed and cancelled tasks never count.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from video_sum_core.models.tasks import UNFINISHED_STATUSES, TaskOptions, TaskStatus

from video_sum_service.schemas import TaskRecord

FULL_SUMMARY_SCOPE = "knowledge_note"
QUICK_SUMMARY_SCOPE = "summary"

ReuseMatch = Literal["completed", "in_progress"]

_DEFAULT_OPTIONS = TaskOptions()
# Language the default prompts produce when none is specified. Unspecified
# (None), the legacy stored default "zh" and an explicit "zh" all mean the same.
_DEFAULT_LANGUAGE = "zh"


def _text(value: str | None) -> str:
    return (value or "").strip()


@dataclass(frozen=True)
class ReuseQuery:
    page_number: int | None = None
    summary_scope: str | None = None
    prompt_preset_id: str | None = None
    language: str | None = None
    extra_instructions: str | None = None
    engine: str | None = None
    visual_note_mode: str | None = None

    @property
    def page(self) -> int:
        return self.page_number if self.page_number is not None else 1

    @property
    def scope(self) -> str:
        return _text(self.summary_scope) or _DEFAULT_OPTIONS.summary_scope

    def accepts_scope(self, scope: str) -> bool:
        if scope == self.scope:
            return True
        return self.scope == QUICK_SUMMARY_SCOPE and scope == FULL_SUMMARY_SCOPE

    def matches_options(self, options: TaskOptions) -> bool:
        if not self.accepts_scope(_text(options.summary_scope) or _DEFAULT_OPTIONS.summary_scope):
            return False
        if _text(options.prompt_preset_id) != _text(self.prompt_preset_id):
            return False
        if (_text(options.language) or _DEFAULT_LANGUAGE) != (
            _text(self.language) or _DEFAULT_LANGUAGE
        ):
            return False
        if _text(options.extra_instructions) != _text(self.extra_instructions):
            return False
        # The engine the task actually runs with (explicit or via default_engine).
        if _text(self.engine) and options.routing.engine != _text(self.engine):
            return False
        if _text(self.visual_note_mode) and _text(options.visual_note_mode) != _text(
            self.visual_note_mode
        ):
            return False
        return True


def _page_of(task: TaskRecord) -> int:
    return task.page_number if task.page_number is not None else 1


def find_reusable_task(
    tasks: Iterable[TaskRecord], query: ReuseQuery
) -> tuple[TaskRecord, ReuseMatch] | None:
    candidates = [
        task
        for task in tasks
        if _page_of(task) == query.page and query.matches_options(task.task_input.options)
    ]
    newest_first = sorted(candidates, key=lambda task: task.created_at, reverse=True)

    completed = [
        task
        for task in newest_first
        if task.status == TaskStatus.COMPLETED and task.result is not None
    ]
    if completed:
        # A task of exactly the requested scope beats a broader one that also answers it.
        exact = [task for task in completed if task.task_input.options.summary_scope == query.scope]
        return (exact or completed)[0], "completed"

    active = [task for task in newest_first if task.status in UNFINISHED_STATUSES]
    if active:
        return active[0], "in_progress"
    return None
