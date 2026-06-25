export type TaskStatus = "queued" | "running" | "completed" | "failed" | "cancelled";
export type PageAggregateStatus = "not_started" | "in_progress" | "completed" | "failed";

export type TimelineItem = {
  title?: string;
  start?: number;
  summary?: string;
};

export type ChapterGroupItem = {
  title?: string;
  start?: number;
  summary?: string;
  children?: TimelineItem[];
};

export type TaskMindMapStatus = "idle" | "generating" | "ready" | "failed";
export type TaskVisualEvidenceStatus = "idle" | "generating" | "ready" | "partial" | "failed" | "unsupported";
export type VisualNoteMode = "text" | "frame_insert" | "vlm_integrated";

export type MindMapNode = {
  id: string;
  label: string;
  type: "root" | "theme" | "topic" | "leaf";
  summary: string;
  children: MindMapNode[];
  time_anchor?: number | null;
  source_chapter_titles: string[];
  source_chapter_starts: number[];
};

export type TaskMindMap = {
  version: number;
  title: string;
  root: string;
  nodes: MindMapNode[];
};

export type TaskResult = {
  overview: string;
  knowledge_note_markdown?: string;
  transcript_text: string;
  segments?: Array<{ start: number; end: number; text: string }>;
  segment_summaries: string[];
  key_points: string[];
  timeline: TimelineItem[];
  chapter_groups?: ChapterGroupItem[];
  artifacts: Record<string, string>;
  llm_prompt_tokens?: number | null;
  llm_completion_tokens?: number | null;
  llm_total_tokens?: number | null;
  mindmap_status?: TaskMindMapStatus;
  mindmap_error_message?: string | null;
  mindmap_artifact_path?: string | null;
  mindmap_updated_at?: string | null;
  visual_note_status?: TaskVisualEvidenceStatus;
  visual_note_error_message?: string | null;
  visual_note_artifact_path?: string | null;
  visual_enhanced_note_artifact_path?: string | null;
  visual_note_updated_at?: string | null;
  visual_note_mode?: VisualNoteMode | string;
  visual_frame_count?: number;
};

export type VideoAssetSummary = {
  video_id: string;
  canonical_id: string;
  platform: string;
  title: string;
  source_url: string;
  cover_url: string;
  duration?: number | null;
  latest_task_id?: string | null;
  latest_status?: TaskStatus | null;
  latest_stage?: string | null;
  has_result: boolean;
  is_favorite: boolean;
  favorite_updated_at?: string | null;
  folder_id?: string | null;
  folder_ids?: string[];
  global_order: number;
  folder_order: number;
  global_pinned: boolean;
  folder_pinned: boolean;
  pages: VideoPageOption[];
  created_at: string;
  updated_at: string;
};

export type VideoFolder = {
  folder_id: string;
  parent_id?: string | null;
  name: string;
  position: number;
  created_at: string;
  updated_at: string;
};

export type VideoLibraryPreferences = {
  new_video_position: "front" | "back";
};

export type VideoLibraryState = {
  videos: VideoAssetSummary[];
  folders: VideoFolder[];
  preferences: VideoLibraryPreferences;
};

export type VideoPageOption = {
  page: number;
  title: string;
  source_url: string;
  cover_url: string;
  duration?: number | null;
};

export type VideoPageBatchOption = VideoPageOption & {
  aggregate_status?: PageAggregateStatus;
  latest_task_status?: TaskStatus | null;
  latest_task_updated_at?: string | null;
  has_completed_result?: boolean;
};

export type VideoAssetDetail = VideoAssetSummary & {
  latest_result?: TaskResult | null;
  latest_error_message?: string | null;
};

export type VideoProbeResult = {
  video: VideoAssetSummary;
  cached: boolean;
  requires_selection: boolean;
  pages: VideoPageOption[];
};

export type PromptPreset = {
  id: string;
  name: string;
  description?: string | null;
  category?: string | null;
  icon?: string | null;
  system_prompt: string;
  user_prompt_template: string;
  auto_match_keywords: string[];
  is_builtin: boolean;
};

export type PromptPresetCreateRequest = {
  name: string;
  system_prompt: string;
  user_prompt_template: string;
  description?: string | null;
  category?: string | null;
  auto_match_keywords?: string[];
};

export type PromptMatchResult = {
  preset: PromptPreset;
  match_type: "keyword" | "fallback" | string;
  confidence: number;
};

export type TaskSummary = {
  task_id: string;
  video_id?: string | null;
  status: TaskStatus;
  input_type: string;
  source: string;
  title?: string | null;
  page_number?: number | null;
  page_title?: string | null;
  created_at: string;
  updated_at: string;
  llm_total_tokens?: number | null;
  task_duration_seconds?: number | null;
};

