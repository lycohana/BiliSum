from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, Field

from video_sum_core.models.tasks import TaskInput, TaskResult


class PipelineEvent(BaseModel):
    stage: str
    progress: int = 0
    message: str
    payload: dict[str, object] = Field(default_factory=dict)


class PipelineContext(BaseModel):
    task_id: str
    task_input: TaskInput


PipelineEventReporter = Callable[[PipelineEvent], None]


@dataclass(slots=True)
class PipelineTranscription:
    """Artifacts handed from the transcription stage to the LLM stage.

    Keeping this boundary explicit lets the service release a transcription
    worker as soon as the transcript is durable, while a separate LLM worker
    continues processing the same task.
    """

    title: str
    transcript: str
    segments: list[dict[str, object]]
    task_dir: Path
    source_kind: str | None = None
    source_path: Path | None = None


class PipelineRunner:
    """Minimal orchestration contract for future pipeline implementations."""

    def preflight(
        self,
        context: PipelineContext,
        on_event: PipelineEventReporter | None = None,
    ) -> None:
        return None

    def supports_staged_execution(self) -> bool:
        """Whether the runner exposes independent transcription/LLM stages."""

        return False

    def run_transcription(
        self,
        context: PipelineContext,
        on_event: PipelineEventReporter | None = None,
    ) -> PipelineTranscription:
        raise NotImplementedError("This pipeline runner does not support staged execution.")

    def run_summary(
        self,
        context: PipelineContext,
        transcription: PipelineTranscription,
        on_event: PipelineEventReporter | None = None,
    ) -> TaskResult:
        raise NotImplementedError("This pipeline runner does not support staged execution.")

    def run(
        self,
        context: PipelineContext,
        on_event: PipelineEventReporter | None = None,
    ) -> tuple[list[PipelineEvent], TaskResult]:
        event = PipelineEvent(
            stage="accepted",
            progress=0,
            message="Pipeline scaffold is ready. Processing implementation is pending.",
        )
        if on_event is not None:
            on_event(event)
        result = TaskResult()
        return [event], result
