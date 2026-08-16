import sqlite3
import httpx
from fastapi import HTTPException

from video_sum_core.models.tasks import TaskResult
from video_sum_infra.config import ServiceSettings
from video_sum_service.app import app
from video_sum_service.context import settings_manager
from video_sum_service.knowledge.index_service import KnowledgeIndexService
from video_sum_service.knowledge.local_llm import (
    KnowledgeLlmStreamEvent,
    _extract_stream_delta,
    _extract_stream_reasoning_delta,
    chat_knowledge_llm,
    ensure_knowledge_llm_settings,
    knowledge_llm_available,
)
from video_sum_service.knowledge.rag_service import (
    KNOWLEDGE_QA_SYSTEM_PROMPT,
    KnowledgeAgent,
    RagService,
    _build_contextual_search_query,
    _build_knowledge_user_prompt,
)
from video_sum_service.knowledge.tag_service import TagService
from video_sum_service.repository import SqliteTaskRepository
from video_sum_service.routers import knowledge as knowledge_router
from video_sum_service.schemas import VideoAssetRecord


class FakeEmbedder:
    def encode(self, texts, normalize_embeddings=True):
        del normalize_embeddings
        return [[float(index + 1), 1.0, 0.5] for index, _text in enumerate(texts)]


class FakeCollection:
    def __init__(self) -> None:
        self.items: list[dict[str, object]] = []

    def add(self, ids, documents, embeddings, metadatas):
        for index, chunk_id in enumerate(ids):
            self.items = [item for item in self.items if item["id"] != chunk_id]
            self.items.append(
                {
                    "id": chunk_id,
                    "document": documents[index],
                    "embedding": embeddings[index],
                    "metadata": metadatas[index],
                }
            )

    def delete(self, ids=None, where=None):
        if ids is not None:
            id_set = set(ids)
            self.items = [item for item in self.items if item["id"] not in id_set]
        elif where and "video_id" in where:
            self.items = [item for item in self.items if item["metadata"].get("video_id") != where["video_id"]]

    def get(self):
        return {"ids": [item["id"] for item in self.items]}

    def query(self, query_embeddings, n_results):
        del query_embeddings
        ranked = sorted(
            self.items,
            key=lambda item: (
                0 if float(item["metadata"].get("anchor_seconds", -1.0) or -1.0) >= 0 else 1,
                str(item["id"]),
            ),
        )[:n_results]
        return {
            "ids": [[item["id"] for item in ranked]],
            "documents": [[item["document"] for item in ranked]],
            "metadatas": [[item["metadata"] for item in ranked]],
            "distances": [[0.1 + index * 0.05 for index, _item in enumerate(ranked)]],
        }


class FakeKnowledgeIndexService(KnowledgeIndexService):
    def __init__(self, repository, settings) -> None:
        super().__init__(repository, settings)
        self._fake_collection = FakeCollection()
        self._embedder = FakeEmbedder()

    def _get_embedder(self):
        return self._embedder

    def _get_collection(self):
        return self._fake_collection


def create_repository() -> SqliteTaskRepository:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    repository = SqliteTaskRepository(connection)
    repository.initialize()
    return repository


def create_request(repository: SqliteTaskRepository):
    app.state.task_repository = repository
    return type("Request", (), {"app": app})()


def enable_knowledge_for_router_tests(settings: ServiceSettings, monkeypatch) -> ServiceSettings:
    enabled = settings.model_copy(update={"knowledge_enabled": True})
    settings_manager._settings = enabled
    monkeypatch.setattr(knowledge_router, "_knowledge_runtime_ready", lambda: True)
    return enabled


