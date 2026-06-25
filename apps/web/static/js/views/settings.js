import { escapeHtml } from "../utils.js";

function getStatusTone(message) {
  if (/失败|错误|不可用|未就绪|无效|已取消|关闭/i.test(message)) {
    return "error";
  }
  if (/完成|成功|已保存|已刷新|已开始|已删除|已更新|已复制|已请求/i.test(message)) {
    return "success";
  }
  return "info";
}

function renderStatusNotice(message, statusKey) {
  if (!message) {
    return "";
  }
  const tone = getStatusTone(message);
  return `
    <div class="action-status tone-${tone}">
      <span class="action-status-copy">${escapeHtml(message)}</span>
      <button class="action-status-close" type="button" data-dismiss-status="${statusKey}" aria-label="关闭提示">
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">
          <path d="M3 3L9 9M9 3L3 9" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"></path>
        </svg>
      </button>
    </div>
  `;
}

export function renderSettingsView(state) {
  const settings = state.settings || {};
  const info = state.systemInfo || {};
  const env = state.environment || {};
  const transcriptionProvider = settings.transcription_provider || "siliconflow";

  return `
    <section class="settings-grid">
      <!-- 运行环境与 CUDA -->
      <article class="grid-card settings-wide env-card">
        <div class="panel-header">
          <h2>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
              <rect x="2" y="3" width="20" height="14" rx="2" ry="2"></rect>
              <line x1="8" y1="21" x2="16" y2="21"></line>
              <line x1="12" y1="17" x2="12" y2="21"></line>
            </svg>
            运行环境与 CUDA
          </h2>
          <p>环境检测、推荐设备和 CUDA 配置</p>
        </div>

        <!-- 环境状态网格 -->
        <section class="env-panel">
          <div class="env-panel-head">
            <span class="env-panel-kicker">Environment Snapshot</span>
            <p>当前硬件、依赖版本和运行环境建议一览</p>
          </div>
          <div class="env-summary-grid">
            ${renderEnvCard("推荐设备", env.recommendedDevice || "-", "cpu")}
            ${renderEnvCard("推荐模型", env.recommendedModel || "-", "model")}
            ${renderEnvCard("GPU 状态", env.cudaAvailable ? "已启用" : "未启用", env.cudaAvailable ? "success" : "warning")}
            ${renderEnvCard("GPU 名称", env.gpuName || "未检测到", env.gpuName ? "success" : "neutral")}
            ${renderEnvCard("Torch", env.torchInstalled ? env.torchVersion || "已安装" : "未安装", env.torchInstalled ? "success" : "warning")}
            ${renderEnvCard("yt-dlp", env.ytDlpVersion || "未安装", env.ytDlpVersion ? "success" : "warning")}
            ${renderEnvCard("本地 ASR", env.localAsrInstalled ? (env.localAsrVersion || "已安装") : "未安装", env.localAsrInstalled ? "success" : "neutral")}
            ${renderEnvCard("FFmpeg", env.ffmpegLocation ? `已安装 (${escapeHtml(env.ffmpegLocation)})` : "未安装", env.ffmpegLocation ? "success" : "warning")}
            ${renderEnvCard("Python", env.pythonVersion || "-", "neutral")}
            ${renderEnvCard("运行环境通道", env.runtimeChannel || settings.runtime_channel || "base", "neutral")}
            ${renderEnvCard("运行环境状态", env.runtimeReady === false ? "未就绪" : "已就绪", env.runtimeReady === false ? "warning" : "success")}
            ${renderEnvCard("运行环境解释器", env.runtimePython || "未检测到", env.runtimePython ? "success" : "neutral")}
          </div>
        </section>

        <!-- CUDA 操作区 -->
        <section class="cuda-control-panel">
          <div class="cuda-control-copy">
            <span class="env-panel-kicker">CUDA Control</span>
            <h3>CUDA 目标版本</h3>
            <p>选择目标运行环境后，可重新检测环境或安装对应 CUDA 支持。</p>
          </div>
          <div class="cuda-actions">
            <label class="input-row cuda-picker">
              <span class="input-label">CUDA 目标版本</span>
              <select id="cuda_variant" class="select-field">
                <option value="cu128" ${settings.cuda_variant === "cu128" ? "selected" : ""}>CUDA 12.8</option>
                <option value="cu126" ${settings.cuda_variant === "cu126" ? "selected" : ""}>CUDA 12.6</option>
                <option value="cu124" ${settings.cuda_variant === "cu124" ? "selected" : ""}>CUDA 12.4</option>
              </select>
            </label>
            <div class="settings-actions cuda-button-row">
              <button id="refresh-env" class="secondary-button" type="button">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                  <polyline points="23 4 23 10 17 10"></polyline>
                  <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"></path>
                </svg>
                重新检测
              </button>
              <button id="install-cuda" class="primary-button" type="button">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                  <line x1="12" y1="5" x2="12" y2="19"></line>
                  <line x1="5" y1="12" x2="19" y2="12"></line>
                </svg>
                安装 CUDA 支持
              </button>
            </div>
          </div>
          ${renderStatusNotice(state.cudaActionStatus, "cudaActionStatus")}
          ${state.cudaInstallOutput ? `
            <label class="input-row">
              <span class="input-label">CUDA 安装输出</span>
              <textarea class="textarea-field log-viewer" rows="8" readonly>${escapeHtml(state.cudaInstallOutput)}</textarea>
            </label>
          ` : ''}
          ${env.runtimeError ? `
            <label class="input-row">
              <span class="input-label">运行环境错误详情</span>
              <textarea class="textarea-field log-viewer" rows="8" readonly>${escapeHtml(env.runtimeError)}</textarea>
            </label>
          ` : ''}
          <label class="input-row">
            <span class="input-label">本地 ASR 运行环境</span>
            <div class="settings-actions">
              <button id="install-local-asr" class="secondary-button" type="button">安装本地 ASR</button>
            </div>
            <span class="input-caption">${env.localAsrInstalled ? `当前已安装 ${escapeHtml(env.localAsrVersion || "")}，安装后会自动切换到本地模式。` : "正式安装包默认不包含本地 ASR；安装到当前运行环境后会自动切换到本地模式。"}</span>
          </label>
          ${renderStatusNotice(state.localAsrActionStatus, "localAsrActionStatus")}
          ${state.localAsrInstallOutput ? `
            <label class="input-row">
              <span class="input-label">本地 ASR 安装输出</span>
              <textarea class="textarea-field log-viewer" rows="8" readonly>${escapeHtml(state.localAsrInstallOutput)}</textarea>
            </label>
          ` : ""}
        </section>
      </article>

      <!-- 运行配置表单 -->
      <article class="grid-card settings-form-card">
        <div class="panel-header">
          <h2>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
              <circle cx="12" cy="12" r="3"></circle>
              <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path>
            </svg>
            运行配置
          </h2>
          <p>编辑并保存后端配置</p>
        </div>

        <form id="settings-form" class="setting-form settings-sections">
          <!-- 基础运行 -->
          <section class="settings-subsection">
            <h3>基础运行</h3>
            ${renderInput("host", "监听地址 Host", settings.host || "", "text", "0.0.0.0")}
            ${renderInput("port", "监听端口", settings.port || "", "number", "3838")}
            ${renderInput("data_dir", "数据目录", settings.data_dir || "", "text", "/path/to/data")}
            ${renderInput("cache_dir", "缓存目录", settings.cache_dir || "", "text", "/path/to/cache")}
            ${renderInput("tasks_dir", "任务目录", settings.tasks_dir || "", "text", "/path/to/tasks")}
            ${renderInput("database_url", "数据库", settings.database_url || "", "text", "sqlite:///data.db")}
            ${renderSelect("runtime_channel", "运行环境通道", settings.runtime_channel || "base", buildRuntimeChannelOptions(settings))}
          </section>

          <!-- 转写模型 -->
          <section class="settings-subsection">
            <h3>转写模型</h3>
            ${renderSelect("transcription_provider", "转写方式", transcriptionProvider, [
              { value: "siliconflow", label: "硅基流动 API" },
              { value: "multimodal", label: "多模态 ASR (第三方)" },
              ...(env.localAsrInstalled ? [{ value: "local", label: "本地 ASR" }] : [])
            ])}
            ${renderSelect("device_preference", "推理设备", settings.device_preference || "cpu", [
              { value: "auto", label: "自动选择" },
              { value: "cuda", label: "CUDA (GPU)" },
              { value: "cpu", label: "CPU" }
            ])}
            ${renderSelect("compute_type", "计算精度", settings.compute_type || "int8", [
              { value: "auto", label: "自动" },
              { value: "float16", label: "Float16" },
              { value: "int8", label: "Int8" },
              { value: "float32", label: "Float32" }
            ])}
            ${renderSelect("model_mode", "模型模式", settings.model_mode || "fixed", [
              { value: "auto", label: "自动" },
              { value: "fixed", label: "固定" }
            ])}
            ${renderSelect("fixed_model", "固定模型", settings.fixed_model || "tiny", [
              { value: "tiny", label: "Tiny (最快)" },
              { value: "base", label: "Base (平衡)" },
              { value: "large-v3-turbo", label: "Large v3 Turbo (最准)" }
            ])}

            <div class="asr-config-group" data-provider="siliconflow" ${transcriptionProvider === "siliconflow" ? "" : "hidden"}>
              <div class="subsection-divider"><span>硅基流动 ASR 配置</span></div>
              ${renderInput("siliconflow_asr_base_url", "SiliconFlow Base URL", settings.siliconflow_asr_base_url || "https://api.siliconflow.cn/v1", "text", "https://api.siliconflow.cn/v1")}
              ${renderInput("siliconflow_asr_model", "SiliconFlow ASR 模型", settings.siliconflow_asr_model || "TeleAI/TeleSpeechASR", "text", "TeleAI/TeleSpeechASR")}
              ${renderSiliconFlowApiKeyInput(settings.siliconflow_asr_api_key || "")}
              ${renderInput("siliconflow_asr_chunk_duration_seconds", "切片时长（秒）", settings.siliconflow_asr_chunk_duration_seconds ?? 1800, "number", "1800")}
              <span class="input-caption" style="margin-top:-4px;margin-bottom:12px;display:block;color:var(--text-secondary);">长音频按此时长切片，默认 1800 秒（30 分钟）。</span>
              ${renderInput("siliconflow_asr_concurrency", "并发数", settings.siliconflow_asr_concurrency ?? 2, "number", "2")}
              <span class="input-caption" style="margin-top:-4px;margin-bottom:12px;display:block;color:var(--text-secondary);">同时发送的转写请求数，默认 2。</span>
            </div>

            <div class="asr-config-group" data-provider="multimodal" ${transcriptionProvider === "multimodal" ? "" : "hidden"}>
              <div class="subsection-divider"><span>多模态 ASR 配置</span></div>
              <span class="input-caption" style="margin-bottom:8px;display:block;color:var(--text-secondary);">使用多模态大模型（如 mimo-v2-omni）通过 chat/completions 接口进行语音转文字。</span>
              ${renderInput("multimodal_asr_base_url", "多模态 ASR Base URL", settings.multimodal_asr_base_url || "", "text", "https://fufu.iqach.top/v1")}
              ${renderInput("multimodal_asr_model", "多模态 ASR 模型", settings.multimodal_asr_model || "mimo-v2-omni", "text", "mimo-v2-omni")}
              ${renderInput("multimodal_asr_api_key", "多模态 ASR API Key", settings.multimodal_asr_api_key || "", "password", "如有则填写", "current-password")}
              ${renderInput("multimodal_asr_chunk_duration_seconds", "切片时长（秒）", settings.multimodal_asr_chunk_duration_seconds ?? 180, "number", "180")}
              <span class="input-caption" style="margin-top:-4px;margin-bottom:12px;display:block;color:var(--text-secondary);">长音频自动切片的每段秒数，默认 180 秒。</span>
              ${renderInput("multimodal_asr_max_retries", "切片重试次数", settings.multimodal_asr_max_retries ?? 5, "number", "5")}
              <span class="input-caption" style="margin-top:-4px;margin-bottom:12px;display:block;color:var(--text-secondary);">每段切片返回空时最多重试几次，默认 5 次。</span>
            </div>

            ${renderInput("language", "语言", settings.language || "", "text", "zh")}
          </section>

          <!-- 输出与缓存 -->
          <section class="settings-subsection">
            <h3>输出与缓存</h3>
            ${renderInput("output_dir", "输出目录", settings.output_dir || "", "text", "/path/to/output")}
            <label class="toggle-row">
              <span>保留临时音频</span>
              <input id="preserve_temp_audio" type="checkbox" ${settings.preserve_temp_audio ? "checked" : ""} />
            </label>
            <label class="toggle-row">
              <span>启用本地缓存</span>
              <input id="enable_cache" type="checkbox" ${settings.enable_cache ? "checked" : ""} />
            </label>
          </section>

          <!-- 摘要配置 -->
          <section class="settings-subsection">
            <h3>摘要配置</h3>
            ${renderSelect("summary_mode", "摘要模式", settings.summary_mode || "llm", [
              { value: "auto", label: "自动" },
              { value: "rule", label: "规则摘要" },
              { value: "llm", label: "LLM 摘要" }
            ])}
            <label class="toggle-row">
              <span>启用 LLM 摘要</span>
              <input id="llm_enabled" type="checkbox" ${settings.llm_enabled ? "checked" : ""} />
            </label>
            ${renderInput("llm_provider", "LLM Provider", settings.llm_provider || "", "text", "openai-compatible")}
            ${renderInput("llm_base_url", "LLM Base URL", settings.llm_base_url || "", "text", "https://api.openai.com/v1")}
            ${renderInput("llm_model", "LLM 模型", settings.llm_model || "", "text", "gpt-3.5-turbo")}
            ${renderInput("llm_api_key", "LLM API Key", settings.llm_api_key || "", "password", "sk-...", "current-password")}
            ${renderInput("summary_chunk_target_chars", "分块目标字符数", settings.summary_chunk_target_chars || 2200, "number", "2200")}
            ${renderInput("summary_chunk_overlap_segments", "分块重叠段数", settings.summary_chunk_overlap_segments || 2, "number", "2")}
            ${renderInput("summary_chunk_concurrency", "分块并发数", settings.summary_chunk_concurrency || 2, "number", "2")}
            ${renderInput("summary_chunk_retry_count", "单块重试次数", settings.summary_chunk_retry_count || 2, "number", "2")}
            ${renderTextarea("summary_system_prompt", "系统提示词", settings.summary_system_prompt || "", 4)}
            ${renderTextarea("summary_user_prompt_template", "用户提示词模板", settings.summary_user_prompt_template || "", 6)}
          </section>

          <!-- Twelve Labs Pegasus 视频理解（可选） -->
          <section class="settings-subsection">
            <h3>Twelve Labs Pegasus 视频理解</h3>
            <label class="toggle-row">
              <span>启用 Pegasus 视频摘要（仅本地视频，写入知识笔记）</span>
              <input id="twelvelabs_summary_enabled" type="checkbox" ${settings.twelvelabs_summary_enabled ? "checked" : ""} />
            </label>
            ${renderInput("twelvelabs_api_key", "Twelve Labs API Key", settings.twelvelabs_api_key || "", "password", "tlk_...", "current-password")}
            ${renderInput("twelvelabs_model", "Pegasus 模型", settings.twelvelabs_model || "pegasus1.5", "text", "pegasus1.5")}
            ${renderInput("twelvelabs_base_url", "API Base URL", settings.twelvelabs_base_url || "", "text", "https://api.twelvelabs.io/v1.3")}
            ${renderTextarea("twelvelabs_prompt", "Pegasus 提示词（留空用默认）", settings.twelvelabs_prompt || "", 4)}
          </section>

          <!-- 知识库 LLM -->
          <section class="settings-subsection">
            <h3>知识库 LLM</h3>
            <label class="toggle-row">
              <span>启用知识库</span>
              <input id="knowledge_enabled" type="checkbox" ${settings.knowledge_enabled ? "checked" : ""} />
            </label>
            ${renderSelect("knowledge_index_auto_rebuild", "自动维护知识库索引", settings.knowledge_index_auto_rebuild || "disabled", [
              { value: "disabled", label: "关闭自动维护" },
              { value: "on_task_completed", label: "视频生成结束后更新索引" }
            ])}
            ${renderSelect("knowledge_llm_mode", "知识库 LLM 来源", settings.knowledge_llm_mode || "same_as_main", [
              { value: "same_as_main", label: "跟随主 LLM" },
              { value: "custom", label: "使用独立配置" }
            ])}
            <label class="toggle-row">
              <span>启用独立知识库 LLM</span>
              <input id="knowledge_llm_enabled" type="checkbox" ${settings.knowledge_llm_enabled ? "checked" : ""} />
            </label>
            ${renderSelect("knowledge_llm_provider", "LLM 提供商", settings.knowledge_llm_provider || "openai-compatible", [
              { value: "openai-compatible", label: "OpenAI Compatible" },
              { value: "openai", label: "OpenAI" },
              { value: "anthropic", label: "Anthropic" },
              { value: "custom", label: "自建端点" }
            ])}
            ${renderInput("knowledge_llm_base_url", "API Base URL", settings.knowledge_llm_base_url || "", "text", "https://api.openai.com/v1")}
            ${renderInput("knowledge_llm_model", "模型名称", settings.knowledge_llm_model || "", "text", "gpt-4o-mini / claude-3-haiku")}
            ${renderInput("knowledge_llm_api_key", "API Key", settings.knowledge_llm_api_key || "", "password", "sk-...", "current-password")}
          </section>

          <!-- 保存按钮 -->
          <section class="settings-subsection settings-actions-section">
            <div class="settings-actions">
              <button class="primary-button" type="submit">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                  <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"></path>
                  <polyline points="17 21 17 13 7 13 7 21"></polyline>
                  <polyline points="7 3 7 8 15 8"></polyline>
                </svg>
                保存设置
              </button>
              ${renderStatusNotice(state.settingsSaveStatus, "settingsSaveStatus")}
            </div>
          </section>
        </form>
      </article>

      <!-- 摘要配置概览 -->
      <article class="grid-card">
        <div class="panel-header">
          <h2>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
              <polyline points="14 2 14 8 20 8"></polyline>
              <line x1="16" y1="13" x2="8" y2="13"></line>
              <line x1="16" y1="17" x2="8" y2="17"></line>
              <polyline points="10 9 9 9 8 9"></polyline>
            </svg>
            摘要配置概览
          </h2>
          <p>当前摘要相关运行参数</p>
        </div>
        <div class="setting-list">
          ${renderRow("LLM 启用", settings.llm_enabled ? "✓ 是" : "✗ 否", settings.llm_enabled ? "success" : "neutral")}
          ${renderRow("转写方式", transcriptionProvider === "siliconflow" ? "硅基流动 API" : transcriptionProvider === "multimodal" ? "多模态 ASR" : transcriptionProvider === "funasr" ? "FunASR" : "Whisper 转写", transcriptionProvider === "siliconflow" || transcriptionProvider === "multimodal" ? "success" : "neutral")}
          ${transcriptionProvider === "multimodal" ? `
          ${renderRow("多模态模型", settings.multimodal_asr_model || "-", settings.multimodal_asr_model ? "success" : "neutral")}
          ${renderRow("多模态 API Key", settings.multimodal_asr_api_key_configured ? "✓ 已配置" : "✗ 未配置", settings.multimodal_asr_api_key_configured ? "success" : "warning")}
          ` : `
          ${renderRow("SiliconFlow 模型", settings.siliconflow_asr_model || "-", settings.siliconflow_asr_model ? "success" : "neutral")}
          ${renderRow("SiliconFlow API Key", settings.siliconflow_asr_api_key_configured ? "✓ 已配置" : "✗ 未配置", settings.siliconflow_asr_api_key_configured ? "success" : "warning")}
          `}
          ${renderRow("本地 ASR", env.localAsrInstalled ? `✓ 已安装${env.localAsrVersion ? ` (${env.localAsrVersion})` : ""}` : "✗ 未安装", env.localAsrInstalled ? "success" : "neutral")}
          ${renderRow("LLM Base URL", settings.llm_base_url || "-", settings.llm_base_url ? "success" : "neutral")}
          ${renderRow("LLM 模型", settings.llm_model || "-", settings.llm_model ? "success" : "neutral")}
          ${renderRow("运行环境通道", settings.runtime_channel || "base", "neutral")}
          ${renderRow("摘要模式", settings.summary_mode || "-", "neutral")}
          ${renderRow("分块大小", String(settings.summary_chunk_target_chars || "-"), "neutral")}
          ${renderRow("分块并发", String(settings.summary_chunk_concurrency || "-"), "neutral")}
          ${renderRow("Pegasus 视频理解", settings.twelvelabs_summary_enabled ? "✓ 已启用" : "✗ 关闭", settings.twelvelabs_summary_enabled ? "success" : "neutral")}
          ${renderRow("Twelve Labs API Key", settings.twelvelabs_api_key_configured ? "✓ 已配置" : "✗ 未配置", settings.twelvelabs_api_key_configured ? "success" : "warning")}
          ${renderRow("API Key", settings.llm_api_key_configured ? "✓ 已配置" : "✗ 未配置", settings.llm_api_key_configured ? "success" : "warning")}
        </div>
      </article>

      <!-- 后端信息 -->
      <article class="grid-card">
        <div class="panel-header">
          <h2>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
              <rect x="2" y="3" width="20" height="14" rx="2" ry="2"></rect>
              <line x1="8" y1="21" x2="16" y2="21"></line>
              <line x1="12" y1="17" x2="12" y2="21"></line>
            </svg>
            后端信息
          </h2>
          <p>系统运行详情</p>
        </div>
        <div class="setting-list">
          ${renderRow("服务名", info.application?.name || "-")}
          ${renderRow("版本", info.application?.version || "-")}
          ${renderRow("任务状态", (info.taskModel?.statuses || []).join(", ") || "-")}
          ${renderRow("日志文件", info.service?.log_file || state.logPath || "-", "neutral")}
        </div>
      </article>

      <!-- 日志与控制 -->
      <article class="grid-card settings-wide">
        <div class="panel-header">
          <h2>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
              <polyline points="14 2 14 8 20 8"></polyline>
              <line x1="16" y1="13" x2="8" y2="13"></line>
              <line x1="16" y1="17" x2="8" y2="17"></line>
            </svg>
            日志与控制
          </h2>
          <p>查看后端日志，并直接关闭当前后端服务</p>
        </div>
        <div class="settings-actions">
          <button id="refresh-logs" class="secondary-button" type="button">刷新日志</button>
          <button id="shutdown-service" class="secondary-button danger-button" type="button">关闭后端服务</button>
        </div>
        ${renderStatusNotice(state.serviceActionStatus, "serviceActionStatus")}
        <label class="input-row">
          <span class="input-label">当前日志文件</span>
          <input class="input-field" value="${escapeHtml(state.logPath || info.service?.log_file || '')}" readonly />
        </label>
        <label class="input-row">
          <span class="input-label">最近日志</span>
          <textarea class="textarea-field log-viewer" rows="16" readonly>${escapeHtml(state.logOutput || "")}</textarea>
        </label>
      </article>

      <!-- 关于 -->
      <article class="grid-card about-card">
        <div class="panel-header">
          <h2>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
              <circle cx="12" cy="12" r="10"></circle>
              <line x1="12" y1="16" x2="12" y2="12"></line>
              <line x1="12" y1="8" x2="12.01" y2="8"></line>
            </svg>
            关于
          </h2>
          <p>项目信息</p>
        </div>
        <div class="setting-list">
          ${renderRow("当前形态", "开发态可用 + 本地 Web UI", "info")}
          ${renderRow("下一目标", "桌面 UI + 可执行后端", "info")}
          ${renderRow("最终目标", "MSI / DMG / AppImage", "info")}
        </div>
      </article>
    </section>
  `;
}

