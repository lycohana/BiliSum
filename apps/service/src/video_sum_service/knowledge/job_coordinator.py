from __future__ import annotations

import json
from datetime import datetime, timezone
from threading import Event, Lock, Thread

from video_sum_core.models.tasks import TaskStatus
from video_sum_infra.config import ServiceSettings
from video_sum_service.knowledge.local_llm import chat_knowledge_llm, knowledge_llm_available, parse_json_payload
from video_sum_service.repository import SqliteTaskRepository


class KnowledgeJobCoordinator:
    """Runs the persisted multi-round Agent after one-shot video tasks finish."""

    def __init__(self, repository: SqliteTaskRepository, settings: ServiceSettings) -> None:
        self._repository = repository
        self._settings = settings
        self._stop = Event()
        self._wake = Event()
        self._active: set[str] = set()
        self._lock = Lock()
        self._thread = Thread(target=self._run, name="knowledge-job-coordinator", daemon=True)

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def submit(self, job_id: str) -> None:
        with self._lock:
            self._active.add(job_id)
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            persisted = self._repository.list_knowledge_jobs(["queued", "running", "agent_review", "agent_writing"])
            with self._lock:
                self._active.update(str(job["job_id"]) for job in persisted)
                active = list(self._active)
            for job_id in active:
                try:
                    if self._process(job_id):
                        with self._lock:
                            self._active.discard(job_id)
                except Exception:
                    # Keep the persisted state recoverable on the next poll or service restart.
                    continue
            self._wake.wait(timeout=1.5)
            self._wake.clear()

    def _process(self, job_id: str) -> bool:
        job = self._repository.get_knowledge_job(job_id)
        if job is None:
            return True
        items = [item for item in job.get("items", []) if isinstance(item, dict)]
        state = self._state(job)
        phase = str(state.get("phase") or "waiting_tasks")
        if not items:
            state["phase"] = "failed"
            self._repository.update_knowledge_job(
                job_id,
                status="failed",
                error_message="任务没有可执行的视频。",
                completed_at=datetime.now(timezone.utc).isoformat(),
                agent_state=state,
            )
            self._repository.update_knowledge_message(str(job["assistant_message_id"]), content="任务没有可执行的视频。", status="error")
            return True

        statuses = {str(item.get("status") or TaskStatus.FAILED.value) for item in items}
        terminal = {TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}
        if not statuses.issubset(terminal):
            if str(job.get("status")) == "queued" or phase != "waiting_tasks":
                state["phase"] = "waiting_tasks"
                self._repository.update_knowledge_job(job_id, status="running", agent_round=1, agent_state=state)
            return False
        if str(job.get("status")) == "completed" or phase == "completed":
            return True

        if phase in {"waiting_tasks", "queued", "running"}:
            state.update({
                "phase": "agent_review",
                "review": None,
                "rounds": self._append_round(state, 2, "review", "running"),
            })
            self._repository.update_knowledge_job(job_id, status="agent_review", agent_round=2, agent_state=state)
            self._repository.update_knowledge_message(
                str(job["assistant_message_id"]),
                content="视频任务已完成，Agent 正在进行第 2 轮：审阅摘要、失败项和文档结构……",
                status="waiting_tasks",
                tools=[{"id": "knowledge_agent_review", "label": "Agent 审阅轮", "status": "running", "detail": "正在检查视频摘要与缺失内容。"}],
            )
            return False

        if phase == "agent_review":
            review = state.get("review")
            if not isinstance(review, dict):
                review = self._review(str(job["query"]), items)
                state["review"] = review
            state["phase"] = "agent_writing"
            state["rounds"] = self._append_round(state, 3, "writing", "running", complete_round=2)
            self._repository.update_knowledge_job(job_id, status="agent_writing", agent_round=3, agent_state=state)
            self._repository.update_knowledge_message(
                str(job["assistant_message_id"]),
                content="Agent 第 2 轮已完成，正在进入第 3 轮：根据审阅结果生成最终 Markdown……",
                status="waiting_tasks",
                tools=[
                    {"id": "knowledge_agent_review", "label": "Agent 审阅轮", "status": "completed", "detail": "审阅完成，已形成文档结构。"},
                    {"id": "knowledge_agent_write", "label": "Agent 写作轮", "status": "running", "detail": "正在生成最终文档。"},
                ],
            )
            return False

        review = state.get("review") if isinstance(state.get("review"), dict) else self._review(str(job["query"]), items)
        content = self._write(str(job["query"]), items, review)
        failures = [
            str(item.get("task_error_message") or item.get("error_message") or item.get("task_id"))
            for item in items
            if str(item.get("status")) != TaskStatus.COMPLETED.value
        ]
        error_message = "；".join(failures) if failures else None
        assistant_content = content
        if error_message:
            assistant_content += f"\n\n> 注：以下子任务未能完成，已从聚合中标记：{error_message}"
        state["phase"] = "completed"
        state["review"] = review
        state["rounds"] = self._append_round(state, 3, "writing", "completed", complete_round=2)
        self._repository.update_knowledge_message(
            str(job["assistant_message_id"]),
            content=assistant_content,
            status="completed",
            tools=[
                {"id": "knowledge_agent_review", "label": "Agent 审阅轮", "status": "completed", "detail": "已检查摘要完整性与失败项。"},
                {"id": "knowledge_agent_write", "label": "Agent 写作轮", "status": "completed", "detail": f"已完成 {len(items) - len(failures)}/{len(items)} 个视频的聚合文档。"},
            ],
        )
        self._repository.update_knowledge_job(
            job_id,
            status="completed",
            error_message=error_message,
            completed_at=datetime.now(timezone.utc).isoformat(),
            agent_round=3,
            agent_state=state,
        )
        return True

    @staticmethod
    def _state(job: dict[str, object]) -> dict[str, object]:
        try:
            state = json.loads(str(job.get("agent_state_json") or "{}"))
        except (TypeError, ValueError):
            state = {}
        return state if isinstance(state, dict) else {}

    @staticmethod
    def _append_round(
        state: dict[str, object],
        round_number: int,
        name: str,
        status: str,
        *,
        complete_round: int | None = None,
    ) -> list[dict[str, object]]:
        existing = [item for item in state.get("rounds", []) if isinstance(item, dict)] if isinstance(state.get("rounds"), list) else []
        if complete_round is not None:
            existing = [{**item, "status": "completed"} if item.get("round") == complete_round else item for item in existing]
        if not any(item.get("round") == round_number for item in existing):
            existing.append({"round": round_number, "name": name, "status": status})
        else:
            existing = [{**item, "status": status} if item.get("round") == round_number else item for item in existing]
        return existing

    def _build_sections(self, items: list[dict[str, object]]) -> tuple[list[str], list[str]]:
        sections: list[str] = []
        failures: list[str] = []
        for index, item in enumerate(items, start=1):
            if str(item.get("status")) != TaskStatus.COMPLETED.value:
                failures.append(str(item.get("task_error_message") or item.get("error_message") or item.get("task_id")))
                continue
            task = self._repository.get_task(str(item.get("task_id") or ""))
            if task is None or task.result is None:
                failures.append(str(item.get("task_id") or "未知任务"))
                continue
            result = task.result
            sections.append(
                f"## 视频 {index}：{task.task_input.title or task.task_input.source}\n"
                f"概览：{result.overview}\n\n"
                f"关键点：\n" + "\n".join(f"- {point}" for point in result.key_points[:8]) + "\n\n"
                f"知识笔记：\n{result.knowledge_note_markdown[:5000]}"
            )
        return sections, failures

    def _review(self, query: str, items: list[dict[str, object]]) -> dict[str, object]:
        sections, failures = self._build_sections(items)
        material = "\n\n".join(sections)[:28000]
        fallback: dict[str, object] = {
            "review": "已检查所有可用视频摘要，并按共同主题组织最终文档。",
            "outline": ["总览", "共同主题", "分视频要点", "学习顺序"],
            "missing": failures,
            "next_action": "基于可用摘要生成最终 Markdown；明确标注失败视频。",
        }
        if not knowledge_llm_available(self._settings):
            return fallback
        try:
            text, _body = chat_knowledge_llm(
                self._settings,
                system_prompt="你是知识库 Agent 的审阅轮。只返回 JSON：{\"review\":\"...\",\"outline\":[\"...\"],\"missing\":[\"...\"],\"next_action\":\"...\"}。审阅视频摘要，识别共同主题、缺失信息和最终文档结构，不要直接写最终文档。",
                user_prompt=f"任务要求：{query}\n\n失败项：{json.dumps(failures, ensure_ascii=False)}\n\n视频摘要：\n{material}",
                max_tokens=900,
                temperature=0.2,
                require_json=True,
            )
            payload = parse_json_payload(text)
            return {
                "review": str(payload.get("review") or fallback["review"])[:3000],
                "outline": [str(item)[:120] for item in payload.get("outline", []) if str(item).strip()][:12] or fallback["outline"],
                "missing": [str(item)[:300] for item in payload.get("missing", []) if str(item).strip()][:20] or failures,
                "next_action": str(payload.get("next_action") or fallback["next_action"])[:500],
            }
        except Exception:
            return fallback

    def _write(self, query: str, items: list[dict[str, object]], review: dict[str, object]) -> str:
        sections, _failures = self._build_sections(items)
        if not sections:
            return "# 视频总结聚合\n\n目前没有可用于聚合的已完成摘要。"
        prompt = "\n\n".join(sections)
        review_text = json.dumps(review, ensure_ascii=False)
        if not knowledge_llm_available(self._settings):
            return f"# 视频总结聚合\n\n## 任务说明\n{query}\n\n## Agent 审阅结果\n{review.get('review', '')}\n\n{prompt}"
        try:
            answer, _body = chat_knowledge_llm(
                self._settings,
                system_prompt="你是知识库 Agent 的最终写作轮。根据任务要求、Agent 审阅结果和视频摘要，直接输出最终 Markdown 文档；不要输出思考过程，不要虚构摘要外的事实。必须包含总览、共同主题、分视频要点、建议学习顺序，并明确标记缺失内容。",
                user_prompt=f"任务要求：{query}\n\nAgent 审阅结果：\n{review_text}\n\n视频摘要材料：\n{prompt[:28000]}",
                max_tokens=2400,
                temperature=0.25,
            )
            return answer.strip()
        except Exception:
            return f"# 视频总结聚合\n\n## 任务说明\n{query}\n\n## Agent 审阅结果\n{review.get('review', '')}\n\n{prompt}"
