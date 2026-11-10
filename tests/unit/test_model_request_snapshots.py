"""Request snapshots: the exact HTTP request sent to the model for every ``usage_stage``.

Each scenario drives one or more pipeline model steps with a scripted model and
compares the recorded wire requests (URL, headers, JSON body, timeout) against a
committed snapshot under ``tests/unit/snapshots/model_requests/``. The snapshots
are the baseline for refactors (R1/R2) and for later prompt changes (C2): a
refactor must leave them byte-identical.

Regenerate deliberately with ``BILISUM_UPDATE_SNAPSHOTS=1``.
"""

from __future__ import annotations

import functools
import json
import os
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

SNAPSHOT_DIR = Path(__file__).parent / "snapshots" / "model_requests"
UPDATE_SNAPSHOTS = os.environ.get("BILISUM_UPDATE_SNAPSHOTS") == "1"

SUMMARY_CONTENT = json.dumps(
    {
        "title": "示例标题",
        "overview": "示例概览",
        "bulletPoints": ["要点一", "要点二"],
        "chapters": [
            {"title": "开场", "start": 0, "summary": "开场介绍"},
            {"title": "方法", "start": 30, "summary": "方法说明"},
        ],
    },
    ensure_ascii=False,
)
KNOWLEDGE_NOTE_CONTENT = json.dumps(
    {"knowledgeNoteMarkdown": "# 知识笔记\n\n## 开场\n\n内容"}, ensure_ascii=False
)
THIN_MINDMAP_CONTENT = json.dumps(
    {
        "title": "导图",
        "root": "root",
        "nodes": [
            {
                "id": "root",
                "label": "导图",
                "type": "root",
                "children": [{"id": "t1", "label": "唯一分支", "type": "theme"}],
            }
        ],
    },
    ensure_ascii=False,
)
KEYFRAME_PLAN_CONTENT = json.dumps(
    {
        "keyframes": [
            {"timestamp_seconds": 12, "anchor_heading": "开场", "concept": "界面", "priority": 0.9}
        ]
    },
    ensure_ascii=False,
)
VISUAL_FRAME_CONTENT = json.dumps(
    {
        "visual_type": "slide",
        "caption": "幻灯片",
        "key_facts": ["事实"],
        "importance": 4,
        "should_insert": True,
    },
    ensure_ascii=False,
)
COMPOSE_CONTENT = "# 图文笔记\n\n段落\n\n![图](visual://f0001)\n"
USAGE = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}

TRANSCRIPT = "[00:00] 开场介绍今天的主题\n[00:30] 讲解具体方法"
SEGMENTS = [
    {"start": 0, "end": 30, "text": "开场介绍今天的主题"},
    {"start": 30, "end": 60, "text": "讲解具体方法"},
]
RESULT = TaskResult(
    overview="视频概览",
    key_points=["关键点一", "关键点二"],
    timeline=[
        {"title": "开场", "start": 0, "summary": "开场介绍"},
        {"title": "方法", "start": 30, "summary": "方法说明"},
    ],
    knowledge_note_markdown="# 知识笔记\n\n## 开场\n\n内容",
)
OBSERVATIONS = [
    {
        "frame_id": "f0001",
        "timestamp_seconds": 12.0,
        "visual_type": "slide",
        "caption": "幻灯片",
        "ocr_text": "",
        "semantic_summary": "说明",
        "key_facts": ["事实"],
        "importance": 4,
        "should_insert": True,
        "suggested_anchor": "开场",
        "scene": "slide",
        "confidence": 0.9,
    }
]


def _noop_emit(*_args, **_kwargs) -> None:
    return None


def _settings(tmp_path: Path, **overrides: object) -> PipelineSettings:
    base: dict[str, object] = {
        "tasks_dir": tmp_path / "tasks",
        "llm_enabled": True,
        "llm_provider": "openai-compatible",
        "llm_api_key": "test-key",
        "llm_base_url": "https://llm.example.com/v1",
        "llm_model": "test-model",
        "summary_chunk_concurrency": 1,
        "visual_evidence_use_llm": True,
        "visual_multimodal_enabled": True,
        "visual_vlm_provider": "openai-compatible",
        "visual_evidence_base_url": "https://vlm.example.com/v1",
        "visual_evidence_model": "vision-model",
        "visual_evidence_api_key": "vision-key",
    }
    base.update(overrides)
    return PipelineSettings(**base)  # type: ignore[arg-type]


