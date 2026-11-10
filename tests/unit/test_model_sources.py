"""F1b: every result reports which source produced each model step (``model_sources``).

Seam: FastAPI TestClient + real worker/pipeline + FakeModelClient (``service_seam``).
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
from service_seam import routed_fake, service_seam
from video_sum_infra.model_client import ModelRequest

SUMMARY = {
    "title": "标题",
    "overview": "概览",
    "bulletPoints": ["要点"],
    "chapters": [{"title": "开场", "start": 0, "summary": "开场介绍"}],
}
RICH_MINDMAP = {
    "title": "导图",
    "root": "root",
    "nodes": [
        {
            "id": "root",
            "label": "导图",
            "type": "root",
            "children": [
                {"id": f"t{i}", "label": f"主题{i}", "type": "theme", "time_anchor": i}
                for i in range(1, 5)
            ],
        }
    ],
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
REPLIES: dict[str, object] = {
    "llm": {"ok": True},
    "summary_full": SUMMARY,
    "knowledge_note": {"knowledgeNoteMarkdown": "# 笔记\n\n## 开场\n\n内容"},
    "mindmap": RICH_MINDMAP,
    "mindmap_repair": THIN_MINDMAP,
}
NOTHING_ELSE = {
    "keyframe_plan": "none",
    "visual_frame": "none",
    "visual_compose": "none",
    "mindmap": "none",
}


def _replies(**overrides: object):
    table = {**REPLIES, **overrides}

    def reply(request: ModelRequest):
        value = table[request.usage_stage]
        if isinstance(value, BaseException):
            raise value
        return value

    return reply


def _auth_error() -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://llm.invalid/v1/chat/completions")
    return httpx.HTTPStatusError(
        "denied", request=request, response=httpx.Response(401, request=request)
    )


def _wait_for_mindmap(seam, task_id: str, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    result: dict = {}
    while time.monotonic() < deadline:
        result = seam.detail(task_id)["result"] or {}
        if result.get("mindmap_status") in {"ready", "failed"}:
            return result
        time.sleep(0.05)
    raise AssertionError(f"mindmap did not finish: {result.get('mindmap_status')}")


def test_without_builtin_model_the_result_marks_local_fallback_steps(
    tmp_path: Path, monkeypatch
) -> None:
    fake = routed_fake()  # any model call fails the test
    with service_seam(monkeypatch, tmp_path, fake, llm_enabled=False) as seam:
        detail = seam.wait_for_terminal(seam.create_transcript_task())

    assert detail["status"] == "completed"
    assert detail["result"]["model_sources"] == {
        "summary": "local",
        "knowledge_note": "local",
        **NOTHING_ELSE,
    }


def test_builtin_model_steps_are_marked_internal(tmp_path: Path, monkeypatch) -> None:
    fake = routed_fake(default=_replies())
    with service_seam(monkeypatch, tmp_path, fake) as seam:
        detail = seam.wait_for_terminal(seam.create_transcript_task())

    assert detail["status"] == "completed"
    assert detail["result"]["model_sources"] == {
        "summary": "internal",
        "knowledge_note": "internal",
        **NOTHING_ELSE,
    }


def test_summary_that_fell_back_to_rules_is_marked_local(tmp_path: Path, monkeypatch) -> None:
    fake = routed_fake(default=_replies(summary_full=_auth_error()))
    with service_seam(monkeypatch, tmp_path, fake) as seam:
        detail = seam.wait_for_terminal(seam.create_transcript_task())

    assert detail["status"] == "completed"
    assert detail["result"]["model_sources"]["summary"] == "local"
    assert detail["result"]["model_sources"]["knowledge_note"] == "local"


def test_failed_knowledge_note_is_marked_local_while_summary_stays_internal(
    tmp_path: Path, monkeypatch
) -> None:
    fake = routed_fake(default=_replies(knowledge_note={"knowledgeNoteMarkdown": ""}))
    with service_seam(monkeypatch, tmp_path, fake) as seam:
        detail = seam.wait_for_terminal(seam.create_transcript_task())

    assert detail["status"] == "completed"
    assert detail["result"]["model_sources"]["summary"] == "internal"
    assert detail["result"]["model_sources"]["knowledge_note"] == "local"


def test_summary_only_scope_does_not_run_the_knowledge_note_step(
    tmp_path: Path, monkeypatch
) -> None:
    fake = routed_fake(default=_replies())
    with service_seam(monkeypatch, tmp_path, fake) as seam:
        detail = seam.wait_for_terminal(
            seam.create_transcript_task(options={"summary_scope": "summary"})
        )

    assert detail["result"]["model_sources"]["summary"] == "internal"
    assert detail["result"]["model_sources"]["knowledge_note"] == "none"


def test_transcript_only_result_reports_no_model_step(tmp_path: Path, monkeypatch) -> None:
    fake = routed_fake()
    with service_seam(monkeypatch, tmp_path, fake) as seam:
        detail = seam.wait_for_terminal(
            seam.create_transcript_task(options={"summary_scope": "transcript_only"})
        )

    assert detail["status"] == "completed"
    assert set(detail["result"]["model_sources"].values()) == {"none"}


def test_mindmap_generated_by_the_builtin_model_is_marked_internal(
    tmp_path: Path, monkeypatch
) -> None:
    fake = routed_fake(default=_replies())
    with service_seam(
        monkeypatch, tmp_path, fake, worker_kwargs={"auto_generate_mindmap": True}
    ) as seam:
        task_id = seam.create_transcript_task()
        seam.wait_for_terminal(task_id)
        result = _wait_for_mindmap(seam, task_id)

    assert result["mindmap_status"] == "ready"
    assert result["model_sources"]["mindmap"] == "internal"
    assert result["model_sources"]["summary"] == "internal"


def test_mindmap_replaced_by_the_local_fallback_is_marked_local(
    tmp_path: Path, monkeypatch
) -> None:
    fake = routed_fake(default=_replies(mindmap=THIN_MINDMAP))
    with service_seam(
        monkeypatch, tmp_path, fake, worker_kwargs={"auto_generate_mindmap": True}
    ) as seam:
        task_id = seam.create_transcript_task()
        seam.wait_for_terminal(task_id)
        result = _wait_for_mindmap(seam, task_id)

    assert result["mindmap_status"] == "ready"
    assert result["model_sources"]["mindmap"] == "local"


# -- 图文笔记 steps (pipeline seam: real runner + FakeModelClient, frame extraction stubbed) --
def test_visual_note_steps_report_their_sources(tmp_path: Path, monkeypatch) -> None:
    from test_step_router import GOOD_REPLIES, _run_summary, _visual_runner

    fake = routed_fake(default=lambda request: GOOD_REPLIES[request.usage_stage])
    result = _run_summary(_visual_runner(tmp_path, fake, monkeypatch), tmp_path)

    assert result.visual_note_status == "ready"
    assert result.model_sources == {
        "summary": "internal",
        "knowledge_note": "internal",
        "keyframe_plan": "internal",
        "visual_frame": "internal",
        "visual_compose": "internal",
        "mindmap": "none",
    }


def test_visual_note_steps_without_a_vision_model_are_marked_local(
    tmp_path: Path, monkeypatch
) -> None:
    from test_step_router import GOOD_REPLIES, _run_summary, _visual_runner

    fake = routed_fake(default=lambda request: GOOD_REPLIES[request.usage_stage])
    runner = _visual_runner(
        tmp_path, fake, monkeypatch, visual_evidence_use_llm=False, visual_multimodal_enabled=False
    )
    result = _run_summary(runner, tmp_path)

    assert result.model_sources["summary"] == "internal"
    assert result.model_sources["visual_frame"] == "local"
    assert result.model_sources["visual_compose"] == "local"
