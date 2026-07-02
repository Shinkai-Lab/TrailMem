#!/bin/bash
# trailmem-decay.sh — 月次decay処理
# 1ヶ月recallされなかったトレイルのdecay *= 0.5
# Usage: bash trailmem-decay.sh [--dry-run]

set -euo pipefail

DB="${TRAILMEM_DB:-$HOME/.trailmem/trailmem.db}"
DECAY_FACTOR=0.5
ONE_MONTH_AGO=$(date -u -d "1 month ago" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -v-1m +%Y-%m-%dT%H:%M:%SZ)
DRY_RUN="${1:-}"

echo "TrailMem monthly decay"
echo "  DB: $DB"
echo "  Cutoff: $ONE_MONTH_AGO"
echo "  Factor: $DECAY_FACTOR"

# Count affected trails
AFFECTED=$(sqlite3 "$DB" "
  SELECT COUNT(*) FROM episode_keywords
  WHERE is_deleted = 0
    AND (last_recalled IS NULL OR last_recalled < '$ONE_MONTH_AGO');
")

echo "  Affected trails: $AFFECTED"

if [ "$DRY_RUN" = "--dry-run" ]; then
  echo "  (dry run — no changes made)"
elif [ "$AFFECTED" = "0" ]; then
  echo "  Nothing to decay."
else
  # Apply decay and recalculate effective_strength
  sqlite3 "$DB" "
    UPDATE episode_keywords
    SET decay = decay * $DECAY_FACTOR,
        effective_strength = (CAST((used - misled + 1) AS REAL) / (shown + 2)) * (decay * $DECAY_FACTOR)
    WHERE is_deleted = 0
      AND (last_recalled IS NULL OR last_recalled < '$ONE_MONTH_AGO');
  "

  # Mark for deletion: shown >= 10 AND used == 0
  DELETION_CANDIDATES=$(sqlite3 "$DB" "
    SELECT COUNT(*) FROM episode_keywords
    WHERE is_deleted = 0 AND shown >= 10 AND used = 0;
  ")

  if [ "$DELETION_CANDIDATES" -gt 0 ]; then
    echo "  Deletion candidates (shown>=10, used=0): $DELETION_CANDIDATES"
    sqlite3 "$DB" "
      UPDATE episode_keywords SET is_deleted = 1
      WHERE is_deleted = 0 AND shown >= 10 AND used = 0;
    "
    echo "  Marked as deleted."
  fi

  echo "  Done. $AFFECTED trails decayed."
fi

# --- アメーバ網: エッジ減衰（けもの道のエッジ版） ---
# 設計書 ④: 長期間通られていないエッジは weight *= 0.998。
# weight < 0.02 になったエッジは物理削除（網のスパース性維持）。
HAS_EDGES=$(sqlite3 "$DB" "SELECT name FROM sqlite_master WHERE type='table' AND name='keyword_edges';")
if [ -n "$HAS_EDGES" ]; then
  EDGE_DECAY=0.998
  # 「長期間未通過」= 現在 turn_seq から STALE_TURNS 以上前に最後に通られたエッジ
  STALE_TURNS=100
  CURRENT_SEQ=$(sqlite3 "$DB" "SELECT COALESCE(value,'0') FROM trailmem_meta WHERE key='turn_seq';")
  STALE_CUTOFF=$(( CURRENT_SEQ - STALE_TURNS ))

  EDGE_AFFECTED=$(sqlite3 "$DB" "
    SELECT COUNT(*) FROM keyword_edges WHERE last_traversed_seq < $STALE_CUTOFF;
  ")
  echo ""
  echo "  [amoeba] Edge decay (factor $EDGE_DECAY, stale < seq $STALE_CUTOFF)"
  echo "  [amoeba] Stale edges: $EDGE_AFFECTED"

  if [ "$DRY_RUN" = "--dry-run" ]; then
    echo "  [amoeba] (dry run — no changes made)"
  elif [ "$EDGE_AFFECTED" != "0" ]; then
    sqlite3 "$DB" "
      UPDATE keyword_edges
      SET weight = weight * $EDGE_DECAY
      WHERE last_traversed_seq < $STALE_CUTOFF;
    "
    PRUNED=$(sqlite3 "$DB" "SELECT COUNT(*) FROM keyword_edges WHERE weight < 0.02;")
    if [ "$PRUNED" -gt 0 ]; then
      sqlite3 "$DB" "DELETE FROM keyword_edges WHERE weight < 0.02;"
      echo "  [amoeba] Pruned $PRUNED edges (weight < 0.02)."
    fi
    echo "  [amoeba] Done. $EDGE_AFFECTED edges decayed."
  fi
fi
