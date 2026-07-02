#!/bin/bash
# trailmem-vec-migrate.sh — sqlite-vecベクトル検索テーブルの追加
# 既存エピソードのembeddingを一括生成

set -euo pipefail

DB="${TRAILMEM_DB:-$HOME/.trailmem/trailmem.db}"

echo "Vector migration for $DB"

cp "$DB" "${DB}.bak-vec-$(date +%Y%m%d%H%M%S)"
echo "  backup created"

python3 - "$DB" << 'PYEOF'
import sqlite3, sqlite_vec, struct, sys, time
from sentence_transformers import SentenceTransformer

db_path = sys.argv[1]

conn = sqlite3.connect(db_path)
conn.enable_load_extension(True)
sqlite_vec.load(conn)

# ベクトルテーブル作成（既にあればスキップ）
tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
if 'episode_vec' not in tables:
    conn.execute("CREATE VIRTUAL TABLE episode_vec USING vec0(embedding float[384])")
    print("  episode_vec table created")
else:
    print("  episode_vec table already exists")

# メタデータ更新
conn.execute("INSERT OR REPLACE INTO trailmem_meta (key, value) VALUES ('vec_version', '1')")
conn.execute("INSERT OR REPLACE INTO trailmem_meta (key, value) VALUES ('vec_model', 'all-MiniLM-L6-v2')")
conn.execute("INSERT OR REPLACE INTO trailmem_meta (key, value) VALUES ('vec_dim', '384')")

# 既存エピソードのembedding生成
episodes = conn.execute("SELECT id, summary, inner FROM episodes").fetchall()

# 既にembedding済みのrowid取得
existing = set()
try:
    for row in conn.execute("SELECT rowid FROM episode_vec"):
        existing.add(row[0])
except:
    pass

# episode IDから数値rowid用のマッピング
# episode_vecのrowidはepisodesテーブルのROWIDと揃える
ep_rowids = conn.execute("SELECT rowid, id FROM episodes").fetchall()
id_to_rowid = {eid: rowid for rowid, eid in ep_rowids}

pending = [(eid, summary, inner) for eid, summary, inner in episodes if id_to_rowid.get(eid, -1) not in existing]
print(f"  {len(pending)} episodes to embed ({len(existing)} already done)")

if not pending:
    conn.commit()
    conn.close()
    print("  done (nothing to do)")
    sys.exit(0)

# モデルロード
t0 = time.time()
model = SentenceTransformer('all-MiniLM-L6-v2')
t1 = time.time()
print(f"  model loaded in {t1-t0:.1f}s")

# バッチembedding
texts = [f"{summary} {inner}" for _, summary, inner in pending]
t2 = time.time()
embeddings = model.encode(texts, show_progress_bar=False)
t3 = time.time()
print(f"  {len(texts)} embeddings generated in {t3-t2:.1f}s")

# DB挿入
def serialize(vec):
    return struct.pack(f'{len(vec)}f', *vec)

for i, (eid, _, _) in enumerate(pending):
    rowid = id_to_rowid[eid]
    conn.execute(
        "INSERT INTO episode_vec(rowid, embedding) VALUES (?, ?)",
        (rowid, serialize(embeddings[i].tolist()))
    )

conn.commit()
conn.close()
t4 = time.time()
print(f"  inserted {len(pending)} vectors in {t4-t3:.1f}s")
print(f"  total time: {t4-t0:.1f}s")
print("  migration complete")
PYEOF
