#!/bin/bash
# trailmem-add.sh — trailmem.dbにエピソードを投入する
# Usage: bash trailmem-add.sh "要約" "内面コメント" "keyword1,keyword2,keyword3"
#
# 自動処理:
# - ネガポジ判定 (LLM or 手動指定)
# - 新規キーワードのsynonym自動生成
# - 前回recall分のフィードバック処理

set -euo pipefail

DB="${TRAILMEM_DB:-$HOME/.trailmem/trailmem.db}"
SHOWN_FILE="/tmp/trailmem-shown.json"
NOISE_FILE="/tmp/trailmem-noise.txt"

SUMMARY="${1:?Usage: trailmem-add.sh SUMMARY INNER KEYWORDS}"
INNER="${2:?Usage: trailmem-add.sh SUMMARY INNER KEYWORDS}"
KEYWORDS_CSV="${3:?Usage: trailmem-add.sh SUMMARY INNER KEYWORDS}"
QUOTE="${4:-}"
# Manual sentiment override: trailmem-add.sh "..." "..." "..." "" 30 70
SENTIMENT_NEG="${5:-}"
SENTIMENT_POS="${6:-}"

EPISODE_ID="episode-$(date +%s)-$(head -c 4 /dev/urandom | xxd -p)"
CREATED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
SOURCE_TYPE="cto_session"
SOURCE_REF="session:$(date +%Y-%m-%d):${EPISODE_ID}"

