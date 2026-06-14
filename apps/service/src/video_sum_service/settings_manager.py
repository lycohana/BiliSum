from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel
from video_sum_infra.config import (
    DEFAULT_KNOWLEDGE_NOTE_SYSTEM_PROMPT,
    DEFAULT_KNOWLEDGE_NOTE_USER_PROMPT_TEMPLATE,
    DEFAULT_SUMMARY_SYSTEM_PROMPT,
    DEFAULT_SUMMARY_USER_PROMPT_TEMPLATE,
    DEFAULT_VISUAL_FRAME_PLANNING_PROMPT,
    DEFAULT_VISUAL_NOTE_SYSTEM_PROMPT,
    DEFAULT_VISUAL_NOTE_USER_PROMPT_TEMPLATE,
    DEFAULT_VISUAL_VLM_PROMPT,
    LEGACY_SUMMARY_SYSTEM_PROMPT,
    LEGACY_SUMMARY_USER_PROMPT_TEMPLATE,
    PREVIOUS_DEFAULT_SUMMARY_SYSTEM_PROMPT,
    PREVIOUS_DEFAULT_SUMMARY_USER_PROMPT_TEMPLATE,
    PREVIOUS_DEFAULT_VISUAL_FRAME_PLANNING_PROMPT,
    PREVIOUS_DEFAULT_VISUAL_VLM_PROMPT,
    ServiceSettings,
    recommend_mindmap_concurrency,
    recommend_task_concurrency,
)

SECRET_SETTINGS_FIELDS = {
    "siliconflow_asr_api_key",
    "multimodal_asr_api_key",
    "llm_api_key",
    "knowledge_llm_api_key",
    "visual_evidence_api_key",
    "siliconflow_embedding_api_key",
}
MASKED_SECRET_PLACEHOLDER = "******"


def is_blank_or_masked_secret(value: object) -> bool:
    text = str(value or "").strip()
    return not text or text == MASKED_SECRET_PLACEHOLDER


class SettingsUpdatePayload(BaseModel):
    host: str | None = None
    port: int | None = None
    data_dir: str | None = None
    cache_dir: str | None = None
    tasks_dir: str | None = None
    database_url: str | None = None
    whisper_model: str | None = None
    whisper_device: str | None = None
    whisper_compute_type: str | None = None
    transcription_provider: str | None = None
    prefer_bilibili_subtitle: bool | None = None
    device_preference: str | None = None
    compute_type: str | None = None
    model_mode: str | None = None
    fixed_model: str | None = None
    siliconflow_asr_base_url: str | None = None
    siliconflow_asr_model: str | None = None
    siliconflow_asr_api_key: str | None = None
    siliconflow_asr_chunk_duration_seconds: int | None = None
    siliconflow_asr_concurrency: int | None = None
    multimodal_asr_base_url: str | None = None
    multimodal_asr_model: str | None = None
    multimodal_asr_api_key: str | None = None
    multimodal_asr_chunk_duration_seconds: int | None = None
    multimodal_asr_max_retries: int | None = None
    funasr_model: str | None = None
    funasr_device: str | None = None
    funasr_vad_model: str | None = None
    funasr_punc_model: str | None = None
    funasr_spk_model: str | None = None
    funasr_hub: str | None = None
    funasr_hotword: str | None = None
    cuda_variant: str | None = None
    runtime_channel: str | None = None
    output_dir: str | None = None
    preserve_temp_audio: bool | None = None
    enable_cache: bool | None = None
    language: str | None = None
    summary_mode: str | None = None
    prompt_router_mode: str | None = None
    prompt_presets_path: str | None = None
    llm_enabled: bool | None = None
    auto_generate_mindmap: bool | None = None
    visual_note_mode: str | None = None
    visual_evidence_enabled: bool | None = None
    visual_multimodal_enabled: bool | None = None
    visual_download_resolution: str | None = None
    visual_evidence_use_llm: bool | None = None
    visual_vlm_provider: str | None = None
    visual_evidence_base_url: str | None = None
    visual_evidence_model: str | None = None
    visual_evidence_api_key: str | None = None
    visual_evidence_max_frames: int | None = None
    visual_evidence_frame_interval_seconds: int | None = None
    visual_evidence_frame_width: int | None = None
    visual_evidence_image_quality: int | None = None
    visual_evidence_timeout_seconds: int | None = None
    visual_evidence_retry_count: int | None = None
    llm_provider: str | None = None
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = None
    llm_test_scope: Literal["main", "knowledge", "visual"] | None = None
    knowledge_llm_mode: str | None = None
    knowledge_llm_enabled: bool | None = None
    knowledge_llm_provider: str | None = None
    knowledge_llm_base_url: str | None = None
    knowledge_llm_model: str | None = None
    knowledge_llm_api_key: str | None = None
    knowledge_index_auto_rebuild: str | None = None
    knowledge_enabled: bool | None = None
    knowledge_embedding_provider: str | None = None
    knowledge_embedding_model: str | None = None
    siliconflow_embedding_api_key: str | None = None
    siliconflow_embedding_base_url: str | None = None
    siliconflow_embedding_model: str | None = None
    hf_endpoint: str | None = None
    summary_system_prompt: str | None = None
    summary_user_prompt_template: str | None = None
    knowledge_note_system_prompt: str | None = None
    knowledge_note_user_prompt_template: str | None = None
    visual_note_system_prompt: str | None = None
    visual_note_user_prompt_template: str | None = None
    visual_frame_planning_prompt: str | None = None
    visual_vlm_prompt: str | None = None
    summary_chunk_target_chars: int | None = None
    summary_chunk_overlap_segments: int | None = None
    task_concurrency: int | None = None
    mindmap_concurrency: int | None = None
    summary_chunk_concurrency: int | None = None
    summary_chunk_retry_count: int | None = None
    ytdlp_cookies_file: str | None = None
    ytdlp_cookies_browser: str | None = None


