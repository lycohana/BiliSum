---
name: bilisum-video-understanding
description: Use the BiliSum CLI to understand videos from Bilibili, YouTube, or local files. Trigger when an agent needs video summaries, concise briefs, transcripts, chapters, multi-page processing, task-status recovery, or answers grounded in a video's transcript and generated notes.
---

# BiliSum Video Understanding

Use BiliSum as the video-understanding backend and return a grounded, structured answer in the user's language. Keep the CLI's machine-readable output separate from progress messages and do not invent fields that the CLI did not return.

## Prepare the CLI

- Prefer the installed `bilisum` command.
- If it is unavailable, use `npx --yes bilisum` for the command in the current request.
- Run `bilisum doctor` when setup is unclear. If no desktop installation is available, initialize the standalone environment with `bilisum env setup`; Python 3.12 is required for that first setup.
- Ask the user to configure keys through `bilisum --setting` when the service reports missing LLM/ASR configuration. Never ask the user to paste secrets into the conversation.
- Do not open a browser or upload a local file outside BiliSum's CLI workflow unless the user explicitly asks.

## Choose the command

| User intent | Command |
| --- | --- |
| Full understanding, notes, or later question answering | `bilisum summarize <source> --format json --quiet` |
| Short overview and key points | `bilisum brief <source> --format json --quiet` |
| Full transcript | `bilisum transcribe <source> --format json --quiet` |
| One page of a multi-page video | Add `--page <n>` |
| All pages of a series | Add `--all-pages` |
| Continue a timed-out or asynchronous task | `bilisum status <task-id> --json` |
| Inspect recent tasks | `bilisum tasks --json` |

Use one source per command. Quote URLs and local paths so shell characters are not interpreted. Prefer waiting for completion unless the user explicitly asks for an asynchronous task.

## Run and interpret results

1. Run the selected command with `--format json --quiet`; progress belongs on stderr and the JSON payload belongs on stdout.
2. Parse the single-task object. Use `status`, `task_id`, `title`, `video_id`, `error_code`, and `error_message` for task state and provenance.
3. When `result` exists, use these fields as available:
   - `knowledge_note_markdown`: the most complete generated note.
   - `overview`: the concise overall explanation.
   - `key_points`: the main takeaways.
   - `timeline`: chapter titles, summaries, and seconds-based start times; render timestamps as `[mm:ss]` when useful.
   - `transcript_text`: the transcript for verbatim grounding and follow-up questions.
4. Answer from the returned content. Preserve uncertainty, distinguish transcript evidence from model-generated synthesis, and state when a requested field is unavailable.
5. Include the source title and useful timestamps when presenting a result. Do not dump the entire transcript unless requested.

For `--format json`, a completed task has the stable shape below; a multi-page request may return `{ "tasks": [...] }`:

```json
{
  "task_id": "...",
  "video_id": "...",
  "title": "...",
  "status": "completed",
  "result": {
    "overview": "...",
    "key_points": ["..."],
    "timeline": [{"title": "...", "start": 0, "summary": "..."}],
    "knowledge_note_markdown": "...",
    "transcript_text": "..."
  }
}
```

## Handle failures and long tasks

- If the command times out, report the `task_id` and query it with `bilisum status <task-id> --json`; do not start a duplicate task without checking status first.
- If `status` is `failed` or `cancelled`, report the CLI error fields and give the next actionable setup/configuration step.
- If JSON parsing fails, preserve stderr separately, retry once with `--quiet`, and report the raw command failure rather than guessing.
- If a multi-page request contains mixed task states, summarize completed pages and list unfinished or failed pages separately.
- Treat missing transcript, empty notes, or absent chapters as unavailable data, not as evidence that the video contains nothing.