export type TaskDetail = TaskSummary & {
  result?: TaskResult | null;
  error_code?: string | null;
  error_message?: string | null;
};

export type VideoTaskBatchOperation = "create" | "resummary";
export type VideoTaskBatchPageAction = "skip" | "rerun";

export type VideoTaskBatchPageResult = {
  page_number: number;
  page_title?: string | null;
  action: VideoTaskBatchPageAction;
  reason?: string | null;
  existing_task_id?: string | null;
  existing_status?: TaskStatus | null;
  has_existing_result: boolean;
  task?: TaskDetail | null;
};

export type VideoTaskBatchRequest = {
  page_numbers: number[];
  confirm?: boolean;
  prompt_preset_id?: string | null;
};

export type VideoTaskBatchResponse = {
  operation: VideoTaskBatchOperation;
  requested_page_numbers: number[];
  requires_confirmation: boolean;
  created_tasks: TaskDetail[];
  skipped_pages: VideoTaskBatchPageResult[];
  conflict_pages: VideoTaskBatchPageResult[];
};

export type TaskMindMapResponse = {
  task_id: string;
  status: TaskMindMapStatus;
  error_message?: string | null;
  updated_at?: string | null;
  mindmap?: TaskMindMap | null;
};

export type VisualEvidenceFrame = {
  frame_id: string;
  timestamp_seconds: number;
  timestamp?: string;
  file_name: string;
  image_path?: string;
};

export type VisualEvidenceObservation = {
  frame_id: string;
  timestamp_seconds: number;
  caption: string;
  ocr_text?: string;
  scene?: string;
  confidence?: number | null;
};

export type VisualEvidenceContext = {
  schema_version?: number;
  task_id?: string;
  status?: TaskVisualEvidenceStatus | string;
  source_kind?: string;
  provider?: string;
  model?: string;
  frame_count?: number;
  frames?: VisualEvidenceFrame[];
  observations?: VisualEvidenceObservation[];
  warnings?: string[];
};

export type TaskVisualEvidenceResponse = {
  task_id: string;
  mode: VisualNoteMode | string;
  status: TaskVisualEvidenceStatus;
  error_message?: string | null;
  updated_at?: string | null;
  frame_count: number;
  insert_count: number;
  visual_note_markdown: string;
  enhanced_note_markdown: string;
  context?: VisualEvidenceContext | null;
};

export type TaskMarkdownExportTarget = "markdown" | "obsidian";
export type TaskExportTarget = TaskMarkdownExportTarget | "transcript";

export type TaskMarkdownExportResponse = {
  task_id: string;
  target_format: TaskExportTarget;
  path: string;
  directory: string;
  file_name: string;
  overwritten: boolean;
  artifact_key: string;
};

export type TaskEvent = {
  event_id: string;
  task_id: string;
  stage: string;
  progress: number;
  message: string;
  created_at: string;
  payload: Record<string, unknown>;
};

export type EnvironmentInfo = {
  pythonVersion?: string;
  torchInstalled?: boolean;
  torchVersion?: string;
  torchError?: string;
  torchvisionInstalled?: boolean;
  torchvisionVersion?: string;
  torchvisionBroken?: boolean;
  torchvisionError?: string;
  torchaudioInstalled?: boolean;
  torchaudioVersion?: string;
  torchaudioBroken?: boolean;
  torchaudioError?: string;
  cudaAvailable?: boolean;
  gpuName?: string;
  ytDlpVersion?: string;
  localAsrInstalled?: boolean;
  localAsrAvailable?: boolean;
  localAsrVersion?: string;
  funasrInstalled?: boolean;
  funasrAvailable?: boolean;
  funasrVersion?: string;
  funasrBroken?: boolean;
  funasrError?: string;
  chromadbInstalled?: boolean;
  chromadbVersion?: string;
  chromadbBroken?: boolean;
  chromadbError?: string;
  sentenceTransformersInstalled?: boolean;
  sentenceTransformersVersion?: string;
  sentenceTransformersBroken?: boolean;
  sentenceTransformersError?: string;
  modelscopeInstalled?: boolean;
  modelscopeVersion?: string;
  modelscopeBroken?: boolean;
  modelscopeError?: string;
  knowledgeDependenciesReady?: boolean;
  knowledgeDependenciesError?: string;
  ffmpegLocation?: string;
  recommendedModel?: string;
  recommendedDevice?: string;
  runtimeChannel?: string;
  runtimeReady?: boolean;
  runtimePython?: string;
  runtimeError?: string;
};