class SettingsManager:
    def __init__(self, base_settings: ServiceSettings) -> None:
        self._base_settings = base_settings
        self._settings = base_settings
        self._settings_path = Path(base_settings.data_dir) / "settings.json"

    @property
    def current(self) -> ServiceSettings:
        return self._settings

    @property
    def has_persisted_settings(self) -> bool:
        return self._settings_path.exists()

    def load(self) -> ServiceSettings:
        if self._settings_path.exists():
            stored = json.loads(self._settings_path.read_text(encoding="utf-8"))
            if stored.get("summary_system_prompt") == LEGACY_SUMMARY_SYSTEM_PROMPT:
                stored["summary_system_prompt"] = DEFAULT_SUMMARY_SYSTEM_PROMPT
            if stored.get("summary_system_prompt") == PREVIOUS_DEFAULT_SUMMARY_SYSTEM_PROMPT:
                stored["summary_system_prompt"] = DEFAULT_SUMMARY_SYSTEM_PROMPT
            if stored.get("summary_user_prompt_template") == LEGACY_SUMMARY_USER_PROMPT_TEMPLATE:
                stored["summary_user_prompt_template"] = DEFAULT_SUMMARY_USER_PROMPT_TEMPLATE
            if stored.get("summary_user_prompt_template") == PREVIOUS_DEFAULT_SUMMARY_USER_PROMPT_TEMPLATE:
                stored["summary_user_prompt_template"] = DEFAULT_SUMMARY_USER_PROMPT_TEMPLATE
            if "knowledge_note_system_prompt" not in stored:
                stored["knowledge_note_system_prompt"] = DEFAULT_KNOWLEDGE_NOTE_SYSTEM_PROMPT
            if "knowledge_note_user_prompt_template" not in stored:
                stored["knowledge_note_user_prompt_template"] = DEFAULT_KNOWLEDGE_NOTE_USER_PROMPT_TEMPLATE
            if "visual_note_system_prompt" not in stored:
                stored["visual_note_system_prompt"] = DEFAULT_VISUAL_NOTE_SYSTEM_PROMPT
            if "visual_note_user_prompt_template" not in stored:
                stored["visual_note_user_prompt_template"] = DEFAULT_VISUAL_NOTE_USER_PROMPT_TEMPLATE
            if "visual_frame_planning_prompt" not in stored:
                stored["visual_frame_planning_prompt"] = DEFAULT_VISUAL_FRAME_PLANNING_PROMPT
            if stored.get("visual_frame_planning_prompt") == PREVIOUS_DEFAULT_VISUAL_FRAME_PLANNING_PROMPT:
                stored["visual_frame_planning_prompt"] = DEFAULT_VISUAL_FRAME_PLANNING_PROMPT
            if "visual_vlm_prompt" not in stored:
                stored["visual_vlm_prompt"] = DEFAULT_VISUAL_VLM_PROMPT
            if stored.get("visual_vlm_prompt") == PREVIOUS_DEFAULT_VISUAL_VLM_PROMPT:
                stored["visual_vlm_prompt"] = DEFAULT_VISUAL_VLM_PROMPT
            migrated = False
            candidate = ServiceSettings.model_validate({**self._base_settings.model_dump(), **stored})
            if "task_concurrency" not in stored:
                stored["task_concurrency"] = recommend_task_concurrency(candidate)
                migrated = True
            if "mindmap_concurrency" not in stored:
                stored["mindmap_concurrency"] = recommend_mindmap_concurrency()
                migrated = True
            self._settings = ServiceSettings.model_validate({**self._base_settings.model_dump(), **stored})
            if migrated:
                self._settings_path.parent.mkdir(parents=True, exist_ok=True)
                self._settings_path.write_text(
                    json.dumps(self._settings.model_dump(mode="json"), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        else:
            self._settings = self._base_settings.model_copy(
                update={
                    "task_concurrency": recommend_task_concurrency(self._base_settings),
                    "mindmap_concurrency": recommend_mindmap_concurrency(),
                }
            )
        return self._settings

    def save(self, payload: SettingsUpdatePayload) -> ServiceSettings:
        current_dump = self._settings.model_dump(mode="json")
        updates = payload.model_dump(exclude_none=True, exclude={"llm_test_scope"})
        for field in SECRET_SETTINGS_FIELDS:
            if field in updates and is_blank_or_masked_secret(updates[field]) and current_dump.get(field):
                updates.pop(field)
        next_settings = ServiceSettings.model_validate({**current_dump, **updates})
        self._settings_path.parent.mkdir(parents=True, exist_ok=True)
        self._settings_path.write_text(
            json.dumps(next_settings.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._settings = next_settings
        return self._settings
