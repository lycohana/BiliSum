import json
import sqlite3

import pytest

from video_sum_core.models.tasks import InputType, TaskInput, TaskResult, TaskStatus
from video_sum_infra.config import ServiceSettings
from video_sum_infra.llm import extract_llm_finish_reason, extract_llm_message_content
from video_sum_service.knowledge.job_coordinator import KnowledgeJobCoordinator
from video_sum_service.knowledge import job_coordinator as job_coordinator_module
from video_sum_service.knowledge import conversation_service
from video_sum_service.knowledge.conversation_service import get_job, get_suggestions, stream_conversation
from video_sum_service.knowledge.rag_service import KnowledgeAgent
from video_sum_service.repository import SqliteTaskRepository
from video_sum_service.schemas import KnowledgeReference, VideoAssetRecord


def create_repository() -> SqliteTaskRepository:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    repository = SqliteTaskRepository(connection)
    repository.initialize()
    return repository


def test_knowledge_conversation_crud_and_message_persistence() -> None:
    repository = create_repository()
    conversation = repository.create_knowledge_conversation("学习记录")
    conversation_id = str(conversation["conversation_id"])
    user = repository.create_knowledge_message(conversation_id, "user", "我最近在学什么？", references=[{"kind": "video", "id": "v1", "label": "视频"}])
    assistant = repository.create_knowledge_message(conversation_id, "assistant", "正在回答", status="streaming")
    assert user is not None and assistant is not None
    repository.update_knowledge_message(str(assistant["message_id"]), content="已完成", status="completed", sources=[])
    messages = repository.list_knowledge_messages(conversation_id)
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[-1]["content"] == "已完成"
    assert repository.update_knowledge_conversation_title(conversation_id, "新的主题") is not None
    assert repository.get_knowledge_conversation(conversation_id)["title"] == "新的主题"  # type: ignore[index]
    assert repository.delete_knowledge_conversation(conversation_id) is True
    assert repository.get_knowledge_conversation(conversation_id) is None


def test_video_task_time_metadata_is_updated() -> None:
    repository = create_repository()
    video = repository.upsert_video_asset(
        VideoAssetRecord(canonical_id="BV-time", platform="bilibili", title="时间测试", source_url="https://www.bilibili.com/video/BV-time")
    )
    task = repository.create_task(TaskInput(input_type=InputType.URL, source=video.source_url, title=video.title), video_id=video.video_id)
    repository.update_status(task.task_id, TaskStatus.COMPLETED)
    refreshed = repository.get_video_asset(video.video_id)
    assert refreshed is not None
    assert refreshed.latest_task_created_at is not None
    assert refreshed.latest_task_completed_at is not None
    assert refreshed.latest_task_duration_seconds is not None
    assert refreshed.last_summary_at is not None


def test_knowledge_suggestions_read_dictionary_tags() -> None:
    repository = create_repository()
    video = repository.upsert_video_asset(
        VideoAssetRecord(canonical_id="BV-suggestion", platform="bilibili", title="推荐测试", source_url="https://www.bilibili.com/video/BV-suggestion")
    )
    repository.add_video_tag(video.video_id, "本地 AI")

    response = get_suggestions(repository, ServiceSettings())

    assert len(response.suggestions) == 4
    assert all(item.text for item in response.suggestions)


