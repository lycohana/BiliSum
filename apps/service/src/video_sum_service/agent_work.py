"""Agent work storage in the task directory (F2).

* ``agent_results/`` -- the step-result cache of an Agent task, one JSON file per
  key ``usage_stage#index`` (entry shapes: ``model_steps.AgentResultStore``).
  Agent, internal and local outcomes all live here, failures included.
* ``agent_work.json`` -- this round's pending items: ``{"round", "items",
  "updated_at", "rejected"?, "lease"?, "use_builtin"?}``. ``updated_at`` is when
  the task entered ``agent_summarizing`` for this round; ``lease`` is the current
  exclusive claim (``{"lease_id", "agent", "claimed_at", "expires_at", "round"}``),
  checked for expiry on the spot (no scanner) and kept across restarts.
  ``use_builtin`` = the user chose "改用内置模型": every Agent step left runs on
  the default chain.

The protocol functions (claim / heartbeat / submit / use_builtin / view) raise
:class:`AgentWorkError` carrying the HTTP status for the router.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from video_sum_core.models.tasks import ACTIVE_STATUSES, UNFINISHED_STATUSES, TaskStatus
from video_sum_core.pipeline.model_steps import STAGES, PendingItem, agent_response

if TYPE_CHECKING:
    from video_sum_service.repository import SqliteTaskRepository
    from video_sum_service.schemas import TaskRecord
    from video_sum_service.worker import TaskWorker

logger = logging.getLogger("video_sum_service.agent_work")

AGENT_RESULTS_DIRNAME = "agent_results"
AGENT_WORK_FILENAME = "agent_work.json"


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        logger.exception("unreadable agent work file path=%s", path)
        return None


def _key_filename(key: str) -> str:
    stage, _, index = key.partition("#")
    return f"{stage}__{index or '0'}.json"


class AgentResultFiles:
    """``AgentResultStore`` backed by ``task_dir/agent_results/``."""

    def __init__(self, task_dir: Path) -> None:
        self.directory = Path(task_dir) / AGENT_RESULTS_DIRNAME

    def path_for(self, key: str) -> Path:
        return self.directory / _key_filename(key)

    def load(self, key: str) -> dict[str, object] | None:
        data = _read_json(self.path_for(key))
        return data if isinstance(data, dict) else None

    def save(self, key: str, entry: dict[str, object]) -> None:
        _write_json(self.path_for(key), {**entry, "key": key})

    def discard(self, key: str) -> None:
        self.path_for(key).unlink(missing_ok=True)


def save_agent_result(
    task_dir: Path,
    item_id: str,
    content: str,
    *,
    request_hash: str | None = None,
    agent: dict[str, object] | None = None,
) -> None:
    """Store the Agent's raw output for one pending item (replayed through the stage parser).

    ``agent`` is the submitter's self-reported identity (``agent_identities``)."""
    entry: dict[str, object] = {"source": "agent", "content": content}
    if request_hash:
        entry["request_hash"] = request_hash
    if agent:
        entry["agent"] = agent
        entry["submitted_at"] = utcnow().isoformat()
    AgentResultFiles(task_dir).save(item_id, entry)


def save_agent_declined(task_dir: Path, item_id: str, reason: str = "") -> None:
    """The Agent declined ``item_id``: the next replay runs it on the default chain."""
    AgentResultFiles(task_dir).save(
        item_id, {"source": "agent", "declined": True, "reason": reason}
    )


def load_agent_work(task_dir: Path) -> dict[str, object]:
    """This round's pending items (``{"round": 0, "items": []}`` when none were saved)."""
    data = _read_json(Path(task_dir) / AGENT_WORK_FILENAME)
    if not isinstance(data, dict):
        return {"round": 0, "items": []}
    data.setdefault("round", 0)
    data.setdefault("items", [])
    return data