export type RuntimeChannelStatus = {
  runtimeChannel: string;
  path: string;
  exists: boolean;
  ready: boolean;
  python: string;
  appVersion: string;
  runtimeLayout: string;
  targetAppVersion: string;
  targetRuntimeLayout: string;
  pythonVersion: string;
  targetPythonVersion: string;
  needsUpdate: boolean;
  cudaVariant: string;
  localAsrInstalled: boolean;
  knowledgeDependenciesReady: boolean;
  environmentStatusSource?: "probe-cache" | "metadata";
};

export type RuntimeStatus = {
  baseAppVersion: string;
  baseRuntimeLayout: string;
  basePythonVersion?: string;
  pipIndexes: Array<{ label: string; url: string }>;
  channels: RuntimeChannelStatus[];
};

export type AuthStatus = {
  required: boolean;
  authenticated: boolean;
  configuredFromEnv?: boolean;
  tokenFile?: string;
};

export type ServiceSettings = {
  host: string;
  port: number;
  data_dir: string;
  cache_dir: string;
  tasks_dir: string;
  database_url: string;
  transcription_provider: string;
  prefer_bilibili_subtitle: boolean;
  whisper_model: string;
  whisper_device: string;
  whisper_compute_type: string;
  device_preference: string;
  compute_type: string;
  model_mode: string;
  fixed_model: string;
  siliconflow_asr_base_url: string;
  siliconflow_asr_model: string;
  siliconflow_asr_api_key: string;
  siliconflow_asr_api_key_configured?: boolean;
  siliconflow_asr_chunk_duration_seconds: number;
  siliconflow_asr_concurrency: number;
  multimodal_asr_base_url: string;
  multimodal_asr_model: string;
  multimodal_asr_api_key: string;
  multimodal_asr_api_key_configured?: boolean;
  multimodal_asr_chunk_duration_seconds: number;
  multimodal_asr_max_retries: number;
  funasr_model: string;
  funasr_device: string;
  funasr_vad_model: string;
  funasr_punc_model: string;
  funasr_spk_model: string;
  funasr_hub: string;
  funasr_hotword: string;
  funasr_available: boolean;
  cuda_variant: string;
  runtime_channel: string;
  output_dir: string;
  preserve_temp_audio: boolean;
  enable_cache: boolean;
  language: string;
  summary_mode: string;
  prompt_router_mode: "auto" | "confirm" | string;
  prompt_presets_path: string;
  llm_enabled: boolean;
  auto_generate_mindmap: boolean;
  visual_note_mode: VisualNoteMode;
  visual_evidence_enabled: boolean;
  visual_multimodal_enabled: boolean;
  visual_download_resolution: string;
  visual_evidence_use_llm: boolean;
  visual_vlm_provider: string;
  visual_evidence_base_url: string;
  visual_evidence_model: string;
  visual_evidence_api_key: string;
  visual_evidence_api_key_configured?: boolean;
  visual_evidence_max_frames: number;
  visual_evidence_frame_interval_seconds: number;
  visual_evidence_frame_width: number;
  visual_evidence_image_quality: number;
  visual_evidence_timeout_seconds: number;
  visual_evidence_retry_count: number;
  twelvelabs_summary_enabled: boolean;
  twelvelabs_api_key: string;
  twelvelabs_api_key_configured?: boolean;
  twelvelabs_model: string;
  twelvelabs_base_url: string;
  twelvelabs_prompt: string;
  llm_provider: string;
  llm_api_key: string;
  llm_base_url: string;
  llm_model: string;
  llm_api_key_configured?: boolean;
  knowledge_llm_mode: string;
  knowledge_llm_enabled: boolean;
  knowledge_llm_provider: string;
  knowledge_llm_api_key: string;
  knowledge_llm_base_url: string;
  knowledge_llm_model: string;
  knowledge_llm_api_key_configured?: boolean;
  knowledge_enabled: boolean;
  knowledge_embedding_provider: string;
  knowledge_embedding_model: string;
  hf_endpoint: string;
  siliconflow_embedding_api_key: string;
  siliconflow_embedding_api_key_configured?: boolean;
  siliconflow_embedding_base_url: string;
  siliconflow_embedding_model: string;
  knowledge_index_auto_rebuild: string;
  summary_system_prompt: string;
  summary_user_prompt_template: string;
  knowledge_note_system_prompt: string;
  knowledge_note_user_prompt_template: string;
  visual_note_system_prompt: string;
  visual_note_user_prompt_template: string;
  visual_frame_planning_prompt: string;
  visual_vlm_prompt: string;
  summary_chunk_target_chars: number;
  summary_chunk_overlap_segments: number;
  task_concurrency: number;
  mindmap_concurrency: number;
  summary_chunk_concurrency: number;
  summary_chunk_retry_count: number;
  ytdlp_cookies_file: string;
  ytdlp_cookies_browser: string;
  settings_file_exists?: boolean;
  defaults?: {
    knowledge_note_system_prompt?: string;
    knowledge_note_user_prompt_template?: string;
    visual_note_system_prompt?: string;
    visual_note_user_prompt_template?: string;
    visual_frame_planning_prompt?: string;
    visual_vlm_prompt?: string;
    summary_system_prompt?: string;
    summary_user_prompt_template?: string;
  };
};

