#!/bin/bash
# trailmem_hook.sh — TrailMem記憶フック
# UserPromptSubmitフックで発動
# ghost_recall.shの後継。ghost依存を全て除去、TrailMem単独で動作

TRAILMEM_DB="${TRAILMEM_DB:-$HOME/.trailmem/trailmem.db}"
TRAILMEM_COUNTER="${TRAILMEM_COUNTER:-/tmp/trailmem-turn-count}"
TRAILMEM_LOG="${TRAILMEM_LOG:-$HOME/.trailmem/trailmem-hook.log}"
TRAILMEM_PID_FILE="${TRAILMEM_PID_FILE:-$HOME/.trailmem/daemon.pid}"
TRAILMEM_SCAN_TIMEOUT="${TRAILMEM_SCAN_TIMEOUT:-6s}"
# scan.sh等はこのファイルと同じディレクトリから解決する(env上書き可)
SCRIPT_DIR="${TRAILMEM_SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
export TRAILMEM_SCRIPT_DIR="$SCRIPT_DIR"
# noiseフィードバックはDBと同じディレクトリに永続化(/tmpは再起動で消えるため)
TRAILMEM_NOISE_FILE="${TRAILMEM_NOISE_FILE:-$(dirname "$TRAILMEM_DB")/noise.txt}"

mkdir -p "$(dirname "$TRAILMEM_LOG")" 2>/dev/null || true
touch "$TRAILMEM_LOG" 2>/dev/null || true

# --- daemon生存チェック ---
DAEMON_STATUS="ok"
if [ -f "$TRAILMEM_PID_FILE" ]; then
  DPID=$(cat "$TRAILMEM_PID_FILE" 2>/dev/null)
  if [ -n "$DPID" ] && ! kill -0 "$DPID" 2>/dev/null; then
    DAEMON_STATUS="WARNING: daemon停止中 (pid=$DPID)"
    echo "[trailmem] WARNING: daemon停止中 (pid=$DPID)" >&2
  fi
else
  DAEMON_STATUS="WARNING: daemon未起動 (pidファイルなし)"
  echo "[trailmem] WARNING: daemon未起動 (pidファイルなし)" >&2
fi

INPUT=$(cat)