def visual_media_url(task_id: str, path: str | Path) -> str | None:
    """Media endpoint address of a frame file under ``<task>/visual_evidence/frames``."""
    file = Path(path)
    if file.parent.name != "frames" or file.parent.parent.name != "visual_evidence":
        return None
    return f"/api/v1/tasks/{task_id}/visual-evidence/media/{file.name}"


def _item_document(task_dir: Path, item: PendingItem) -> dict[str, object]:
    """``PendingItem.to_dict()`` plus the media endpoint ``url`` of every image
    (task-relative path; the endpoint needs no token)."""
    document = item.to_dict()
    task_id = Path(task_dir).name
    document["images"] = [
        {"path": path, "url": visual_media_url(task_id, path)} for path in item.images
    ]
    return document


def save_pending_items(
    task_dir: Path, items: Sequence[PendingItem], *, rejected: dict[str, str] | None = None
) -> dict[str, object]:
    """Start a new round with ``items``; returns the saved document."""
    previous = load_agent_work(task_dir)
    document: dict[str, object] = {
        **previous,
        "round": int(previous.get("round") or 0) + 1,
        "items": [_item_document(task_dir, item) for item in items],
        "updated_at": datetime.now(UTC).isoformat(),
    }
    document.pop("rejected", None)
    document.pop("lease", None)  # a lease covers one round only
    if rejected:
        document["rejected"] = rejected
    _write_json(Path(task_dir) / AGENT_WORK_FILENAME, document)
    return document


def clear_agent_state(task_dir: Path) -> None:
    """Forget cached results and pending items (the task restarts from transcription)."""
    task_dir = Path(task_dir)
    (task_dir / AGENT_WORK_FILENAME).unlink(missing_ok=True)
    shutil.rmtree(task_dir / AGENT_RESULTS_DIRNAME, ignore_errors=True)


# -- protocol (ticket 11) ------------------------------------------------------
MAX_CONTENT_CHARS = 1_000_000
"""Upper bound of one submitted ``content`` (characters)."""

USE_BUILTIN_REASON = "用户改用内置模型"
LEASE_LOST = "已被其他 Agent 处理"
NO_PENDING_ITEMS = "任务当前没有待办项。"

_LOCK = threading.RLock()
"""Serializes claim / heartbeat / submit / use_builtin / cancel (one service process)."""


def utcnow() -> datetime:
    """The protocol clock (tests replace it)."""
    return datetime.now(UTC)


class AgentWorkError(Exception):
    def __init__(self, status_code: int, detail: object) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


def _parse_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def identity(agent: dict[str, object]) -> dict[str, object]:
    """``{name, model, self_reported}`` from an Agent's self-report."""
    model = agent.get("model")
    return {
        "name": str(agent.get("name") or ""),
        "model": str(model) if model else None,
        "self_reported": True,
    }


def _save_document(task_dir: Path, document: dict[str, object]) -> None:
    _write_json(Path(task_dir) / AGENT_WORK_FILENAME, document)


def live_lease(
    document: dict[str, object], now: datetime | None = None
) -> dict[str, object] | None:
    """The document's lease while it has not expired (judged now, no scanning)."""
    lease = document.get("lease")
    if not isinstance(lease, dict):
        return None
    expires = _parse_time(lease.get("expires_at"))
    if expires is None or expires <= (now or utcnow()):
        return None
    return lease


def release_lease(task_dir: Path) -> None:
    with _LOCK:
        document = load_agent_work(task_dir)
        if document.pop("lease", None) is not None:
            _save_document(task_dir, document)


def builtin_override(task_dir: Path) -> bool:
    """The user chose "改用内置模型": no step goes to the Agent any more."""
    return bool(load_agent_work(task_dir).get("use_builtin"))