def seed_video(repository: SqliteTaskRepository, video_id_suffix: str = "knowledge") -> VideoAssetRecord:
    video = repository.upsert_video_asset(
        VideoAssetRecord(
            canonical_id=f"BV-{video_id_suffix}",
            platform="bilibili",
            title="答题卡识别判卷",
            source_url=f"https://www.bilibili.com/video/BV-{video_id_suffix}",
        )
    )
    repository.save_result(
        repository.create_task(
            task_input=type("TaskInputProxy", (), {"model_dump": lambda self, mode='json': {}})(),  # type: ignore[arg-type]
        ).task_id,
        TaskResult(),
    )
    result = TaskResult(
        overview="视频讲解了答题卡识别和判卷流程。",
        knowledge_note_markdown="# 判卷流程\n\n先做轮廓检测，再进行填涂识别。",
        key_points=["轮廓检测是前置步骤", "填涂识别依赖透视校正"],
        timeline=[
            {"title": "轮廓检测", "start": 12, "summary": "先定位答题卡外轮廓。"},
            {"title": "填涂识别", "start": 87, "summary": "再识别涂黑区域并判分。"},
        ],
        artifacts={"summary_path": "C:/tmp/summary.json"},
    )
    task = repository.create_task(
        type("TaskInputProxy", (), {"model_dump": lambda self, mode='json': {}})(),  # type: ignore[arg-type]
        video_id=video.video_id,
    )
    repository.save_result(task.task_id, result)
    repository.update_status(task.task_id, "completed")  # type: ignore[arg-type]
    return repository.get_video_asset(video.video_id) or video


def seed_video_with_real_task(
    repository: SqliteTaskRepository,
    video_id_suffix: str = "knowledge",
    page_number: int | None = None,
    page_title: str | None = None,
) -> VideoAssetRecord:
    from video_sum_core.models.tasks import InputType, TaskInput, TaskStatus

    video = repository.upsert_video_asset(
        VideoAssetRecord(
            canonical_id=f"BV-{video_id_suffix}",
            platform="bilibili",
            title="答题卡识别判卷",
            source_url=f"https://www.bilibili.com/video/BV-{video_id_suffix}",
        )
    )
    task = repository.create_task(
        TaskInput(input_type=InputType.URL, source=video.source_url, title=page_title or video.title),
        video_id=video.video_id,
        page_number=page_number,
        page_title=page_title,
    )
    repository.save_result(
        task.task_id,
        TaskResult(
            overview="视频讲解了答题卡识别和判卷流程。",
            knowledge_note_markdown="# 判卷流程\n\n先做轮廓检测，再进行填涂识别。",
            key_points=["轮廓检测是前置步骤", "填涂识别依赖透视校正"],
            timeline=[
                {"title": "轮廓检测", "start": 12, "summary": "先定位答题卡外轮廓。"},
                {"title": "填涂识别", "start": 87, "summary": "再识别涂黑区域并判分。"},
            ],
            artifacts={"summary_path": "C:/tmp/summary.json"},
        ),
    )
    repository.update_status(task.task_id, TaskStatus.COMPLETED)
    return repository.get_video_asset(video.video_id) or video


def test_knowledge_tag_crud_and_network() -> None:
    repository = create_repository()
    video = seed_video_with_real_task(repository, "tag-network")
    repository.add_video_tag(video.video_id, "AI")
    repository.add_video_tag(video.video_id, "OpenCV")
    second_video = seed_video_with_real_task(repository, "tag-network-2")
    repository.add_video_tag(second_video.video_id, "AI")
    repository.add_video_tag(second_video.video_id, "CV")
    request = create_request(repository)
    settings = ServiceSettings(llm_enabled=False)
    tag_service = TagService(repository, settings)
    index_service = FakeKnowledgeIndexService(repository, settings)
    rag_service = RagService(repository, index_service, tag_service, settings)
    knowledge_router._get_services = lambda req: (tag_service, index_service, rag_service)  # type: ignore[assignment]

    all_tags = knowledge_router.get_tags(request)
    network = knowledge_router.get_network(request)
    focused_network = knowledge_router.get_network(request, selected_tag=["AI"], max_tags=12, max_videos=8)
    deleted = knowledge_router.delete_tag(video.video_id, "OpenCV", request)

    assert {item.tag for item in all_tags.items} >= {"AI", "OpenCV", "CV"}
    assert network.mode == "overview"
    assert all(node.type == "tag" for node in network.nodes)
    assert any(link.kind == "cooccurrence" for link in network.links)
    assert focused_network.mode == "focus"
    assert any(node.type == "video" for node in focused_network.nodes)
    assert any(link.kind == "association" for link in focused_network.links)
    assert all(item.tag != "OpenCV" for item in deleted.items)


