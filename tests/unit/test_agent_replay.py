"""F2-2: the ``agent`` execution mode, full-step caching and replay.

Seam: FastAPI TestClient + real worker/pipeline + FakeModelClient (``service_seam``).
Agent tasks are created through ``POST /api/v1/tasks`` (``engine=agent``,
``agent_caller=true``). The Agent is played over the work protocol API (ticket 11);
only content that must fail at replay time is written straight to the cache.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
import video_sum_service.context as service_context
from service_seam import HEADERS, routed_fake, service_seam
from video_sum_infra.config import ServiceSettings
from video_sum_infra.model_client import ModelRequest
from video_sum_service import agent_work
from video_sum_service.worker import TaskWorker

SUMMARY = {
    "title": "标题",
    "overview": "概览",
    "bulletPoints": ["要点"],
    "chapters": [{"title": "开场", "start": 0, "summary": "开场介绍"}],
}
NOTE = {"knowledgeNoteMarkdown": "# 笔记\n\n## 开场\n\n内容"}
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
NO_LLM = {"llm_enabled": False, "llm_api_key": "", "llm_base_url": "", "llm_model": ""}


def _configure_service(monkeypatch, tmp_path: Path, **settings: object) -> None:
    values = {
        "data_dir": tmp_path,
        "cache_dir": tmp_path / "cache",
        "tasks_dir": tmp_path / "tasks",
        **settings,
    }
    monkeypatch.setattr(
        service_context.settings_manager, "_settings", ServiceSettings(_env_file=None, **values)
    )


def _agent_seam(
    monkeypatch,
    tmp_path: Path,
    fake,
    *,
    delegation: str | None = None,
    mindmap: bool = True,
    **overrides,
):
    service_settings = {} if delegation is None else {"agent_delegation": delegation}
    _configure_service(monkeypatch, tmp_path, **service_settings)
    return service_seam(
        monkeypatch, tmp_path, fake, worker_kwargs={"auto_generate_mindmap": mindmap}, **overrides
    )


def _create_agent_task(seam, *, segments=None, options: dict | None = None) -> str:
    body: dict[str, object] = {
        "input_type": "transcript_text",
        "source": json.dumps(
            {
                "title": "测试视频",
                "transcript": "\n".join(str(s["text"]) for s in (segments or []))
                or "第一段内容\n第二段内容",
                "segments": segments
                or [
                    {"start": 0.0, "end": 4.0, "text": "第一段内容"},
                    {"start": 4.0, "end": 9.0, "text": "第二段内容"},
                ],
            },
            ensure_ascii=False,
        ),
        "title": "测试视频",
        "engine": "agent",
        "agent_caller": True,
    }
    if options is not None:
        body["options"] = options
    response = seam.client.post("/api/v1/tasks", json=body, headers=HEADERS)
    assert response.status_code == 201, response.text
    return response.json()["task_id"]


def _wait_status(seam, task_id: str, statuses: set[str], timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    detail: dict = {}
    while time.monotonic() < deadline:
        detail = seam.detail(task_id)
        if detail["status"] in statuses:
            # Let the job thread release its slot before the next round.
            time.sleep(0.05)
            return detail
        time.sleep(0.02)
    raise AssertionError(f"task {task_id} did not reach {statuses}: {detail.get('status')}")


def _task_dir(seam, task_id: str) -> Path:
    return seam.worker._pipeline_runner.task_dir(task_id)  # type: ignore[attr-defined]


def _pending(seam, task_id: str) -> list[dict]:
    response = seam.client.get(f"/api/v1/tasks/{task_id}/agent-work", headers=HEADERS)
    assert response.status_code == 200, response.text
    return response.json()["items"]


def _pending_ids(seam, task_id: str) -> list[str]:
    return [item["item_id"] for item in _pending(seam, task_id)]


def _text(content: object) -> str:
    return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)


def _submit(seam, task_id: str, results: dict[str, object]) -> None:
    """Play the Agent over the work protocol: claim the task, submit every result."""
    claimed = seam.client.post(
        "/api/v1/agent-work/claim",
        json={"scope": "task", "task_id": task_id, "agent": {"name": "Test Agent"}},
        headers=HEADERS,
    )
    assert claimed.status_code == 200 and claimed.json()["task_id"] == task_id, claimed.text
    response = seam.client.put(
        f"/api/v1/tasks/{task_id}/agent-work",
        json={
            "lease_id": claimed.json()["lease_id"],
            "agent": {"name": "Test Agent"},
            "results": [
                {"item_id": item_id, "content": _text(content)}
                for item_id, content in results.items()
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 202, response.text


def _submit_unchecked(seam, task_id: str, results: dict[str, object]) -> None:
    """Content that passes submission but fails at replay (context-dependent problems)."""
    task_dir = _task_dir(seam, task_id)
    for item_id, content in results.items():
        agent_work.save_agent_result(task_dir, item_id, _text(content))
    assert seam.worker.resume_agent_task(task_id) is True


def _waiting(seam, task_id: str) -> list[str]:
    detail = _wait_status(seam, task_id, {"agent_summarizing", "completed", "failed"})
    assert detail["status"] == "agent_summarizing", detail
    return _pending_ids(seam, task_id)


def test_agent_steps_wait_for_the_agent_round_by_round_until_completed(
    tmp_path: Path, monkeypatch
) -> None:
    fake = routed_fake()  # nothing may reach a model: every text step is the Agent's
    with _agent_seam(monkeypatch, tmp_path, fake, **NO_LLM) as seam:
        task_id = _create_agent_task(seam, options={"extra_instructions": "请重点关注第二段"})

        assert _waiting(seam, task_id) == ["summary_full#0"]
        item = _pending(seam, task_id)[0]
        assert (
            item["step"] == "summary"
            and item["kind"] == "text"
            and item["response_format"] == "json"
        )
        prompt = json.dumps(item["messages"], ensure_ascii=False)
        assert "第二段内容" in prompt
        assert "请重点关注第二段" in prompt  # extra instructions reach the Agent's item

        _submit(seam, task_id, {"summary_full#0": SUMMARY})
        assert _waiting(seam, task_id) == ["knowledge_note#0"]
        _submit(seam, task_id, {"knowledge_note#0": NOTE})
        # The mindmap is part of the summary unit: missing -> still waiting, not completed.
        assert _waiting(seam, task_id) == ["mindmap#0"]
        _submit(seam, task_id, {"mindmap#0": RICH_MINDMAP})

        detail = _wait_status(seam, task_id, {"completed", "failed"})
        assert detail["status"] == "completed", detail
        result = detail["result"]
        assert result["mindmap_status"] == "ready"
        assert result["knowledge_note_markdown"].startswith("# 笔记")
        assert {
            k: result["model_sources"][k] for k in ("summary", "knowledge_note", "mindmap")
        } == {
            "summary": "agent",
            "knowledge_note": "agent",
            "mindmap": "agent",
        }
        assert fake.requests == []

        progress = [event["progress"] for event in seam.events(task_id)]
        assert progress == sorted(progress), progress  # replays never move progress backwards
        stages = [event["stage"] for event in seam.events(task_id)]
        assert stages.count("agent_work_required") == 3


def test_agent_summary_forces_full_context_even_when_chunked(tmp_path: Path, monkeypatch) -> None:
    fake = routed_fake()
    segments = [
        {"start": float(i), "end": float(i + 1), "text": f"第{i}段" + "内容" * 40}
        for i in range(12)
    ]
    with _agent_seam(
        monkeypatch,
        tmp_path,
        fake,
        summary_context_mode="chunked",
        summary_chunk_target_chars=200,
        **NO_LLM,
    ) as seam:
        task_id = _create_agent_task(seam, segments=segments)
        assert _waiting(seam, task_id) == ["summary_full#0"]


def test_mixed_routing_calls_each_builtin_step_once_across_rounds(
    tmp_path: Path, monkeypatch
) -> None:
    fake = routed_fake(
        by_stage={"knowledge_note": [NOTE], "llm": [{"ok": True}] * 5}
    )  # one knowledge note call
    with _agent_seam(monkeypatch, tmp_path, fake, delegation="summary,mindmap") as seam:
        task_id = _create_agent_task(seam)
        assert _waiting(seam, task_id) == ["summary_full#0"]
        _submit(seam, task_id, {"summary_full#0": SUMMARY})
        assert _waiting(seam, task_id) == ["mindmap#0"]  # knowledge note ran on the builtin model
        _submit(seam, task_id, {"mindmap#0": RICH_MINDMAP})

        detail = _wait_status(seam, task_id, {"completed", "failed"})
        assert detail["status"] == "completed", detail
        assert fake.stages.count("knowledge_note") == 1
        assert "summary_full" not in fake.stages and "mindmap" not in fake.stages
        sources = detail["result"]["model_sources"]
        assert (sources["summary"], sources["knowledge_note"], sources["mindmap"]) == (
            "agent",
            "internal",
            "agent",
        )


def test_failed_chunk_stays_skipped_in_later_rounds(tmp_path: Path, monkeypatch) -> None:
    calls: dict[str, int] = {}

    def chunk_reply(request: ModelRequest):
        prompt = json.dumps(request.payload["messages"], ensure_ascii=False)
        key = "chunk2" if "分块 2" in prompt else "other"
        calls[key] = calls.get(key, 0) + 1
        if key == "chunk2" and calls[key] == 1:
            raise RuntimeError("chunk 2 failed once")
        return SUMMARY

    segments = [
        {"start": float(i), "end": float(i + 1), "text": f"第{i}段" + "内容" * 40}
        for i in range(12)
    ]
    fake = routed_fake(by_stage={"knowledge_note": [NOTE]}, default=chunk_reply)
    with _agent_seam(
        monkeypatch,
        tmp_path,
        fake,
        delegation="mindmap",
        summary_context_mode="chunked",
        summary_chunk_target_chars=200,
        summary_chunk_retry_count=0,
    ) as seam:
        task_id = _create_agent_task(seam, segments=segments)
        assert _waiting(seam, task_id) == ["mindmap#0"]
        first_round = [stage for stage in fake.stages if stage != "llm"]
        _submit(seam, task_id, {"mindmap#0": RICH_MINDMAP})

        detail = _wait_status(seam, task_id, {"completed", "failed"})
        assert detail["status"] == "completed", detail
        assert [
            stage for stage in fake.stages if stage != "llm"
        ] == first_round  # replay: no model step called
        assert calls["chunk2"] == 1  # the failed chunk was not asked again
        assert "1 个分块摘要失败" in detail["result"]["overview"]


def test_mindmap_and_its_repair_are_cached_under_their_own_keys(
    tmp_path: Path, monkeypatch
) -> None:
    fake = routed_fake()
    with _agent_seam(monkeypatch, tmp_path, fake, **NO_LLM) as seam:
        task_id = _create_agent_task(seam)
        assert _waiting(seam, task_id) == ["summary_full#0"]
        _submit(seam, task_id, {"summary_full#0": SUMMARY})
        assert _waiting(seam, task_id) == ["knowledge_note#0"]
        _submit(seam, task_id, {"knowledge_note#0": NOTE})
        assert _waiting(seam, task_id) == ["mindmap#0"]
        _submit(seam, task_id, {"mindmap#0": THIN_MINDMAP})  # too few branches -> repair
        assert _waiting(seam, task_id) == ["mindmap_repair#0"]
        _submit(seam, task_id, {"mindmap_repair#0": RICH_MINDMAP})

        detail = _wait_status(seam, task_id, {"completed", "failed"})
        assert detail["status"] == "completed", detail
        mindmap = json.loads(
            Path(detail["result"]["mindmap_artifact_path"]).read_text(encoding="utf-8")
        )
        assert (
            len(mindmap["nodes"][0]["children"]) == 4
        )  # the repair reply, not the cached first one


def test_rejected_agent_content_becomes_a_pending_item_again(tmp_path: Path, monkeypatch) -> None:
    fake = routed_fake()
    with _agent_seam(monkeypatch, tmp_path, fake, **NO_LLM) as seam:
        task_id = _create_agent_task(seam)
        assert _waiting(seam, task_id) == ["summary_full#0"]
        _submit_unchecked(seam, task_id, {"summary_full#0": "这不是 JSON"})

        assert _waiting(seam, task_id) == ["summary_full#0"]
        rejected = [
            event for event in seam.events(task_id) if event["stage"] == "agent_work_rejected"
        ]
        assert len(rejected) == 1
        assert rejected[0]["payload"]["item_id"] == "summary_full#0"
        assert rejected[0]["payload"]["reason"]

        _submit(seam, task_id, {"summary_full#0": SUMMARY})
        assert _waiting(seam, task_id) == ["knowledge_note#0"]


def test_other_errors_during_replay_pause_the_task(tmp_path: Path, monkeypatch) -> None:
    fake = routed_fake()
    with _agent_seam(monkeypatch, tmp_path, fake, **NO_LLM) as seam:
        task_id = _create_agent_task(seam)
        assert _waiting(seam, task_id) == ["summary_full#0"]
        (
            _task_dir(seam, task_id) / "transcription.json"
        ).unlink()  # not something the Agent can fix
        _submit(seam, task_id, {"summary_full#0": SUMMARY})

        # Leaves the Agent loop for good. The transcript is saved, so the task
        # pauses for "仅重跑 LLM" like any default-mode LLM failure.
        detail = _wait_status(seam, task_id, {"completed", "failed", "paused"})
        assert detail["status"] == "paused"
        assert "agent_work_rejected" not in [event["stage"] for event in seam.events(task_id)]


def test_restart_leaves_waiting_tasks_alone_and_replays_from_cache(
    tmp_path: Path, monkeypatch
) -> None:
    from video_sum_service.app import recover_incomplete_tasks

    fake = routed_fake(
        by_stage={"summary_full": [SUMMARY], "llm": [{"ok": True}] * 5}
    )  # one summary call, ever
    with _agent_seam(monkeypatch, tmp_path, fake, delegation="knowledge_note") as seam:
        task_id = _create_agent_task(seam)
        assert _waiting(seam, task_id) == ["knowledge_note#0"]

        # Restart while waiting: the task is not resubmitted.
        restarted = TaskWorker(seam.repository, seam.worker._pipeline_runner)  # type: ignore[arg-type]
        try:
            assert recover_incomplete_tasks(seam.repository, restarted) == 0
            assert seam.detail(task_id)["status"] == "agent_summarizing"
        finally:
            restarted.shutdown(wait=True, timeout=5)

        # The Agent submits, then the service dies before the replay runs.
        agent_work.save_agent_result(
            _task_dir(seam, task_id), "knowledge_note#0", json.dumps(NOTE, ensure_ascii=False)
        )
        from video_sum_core.models.tasks import TaskStatus

        assert (
            seam.repository.transition(task_id, TaskStatus.AGENT_SUMMARIZING, TaskStatus.RUNNING)
            is not None
        )

        restarted = TaskWorker(seam.repository, seam.worker._pipeline_runner)  # type: ignore[arg-type]
        try:
            assert recover_incomplete_tasks(seam.repository, restarted) == 1
            detail = _wait_status(seam, task_id, {"completed", "failed"})
        finally:
            restarted.shutdown(wait=True, timeout=5)
        assert detail["status"] == "completed", detail
        assert fake.stages.count("summary_full") == 1
        stages = [event["stage"] for event in seam.events(task_id)]
        assert stages.count("transcription_started") == 1


@pytest.mark.parametrize(
    "status_set", ["not_active", "unfinished", "not_terminal", "not_recoverable"]
)
def test_agent_summarizing_status_sets(status_set: str) -> None:
    from video_sum_core.models.tasks import (
        ACTIVE_STATUSES,
        RECOVERABLE_STATUSES,
        TERMINAL_STATUSES,
        UNFINISHED_STATUSES,
        TaskStatus,
    )

    status = TaskStatus("agent_summarizing")
    if status_set == "not_active":  # spec: does not block idle shutdown
        assert status not in ACTIVE_STATUSES
    elif status_set == "unfinished":  # still joined by reuse, still counted for the Agent
        assert status in UNFINISHED_STATUSES
    elif status_set == "not_terminal":
        assert status not in TERMINAL_STATUSES
    else:
        assert status not in RECOVERABLE_STATUSES