def _frame(tmp_path: Path) -> dict[str, object]:
    image = tmp_path / "f0001.jpg"
    image.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    return {
        "frame_id": "f0001",
        "timestamp_seconds": 12.0,
        "timestamp": "00:12",
        "_absolute_path": str(image),
    }


def _drive_text_steps(runner: RealPipelineRunner, tmp_path: Path) -> None:
    runner._preflight_llm_availability()
    runner._summarize(TRANSCRIPT, SEGMENTS, "示例视频", _noop_emit)
    runner._generate_mindmap_with_llm(title="示例视频", result=RESULT)
    runner._build_visual_keyframe_plan_with_llm("示例视频", RESULT, "vlm_integrated")


def _drive_chunked_summary(runner: RealPipelineRunner, tmp_path: Path) -> None:
    segments = [
        {"start": index * 30, "end": index * 30 + 30, "text": f"第{index}段：" + "内容" * 120}
        for index in range(6)
    ]
    transcript = "\n".join(f"[{index * 30}] {item['text']}" for index, item in enumerate(segments))
    runner._summarize(transcript, segments, "长视频", _noop_emit, summary_scope="summary")


def _drive_aggregate(runner: RealPipelineRunner, tmp_path: Path) -> None:
    runner._summarize(
        TRANSCRIPT,
        SEGMENTS,
        "合集",
        _noop_emit,
        source_kind="aggregate_series",
        summary_scope="summary",
    )


def _drive_visual(runner: RealPipelineRunner, tmp_path: Path) -> None:
    runner._describe_visual_frames([_frame(tmp_path)], "示例视频", RESULT, usage_records=[])
    runner._compose_visual_note_with_llm(
        "示例视频",
        RESULT.knowledge_note_markdown or "",
        OBSERVATIONS,
        {"insertions": [{"frame_id": "f0001", "anchor_heading": "开场"}]},
        usage_records=[],
    )


SCENARIOS: dict[
    str, tuple[dict[str, object], Callable[[RealPipelineRunner, Path], None], list[str]]
] = {
    # name: (settings overrides, driver, scripted contents in call order)
    "openai_text_steps": (
        {"summary_context_mode": "full"},
        _drive_text_steps,
        [
            '{"ok":true}',
            SUMMARY_CONTENT,
            KNOWLEDGE_NOTE_CONTENT,
            THIN_MINDMAP_CONTENT,
            THIN_MINDMAP_CONTENT,
            KEYFRAME_PLAN_CONTENT,
        ],
    ),
    "anthropic_text_steps": (
        {
            "summary_context_mode": "full",
            "llm_provider": "anthropic",
            "llm_base_url": "https://api.anthropic.com/v1",
            "llm_model": "claude-test",
        },
        _drive_text_steps,
        [
            '{"ok":true}',
            SUMMARY_CONTENT,
            KNOWLEDGE_NOTE_CONTENT,
            THIN_MINDMAP_CONTENT,
            THIN_MINDMAP_CONTENT,
            KEYFRAME_PLAN_CONTENT,
        ],
    ),
    "mimo_reasoning_text_steps": (
        {
            "summary_context_mode": "full",
            "llm_provider": "mimo",
            "llm_model": "MiMo-V2-Test",
            "llm_thinking_type": "disabled",
            "llm_reasoning_effort": "high",
        },
        _drive_text_steps,
        [
            '{"ok":true}',
            SUMMARY_CONTENT,
            KNOWLEDGE_NOTE_CONTENT,
            THIN_MINDMAP_CONTENT,
            THIN_MINDMAP_CONTENT,
            KEYFRAME_PLAN_CONTENT,
        ],
    ),
    "chunked_summary": (
        {"summary_context_mode": "chunked", "summary_chunk_target_chars": 800},
        _drive_chunked_summary,
        [SUMMARY_CONTENT] * 12,
    ),
    "aggregate_summary": (
        {},
        _drive_aggregate,
        [SUMMARY_CONTENT],
    ),
    "openai_visual_steps": (
        {},
        _drive_visual,
        [VISUAL_FRAME_CONTENT, COMPOSE_CONTENT],
    ),
    "mimo_reasoning_visual_steps": (
        {"llm_provider": "mimo", "visual_evidence_model": "MiMo-VL-Test"},
        _drive_visual,
        [VISUAL_FRAME_CONTENT, COMPOSE_CONTENT],
    ),
    "anthropic_visual_steps": (
        {
            "visual_vlm_provider": "anthropic",
            "visual_evidence_base_url": "https://api.anthropic.com/v1",
            "visual_evidence_model": "claude-vision",
        },
        _drive_visual,
        [VISUAL_FRAME_CONTENT, COMPOSE_CONTENT],
    ),
}


