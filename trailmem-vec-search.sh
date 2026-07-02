#!/bin/bash
# trailmem-vec-search.sh — ベクトル検索でエピソードを想起
# Usage: bash trailmem-vec-search.sh "検索クエリ" [k=5]
# キーワード検索(recall.sh)と組み合わせて使う

set -euo pipefail

DB="${TRAILMEM_DB:-$HOME/.trailmem/trailmem.db}"
QUERY="${1:?Usage: trailmem-vec-search.sh QUERY [k]}"
K="${2:-5}"

python3 - "$DB" "$QUERY" "$K" << 'PYEOF'
import sqlite3, sqlite_vec, struct, sys
from sentence_transformers import SentenceTransformer

db_path, query, k = sys.argv[1], sys.argv[2], int(sys.argv[3])

conn = sqlite3.connect(db_path)
conn.enable_load_extension(True)
sqlite_vec.load(conn)

model = SentenceTransformer('all-MiniLM-L6-v2')
emb = model.encode([query])[0]
vec_bytes = struct.pack(f'{len(emb)}f', *emb.tolist())

results = conn.execute(f"""
    SELECT ev.rowid, ev.distance
    FROM episode_vec ev
    WHERE ev.embedding MATCH ? AND k = ?
    ORDER BY ev.distance
""", (vec_bytes, k)).fetchall()

if not results:
    print("(ベクトル検索結果なし)")
    sys.exit(0)

for rowid, dist in results:
    ep = conn.execute("""
        SELECT id, summary, inner, sentiment_neg, sentiment_pos, created_at
        FROM episodes WHERE rowid = ?
    """, (rowid,)).fetchone()
    if not ep:
        continue
    eid, summary, inner, neg, pos, created = ep
    # トレイル情報も取得
    trails = conn.execute("""
        SELECT keyword, printf('%.3f', effective_strength)
        FROM episode_keywords WHERE episode_id = ? AND is_deleted = 0
        ORDER BY effective_strength DESC LIMIT 3
    """, (eid,)).fetchall()
    kws = ", ".join(f"{kw}({s})" for kw, s in trails)

    sim = max(0, 1.0 - dist / 2.0)  # 距離→類似度に概算変換
    print(f"[{eid}] (sim={sim:.2f}) {summary}")
    print(f"  inner: {inner}")
    print(f"  trails: {kws}  neg/pos: {neg}/{pos}  date: {created[:10]}")
    print()

conn.close()
PYEOF
