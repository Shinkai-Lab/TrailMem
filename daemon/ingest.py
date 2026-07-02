"""LLM-backed episode extraction + SQLite insert.

Backends:
- "cli": calls `claude -p --model <model>` (default)
- "api": calls Anthropic API directly (requires anthropic_api_key)
- "openai": calls OpenAI-compatible chat completions (baseURL+key)

Logic faithfully ported from scripts/trailmem-auto-ingest.sh.
Schema is auto-created (matches the production trailmem.db schema) and extended with
`source_jsonl_uuids` so deep-recall can grep originals later.
"""
from __future__ import annotations

import itertools
import json
import re
import secrets
import sqlite3
import struct
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

PROMPT_HEADER = """{persona}

Choose what YOUR future self would need to stand in the same place after forgetting.

Rules:
- Return JSON array only. No markdown fences, no prose.
- TOPIC-BASED GROUPING: Summarize conversations by topic boundaries, not by fixed intervals.
  - Merge as many conversations as possible into one episode. Target: minimum 20 conversations per episode, maximum 50.
  - If a completed topic is under 20 conversations, merge it with the next topic.
  - If merging brings the total to 20+, cut there and start a new episode.
  - If a topic is still in progress and under 20 conversations, DO NOT summarize it — return empty array [] so it carries over to the next batch.
  - If all 50 conversations are the same topic, produce exactly 1 episode.
- summary: max 900 chars, factual. Who, what, when.
- inner: what happened and how you reacted, as fact. Brief.
- keywords: 3-6 short searchable nouns/noun phrases. Same language as content.
  IMPORTANT: Reuse existing keywords when the concept matches. Here are existing keywords:
  {existing_keywords}
- themes: 1-2 short ABSTRACT nouns per episode, each with synonyms (inflections /
  closely related words). A theme is NOT the name of the event — it's the abstract
  topic this memory should resurface for later, even when the future conversation
  paraphrases it completely differently. This is a separate bucket from `keywords`
  (concrete nouns): keywords answer "what/who was this about", themes answer "what
  kind of future conversation should dredge this back up".
  Example: an episode about a friend backing out of a promised deal ->
  keywords=["契約","友人"], themes=[{{"kw": "裏切り", "synonyms": ["裏切る","裏切られた"]}},
  {{"kw": "後悔", "synonyms": ["悔やむ","悔しい"]}}].
  More reference pairs: kw=正しさ synonyms=["正しい","正義"] / kw=優しさ synonyms=["優しい","思いやり"] /
  kw=初恋 synonyms=["恋に落ちる","好きになる"] / kw=喪失 synonyms=["死別","失う"].
  If nothing abstract stands out, it's fine to return an empty list for that episode.
- neg/pos: 0-100, independent. Avoid 50/50.
  CALIBRATION — use these reference points for scoring:
  {calibration_samples}
  Most routine work episodes should be neg=10-20, pos=50-70. Reserve pos>80 for genuinely emotional moments.
- salience: 0.0-1.0. Most episodes are 0.3-0.6. Only truly pivotal moments get 0.8+.
- Skip trivial tool calls or routine operations.

Output schema per episode:
{{"summary": "str", "inner": "str", "keywords": ["str"], "themes": [{{"kw": "str", "synonyms": ["str"]}}], "neg": 0-100, "pos": 0-100, "memoryType": "general|decision|risk|preference|relationship|keepsake|design|incident", "salience": 0.0-1.0, "conversation_range": "1-25"}}

conversation_range indicates which conversation numbers (from this batch) were covered by this episode.

以下の会話ログから記憶エピソードを抽出せよ。JSON配列で返せ。

"""

# フラッシュバック有用性の後段判定 (エージェントはサボる前提の設計):
# 会話中のエージェントにフィードバックさせるのではなく、どうせ呼んでいる
# 抽出LLMに相乗りさせて「使われた/無視された/ミスリード」を判定する。
# 追加のLLM呼び出しはゼロ。
FLASHBACK_JUDGE_SECTION = """
ADDITIONALLY: the memory system flashed back the following episodes during this
conversation. For EACH flashback id, judge from the conversation log how it landed:
- "used": the conversation actually built on it, or the assistant clearly drew on it
- "ignored": harmless, but nothing in the conversation connected to it
- "misleading": irrelevant noise, or it pulled the conversation in a wrong direction

Flashbacks to judge:
{flashback_list}

IMPORTANT: because feedback is requested, return a single JSON OBJECT instead of a
bare array:
{{"episodes": [ ...episode objects as specified above... ],
  "flashback_feedback": [{{"id": "<episode-id>", "verdict": "used|ignored|misleading"}}]}}

"""


