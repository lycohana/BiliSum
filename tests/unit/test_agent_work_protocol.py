"""F2-3: the Agent work protocol API (claim, lease, submit, heartbeat).

Seam: FastAPI TestClient + real worker/pipeline + FakeModelClient (``service_seam``).
The Agent is played purely over HTTP. Lease expiry is driven by the service's
clock (``agent_work.utcnow``), the only time seam.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from service_seam import HEADERS, routed_fake
from test_agent_replay import (
    NO_LLM,
    NOTE,
    RICH_MINDMAP,
    SUMMARY,
    _agent_seam,
    _create_agent_task,
    _wait_status,
)
from video_sum_service import agent_work
from video_sum_service.worker import TaskWorker

CLAUDE = {"name": "Claude Code", "model": "claude-opus"}
CODEX = {"name": "Codex"}


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock(monkeypatch) -> Clock:
    fake_clock = Clock()
    monkeypatch.setattr(agent_work, "utcnow", fake_clock)
    return fake_clock


def _claim(
    seam,
    *,
    scope: str = "batch",
    batch_id: str | None = None,
    task_id: str | None = None,
    agent=CLAUDE,
):
    body: dict[str, object] = {"scope": scope, "agent": agent}
    if batch_id is not None:
        body["batch_id"] = batch_id
    if task_id is not None:
        body["task_id"] = task_id
    return seam.client.post("/api/v1/agent-work/claim", json=body, headers=HEADERS)


def _put(seam, task_id: str, lease_id: str, results: list[dict], agent=CLAUDE):
    body = {"lease_id": lease_id, "agent": agent, "results": results}
    return seam.client.put(f"/api/v1/tasks/{task_id}/agent-work", json=body, headers=HEADERS)


def _content(item_id: str, value: object) -> dict:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return {"item_id": item_id, "content": text}


def _batch_id(seam, task_id: str) -> str:
    return seam.detail(task_id)["batch_id"]


def _wait_waiting(seam, task_id: str) -> dict:
    detail = _wait_status(seam, task_id, {"agent_summarizing", "completed", "failed", "paused"})
    assert detail["status"] == "agent_summarizing", detail
    return detail


def _claim_and_submit(seam, task_id: str, results: dict[str, object], agent=CLAUDE) -> None:
    _wait_waiting(seam, task_id)
    claimed = _claim(seam, scope="task", task_id=task_id, agent=agent)
    assert claimed.status_code == 200, claimed.text
    data = claimed.json()
    assert [item["item_id"] for item in data["items"]] == list(results)
    response = _put(
        seam, task_id, data["lease_id"], [_content(k, v) for k, v in results.items()], agent=agent
    )
    assert response.status_code == 202, response.text


def test_agent_completes_a_task_over_http_and_each_step_records_its_agent(
    tmp_path: Path, monkeypatch
) -> None:
    fake = routed_fake()
    with _agent_seam(monkeypatch, tmp_path, fake, **NO_LLM) as seam:
        task_id = _create_agent_task(seam)
        _wait_waiting(seam, task_id)

        claimed = _claim(seam, batch_id=_batch_id(seam, task_id))
        assert claimed.status_code == 200, claimed.text
        data = claimed.json()
        assert data["task_id"] == task_id
        assert data["batch_id"] == _batch_id(seam, task_id)
        assert data["round"] == 1
        assert data["lease_id"]
        assert datetime.fromisoformat(data["lease_expires_at"]) > datetime.now(UTC)
        item = data["items"][0]
        assert (item["item_id"], item["step"], item["kind"], item["response_format"]) == (
            "summary_full#0",
            "summary",
            "text",
            "json",
        )
        assert item["messages"]

        response = _put(seam, task_id, data["lease_id"], [_content("summary_full#0", SUMMARY)])
        assert response.status_code == 202, response.text
        assert response.json()["status"] == "running"

        # The Agent changes mid-task: each item keeps the name of who answered it.
        _claim_and_submit(seam, task_id, {"knowledge_note#0": NOTE}, agent=CODEX)
        _claim_and_submit(seam, task_id, {"mindmap#0": RICH_MINDMAP}, agent=CODEX)

        detail = _wait_status(seam, task_id, {"completed", "failed", "paused"})
        assert detail["status"] == "completed", detail
        identities = detail["result"]["agent_identities"]
        assert identities["summary_full#0"] == {
            "name": "Claude Code",
            "model": "claude-opus",
            "self_reported": True,
        }
        assert identities["knowledge_note#0"] == {
            "name": "Codex",
            "model": None,
            "self_reported": True,
        }
        assert identities["mindmap#0"]["name"] == "Codex"
        assert detail["result"]["model_sources"]["summary"] == "agent"
        assert fake.requests == []


def test_nothing_to_claim_reports_tasks_still_in_progress(tmp_path: Path, monkeypatch) -> None:
    with _agent_seam(monkeypatch, tmp_path, routed_fake(), **NO_LLM) as seam:
        task_id = _create_agent_task(seam)
        _wait_waiting(seam, task_id)
        batch_id = _batch_id(seam, task_id)

        assert _claim(seam, batch_id=batch_id).status_code == 200
        empty = _claim(seam, batch_id=batch_id, agent=CODEX)
        assert empty.status_code == 200
        assert empty.json() == {"task_id": None, "pending_tasks": 1}

        other = _claim(seam, batch_id="batch-unknown")
        assert other.json() == {"task_id": None, "pending_tasks": 0}


def test_claim_scopes_batch_all_and_task(tmp_path: Path, monkeypatch) -> None:
    with _agent_seam(monkeypatch, tmp_path, routed_fake(), **NO_LLM) as seam:
        first = _create_agent_task(seam)
        _wait_waiting(seam, first)
        second = _create_agent_task(seam)
        _wait_waiting(seam, second)
        assert _batch_id(seam, first) != _batch_id(seam, second)

        # batch: only that batch's task
        by_batch = _claim(seam, batch_id=_batch_id(seam, second)).json()
        assert by_batch["task_id"] == second
        # all: the earliest waiting task not leased by someone else
        by_all = _claim(seam, scope="all", agent=CODEX).json()
        assert by_all["task_id"] == first
        assert _claim(seam, scope="all").json() == {"task_id": None, "pending_tasks": 2}

        # task: leased by another Agent -> 409; unknown -> 404
        leased = _claim(seam, scope="task", task_id=first)
        assert (leased.status_code, leased.json()["detail"]) == (409, "已被其他 Agent 处理")
        assert _claim(seam, scope="task", task_id="missing").status_code == 404
        # scope parameters are required
        assert _claim(seam, scope="batch").status_code == 422
        assert _claim(seam, scope="task").status_code == 422


def test_scope_task_on_a_finished_task_is_a_conflict(tmp_path: Path, monkeypatch) -> None:
    with _agent_seam(monkeypatch, tmp_path, routed_fake(), **NO_LLM) as seam:
        task_id = _create_agent_task(seam)
        _wait_waiting(seam, task_id)
        assert (
            seam.client.post(f"/api/v1/tasks/{task_id}/cancel", headers=HEADERS).status_code == 200
        )
        assert _claim(seam, scope="task", task_id=task_id).status_code == 409


def test_scope_task_on_a_waiting_task_without_items_says_so(tmp_path: Path, monkeypatch) -> None:
    with _agent_seam(monkeypatch, tmp_path, routed_fake(), **NO_LLM) as seam:
        task_id = _create_agent_task(seam)
        _wait_waiting(seam, task_id)
        agent_work.clear_agent_state(seam.worker.agent_task_dir(task_id))

        response = _claim(seam, scope="task", task_id=task_id)

        assert (response.status_code, response.json()["detail"]) == (409, "任务当前没有待办项。")


def test_lease_expires_heartbeat_renews_and_wrong_lease_is_409(
    tmp_path: Path, monkeypatch, clock: Clock
) -> None:
    from test_agent_replay import _configure_service

    with _agent_seam(monkeypatch, tmp_path, routed_fake(), **NO_LLM) as seam:
        _configure_service(monkeypatch, tmp_path, agent_lease_ttl=60)
        task_id = _create_agent_task(seam)
        _wait_waiting(seam, task_id)
        lease_id = _claim(seam, scope="task", task_id=task_id).json()["lease_id"]

        clock.advance(50)
        beat = seam.client.post(
            f"/api/v1/tasks/{task_id}/agent-heartbeat", json={"lease_id": lease_id}, headers=HEADERS
        )
        assert beat.status_code == 200, beat.text
        clock.advance(50)  # 100 s after the claim, 50 s after the heartbeat: still held
        assert _claim(seam, scope="all", agent=CODEX).json()["task_id"] is None
        bad = seam.client.post(
            f"/api/v1/tasks/{task_id}/agent-heartbeat", json={"lease_id": "nope"}, headers=HEADERS
        )
        assert bad.status_code == 409
        assert _put(seam, task_id, "nope", [_content("summary_full#0", SUMMARY)]).status_code == 409

        clock.advance(61)  # expired: anyone may claim it, the old lease is dead
        view = seam.client.get(f"/api/v1/tasks/{task_id}/agent-work", headers=HEADERS).json()
        assert view["lease"] is None
        assert seam.detail(task_id)["status"] == "agent_summarizing"  # no automatic fallback
        expired = seam.client.post(
            f"/api/v1/tasks/{task_id}/agent-heartbeat", json={"lease_id": lease_id}, headers=HEADERS
        )
        assert expired.status_code == 409
        assert (
            _put(seam, task_id, lease_id, [_content("summary_full#0", SUMMARY)]).status_code == 409
        )
        reclaimed = _claim(seam, scope="all", agent=CODEX).json()
        assert reclaimed["task_id"] == task_id
        assert reclaimed["lease_id"] != lease_id
        assert (
            _put(
                seam,
                task_id,
                reclaimed["lease_id"],
                [_content("summary_full#0", SUMMARY)],
                agent=CODEX,
            ).status_code
            == 202
        )


def test_read_only_view_shows_items_and_holder_without_claiming(
    tmp_path: Path, monkeypatch
) -> None:
    with _agent_seam(monkeypatch, tmp_path, routed_fake(), **NO_LLM) as seam:
        task_id = _create_agent_task(seam)
        _wait_waiting(seam, task_id)

        view = seam.client.get(f"/api/v1/tasks/{task_id}/agent-work", headers=HEADERS)
        assert view.status_code == 200, view.text
        data = view.json()
        assert data["status"] == "agent_summarizing"
        assert [item["item_id"] for item in data["items"]] == ["summary_full#0"]
        assert data["round"] == 1 and data["waiting_since"]
        assert data["lease"] is None
        assert "lease_id" not in json.dumps(data)

        _claim(seam, scope="task", task_id=task_id)
        data = seam.client.get(f"/api/v1/tasks/{task_id}/agent-work", headers=HEADERS).json()
        assert data["lease"]["agent"] == CLAUDE | {"self_reported": True}
        assert data["lease"]["expires_at"]
        assert "lease_id" not in json.dumps(data)
        assert (
            seam.client.get("/api/v1/tasks/missing/agent-work", headers=HEADERS).status_code == 404
        )


def test_invalid_submissions_are_422_and_leave_the_task_waiting(
    tmp_path: Path, monkeypatch
) -> None:
    with _agent_seam(monkeypatch, tmp_path, routed_fake(), **NO_LLM) as seam:
        task_id = _create_agent_task(seam)
        _wait_waiting(seam, task_id)
        lease_id = _claim(seam, scope="task", task_id=task_id).json()["lease_id"]

        for results in (
            [],  # item missing
            [_content("summary_full#0", "不是 JSON")],  # stage parser fails
            [
                _content("summary_full#0", SUMMARY),
                _content("knowledge_note#0", NOTE),
            ],  # not in this round
            [{"item_id": "summary_full#0"}],  # neither content nor declined
            [_content("summary_full#0", "x" * (agent_work.MAX_CONTENT_CHARS + 1))],
        ):
            response = _put(seam, task_id, lease_id, results)
            assert response.status_code == 422, (results, response.text)
            assert seam.detail(task_id)["status"] == "agent_summarizing"

        # The lease survives a rejected submission.
        assert (
            _put(seam, task_id, lease_id, [_content("summary_full#0", SUMMARY)]).status_code == 202
        )


def test_duplicate_submission_is_409_and_runs_one_replay(tmp_path: Path, monkeypatch) -> None:
    with _agent_seam(monkeypatch, tmp_path, routed_fake(), **NO_LLM) as seam:
        task_id = _create_agent_task(seam)
        _wait_waiting(seam, task_id)
        lease_id = _claim(seam, scope="task", task_id=task_id).json()["lease_id"]
        results = [_content("summary_full#0", SUMMARY)]

        codes: list[int] = []
        threads = [
            threading.Thread(
                target=lambda: codes.append(_put(seam, task_id, lease_id, results).status_code)
            )
            for _ in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(codes) == [202, 409, 409, 409]

        _wait_waiting(seam, task_id)
        stages = [event["stage"] for event in seam.events(task_id)]
        assert stages.count("agent_work_required") == 2


def test_submit_to_a_deleted_task_is_404(tmp_path: Path, monkeypatch) -> None:
    with _agent_seam(monkeypatch, tmp_path, routed_fake(), **NO_LLM) as seam:
        task_id = _create_agent_task(seam)
        _wait_waiting(seam, task_id)
        lease_id = _claim(seam, scope="task", task_id=task_id).json()["lease_id"]
        assert seam.client.delete(f"/api/v1/tasks/{task_id}", headers=HEADERS).status_code == 200

        assert (
            _put(seam, task_id, lease_id, [_content("summary_full#0", SUMMARY)]).status_code == 404
        )
        assert seam.client.post(
            f"/api/v1/tasks/{task_id}/agent-heartbeat", json={"lease_id": lease_id}, headers=HEADERS
        ).status_code == 404


def test_unclaimed_lease_survives_a_restart(tmp_path: Path, monkeypatch) -> None:
    from video_sum_service.app import app, recover_incomplete_tasks

    with _agent_seam(monkeypatch, tmp_path, routed_fake(), **NO_LLM) as seam:
        task_id = _create_agent_task(seam)
        _wait_waiting(seam, task_id)
        lease_id = _claim(seam, scope="task", task_id=task_id).json()["lease_id"]

        seam.worker.shutdown(wait=True, timeout=5)
        restarted = TaskWorker(seam.repository, seam.worker._pipeline_runner)  # type: ignore[arg-type]
        app.state.task_worker = restarted
        seam.worker = restarted
        assert recover_incomplete_tasks(seam.repository, restarted) == 0

        assert _claim(seam, scope="all", agent=CODEX).json()["task_id"] is None  # still held
        assert (
            _put(seam, task_id, lease_id, [_content("summary_full#0", SUMMARY)]).status_code == 202
        )
        _wait_waiting(seam, task_id)


def test_declined_items_run_on_the_default_chain(tmp_path: Path, monkeypatch) -> None:
    fake = routed_fake(by_stage={"knowledge_note": [NOTE], "llm": [{"ok": True}] * 5})
    with _agent_seam(
        monkeypatch, tmp_path, fake, delegation="summary,knowledge_note", mindmap=False
    ) as seam:
        task_id = _create_agent_task(seam)
        _claim_and_submit(seam, task_id, {"summary_full#0": SUMMARY})
        _wait_waiting(seam, task_id)
        lease_id = _claim(seam, scope="task", task_id=task_id).json()["lease_id"]
        response = _put(
            seam,
            task_id,
            lease_id,
            [{"item_id": "knowledge_note#0", "declined": True, "reason": "做不了"}],
        )
        assert response.status_code == 202, response.text

        detail = _wait_status(seam, task_id, {"completed", "failed", "paused"})
        assert detail["status"] == "completed", detail
        assert fake.stages.count("knowledge_note") == 1
        assert detail["result"]["model_sources"]["knowledge_note"] == "internal"
        assert "knowledge_note#0" not in detail["result"]["agent_identities"]


def test_use_builtin_declines_every_remaining_agent_step(tmp_path: Path, monkeypatch) -> None:
    fake = routed_fake(
        by_stage={"knowledge_note": [NOTE], "mindmap": [RICH_MINDMAP], "llm": [{"ok": True}] * 5}
    )
    with _agent_seam(monkeypatch, tmp_path, fake) as seam:
        task_id = _create_agent_task(seam)
        _claim_and_submit(seam, task_id, {"summary_full#0": SUMMARY})
        _wait_waiting(seam, task_id)
        lease_id = _claim(seam, scope="task", task_id=task_id).json()[
            "lease_id"
        ]  # an Agent holds it

        response = seam.client.post(
            f"/api/v1/tasks/{task_id}/agent-work/use-builtin", headers=HEADERS
        )
        assert response.status_code == 202, response.text
        detail = _wait_status(seam, task_id, {"completed", "failed", "paused", "agent_summarizing"})
        assert detail["status"] == "completed", detail
        sources = detail["result"]["model_sources"]
        assert (sources["summary"], sources["knowledge_note"], sources["mindmap"]) == (
            "agent",
            "internal",
            "internal",
        )
        assert "summary_full" not in fake.stages
        # The Agent that held the lease can no longer submit.
        assert (
            _put(seam, task_id, lease_id, [_content("knowledge_note#0", NOTE)]).status_code == 409
        )
        assert (
            seam.client.post(
                f"/api/v1/tasks/{task_id}/agent-work/use-builtin", headers=HEADERS
            ).status_code
            == 409
        )


def test_use_builtin_without_internal_model_falls_back_locally(tmp_path: Path, monkeypatch) -> None:
    with _agent_seam(monkeypatch, tmp_path, routed_fake(), **NO_LLM) as seam:
        task_id = _create_agent_task(seam)
        _wait_waiting(seam, task_id)
        assert (
            seam.client.post(
                f"/api/v1/tasks/{task_id}/agent-work/use-builtin", headers=HEADERS
            ).status_code
            == 202
        )
        detail = _wait_status(seam, task_id, {"completed", "failed", "paused", "agent_summarizing"})
        assert detail["status"] == "completed", detail
        assert detail["result"]["model_sources"]["summary"] == "local"


def test_cancel_leaves_the_waiting_state_and_keeps_the_event_stream_open_until_then(
    tmp_path: Path, monkeypatch
) -> None:
    with _agent_seam(monkeypatch, tmp_path, routed_fake(), **NO_LLM) as seam:
        task_id = _create_agent_task(seam)
        _wait_waiting(seam, task_id)
        lease_id = _claim(seam, scope="task", task_id=task_id).json()["lease_id"]

        def cancel_later() -> None:
            time.sleep(1.5)
            seam.client.post(f"/api/v1/tasks/{task_id}/cancel", headers=HEADERS)

        canceller = threading.Thread(target=cancel_later)
        canceller.start()
        statuses: list[str] = []
        with seam.client.stream(
            "GET", f"/api/v1/tasks/{task_id}/events/stream", headers=HEADERS
        ) as stream:
            for line in stream.iter_lines():
                if line.startswith("data:") and '"status"' in line:
                    statuses.append(json.loads(line[5:])["status"])
        canceller.join()
        assert statuses[-1] == "cancelled"  # the stream stayed open while waiting
        assert seam.detail(task_id)["status"] == "cancelled"

        assert (
            _put(seam, task_id, lease_id, [_content("summary_full#0", SUMMARY)]).status_code == 409
        )
        assert (
            seam.client.post(f"/api/v1/tasks/{task_id}/cancel", headers=HEADERS).status_code == 409
        )


def test_manual_regeneration_is_refused_while_waiting_and_ignores_the_agent_cache(
    tmp_path: Path, monkeypatch
) -> None:
    fake = routed_fake(by_stage={"mindmap": [RICH_MINDMAP], "llm": [{"ok": True}] * 5})
    with _agent_seam(monkeypatch, tmp_path, fake) as seam:
        task_id = _create_agent_task(seam)
        _wait_waiting(seam, task_id)
        assert (
            seam.client.post(f"/api/v1/tasks/{task_id}/mindmap", headers=HEADERS).status_code == 400
        )
        assert (
            seam.client.post(
                f"/api/v1/tasks/{task_id}/visual-evidence", headers=HEADERS
            ).status_code
            == 400
        )

        _claim_and_submit(seam, task_id, {"summary_full#0": SUMMARY})
        _claim_and_submit(seam, task_id, {"knowledge_note#0": NOTE})
        _claim_and_submit(seam, task_id, {"mindmap#0": RICH_MINDMAP})
        detail = _wait_status(seam, task_id, {"completed", "failed", "paused"})
        assert detail["status"] == "completed", detail
        assert "mindmap" not in fake.stages

        response = seam.client.post(f"/api/v1/tasks/{task_id}/mindmap?force=true", headers=HEADERS)
        assert response.status_code == 200, response.text
        deadline = time.monotonic() + 10
        while "mindmap" not in fake.stages and time.monotonic() < deadline:
            time.sleep(0.05)
        assert fake.stages.count("mindmap") == 1  # the builtin model, not the cached Agent reply
