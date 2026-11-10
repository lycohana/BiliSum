"""Agent work protocol (F2, design doc section 6): claim, submit, heartbeat, view,
plus the user's two ways out of ``agent_summarizing``: use builtin and cancel."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from video_sum_service import agent_work
from video_sum_service.context import settings_manager

router = APIRouter(prefix="/api/v1")


class AgentInfo(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    model: str | None = Field(default=None, max_length=200)


class AgentClaimRequest(BaseModel):
    scope: Literal["batch", "all", "task"] = "batch"
    batch_id: str | None = Field(default=None, max_length=128)
    task_id: str | None = Field(default=None, max_length=128)
    agent: AgentInfo


class AgentResultItem(BaseModel):
    item_id: str = Field(min_length=1, max_length=200)
    content: str | None = None
    declined: bool = False
    reason: str | None = Field(default=None, max_length=2000)


class AgentSubmitRequest(BaseModel):
    lease_id: str = Field(min_length=1, max_length=128)
    agent: AgentInfo
    results: list[AgentResultItem] = Field(default_factory=list, max_length=1000)


class AgentHeartbeatRequest(BaseModel):
    lease_id: str = Field(min_length=1, max_length=128)


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except agent_work.AgentWorkError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _deps(request: Request):
    return request.app.state.task_repository, request.app.state.task_worker


def _ttl() -> int:
    return int(settings_manager.current.agent_lease_ttl)


@router.post("/agent-work/claim")
def claim_agent_work(body: AgentClaimRequest, request: Request) -> dict[str, object]:
    repository, worker = _deps(request)
    return _call(
        agent_work.claim,
        repository,
        worker,
        scope=body.scope,
        batch_id=body.batch_id,
        task_id=body.task_id,
        agent=body.agent.model_dump(),
        ttl_seconds=_ttl(),
    )


@router.get("/tasks/{task_id}/agent-work")
def get_agent_work(task_id: str, request: Request) -> dict[str, object]:
    repository, worker = _deps(request)
    return _call(agent_work.view, repository, worker, task_id)


@router.put("/tasks/{task_id}/agent-work", status_code=status.HTTP_202_ACCEPTED)
def submit_agent_work(
    task_id: str, body: AgentSubmitRequest, request: Request
) -> dict[str, object]:
    repository, worker = _deps(request)
    return _call(
        agent_work.submit,
        repository,
        worker,
        task_id,
        lease_id=body.lease_id,
        agent=body.agent.model_dump(),
        results=[item.model_dump() for item in body.results],
    )


@router.post("/tasks/{task_id}/agent-heartbeat")
def agent_heartbeat(
    task_id: str, body: AgentHeartbeatRequest, request: Request
) -> dict[str, object]:
    repository, worker = _deps(request)
    return _call(
        agent_work.heartbeat, repository, worker, task_id, body.lease_id, ttl_seconds=_ttl()
    )


@router.post("/tasks/{task_id}/agent-work/use-builtin", status_code=status.HTTP_202_ACCEPTED)
def use_builtin_model(task_id: str, request: Request) -> dict[str, object]:
    repository, worker = _deps(request)
    return _call(agent_work.use_builtin, repository, worker, task_id)


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str, request: Request) -> dict[str, object]:
    repository, worker = _deps(request)
    return _call(agent_work.cancel, repository, worker, task_id)
