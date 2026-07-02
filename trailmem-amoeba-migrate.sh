#!/bin/bash
# trailmem-amoeba-migrate.sh — アメーバ網(keyword_edges)の初期生成
#
# 設計書 DESIGN-amoeba.md ① エッジ生成（ヘッブ則・初回バッチ）
# - keyword_edges テーブルを作成（無向エッジ kw_a < kw_b）
# - 既存全エピソードのキーワード共起(co-occurrence)からエッジを初期生成
#   weight = MIN(1.0, co_count * 0.1)
#   context = {emotion:{neg,pos平均}, co_kw:[共起上位3], last_episode}
# - 冪等（再実行で壊れない。テーブルはIF NOT EXISTS、エッジは作り直し）
#
# Usage: bash trailmem-amoeba-migrate.sh
#        TRAILMEM_DB=/path/to/test.db bash trailmem-amoeba-migrate.sh

set -euo pipefail

DB="${TRAILMEM_DB:-$HOME/.trailmem/trailmem.db}"

echo "TrailMem amoeba migrate"
echo "  DB: $DB"

# --- ① テーブル作成（冪等） ---
sqlite3 "$DB" <<'SQL'
CREATE TABLE IF NOT EXISTS keyword_edges (
  kw_a TEXT NOT NULL,
  kw_b TEXT NOT NULL,
  weight REAL NOT NULL DEFAULT 0.1 CHECK (weight > 0),
  co_count INTEGER NOT NULL DEFAULT 1,
  last_traversed_seq INTEGER NOT NULL DEFAULT 0,
  context TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (kw_a, kw_b),
  FOREIGN KEY (kw_a) REFERENCES keywords(keyword),
  FOREIGN KEY (kw_b) REFERENCES keywords(keyword)
);
CREATE INDEX IF NOT EXISTS idx_edge_a ON keyword_edges(kw_a, weight DESC);
CREATE INDEX IF NOT EXISTS idx_edge_b ON keyword_edges(kw_b, weight DESC);
SQL

# --- エッジ初期生成（共起計算）。冪等のため作り直し ---
python3 - "$DB" <<'PYEOF'
import sqlite3, sys, json
from collections import defaultdict
from itertools import combinations

db = sys.argv[1]
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row

# エピソードごとのキーワード集合（生きてるトレイルのみ）
ep_kws = defaultdict(set)
for r in conn.execute("""
    SELECT episode_id, keyword FROM episode_keywords
    WHERE is_deleted = 0
"""):
    ep_kws[r["episode_id"]].add(r["keyword"])

# エピソードの感情・turn_seq
ep_info = {}
for r in conn.execute("SELECT id, sentiment_neg, sentiment_pos, created_turn_seq FROM episodes"):
    ep_info[r["id"]] = (r["sentiment_neg"], r["sentiment_pos"], r["created_turn_seq"])

# ペア集計
# pair -> {co_count, neg_sum, pos_sum, last_seq, last_ep}
pairs = {}
# kw -> co出現相手のカウント（co_kw堆積用）
co_partner = defaultdict(lambda: defaultdict(int))

for ep, kws in ep_kws.items():
    if ep not in ep_info:
        continue
    neg, pos, seq = ep_info[ep]
    for a, b in combinations(sorted(kws), 2):
        key = (a, b)
        d = pairs.get(key)
        if d is None:
            d = {"co": 0, "neg": 0, "pos": 0, "last_seq": -1, "last_ep": ""}
            pairs[key] = d
        d["co"] += 1
        d["neg"] += neg
        d["pos"] += pos
        if seq >= d["last_seq"]:
            d["last_seq"] = seq
            d["last_ep"] = ep
        co_partner[a][b] += 1
        co_partner[b][a] += 1

# エッジ作り直し（冪等）
conn.execute("DELETE FROM keyword_edges")

rows = []
for (a, b), d in pairs.items():
    co = d["co"]
    weight = min(1.0, co * 0.1)
    avg_neg = round(d["neg"] / co)
    avg_pos = round(d["pos"] / co)
    # co_kw: a,b 双方の共起相手の合算上位3（自分自身は除外）
    combined = defaultdict(int)
    for partner, c in co_partner[a].items():
        if partner not in (a, b):
            combined[partner] += c
    for partner, c in co_partner[b].items():
        if partner not in (a, b):
            combined[partner] += c
    co_kw = [k for k, _ in sorted(combined.items(), key=lambda x: -x[1])[:3]]
    context = json.dumps({
        "emotion": {"neg": avg_neg, "pos": avg_pos},
        "co_kw": co_kw,
        "last_episode": d["last_ep"],
    }, ensure_ascii=False)
    rows.append((a, b, weight, co, d["last_seq"] if d["last_seq"] >= 0 else 0, context))

conn.executemany("""
    INSERT INTO keyword_edges (kw_a, kw_b, weight, co_count, last_traversed_seq, context)
    VALUES (?, ?, ?, ?, ?, ?)
""", rows)
conn.commit()

print(f"  edges created: {len(rows)}")

# 統計
mx = conn.execute("SELECT MAX(weight) FROM keyword_edges").fetchone()[0]
print(f"  max weight: {mx}")

# 次数（ノードごとのエッジ本数）上位
deg = defaultdict(int)
for a, b in conn.execute("SELECT kw_a, kw_b FROM keyword_edges"):
    deg[a] += 1
    deg[b] += 1
print("  hub nodes (degree top5):")
for k, d in sorted(deg.items(), key=lambda x: -x[1])[:5]:
    print(f"    {k}: {d}")

conn.close()
PYEOF

echo "Done."
