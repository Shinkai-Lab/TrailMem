#!/bin/bash
# trailmem-recall-hybrid.sh — キーワード検索+ベクトル検索のハイブリッド想起
# Usage: bash trailmem-recall-hybrid.sh "自然言語クエリ or キーワード" [limit=5]
#
# 1. 入力からキーワードを抽出してトレイル検索（既存recall）
# 2. 入力全体でベクトル検索（セマンティック）
# 3. 重複除去してトレイル強度+ベクトル類似度でランキング

set -euo pipefail

DB="${TRAILMEM_DB:-$HOME/.trailmem/trailmem.db}"
QUERY="${1:?Usage: trailmem-recall-hybrid.sh QUERY [limit]}"
LIMIT="${2:-5}"
SHOWN_FILE="/tmp/trailmem-shown.json"

python3 - "$DB" "$QUERY" "$LIMIT" << 'PYEOF'
import sqlite3, struct, sys, json

db_path = sys.argv[1]
query = sys.argv[2]
limit = int(sys.argv[3])

conn = sqlite3.connect(db_path)

# --- Phase 1: キーワード検索 ---
query_lower = query.lower()
kw_results = {}

keywords = conn.execute("SELECT keyword, synonyms FROM keywords").fetchall()
matched_kws = []
for kw, syns_json in keywords:
    if kw in query_lower:
        matched_kws.append(kw)
        continue
    try:
        syns = json.loads(syns_json)
        if any(s.lower() in query_lower for s in syns if s):
            matched_kws.append(kw)
    except:
        pass

if matched_kws:
    placeholders = ",".join(["?"] * len(matched_kws))
    rows = conn.execute(f"""
        SELECT e.id, e.summary, e.inner, e.sentiment_neg, e.sentiment_pos,
               e.created_at, ek.keyword, ek.effective_strength
        FROM episode_keywords ek
        JOIN episodes e ON e.id = ek.episode_id
        WHERE ek.keyword IN ({placeholders})
          AND ek.is_deleted = 0
          AND ek.effective_strength >= 0.01
        ORDER BY ek.effective_strength DESC
    """, matched_kws).fetchall()

    for eid, summary, inner, neg, pos, created, kw, strength in rows:
        if eid not in kw_results or kw_results[eid]["score"] < strength:
            kw_results[eid] = {
                "summary": summary, "inner": inner, "neg": neg, "pos": pos,
                "created": created, "keyword": kw, "score": float(strength),
                "source": "keyword"
            }

# --- Phase 2: ベクトル検索 ---
vec_results = {}
try:
    import sqlite_vec
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)

    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer('all-MiniLM-L6-v2')
    emb = model.encode([query])[0]
    vec_bytes = struct.pack(f'{len(emb)}f', *emb.tolist())

    vec_rows = conn.execute("""
        SELECT ev.rowid, ev.distance
        FROM episode_vec ev
        WHERE ev.embedding MATCH ? AND k = ?
        ORDER BY ev.distance
    """, (vec_bytes, limit * 2)).fetchall()

    for rowid, dist in vec_rows:
        ep = conn.execute("""
            SELECT id, summary, inner, sentiment_neg, sentiment_pos, created_at
            FROM episodes WHERE rowid = ?
        """, (rowid,)).fetchone()
        if ep:
            eid = ep[0]
            sim = max(0, 1.0 - dist / 2.0)
            vec_results[eid] = {
                "summary": ep[1], "inner": ep[2], "neg": ep[3], "pos": ep[4],
                "created": ep[5], "keyword": f"vec({sim:.2f})", "score": sim * 0.8,
                "source": "vector"
            }
except Exception as e:
    print(f"(ベクトル検索スキップ: {e})", file=sys.stderr)

# --- Phase 3: マージ+ランキング ---
merged = {}
for eid, data in kw_results.items():
    merged[eid] = data.copy()

for eid, data in vec_results.items():
    if eid in merged:
        merged[eid]["score"] = max(merged[eid]["score"], data["score"])
        merged[eid]["source"] = "both"
    else:
        merged[eid] = data.copy()

ranked = sorted(merged.items(), key=lambda x: x[1]["score"], reverse=True)[:limit]

if not ranked:
    print("(想起結果なし)")
    sys.exit(0)

# 出力
shown_entries = []
for eid, data in ranked:
    src_tag = {"keyword": "KW", "vector": "VEC", "both": "KW+VEC"}[data["source"]]
    print(f"[{eid}] ({data['score']:.3f} {src_tag}) {data['summary']}")
    print(f"  inner: {data['inner']}")
    print(f"  {data['keyword']}  neg/pos: {data['neg']}/{data['pos']}  date: {data['created'][:10]}")
    print()
    shown_entries.append(json.dumps({
        "episode_id": eid,
        "keyword": data.get("keyword", ""),
        "recalled_at": data["created"]
    }))

# フィードバック記録
with open("/tmp/trailmem-shown.json", "a") as f:
    for entry in shown_entries:
        f.write(entry + "\n")

print(f"--- {len(ranked)} 件を想起 (KW:{len(kw_results)} VEC:{len(vec_results)}) ---")

conn.close()
PYEOF