def agent_identities(task_dir: Path) -> dict[str, dict[str, object]]:
    """``item key (usage_stage#index) -> identity`` of every Agent answer still in the cache.

    Recorded per item, so each frame / chunk keeps the Agent that answered it even
    when another Agent took over a later round. Declined items are absent.
    """
    directory = Path(task_dir) / AGENT_RESULTS_DIRNAME
    found: dict[str, dict[str, object]] = {}
    paths = sorted(directory.glob("*.json")) if directory.is_dir() else []
    for path in paths:
        entry = _read_json(path)
        if not isinstance(entry, dict) or entry.get("source") != "agent" or entry.get("declined"):
            continue
        agent = entry.get("agent")
        key = str(entry.get("key") or "")
        if not isinstance(agent, dict) or not key:
            continue
        found[key] = identity(agent)
    return found


def _task_dir(worker: TaskWorker, task_id: str) -> Path:
    task_dir = worker.agent_task_dir(task_id)
    if task_dir is None:
        raise AgentWorkError(409, "任务目录不可用，无法处理 Agent 工作。")
    return task_dir


def _require_task(repository: SqliteTaskRepository, task_id: str) -> TaskRecord:
    record = repository.get_task(task_id)
    if record is None:
        raise AgentWorkError(404, "Task not found.")
    return record


def _waiting_since(document: dict[str, object], record: TaskRecord) -> datetime:
    return _parse_time(document.get("updated_at")) or record.updated_at


def _latest_progress(repository: SqliteTaskRepository, task_id: str) -> int:
    try:
        return max((int(event.progress) for event in repository.list_events(task_id)), default=0)
    except Exception:  # pragma: no cover - progress is cosmetic here
        return 0


def claim(
    repository: SqliteTaskRepository,
    worker: TaskWorker,
    *,
    scope: Literal["batch", "all", "task"],
    agent: dict[str, object],
    ttl_seconds: int,
    batch_id: str | None = None,
    task_id: str | None = None,
) -> dict[str, object]:
    """Lease the earliest waiting, unleased task in scope and return its pending items."""
    if scope == "batch" and not batch_id:
        raise AgentWorkError(422, "scope=batch 需要 batch_id。")
    if scope == "task" and not task_id:
        raise AgentWorkError(422, "scope=task 需要 task_id。")
    with _LOCK:
        now = utcnow()
        if scope == "task":
            record = _require_task(repository, str(task_id))
            if record.status != TaskStatus.AGENT_SUMMARIZING:
                if record.status in ACTIVE_STATUSES and record.task_input.options.routing.is_agent:
                    return {"task_id": None, "pending_tasks": 1}  # still transcribing / replaying
                raise AgentWorkError(409, "任务不在等待 Agent 的状态。")
            records = [record]
        else:
            records = [
                record
                for record in repository.list_tasks()
                if record.task_input.options.routing.is_agent
                and (scope == "all" or record.task_input.options.batch_id == batch_id)
            ]
        candidates: list[tuple[datetime, datetime, TaskRecord, Path, dict[str, object]]] = []
        leased_elsewhere = False
        for record in records:
            if record.status != TaskStatus.AGENT_SUMMARIZING:
                continue
            task_dir = worker.agent_task_dir(record.task_id)
            if task_dir is None:
                continue
            document = load_agent_work(task_dir)
            if not document.get("items"):
                continue
            if live_lease(document, now) is not None:
                leased_elsewhere = True
                continue
            candidates.append(
                (_waiting_since(document, record), record.created_at, record, task_dir, document)
            )
        if not candidates:
            if scope == "task":
                raise AgentWorkError(409, LEASE_LOST if leased_elsewhere else NO_PENDING_ITEMS)
            pending = sum(1 for record in records if record.status in UNFINISHED_STATUSES)
            return {"task_id": None, "pending_tasks": pending}
        _, _, record, task_dir, document = min(candidates, key=lambda c: (c[0], c[1]))
        expires = now + timedelta(seconds=ttl_seconds)
        lease = {
            "lease_id": uuid.uuid4().hex,
            "agent": identity(agent),
            "claimed_at": now.isoformat(),
            "expires_at": expires.isoformat(),
            "round": document.get("round"),
        }
        document["lease"] = lease
        _save_document(task_dir, document)
    return {
        "task_id": record.task_id,
        "batch_id": record.task_input.options.batch_id,
        "round": document.get("round"),
        "lease_id": lease["lease_id"],
        "lease_expires_at": _iso(expires),
        "items": document.get("items") or [],
    }


