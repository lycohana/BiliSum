import assert from "node:assert/strict";

function run(name: string, fn: () => Promise<void> | void) {
  Promise.resolve(fn()).then(() => {
    console.log(`ok - ${name}`);
  }).catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
}

run("calls markdown and transcript export APIs", async () => {
  const originalFetch = globalThis.fetch;
  const originalWindow = globalThis.window;
  const requests: Array<{ url: string; options?: RequestInit }> = [];

  globalThis.window = { location: { origin: "http://127.0.0.1:3838" } } as typeof window;
  globalThis.fetch = (async (url: string | URL | Request, options?: RequestInit) => {
    requests.push({ url: String(url), options });
    const rawUrl = String(url);
    if (rawUrl.endsWith("/prompts/presets") && options?.method !== "POST") {
      return new Response(JSON.stringify([{ id: "general", name: "通用", system_prompt: "s", user_prompt_template: "u", auto_match_keywords: [], is_builtin: true }]), { status: 200 });
    }
    if (rawUrl.endsWith("/prompts/match")) {
      return new Response(JSON.stringify({ preset: { id: "general", name: "通用", system_prompt: "s", user_prompt_template: "u", auto_match_keywords: [], is_builtin: true }, match_type: "fallback", confidence: 0.4 }), { status: 200 });
    }
    if (rawUrl.endsWith("/videos/upload/batch")) {
      return new Response(JSON.stringify([]), { status: 200 });
    }
    if (rawUrl.endsWith("/videos/library")) {
      return new Response(JSON.stringify({ videos: [], folders: [], preferences: { new_video_position: "front" } }), { status: 200 });
    }
    if (rawUrl.endsWith("/videos/library/preferences")) {
      return new Response(String(options?.body || "{}"), { status: 200 });
    }
    if (rawUrl.endsWith("/videos/folders")) {
      return new Response(JSON.stringify({ folder_id: "folder-1", name: "课程", parent_id: null, position: 0, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" }), { status: 200 });
    }
    if (rawUrl.endsWith("/videos/folders/child")) {
      return new Response(JSON.stringify({ folder_id: "child", name: "子级", parent_id: null, position: 500, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" }), { status: 200 });
    }
    if (rawUrl.endsWith("/videos/reorder")) {
      return new Response(JSON.stringify([]), { status: 200 });
    }
    if (rawUrl.endsWith("/videos/video-1/move") || rawUrl.endsWith("/videos/video-1/pin")) {
      return new Response(JSON.stringify({ video_id: "video-1", canonical_id: "video-1", platform: "bilibili", title: "Video", source_url: "", cover_url: "", has_result: false, is_favorite: false, global_order: 0, folder_order: 0, global_pinned: false, folder_pinned: false, pages: [], created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" }), { status: 200 });
    }
    const isTranscript = String(url).includes("/exports/transcript");
    return new Response(JSON.stringify({
      task_id: "task-1",
      target_format: isTranscript ? "transcript" : "obsidian",
      path: isTranscript ? "C:/vault/transcript.txt" : "C:/vault/note.md",
      directory: "C:/vault",
      file_name: isTranscript ? "transcript.txt" : "note.md",
      overwritten: false,
      artifact_key: isTranscript ? "transcript_export_path" : "obsidian_note_path",
    }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;

  const { api } = await import("../src/api.ts");
  const response = await api.exportTaskMarkdown("task-1", {
    target: "obsidian",
    include_transcript: true,
    output_dir: "C:/picked",
  });

  assert.equal(response.target_format, "obsidian");
  assert.equal(requests[0]?.url, "/api/v1/tasks/task-1/exports/markdown");
  assert.equal(requests[0]?.options?.method, "POST");
  const markdownBody = JSON.parse(String(requests[0]?.options?.body));
  assert.equal(markdownBody.target, "obsidian");
  assert.equal(markdownBody.include_transcript, true);
  assert.equal(markdownBody.output_dir, "C:/picked");

  const transcriptResponse = await api.exportTaskTranscript("task-1", { output_dir: "C:/picked" });

  assert.equal(transcriptResponse.target_format, "transcript");
  assert.equal(requests[1]?.url, "/api/v1/tasks/task-1/exports/transcript");
  assert.equal(requests[1]?.options?.method, "POST");
  const transcriptBody = JSON.parse(String(requests[1]?.options?.body));
  assert.equal(transcriptBody.output_dir, "C:/picked");

  await api.listPromptPresets();
  await api.matchPrompt("pytest 教程");
  await api.uploadBatchVideos([new File(["video"], "demo.mp4", { type: "video/mp4" })]);
  await api.getVideoLibrary();
  await api.updateVideoLibraryPreferences({ new_video_position: "back" });
  await api.createVideoFolder({ name: "课程", parent_id: null });
  await api.updateVideoFolder("child", { parent_id: null, position: 500 });
  await api.reorderVideos({ video_ids: ["video-1"], folder_id: "__global__" });
  await api.moveVideoToFolder("video-1", { folder_id: "folder-1" });
  await api.setVideoPin("video-1", { global_pinned: true });

  assert.equal(requests[2]?.url, "/api/v1/prompts/presets");
  assert.equal(requests[3]?.url, "/api/v1/prompts/match");
  assert.equal(requests[3]?.options?.method, "POST");
  assert.deepEqual(JSON.parse(String(requests[3]?.options?.body)), { title: "pytest 教程" });
  assert.equal(requests[4]?.url, "/api/v1/videos/upload/batch");
  assert.equal(requests[4]?.options?.method, "POST");
  assert.ok(requests[4]?.options?.body instanceof FormData);
  assert.equal(requests[5]?.url, "/api/v1/videos/library");
  assert.equal(requests[6]?.url, "/api/v1/videos/library/preferences");
  assert.deepEqual(JSON.parse(String(requests[6]?.options?.body)), { new_video_position: "back" });
  assert.equal(requests[7]?.url, "/api/v1/videos/folders");
  assert.equal(requests[8]?.url, "/api/v1/videos/folders/child");
  assert.equal(requests[8]?.options?.method, "PATCH");
  assert.deepEqual(JSON.parse(String(requests[8]?.options?.body)), { parent_id: null, position: 500 });
  assert.deepEqual(JSON.parse(String(requests[9]?.options?.body)), { video_ids: ["video-1"], folder_id: "__global__" });
  assert.equal(requests[10]?.url, "/api/v1/videos/video-1/move");
  assert.equal(requests[11]?.url, "/api/v1/videos/video-1/pin");

  globalThis.fetch = originalFetch;
  globalThis.window = originalWindow;
});
