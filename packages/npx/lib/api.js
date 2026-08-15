"use strict";

/**
 * Minimal HTTP client for the BiliSum service API.
 *
 * Zero-dependency: uses Node's global fetch (Node >= 18). All requests are
 * authenticated with a Bearer token when one is available.
 */

const { createReadStream } = require("node:fs");
const { extname } = require("node:path");

const DEFAULT_TIMEOUT_MS = 30_000;

class ApiError extends Error {
  constructor(message, status, detail) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

function extractDetail(payload) {
  if (payload == null) {
    return "";
  }
  if (typeof payload === "string") {
    return payload;
  }
  if (typeof payload.detail === "string") {
    return payload.detail;
  }
  if (payload.detail && typeof payload.detail === "object") {
    return JSON.stringify(payload.detail);
  }
  return JSON.stringify(payload);
}

async function apiRequest(baseUrl, path, {
  method = "GET",
  token = null,
  json = undefined,
  body = undefined,
  headers = {},
  timeoutMs = DEFAULT_TIMEOUT_MS,
} = {}) {
  const url = `${baseUrl}${path}`;
  const requestHeaders = { ...headers };
  if (token) {
    requestHeaders.Authorization = `Bearer ${token}`;
  }
  if (json !== undefined) {
    requestHeaders["Content-Type"] = "application/json";
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let response;
  try {
    response = await fetch(url, {
      method,
      headers: requestHeaders,
      body: json !== undefined ? JSON.stringify(json) : body,
      signal: controller.signal,
    });
  } catch (error) {
    if (error && error.name === "AbortError") {
      throw new ApiError(`请求超时（${timeoutMs}ms）：${path}`, 0, "");
    }
    throw new ApiError(`无法连接 BiliSum 服务（${url}）：${error.message}`, 0, "");
  } finally {
    clearTimeout(timer);
  }

  const text = await response.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }
  if (!response.ok) {
    throw new ApiError(
      `BiliSum API ${response.status} ${response.statusText}`,
      response.status,
      extractDetail(payload),
    );
  }
  return payload;
}

function baseUrlFor(host, port) {
  return `http://${host}:${port}`;
}

function health(baseUrl, { timeoutMs = 3000 } = {}) {
  return apiRequest(baseUrl, "/health", { timeoutMs });
}

function probeVideo(baseUrl, token, url, { forceRefresh = false, timeoutMs = 120_000 } = {}) {
  return apiRequest(baseUrl, "/api/v1/videos/probe", {
    method: "POST",
    token,
    json: { url, force_refresh: forceRefresh },
    timeoutMs,
  });
}

function uploadLocalMedia(baseUrl, token, filePath, { timeoutMs = 30 * 60_000 } = {}) {
  const fileName = filePath.split(/[\\/]/).pop() || "local.mp4";
  const contentType = extname(fileName).toLowerCase() === ".mp3" ? "audio/mpeg" : "application/octet-stream";
  const encodedName = encodeURIComponent(fileName);
  return apiRequest(baseUrl, `/api/v1/videos/upload?filename=${encodedName}`, {
    method: "POST",
    token,
    body: createReadStream(filePath),
    headers: { "Content-Type": contentType },
    timeoutMs,
  });
}

function createVideoTask(baseUrl, token, videoId, { pageNumber, visualNoteMode, promptPresetId, timeoutMs = 30_000 } = {}) {
  const payload = {};
  if (pageNumber !== undefined && pageNumber !== null) {
    payload.page_number = pageNumber;
  }
  if (visualNoteMode) {
    payload.visual_note_mode = visualNoteMode;
  }
  if (promptPresetId) {
    payload.prompt_preset_id = promptPresetId;
  }
  return apiRequest(baseUrl, `/api/v1/videos/${encodeURIComponent(videoId)}/tasks`, {
    method: "POST",
    token,
    json: payload,
    timeoutMs,
  });
}

function getTask(baseUrl, token, taskId, { timeoutMs = 30_000 } = {}) {
  return apiRequest(baseUrl, `/api/v1/tasks/${encodeURIComponent(taskId)}`, {
    token,
    timeoutMs,
  });
}

function listTasks(baseUrl, token, { timeoutMs = 30_000 } = {}) {
  return apiRequest(baseUrl, "/api/v1/tasks", { token, timeoutMs });
}

function deleteTask(baseUrl, token, taskId, { timeoutMs = 30_000 } = {}) {
  return apiRequest(baseUrl, `/api/v1/tasks/${encodeURIComponent(taskId)}`, {
    method: "DELETE",
    token,
    timeoutMs,
  });
}

module.exports = {
  ApiError,
  apiRequest,
  baseUrlFor,
  health,
  probeVideo,
  uploadLocalMedia,
  createVideoTask,
  getTask,
  listTasks,
  deleteTask,
};
