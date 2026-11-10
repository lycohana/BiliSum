"""C2: extra instructions (补充要求) and output language in assembled prompts.

Observed through the model-request seam (FakeModelClient): a task without
extra instructions or language must send exactly what it sent before; a task
with them only gains one clearly labelled section at the end of each user
message.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from video_sum_core.models.tasks import InputType, TaskInput, TaskOptions, TaskResult
from video_sum_core.pipeline.base import PipelineContext, PipelineTranscription
from video_sum_core.pipeline.real import PipelineSettings, RealPipelineRunner
from video_sum_infra.model_client_fake import FakeModelClient

SUMMARY = {
    "title": "示例标题",
    "overview": "示例概览",
    "bulletPoints": ["要点一", "要点二"],
    "chapters": [
        {"title": "开场", "start": 0, "summary": "开场介绍"},
        {"title": "方法", "start": 30, "summary": "方法说明"},
    ],
}
NOTE = {"knowledgeNoteMarkdown": "# 知识笔记\n\n## 开场\n\n内容"}
MINDMAP = {
    "title": "导图",
    "root": "root",
    "nodes": [
        {
            "id": "root",
            "label": "导图",
            "type": "root",
            "children": [
                {"id": "t1", "label": "概念", "type": "theme"},
                {"id": "t2", "label": "方法", "type": "theme"},
                {"id": "t3", "label": "应用", "type": "theme"},
            ],
        }
    ],
}
SEGMENTS = [
    {"start": 0, "end": 30, "text": "开场介绍今天的主题"},
    {"start": 30, "end": 60, "text": "讲解具体方法"},
]
TRANSCRIPT = "[00:00] 开场介绍今天的主题\n[00:30] 讲解具体方法"

INSTRUCTIONS_SECTION = (
    "\n\n【补充要求】（来源：用户为本次任务提供的补充要求。只影响内容取舍和侧重点，"
    "不改变上面规定的输出格式、字段和结构）\n重点讲技术细节"
)
LANGUAGE_SECTION_EN = (
    "\n\n【输出语言】（来源：本次任务指定的输出语言）\n"
    "请使用 English 撰写所有面向读者的文字内容；JSON 字段名和输出结构保持不变。"
)


def _runner(tmp_path: Path, fake: FakeModelClient, **overrides: object) -> RealPipelineRunner:
    base: dict[str, object] = dict(
        tasks_dir=tmp_path / "tasks",
        llm_enabled=True,
        llm_provider="openai-compatible",
        llm_api_key="test-key",
        llm_base_url="https://llm.example.com/v1",
        llm_model="test-model",
        summary_context_mode="full",
        summary_chunk_concurrency=1,
    )
    base.update(overrides)
    return RealPipelineRunner(PipelineSettings(**base), model_client=fake)  # type: ignore[arg-type]


def _run_summary(
    tmp_path: Path,
    options: TaskOptions,
    *,
    transcript: str = TRANSCRIPT,
    segments: list[dict[str, object]] = SEGMENTS,
    source_kind: str | None = None,
    **overrides: object,
) -> tuple[TaskResult, FakeModelClient]:
    fake = FakeModelClient(by_stage={"knowledge_note": [NOTE] * 4}, default=SUMMARY)
    runner = _runner(tmp_path, fake, **overrides)
    task_dir = tmp_path / "tasks" / "t1"
    task_dir.mkdir(parents=True, exist_ok=True)
    context = PipelineContext(
        task_id="t1",
        task_input=TaskInput(
            input_type=InputType.URL, source="https://example.com/v", options=options
        ),
    )
    transcription = PipelineTranscription(
        title="示例视频",
        transcript=transcript,
        segments=segments,
        task_dir=task_dir,
        source_kind=source_kind,
    )
    result = runner.run_summary(context, transcription)
    return result, fake


def _user_contents(fake: FakeModelClient) -> list[str]:
    return [
        [m for m in request.payload["messages"] if m["role"] == "user"][-1]["content"]
        for request in fake.requests
    ]


def _without_last_user(fake: FakeModelClient) -> list[dict[str, object]]:
    stripped = []
    for request in fake.requests:
        payload = json.loads(json.dumps(request.payload))
        users = [m for m in payload["messages"] if m["role"] == "user"]
        users[-1]["content"] = "<user>"
        stripped.append({"stage": request.usage_stage, "payload": payload})
    return stripped


def test_instructions_and_language_only_add_labelled_sections(tmp_path: Path) -> None:
    _, plain = _run_summary(tmp_path / "plain", TaskOptions())
    _, directed = _run_summary(
        tmp_path / "directed", TaskOptions(extra_instructions="重点讲技术细节", language="en")
    )

    assert [r.usage_stage for r in directed.requests] == ["summary_full", "knowledge_note"]
    assert _without_last_user(directed) == _without_last_user(plain)
    for before, after in zip(_user_contents(plain), _user_contents(directed), strict=True):
        assert after == before + INSTRUCTIONS_SECTION + LANGUAGE_SECTION_EN


LONG_SEGMENTS = [
    {"start": index * 30, "end": index * 30 + 30, "text": f"第{index}段：" + "内容" * 120}
    for index in range(6)
]
LONG_TRANSCRIPT = "\n".join(
    f"[{index * 30}] {item['text']}" for index, item in enumerate(LONG_SEGMENTS)
)


@pytest.mark.parametrize(
    ("kwargs", "stages"),
    [
        (
            {
                "transcript": LONG_TRANSCRIPT,
                "segments": LONG_SEGMENTS,
                "summary_context_mode": "chunked",
                "summary_chunk_target_chars": 800,
            },
            {"summary_chunk", "summary_merge", "knowledge_note"},
        ),
        ({"source_kind": "aggregate_series"}, {"aggregate_summary", "knowledge_note"}),
    ],
    ids=["chunked", "aggregate"],
)
def test_every_summary_step_gets_the_same_sections(
    tmp_path: Path, kwargs: dict, stages: set[str]
) -> None:
    _, plain = _run_summary(tmp_path / "plain", TaskOptions(), **kwargs)
    _, directed = _run_summary(
        tmp_path / "directed",
        TaskOptions(extra_instructions="重点讲技术细节", language="en"),
        **kwargs,
    )

    assert {r.usage_stage for r in directed.requests} == stages
    assert _without_last_user(directed) == _without_last_user(plain)
    for before, after in zip(_user_contents(plain), _user_contents(directed), strict=True):
        assert after == before + INSTRUCTIONS_SECTION + LANGUAGE_SECTION_EN


def test_language_alone_adds_only_the_language_section(tmp_path: Path) -> None:
    _, plain = _run_summary(tmp_path / "plain", TaskOptions())
    _, directed = _run_summary(tmp_path / "directed", TaskOptions(language="en"))

    for before, after in zip(_user_contents(plain), _user_contents(directed), strict=True):
        assert after == before + LANGUAGE_SECTION_EN


def test_result_records_the_instructions_used(tmp_path: Path) -> None:
    result, _ = _run_summary(
        tmp_path, TaskOptions(extra_instructions="  重点讲技术细节  ", language="en")
    )

    assert result.extra_instructions == "重点讲技术细节"
    assert result.language == "en"


def test_result_without_instructions_records_none(tmp_path: Path) -> None:
    result, _ = _run_summary(tmp_path, TaskOptions())

    assert result.extra_instructions is None
    assert result.language is None


def test_mindmap_follows_instructions_recorded_on_the_result(tmp_path: Path) -> None:
    base = TaskResult(overview="概览", key_points=["一"], knowledge_note_markdown="# 笔记")
    plain = FakeModelClient([MINDMAP])
    _runner(tmp_path, plain)._generate_mindmap_with_llm(title="视频", result=base)
    directed = FakeModelClient([MINDMAP])
    _runner(tmp_path, directed)._generate_mindmap_with_llm(
        title="视频",
        result=base.model_copy(update={"extra_instructions": "重点讲技术细节", "language": "en"}),
    )

    assert _without_last_user(directed) == _without_last_user(plain)
    assert (
        _user_contents(directed)[0]
        == _user_contents(plain)[0] + INSTRUCTIONS_SECTION + LANGUAGE_SECTION_EN
    )


def test_options_default_to_no_instructions_and_no_language() -> None:
    options = TaskOptions()

    assert options.extra_instructions is None
    assert options.language is None


def test_blank_instructions_and_language_mean_unspecified() -> None:
    options = TaskOptions(extra_instructions="   ", language=" ")

    assert options.extra_instructions is None
    assert options.language is None


def test_instructions_at_the_limit_are_accepted() -> None:
    assert TaskOptions(extra_instructions="字" * 1000).extra_instructions == "字" * 1000


def test_instructions_over_the_limit_are_rejected() -> None:
    with pytest.raises(ValidationError, match="1000"):
        TaskOptions(extra_instructions="字" * 1001)
