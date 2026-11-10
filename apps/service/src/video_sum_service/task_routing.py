"""F2 routing decided when a task is created.

    engine       = explicit engine ?? default_engine;
                   a caller that is not an Agent always gets builtin
    capabilities = explicit agent_capabilities ?? agent_default_capabilities
    agent_steps  = steps whose capability is declared ∩ agent_delegation ∩ AGENT_DELEGABLE_STEPS

The result is written into ``TaskOptions.model_routing`` together with the
request's ``batch_id`` and never changes while the task runs.
"""

from __future__ import annotations

from uuid import uuid4

from pydantic import BaseModel, Field, field_validator
from video_sum_core.models.tasks import BATCH_ID_MAX_CHARS, ModelRouting, TaskOptions, blank_to_none
from video_sum_core.pipeline.model_steps import agent_steps_for
from video_sum_infra.config import AGENT_CAPABILITIES, ENGINES, ServiceSettings, parse_choice_list


class TaskRoutingRequest(BaseModel):
    """Routing fields accepted by the create-task endpoints."""

    # Explicit engine (builtin / agent); None = follow default_engine.
    engine: str | None = None
    # Explicit Agent capabilities ("text,vision" or a list);
    # None/blank = follow agent_default_capabilities.
    agent_capabilities: list[str] | None = None
    # The caller declares itself an Agent (CLI driven by an Agent, MCP server).
    # Desktop / scripts leave it False.
    agent_caller: bool = False
    # Join an existing batch (e.g. a CLI creating pages one request at a time); None = new batch.
    batch_id: str | None = Field(default=None, max_length=BATCH_ID_MAX_CHARS)

    @field_validator("engine", "batch_id", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        return blank_to_none(value)

    @field_validator("engine")
    @classmethod
    def _check_engine(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.lower()
        if value not in ENGINES:
            raise ValueError(f"不支持的引擎：{value}（可选：{', '.join(ENGINES)}）。")
        return value

    @field_validator("agent_capabilities", mode="before")
    @classmethod
    def _parse_capabilities(cls, value: object) -> list[str] | None:
        if value is None:
            return None
        parsed = parse_choice_list(value, AGENT_CAPABILITIES)
        return parsed or None


DESKTOP_ROUTING = TaskRoutingRequest()
"""Routing request of the desktop UI and other non-Agent callers."""


def new_batch_id() -> str:
    return f"batch-{uuid4().hex}"


def resolve_model_routing(settings: ServiceSettings, request: TaskRoutingRequest) -> ModelRouting:
    engine = request.engine or settings.default_engine
    if not request.agent_caller or engine != "agent":
        return ModelRouting(engine="builtin")
    capabilities = request.agent_capabilities or settings.agent_default_capability_list
    return ModelRouting(
        engine="agent",
        agent_capabilities=list(capabilities),
        agent_steps=agent_steps_for(capabilities, settings.agent_delegation_list),
    )


def apply_task_routing(
    options: TaskOptions,
    settings: ServiceSettings,
    request: TaskRoutingRequest,
    *,
    batch_id: str,
) -> TaskOptions:
    """Fix the route table and batch of one task (mutates and returns ``options``)."""
    if request.engine is not None:
        options.engine = request.engine
    options.model_routing = resolve_model_routing(settings, request)
    options.batch_id = batch_id
    return options
