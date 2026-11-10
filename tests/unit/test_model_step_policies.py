"""Retry and fallback matrix: every ``usage_stage`` x every failure class.

Characterizes how each model step reacts to a failing model (R2 must keep it
unchanged): how many calls reach the model, which per-call transport retry
budget it asks for, and whether the step raises, falls back to its local
substitute, or succeeds.

Failure classes: timeout, transport error, 200 with empty content, unparsable
content, authentication failure (HTTP 401) and incomplete configuration.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from video_sum_core.models.tasks import TaskResult
from video_sum_core.pipeline.model_steps import require_step_router
from video_sum_core.pipeline.real import PipelineSettings, RealPipelineRunner
from video_sum_infra.model_client_fake import FakeModelClient

# Pipeline tests: any model call that bypasses StepRouter fails.
RoutedFake = functools.partial(FakeModelClient, guard=require_step_router)

SUMMARY = {
    "title": "标题",
    "overview": "概览",
    "bulletPoints": ["要点"],
    "chapters": [{"title": "开场", "start": 0, "summary": "开场介绍"}],
}
THIN_MINDMAP = {
    "title": "导图",
    "root": "root",
    "nodes": [
        {
            "id": "root",
            "label": "导图",
            "type": "root",
            "children": [{"id": "t1", "label": "唯一", "type": "theme"}],
        }
    ],
}
GOOD_REPLIES: dict[str, object] = {
    "llm": {"ok": True},
    "summary_full": SUMMARY,
    "summary_chunk": SUMMARY,
    "summary_merge": SUMMARY,
    "aggregate_summary": SUMMARY,
    "knowledge_note": {"knowledgeNoteMarkdown": "# 笔记\n\n## 开场\n\n内容"},
    "mindmap": THIN_MINDMAP,
    "mindmap_repair": THIN_MINDMAP,
    "visual_keyframe_plan": {"keyframes": [{"timestamp_seconds": 3, "concept": "界面"}]},
    "visual_frame": {"caption": "画面"},
    "visual_note_compose": "# 图文\n\n![图](visual://f0001)\n" + "正文" * 200,
}
RESULT = TaskResult(
    overview="概览",
    key_points=["要点"],
    timeline=[{"title": "开场", "start": 0, "summary": "开场介绍"}],
    knowledge_note_markdown="# 笔记\n\n## 开场\n\n内容",
)
SEGMENTS = [{"start": 0, "end": 5, "text": "第一段"}, {"start": 5, "end": 9, "text": "第二段"}]
TRANSCRIPT = "第一段\n第二段"
RETRY_COUNT = 2  # summary_chunk_retry_count -> 3 attempts where the stage retries


def _auth_error() -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://llm.example.com/v1/chat/completions")
    response = httpx.Response(401, request=request, json={"error": {"message": "bad key"}})
    return httpx.HTTPStatusError("401", request=request, response=response)


FAILURES: dict[str, Callable[[], object]] = {
    "timeout": lambda: httpx.ReadTimeout("slow"),
    "transport": lambda: httpx.ConnectError("refused"),
    "empty": lambda: "",
    "parse": lambda: "definitely not json {",
    "auth": _auth_error,
}
CONFIG_BROKEN = {
    "llm_model": "",
    "visual_evidence_model": "",
    "visual_evidence_api_key": "",
    "llm_api_key": "k",
}


def _runner(tmp_path: Path, stage: str, failure: str) -> tuple[RealPipelineRunner, FakeModelClient]:
    def reply(request):
        if request.usage_stage == stage and failure != "config":
            return FAILURES[failure]()
        return GOOD_REPLIES[request.usage_stage]

    fake = RoutedFake(default=reply)
    settings: dict[str, object] = {
        "tasks_dir": tmp_path / "tasks",
        "llm_enabled": True,
        "llm_api_key": "test-key",
        "llm_base_url": "https://llm.example.com/v1",
        "llm_model": "test-model",
        "summary_chunk_retry_count": RETRY_COUNT,
        "summary_chunk_concurrency": 1,
        "summary_context_mode": "full",
        "visual_evidence_use_llm": True,
        "visual_multimodal_enabled": True,
        "visual_evidence_base_url": "https://vlm.example.com/v1",
        "visual_evidence_model": "vision-model",
        "visual_evidence_api_key": "vision-key",
        "visual_evidence_retry_count": 1,
    }
    if failure == "config":
        if stage.startswith("visual_frame") or stage == "visual_note_compose":
            settings.update(
                visual_evidence_model="", llm_model="", visual_evidence_api_key="", llm_api_key=""
            )
            settings["llm_enabled"] = True
        else:
            settings["llm_model"] = ""
    if stage in {"summary_chunk", "summary_merge"}:
        settings["summary_context_mode"] = "chunked"
    return RealPipelineRunner(PipelineSettings(**settings), model_client=fake), fake  # type: ignore[arg-type]


def _summary_outcome(runner: RealPipelineRunner, **kwargs: object) -> str:
    events: list[dict[str, object]] = []
    runner._summarize(
        TRANSCRIPT,
        SEGMENTS,
        "标题",
        lambda _s, _p, _m, payload=None: events.append(payload or {}),
        **kwargs,  # type: ignore[arg-type]
    )
    fallbacks = [str(item.get("fallback")) for item in events if item.get("fallback")]
    return f"fallback:{fallbacks[0]}" if fallbacks else "ok"


def _frame(tmp_path: Path) -> dict[str, object]:
    image = tmp_path / "f0001.jpg"
    image.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    return {
        "frame_id": "f0001",
        "timestamp_seconds": 3.0,
        "timestamp": "00:03",
        "_absolute_path": str(image),
    }


def _drive(stage: str, runner: RealPipelineRunner, tmp_path: Path) -> str:
    if stage == "llm":
        runner._preflight_llm_availability()
        return "ok"
    if stage in {"summary_full", "summary_chunk", "summary_merge"}:
        return _summary_outcome(runner, summary_scope="summary")
    if stage == "aggregate_summary":
        return _summary_outcome(runner, source_kind="aggregate_series", summary_scope="summary")
    if stage == "knowledge_note":
        return _summary_outcome(runner)
    if stage in {"mindmap", "mindmap_repair"}:
        mindmap, _usage = runner._generate_mindmap_with_llm(title="标题", result=RESULT)
        labels = {node.label for node in mindmap.nodes}
        return "ok" if "唯一" in labels else "fallback"
    if stage == "visual_keyframe_plan":
        plan = runner._build_visual_keyframe_plan("标题", RESULT, "vlm_integrated")
        return "fallback" if plan.get("planner") == "local" else "ok"
    if stage == "visual_frame":
        if not runner._visual_llm_available():
            return "skipped"
        [observation] = runner._describe_visual_frames(
            [_frame(tmp_path)], "标题", RESULT, usage_records=[]
        )
        return "ok" if observation["caption"] == "画面" else "fallback"
    if stage == "visual_note_compose":
        observations = [
            {"frame_id": "f0001", "timestamp_seconds": 3.0, "caption": "画面", "importance": 4}
        ]
        insert_plan = {
            "insertions": [
                {"frame_id": "f0001", "anchor_heading": "开场", "markdown_image": "visual://f0001"}
            ]
        }
        note = runner._compose_visual_enhanced_note(
            title="标题",
            result=RESULT,
            observations=observations,
            insert_plan=insert_plan,
            mode="vlm_integrated",
        )
        return "ok" if note.startswith("# 图文") else "fallback"
    raise AssertionError(stage)


# (calls reaching the model for the stage, transport retries per call, outcome)
EXPECTED: dict[str, dict[str, tuple[int, int | None, str]]] = {
    "llm": {
        "timeout": (3, 0, "raise:VideoSumError"),
        "transport": (3, 0, "raise:VideoSumError"),
        "empty": (3, 0, "raise:VideoSumError"),
        "parse": (3, 0, "raise:VideoSumError"),
        "auth": (1, 0, "raise:LLMAuthenticationError"),
        "config": (0, None, "raise:LLMConfigurationError"),
    },
    "summary_full": {
        "timeout": (3, 0, "raise:ReadTimeout"),
        "transport": (3, 0, "raise:ConnectError"),
        "empty": (3, 0, "raise:VideoSumError"),
        "parse": (1, 0, "raise:VideoSumError"),
        "auth": (1, 0, "fallback:rules"),
        "config": (0, None, "fallback:rules"),
    },
    "summary_chunk": {
        # any error is retried, then the chunk is skipped; all chunks failed -> raise
        "timeout": (3, RETRY_COUNT, "raise:VideoSumError"),
        "transport": (3, RETRY_COUNT, "raise:VideoSumError"),
        "empty": (3, RETRY_COUNT, "raise:VideoSumError"),
        "parse": (3, RETRY_COUNT, "raise:VideoSumError"),
        "auth": (3, RETRY_COUNT, "raise:VideoSumError"),
        "config": (0, None, "fallback:rules"),
    },
    "summary_merge": {
        "timeout": (3, 0, "raise:ReadTimeout"),
        "transport": (3, 0, "raise:ConnectError"),
        "empty": (3, 0, "raise:VideoSumError"),
        "parse": (1, 0, "raise:VideoSumError"),
        "auth": (1, 0, "fallback:rules"),
        "config": (0, None, "fallback:rules"),
    },
    "aggregate_summary": {
        "timeout": (3, 0, "raise:ReadTimeout"),
        "transport": (3, 0, "raise:ConnectError"),
        "empty": (3, 0, "raise:VideoSumError"),
        "parse": (1, 0, "raise:VideoSumError"),
        "auth": (1, 0, "fallback:rules"),
        "config": (0, None, "fallback:rules"),
    },
    "knowledge_note": {
        "timeout": (3, 0, "raise:ReadTimeout"),
        "transport": (3, 0, "raise:ConnectError"),
        "empty": (3, 0, "fallback:knowledge_note_rules"),
        "parse": (1, 0, "fallback:knowledge_note_rules"),
        "auth": (1, 0, "fallback:knowledge_note_rules"),
        "config": (0, None, "fallback:rules"),
    },
    "mindmap": {
        "timeout": (3, 0, "raise:ReadTimeout"),
        "transport": (3, 0, "raise:ConnectError"),
        "empty": (3, 0, "raise:VideoSumError"),
        "parse": (1, 0, "raise:VideoSumError"),
        "auth": (1, 0, "raise:LLMAuthenticationError"),
        "config": (0, None, "raise:LLMConfigurationError"),
    },
    "mindmap_repair": {
        "timeout": (3, 0, "fallback"),
        "transport": (3, 0, "fallback"),
        "empty": (3, 0, "fallback"),
        "parse": (1, 0, "fallback"),
        "auth": (1, 0, "fallback"),
        "config": (0, None, "raise:LLMConfigurationError"),
    },
    "visual_keyframe_plan": {
        "timeout": (1, 1, "fallback"),
        "transport": (1, 1, "fallback"),
        "empty": (1, 1, "fallback"),
        "parse": (1, 1, "fallback"),
        "auth": (1, 1, "fallback"),
        "config": (0, None, "fallback"),
    },
    "visual_frame": {
        "timeout": (2, 0, "fallback"),
        "transport": (2, 0, "fallback"),
        "empty": (2, 0, "fallback"),
        "parse": (2, 0, "fallback"),
        "auth": (2, 0, "fallback"),
        "config": (0, None, "skipped"),
    },
    "visual_note_compose": {
        "timeout": (1, 0, "fallback"),
        "transport": (1, 0, "fallback"),
        "empty": (1, 0, "fallback"),
        "parse": (
            1,
            0,
            "fallback",
        ),  # free-form text without visual:// images is rejected downstream
        "auth": (1, 0, "fallback"),
        "config": (0, None, "fallback"),
    },
}
CASES = [(stage, failure) for stage, row in EXPECTED.items() for failure in row]


@pytest.mark.parametrize(("stage", "failure"), CASES, ids=[f"{s}-{f}" for s, f in CASES])
def test_stage_failure_handling_matches_characterized_matrix(
    stage: str, failure: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("video_sum_core.pipeline.real.time.sleep", lambda _seconds: None)
    runner, fake = _runner(tmp_path, stage, failure)

    try:
        outcome = _drive(stage, runner, tmp_path)
    except Exception as exc:  # noqa: BLE001 - the matrix records the raised type
        outcome = f"raise:{type(exc).__name__}"

    calls = fake.calls_for(stage)
    expected_calls, expected_transport_retries, expected_outcome = EXPECTED[stage][failure]
    assert (len(calls), calls[0].transport_retries if calls else None, outcome) == (
        expected_calls,
        expected_transport_retries,
        expected_outcome,
    )