def test_knowledge_suggestions_fall_back_when_llm_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    repository = create_repository()
    monkeypatch.setattr(conversation_service, "knowledge_llm_available", lambda _settings: True)

    def fail_llm(*args, **kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(conversation_service, "chat_knowledge_llm", fail_llm)

    response = get_suggestions(repository, ServiceSettings())

    assert len(response.suggestions) == 4
    assert response.suggestions[0].text == "我最近在学什么主题？"


def test_agent_system_question_skips_semantic_search_and_uses_recent_summary() -> None:
    repository = create_repository()
    video = repository.upsert_video_asset(
        VideoAssetRecord(canonical_id="BV-system", platform="bilibili", title="机器学习入门", source_url="https://www.bilibili.com/video/BV-system")
    )
    task = repository.create_task(TaskInput(input_type=InputType.URL, source=video.source_url, title=video.title), video_id=video.video_id)
    repository.save_result(task.task_id, TaskResult(overview="介绍监督学习与特征工程。"))
    repository.update_status(task.task_id, TaskStatus.COMPLETED)

    class NoSearchIndex:
        def search_chunks(self, *args, **kwargs):
            raise AssertionError("系统问题不应调用向量检索")

    settings = ServiceSettings(knowledge_enabled=True)
    agent = KnowledgeAgent(repository, NoSearchIndex(), settings)  # type: ignore[arg-type]
    run = agent.execute("我最近在学什么主题？")
    assert run.plan.intent == "system"
    assert "机器学习入门" in run.answer


def test_recent_summary_video_question_returns_compact_list_without_llm_or_search() -> None:
    repository = create_repository()
    video = repository.upsert_video_asset(
        VideoAssetRecord(canonical_id="BV-recent", platform="bilibili", title="我做了个b站省流工具", source_url="https://www.bilibili.com/video/BV-recent")
    )
    task = repository.create_task(TaskInput(input_type=InputType.URL, source=video.source_url, title=video.title), video_id=video.video_id)
    repository.save_result(task.task_id, TaskResult(overview="一段本地化 AI 工具开发复盘。"))
    repository.update_status(task.task_id, TaskStatus.COMPLETED)

    class NoSearchIndex:
        def search_chunks(self, *args, **kwargs):
            raise AssertionError("最近总结清单不应调用向量检索")

    agent = KnowledgeAgent(repository, NoSearchIndex(), ServiceSettings(knowledge_enabled=True))  # type: ignore[arg-type]
    run = agent.execute("我最近总结了什么视频？")
    assert run.plan.intent == "system"
    assert "我做了个b站省流工具" in run.answer
    assert "开发动机" not in run.answer
    assert "核心工作流" not in run.answer

    events = list(agent.stream("我最近总结了什么视频？"))
    assert not any(event[0] == "tool" and event[1].get("id") == "knowledge_llm" for event in events)
    done = next(payload for event, payload in events if event == "done")
    assert "我做了个b站省流工具" in done["answer"]


def test_reference_video_and_folder_are_resolved_to_scoped_video_ids() -> None:
    repository = create_repository()
    video = repository.upsert_video_asset(
        VideoAssetRecord(canonical_id="BV-ref", platform="bilibili", title="引用视频", source_url="https://www.bilibili.com/video/BV-ref")
    )
    folder = repository.create_video_folder("机器学习")
    assert folder is not None
    repository.add_video_to_folder(video.video_id, folder.folder_id)

    class NoSearchIndex:
        def search_chunks(self, *args, **kwargs):
            return []

    agent = KnowledgeAgent(repository, NoSearchIndex(), ServiceSettings(knowledge_enabled=True))  # type: ignore[arg-type]
    plan = agent.make_plan("解释这个视频", references=[KnowledgeReference(kind="folder", id=folder.folder_id, label=folder.name)])
    assert plan.video_ids == {video.video_id}


def test_llm_extractors_handle_output_blocks_and_finish_reason() -> None:
    assert extract_llm_message_content({"output": [{"content": [{"type": "reasoning", "text": "hidden"}, {"type": "output_text", "text": "正文"}]}]}) == "正文"
    assert extract_llm_message_content({"choices": [{"message": {"content": [{"type": "text", "text": "回答"}]}}]}) == "回答"
    assert extract_llm_finish_reason({"choices": [{"finish_reason": "length"}]}) == "length"


def test_knowledge_job_coordinator_aggregates_completed_tasks() -> None:
    repository = create_repository()
    conversation = repository.create_knowledge_conversation()
    conversation_id = str(conversation["conversation_id"])
    assistant = repository.create_knowledge_message(conversation_id, "assistant", "任务已创建", status="waiting_tasks")
    assert assistant is not None
    task = repository.create_task(TaskInput(input_type=InputType.TRANSCRIPT_TEXT, source="文本", title="视频一"))
    repository.save_result(task.task_id, TaskResult(overview="核心内容", key_points=["要点一"]))
    repository.update_status(task.task_id, TaskStatus.COMPLETED)
    job = repository.create_knowledge_job(conversation_id, str(assistant["message_id"]), "multi_video_summary", "queued", "总结")
    assert job is not None
    repository.add_knowledge_job_items(str(job["job_id"]), [(task.task_id, None, 0)])
    coordinator = KnowledgeJobCoordinator(repository, ServiceSettings())
    assert coordinator._process(str(job["job_id"])) is False
    assert coordinator._process(str(job["job_id"])) is False
    assert coordinator._process(str(job["job_id"])) is True
    refreshed = repository.get_knowledge_job(str(job["job_id"]))
    message = repository.get_knowledge_message(str(assistant["message_id"]))
    assert refreshed is not None and refreshed["status"] == "completed"
    assert message is not None and message["status"] == "completed" and "核心内容" in message["content"]
    response = get_job(repository, str(job["job_id"]))
    assert response.progress == 100
    assert response.items[0].progress == 100
    assert response.agent_round == 3
    assert response.agent_phase == "completed"


def test_knowledge_job_runs_persisted_agent_review_and_writing_rounds(monkeypatch: pytest.MonkeyPatch) -> None:
    repository = create_repository()
    conversation = repository.create_knowledge_conversation()
    assistant = repository.create_knowledge_message(str(conversation["conversation_id"]), "assistant", "任务已创建", status="waiting_tasks")
    assert assistant is not None
    task = repository.create_task(TaskInput(input_type=InputType.TRANSCRIPT_TEXT, source="文本", title="视频一"))
    repository.save_result(task.task_id, TaskResult(overview="核心内容", key_points=["要点一"]))
    repository.update_status(task.task_id, TaskStatus.COMPLETED)
    job = repository.create_knowledge_job(str(conversation["conversation_id"]), str(assistant["message_id"]), "multi_video_summary", "queued", "总结视频")
    assert job is not None
    repository.add_knowledge_job_items(str(job["job_id"]), [(task.task_id, None, 0)])
    calls: list[bool] = []

    monkeypatch.setattr(job_coordinator_module, "knowledge_llm_available", lambda _settings: True)

    def fake_llm(_settings, *, require_json=False, **_kwargs):
        calls.append(require_json)
        if require_json:
            return '{"review":"先审阅摘要","outline":["总览"],"missing":[],"next_action":"开始写作"}', {}
        return "# 多轮 Agent 最终文档", {}

    monkeypatch.setattr(job_coordinator_module, "chat_knowledge_llm", fake_llm)
    coordinator = KnowledgeJobCoordinator(repository, ServiceSettings())
    assert coordinator._process(str(job["job_id"])) is False
    assert coordinator._process(str(job["job_id"])) is False
    assert coordinator._process(str(job["job_id"])) is True

    refreshed = repository.get_knowledge_job(str(job["job_id"]))
    message = repository.get_knowledge_message(str(assistant["message_id"]))
    assert calls == [True, False]
    assert refreshed is not None and refreshed["agent_round"] == 3
    assert json.loads(str(refreshed["agent_state_json"]))["phase"] == "completed"
    assert message is not None and message["content"] == "# 多轮 Agent 最终文档"


def test_conversation_stream_persists_success_failure_and_interrupt() -> None:
    repository = create_repository()
    conversation = repository.create_knowledge_conversation()
    conversation_id = str(conversation["conversation_id"])

    class FakeRag:
        def __init__(self, mode: str) -> None:
            self.mode = mode

        def ask_stream(self, *args, **kwargs):
            if self.mode == "error":
                yield ("error", {"message": "模型失败", "status_code": 502})
                return
            yield ("text_delta", {"delta": "回答正文"})
            yield ("done", {"answer": "回答正文", "sources": []})

    list(stream_conversation(object(), repository, FakeRag("success"), conversation_id, "成功", 5, [], lambda: False, lambda _job_id: None))
    assert repository.list_knowledge_messages(conversation_id)[-1]["status"] == "completed"

    list(stream_conversation(object(), repository, FakeRag("error"), conversation_id, "失败", 5, [], lambda: False, lambda _job_id: None))
    assert repository.list_knowledge_messages(conversation_id)[-1]["status"] == "error"

    interrupted = stream_conversation(object(), repository, FakeRag("success"), conversation_id, "中断", 5, [], lambda: True, lambda _job_id: None)
    next(interrupted)
    next(interrupted)
    interrupted.close()
    assert repository.list_knowledge_messages(conversation_id)[-1]["status"] == "interrupted"