def test_knowledge_search_returns_snippet_and_timestamp(monkeypatch) -> None:
    repository = create_repository()
    video = seed_video_with_real_task(repository, "search")
    repository.add_video_tag(video.video_id, "CV")
    request = create_request(repository)
    settings = enable_knowledge_for_router_tests(ServiceSettings(llm_enabled=False), monkeypatch)
    tag_service = TagService(repository, settings)
    index_service = FakeKnowledgeIndexService(repository, settings)
    rag_service = RagService(repository, index_service, tag_service, settings)
    knowledge_router._get_services = lambda req: (tag_service, index_service, rag_service)  # type: ignore[assignment]
    index_service.index_video(video.video_id)

    response = knowledge_router.search_knowledge(
        type("Body", (), {"query": "轮廓检测", "limit": 10, "filters": type("Filters", (), {"tags": ["CV"]})()})(),
        request,
    )

    assert response.total >= 1
    assert response.results[0].snippet
    assert response.results[0].timestamp is not None


def test_knowledge_search_requires_enabled_knowledge(monkeypatch) -> None:
    repository = create_repository()
    request = create_request(repository)
    settings_manager._settings = ServiceSettings(knowledge_enabled=False)
    monkeypatch.setattr(knowledge_router, "_knowledge_runtime_ready", lambda: True)

    try:
        knowledge_router.search_knowledge(
            type("Body", (), {"query": "测试", "limit": 10, "filters": None})(),
            request,
        )
    except HTTPException as exc:
        assert exc.status_code == 400
        assert "知识库当前未启用" in str(exc.detail)
    else:
        raise AssertionError("expected HTTPException")


def test_knowledge_index_adds_runtime_site_packages_to_import_path(monkeypatch) -> None:
    repository = create_repository()
    settings = ServiceSettings(runtime_channel="gpu-cu128")
    calls: list[str] = []
    monkeypatch.setattr("video_sum_service.knowledge.index_service.activate_runtime_pythonpath", calls.append)

    service = KnowledgeIndexService(repository, settings)
    service._ensure_runtime_import_path()

    assert calls == ["gpu-cu128"]


def test_knowledge_index_reports_missing_sentence_transformers(monkeypatch) -> None:
    repository = create_repository()
    settings = ServiceSettings(runtime_channel="gpu-cu128")
    monkeypatch.setattr("video_sum_service.knowledge.index_service.activate_runtime_pythonpath", lambda _channel: None)
    monkeypatch.setattr("video_sum_service.knowledge.index_service.importlib.util.find_spec", lambda _name: None)

    service = KnowledgeIndexService(repository, settings)

    try:
        service._get_embedder()
    except HTTPException as exc:
        assert exc.status_code == 500
        assert "缺少 sentence-transformers" in str(exc.detail)
    else:
        raise AssertionError("expected HTTPException")


def test_knowledge_index_reports_broken_sentence_transformers_import(monkeypatch) -> None:
    repository = create_repository()
    settings = ServiceSettings(runtime_channel="gpu-cu128")
    spec = type("Spec", (), {"origin": "runtime-site-packages"})()
    monkeypatch.setattr("video_sum_service.knowledge.index_service.activate_runtime_pythonpath", lambda _channel: None)
    monkeypatch.setattr("video_sum_service.knowledge.index_service.importlib.util.find_spec", lambda _name: spec)

    def fail_import(_name):
        raise ImportError("No module named 'transformers'")

    monkeypatch.setattr("video_sum_service.knowledge.index_service.importlib.import_module", fail_import)
    service = KnowledgeIndexService(repository, settings)

    try:
        service._get_embedder()
    except HTTPException as exc:
        assert exc.status_code == 500
        assert "已安装但加载失败" in str(exc.detail)
        assert "transformers" in str(exc.detail)
    else:
        raise AssertionError("expected HTTPException")