export type SystemInfo = {
  application?: {
    name: string;
    version: string;
  };
  taskModel?: {
    statuses?: string[];
  };
  service?: {
    log_file?: string;
  };
  settings?: ServiceSettings;
  environment?: EnvironmentInfo;
  runtimeStartup?: RuntimeStartupInfo;
};

export type RuntimeStartupInfo = {
  status?: "initializing" | "ready" | "error" | string;
  message?: string | null;
  started_at?: string | null;
  ready_at?: string | null;
  error_at?: string | null;
  environment?: EnvironmentInfo;
};

export type SystemLogResponse = {
  path: string;
  lines: number;
  content: string;
};

export type LlmTestResponse = {
  ok: boolean;
  message: string;
  model: string;
  baseUrl: string;
  responsePreview?: string;
  jsonOutputAvailable?: boolean;
  jsonPreview?: string;
};

// 文件管理相关类型（与 desktop.d.ts 保持一致）
export type StorageLocationKind = "data" | "cache" | "tasks" | "logs" | "runtime";

export type StorageDirectoryStat = {
  key: StorageLocationKind;
  label: string;
  path: string;
  exists: boolean;
  sizeBytes: number;
  fileCount: number;
  directoryCount: number;
};

export type StorageOverview = {
  generatedAt: string;
  totals: {
    managedBytes: number;
    managedFiles: number;
    managedDirectories: number;
  };
  directories: StorageDirectoryStat[];
  cleanup: {
    serviceAvailable: boolean;
    orphanTaskCount: number;
    orphanTaskBytes: number;
    cacheCandidateCount: number;
    cacheCandidateBytes: number;
  };
};

export type KnowledgeTagRecord = {
  video_id: string;
  tag: string;
  source: string;
  confidence: number;
  created_at: string;
};

export type KnowledgeTagItem = {
  tag: string;
  count: number;
  videos: string[];
};

export type KnowledgeTagListResponse = {
  items: KnowledgeTagItem[];
};

export type VideoKnowledgeTagListResponse = {
  video_id: string;
  items: KnowledgeTagRecord[];
};

export type KnowledgeNetworkNode = {
  id: string;
  label: string;
  type: "tag" | "video" | string;
  count?: number | null;
  tags: string[];
  degree?: number | null;
  focus?: boolean;
  hidden_count?: number;
  video_count?: number;
};

export type KnowledgeNetworkLink = {
  source: string;
  target: string;
  weight?: number;
  kind?: "cooccurrence" | "association" | string;
};

export type KnowledgeNetworkResponse = {
  nodes: KnowledgeNetworkNode[];
  links: KnowledgeNetworkLink[];
  mode?: string;
  hidden_tag_count?: number;
  selected_tags?: string[];
};

export type KnowledgeSearchFilters = {
  tags?: string[];
};

export type KnowledgeSearchRequest = {
  query: string;
  limit?: number;
  filters?: KnowledgeSearchFilters;
};

export type KnowledgeSearchResult = {
  video_id: string;
  title: string;
  relevance_score: number;
  snippet: string;
  tags: string[];
  cover_url: string;
  timestamp?: string | null;
  video_title?: string | null;
  page_title?: string | null;
  page_number?: number | null;
};

export type KnowledgeSearchResponse = {
  query: string;
  results: KnowledgeSearchResult[];
  total: number;
};

export type KnowledgeSourceRef = {
  video_id: string;
  title: string;
  relevance_score: number;
  timestamp?: string | null;
  video_title?: string | null;
  page_title?: string | null;
  page_number?: number | null;
};

export type KnowledgeAskResponse = {
  query: string;
  answer: string;
  sources: KnowledgeSourceRef[];
};

export type KnowledgeReasoningDelta = {
  delta: string;
};

export type KnowledgeChatHistoryItem = {
  role: "user" | "assistant";
  content: string;
};

export type KnowledgeToolTrace = {
  id: string;
  label: string;
  status: "running" | "completed" | "error";
  detail?: string;
  meta?: Record<string, unknown>;
};

export type KnowledgeStatsResponse = {
  video_count: number;
  indexed_chunk_count: number;
  tag_count: number;
  untagged_video_count: number;
  knowledge_llm_available: boolean;
};

export type KnowledgeAutoTagVideoResult = {
  video_id: string;
  tags: string[];
};

export type KnowledgeAutoTagResponse = {
  items: KnowledgeAutoTagVideoResult[];
};
