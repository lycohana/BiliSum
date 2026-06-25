import assert from "node:assert/strict";

import { getConfigHealth, shouldShowSetupAssistant } from "../src/appModel.ts";
import type { EnvironmentInfo, ServiceSettings } from "../src/types.ts";

function run(name: string, fn: () => void) {
  fn();
  console.log(`ok - ${name}`);
}

function createSettings(overrides: Partial<ServiceSettings> = {}): ServiceSettings {
  return {
    host: "127.0.0.1",
    port: 3838,
    data_dir: "data",
    cache_dir: "cache",
    tasks_dir: "tasks",
    database_url: "sqlite:///test.db",
    transcription_provider: "siliconflow",
    whisper_model: "tiny",
    whisper_device: "cpu",
    whisper_compute_type: "int8",
    device_preference: "cpu",
    compute_type: "int8",
    model_mode: "fixed",
    fixed_model: "tiny",
    siliconflow_asr_base_url: "https://api.siliconflow.cn/v1",
    siliconflow_asr_model: "TeleAI/TeleSpeechASR",
    siliconflow_asr_api_key: "",
    siliconflow_asr_api_key_configured: false,
    multimodal_asr_base_url: "",
    multimodal_asr_model: "",
    multimodal_asr_api_key: "",
    multimodal_asr_api_key_configured: false,
    funasr_model: "iic/Speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
    funasr_device: "cpu",
    funasr_vad_model: "fsmn-vad",
    funasr_punc_model: "ct-punc",
    funasr_spk_model: "",
    funasr_hub: "modelscope",
    funasr_hotword: "",
    cuda_variant: "cu128",
    runtime_channel: "base",
    output_dir: "",
    preserve_temp_audio: false,
    enable_cache: true,
    language: "zh",
    summary_mode: "llm",
    llm_enabled: false,
    auto_generate_mindmap: false,
    prompt_router_mode: "auto",
    prompt_presets_path: "",
    visual_note_mode: "text",
    visual_evidence_enabled: false,
    visual_multimodal_enabled: false,
    visual_download_resolution: "720p",
    visual_evidence_use_llm: false,
    visual_vlm_provider: "openai-compatible",
    visual_evidence_base_url: "",
    visual_evidence_model: "",
    visual_evidence_api_key: "",
    visual_evidence_api_key_configured: false,
    visual_evidence_max_frames: 12,
    visual_evidence_frame_interval_seconds: 10,
    visual_evidence_frame_width: 960,
    visual_evidence_image_quality: 85,
    visual_evidence_timeout_seconds: 60,
    visual_evidence_retry_count: 2,
    twelvelabs_summary_enabled: false,
    twelvelabs_api_key: "",
    twelvelabs_api_key_configured: false,
    twelvelabs_model: "pegasus1.5",
    twelvelabs_base_url: "https://api.twelvelabs.io/v1.3",
    twelvelabs_prompt: "",
    llm_provider: "openai-compatible",
    llm_api_key: "",
    llm_api_key_configured: false,
    llm_base_url: "",
    llm_model: "",
    knowledge_llm_mode: "same_as_main",
    knowledge_llm_enabled: false,
    knowledge_llm_provider: "openai-compatible",
    knowledge_llm_api_key: "",
    knowledge_llm_api_key_configured: false,
    knowledge_llm_base_url: "",
    knowledge_llm_model: "",
    knowledge_enabled: false,
    knowledge_embedding_provider: "local_huggingface",
    knowledge_embedding_model: "BAAI/bge-small-zh-v1.5",
    hf_endpoint: "",
    siliconflow_embedding_api_key: "",
    siliconflow_embedding_api_key_configured: false,
    siliconflow_embedding_base_url: "https://api.siliconflow.cn/v1",
    siliconflow_embedding_model: "BAAI/bge-large-zh-v1.5",
    knowledge_index_auto_rebuild: "disabled",
    summary_system_prompt: "",
    summary_user_prompt_template: "",
    knowledge_note_system_prompt: "",
    knowledge_note_user_prompt_template: "",
    visual_note_system_prompt: "",
    visual_note_user_prompt_template: "",
    visual_frame_planning_prompt: "",
    visual_vlm_prompt: "",
    summary_chunk_target_chars: 2200,
    summary_chunk_overlap_segments: 2,
    task_concurrency: 2,
    mindmap_concurrency: 1,
    summary_chunk_concurrency: 2,
    summary_chunk_retry_count: 2,
    settings_file_exists: true,
    ...overrides,
  };
}

run("marks siliconflow api key as blocking when missing", () => {
  const health = getConfigHealth(createSettings({ settings_file_exists: false }));

  assert.equal(health.state, "critical");
  assert.equal(health.hasBlockingIssues, true);
  assert.equal(health.blockingIssues[0]?.key, "siliconflow_asr_api_key");
});

run("marks incomplete llm setup as warning when llm is enabled", () => {
  const health = getConfigHealth(createSettings({
    llm_enabled: true,
    llm_api_key_configured: false,
    llm_base_url: "",
    llm_model: "",
    siliconflow_asr_api_key_configured: true,
  }));

  assert.equal(health.state, "warning");
  assert.equal(health.hasBlockingIssues, false);
  assert.equal(health.issues[0]?.key, "llm_configuration");
});

run("marks missing local asr runtime as blocking when local provider is selected", () => {
  const health = getConfigHealth(
    createSettings({
      transcription_provider: "local",
      siliconflow_asr_api_key_configured: true,
    }),
    { localAsrAvailable: false } as EnvironmentInfo,
  );

  assert.equal(health.state, "critical");
  assert.equal(health.blockingIssues[0]?.key, "local_asr_runtime");
});

run("separates siliconflow embedding api config from chromadb dependency status", () => {
  const health = getConfigHealth(
    createSettings({
      siliconflow_asr_api_key_configured: true,
      knowledge_enabled: true,
      knowledge_embedding_provider: "siliconflow",
      siliconflow_embedding_api_key_configured: false,
      siliconflow_embedding_base_url: "",
      siliconflow_embedding_model: "",
    }),
    { knowledgeDependenciesReady: false } as EnvironmentInfo,
  );

  assert.equal(health.state, "warning");
  assert.equal(health.hasBlockingIssues, false);
  assert.deepEqual(
    health.issues.map((issue) => issue.key),
    ["knowledge_dependencies", "siliconflow_embedding_api_key", "knowledge_llm_configuration"],
  );
  assert.match(health.issues[0]?.description || "", /chromadb/);
  assert.doesNotMatch(health.issues[0]?.description || "", /sentence-transformers/);
});

run("accepts default siliconflow embedding endpoint and model when key is configured", () => {
  const health = getConfigHealth(
    createSettings({
      siliconflow_asr_api_key_configured: true,
      llm_enabled: true,
      llm_api_key_configured: true,
      llm_base_url: "https://api.example.com/v1",
      llm_model: "test-model",
      knowledge_enabled: true,
      knowledge_embedding_provider: "siliconflow",
      siliconflow_embedding_api_key_configured: true,
      siliconflow_embedding_base_url: "",
      siliconflow_embedding_model: "",
    }),
    { knowledgeDependenciesReady: true } as EnvironmentInfo,
  );

  assert.equal(health.issues.some((issue) => issue.key === "siliconflow_embedding_api_key"), false);
});

run("shows setup assistant only for first-run installs with outstanding issues", () => {
  const settings = createSettings({ settings_file_exists: false });
  const health = getConfigHealth(settings);

  assert.equal(shouldShowSetupAssistant(health, settings), true);
  assert.equal(shouldShowSetupAssistant(health, createSettings({ settings_file_exists: true })), false);
});
