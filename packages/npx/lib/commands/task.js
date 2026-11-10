"use strict";

/**
 * summarize / brief / transcribe: probe or upload the source, create one task
 * per selected page, and (unless options.wait is false) wait for completion.
 */

const { existsSync } = require("node:fs");

const {
  ApiError,
  createVideoTask,
  findReusableTask,
  getTask,
  getTaskProgress,
  probeVideo,
  uploadLocalMedia,
} = require("../api");
const { UsageError } = require("../args");
const { connect } = require("./connection");
const { isAgentWaitingStatus, isFailedStatus, isTerminalStatus } = require("../status");
const { toReporter } = require("./reporter");

const POLL_INTERVAL_MS = 2000;

/** Whether the source is an http(s) URL rather than a local file path. */
function isUrlSource(source) {
  return /^https?:\/\//i.test(source);
}

/** Resolve after `ms` milliseconds. */
function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** The `?p=` page number in a video URL, or null. */
function extractUrlPageParam(source) {
  try {
    const parsed = new URL(source);
    const raw = parsed.searchParams.get("p");
    if (!raw) {
      return null;
    }
    const page = Number.parseInt(raw, 10);
    return Number.isFinite(page) && page > 0 ? page : null;
  } catch {
    return null;
  }
}

/** Page numbers to create tasks for: all pages, the `--page` one, the URL's `?p=`, or the whole video (null). */
function selectPages(source, video, options, log) {
  const pages = video.pages || [];
  const pageNumbers = [];
  if (options.allPages) {
    if (!pages.length) {
      log("当前视频没有分 P，按单个任务处理。");
      pageNumbers.push(null);
    } else {
      for (const page of pages) {
        pageNumbers.push(page.page);
      }
    }
  } else if (options.page !== null && options.page !== undefined) {
    if (pages.length && !pages.some((page) => page.page === options.page)) {
      throw new UsageError(
        `分 P ${options.page} 不存在。可选分 P：${pages.map((page) => page.page).join(", ")}`,
      );
    }
    pageNumbers.push(options.page);
  } else {
    const urlPage = isUrlSource(source) ? extractUrlPageParam(source) : null;
    if (urlPage !== null && pages.length && !pages.some((page) => page.page === urlPage)) {
      throw new UsageError(`URL 指定的分 P ${urlPage} 不存在。可选分 P：${pages.map((page) => page.page).join(", ")}`);
    }
    pageNumbers.push(urlPage);
  }
  return pageNumbers;
}

/** Create (or reuse) one task per selected page; tasks from one command share a batch_id. */
async function createTasks(source, options, { baseUrl, token }, log) {
  let video;
  if (isUrlSource(source)) {
    log(`探测视频信息：${source}`);
    video = (await probeVideo(baseUrl, token, source)).video;
  } else {
    if (!existsSync(source)) {
      throw new UsageError(`本地文件不存在：${source}`);
    }
    log(`上传本地媒体：${source}`);
    video = (await uploadLocalMedia(baseUrl, token, source)).video;
  }

  const created = [];
  // Pages created by this command share one batch (the first created task's).
  let batchId = null;
  for (const pageNumber of selectPages(source, video, options, log)) {
    const taskOptions = {
      pageNumber,
      visualNoteMode: options.visualNote,
      promptPresetId: options.promptPreset,
      summaryScope: options.summaryScope,
      language: options.language,
      extraInstructions: options.instructions,
      engine: options.engine,
    };
    const routing = {
      agentCapabilities: options.agentCapabilities,
      // Only a caller that declares itself an Agent can get the agent engine (design F2 7.1).
      agentCaller: Boolean(options.agentName) || options.engine === "agent",
    };
    const label = `${video.title || source}${pageNumber ? `（P${pageNumber}）` : ""}`;
    const reusable = options.force ? null : await findReusableTask(baseUrl, token, video.video_id, taskOptions);
    if (reusable) {
      const task = { ...reusable.task, reused: true, reuse_match: reusable.match };
      if (reusable.batch_id) {
        task.batch_id = reusable.batch_id;
      }
      log(
        reusable.match === "completed"
          ? `复用已有任务：${task.task_id}（已完成）${label}`
          : `接上进行中的任务：${task.task_id}（${task.status}）${label}`,
      );
      created.push(task);
      continue;
    }
    log(`提交任务：${label}`);
    const task = await createVideoTask(baseUrl, token, video.video_id, { ...taskOptions, ...routing, batchId });
    batchId = batchId || task.batch_id || null;
    created.push({ ...task, reused: false });
    log(`任务已创建：${task.task_id}`);
  }
  return created;
}

/**
 * Poll until every task is terminal and return their final details.
 *
 * Unbounded (CLI): waits up to options.timeoutSeconds, then throws.
 * Bounded (options.waitSeconds is a number, MCP): waits at most that long and
 * then resolves { timedOut: true, tasks: latest progress snapshots } instead
 * of throwing; a terminal outcome resolves { timedOut: false, tasks }.
 * Unbounded calls resolve the plain task array.
 *
 * Waiting also ends as soon as any task reaches agent_summarizing: nothing
 * happens until an Agent claims the work, so the caller gets the details
 * (that task still agent_summarizing, the others as they are) and can start
 * the `agent next` loop.
 */