def test_knowledge_service_signature_changes_with_runtime_channel() -> None:
    base_signature = knowledge_router._knowledge_settings_signature(
        ServiceSettings(runtime_channel="base", knowledge_enabled=True)
    )
    gpu_signature = knowledge_router._knowledge_settings_signature(
        ServiceSettings(runtime_channel="gpu-cu128", knowledge_enabled=True)
    )

    assert base_signature != gpu_signature


def test_knowledge_sources_prefer_multi_page_title(monkeypatch) -> None:
    repository = create_repository()
    page_title = "P72 项目实战-答题卡识别判卷：3-填涂轮廓检测"
    video = seed_video_with_real_task(repository, "ask-page-title", page_number=72, page_title=page_title)
    request = create_request(repository)
    settings = enable_knowledge_for_router_tests(ServiceSettings(
        knowledge_llm_mode="custom",
        knowledge_llm_enabled=True,
        knowledge_llm_base_url="https://api.example.com/v1",
        knowledge_llm_model="remote-model",
    ), monkeypatch)
    tag_service = TagService(repository, settings)
    index_service = FakeKnowledgeIndexService(repository, settings)
    rag_service = RagService(repository, index_service, tag_service, settings)
    knowledge_router._get_services = lambda req: (tag_service, index_service, rag_service)  # type: ignore[assignment]
    index_service.index_video(video.video_id)
    monkeypatch.setattr(
        "video_sum_service.knowledge.rag_service.chat_knowledge_llm",
        lambda *args, **kwargs: ("这部分主要在讲填涂轮廓检测。", {"choices": []}),
    )

    response = knowledge_router.ask_knowledge(type("Body", (), {"query": "讲了什么", "context_limit": 3})(), request)
    search_response = knowledge_router.search_knowledge(
        type("Body", (), {"query": "填涂轮廓检测", "limit": 3, "filters": type("Filters", (), {"tags": []})()})(),
        request,
    )

    assert response.sources
    assert response.sources[0].title == page_title
    assert response.sources[0].page_title == page_title
    assert response.sources[0].page_number == 72
    assert response.sources[0].video_title == "答题卡识别判卷"
    assert search_response.results[0].title == page_title
    assert search_response.results[0].video_title == "答题卡识别判卷"


def test_knowledge_ask_accepts_remote_custom_llm(monkeypatch) -> None:
    repository = create_repository()
    video = seed_video_with_real_task(repository, "ask")
    request = create_request(repository)
    settings = enable_knowledge_for_router_tests(ServiceSettings(
        knowledge_llm_mode="custom",
        knowledge_llm_enabled=True,
        knowledge_llm_base_url="https://api.example.com/v1",
        knowledge_llm_model="remote-model",
    ), monkeypatch)
    tag_service = TagService(repository, settings)
    index_service = FakeKnowledgeIndexService(repository, settings)
    rag_service = RagService(repository, index_service, tag_service, settings)
    knowledge_router._get_services = lambda req: (tag_service, index_service, rag_service)  # type: ignore[assignment]
    index_service.index_video(video.video_id)
    monkeypatch.setattr(
        "video_sum_service.knowledge.rag_service.chat_knowledge_llm",
        lambda *args, **kwargs: ("先做轮廓检测，再做填涂识别。", {"choices": []}),
    )

    response = knowledge_router.ask_knowledge(type("Body", (), {"query": "讲了哪些步骤", "context_limit": 3})(), request)

    assert "轮廓检测" in response.answer
    assert response.sources