function renderEnvCard(label, value, type) {
  const icons = {
    cpu: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="4" y="4" width="16" height="16" rx="2" ry="2"></rect><rect x="9" y="9" width="6" height="6"></rect><line x1="9" y1="1" x2="9" y2="4"></line><line x1="15" y1="1" x2="15" y2="4"></line><line x1="9" y1="20" x2="9" y2="23"></line><line x1="15" y1="20" x2="15" y2="23"></line><line x1="20" y1="9" x2="23" y2="9"></line><line x1="20" y1="14" x2="23" y2="14"></line><line x1="1" y1="9" x2="4" y2="9"></line><line x1="1" y1="14" x2="4" y2="14"></line></svg>`,
    model: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"></path></svg>`,
    success: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true"><polyline points="20 6 9 17 4 12"></polyline></svg>`,
    warning: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path><line x1="12" y1="9" x2="12" y2="13"></line><line x1="12" y1="17" x2="12.01" y2="17"></line></svg>`,
    neutral: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="16" x2="12" y2="12"></line><line x1="12" y1="8" x2="12.01" y2="8"></line></svg>`,
  };

  const icon = icons[type] || icons.neutral;

  return `
    <div class="env-card-item ${type}">
      <div class="env-card-icon">${icon}</div>
      <div class="env-card-content">
        <span class="env-card-label">${escapeHtml(label)}</span>
        <span class="env-card-value">${escapeHtml(value)}</span>
      </div>
    </div>
  `;
}

