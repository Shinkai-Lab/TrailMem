#!/bin/bash
# trailmem-actr-migrate.sh — ACT-R base_level用のDBマイグレーション
# episode_keywordsにrecall_historyカラムを追加
# effective_strengthの計算にACT-R base-level activationを使う

set -euo pipefail

DB="${TRAILMEM_DB:-$HOME/.trailmem/trailmem.db}"

echo "ACT-R migration for $DB"

# バックアップ
cp "$DB" "${DB}.bak-actr-$(date +%Y%m%d%H%M%S)"
echo "  backup created"

# recall_history: JSON array of turn_seq values when this trail was recalled
# actr_base_level: ACT-R computed base-level activation (replaces decay-based calculation)
sqlite3 "$DB" << 'SQL'
-- recall history for ACT-R base-level computation
ALTER TABLE episode_keywords ADD COLUMN recall_history TEXT NOT NULL DEFAULT '[]';

-- ACT-R base-level activation (computed from recall_history)
ALTER TABLE episode_keywords ADD COLUMN actr_base_level REAL NOT NULL DEFAULT 0.0;

-- Schema version bump
INSERT OR REPLACE INTO trailmem_meta (key, value) VALUES ('actr_version', '1');

-- Seed recall_history from existing data:
-- If last_recalled_seq > 0, add it as the only known recall point
UPDATE episode_keywords
SET recall_history = json_array(last_recalled_seq)
WHERE last_recalled_seq > 0 AND recall_history = '[]';

-- For never-recalled trails, seed with created_turn_seq from the episode
UPDATE episode_keywords
SET recall_history = json_array(
  (SELECT COALESCE(e.created_turn_seq, 0) FROM episodes e WHERE e.id = episode_keywords.episode_id)
)
WHERE recall_history = '[]';
SQL

echo "  columns added, history seeded"

# Compute initial ACT-R base-level for all trails
python3 - "$DB" << 'PYEOF'
import sqlite3, json, math, sys

db_path = sys.argv[1]
conn = sqlite3.connect(db_path)
cur = conn.cursor()

current_seq = int(cur.execute("SELECT COALESCE(value, '0') FROM trailmem_meta WHERE key = 'turn_seq'").fetchone()[0])
d = 0.5  # ACT-R standard decay

rows = cur.execute("""
    SELECT episode_id, keyword, recall_history, base_strength
    FROM episode_keywords WHERE is_deleted = 0
""").fetchall()

updated = 0
for ep_id, kw, hist_json, base_str in rows:
    try:
        history = json.loads(hist_json)
    except:
        history = []

    if not history:
        # No history: use base_strength as fallback
        actr_bl = math.log(max(base_str, 0.01))
    else:
        # B_i = ln(sum(t_j^(-d))) where t_j = current_seq - recall_seq
        total = 0.0
        for seq in history:
            if seq is None:
                continue
            t = max(1, current_seq - int(seq))
            total += t ** (-d)
        actr_bl = math.log(max(total, 1e-10))

    # Normalize to 0-1 range using sigmoid
    # sigmoid(x) maps (-inf, +inf) to (0, 1)
    actr_normalized = 1.0 / (1.0 + math.exp(-actr_bl))

    cur.execute("""
        UPDATE episode_keywords
        SET actr_base_level = ?,
            effective_strength = ?
        WHERE episode_id = ? AND keyword = ?
    """, (actr_bl, actr_normalized, ep_id, kw))
    updated += 1

conn.commit()
conn.close()
print(f"  {updated} trails updated with ACT-R base-level")
PYEOF

echo "  migration complete"