def test_knowledge_qa_prompt_encourages_evidence_based_learning_insight() -> None:
    user_prompt = _build_knowledge_user_prompt(
        "我最近主要在学什么主题？",
        ["[视频：高等数学]\n[时间：00:00]\n极限、导数和积分是本节的核心。"],
    )

    assert "学习画像" in KNOWLEDGE_QA_SYSTEM_PROMPT
    assert "直接归纳学习主题" in KNOWLEDGE_QA_SYSTEM_PROMPT
    assert "不要说" in KNOWLEDGE_QA_SYSTEM_PROMPT
    assert "暂无您的个人学习记录" in KNOWLEDGE_QA_SYSTEM_PROMPT
    assert "不要输出" in user_prompt
    assert "请提供更多上下文或学习记录" in user_prompt


def test_knowledge_qa_uses_conversation_context() -> None:
    history = [
        type("History", (), {"role": "user", "content": "我刚才问了 OpenCV 的学习线索"})(),
        type("History", (), {"role": "assistant", "content": "目前主要围绕图像处理和目标检测"})(),
    ]

    user_prompt = _build_knowledge_user_prompt(
        "那数学部分呢？",
        ["[视频：高等数学]\n[时间：00:00]\n极限、导数和积分是本节的核心。"],
        history,  # type: ignore[arg-type]
    )
    search_query = _build_contextual_search_query("那数学部分呢？", history)  # type: ignore[arg-type]

    assert "本轮会话上下文" in user_prompt
    assert "OpenCV 的学习线索" in user_prompt
    assert "近期追问线索" in search_query
    assert "OpenCV 的学习线索" in search_query


def test_knowledge_agent_plan_has_stable_tool_chain() -> None:
    repository = create_repository()
    settings = ServiceSettings(llm_enabled=False)
    index_service = FakeKnowledgeIndexService(repository, settings)
    agent = KnowledgeAgent(repository, index_service, settings)

    plan = agent.make_plan("继续讲数学部分", context_limit=3)

    assert plan.steps == ["conversation_context", "semantic_search", "context_builder", "knowledge_llm"]
    assert plan.context_limit == 3
    assert "继续讲数学部分" in plan.search_query


def test_knowledge_ask_stream_emits_tool_and_done_events(monkeypatch) -> None:
    repository = create_repository()
    video = seed_video_with_real_task(repository, "ask-stream")
    settings = ServiceSettings(
        knowledge_llm_mode="custom",
        knowledge_llm_enabled=True,
        knowledge_llm_base_url="https://api.example.com/v1",
        knowledge_llm_model="remote-model",
    )
    tag_service = TagService(repository, settings)
    index_service = FakeKnowledgeIndexService(repository, settings)
    rag_service = RagService(repository, index_service, tag_service, settings)
    index_service.index_video(video.video_id)

    monkeypatch.setattr(
        "video_sum_service.knowledge.rag_service.stream_knowledge_llm",
        lambda *args, **kwargs: iter(["先做轮廓检测", "，再做填涂识别。"]),
    )

    events = list(rag_service.ask_stream("讲了哪些步骤", context_limit=3))

    event_names = [name for name, _payload in events]
    assert "tool" in event_names
    assert "text_delta" in event_names
    assert "done" in event_names
    tool_ids = [payload["id"] for name, payload in events if name == "tool"]
    assert tool_ids[:4] == ["agent_plan", "semantic_search", "semantic_search", "context_builder"]
    assert "knowledge_llm" in tool_ids
    done_payload = next(payload for name, payload in events if name == "done")
    assert "轮廓检测" in str(done_payload["answer"])


