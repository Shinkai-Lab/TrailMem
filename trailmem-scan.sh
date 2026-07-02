#!/bin/bash
# trailmem-scan.sh — 入力テキストのキーワード統計 + フラッシュバック
# Usage: bash trailmem-scan.sh "きょう雨だったけどTrailMemの話をした"
#
# フラッシュバックの門は2つ (OR):
#   門1(感情):  best_strength × 感情強度 × 経年減衰 > TRAILMEM_FLASHBACK_THRESHOLD
#              (単文字キーワードは部分一致誤爆が多いため門1の単独トリガーにはなれない)
#   門2(収束):  入力中の2キーワード以上が同一エピソードへ収束し
#              Σ(リンク強度×ハブ割引) × 経年減衰 > TRAILMEM_DOOR2_THRESHOLD (感情値を参照しない)
# 門のスコアは「入場審査(関連性)」のみ。発火順は「忘れられ度」(想起回数R昇順)で
# 最大 TRAILMEM_MAX_FLASHBACKS 件 (大御所が新人に道を譲る: 何度も想起された
# 殿堂入り記憶は、忘れられている記憶に候補がない時だけ出てくる)
#
# 発火したフラッシュバックは flashback-buffer.jsonl (DBと同じディレクトリ) に記録され、
# daemonのingest時にLLMが「使われた/無視された/ミスリード」を後段判定して
# shown/used/misled に反映する。エージェント本人は何も意識しなくてよい。
#
# 環境変数:
#   TRAILMEM_DB                    DBパス
#   TRAILMEM_SCAN_THRESHOLD        統計の strong 判定閾値 (default 0.5)
#   TRAILMEM_STATS_LIMIT           統計表示の最大キーワード数 (default 15)
#   TRAILMEM_FLASHBACK_THRESHOLD   門1閾値 (default 0.42)
#   TRAILMEM_DOOR2                 門2 on/off (default 1)
#   TRAILMEM_DOOR2_THRESHOLD       門2閾値 (default 0.125, ベンチ実証値)
#   TRAILMEM_DOOR2_MIN_LINK        門2で収束に数えるリンクの最低強度 (default 0.12)
#   TRAILMEM_WEAKEST_FIRST         弱い子優先 on/off (default 1)
#   TRAILMEM_MIN_SOLO_KW_LEN       門1単独トリガーに必要なキーワード長 (default 2)
#   TRAILMEM_AGE_DECAY             経年減衰/ターン (default 0.9997 ≒ 半減期2000ターン)
#   TRAILMEM_FLASHBACK_COOLDOWN    エピソード単位クールダウン(ターン) (default 30)
#   TRAILMEM_MAX_FLASHBACKS        1ターン最大発火数 (default 3)
#   TRAILMEM_SPREAD_FLASHBACK      連想フラッシュバック on/off (default 1)
#   TRAILMEM_FLASHBACK_BUFFER      バッファファイルパス (default: DBと同じdir)

set -euo pipefail

DB="${TRAILMEM_DB:-$HOME/.trailmem/trailmem.db}"
THRESHOLD="${TRAILMEM_SCAN_THRESHOLD:-0.5}"
export TRAILMEM_SCRIPT_DIR="${TRAILMEM_SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"