# ---------------------------------------------------------------- DB schema --

SCHEMA_SQL = [
    """CREATE TABLE IF NOT EXISTS episodes (
      id            TEXT PRIMARY KEY,
      summary       TEXT NOT NULL,
      inner         TEXT NOT NULL,
      quote         TEXT,
      sentiment_neg INTEGER NOT NULL DEFAULT 50,
      sentiment_pos INTEGER NOT NULL DEFAULT 50,
      created_at    TEXT NOT NULL,
      source_type   TEXT NOT NULL,
      source_ref    TEXT NOT NULL,
      feeling_intensity REAL NOT NULL DEFAULT 1.0,
      created_turn_seq INTEGER NOT NULL DEFAULT 0,
      memory_type   TEXT NOT NULL DEFAULT '',
      salience      REAL NOT NULL DEFAULT 0.5
    )""",
    """CREATE TABLE IF NOT EXISTS keywords (
      keyword    TEXT PRIMARY KEY,
      synonyms   TEXT NOT NULL DEFAULT '[]',
      created_at TEXT NOT NULL,
      axis       TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS episode_keywords (
      episode_id       TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
      keyword          TEXT NOT NULL REFERENCES keywords(keyword),
      shown            INTEGER NOT NULL DEFAULT 0,
      used             INTEGER NOT NULL DEFAULT 0,
      misled           INTEGER NOT NULL DEFAULT 0,
      base_strength    REAL NOT NULL DEFAULT 0.5,
      decay            REAL NOT NULL DEFAULT 1.0,
      effective_strength REAL NOT NULL DEFAULT 0.5,
      last_recalled    TEXT,
      is_deleted       INTEGER NOT NULL DEFAULT 0,
      last_recalled_seq INTEGER NOT NULL DEFAULT 0,
      recall_history TEXT NOT NULL DEFAULT '[]',
      actr_base_level REAL NOT NULL DEFAULT 0.0,
      PRIMARY KEY (episode_id, keyword)
    )""",
    """CREATE TABLE IF NOT EXISTS keyword_edges (
      kw_a TEXT NOT NULL,
      kw_b TEXT NOT NULL,
      weight REAL NOT NULL DEFAULT 0.1,
      co_count INTEGER NOT NULL DEFAULT 1,
      last_traversed_seq INTEGER NOT NULL DEFAULT 0,
      context TEXT NOT NULL DEFAULT '{}',
      created_at TEXT NOT NULL,
      PRIMARY KEY (kw_a, kw_b)
    )""",
    """CREATE TABLE IF NOT EXISTS trailmem_meta (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL
    )""",
]


def ensure_schema(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        for stmt in SCHEMA_SQL:
            conn.execute(stmt)
        # add source_jsonl_uuids column if missing
        cols = {row[1] for row in conn.execute("PRAGMA table_info(episodes)")}
        if "source_jsonl_uuids" not in cols:
            conn.execute(
                "ALTER TABLE episodes ADD COLUMN source_jsonl_uuids TEXT NOT NULL DEFAULT '[]'"
            )
        # memory_type/salience: previously extracted from the LLM but discarded at
        # insert time. Add columns so existing (pre-theme) DBs pick them up too.
        if "memory_type" not in cols:
            conn.execute(
                "ALTER TABLE episodes ADD COLUMN memory_type TEXT NOT NULL DEFAULT ''"
            )
        if "salience" not in cols:
            conn.execute(
                "ALTER TABLE episodes ADD COLUMN salience REAL NOT NULL DEFAULT 0.5"
            )
        # axis: tags a keywords row as a theme keyword ('theme') vs a regular
        # concrete keyword (''). Added post-hoc for existing DBs.
        kw_cols = {row[1] for row in conn.execute("PRAGMA table_info(keywords)")}
        if "axis" not in kw_cols:
            conn.execute(
                "ALTER TABLE keywords ADD COLUMN axis TEXT NOT NULL DEFAULT ''"
            )
        # seed turn_seq if absent
        row = conn.execute(
            "SELECT value FROM trailmem_meta WHERE key='turn_seq'"
        ).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO trailmem_meta(key, value) VALUES ('turn_seq', '0')"
            )
        conn.commit()
    finally:
        conn.close()