def test_knowledge_ask_stream_falls_back_when_stream_has_no_text(monkeypatch) -> None:
    repository = create_repository()
    video = seed_video_with_real_task(repository, "ask-stream-fallback")
    settings = ServiceSettings(
        knowledge_llm_mode="custom",
        knowledge_llm_enabled=True,
        knowledge_llm_base_url="https://api.example.com/v1",
        knowledge_llm_model="remote-model",
    )
    tag_service = TagService(repository, settings)
    index_service = FakeKnowledgeIndexService(repository, settings)
    rag_service = RagService(repository, index_service, tag_service, settings)
    index_service.index_video(video.video_id)

    monkeypatch.setattr(
        "video_sum_service.knowledge.rag_service.stream_knowledge_llm",
        lambda *args, **kwargs: iter([]),
    )
    monkeypatch.setattr(
        "video_sum_service.knowledge.rag_service.chat_knowledge_llm",
        lambda *args, **kwargs: ("流式无正文后切换为普通回答。", {"choices": []}),
    )

    events = list(rag_service.ask_stream("我最近主要在学什么主题？", context_limit=3))

    assert any(
        name == "tool"
        and payload["id"] == "knowledge_llm"
        and payload.get("meta", {}).get("fallback") == "non_streaming"
        for name, payload in events
    )
    done_payload = next(payload for name, payload in events if name == "done")
    assert done_payload["answer"] == "流式无正文后切换为普通回答。"


def test_knowledge_ask_stream_continues_after_length_finish(monkeypatch) -> None:
    repository = create_repository()
    video = seed_video_with_real_task(repository, "ask-stream-length")
    settings = ServiceSettings(
        knowledge_llm_mode="custom",
        knowledge_llm_enabled=True,
        knowledge_llm_base_url="https://api.example.com/v1",
        knowledge_llm_model="remote-model",
    )
    tag_service = TagService(repository, settings)
    index_service = FakeKnowledgeIndexService(repository, settings)
    rag_service = RagService(repository, index_service, tag_service, settings)
    index_service.index_video(video.video_id)
    continuation_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        "video_sum_service.knowledge.rag_service.stream_knowledge_llm",
        lambda *args, **kwargs: iter([
            KnowledgeLlmStreamEvent(kind="content", delta="第一部分，最后讲到《高数"),
            KnowledgeLlmStreamEvent(kind="finish", delta="length"),
        ]),
    )

    def continue_chat(*args, **kwargs):
        del args
        continuation_calls.append(kwargs)
        return "《高数》的复习方法。回答完成。", {"choices": [{"finish_reason": "stop"}]}

    monkeypatch.setattr("video_sum_service.knowledge.rag_service.chat_knowledge_llm", continue_chat)

    events = list(rag_service.ask_stream("整理最近学习主题", context_limit=3))

    assert continuation_calls[0]["max_tokens"] == 3000
    assert any(
        name == "tool" and payload.get("meta", {}).get("fallback") == "continue_after_length"
        for name, payload in events
    )
    done_payload = next(payload for name, payload in events if name == "done")
    assert done_payload["answer"] == "第一部分，最后讲到《高数》的复习方法。回答完成。"


def test_knowledge_ask_stream_does_not_fallback_on_llm_timeout(monkeypatch) -> None:
    repository = create_repository()
    video = seed_video_with_real_task(repository, "ask-stream-timeout")
    settings = ServiceSettings(
        knowledge_llm_mode="custom",
        knowledge_llm_enabled=True,
        knowledge_llm_base_url="https://api.example.com/v1",
        knowledge_llm_model="remote-model",
    )
    tag_service = TagService(repository, settings)
    index_service = FakeKnowledgeIndexService(repository, settings)
    rag_service = RagService(repository, index_service, tag_service, settings)
    index_service.index_video(video.video_id)
    fallback_called = False

    def fail_stream(*args, **kwargs):
        del args, kwargs
        raise HTTPException(status_code=504, detail="知识库 LLM 响应超时")
        yield ""

    def fallback_chat(*args, **kwargs):
        del args, kwargs
        nonlocal fallback_called
        fallback_called = True
        return "不应该调用普通问答。", {"choices": []}

    monkeypatch.setattr("video_sum_service.knowledge.rag_service.stream_knowledge_llm", fail_stream)
    monkeypatch.setattr("video_sum_service.knowledge.rag_service.chat_knowledge_llm", fallback_chat)

    events = list(rag_service.ask_stream("讲了哪些步骤", context_limit=3))

    assert fallback_called is False
    assert any(
        name == "tool"
        and payload["id"] == "knowledge_llm"
        and payload["status"] == "error"
        and payload.get("meta", {}).get("status_code") == 504
        for name, payload in events
    )
    assert any(name == "error" and payload.get("status_code") == 504 for name, payload in events)
    assert not any(name == "done" for name, _payload in events)


