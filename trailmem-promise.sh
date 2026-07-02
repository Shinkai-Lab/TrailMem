#!/bin/bash
# trailmem-promise.sh — 「約束」を初期ブースト付きエピソードとして投入する
# Usage: bash trailmem-promise.sh "約束の内容" "キーワード1,キーワード2"
#
# 「約束」= 殿堂入り固定ではない、初期値が高い通常エピソード。
# 放置すれば薄れ、触れ続ければ定着する。3つの特性で保持:
#  1. キーワードに必ず「約束」を付与（多言語synonyms付き）
#  2. 初期 effective/base_strength を高く (PROMISE_INIT_STRENGTH=0.8)
#  3. recall_history に初期想起を N 回分入れて中期記憶からスタート
#     (PROMISE_INIT_RECALLS=15 → フロア 0.55相当。N_CONSOLIDATE=30 の半分で殿堂入りはしない)

set -euo pipefail

DB="${TRAILMEM_DB:-$HOME/.trailmem/trailmem.db}"

PROMISE_INIT_STRENGTH="${PROMISE_INIT_STRENGTH:-0.8}"
PROMISE_INIT_RECALLS="${PROMISE_INIT_RECALLS:-15}"
# フロア計算パラメータ（hookと合わせる。報告用フロア値算出にも使う）
FLOOR_MIN="${TRAILMEM_FLOOR_MIN:-0.1}"
N_CONSOLIDATE="${TRAILMEM_N_CONSOLIDATE:-30}"

SUMMARY="${1:?Usage: trailmem-promise.sh \"約束の内容\" \"キーワード1,キーワード2\"}"
KEYWORDS_CSV="${2:-}"

# 「約束」キーワードの多言語・多ニュアンスsynonyms
PROMISE_SYNONYMS='["指切り","契約","宣誓","誓い","約束する","promise","promised","we promised","vow","swear","pledge"]'

EPISODE_ID="episode-$(date +%s)-$(head -c 4 /dev/urandom | xxd -p)"
CREATED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
SOURCE_TYPE="promise"

sql_escape() { echo "$1" | sed "s/'/''/g"; }

# --- Step 1: turn_seq を取得（インクリメントせずに現在値を基準にする） ---
TURN_SEQ=$(sqlite3 "$DB" "SELECT COALESCE(value,'0') FROM trailmem_meta WHERE key='turn_seq';")
TURN_SEQ="${TURN_SEQ:-0}"
SOURCE_REF="promise:$(date +%Y-%m-%d):${EPISODE_ID}"

# --- Step 2: episode を insert（feeling_intensity高め=1.0, sentiment中立） ---
sqlite3 "$DB" "INSERT INTO episodes (id, summary, inner, quote, sentiment_neg, sentiment_pos, created_at, source_type, source_ref, feeling_intensity, created_turn_seq)
VALUES (
  '$EPISODE_ID',
  '$(sql_escape "$SUMMARY")',
  '$(sql_escape "【約束】$SUMMARY")',
  NULL,
  50, 50,
  '$CREATED_AT', '$SOURCE_TYPE', '$SOURCE_REF',
  1.0, $TURN_SEQ
);"

# --- Step 3: recall_history を構築（現在seqを N 個。フロア計算は要素数のみ参照） ---
RECALL_HISTORY=$(python3 -c "import json,sys; print(json.dumps([int(sys.argv[1])]*int(sys.argv[2])))" "$TURN_SEQ" "$PROMISE_INIT_RECALLS")

# --- Step 4: キーワード集合を構築（「約束」を必ず先頭に） ---
declare -a KW_LIST=("約束")
if [ -n "$KEYWORDS_CSV" ]; then
  IFS=',' read -ra KW_ARRAY <<< "$KEYWORDS_CSV"
  for kw in "${KW_ARRAY[@]}"; do
    kw_normalized=$(echo "$kw" | tr '[:upper:]' '[:lower:]' | xargs)
    [ -z "$kw_normalized" ] && continue
    [ "$kw_normalized" = "約束" ] && continue
    KW_LIST+=("$kw_normalized")
  done
fi

ADDED_KW=""
for kw in "${KW_LIST[@]}"; do
  EXISTS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM keywords WHERE keyword = '$(sql_escape "$kw")';")

  if [ "$kw" = "約束" ]; then
    # 「約束」キーワード: synonyms を多言語セットで作成/マージ
    if [ "$EXISTS" = "0" ]; then
      sqlite3 "$DB" "INSERT INTO keywords (keyword, synonyms, created_at) VALUES ('約束', '$(sql_escape "$PROMISE_SYNONYMS")', '$CREATED_AT');"
    else
      EXISTING_SYN=$(sqlite3 "$DB" "SELECT synonyms FROM keywords WHERE keyword='約束';")
      MERGED=$(python3 -c "
import json,sys
try: a=json.loads(sys.argv[1])
except: a=[]
try: b=json.loads(sys.argv[2])
except: b=[]
seen=[]
for s in a+b:
    if isinstance(s,str) and s.strip() and s not in seen:
        seen.append(s)
print(json.dumps(seen, ensure_ascii=False))
" "${EXISTING_SYN:-[]}" "$PROMISE_SYNONYMS")
      sqlite3 "$DB" "UPDATE keywords SET synonyms='$(sql_escape "$MERGED")' WHERE keyword='約束';"
    fi
  else
    # 指定キーワード: 無ければ空synonymsで作成
    if [ "$EXISTS" = "0" ]; then
      sqlite3 "$DB" "INSERT INTO keywords (keyword, synonyms, created_at) VALUES ('$(sql_escape "$kw")', '[]', '$CREATED_AT');"
    fi
  fi

  # --- episode_keywords リンク ---
  if [ "$kw" = "約束" ]; then
    # 約束リンク: 初期ブースト + 中期記憶 recall_history
    sqlite3 "$DB" "INSERT OR IGNORE INTO episode_keywords
      (episode_id, keyword, base_strength, effective_strength, decay, recall_history, last_recalled, last_recalled_seq)
      VALUES (
        '$EPISODE_ID', '約束',
        $PROMISE_INIT_STRENGTH, $PROMISE_INIT_STRENGTH, 1.0,
        '$(sql_escape "$RECALL_HISTORY")',
        '$CREATED_AT', $TURN_SEQ
      );"
  else
    # 通常リンク: デフォルト強度
    sqlite3 "$DB" "INSERT OR IGNORE INTO episode_keywords (episode_id, keyword) VALUES ('$EPISODE_ID', '$(sql_escape "$kw")');"
  fi

  ADDED_KW="${ADDED_KW}${ADDED_KW:+,}$kw"
done

# --- Step 5: フロア値を算出して報告 ---
FLOOR=$(python3 -c "
R=int('$PROMISE_INIT_RECALLS'); N=$N_CONSOLIDATE; fmin=$FLOOR_MIN
print(f'{min(1.0, fmin + (R/N)*(1.0-fmin)):.3f}')
")

echo "promise registered:"
echo "  episode    = $EPISODE_ID"
echo "  keywords   = $ADDED_KW"
echo "  init_strength = $PROMISE_INIT_STRENGTH (base & effective)"
echo "  recall_history length = $PROMISE_INIT_RECALLS (seq=$TURN_SEQ)"
echo "  floor(R=$PROMISE_INIT_RECALLS, N=$N_CONSOLIDATE) = $FLOOR"
echo "  summary    = $(echo "$SUMMARY" | head -c 60)"