# ------------------------------------------------------------ flashback fb --
# trailmem-scan.sh が門1/門2/spread で発火させたフラッシュバックは
# (DBと同じディレクトリ)/flashback-buffer.jsonl にJSONL追記される。
# daemon側はどうせ呼ぶ抽出LLMに相乗りして「used/ignored/misleading」を
# 判定させ、episode_keywords の shown/used/misled にフィードバックする。
# 追加のLLM呼び出しはゼロ。

def flashback_buffer_path(cfg: dict[str, Any]) -> Path:
    """Resolve the flashback buffer path.

    Explicit `flashback_buffer` config wins; otherwise it's the sibling of the
    DB file (matches trailmem-scan.sh's own default).
    """
    override = cfg.get("flashback_buffer")
    if override:
        return Path(override).expanduser()
    return Path(cfg["db_path"]).expanduser().parent / "flashback-buffer.jsonl"


def read_flashback_buffer(
    path: Path, limit: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read pending flashback entries, split into (batch to judge now, rest).

    Malformed lines are skipped silently. Returns ([], []) if the file is
    absent — the common case when no flashback fired since the last ingest.
    """
    if not path.exists():
        return [], []
    entries: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    entries.append(obj)
    except OSError:
        return [], []
    return entries[:limit], entries[limit:]


def write_flashback_buffer(path: Path, entries: list[dict[str, Any]]) -> None:
    """Overwrite the buffer with `entries` (JSONL). Deletes the file when empty
    so scan.sh doesn't keep appending to a stale leftover."""
    if not entries:
        try:
            path.unlink()
        except OSError:
            pass
        return
    try:
        with path.open("w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
    except OSError:
        pass


def apply_flashback_feedback(
    db_path: str,
    entries: list[dict[str, Any]],
    verdicts: dict[str, str],
) -> int:
    """Fold the piggy-backed LLM verdicts into episode_keywords counters.

    For each flashback entry, look up its verdict (default "ignored" if the
    LLM didn't judge it — e.g. it fell out of the batch or the LLM skipped it),
    bump shown/used/misled on every keyword that contributed to the flashback,
    then recompute base_strength/effective_strength from the fresh counters.
    Returns the number of (episode, keyword) links actually updated.
    """
    if not entries:
        return 0
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = sqlite3.connect(db_path)
    updated = 0
    try:
        for entry in entries:
            ep_id = entry.get("episode_id")
            if not ep_id:
                continue
            verdict = verdicts.get(str(ep_id), "ignored")
            used_inc = 1 if verdict in ("used", "misleading") else 0
            misled_inc = 1 if verdict == "misleading" else 0
            for kw in entry.get("keywords") or []:
                if not isinstance(kw, str) or not kw.strip():
                    continue
                kw = kw.strip()
                try:
                    cur = conn.execute(
                        """UPDATE episode_keywords
                           SET shown = shown + 1,
                               used = used + ?,
                               misled = misled + ?,
                               last_recalled = ?
                           WHERE episode_id = ? AND keyword = ? AND is_deleted = 0""",
                        (used_inc, misled_inc, now, ep_id, kw),
                    )
                    if cur.rowcount <= 0:
                        continue
                    conn.execute(
                        """UPDATE episode_keywords
                           SET base_strength = CAST((used - misled + 1) AS REAL) / (shown + 2),
                               effective_strength = (CAST((used - misled + 1) AS REAL) / (shown + 2)) * decay
                           WHERE episode_id = ? AND keyword = ?""",
                        (ep_id, kw),
                    )
                    updated += 1
                except sqlite3.Error:
                    continue
        conn.commit()
    finally:
        conn.close()
    return updated


def consume_noise(db_path: str) -> int:
    """Process (DBと同じディレクトリ)/noise.txt if present.

    One episode_id per line = "this episode's flashbacks were pure noise",
    an out-of-band override for cases the LLM judge doesn't cover (e.g. a
    human flagging garbage recall directly). Every live link of that episode
    gets shown+1/used+1/misled+1 (net negative to strength) plus the usual
    recomputation. The file is deleted once consumed. Returns lines processed.
    """
    path = Path(db_path).parent / "noise.txt"
    if not path.exists():
        return 0
    try:
        lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return 0
    processed = 0
    if lines:
        now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = sqlite3.connect(db_path)
        try:
            for ep_id in lines:
                try:
                    conn.execute(
                        """UPDATE episode_keywords
                           SET shown = shown + 1, used = used + 1, misled = misled + 1,
                               last_recalled = ?
                           WHERE episode_id = ? AND is_deleted = 0""",
                        (now, ep_id),
                    )
                    conn.execute(
                        """UPDATE episode_keywords
                           SET base_strength = CAST((used - misled + 1) AS REAL) / (shown + 2),
                               effective_strength = (CAST((used - misled + 1) AS REAL) / (shown + 2)) * decay
                           WHERE episode_id = ? AND is_deleted = 0""",
                        (ep_id,),
                    )
                    processed += 1
                except sqlite3.Error:
                    continue
            conn.commit()
        finally:
            conn.close()
    try:
        path.unlink()
    except OSError:
        pass
    return processed


# -------------------------------------------------------------- jsonl parse --

def parse_jsonl_messages(
    lines: list[str],
    *,
    include_compaction: bool = False,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return list of {role, text, uuid} from raw jsonl lines.

    Skips compaction summaries (isCompactSummary, subtype=='compact', etc.)
    and tool-only turns. Returns (messages, all_uuids_seen).
    """
    msgs: list[dict[str, Any]] = []
    uuids: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        # compaction skip
        if not include_compaction:
            if entry.get("isCompactSummary"):
                continue
            if entry.get("subtype") in ("compact", "summary"):
                continue
            if entry.get("type") == "summary":
                continue
        msg = entry.get("message", {})
        role = msg.get("role", "")
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content", "")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = []
            for p in content:
                if isinstance(p, dict) and p.get("type") == "text":
                    parts.append(p.get("text", ""))
            text = " ".join(parts)
        text = text.strip()
        if not text or len(text) < 10:
            continue
        uuid = entry.get("uuid") or entry.get("id") or ""
        msgs.append({"role": role, "text": text[:1200], "uuid": uuid})
        if uuid:
            uuids.append(uuid)
    return msgs, uuids


def build_conversation(msgs: list[dict[str, Any]]) -> str:
    if not msgs:
        return ""
    return "\n---\n".join(f"[{m['role']}] {m['text']}" for m in msgs[-100:])


# ---------------------------------------------------------------- LLM call --

def existing_keywords(db_path: str, limit: int = 200) -> str:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT keyword FROM keywords ORDER BY keyword LIMIT {limit}"
        ).fetchall()
    finally:
        conn.close()
    return "、".join(r[0] for r in rows)


def calibration_samples(db_path: str) -> str:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("""
            SELECT sentiment_neg, sentiment_pos, substr(summary, 1, 60)
            FROM episodes
            WHERE source_type != 'daemon_ingest'
            ORDER BY RANDOM()
            LIMIT 3
        """).fetchall()
    finally:
        conn.close()
    if not rows:
        return "low: neg=10 pos=55 (routine work) / mid: neg=25 pos=70 (notable event) / high: neg=5 pos=90 (deeply emotional)"
    labels = ["low", "mid", "high"]
    rows_sorted = sorted(rows, key=lambda r: r[0] + r[1])
    parts = []
    for label, r in zip(labels, rows_sorted):
        parts.append(f"{label}: neg={r[0]} pos={r[1]} ({r[2]})")
    return " / ".join(parts)


def _resolve_model(backend: str, model: str) -> str:
    """Resolve shorthand model names per backend.

    NOTE: this alias map pins the model generation current as of this
    project's public release. Update it as new model generations ship;
    "sonnet"/"opus" are meant to stay stable shorthand in config.json
    while the concrete dated model strings below evolve underneath them.
    """
    aliases = {
        "sonnet": {
            "cli": "claude-sonnet-4-6",
            "api": "claude-sonnet-4-5-20250929",
        },
        "opus": {
            "cli": "claude-opus-4-6",
            "api": "claude-opus-4-5-20250929",
        },
    }
    return aliases.get(model, {}).get(backend, model)


def call_llm(prompt: str, cfg: dict[str, Any]) -> str:
    backend = cfg.get("llm_backend", "cli")
    model = _resolve_model(backend, cfg.get("llm_model", "sonnet"))

    if backend == "api":
        if not cfg.get("anthropic_api_key"):
            # fallback to cli
            return _call_cli(prompt, _resolve_model("cli", cfg.get("llm_model", "sonnet")))
        return _call_anthropic(prompt, model, cfg["anthropic_api_key"])
    if backend == "openai":
        if not cfg.get("openai_api_key"):
            return _call_cli(prompt, _resolve_model("cli", cfg.get("llm_model", "sonnet")))
        return _call_openai(
            prompt, model, cfg["openai_api_key"], cfg.get("openai_base_url")
        )
    return _call_cli(prompt, model)


def _call_cli(prompt: str, model: str) -> str:
    try:
        proc = subprocess.run(
            ["claude", "--model", model, "-p", "--max-turns", "3"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=180,
        )
        return proc.stdout or ""
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return ""


def _call_anthropic(prompt: str, model: str, api_key: str) -> str:
    try:
        import anthropic
    except ImportError:
        return ""
    client = anthropic.Anthropic(api_key=api_key)
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        parts = []
        for blk in resp.content:
            if getattr(blk, "type", None) == "text":
                parts.append(blk.text)
        return "".join(parts)
    except Exception:
        return ""


def _call_openai(prompt: str, model: str, api_key: str, base_url: str | None) -> str:
    try:
        import urllib.request
    except ImportError:
        return ""
    url = (base_url or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 4096,
        }
    ).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"]
    except Exception:
        return ""


# ---------------------------------------------------------------- DB write --

def _parse_result(raw: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse the LLM response into (episodes, flashback_feedback).

    Normally the model returns a bare JSON array of episodes. When
    FLASHBACK_JUDGE_SECTION was appended to the prompt (pending flashbacks to
    judge), it instead returns a single JSON object with "episodes" and
    "flashback_feedback" keys. Try the object shape first, then fall back to
    the legacy array-only parse so old prompts/behavior keep working.
    """
    if not raw:
        return [], []
    raw = re.sub(r"```(?:json)?\s*", "", raw)
    raw = re.sub(r"```\s*$", "", raw)

    m_obj = re.search(r"\{.*\}", raw, re.DOTALL)
    if m_obj:
        try:
            obj = json.loads(m_obj.group())
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict) and "episodes" in obj:
            eps = obj.get("episodes")
            fb = obj.get("flashback_feedback")
            return (
                eps if isinstance(eps, list) else [],
                fb if isinstance(fb, list) else [],
            )

    m_arr = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m_arr:
        return [], []
    try:
        eps = json.loads(m_arr.group())
    except json.JSONDecodeError:
        return [], []
    return (eps if isinstance(eps, list) else []), []


def _load_embedder(conn: sqlite3.Connection) -> Any:
    """Best-effort load of sqlite-vec + a local sentence-transformer model for
    episode semantic search. Embeddings are a nice-to-have: any failure
    (extension missing, model not installed, load blocked, ...) just disables
    embedding for this insert — episodes still get written without vectors.
    """
    try:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS episode_vec USING vec0(embedding float[384])"
        )
        from sentence_transformers import SentenceTransformer

        return SentenceTransformer("all-MiniLM-L6-v2")
    except Exception:
        return None


def insert_episodes(
    db_path: str,
    episodes: list[dict[str, Any]],
    *,
    source_uuids: list[str],
    source_jsonl: str,
) -> int:
    if not episodes:
        return 0
    conn = sqlite3.connect(db_path)
    inserted = 0
    embedder = _load_embedder(conn)
    try:
        created_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        # turn_seqはこのチャンク内の全エピソードで同じ値を使う(hookの
        # ターン単位カウントとは別物。チャンク単位の粒度で十分)。
        row = conn.execute(
            "SELECT value FROM trailmem_meta WHERE key='turn_seq'"
        ).fetchone()
        turn_seq = int(row[0]) if row else 0

        for ep in episodes:
            summary = (ep.get("summary") or "").strip()
            inner = (ep.get("inner") or "").strip() or "(auto-ingested)"
            keywords = ep.get("keywords") or []
            themes = ep.get("themes") or []
            memory_type = str(ep.get("memoryType") or "").strip()
            try:
                neg = max(0, min(100, int(ep.get("neg", 50))))
                pos = max(0, min(100, int(ep.get("pos", 50))))
            except (TypeError, ValueError):
                neg, pos = 50, 50
            try:
                salience = float(ep.get("salience", 0.5))
            except (TypeError, ValueError):
                salience = 0.5

            if not summary or len(summary) < 10:
                continue
            if len(keywords) < 2:
                continue
            if salience < 0.2:
                continue

            ep_id = f"episode-{int(time.time())}-{secrets.token_hex(4)}"
            source_ref = (
                f"daemon:{datetime.utcnow().strftime('%Y-%m-%d')}:"
                f"{Path(source_jsonl).stem}:{ep_id}"
            )
            conn.execute(
                """INSERT INTO episodes
                  (id, summary, inner, quote, sentiment_neg, sentiment_pos,
                   created_at, source_type, source_ref, created_turn_seq,
                   source_jsonl_uuids, memory_type, salience)
                  VALUES (?, ?, ?, NULL, ?, ?, ?, 'daemon_ingest', ?, ?, ?, ?, ?)""",
                (
                    ep_id,
                    summary,
                    inner,
                    neg,
                    pos,
                    created_at,
                    source_ref,
                    turn_seq,
                    json.dumps(source_uuids, ensure_ascii=False),
                    memory_type,
                    salience,
                ),
            )

            kw_norms: list[str] = []
            for kw in keywords:
                if not isinstance(kw, str):
                    continue
                norm = kw.strip().lower()
                if not norm:
                    continue
                kw_norms.append(norm)
                conn.execute(
                    "INSERT OR IGNORE INTO keywords(keyword, synonyms, created_at) "
                    "VALUES (?, '[]', ?)",
                    (norm, created_at),
                )
                conn.execute(
                    """INSERT OR IGNORE INTO episode_keywords
                      (episode_id, keyword, recall_history, actr_base_level)
                      VALUES (?, ?, ?, 0.0)""",
                    (ep_id, norm, json.dumps([turn_seq])),
                )

            # theme keywords (axis='theme'): abstract "what topic should this
            # resurface for" words, separate bucket from concrete `keywords`
            # but riding the exact same amoeba/episode_keywords machinery —
            # low-degree theme words dodge the hub discount and can carry
            # door2 (convergence) on their own once a couple exist per episode.
            for theme in themes:
                if not isinstance(theme, dict):
                    continue
                norm = str(theme.get("kw") or "").strip().lower()
                if not norm:
                    continue
                raw_syns = theme.get("synonyms") or []
                syns = [
                    str(s).strip()
                    for s in raw_syns
                    if isinstance(raw_syns, list) and isinstance(s, str) and str(s).strip()
                ]
                existing = conn.execute(
                    "SELECT synonyms, axis FROM keywords WHERE keyword=?", (norm,)
                ).fetchone()
                if existing is None:
                    conn.execute(
                        "INSERT INTO keywords(keyword, synonyms, created_at, axis) "
                        "VALUES (?, ?, ?, 'theme')",
                        (norm, json.dumps(syns, ensure_ascii=False), created_at),
                    )
                else:
                    old_syns_raw, old_axis = existing
                    try:
                        old_syns = json.loads(old_syns_raw) or []
                        if not isinstance(old_syns, list):
                            old_syns = []
                    except json.JSONDecodeError:
                        old_syns = []
                    merged = sorted(set(old_syns) | set(syns))
                    new_axis = old_axis if old_axis else "theme"
                    conn.execute(
                        "UPDATE keywords SET synonyms=?, axis=? WHERE keyword=?",
                        (json.dumps(merged, ensure_ascii=False), new_axis, norm),
                    )
                if norm not in kw_norms:
                    kw_norms.append(norm)
                conn.execute(
                    """INSERT OR IGNORE INTO episode_keywords
                      (episode_id, keyword, recall_history, actr_base_level)
                      VALUES (?, ?, ?, 0.0)""",
                    (ep_id, norm, json.dumps([turn_seq])),
                )

            # amoeba edges (concrete keywords + theme keywords together)
            uniq = sorted(set(kw_norms))
            for a, b in itertools.combinations(uniq, 2):
                co_kw = [k for k in uniq if k not in (a, b)][:3]
                row = conn.execute(
                    "SELECT weight, co_count, context FROM keyword_edges "
                    "WHERE kw_a=? AND kw_b=?",
                    (a, b),
                ).fetchone()
                if row is None:
                    ctx = json.dumps(
                        {
                            "emotion": {"neg": neg, "pos": pos},
                            "co_kw": co_kw,
                            "last_episode": ep_id,
                        },
                        ensure_ascii=False,
                    )
                    conn.execute(
                        """INSERT INTO keyword_edges
                          (kw_a, kw_b, weight, co_count, last_traversed_seq,
                           context, created_at)
                          VALUES (?, ?, 0.1, 1, ?, ?, ?)""",
                        (a, b, turn_seq, ctx, created_at),
                    )
                else:
                    old_w, old_co, old_ctx = row
                    new_w = min(1.0, old_w + 0.1)
                    new_co = old_co + 1
                    try:
                        c = json.loads(old_ctx)
                    except json.JSONDecodeError:
                        c = {}
                    em = c.get("emotion", {"neg": neg, "pos": pos})
                    em["neg"] = round(
                        (em.get("neg", neg) * old_co + neg) / new_co
                    )
                    em["pos"] = round(
                        (em.get("pos", pos) * old_co + pos) / new_co
                    )
                    c["emotion"] = em
                    c["co_kw"] = co_kw
                    c["last_episode"] = ep_id
                    conn.execute(
                        """UPDATE keyword_edges
                          SET weight=?, co_count=?, last_traversed_seq=?, context=?
                          WHERE kw_a=? AND kw_b=?""",
                        (
                            new_w,
                            new_co,
                            turn_seq,
                            json.dumps(c, ensure_ascii=False),
                            a,
                            b,
                        ),
                    )

            inserted += 1

            # embedding (best-effort, per エピソード)
            if embedder is not None:
                try:
                    vec = embedder.encode(f"{summary} {inner}")
                    packed = struct.pack(f"{len(vec)}f", *vec.tolist())
                    vec_row = conn.execute(
                        "SELECT rowid FROM episodes WHERE id=?", (ep_id,)
                    ).fetchone()
                    if vec_row:
                        conn.execute(
                            "INSERT INTO episode_vec(rowid, embedding) VALUES (?, ?)",
                            (vec_row[0], packed),
                        )
                except Exception:
                    pass

        # チャンク単位フォールバック心拍: turn_seqは本来hookが毎ターン刻むもの
        # だが、hookの無い環境(API/openaiバックエンドのみで動く構成など)では
        # 何も刻まれないと時間が完全凍結してしまう。ここで1回だけ+1しておき、
        # 挿入されたエピソードが無くても最低限の時間の流れを保証する。
        turn_seq += 1
        conn.execute(
            "UPDATE trailmem_meta SET value=? WHERE key='turn_seq'",
            (str(turn_seq),),
        )
        conn.commit()
    finally:
        conn.close()
    return inserted


# ---------------------------------------------------------------- main API --

def ingest_chunk(
    cfg: dict[str, Any],
    *,
    jsonl_path: str,
    new_lines: list[str],
) -> tuple[int, list[str]]:
    """Process raw jsonl lines: parse, call LLM, insert. Returns (count, uuids)."""
    msgs, uuids = parse_jsonl_messages(
        new_lines,
        include_compaction=bool(cfg.get("include_compaction_summary", False)),
    )
    if len(msgs) < int(cfg.get("min_chunk_lines", 3)):
        return 0, uuids
    conv = build_conversation(msgs)
    if len(conv) < 50:
        return 0, uuids
    db = cfg["db_path"]
    ensure_schema(db)
    kw_str = existing_keywords(db)
    cal_str = calibration_samples(db)

    # 抽出LLM呼び出しに相乗りして、保留中のフラッシュバックの有用性を判定
    # させる(追加のLLM呼び出しはゼロ)。
    buffer = flashback_buffer_path(cfg)
    pending, rest = read_flashback_buffer(buffer, int(cfg.get("judge_max_items", 30)))

    persona = cfg.get("persona") or (
        "You are an AI agent performing sleep-time memory consolidation "
        "on your own recent conversation."
    )
    base_prompt = PROMPT_HEADER.format(
        persona=persona, existing_keywords=kw_str, calibration_samples=cal_str
    )
    if pending:
        listing = "\n".join(
            f'- id={e.get("episode_id", "?")} (turn {e.get("seq", "?")}): {e.get("summary", "")}'
            for e in pending
        )
        prompt = base_prompt + FLASHBACK_JUDGE_SECTION.format(flashback_list=listing) + conv
    else:
        prompt = base_prompt + conv

    raw = call_llm(prompt, cfg)
    eps, feedback = _parse_result(raw)
    n = insert_episodes(db, eps, source_uuids=uuids, source_jsonl=jsonl_path)

    if raw and pending:
        verdicts = {
            str(f.get("id", "")): str(f.get("verdict", "ignored"))
            for f in feedback
            if isinstance(f, dict)
        }
        apply_flashback_feedback(db, pending, verdicts)
        write_flashback_buffer(buffer, rest)
    # raw が空 (LLM呼び出し失敗) の場合はバッファを消費しない。次回のチャンクで
    # 再度judge対象になる。

    consume_noise(db)
    return n, uuids


# ------------------------------------------------------------- maintenance --

def maintenance(cfg: dict[str, Any]) -> dict[str, Any]:
    """Cron-free monthly upkeep, meant to be polled periodically by the daemon
    (e.g. hourly) but only actually does work once every 30 days — gated on
    trailmem_meta['last_maintenance']. Replaces the old trailmem-decay.sh cron
    job: monthly strength decay, dead-link pruning, amoeba edge decay/pruning,
    and a slow forgetting horizon for stale-but-strong links.

    Returns a stats dict, or {} if the 30-day gate hasn't elapsed yet.
    """
    db_path = cfg["db_path"]
    ensure_schema(db_path)
    now = datetime.utcnow()
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = sqlite3.connect(db_path)
    stats: dict[str, Any] = {}
    try:
        row = conn.execute(
            "SELECT value FROM trailmem_meta WHERE key='last_maintenance'"
        ).fetchone()
        if row:
            try:
                last = datetime.strptime(row[0], "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                last = None
            if last is not None and (now - last) < timedelta(days=30):
                return {}

        monthly_decay_factor = float(cfg.get("monthly_decay_factor", 0.5))
        cutoff_30d = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")

        # 月次減衰: last_recalledがNULL、または30日以上前のliveリンク。
        # trailmem-decay.shと同じ式 (decay *= factor / effective_strength再計算)。
        rows = conn.execute(
            """SELECT episode_id, keyword, used, misled, shown, decay
               FROM episode_keywords
               WHERE is_deleted = 0 AND (last_recalled IS NULL OR last_recalled < ?)""",
            (cutoff_30d,),
        ).fetchall()
        decayed = 0
        for episode_id, keyword, used, misled, shown, decay in rows:
            new_decay = decay * monthly_decay_factor
            base = float(used - misled + 1) / float(shown + 2)
            conn.execute(
                """UPDATE episode_keywords SET decay = ?, effective_strength = ?
                   WHERE episode_id = ? AND keyword = ?""",
                (new_decay, base * new_decay, episode_id, keyword),
            )
            decayed += 1
        stats["decayed_links"] = decayed

        # リンク削除マーク: 十分見せたのに一度も使われなかったリンクは死んだとみなす
        cur = conn.execute(
            """UPDATE episode_keywords SET is_deleted = 1
               WHERE shown >= 10 AND used = 0 AND is_deleted = 0"""
        )
        stats["deleted_links"] = cur.rowcount if cur.rowcount is not None else 0

        # エッジ減衰 (keyword_edgesテーブルがある場合のみ = amoeba移行済みDB)
        has_edges = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='keyword_edges'"
        ).fetchone()
        if has_edges:
            seq_row = conn.execute(
                "SELECT value FROM trailmem_meta WHERE key='turn_seq'"
            ).fetchone()
            turn_seq = int(seq_row[0]) if seq_row else 0
            stale_cutoff = turn_seq - 100
            cur = conn.execute(
                "UPDATE keyword_edges SET weight = weight * 0.998 WHERE last_traversed_seq < ?",
                (stale_cutoff,),
            )
            stats["decayed_edges"] = cur.rowcount if cur.rowcount is not None else 0
            cur = conn.execute("DELETE FROM keyword_edges WHERE weight < 0.02")
            stats["pruned_edges"] = cur.rowcount if cur.rowcount is not None else 0

        # 忘却ホライズン: 触れ続けない限り殿堂入り(フロア)もゆっくり沈む。
        # 人間の記憶と同じ — recall_historyの古い半分を捨てて履歴を薄くする。
        horizon_months = int(cfg.get("forget_horizon_months", 6))
        horizon_cutoff = (now - timedelta(days=30 * horizon_months)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        rows = conn.execute(
            """SELECT episode_id, keyword, recall_history FROM episode_keywords
               WHERE is_deleted = 0 AND last_recalled IS NOT NULL AND last_recalled < ?
                 AND json_array_length(recall_history) > 1""",
            (horizon_cutoff,),
        ).fetchall()
        halved = 0
        for episode_id, keyword, history_raw in rows:
            try:
                arr = json.loads(history_raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(arr, list) or len(arr) <= 1:
                continue
            new_arr = arr[len(arr) // 2 :]
            conn.execute(
                "UPDATE episode_keywords SET recall_history = ? WHERE episode_id = ? AND keyword = ?",
                (json.dumps(new_arr), episode_id, keyword),
            )
            halved += 1
        stats["r_halved_links"] = halved

        conn.execute(
            "INSERT OR REPLACE INTO trailmem_meta(key, value) VALUES ('last_maintenance', ?)",
            (now_iso,),
        )
        conn.commit()
    finally:
        conn.close()
    return stats
