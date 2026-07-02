#!/usr/bin/env bash
# trailmem-raw-search.sh — セッションJSONLの生ログ検索
# 用途: 「あの日のあの会話」の原文を辿る
#
# Usage:
#   trailmem-raw-search.sh -k KEYWORD              # キーワード検索
#   trailmem-raw-search.sh -d 2026-06-21            # 日付で絞る
#   trailmem-raw-search.sh -d 2026-06-20..2026-06-21 -k 海  # 日付+キーワード
#   trailmem-raw-search.sh -e EPISODE_ID            # エピソードIDから原文
#   trailmem-raw-search.sh -k KEYWORD -c 5          # 前後5ターン表示
set -euo pipefail

# 検索対象のセッションログディレクトリ(コロン区切りで複数指定可)
IFS=':' read -ra SEARCH_DIRS <<< "${TRAILMEM_SEARCH_DIRS:-$HOME/.claude/projects/-root}"
DB="${TRAILMEM_DB:-$HOME/.trailmem/trailmem.db}"
CONTEXT=2
MAX_RESULTS=10
KEYWORD=""
DATE_FROM=""
DATE_TO=""
EPISODE_ID=""

usage() {
  echo "Usage: $0 [-k keyword] [-d date|date..date] [-e episode_id] [-c context] [-n max]"
  exit 1
}

while getopts "k:d:e:c:n:h" opt; do
  case "$opt" in
    k) KEYWORD="$OPTARG" ;;
    d)
      if [[ "$OPTARG" == *..* ]]; then
        DATE_FROM="${OPTARG%..*}"
        DATE_TO="${OPTARG#*..}"
      else
        DATE_FROM="$OPTARG"
        DATE_TO="$OPTARG"
      fi
      ;;
    e) EPISODE_ID="$OPTARG" ;;
    c) CONTEXT="$OPTARG" ;;
    n) MAX_RESULTS="$OPTARG" ;;
    h) usage ;;
    *) usage ;;
  esac
done

# エピソードIDから原文検索
if [[ -n "$EPISODE_ID" ]]; then
  UUIDS=$(sqlite3 "$DB" "SELECT source_jsonl_uuids FROM episodes WHERE id='$EPISODE_ID'" 2>/dev/null)
  if [[ -z "$UUIDS" || "$UUIDS" == "[]" ]]; then
    echo "Episode '$EPISODE_ID' not found or no source UUIDs."
    echo "---"
    echo "Episode summary:"
    sqlite3 "$DB" "SELECT summary FROM episodes WHERE id='$EPISODE_ID'"
    exit 0
  fi
  echo "=== Episode: $EPISODE_ID ==="
  sqlite3 "$DB" "SELECT summary FROM episodes WHERE id='$EPISODE_ID'"
  echo "---"
  # UUIDリストからJSONLを検索
  echo "$UUIDS" | python3 -c "
import json, sys, glob, os
uuids = set(json.loads(sys.stdin.read()))
if not uuids:
    print('No UUIDs to search'); sys.exit(0)
dirs = ['$HOME/.claude/projects/-root']
files = []
for d in dirs:
    files.extend(glob.glob(os.path.join(d, '*.jsonl')))
    files.extend(glob.glob(os.path.join(d, '*.protected-*.jsonl')))
found = 0
for f in sorted(files, key=os.path.getmtime, reverse=True):
    if found >= len(uuids): break
    with open(f) as fh:
        for line in fh:
            try:
                obj = json.loads(line.strip())
            except: continue
            uid = obj.get('uuid', '')
            if uid in uuids:
                msg = obj.get('message', {})
                role = msg.get('role', '?')
                content = msg.get('content', '')
                text = ''
                if isinstance(content, str): text = content
                elif isinstance(content, list):
                    text = ' '.join(p.get('text','') for p in content if isinstance(p,dict) and p.get('type')=='text')
                if text.strip():
                    print(f'[{role}] {text.strip()[:500]}')
                    print('---')
                    found += 1