async function waitForTasks(taskIds, options, { baseUrl, token }, reporter) {
  const bounded = typeof options.waitSeconds === "number";
  const deadline = Date.now() + (bounded ? options.waitSeconds : options.timeoutSeconds) * 1000;
  const seenProgress = new Set();
  const statuses = new Map(taskIds.map((id) => [id, "queued"]));
  const snapshots = new Map();

  while (true) {
    let allTerminal = taskIds.length > 0;
    for (const taskId of taskIds) {
      if (isTerminalStatus(statuses.get(taskId))) {
        continue;
      }
      let progress;
      try {
        progress = await getTaskProgress(baseUrl, token, taskId);
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) {
          throw new Error(`任务不存在：${taskId}`);
        }
        // Fall back to the detail endpoint if progress is unavailable.
        progress = await getTask(baseUrl, token, taskId);
      }
      statuses.set(taskId, progress.status);
      snapshots.set(taskId, {
        task_id: taskId,
        status: progress.status,
        progress: progress.progress ?? null,
        latest_stage: progress.latest_stage || "",
        latest_message: progress.latest_message || "",
      });
      if (isTerminalStatus(progress.status)) {
        continue;
      }
      allTerminal = false;
      const marker = `${taskId}:${progress.status}:${progress.progress ?? ""}:${progress.latest_stage ?? ""}`;
      if (!seenProgress.has(marker)) {
        seenProgress.add(marker);
        reporter.progress({
          taskId,
          status: progress.status,
          progress: progress.progress,
          stage: progress.latest_stage || "",
          message: progress.latest_message || "",
        });
      }
    }

    const needsAgent = taskIds.some((id) => isAgentWaitingStatus(statuses.get(id)));
    if (allTerminal || needsAgent) {
      const results = [];
      for (const taskId of taskIds) {
        results.push(await getTask(baseUrl, token, taskId));
      }
      return bounded ? { timedOut: false, tasks: results } : results;
    }
    if (bounded && Date.now() >= deadline) {
      return { timedOut: true, tasks: taskIds.map((id) => snapshots.get(id)) };
    }
    if (Date.now() > deadline) {
      throw new Error(
        `任务在 ${options.timeoutSeconds}s 内未完成。可运行 bilisum status ${taskIds.join(" ")} 继续查看进度。`,
      );
    }
    await sleep(bounded ? Math.max(0, Math.min(POLL_INTERVAL_MS, deadline - Date.now())) : POLL_INTERVAL_MS);
  }
}

/**
 * Run a task command. `options` is the shape returned by parseTaskCommandArgs
 * plus `brief` and `summaryScope` (see resolveSummaryScope in lib/args.js). Returns:
 *   { waited: false, tasks }            options.wait === false (created or reused tasks)
 *   { waited: true, tasks, failed, awaitingAgent }
 *                                       final task details; failed = failed/cancelled subset;
 *                                       awaitingAgent = tasks in agent_summarizing (waiting
 *                                       stopped for the Agent, see waitForTasks)
 *   { waited: true, timedOut: true, tasks, failed: [] }
 *                                       only with options.waitSeconds (bounded wait, see
 *                                       waitForTasks): created tasks overlaid with
 *                                       their latest progress snapshot
 * Every task carries `reused` (true when an existing task answered the request,
 * see GET /videos/:id/reusable-task; options.force skips that lookup).
 * options.language / instructions / engine / agentCapabilities are sent when set
 * (C2 / F2 flags); options.agentName or engine "agent" declares an Agent caller.
 */
async function runTaskCommand(source, options, reporter) {
  const report = toReporter(reporter);
  // Validate local inputs before any service work (no pointless auto-start).
  if (!isUrlSource(source) && !existsSync(source)) {
    throw new UsageError(`本地文件不存在：${source}`);
  }
  const connection = await connect(options, report);
  const tasks = await createTasks(source, options, connection, report.log);

  if (!options.wait) {
    for (const task of tasks) {
      report.log(`任务已创建（不等待）：${task.task_id}`);
    }
    return { waited: false, tasks };
  }

  const reusedById = new Map(tasks.map((task) => [task.task_id, task]));
  const withOrigin = (task) => {
    const origin = reusedById.get(task.task_id) || {};
    return { ...task, reused: origin.reused === true, ...(origin.batch_id ? { batch_id: origin.batch_id } : {}) };
  };
  const waited = await waitForTasks(tasks.map((task) => task.task_id), options, connection, report);
  if (waited.timedOut) {
    const latest = waited.tasks.map((snapshot, index) => ({ ...tasks[index], ...snapshot }));
    return { waited: true, timedOut: true, tasks: latest.map(withOrigin), failed: [] };
  }
  const completed = (Array.isArray(waited) ? waited : waited.tasks).map(withOrigin);
  const failed = completed.filter((task) => isFailedStatus(task.status));
  for (const task of failed) {
    report.log(`[${task.task_id}] ${task.status}：${task.error_message || "未知错误"}`);
  }
  const awaitingAgent = completed.filter((task) => isAgentWaitingStatus(task.status));
  return { waited: true, tasks: completed, failed, awaitingAgent };
}

module.exports = { runTaskCommand, waitForTasks };
