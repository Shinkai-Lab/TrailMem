#!/bin/bash
# trailmem-recall.sh — trailmem.dbから記憶を想起する
# Usage: bash trailmem-recall.sh keyword1 [keyword2 ...]
# Level: recall(>=0.5) / deep(0.2-0.5) / dig(<0.2)

set -euo pipefail

DB="${TRAILMEM_DB:-$HOME/.trailmem/trailmem.db}"
LEVEL="${TRAILMEM_LEVEL:-recall}"
LIMIT="${TRAILMEM_LIMIT:-5}"
SHOWN_FILE="/tmp/trailmem-shown.json"

if [ $# -eq 0 ]; then
  echo "Usage: bash trailmem-recall.sh keyword1 [keyword2 ...]"
  echo "  TRAILMEM_LEVEL=deep bash trailmem-recall.sh keyword  (deep recall: 0.2-0.5)"
  echo "  TRAILMEM_LEVEL=dig bash trailmem-recall.sh keyword   (dig: <0.2)"
  exit 1
fi

case "$LEVEL" in
  recall) MIN=0.5; MAX_CLAUSE="" ;;
  deep)   MIN=0.2; MAX_CLAUSE="AND ek.effective_strength < 0.5" ;;
  dig)    MIN=0.0; MAX_CLAUSE="AND ek.effective_strength < 0.2" ;;
  *)      echo "Unknown level: $LEVEL"; exit 1 ;;
esac

KW_CONDITIONS=""
for kw in "$@"; do
  kw_lower=$(echo "$kw" | tr '[:upper:]' '[:lower:]')
  [ -n "$KW_CONDITIONS" ] && KW_CONDITIONS="$KW_CONDITIONS, "
  KW_CONDITIONS="${KW_CONDITIONS}'$kw_lower'"
done

# Synonym reverse lookup: if input word appears in any keyword's synonyms array, add that keyword
SYNONYM_HITS=$(sqlite3 "$DB" "
  SELECT keyword FROM keywords
  WHERE synonyms LIKE '%$1%'
    AND keyword NOT IN ($KW_CONDITIONS);
" 2>/dev/null || true)

if [ -n "$SYNONYM_HITS" ]; then
  while IFS= read -r hit; do
    hit_lower=$(echo "$hit" | tr '[:upper:]' '[:lower:]')
    KW_CONDITIONS="${KW_CONDITIONS}, '$hit_lower'"
  done <<< "$SYNONYM_HITS"
fi

# Query and capture episode IDs for feedback tracking
RESULTS=$(sqlite3 -separator '|' "$DB" "
WITH ranked AS (
  SELECT e.id, e.summary, e.inner, e.created_at,
         ek.keyword, ek.effective_strength,
         MAX(ek.effective_strength) OVER (PARTITION BY e.id) AS best,
         ROW_NUMBER() OVER (PARTITION BY e.id ORDER BY ek.effective_strength DESC) AS rank
  FROM episode_keywords ek
  JOIN episodes e ON e.id = ek.episode_id
  WHERE ek.keyword IN ($KW_CONDITIONS)
    AND ek.is_deleted = 0
    AND ek.effective_strength >= $MIN
    $MAX_CLAUSE
)
SELECT id, summary, inner, keyword, printf('%.2f', best) as strength, substr(created_at, 1, 10) as date
FROM ranked WHERE rank = 1
ORDER BY best DESC LIMIT $LIMIT;
")

if [ -z "$RESULTS" ]; then
  echo "(想起結果なし)"
  exit 0
fi

# Display results
echo "$RESULTS" | while IFS='|' read -r id summary inner keyword strength date; do
  echo "[$id] ($strength) $summary"
  echo "  inner: $inner"
  echo "  keyword: $keyword  date: $date"
  echo ""
done

# Record shown episodes for feedback (append to JSON file)
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "$RESULTS" | while IFS='|' read -r id summary inner keyword strength date; do
  echo "{\"episode_id\":\"$id\",\"keyword\":\"$keyword\",\"recalled_at\":\"$TIMESTAMP\"}"
done >> "$SHOWN_FILE"

# Update last_recalled + strengthen trail (recall = walking the path = path gets wider)
# deep/dig = deliberate effort to remember → bigger bonus
case "$LEVEL" in
  recall) BOOST=1.05 ;;
  deep)   BOOST=1.15 ;;
  dig)    BOOST=1.25 ;;
  *)      BOOST=1.05 ;;
esac
echo "$RESULTS" | while IFS='|' read -r id summary inner keyword strength date; do
  sqlite3 "$DB" "
    UPDATE episode_keywords
    SET last_recalled = '$TIMESTAMP',
        shown = shown + 1,
        effective_strength = MIN(1.0, effective_strength * $BOOST)
    WHERE episode_id = '$id' AND keyword = '$keyword';
  " 2>/dev/null || true
done

SHOWN_COUNT=$(echo "$RESULTS" | wc -l)
echo "--- $SHOWN_COUNT 件を想起。フィードバック対象として記録済み ---"
echo "ノイズだった記憶は応答に <noise>episode-id</noise> を含めてください"