# --- Step 1: Sentiment auto-detection (if not manually specified) ---
if [ -z "$SENTIMENT_NEG" ] || [ -z "$SENTIMENT_POS" ]; then
  SENTIMENT_RESULT=$(echo "この要約文と内面コメントの感情温度をJSON形式で判定してください。neg(ネガティブ度0-100)とpos(ポジティブ度0-100)の2値で返してください。値は独立しています(合計100にならなくてOK)。【重要】50:50は禁止です。必ずどちらかに寄せてください。完全にフラットな文脈でも、微妙な温度を読み取って判断してください。要約: $SUMMARY / 内面: $INNER" | claude --model claude-haiku-4-5-20251001 -p 2>/dev/null | python3 -c "
import sys, json, re
try:
    raw = sys.stdin.read()
    # Extract JSON from response
    match = re.search(r'\{[^}]+\}', raw)
    if match:
        data = json.loads(match.group())
        neg = int(data.get('neg', 50))
        pos = int(data.get('pos', 50))
        neg = max(0, min(100, neg))
        pos = max(0, min(100, pos))
        print(f'{neg} {pos}')
    else:
        print('50 50')
except:
    print('50 50')
" 2>/dev/null || echo "50 50")
  SENTIMENT_NEG=$(echo "$SENTIMENT_RESULT" | cut -d' ' -f1)
  SENTIMENT_POS=$(echo "$SENTIMENT_RESULT" | cut -d' ' -f2)
  echo "sentiment: neg=$SENTIMENT_NEG pos=$SENTIMENT_POS (auto)"
fi

# 0-1スケールで渡された場合は100倍する
if [ "$(echo "$SENTIMENT_NEG <= 1" | bc 2>/dev/null)" = "1" ] && [ "$SENTIMENT_NEG" != "0" ]; then
  SENTIMENT_NEG=$(echo "$SENTIMENT_NEG * 100" | bc | cut -d. -f1)
fi
if [ "$(echo "$SENTIMENT_POS <= 1" | bc 2>/dev/null)" = "1" ] && [ "$SENTIMENT_POS" != "0" ]; then
  SENTIMENT_POS=$(echo "$SENTIMENT_POS * 100" | bc | cut -d. -f1)
fi

# --- Step 2: Create episode + increment turn_seq ---
TURN_SEQ=$(sqlite3 "$DB" "UPDATE trailmem_meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'turn_seq'; SELECT value FROM trailmem_meta WHERE key = 'turn_seq';")
sqlite3 "$DB" "INSERT INTO episodes (id, summary, inner, quote, sentiment_neg, sentiment_pos, created_at, source_type, source_ref, created_turn_seq)
VALUES ('$EPISODE_ID', '$(echo "$SUMMARY" | sed "s/'/''/g")', '$(echo "$INNER" | sed "s/'/''/g")', $([ -n "$QUOTE" ] && echo "'$(echo "$QUOTE" | sed "s/'/''/g")'" || echo "NULL"), $SENTIMENT_NEG, $SENTIMENT_POS, '$CREATED_AT', '$SOURCE_TYPE', '$SOURCE_REF', $TURN_SEQ);"

# --- Step 3: Create keywords + trails (with synonym auto-generation) ---
IFS=',' read -ra KW_ARRAY <<< "$KEYWORDS_CSV"
for kw in "${KW_ARRAY[@]}"; do
  kw_normalized=$(echo "$kw" | tr '[:upper:]' '[:lower:]' | xargs)
  [ -z "$kw_normalized" ] && continue

  # Check if keyword is new
  EXISTS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM keywords WHERE keyword = '$kw_normalized';")

  if [ "$EXISTS" = "0" ]; then
    # New keyword: create + generate synonyms
    sqlite3 "$DB" "INSERT INTO keywords (keyword, synonyms, created_at) VALUES ('$kw_normalized', '[]', '$CREATED_AT');"

    # Synonym auto-generation via LLM (language-aware)
    SYNONYMS=$(echo "Generate up to 5 synonyms for \"${kw_normalized}\" as used in this context. Return ONLY a JSON array of synonyms in the SAME LANGUAGE as the keyword and context. No cross-language translations. Context: $SUMMARY / Example for Japanese keyword: [\"お母さん\",\"ママ\",\"おかん\"] / Example for English keyword: [\"mother\",\"mom\",\"mum\"] / Return the array only." | claude --model claude-haiku-4-5-20251001 -p 2>/dev/null | python3 -c "
import sys, json, re
try:
    raw = sys.stdin.read()
    match = re.search(r'\[.*?\]', raw, re.DOTALL)
    if match:
        arr = json.loads(match.group())
        cleaned = [s.strip().lower() for s in arr if isinstance(s, str) and s.strip()]
        print(json.dumps(cleaned, ensure_ascii=False))
    else:
        print('[]')
except:
    print('[]')
" 2>/dev/null || echo "[]")

    sqlite3 "$DB" "UPDATE keywords SET synonyms = '$(echo "$SYNONYMS" | sed "s/'/''/g")' WHERE keyword = '$kw_normalized';"
    echo "  new keyword: $kw_normalized synonyms=$SYNONYMS"
  fi

  sqlite3 "$DB" "INSERT OR IGNORE INTO episode_keywords (episode_id, keyword) VALUES ('$EPISODE_ID', '$kw_normalized');"
done

# --- Step 4: Process feedback from previous recalls ---
if [ -f "$SHOWN_FILE" ] && [ -s "$SHOWN_FILE" ]; then
  # Read noise markers (episode IDs tagged as noise in conversation)
  NOISE_IDS=""
  if [ -f "$NOISE_FILE" ]; then
    NOISE_IDS=$(cat "$NOISE_FILE")
  fi

  FEEDBACK_COUNT=0
  while IFS= read -r line; do
    EP_ID=$(echo "$line" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('episode_id',''))" 2>/dev/null || true)
    KW=$(echo "$line" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('keyword',''))" 2>/dev/null || true)

    [ -z "$EP_ID" ] || [ -z "$KW" ] && continue

    # Check if this episode was marked as noise
    IS_NOISE=0
    if echo "$NOISE_IDS" | grep -qF "$EP_ID"; then
      IS_NOISE=1
    fi

    if [ "$IS_NOISE" = "1" ]; then
      # Misled: shown + used + misled all increment
      sqlite3 "$DB" "UPDATE episode_keywords SET shown = shown + 1, used = used + 1, misled = misled + 1, last_recalled = '$CREATED_AT' WHERE episode_id = '$EP_ID' AND keyword = '$KW' AND is_deleted = 0;"
      echo "  feedback: $EP_ID/$KW → misled"
    else
      # Used (default): shown + used increment
      sqlite3 "$DB" "UPDATE episode_keywords SET shown = shown + 1, used = used + 1, last_recalled = '$CREATED_AT' WHERE episode_id = '$EP_ID' AND keyword = '$KW' AND is_deleted = 0;"
    fi

    # Recalculate strength
    sqlite3 "$DB" "
      UPDATE episode_keywords
      SET base_strength = CAST((used - misled + 1) AS REAL) / (shown + 2),
          effective_strength = (CAST((used - misled + 1) AS REAL) / (shown + 2)) * decay
      WHERE episode_id = '$EP_ID' AND keyword = '$KW';
    "

    FEEDBACK_COUNT=$((FEEDBACK_COUNT + 1))
  done < "$SHOWN_FILE"

  # Clear feedback files
  rm -f "$SHOWN_FILE" "$NOISE_FILE"
  [ "$FEEDBACK_COUNT" -gt 0 ] && echo "  feedback: $FEEDBACK_COUNT trails updated"
fi

# --- Step 5: Increment epoch counter ---
EPOCH_FILE="/tmp/trailmem-epoch"
CURRENT_EPOCH=$(cat "$EPOCH_FILE" 2>/dev/null || echo "0")
echo "$((CURRENT_EPOCH + 1))" > "$EPOCH_FILE"

KW_COUNT=${#KW_ARRAY[@]}
echo "episode=$EPISODE_ID trails=$KW_COUNT summary=$(echo "$SUMMARY" | head -c 60)"
