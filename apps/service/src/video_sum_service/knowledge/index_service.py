from __future__ import annotations

import importlib
import importlib.util
import json
import os
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from video_sum_infra.config import ServiceSettings
from video_sum_infra.runtime import activate_runtime_pythonpath, app_data_root
from video_sum_service.repository import SqliteTaskRepository
from video_sum_service.schemas import KnowledgeIndexChunkRecord, KnowledgeSearchResult

logger = logging.getLogger("video_sum_service.knowledge")


def format_anchor_seconds(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    total = max(0, int(seconds))
    minutes, sec = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{sec:02d}"
    return f"{minutes:02d}:{sec:02d}"


class KnowledgeIndexService:
    def __init__(
        self,
        repository: SqliteTaskRepository,
        settings: ServiceSettings,
        chroma_path: str | Path | None = None,
        model_name: str = "",
    ) -> None:
        self._repository = repository
        self._settings = settings
        self._model_name = model_name or settings.knowledge_embedding_model
        self._chroma_path = Path(chroma_path or (app_data_root() / "knowledge_index"))
        self._embedder = None
        self._collection = None

    def _ensure_runtime_import_path(self) -> None:
        activate_runtime_pythonpath(self._settings.runtime_channel)

    def _short_error(self, exc: BaseException, *, limit: int = 500) -> str:
        message = str(exc).strip() or exc.__class__.__name__
        return message if len(message) <= limit else f"{message[:limit]}..."

    def _import_runtime_dependency(self, module_name: str, package_label: str, missing_detail: str):
        self._ensure_runtime_import_path()
        spec = importlib.util.find_spec(module_name)
        if spec is None:
            logger.warning(
                "knowledge dependency missing package=%s module=%s runtime_channel=%s sys_path_tail=%s",
                package_label,
                module_name,
                self._settings.runtime_channel,
                sys.path[-5:],
            )
            raise HTTPException(status_code=500, detail=missing_detail)
        try:
            return importlib.import_module(module_name)
        except Exception as exc:
            logger.exception(
                "knowledge dependency import failed package=%s module=%s runtime_channel=%s origin=%s sys_path_tail=%s",
                package_label,
                module_name,
                self._settings.runtime_channel,
                getattr(spec, "origin", ""),
                sys.path[-5:],
            )
            raise HTTPException(
                status_code=500,
                detail=(
                    f"{package_label} 已安装但加载失败：{self._short_error(exc)}。"
                    "请在设置的知识库板块重新安装依赖，或先同步当前 runtime。"
                ),
            ) from exc

    def _get_embedder(self):
        if self._embedder is not None:
            return self._embedder

        provider = self._settings.knowledge_embedding_provider
        if provider == "online":
            raise HTTPException(
                status_code=501,
                detail="在线 Embedding API 暂未支持，请切换为本地 HuggingFace、ModelScope 或硅基流动。",
            )

        if provider == "siliconflow":
            self._embedder = "siliconflow"
            return self._embedder

        sentence_transformers = self._import_runtime_dependency(
            "sentence_transformers",
            "sentence-transformers",
            "缺少 sentence-transformers 依赖，无法构建知识库索引。",
        )
        try:
            if provider == "local_modelscope":
                _modelscope = self._import_runtime_dependency(
                    "modelscope",
                    "modelscope",
                    "使用 ModelScope 源需要安装 modelscope 依赖。",
                )
                from modelscope.hub.snapshot_download import snapshot_download
                model_path = snapshot_download(self._model_name)
                self._embedder = sentence_transformers.SentenceTransformer(
                    model_path, local_files_only=True
                )
            else:
                # local_huggingface — use HF Endpoint mirror if configured
                if self._settings.hf_endpoint:
                    os.environ["HF_ENDPOINT"] = self._settings.hf_endpoint
                self._embedder = sentence_transformers.SentenceTransformer(self._model_name)
        except Exception as exc:
            logger.exception(
                "knowledge embedding model load failed model=%s provider=%s runtime_channel=%s hf_endpoint=%s",
                self._model_name,
                provider,
                self._settings.runtime_channel,
                self._settings.hf_endpoint or "(default)",
            )
            raise HTTPException(
                status_code=500,
                detail=(
                    f"知识库向量模型加载失败：{self._short_error(exc)}。"
                    "请检查网络 / HuggingFace 缓存，或稍后重试。"
                ),
            ) from exc
        return self._embedder

    def _get_collection(self):
        if self._collection is None:
            chromadb = self._import_runtime_dependency(
                "chromadb",
                "chromadb",
                "缺少 chromadb 依赖，无法构建知识库索引。",
            )
            try:
                self._chroma_path.mkdir(parents=True, exist_ok=True)
                client = chromadb.PersistentClient(path=str(self._chroma_path))
                # Determine embedding dimension from the model before creating
                # the collection, so chromadb can validate vector dimensions.
                embedder = self._get_embedder()
                if embedder == "siliconflow":
                    # Get dimension by calling API with a test query
                    test_embedding = self._embed_texts_siliconflow(["test"])
                    dim = len(test_embedding[0]) if test_embedding else 1024
                else:
                    dim = embedder.get_sentence_embedding_dimension() or 0
                existing = None
                try:
                    existing = client.get_collection("bilisum_knowledge")
                except Exception:
                    pass
                if existing is not None:
                    existing_dim = (existing.metadata or {}).get("embedding_dim", 0) if hasattr(existing, "metadata") else 0
                    if int(existing_dim or 0) != int(dim):
                        client.delete_collection("bilisum_knowledge")
                        existing = None
                if existing is None:
                    self._collection = client.create_collection(
                        name="bilisum_knowledge",
                        metadata={"hnsw:space": "cosine", "embedding_dim": dim},
                    )
                else:
                    self._collection = existing
            except Exception as exc:
                logger.exception(
                    "knowledge chromadb collection init failed path=%s runtime_channel=%s",
                    self._chroma_path,
                    self._settings.runtime_channel,
                )
                raise HTTPException(
                    status_code=500,
                    detail=f"知识库索引存储初始化失败：{self._short_error(exc)}。",
                ) from exc
        return self._collection

    def _embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        embedder = self._get_embedder()
        if embedder == "siliconflow":
            return self._embed_texts_siliconflow(texts)

        vectors = embedder.encode(texts, normalize_embeddings=True)
        return [list(map(float, vector)) for vector in vectors]

    def _embed_texts_siliconflow(self, texts: list[str]) -> list[list[float]]:
        import httpx

        api_key = self._settings.siliconflow_embedding_api_key
        if not api_key:
            raise HTTPException(
                status_code=500,
                detail="硅基流动 Embedding API Key 未配置。请在设置中填写 siliconflow_embedding_api_key。",
            )

        base_url = self._settings.siliconflow_embedding_base_url.rstrip("/")
        model = self._settings.siliconflow_embedding_model or self._model_name

        try:
            with httpx.Client(timeout=60.0) as client:
                response = client.post(
                    f"{base_url}/embeddings",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "input": texts,
                        "encoding_format": "float",
                    },
                )
                response.raise_for_status()
                result = response.json()
                data = result.get("data", [])
                if not data:
                    raise HTTPException(
                        status_code=500,
                        detail="硅基流动 Embedding API 返回数据为空。",
                    )
                embeddings = [item["embedding"] for item in sorted(data, key=lambda x: x["index"])]
                return embeddings
        except httpx.HTTPStatusError as exc:
            logger.exception(
                "siliconflow embedding API error status=%s model=%s",
                exc.response.status_code,
                model,
            )
            raise HTTPException(
                status_code=502,
                detail=f"硅基流动 Embedding API 请求失败（HTTP {exc.response.status_code}）。",
            ) from exc
        except Exception as exc:
            logger.exception(
                "siliconflow embedding API error model=%s",
                model,
            )
            raise HTTPException(
                status_code=500,
                detail=f"硅基流动 Embedding API 调用异常：{self._short_error(exc)}。",
            ) from exc

    def _split_markdown_sections(self, markdown: str) -> list[tuple[str, str]]:
        content = str(markdown or "").strip()
        if not content:
            return []
        sections: list[tuple[str, str]] = []
        current_title = "知识笔记"
        current_lines: list[str] = []
        for line in content.splitlines():
            if re.match(r"^\s{0,3}#{1,3}\s+", line):
                if current_lines:
                    section_body = "\n".join(current_lines).strip()
                    if section_body:
                        sections.append((current_title, section_body))
                current_title = re.sub(r"^\s{0,3}#{1,3}\s+", "", line).strip() or "知识笔记"
                current_lines = []
                continue
            current_lines.append(line)
        if current_lines:
            section_body = "\n".join(current_lines).strip()
            if section_body:
                sections.append((current_title, section_body))
        return sections

    def _build_chunks_for_video(self, video_id: str) -> tuple[Any, list[KnowledgeIndexChunkRecord]]:
        asset = self._repository.get_video_asset(video_id)
        if asset is None or asset.latest_result is None:
            return None, []
        result = asset.latest_result
        task = self._repository.get_task(asset.latest_task_id) if asset.latest_task_id else None
        page_title = str(task.page_title or task.task_input.title or "").strip() if task is not None else ""
        display_title = page_title or asset.title
        now = datetime.now(timezone.utc)
        chunk_specs: list[dict[str, object]] = []

        overview_parts = [str(result.overview or "").strip(), *[str(item).strip() for item in result.key_points if str(item).strip()]]
        overview_text = "\n".join([part for part in overview_parts if part]).strip()
        if overview_text:
            chunk_specs.append(
                {
                    "index_type": "video_summary",
                    "segment_order": 0,
                    "anchor_label": None,
                    "anchor_seconds": None,
                    "content": f"{display_title}\n{overview_text}",
                }
            )

        for index, chapter in enumerate(result.timeline):
            if not isinstance(chapter, dict):
                continue
            title = str(chapter.get("title") or f"章节 {index + 1}").strip()
            summary = str(chapter.get("summary") or "").strip()
            start_raw = chapter.get("start")
            start = float(start_raw) if isinstance(start_raw, (int, float)) else None
            content = "\n".join([display_title, title, summary]).strip()
            if summary:
                chunk_specs.append(
                    {
                        "index_type": "chapter",
                        "segment_order": index + 1,
                        "anchor_label": title,
                        "anchor_seconds": start,
                        "content": content,
                    }
                )

        for index, (title, body) in enumerate(self._split_markdown_sections(result.knowledge_note_markdown)):
            chunk_specs.append(
                {
                    "index_type": "knowledge_note",
                    "segment_order": index + 1,
                    "anchor_label": title,
                    "anchor_seconds": None,
                    "content": f"{display_title}\n{title}\n{body}".strip(),
                }
            )

        chunk_specs = [item for item in chunk_specs if str(item["content"]).strip()]
        if not chunk_specs:
            return asset, []

        vectors = self._embed_texts([str(item["content"]) for item in chunk_specs])
        chunks = [
            KnowledgeIndexChunkRecord(
                chunk_id=f"{video_id}:{item['index_type']}:{item['segment_order'] or 0}",
                video_id=video_id,
                embedding_json=json.dumps(vectors[index], ensure_ascii=False),
                indexed_content=str(item["content"]),
                index_type=str(item["index_type"]),
                segment_order=int(item["segment_order"]) if item["segment_order"] is not None else None,
                anchor_label=str(item["anchor_label"]) if item["anchor_label"] else None,
                anchor_seconds=float(item["anchor_seconds"]) if item["anchor_seconds"] is not None else None,
                created_at=now,
                updated_at=now,
            )
            for index, item in enumerate(chunk_specs)
        ]
        return asset, chunks

    def index_video(self, video_id: str, content: str | None = None) -> bool:
        del content
        asset, chunks = self._build_chunks_for_video(video_id)
        if asset is None:
            return False

        collection = self._get_collection()
        try:
            collection.delete(where={"video_id": video_id})
        except Exception:
            pass

        self._repository.replace_knowledge_chunks(video_id, chunks)
        if not chunks:
            return True

        tags = [item.tag for item in self._repository.list_video_tags(video_id)]
        task = self._repository.get_task(asset.latest_task_id) if asset.latest_task_id else None
        page_number = int(task.page_number) if task is not None and task.page_number is not None else None
        page_title = str(task.page_title or task.task_input.title or "").strip() if task is not None else ""
        collection.add(
            ids=[chunk.chunk_id for chunk in chunks],
            documents=[chunk.indexed_content for chunk in chunks],
            embeddings=[json.loads(chunk.embedding_json) for chunk in chunks],
            metadatas=[
                {
                    "video_id": chunk.video_id,
                    "index_type": chunk.index_type,
                    "anchor_label": chunk.anchor_label or "",
                    "anchor_seconds": float(chunk.anchor_seconds) if chunk.anchor_seconds is not None else -1.0,
                    "title": asset.title,
                    "page_number": page_number if page_number is not None else -1,
                    "page_title": page_title,
                    "display_title": page_title or asset.title,
                    "cover_url": asset.cover_url or "",
                    "tags_json": json.dumps(tags, ensure_ascii=False),
                }
                for chunk in chunks
            ],
        )
        return True

    def remove_video(self, video_id: str) -> bool:
        self._repository.delete_knowledge_chunks(video_id)
        try:
            self._get_collection().delete(where={"video_id": video_id})
        except HTTPException:
            raise
        except Exception:
            pass
        return True

    def rebuild_index(self, force: bool = False) -> int:
        del force
        videos = [video for video in self._repository.list_video_assets() if video.latest_result is not None]
        collection = self._get_collection()
        try:
            existing = collection.get()
            ids = existing.get("ids") if isinstance(existing, dict) else None
            if ids:
                collection.delete(ids=ids)
        except Exception:
            pass

        self._repository.clear_knowledge_chunks()
        count = 0
        for video in videos:
            if self.index_video(video.video_id):
                count += 1
        return count

    def _fetch_candidate_chunks(self, query: str, limit: int = 10, tag_filter: list[str] | None = None) -> list[dict[str, object]]:
        cleaned_query = str(query or "").strip()
        if not cleaned_query:
            return []
        if self._repository.get_knowledge_chunk_count() == 0:
            self.rebuild_index()

        effective_limit = max(limit * 4, 12)
        query_embedding = self._embed_texts([cleaned_query])[0]
        query_result = self._get_collection().query(
            query_embeddings=[query_embedding],
            n_results=effective_limit,
        )
        ids = query_result.get("ids", [[]])[0] if isinstance(query_result, dict) else []
        documents = query_result.get("documents", [[]])[0] if isinstance(query_result, dict) else []
        metadatas = query_result.get("metadatas", [[]])[0] if isinstance(query_result, dict) else []
        distances = query_result.get("distances", [[]])[0] if isinstance(query_result, dict) else []

        allowed_video_ids: set[str] | None = None
        if tag_filter:
            selected = {tag.strip() for tag in tag_filter if str(tag).strip()}
            if selected:
                allowed_video_ids = {
                    item.video_id
                    for item in self._repository.list_all_video_tags()
                    if item.tag in selected
                }

        candidates: list[dict[str, object]] = []
        for index, chunk_id in enumerate(ids):
            metadata = metadatas[index] if index < len(metadatas) and isinstance(metadatas[index], dict) else {}
            video_id = str(metadata.get("video_id") or "")
            if allowed_video_ids is not None and video_id not in allowed_video_ids:
                continue
            distance = float(distances[index]) if index < len(distances) else 1.0
            relevance = max(0.0, 1.0 - distance / 2.0)
            candidates.append(
                {
                    "chunk_id": chunk_id,
                    "video_id": video_id,
                    "document": documents[index] if index < len(documents) else "",
                    "metadata": metadata,
                    "relevance_score": round(relevance, 4),
                }
            )
        return candidates

    def search(self, query: str, limit: int = 10, tag_filter: list[str] | None = None) -> list[KnowledgeSearchResult]:
        candidates = self._fetch_candidate_chunks(query, limit=limit, tag_filter=tag_filter)
        if not candidates:
            return []

        grouped: dict[str, dict[str, object]] = {}
        for candidate in candidates:
            video_id = str(candidate["video_id"])
            current = grouped.get(video_id)
            if current is None or float(candidate["relevance_score"]) > float(current["relevance_score"]):
                grouped[video_id] = candidate

        ordered = sorted(grouped.values(), key=lambda item: float(item["relevance_score"]), reverse=True)[: max(1, limit)]
        results: list[KnowledgeSearchResult] = []
        for candidate in ordered:
            video_id = str(candidate["video_id"])
            asset = self._repository.get_video_asset(video_id)
            metadata = candidate["metadata"] if isinstance(candidate["metadata"], dict) else {}
            video_title = asset.title if asset is not None else str(metadata.get("title") or "未知视频")
            page_title = str(metadata.get("page_title") or metadata.get("display_title") or "").strip()
            if page_title == video_title:
                page_title = ""
            page_number_raw = metadata.get("page_number")
            page_number = int(page_number_raw) if isinstance(page_number_raw, (int, float)) and int(page_number_raw) > 0 else None
            snippet = str(candidate["document"]).strip().replace("\n", " ")
            snippet = snippet[:180].rstrip() + ("..." if len(snippet) > 180 else "")
            tags = [item.tag for item in self._repository.list_video_tags(video_id)]
            results.append(
                KnowledgeSearchResult(
                    video_id=video_id,
                    title=page_title or video_title,
                    relevance_score=float(candidate["relevance_score"]),
                    snippet=snippet,
                    tags=tags,
                    cover_url=asset.cover_url if asset is not None else str(metadata.get("cover_url") or ""),
                    timestamp=format_anchor_seconds(
                        float(metadata["anchor_seconds"])
                        if metadata.get("anchor_seconds") not in {None, "", -1, -1.0}
                        else None
                    ),
                    video_title=video_title,
                    page_title=page_title or None,
                    page_number=page_number,
                )
            )
        return results

    def search_chunks(self, query: str, limit: int = 5, tag_filter: list[str] | None = None) -> list[dict[str, object]]:
        return self._fetch_candidate_chunks(query, limit=limit, tag_filter=tag_filter)[: max(1, limit)]