def test_knowledge_ask_stream_tracks_reasoning_before_answer(monkeypatch) -> None:
    repository = create_repository()
    video = seed_video_with_real_task(repository, "ask-stream-reasoning")
    settings = ServiceSettings(
        knowledge_llm_mode="custom",
        knowledge_llm_enabled=True,
        knowledge_llm_base_url="https://api.example.com/v1",
        knowledge_llm_model="remote-model",
    )
    tag_service = TagService(repository, settings)
    index_service = FakeKnowledgeIndexService(repository, settings)
    rag_service = RagService(repository, index_service, tag_service, settings)
    index_service.index_video(video.video_id)

    monkeypatch.setattr(
        "video_sum_service.knowledge.rag_service.stream_knowledge_llm",
        lambda *args, **kwargs: iter([
            KnowledgeLlmStreamEvent(kind="reasoning", delta="先分析检索片段。"),
            KnowledgeLlmStreamEvent(kind="content", delta="答案正文。"),
        ]),
    )

    events = list(rag_service.ask_stream("讲了哪些步骤", context_limit=3))

    reasoning = "".join(str(payload["delta"]) for name, payload in events if name == "reasoning_delta")
    assert reasoning == "先分析检索片段。"
    assert any(
        name == "tool"
        and payload["id"] == "knowledge_llm"
        and payload.get("meta", {}).get("reasoning_character_count")
        for name, payload in events
    )
    text = "".join(str(payload["delta"]) for name, payload in events if name == "text_delta")
    assert text == "答案正文。"


def test_extract_stream_delta_accepts_common_compatible_shapes() -> None:
    assert _extract_stream_delta({"choices": [{"delta": {"content": "openai"}}]}) == "openai"
    assert _extract_stream_delta({"message": {"content": "ollama"}}) == "ollama"
    assert _extract_stream_delta({"response": "text"}) == "text"
    assert _extract_stream_delta({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "claude"}}) == "claude"


def test_extract_stream_reasoning_delta_accepts_reasoning_shapes() -> None:
    assert _extract_stream_reasoning_delta({"choices": [{"delta": {"reasoning_content": "think"}}]}) == "think"
    assert _extract_stream_reasoning_delta({"message": {"reasoning_content": "plan"}}) == "plan"


def test_chat_knowledge_llm_returns_clear_timeout_error(monkeypatch) -> None:
    settings = ServiceSettings(
        knowledge_llm_mode="custom",
        knowledge_llm_enabled=True,
        knowledge_llm_base_url="https://api.example.com/v1",
        knowledge_llm_api_key="test-key",
        knowledge_llm_model="remote-model",
    )

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def post(self, *args, **kwargs):
            raise httpx.ReadTimeout("The read operation timed out")

    monkeypatch.setattr("video_sum_service.knowledge.local_llm.httpx.Client", FakeClient)

    try:
        chat_knowledge_llm(settings, system_prompt="system", user_prompt="user")
    except HTTPException as exc:
        assert exc.status_code == 504
        assert "响应超时" in str(exc.detail)
    else:
        raise AssertionError("expected HTTPException")


def test_chat_knowledge_llm_normalizes_mimo_model(monkeypatch) -> None:
    settings = ServiceSettings(
        knowledge_llm_mode="custom",
        knowledge_llm_enabled=True,
        knowledge_llm_base_url="https://api.example.com/v1",
        knowledge_llm_api_key="test-key",
        knowledge_llm_model="MiMo-V2.5-Pro",
    )
    calls: list[dict[str, object]] = []

    class FakeResponse:
        status_code = 200
        text = '{"choices":[{"message":{"content":"答案"}}]}'

        def json(self) -> dict[str, object]:
            return {"choices": [{"message": {"content": "答案"}}]}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def post(self, url: str, headers: dict[str, str], json: dict[str, object]) -> FakeResponse:
            calls.append({"url": url, "headers": headers, "json": json})
            return FakeResponse()

    monkeypatch.setattr("video_sum_service.knowledge.local_llm.httpx.Client", FakeClient)

    content, _body = chat_knowledge_llm(settings, system_prompt="system", user_prompt="user")

    assert content == "答案"
    assert calls[0]["json"]["model"] == "mimo-v2.5-pro"