" 2>/dev/null
  exit 0
fi

# キーワード・日付検索
collect_files() {
  for dir in "${SEARCH_DIRS[@]}"; do
    [[ -d "$dir" ]] || continue
    find "$dir" -maxdepth 1 -name '*.jsonl' -o -name '*.protected-*.jsonl' 2>/dev/null
  done
}

filter_by_date() {
  if [[ -z "$DATE_FROM" ]]; then
    cat
    return
  fi
  while IFS= read -r f; do
    # protectedファイルは名前に日付を持つ
    if [[ "$f" == *protected-* ]]; then
      fdate=$(echo "$f" | grep -oP '\d{8}' | head -1)
      if [[ -n "$fdate" ]]; then
        fdate_fmt="${fdate:0:4}-${fdate:4:2}-${fdate:6:2}"
        if [[ "$fdate_fmt" > "$DATE_FROM" || "$fdate_fmt" == "$DATE_FROM" ]] && [[ "$fdate_fmt" < "$DATE_TO" || "$fdate_fmt" == "$DATE_TO" ]]; then
          echo "$f"
        fi
        continue
      fi
    fi
    # 通常ファイルはmtimeで判定
    fmtime=$(stat -c %Y "$f" 2>/dev/null || echo 0)
    fdate_fmt=$(date -d "@$fmtime" +%Y-%m-%d 2>/dev/null || echo "")
    if [[ -n "$fdate_fmt" ]] && [[ "$fdate_fmt" > "$DATE_FROM" || "$fdate_fmt" == "$DATE_FROM" ]] && [[ "$fdate_fmt" < "$DATE_TO" || "$fdate_fmt" == "$DATE_TO" ]]; then
      echo "$f"
    fi
  done
}

if [[ -z "$KEYWORD" && -z "$DATE_FROM" ]]; then
  echo "Specify at least -k (keyword) or -d (date)."
  usage
fi

FILES=$(collect_files | filter_by_date | sort)
if [[ -z "$FILES" ]]; then
  echo "No matching files found."
  exit 0
fi

echo "$FILES" | python3 -c "
import json, sys

keyword = '$KEYWORD'.lower()
context = int('$CONTEXT')
max_results = int('$MAX_RESULTS')
files = [l.strip() for l in sys.stdin if l.strip()]
results = 0

for f in files:
    lines = []
    with open(f) as fh:
        for line in fh:
            line = line.strip()
            if not line: continue
            try:
                obj = json.loads(line)
            except: continue
            msg = obj.get('message', {})
            role = msg.get('role', '')
            if role not in ('user', 'assistant'): continue
            content = msg.get('content', '')
            text = ''
            if isinstance(content, str): text = content
            elif isinstance(content, list):
                text = ' '.join(p.get('text','') for p in content if isinstance(p,dict) and p.get('type')=='text')
            text = text.strip()
            if text:
                lines.append((role, text))

    if not keyword:
        # 日付のみ: 先頭と末尾を表示
        if lines:
            import os
            fname = os.path.basename(f)
            print(f'=== {fname} ({len(lines)} turns) ===')
            for role, text in lines[:3]:
                print(f'[{role}] {text[:200]}')
            if len(lines) > 6:
                print(f'  ... ({len(lines)-6} more turns) ...')
            for role, text in lines[-3:]:
                print(f'[{role}] {text[:200]}')
            print('---')
            results += 1
            if results >= max_results: break
        continue

    # キーワード検索
    for i, (role, text) in enumerate(lines):
        if keyword in text.lower():
            import os
            fname = os.path.basename(f)
            print(f'=== {fname} turn {i+1}/{len(lines)} ===')
            start = max(0, i - context)
            end = min(len(lines), i + context + 1)
            for j in range(start, end):
                marker = '>>>' if j == i else '   '
                r, t = lines[j]
                print(f'{marker} [{r}] {t[:300]}')
            print('---')
            results += 1
            if results >= max_results: break
    if results >= max_results: break
" 2>/dev/null