if [ $# -eq 0 ]; then
  echo "Usage: bash trailmem-scan.sh \"input text\""
  exit 1
fi

python3 - "$DB" "$THRESHOLD" "$1" <<'PYEOF'
import datetime
import json
import math
import os
import sqlite3
import subprocess
import sys

db, threshold, input_text = sys.argv[1], float(sys.argv[2]), sys.argv[3]
input_folded = input_text.casefold()

spread_enable = os.environ.get("TRAILMEM_SPREAD_FLASHBACK", "1") == "1"
flashback_cooldown = int(os.environ.get("TRAILMEM_FLASHBACK_COOLDOWN", "30"))
flashback_threshold = float(os.environ.get("TRAILMEM_FLASHBACK_THRESHOLD", "0.42"))
max_flashbacks = int(os.environ.get("TRAILMEM_MAX_FLASHBACKS", "3"))
door2_enable = os.environ.get("TRAILMEM_DOOR2", "1") == "1"
# 門2閾値0.125はこころベンチのスイープ+本番DBプロファイルで決定:
# ハブペナルティ後の典型的な特定ペア収束は0.16-0.26、ハブペアのかすりは
# 0.07-0.08で、0.125はちょうど両者を分離する谷間(ab_flashback_results_v2.json)
door2_threshold = float(os.environ.get("TRAILMEM_DOOR2_THRESHOLD", "0.125"))
door2_min_link = float(os.environ.get("TRAILMEM_DOOR2_MIN_LINK", "0.12"))
weakest_first = os.environ.get("TRAILMEM_WEAKEST_FIRST", "1") == "1"
stats_limit = int(os.environ.get("TRAILMEM_STATS_LIMIT", "15"))
min_solo_kw_len = int(os.environ.get("TRAILMEM_MIN_SOLO_KW_LEN", "2"))
age_decay = float(os.environ.get("TRAILMEM_AGE_DECAY", "0.9997"))
buffer_path = os.environ.get(
    "TRAILMEM_FLASHBACK_BUFFER",
    os.path.join(os.path.dirname(os.path.abspath(db)), "flashback-buffer.jsonl"),
)
script_dir = os.environ.get("TRAILMEM_SCRIPT_DIR", ".")  # bash側で自スクリプトのdirにexport済み。これは到達しない保険

conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row

def scalar(sql, params=(), default=None):
    try:
        row = conn.execute(sql, params).fetchone()
    except sqlite3.Error:
        return default
    if row is None:
        return default
    value = row[0]
    return default if value is None else value

def parse_synonyms(raw):
    if not raw or raw == "[]":
        return []
    try:
        values = json.loads(raw)
    except Exception:
        return []
    if not isinstance(values, list):
        return []
    return [str(v) for v in values if str(v)]

current_seq = int(scalar(
    "SELECT COALESCE(value, '0') FROM trailmem_meta WHERE key = 'turn_seq'",
    default=0,
) or 0)

# --- キーワードマッチ (部分一致 + synonym) ---
matched_keywords = []
stats_rows = []  # (keyword, strong, total, neg, pos)

keywords = conn.execute("SELECT keyword, synonyms FROM keywords").fetchall()
for row in keywords:
    keyword = row["keyword"]
    matched = keyword.casefold() in input_folded

    if not matched:
        for synonym in parse_synonyms(row["synonyms"]):
            if synonym.casefold() in input_folded:
                matched = True
                break

    if not matched:
        continue

    counts = conn.execute(
        """
        SELECT
          COUNT(*) AS total,
          COALESCE(SUM(CASE WHEN effective_strength >= 0.1 THEN 1 ELSE 0 END), 0) AS active,
          COALESCE(SUM(CASE WHEN effective_strength > ? THEN 1 ELSE 0 END), 0) AS strong
        FROM episode_keywords
        WHERE keyword = ? AND is_deleted = 0
        """,
        (threshold, keyword),
    ).fetchone()
    if counts is None or int(counts["active"]) == 0:
        continue

    sentiment = conn.execute(
        """
        SELECT
          CAST(ROUND(AVG(e.sentiment_neg)) AS INTEGER) AS neg,
          CAST(ROUND(AVG(e.sentiment_pos)) AS INTEGER) AS pos
        FROM episode_keywords ek
        JOIN episodes e ON e.id = ek.episode_id
        WHERE ek.keyword = ? AND ek.is_deleted = 0
        """,
        (keyword,),
    ).fetchone()
    neg = sentiment["neg"] if sentiment and sentiment["neg"] is not None else 50
    pos = sentiment["pos"] if sentiment and sentiment["pos"] is not None else 50

    matched_keywords.append(keyword)
    stats_rows.append((keyword, int(counts["strong"]), int(counts["total"]), neg, pos))

# --- 統計ダイエット: strong→total順で上位だけ表示 ---
stats_rows.sort(key=lambda r: (-r[1], -r[2], r[0]))
for keyword, strong, total, neg, pos in stats_rows[:stats_limit]:
    print(f"{keyword} ({strong}/{total}) [{neg}/{pos}]")
if len(stats_rows) > stats_limit:
    print(f"(+{len(stats_rows) - stats_limit} keywords)")

# --- フラッシュバック候補: エピソード単位に収束集計 ---
shown_episode_ids = set()
buffer_records = []

def hub_factors(keyword):
    link_count = int(scalar(
        "SELECT COUNT(DISTINCT episode_id) FROM episode_keywords WHERE keyword = ? AND is_deleted = 0",
        (keyword,), default=1) or 1)
    max_link = int(scalar(
        "SELECT MAX(cnt) FROM (SELECT COUNT(DISTINCT episode_id) AS cnt FROM episode_keywords WHERE is_deleted = 0 GROUP BY keyword)",
        default=1) or 1)
    hub_ratio = link_count / max_link if max_link > 0 else 0
    return 1 + 0.05 * (1 - hub_ratio), 1 - hub_ratio * 0.02

def fire_episode(episode_id, boost_keywords, summary, door, score, via=""):
    """発火処理: 表示 + クールダウン + 貢献リンクの強化 + バッファ記録"""
    label = "連想フラッシュバック" if door == "spread" else "フラッシュバック"
    suffix = f" ({via})" if via else ""
    print(f"  💫 {label}{suffix}: {summary}")
    shown_episode_ids.add(episode_id)
    # episode-level cooldown: エピソードの全リンクに現seqを打つ
    conn.execute(
        "UPDATE episode_keywords SET last_recalled_seq = ? WHERE episode_id = ? AND is_deleted = 0",
        (current_seq, episode_id),
    )
    # 貢献したリンクだけ boost + recall_history push (収束時は複数本まとめて太る)
    for kw in boost_keywords:
        boost, decay = hub_factors(kw)
        conn.execute(
            """
            UPDATE episode_keywords
            SET effective_strength = MIN(1.0, effective_strength * ? * ?),
                recall_history = json_insert(
                  CASE WHEN json_valid(recall_history) THEN recall_history ELSE '[]' END,
                  '$[#]', ?)
            WHERE episode_id = ? AND keyword = ?
            """,
            (boost, decay, current_seq, episode_id, kw),
        )
    buffer_records.append({
        "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "seq": current_seq,
        "episode_id": episode_id,
        "keywords": list(boost_keywords),
        "door": door,
        "score": round(score, 4),
        "summary": summary[:160],
        "input": input_text[:160],
    })

if matched_keywords:
    placeholders = ",".join("?" for _ in matched_keywords)
    rows = conn.execute(
        f"""
        SELECT ek.episode_id, ek.keyword, ek.effective_strength,
               e.summary, e.sentiment_neg, e.sentiment_pos, e.created_turn_seq,
               (SELECT MAX(last_recalled_seq) FROM episode_keywords
                 WHERE episode_id = ek.episode_id AND is_deleted = 0) AS ep_last_seq,
               (SELECT MAX(json_array_length(
                    CASE WHEN json_valid(recall_history) THEN recall_history ELSE '[]' END))
                 FROM episode_keywords
                 WHERE episode_id = ek.episode_id AND is_deleted = 0) AS ep_r
        FROM episode_keywords ek
        JOIN episodes e ON e.id = ek.episode_id
        WHERE ek.keyword IN ({placeholders}) AND ek.is_deleted = 0
        """,
        matched_keywords,
    ).fetchall()

    # ハブ次数: 収束の貢献をIDF的に割り引く(spreadのハブ対策と同思想)。
    # 「先生」「私」のような超ハブ語同士のかすり収束が門2を通るのを防ぐ
    degree = {}
    for r in conn.execute(
        "SELECT keyword, COUNT(DISTINCT episode_id) AS c FROM episode_keywords "
        "WHERE is_deleted = 0 GROUP BY keyword"
    ):
        degree[r["keyword"]] = int(r["c"])

    def hub_weight(kw):
        d = degree.get(kw, 1)
        return min(1.0, 1.0 / math.log(1 + d)) if d > 0 else 1.0

    episodes = {}
    for r in rows:
        ep = episodes.setdefault(r["episode_id"], {
            "links": {}, "summary": r["summary"],
            "neg": r["sentiment_neg"], "pos": r["sentiment_pos"],
            "created_seq": r["created_turn_seq"], "last_seq": int(r["ep_last_seq"] or 0),
            "r": int(r["ep_r"] or 0),
        })
        ep["links"][r["keyword"]] = r["effective_strength"]

    candidates = []  # (gate_score, neglect_r, episode_id, door, boost_keywords, via)
    for ep_id, ep in episodes.items():
        if current_seq - ep["last_seq"] <= flashback_cooldown:
            continue
        age = max(0, current_seq - int(ep["created_seq"] or 0))
        agef = age_decay ** age
        sentiment_strength = max(ep["neg"], ep["pos"]) / 100.0

        # 門1(感情): 単文字キーワードは単独では発火できない
        solo_links = {k: v for k, v in ep["links"].items() if len(k) >= min_solo_kw_len}
        if solo_links:
            best_kw = max(solo_links, key=solo_links.get)
            feeling = solo_links[best_kw] * sentiment_strength * agef
            if feeling > flashback_threshold:
                candidates.append((feeling, ep["r"], ep_id, "emotion", [best_kw], ""))
                continue

        # 門2(収束): 2キーワード以上が同一エピソードを指す。感情値は見ない。
        # 各リンクの貢献はハブ次数で割り引く(特定語の収束だけが門を通る)
        if door2_enable:
            conv_links = {k: v for k, v in ep["links"].items() if v >= door2_min_link}
            if len(conv_links) >= 2:
                contrib = {k: v * hub_weight(k) for k, v in conv_links.items()}
                conv = sum(contrib.values()) * agef
                if conv > door2_threshold:
                    via = "+".join(sorted(contrib, key=contrib.get, reverse=True)[:3])
                    candidates.append((conv, ep["r"], ep_id, "convergence", list(conv_links), via))

    # 弱い子優先(大御所が新人に道を譲る):
    # 門のスコアは「入場審査(関連性)」にだけ使い、発火の優先順位は
    # 「忘れられ度」= エピソードの想起回数R昇順で決める。同着なら関連性が高い方。
    # ゲートすれすれのスコア順に選ぶと、閾値をかすっただけのノイズが
    # 「最弱」として常に選ばれてしまう(こころベンチで実証)ため、この分離が必要。
    if weakest_first:
        candidates.sort(key=lambda c: (c[1], -c[0]))
    else:
        candidates.sort(key=lambda c: -c[0])
    for score, _r, ep_id, door, boost_kws, via in candidates:
        if len(shown_episode_ids) >= max_flashbacks:
            break
        fire_episode(ep_id, boost_kws, episodes[ep_id]["summary"], door, score, via)

# --- 連想フラッシュバック (アメーバ網 spread) ---
if spread_enable and matched_keywords and len(shown_episode_ids) < max_flashbacks:
    seen = set()
    seeds = []
    for keyword in matched_keywords:
        # 単文字キーワードは部分一致誤爆が多い(「本体」→「体」等)ため、
        # 門1と同様にspreadの種にもしない。門2(収束)経由でのみ寄与できる
        if len(keyword) < min_solo_kw_len:
            continue
        if keyword not in seen:
            seen.add(keyword)
            seeds.append(keyword)

    env = os.environ.copy()
    env["TRAILMEM_DB"] = db
    env["TRAILMEM_SPREAD_JSON"] = "1"
    env["TRAILMEM_THETA"] = env.get("TRAILMEM_SPREAD_THETA", "0.3")
    env["TRAILMEM_EP_LIMIT"] = env.get("TRAILMEM_SPREAD_EP_LIMIT", "3")
    timeout = float(env.get("TRAILMEM_SPREAD_TIMEOUT", "3"))

    try:
        proc = subprocess.run(
            ["bash", os.path.join(script_dir, "trailmem-spread.sh"), *seeds],
            check=False,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=env,
        )
        data = json.loads(proc.stdout) if proc.stdout.strip() else {"episodes": []}
    except Exception:
        data = {"episodes": []}

    for episode in data.get("episodes", []):
        episode_id = str(episode.get("id", ""))
        keyword = str(episode.get("kw", ""))
        summary = str(episode.get("summary", ""))
        if not episode_id or not keyword or episode_id in shown_episode_ids:
            continue
        ep_max_recalled = int(scalar(
            "SELECT COALESCE(MAX(last_recalled_seq), 0) FROM episode_keywords WHERE episode_id = ? AND is_deleted = 0",
            (episode_id,), default=0) or 0)
        if (current_seq - ep_max_recalled) <= flashback_cooldown:
            continue
        if len(shown_episode_ids) >= max_flashbacks:
            break
        fire_episode(episode_id, [keyword], summary, "spread",
                     float(episode.get("score", 0.0)), via=keyword)

# --- バッファ書き込み: daemonが後段で有用性を判定する ---
if buffer_records:
    try:
        with open(buffer_path, "a", encoding="utf-8") as f:
            for rec in buffer_records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass

conn.commit()
conn.close()
PYEOF