def test_chat_knowledge_llm_disables_thinking_for_official_deepseek_v4(monkeypatch) -> None:
    settings = ServiceSettings(
        knowledge_llm_mode="custom",
        knowledge_llm_enabled=True,
        knowledge_llm_base_url="https://api.deepseek.com",
        knowledge_llm_api_key="test-key",
        knowledge_llm_model="deepseek-v4-pro",
    )
    calls: list[dict[str, object]] = []

    class FakeResponse:
        status_code = 200
        text = '{"choices":[{"message":{"content":"完整答案"},"finish_reason":"stop"}]}'

        def json(self) -> dict[str, object]:
            return {"choices": [{"message": {"content": "完整答案"}, "finish_reason": "stop"}]}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def post(self, url: str, headers: dict[str, str], json: dict[str, object]) -> FakeResponse:
            calls.append({"url": url, "headers": headers, "json": json})
            return FakeResponse()

    monkeypatch.setattr("video_sum_service.knowledge.local_llm.httpx.Client", FakeClient)

    content, _body = chat_knowledge_llm(settings, system_prompt="system", user_prompt="user", max_tokens=3000)

    assert content == "完整答案"
    assert calls[0]["json"]["thinking"] == {"type": "disabled"}
    assert calls[0]["json"]["max_tokens"] == 3000


def test_knowledge_llm_availability_requires_api_key() -> None:
    missing_key = ServiceSettings(
        knowledge_llm_mode="custom",
        knowledge_llm_enabled=True,
        knowledge_llm_base_url="https://api.example.com/v1",
        knowledge_llm_model="remote-model",
    )
    ready = missing_key.model_copy(update={"knowledge_llm_api_key": "test-key"})

    assert knowledge_llm_available(missing_key) is False
    assert knowledge_llm_available(ready) is True
    try:
        ensure_knowledge_llm_settings(missing_key)
    except HTTPException as exc:
        assert exc.status_code == 400
        assert "API Key" in str(exc.detail)
    else:
        raise AssertionError("expected HTTPException")


def test_chat_knowledge_llm_accepts_anthropic_messages_response(monkeypatch) -> None:
    settings = ServiceSettings(
        llm_provider="openai-compatible",
        knowledge_llm_mode="custom",
        knowledge_llm_enabled=True,
        knowledge_llm_provider="anthropic",
        knowledge_llm_base_url="https://api.anthropic.com/v1",
        knowledge_llm_api_key="test-key",
        knowledge_llm_model="claude-3-5-haiku-latest",
    )
    calls: list[dict[str, object]] = []

    class FakeResponse:
        status_code = 200
        text = '{"content":[{"type":"text","text":"答案"}]}'

        def json(self) -> dict[str, object]:
            return {"content": [{"type": "text", "text": "答案"}]}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def post(self, url: str, headers: dict[str, str], json: dict[str, object]) -> FakeResponse:
            calls.append({"url": url, "headers": headers, "json": json})
            return FakeResponse()

    monkeypatch.setattr("video_sum_service.knowledge.local_llm.httpx.Client", FakeClient)

    content, _body = chat_knowledge_llm(settings, system_prompt="system", user_prompt="user")

    assert content == "答案"
    assert calls[0]["url"] == "https://api.anthropic.com/v1/messages"
    assert calls[0]["headers"]["x-api-key"] == "test-key"
    assert calls[0]["headers"]["anthropic-version"] == "2023-06-01"
    assert calls[0]["json"]["system"] == "system"