PROMPT=$(echo "$INPUT" | python3 -c "
import sys, json
raw = sys.stdin.read()
try:
    data = json.loads(raw)
    print(data.get('prompt', data.get('message', '')))
except:
    print(raw)
" 2>/dev/null)

if [ ${#PROMPT} -lt 15 ]; then
  exit 0
fi

# --- skip: システム通知・task-notification・サブエージェント結果など、
# 「人間の会話入力ではないもの」はscanせず、ログにも出さない。
# 雑なノイズマッチで記憶を汚染しない＆ログを綺麗に保つため。 ---
# <system-reminder>はhookでは弾かない（log-clean.sh側で整形時に除外する）
# hookで弾くのは明確にノイズなもののみ
case "$PROMPT" in
  *"<task-notification>"*|*"<local-command-"*|*"[Request interrupted by user]"*) exit 0 ;;
esac
case "${PROMPT:0:50}" in
  "[SYSTEM]"*|"<task-"*|"<command-"*|"<bash-"*) exit 0 ;;
esac

# --- noise feedback: <noise>episode-id</noise> を検出して記録 ---
echo "$PROMPT" | grep -oP '<noise>\K[^<]+' >> "$TRAILMEM_NOISE_FILE" 2>/dev/null || true

# --- turn_seq: 会話ターンを時間軸の単位として刻む ---
# 以前はエピソード投入時のみ加算されており、クールダウン30が実時間で数日に
# なったり、ingestが止まると時間ごと凍結する問題があった。ここで毎ターン加算する。
# (daemon側のエピソード単位加算は廃止済み。チャンク単位+1のみフォールバックで残る)
sqlite3 "$TRAILMEM_DB" "
  INSERT OR IGNORE INTO trailmem_meta (key, value) VALUES ('turn_seq', '0');
  UPDATE trailmem_meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'turn_seq';
" 2>/dev/null || true

HOOK_START_EPOCH=$(date +%s)
{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') ==="
  echo "[input] $(echo "$PROMPT" | head -3 | cut -c1-200)"
  echo "[daemon] $DAEMON_STATUS"
  echo "[status] started"
} >> "$TRAILMEM_LOG" 2>/dev/null || true

# --- ターンカウント ---
COUNT=$(cat "$TRAILMEM_COUNTER" 2>/dev/null || echo "0")
COUNT=$((COUNT + 1))
echo "$COUNT" > "$TRAILMEM_COUNTER"

# --- micro-decay: 毎ターン道を細くする（フロア方式 / 殿堂入り 統合） ---
# 設計書v2「記憶定着 — フロア方式」:
#   想起回数 R = json_array_length(recall_history)
#   floor(R) = min(1.0, FLOOR_MIN + (R/N_CONSOLIDATE)*(1.0-FLOOR_MIN))
#   通常減衰値 base = (used-misled+1)/(shown+2) * decay
#   effective_strength = max(floor(R), base)   ← フロアより下に沈まない
#   殿堂入り(R≥N_CONSOLIDATE)後は decay を FLOOR_DECAY で（超緩減衰）
# 4パラメータは環境変数で上書き可能。
DECAY_RATE="${TRAILMEM_DECAY_RATE:-0.999}"
N_CONSOLIDATE="${TRAILMEM_N_CONSOLIDATE:-30}"
FLOOR_MIN="${TRAILMEM_FLOOR_MIN:-0.1}"
FLOOR_DECAY="${TRAILMEM_FLOOR_DECAY:-0.99999}"

CUTOFF_MINUTES=$((COUNT * 2))
[ "$CUTOFF_MINUTES" -lt 5 ] && CUTOFF_MINUTES=5
[ "$CUTOFF_MINUTES" -gt 120 ] && CUTOFF_MINUTES=120
sqlite3 "$TRAILMEM_DB" "
  UPDATE episode_keywords
  SET
    -- 想起回数 R に応じて減衰率を選ぶ: 殿堂入り(R>=N)なら FLOOR_DECAY(超緩), それ以外は DECAY_RATE
    decay = MAX(0.01, decay *
      CASE WHEN json_array_length(
             CASE WHEN json_valid(recall_history) THEN recall_history ELSE '[]' END
           ) >= $N_CONSOLIDATE
           THEN $FLOOR_DECAY ELSE $DECAY_RATE END),
    -- effective_strength = max( floor(R), 通常減衰値 )
    effective_strength = MAX(
      -- floor(R)
      MIN(1.0, $FLOOR_MIN + (
        CAST(json_array_length(
          CASE WHEN json_valid(recall_history) THEN recall_history ELSE '[]' END
        ) AS REAL) / $N_CONSOLIDATE) * (1.0 - $FLOOR_MIN)),
      -- 通常減衰値（従来式。decayは上で更新済みの新値を使う）
      (CAST((used - misled + 1) AS REAL) / (shown + 2)) *
        MAX(0.01, decay *
          CASE WHEN json_array_length(
                 CASE WHEN json_valid(recall_history) THEN recall_history ELSE '[]' END
               ) >= $N_CONSOLIDATE
               THEN $FLOOR_DECAY ELSE $DECAY_RATE END)
    )
  WHERE is_deleted = 0
    AND (last_recalled IS NULL OR last_recalled < datetime('now', '-${CUTOFF_MINUTES} minutes'));
" 2>/dev/null || true

# --- scan: キーワード統計 + フラッシュバック ---
SCAN_STATUS=0
SCAN_RESULT=$(timeout "$TRAILMEM_SCAN_TIMEOUT" bash "$SCRIPT_DIR/trailmem-scan.sh" "$PROMPT" 2>/dev/null)
SCAN_STATUS=$?
[ "$SCAN_STATUS" -eq 0 ] || SCAN_RESULT=""
OUTPUT=""
if [ -n "$SCAN_RESULT" ]; then
  OUTPUT="[trailmem] キーワード統計:
$(echo "$SCAN_RESULT" | while read -r line; do echo "  $line"; done)"
  echo ""
  echo "$OUTPUT"
fi

# recallは自動では呼ばない。scanのヒントを見て俺が判断する。
# 手動: bash ./trailmem-recall.sh keyword1 keyword2

# --- ログ出力: ユーザーが注入内容を確認できるように ---
{
  if [ -n "$SCAN_RESULT" ]; then
    echo "[scan] $SCAN_RESULT" | tr '\n' ' '
    echo ""
  elif [ "$SCAN_STATUS" -eq 124 ]; then
    echo "[scan] (timeout after $TRAILMEM_SCAN_TIMEOUT)"
  else
    echo "[scan] (no hit)"
  fi
  echo "[status] done duration=$(( $(date +%s) - HOOK_START_EPOCH ))s"
  echo ""
} >> "$TRAILMEM_LOG" 2>/dev/null || true

# --- チャンク保存+ingestはdaemonに一本化。hookではターンカウントのリセットのみ ---
if [ "$COUNT" -ge 20 ]; then
  echo "0" > "$TRAILMEM_COUNTER"
fi

exit 0
