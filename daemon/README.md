# TrailMem ingest daemon

Watches `~/.claude/projects/**/*.jsonl` and extracts episodes into a SQLite
TrailMem database. Replaces the CC-specific UserPromptSubmit hook approach
with a tool-agnostic background process — works with Claude Code, Cursor,
Cline, Codex, anything that produces jsonl conversation logs in the watched
directory.

## Install

```
pip install -r requirements.txt
```

## Run

```
python -m daemon.cli start           # prompts for opt-in confirmation
python -m daemon.cli start -y        # skip prompt
python -m daemon.cli status
python -m daemon.cli pause
python -m daemon.cli resume
python -m daemon.cli stop
```

(Or run `python daemon/daemon.py` directly in the foreground.)

## Config

`~/.trailmem/config.json` is created on first run.

```json
{
  "watch_dir": "~/.claude/projects",
  "chunk_size": 10,
  "silence_minutes": 5,
  "llm_backend": "cli",
  "llm_model": "sonnet",
  "anthropic_api_key": "",
  "openai_api_key": "",
  "openai_base_url": "https://api.openai.com/v1",
  "db_path": "~/.trailmem/trailmem.db",
  "include_compaction_summary": false
}
```

- `llm_backend`: `"cli"` (default — calls `claude -p`), `"api"` (Anthropic API
  via `anthropic_api_key`/`ANTHROPIC_API_KEY`), or `"openai"` (OpenAI-compatible
  endpoint).
- Chunks flush when `chunk_size` lines accumulate OR `silence_minutes`
  elapse since the last write — whichever first.
- Compaction summaries are skipped (set `include_compaction_summary: true`
  to override).

## Bootstrap behavior

On first start, the daemon scans existing jsonl files and skips to end-of-file
for each — only new writes get ingested. This avoids LLM-flooding on initial
launch. Subsequent restarts resume from the saved offset in
`~/.trailmem/state.json`.

## Hook coexistence

If `~/.claude/settings.json` still contains a `UserPromptSubmit` hook calling
`trailmem_hook.sh` or `trailmem-auto-ingest.sh`, the daemon will log a warning
on startup. Disable the hook to avoid duplicate episodes.

## Schema

`episodes` table gets a new `source_jsonl_uuids` column (JSON array of message
UUIDs the episode was extracted from) so a future deep-recall pass can grep
the original jsonl for the exact prompt/response.
