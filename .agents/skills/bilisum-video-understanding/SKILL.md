---
name: bilisum-video-understanding
description: Use the BiliSum CLI to understand videos from Bilibili, YouTube, or local files. Trigger when an agent needs video summaries, concise briefs, transcripts, chapters, multi-page processing, task-status recovery, or answers grounded in a video's transcript and generated notes.
---

# BiliSum Video Understanding

Use BiliSum as the video-understanding backend and return a grounded, structured answer in the user's language. Keep the CLI's machine-readable output separate from progress messages and do not invent fields that the CLI did not return.

## Language and output boundaries

- When the user speaks Chinese, write the final summary, error explanation, and next steps in Chinese. Titles, commands, field names, and raw error text may remain unchanged.
- Do not claim that processing has started, content has been extracted, or a video has been searched until the CLI returns parseable JSON. Progress updates must make it clear when the task is not yet successful.
- `--quiet` only suppresses progress output; it does not mean the task succeeded. Always inspect the exit code, stderr, and stdout separately.
- When BiliSum fails, do not switch to web search, browser scraping, or uploading a local file to another service. Ask before using an alternative only when the user explicitly requests one.

## Prepare the CLI

- Prefer the installed `bilisum` command.
- First run `bilisum --version` and use the exact executable name. In PowerShell, do not write `$bilisum`: `$` starts a variable and is not part of the command.
- If the command is unavailable or too old for the requested subcommand/flag, use `npx --yes bilisum@latest` consistently for the whole request; do not mix an old global CLI with a newer one.
- Run `bilisum doctor` when setup is unclear, but treat a successful doctor check as an environment check only, not proof that authentication or video processing works.
- If no desktop installation is available, initialize the standalone environment with `bilisum env setup`, then use `--environment cli` for the request. Python 3.12 is required for that first setup.
- When the service reports missing LLM/ASR configuration, ask the user to configure it through `bilisum --setting`. Never ask the user to paste secrets into the conversation.
- If the CLI reports a missing access token, stop the video workflow and give one concrete recovery path: start the desktop app, pass `--environment cli` after standalone setup, or let the user provide a token through their local environment. Do not repeat the same command with only a different `--data` path.
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

1. Run the selected command with `--format json --quiet`, capture the exit code, stderr, and stdout separately, and do not merge the streams. Progress belongs on stderr and the JSON payload belongs on stdout.
2. If the exit code is non-zero, treat the request as failed even when stdout contains help text or partial output. Report the actionable stderr/error message and stop; do not parse help text as a result.
3. Require stdout to be valid JSON before describing a task or video as processed. Parse the single-task object and use `status`, `task_id`, `title`, `video_id`, `error_code`, and `error_message` for task state and provenance.
4. When `result` exists, use these fields as available:
   - `knowledge_note_markdown`: the most complete generated note.
   - `overview`: the concise overall explanation.
   - `key_points`: the main takeaways.
   - `timeline`: chapter titles, summaries, and seconds-based start times; render timestamps as `[mm:ss]` when useful.
   - `transcript_text`: the transcript for verbatim grounding and follow-up questions.
5. Answer from the returned content. Preserve uncertainty, distinguish transcript evidence from model-generated synthesis, and state when a requested field is unavailable.
6. Include the source title and useful timestamps when presenting a result. Do not dump the entire transcript unless requested.

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

- For a missing access token, return a short Chinese setup message and stop. Recommended recovery examples are `bilisum --setting` for service keys, `bilisum env setup` followed by `--environment cli` for a standalone environment, or `--token`/`VIDEO_SUM_ACCESS_TOKEN` when the user has already configured a local secret.
- If the command times out, report the `task_id` and query it with `bilisum status <task-id> --json`; do not start a duplicate task without checking status first.
- If `status` is `failed` or `cancelled`, report the CLI error fields and give the next actionable setup/configuration step.
- If JSON parsing fails, preserve stderr separately, retry at most once with the same executable and `--quiet`, and report the raw command failure rather than guessing. If the output is a CLI help page or an authentication error, do not retry it as a video task.
- If a multi-page request contains mixed task states, summarize completed pages and list unfinished or failed pages separately.
- Treat missing transcript, empty notes, or absent chapters as unavailable data, not as evidence that the video contains nothing.