function renderRow(label, value, type = "neutral") {
  const typeClass = type === "success" ? "row-success" : type === "warning" ? "row-warning" : type === "info" ? "row-info" : "";

  return `
    <div class="setting-row ${typeClass}">
      <span class="setting-label">${escapeHtml(label)}</span>
      <span class="setting-value">${escapeHtml(value)}</span>
    </div>
  `;
}

function renderInput(id, label, value, type = "text", placeholder = "", autocomplete = "") {
  return `
    <label class="input-row">
      <span class="input-label">${escapeHtml(label)}</span>
      <input 
        id="${escapeHtml(id)}" 
        type="${escapeHtml(type)}" 
        value="${escapeHtml(value)}" 
        placeholder="${escapeHtml(placeholder)}"
        ${autocomplete ? `autocomplete="${escapeHtml(autocomplete)}"` : ""}
        class="input-field"
      />
    </label>
  `;
}

function renderSiliconFlowApiKeyInput(value) {
  return `
    <div class="input-row api-key-help-row">
      <label class="input-row" for="siliconflow_asr_api_key">
        <span class="input-label">SiliconFlow API Key</span>
        <input
          id="siliconflow_asr_api_key"
          type="password"
          value="${escapeHtml(value)}"
          placeholder="sk-..."
          autocomplete="current-password"
          class="input-field"
        />
      </label>
      <div class="api-key-help-inline">
        <span class="api-key-help-copy">调用云端语音识别必须提供 API Key。</span>
        <div class="api-key-help-popover">
          <span class="api-key-help-link" role="button" tabindex="0">如何获得 API？</span>
          <div class="api-key-help-popover-card" id="siliconflow-api-help">
            <strong>获取步骤</strong>
            <ol>
              <li>
                注册 SiliconFlow 账号：
                <a href="https://cloud.siliconflow.cn/i/d8SF8w5Z" target="_blank" rel="noreferrer">点此注册</a>
              </li>
              <li>
                新建 API Key：
                <a href="https://cloud.siliconflow.cn/me/account/ak" target="_blank" rel="noreferrer">前往创建</a>
              </li>
            </ol>
          </div>
        </div>
      </div>
    </div>
  `;
}

function renderSelect(id, label, selectedValue, options) {
  return `
    <label class="input-row">
      <span class="input-label">${escapeHtml(label)}</span>
      <select id="${escapeHtml(id)}" class="select-field">
        ${options
          .map((opt) => `<option value="${escapeHtml(opt.value)}" ${opt.value === selectedValue ? "selected" : ""}>${escapeHtml(opt.label)}</option>`)
          .join("")}
      </select>
    </label>
  `;
}

function renderTextarea(id, label, value, rows) {
  return `
    <label class="input-row">
      <span class="input-label">${escapeHtml(label)}</span>
      <textarea 
        id="${escapeHtml(id)}" 
        rows="${rows}"
        class="textarea-field"
      >${escapeHtml(value)}</textarea>
    </label>
  `;
}

function buildRuntimeChannelOptions(settings) {
  const options = [
    { value: "base", label: "base (CPU 基础运行环境)" },
  ];
  for (const value of ["gpu-cu124", "gpu-cu126", "gpu-cu128"]) {
    options.push({
      value,
      label: `${value}${settings.runtime_channel === value ? " (当前)" : ""}`,
    });
  }
  return options;
}