def _require_lease(
    repository: SqliteTaskRepository, worker: TaskWorker, task_id: str, lease_id: str
) -> tuple[TaskRecord, Path, dict[str, object], dict[str, object]]:
    record = _require_task(repository, task_id)
    if record.status != TaskStatus.AGENT_SUMMARIZING:
        raise AgentWorkError(409, LEASE_LOST)
    task_dir = _task_dir(worker, task_id)
    document = load_agent_work(task_dir)
    lease = live_lease(document)
    if lease is None or lease.get("lease_id") != lease_id:
        raise AgentWorkError(409, LEASE_LOST)
    return record, task_dir, document, lease


def heartbeat(
    repository: SqliteTaskRepository,
    worker: TaskWorker,
    task_id: str,
    lease_id: str,
    *,
    ttl_seconds: int,
) -> dict[str, object]:
    """Renew a live lease; task status is untouched."""
    with _LOCK:
        _, task_dir, document, lease = _require_lease(repository, worker, task_id, lease_id)
        expires = utcnow() + timedelta(seconds=ttl_seconds)
        lease["expires_at"] = expires.isoformat()
        document["lease"] = lease
        _save_document(task_dir, document)
    return {"task_id": task_id, "lease_id": lease_id, "lease_expires_at": _iso(expires)}


def _validate_results(
    items: list[dict[str, Any]], results: Sequence[dict[str, Any]]
) -> list[dict[str, str]]:
    by_id = {str(item.get("item_id")): item for item in items}
    errors: list[dict[str, str]] = []
    seen: set[str] = set()
    for result in results:
        item_id = str(result.get("item_id") or "")
        if item_id not in by_id:
            errors.append({"item_id": item_id, "error": "不是本轮的待办项"})
            continue
        if item_id in seen:
            errors.append({"item_id": item_id, "error": "重复提交"})
            continue
        seen.add(item_id)
        if result.get("declined"):
            continue
        content = result.get("content")
        if not isinstance(content, str) or not content.strip():
            errors.append({"item_id": item_id, "error": "缺少 content，或应标记 declined"})
            continue
        if len(content) > MAX_CONTENT_CHARS:
            errors.append(
                {"item_id": item_id, "error": f"content 超过 {MAX_CONTENT_CHARS} 字符上限"}
            )
            continue
        spec = STAGES.get(str(by_id[item_id].get("usage_stage") or item_id.partition("#")[0]))
        if spec is None:
            continue
        try:
            spec.parse(agent_response(content))
        except Exception as exc:
            errors.append(
                {"item_id": item_id, "error": f"内容无法解析：{str(exc) or type(exc).__name__}"}
            )
    for item_id in by_id:
        if item_id not in seen:
            errors.append({"item_id": item_id, "error": "缺少结果（提交 content 或 declined）"})
    return errors


def _resume(repository: SqliteTaskRepository, worker: TaskWorker, task_id: str) -> None:
    if not worker.resume_agent_task(task_id):
        if repository.get_task(task_id) is None:
            raise AgentWorkError(404, "Task not found.")
        raise AgentWorkError(409, LEASE_LOST)