class _WireRecorder:
    """Stands in for ``httpx.Client``; records each POST and replies with scripted content."""

    def __init__(self, contents: list[str]) -> None:
        self.contents = list(contents)
        self.requests: list[dict[str, object]] = []

    def factory(self, *_args, **kwargs):
        recorder = self
        timeout = httpx.Timeout(kwargs.get("timeout"))

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, *_exc) -> None:
                return None

            def post(self, url, *, headers=None, json=None, **_kw):
                recorder.requests.append(
                    {
                        "url": str(url),
                        "headers": dict(headers or {}),
                        "json": json,
                        "timeout": repr(timeout),
                        "follow_redirects": bool(kwargs.get("follow_redirects", False)),
                    }
                )
                if not recorder.contents:
                    raise AssertionError(f"unexpected extra model request to {url}")
                body = {
                    "choices": [
                        {"message": {"role": "assistant", "content": recorder.contents.pop(0)}}
                    ],
                    "usage": USAGE,
                }
                return httpx.Response(200, json=body, request=httpx.Request("POST", str(url)))

        return _Client()


def _assert_matches_snapshot(name: str, requests: list[dict[str, object]]) -> None:
    path = SNAPSHOT_DIR / f"{name}.json"
    rendered = json.dumps(requests, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if UPDATE_SNAPSHOTS:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
        return
    assert path.exists(), f"missing snapshot {path}; run with BILISUM_UPDATE_SNAPSHOTS=1"
    assert rendered == path.read_text(encoding="utf-8"), f"model request snapshot changed: {name}"


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_wire_requests_match_snapshot(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overrides, drive, contents = SCENARIOS[name]
    recorder = _WireRecorder(contents)
    monkeypatch.setattr(httpx, "Client", recorder.factory)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    runner = RealPipelineRunner(_settings(tmp_path, **overrides))

    drive(runner, tmp_path)

    _assert_matches_snapshot(name, recorder.requests)


def wire_snapshot(fake: FakeModelClient) -> list[dict[str, object]]:
    """Render a FakeModelClient's recorded requests in the snapshot format.

    Every recorded request goes through ``build_http_request`` (the same
    function the real client uses), so a FakeModelClient run can be compared
    with the committed wire snapshots without any HTTP stubbing.
    """
    return [
        {
            "url": spec.url,
            "headers": spec.headers,
            "json": spec.json,
            "timeout": repr(httpx.Timeout(spec.timeout)),
            "follow_redirects": True,
        }
        for spec in fake.wire_requests()
    ]


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_fake_model_client_requests_match_snapshot(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overrides, drive, contents = SCENARIOS[name]
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    fake = RoutedFake(contents, usage=USAGE)
    runner = RealPipelineRunner(_settings(tmp_path, **overrides), model_client=fake)

    drive(runner, tmp_path)

    if UPDATE_SNAPSHOTS:
        return
    _assert_matches_snapshot(name, wire_snapshot(fake))
