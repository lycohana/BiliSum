import json
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


TRANSCRIPTION_FILENAME = "transcription.json"
TRANSCRIPTION_FORMAT_VERSION = 1


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

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": TRANSCRIPTION_FORMAT_VERSION,
                "title": self.title,
                "transcript": self.transcript,
                "segments": self.segments,
                "task_dir": str(self.task_dir),
                "source_kind": self.source_kind,
                "source_path": str(self.source_path) if self.source_path is not None else None,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str) -> "PipelineTranscription":
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("transcription payload must be a JSON object")
        source_path = data.get("source_path")
        return cls(
            title=str(data["title"]),
            transcript=str(data["transcript"]),
            segments=list(data.get("segments") or []),
            task_dir=Path(data["task_dir"]),
            source_kind=data.get("source_kind"),
            source_path=Path(source_path) if source_path else None,
        )

    def save(self) -> Path:
        """Atomically write ``task_dir/transcription.json`` and return its path."""

        path = self.task_dir / TRANSCRIPTION_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f"{path.name}.tmp")
        tmp_path.write_text(self.to_json(), encoding="utf-8")
        tmp_path.replace(path)
        return path

    @classmethod
    def load(cls, task_dir: Path) -> "PipelineTranscription | None":
        """Load ``task_dir/transcription.json``; ``None`` if absent or unreadable."""

        path = task_dir / TRANSCRIPTION_FILENAME
        try:
            transcription = cls.from_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError, TypeError):
            return None
        # The directory we were asked to read from wins over the stored one,
        # so a relocated data directory still resolves task artifacts.
        transcription.task_dir = task_dir
        return transcription


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

    def task_dir(self, task_id: str) -> Path | None:
        """Directory holding a task's artifacts (incl. ``transcription.json``).

        ``None`` means the runner has no durable task directory, so staged
        transcriptions cannot be recovered from disk.
        """

        return None

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

    def build_transcript_result(self, transcription: PipelineTranscription) -> TaskResult:
        """Result of a ``transcript_only`` task: the transcript snapshot, no model calls."""

        return TaskResult(
            transcript_text=transcription.transcript, segments=list(transcription.segments)
        )

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