def submit(
    repository: SqliteTaskRepository,
    worker: TaskWorker,
    task_id: str,
    *,
    lease_id: str,
    agent: dict[str, object],
    results: Sequence[dict[str, Any]],
) -> dict[str, object]:
    """Validate and store this round's results, release the lease and replay the task."""
    with _LOCK:
        _, task_dir, document, _ = _require_lease(repository, worker, task_id, lease_id)
        items = [item for item in document.get("items") or [] if isinstance(item, dict)]
        errors = _validate_results(items, results)
        if errors:
            raise AgentWorkError(422, {"message": "提交内容未通过校验", "errors": errors})
        hashes = {str(item.get("item_id")): item.get("request_hash") for item in items}
        submitter = identity(agent)
        declined: list[str] = []
        for result in results:
            item_id = str(result["item_id"])
            if result.get("declined"):
                save_agent_declined(task_dir, item_id, str(result.get("reason") or ""))
                declined.append(item_id)
            else:
                save_agent_result(
                    task_dir,
                    item_id,
                    str(result["content"]),
                    request_hash=str(hashes.get(item_id) or "") or None,
                    agent=submitter,
                )
        document.pop("lease", None)
        _save_document(task_dir, document)
        repository.append_event(
            task_id=task_id,
            stage="agent_work_submitted",
            progress=_latest_progress(repository, task_id),
            message=f"{submitter['name']} 提交了 {len(results)} 项",
            payload={
                "round": document.get("round"),
                "agent": submitter,
                "items": [str(result["item_id"]) for result in results],
                "declined": declined,
            },
        )
        _resume(repository, worker, task_id)
    return {"task_id": task_id, "status": TaskStatus.RUNNING.value, "round": document.get("round")}


def use_builtin(
    repository: SqliteTaskRepository, worker: TaskWorker, task_id: str
) -> dict[str, object]:
    """"改用内置模型": decline every remaining Agent step and replay on the default chain."""
    with _LOCK:
        record = _require_task(repository, task_id)
        if record.status != TaskStatus.AGENT_SUMMARIZING:
            raise AgentWorkError(409, "任务不在等待 Agent 的状态。")
        task_dir = _task_dir(worker, task_id)
        document = load_agent_work(task_dir)
        for item in document.get("items") or []:
            if isinstance(item, dict) and item.get("item_id"):
                save_agent_declined(task_dir, str(item["item_id"]), USE_BUILTIN_REASON)
        document["use_builtin"] = True
        document.pop("lease", None)
        _save_document(task_dir, document)
        repository.append_event(
            task_id=task_id,
            stage="agent_use_builtin",
            progress=_latest_progress(repository, task_id),
            message="已改用内置模型完成剩余步骤",
            payload={"round": document.get("round")},
        )
        _resume(repository, worker, task_id)
    return {"task_id": task_id, "status": TaskStatus.RUNNING.value}


def cancel(repository: SqliteTaskRepository, worker: TaskWorker, task_id: str) -> dict[str, object]:
    """User cancels a task waiting for the Agent: ``agent_summarizing -> cancelled``."""
    with _LOCK:
        _require_task(repository, task_id)
        if not worker.cancel_agent_task(task_id):
            if repository.get_task(task_id) is None:
                raise AgentWorkError(404, "Task not found.")
            raise AgentWorkError(409, "仅等待 Agent 的任务可以取消。")
    return {"task_id": task_id, "status": TaskStatus.CANCELLED.value}


def view(repository: SqliteTaskRepository, worker: TaskWorker, task_id: str) -> dict[str, object]:
    """Read-only: this round's items and the current lease holder (no claim, no renewal)."""
    record = _require_task(repository, task_id)
    task_dir = worker.agent_task_dir(task_id)
    document = load_agent_work(task_dir) if task_dir is not None else {"round": 0, "items": []}
    waiting = record.status == TaskStatus.AGENT_SUMMARIZING
    lease = live_lease(document) if waiting else None
    since = _waiting_since(document, record) if waiting else None
    return {
        "task_id": task_id,
        "status": record.status.value,
        "batch_id": record.task_input.options.batch_id,
        "round": document.get("round") or 0,
        "items": (document.get("items") or []) if waiting else [],
        "rejected": document.get("rejected") or {},
        "waiting_since": _iso(since) if since else None,
        "use_builtin": bool(document.get("use_builtin")),
        "lease": None
        if lease is None
        else {
            "agent": lease.get("agent"),
            "claimed_at": _iso(_parse_time(lease.get("claimed_at")) or utcnow()),
            "expires_at": _iso(_parse_time(lease.get("expires_at")) or utcnow()),
        },
    }
