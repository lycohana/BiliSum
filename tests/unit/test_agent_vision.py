"""F3: the visual steps (keyframe_plan, visual_frame, visual_compose) handed to the Agent.

Seam: FastAPI TestClient + real worker/pipeline + FakeModelClient (``service_seam``).
A local video task; ASR and ffmpeg/yt-dlp are faked at their process boundaries.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from service_seam import HEADERS, routed_fake
from test_agent_replay import (
    NO_LLM,
    NOTE,
    SUMMARY,
    _agent_seam,
    _pending,
    _submit,
    _text,
    _wait_status,
    _waiting,
)
from test_real_pipeline_runner import _VisualAssetFakes

VISUAL = {"visual_evidence_enabled": True, "visual_note_mode": "vlm_integrated"}
KEYFRAMES = {
    "keyframes": [
        {"timestamp_seconds": 0, "anchor_heading": "开场", "concept": "封面", "priority": 0.9},
        {"timestamp_seconds": 120, "anchor_heading": "开场", "concept": "演示", "priority": 0.8},
    ]
}
FRAME = {
    "visual_type": "slide",
    "caption": "演示页面",
    "ocr_text": "BiliSum",
    "key_facts": ["展示了主界面"],
    "semantic_summary": "主界面截图",
    "importance": 5,
    "should_insert": True,
    "suggested_anchor": "开场",
    "scene": "slide",
    "confidence": 0.9,
}
COMPOSED = (
    "# 笔记\n\n## 开场\n\n"
    + "这一段介绍了视频的开场内容，说明了工具的定位和主要用途，帮助读者快速理解背景。" * 4
    + "\n\n![演示页面](visual://f0001)\n\n"
    + "接下来展示了主界面的布局，各个功能区域依次说明，读者可以对照截图理解每个按钮的作用。" * 4
)


def _create_video_task(
    seam, monkeypatch, tmp_path: Path, *, capabilities: str | None = None
) -> str:
    video = tmp_path / "lesson.mp4"
    video.write_bytes(b"video")
    runner = seam.worker._pipeline_runner
    # ASR boundary: audio extraction + transcription.
    monkeypatch.setattr(runner, "_prepare_local_audio_source", lambda **kwargs: video)
    monkeypatch.setattr(
        runner,
        "_transcribe",
        lambda audio_path, language, emit: (
            "第一段内容\n第二段内容",
            [
                {"start": 0.0, "end": 4.0, "text": "第一段内容"},
                {"start": 4.0, "end": 9.0, "text": "第二段内容"},
            ],
        ),
    )
    body: dict[str, object] = {
        "input_type": "video_file",
        "source": str(video),
        "title": "测试视频",
        "engine": "agent",
        "agent_caller": True,
    }
    if capabilities is not None:
        body["agent_capabilities"] = capabilities
    response = seam.client.post("/api/v1/tasks", json=body, headers=HEADERS)
    assert response.status_code == 201, response.text
    return response.json()["task_id"]


def _through_note(seam, task_id: str) -> None:
    assert _waiting(seam, task_id) == ["summary_full#0"]
    _submit(seam, task_id, {"summary_full#0": SUMMARY})
    assert _waiting(seam, task_id) == ["knowledge_note#0"]
    _submit(seam, task_id, {"knowledge_note#0": NOTE})


def _vision_seam(monkeypatch, tmp_path: Path, fake):
    _VisualAssetFakes(monkeypatch, tmp_path)
    seam_cm = _agent_seam(monkeypatch, tmp_path, fake, mindmap=False, **NO_LLM, **VISUAL)
    return seam_cm


def test_all_frames_of_the_visual_note_are_one_round_with_reachable_images(
    tmp_path: Path, monkeypatch
) -> None:
    fake = routed_fake()  # nothing may reach a model
    with _vision_seam(monkeypatch, tmp_path, fake) as seam:
        task_id = _create_video_task(seam, monkeypatch, tmp_path)
        _through_note(seam, task_id)

        assert _waiting(seam, task_id) == ["visual_keyframe_plan#0"]
        plan_item = _pending(seam, task_id)[0]
        assert (plan_item["step"], plan_item["kind"], plan_item["images"]) == (
            "keyframe_plan",
            "text",
            [],
        )
        _submit(seam, task_id, {"visual_keyframe_plan#0": KEYFRAMES})

        # Every frame in one round.
        assert _waiting(seam, task_id) == ["visual_frame#0", "visual_frame#1"]
        items = _pending(seam, task_id)
        for item in items:
            assert (item["step"], item["kind"], item["response_format"]) == (
                "visual_frame",
                "vision",
                "json",
            )
            assert "base64" not in json.dumps(item["messages"])  # images are not inlined
            assert len(item["images"]) == 1
            image = item["images"][0]
            path = Path(image["path"])
            assert path.is_absolute() and path.is_file()
            response = seam.client.get(image["url"])  # the media endpoint needs no token
            assert response.status_code == 200, image
            assert response.content == path.read_bytes()
        assert items[0]["images"][0]["path"] != items[1]["images"][0]["path"]
        assert items[0]["images"][0]["url"].startswith(
            f"/api/v1/tasks/{task_id}/visual-evidence/media/"
        )

        _submit(seam, task_id, {"visual_frame#0": FRAME, "visual_frame#1": FRAME})
        assert _waiting(seam, task_id) == ["visual_note_compose#0"]
        compose = _pending(seam, task_id)[0]
        assert (compose["step"], compose["kind"], compose["response_format"]) == (
            "visual_compose",
            "text",
            "text",
        )
        assert "演示页面" in _text(compose["messages"])
        _submit(seam, task_id, {"visual_note_compose#0": COMPOSED})

        detail = _wait_status(seam, task_id, {"completed", "failed", "paused"})
        assert detail["status"] == "completed", detail
        result = detail["result"]
        assert result["visual_note_status"] == "ready", result.get("visual_note_error_message")
        sources = result["model_sources"]
        assert {k: sources[k] for k in ("keyframe_plan", "visual_frame", "visual_compose")} == {
            "keyframe_plan": "agent",
            "visual_frame": "agent",
            "visual_compose": "agent",
        }
        # Every frame keeps the Agent that answered it.
        assert {key for key in result["agent_identities"] if key.startswith("visual_frame")} == {
            "visual_frame#0",
            "visual_frame#1",
        }
        assert fake.requests == []


def test_declined_frames_fall_back_to_the_default_chain(tmp_path: Path, monkeypatch) -> None:
    fake = (
        routed_fake()
    )  # no internal visual model: the default chain is the placeholder observation
    with _vision_seam(monkeypatch, tmp_path, fake) as seam:
        task_id = _create_video_task(seam, monkeypatch, tmp_path)
        _through_note(seam, task_id)
        assert _waiting(seam, task_id) == ["visual_keyframe_plan#0"]
        _submit(seam, task_id, {"visual_keyframe_plan#0": KEYFRAMES})
        assert _waiting(seam, task_id) == ["visual_frame#0", "visual_frame#1"]

        claimed = seam.client.post(
            "/api/v1/agent-work/claim",
            json={"scope": "task", "task_id": task_id, "agent": {"name": "Text Agent"}},
            headers=HEADERS,
        ).json()
        response = seam.client.put(
            f"/api/v1/tasks/{task_id}/agent-work",
            json={
                "lease_id": claimed["lease_id"],
                "agent": {"name": "Text Agent"},
                "results": [
                    {"item_id": item_id, "declined": True, "reason": "无法看图"}
                    for item_id in ("visual_frame#0", "visual_frame#1")
                ],
            },
            headers=HEADERS,
        )
        assert response.status_code == 202, response.text

        # Compose still goes to the Agent, built on the placeholder observations.
        assert _waiting(seam, task_id) == ["visual_note_compose#0"]
        assert "封面" in _text(_pending(seam, task_id)[0]["messages"])
        _submit(seam, task_id, {"visual_note_compose#0": COMPOSED.replace("演示页面", "封面")})

        detail = _wait_status(seam, task_id, {"completed", "failed", "paused"})
        assert detail["status"] == "completed", detail
        sources = detail["result"]["model_sources"]
        assert (sources["visual_frame"], sources["visual_compose"]) == ("local", "agent")
        assert detail["result"]["visual_frame_count"] == 2
        assert fake.requests == []


@pytest.mark.parametrize("declined", ["visual_frame#0", "visual_frame#1"])
def test_partly_declined_frames_report_the_weakest_source(
    tmp_path: Path, monkeypatch, declined: str
) -> None:
    # One frame from the Agent, the other from the local fallback: the step is
    # reported as local, whichever order the frames were recorded in.
    fake = routed_fake()
    with _vision_seam(monkeypatch, tmp_path, fake) as seam:
        task_id = _create_video_task(seam, monkeypatch, tmp_path)
        _through_note(seam, task_id)
        assert _waiting(seam, task_id) == ["visual_keyframe_plan#0"]
        _submit(seam, task_id, {"visual_keyframe_plan#0": KEYFRAMES})
        assert _waiting(seam, task_id) == ["visual_frame#0", "visual_frame#1"]

        claimed = seam.client.post(
            "/api/v1/agent-work/claim",
            json={"scope": "task", "task_id": task_id, "agent": {"name": "Agent"}},
            headers=HEADERS,
        ).json()
        results = [
            {"item_id": item_id, "declined": True}
            if item_id == declined
            else {"item_id": item_id, "content": _text(FRAME)}
            for item_id in ("visual_frame#0", "visual_frame#1")
        ]
        response = seam.client.put(
            f"/api/v1/tasks/{task_id}/agent-work",
            json={"lease_id": claimed["lease_id"], "agent": {"name": "Agent"}, "results": results},
            headers=HEADERS,
        )
        assert response.status_code == 202, response.text

        assert _waiting(seam, task_id) == ["visual_note_compose#0"]
        _submit(seam, task_id, {"visual_note_compose#0": COMPOSED})

        detail = _wait_status(seam, task_id, {"completed", "failed", "paused"})
        assert detail["status"] == "completed", detail
        assert detail["result"]["model_sources"]["visual_frame"] == "local"


def test_text_only_agent_gets_keyframe_plan_and_compose_but_no_frames(
    tmp_path: Path, monkeypatch
) -> None:
    fake = routed_fake()
    with _vision_seam(monkeypatch, tmp_path, fake) as seam:
        task_id = _create_video_task(seam, monkeypatch, tmp_path, capabilities="text")
        _through_note(seam, task_id)
        assert _waiting(seam, task_id) == ["visual_keyframe_plan#0"]
        _submit(seam, task_id, {"visual_keyframe_plan#0": KEYFRAMES})
        assert _waiting(seam, task_id) == ["visual_note_compose#0"]
        _submit(seam, task_id, {"visual_note_compose#0": COMPOSED.replace("演示页面", "封面")})

        detail = _wait_status(seam, task_id, {"completed", "failed", "paused"})
        assert detail["status"] == "completed", detail
        sources = detail["result"]["model_sources"]
        assert (sources["keyframe_plan"], sources["visual_frame"], sources["visual_compose"]) == (
            "agent",
            "local",
            "agent",
        )
