#!/bin/bash
# trailmem-log-clean.sh — SNS/プレゼン用にtrailmem-hook.logを整形
# システム通知・task-notification・サブエージェント結果を除外し、
# 人間の会話入力 + scan/flashback だけを抽出する
#
# Usage:
#   bash trailmem-log-clean.sh [tail_lines]            # 既定: 全行
#   bash trailmem-log-clean.sh 200                     # 末尾200ブロック
#   bash trailmem-log-clean.sh 200 --html              # HTML出力

set -euo pipefail

LOG="${TRAILMEM_LOG:-$HOME/.trailmem/trailmem-hook.log}"
N="${1:-0}"
FMT="${2:-text}"

python3 - <<PYEOF
import re, sys
log_path = "$LOG"
n = int("$N")
fmt = "$FMT"

# 1ブロック = === 日時 === から次の === までの行群
with open(log_path, "rb") as f:
    raw = f.read()
# 不正バイトをreplace、NEL/CRLFを統一
text = raw.decode("utf-8", errors="replace")
text = text.replace("\x85", "\n").replace("\r\n", "\n")

# === 日時 === でブロックの開始位置を取り、次のヘッダまでがブロック本体
headers = list(re.finditer(r"^=== (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ===\s*$",
                           text, flags=re.MULTILINE))
out = []
for i, h in enumerate(headers):
    ts = h.group(1)
    start = h.end()
    end = headers[i+1].start() if i+1 < len(headers) else len(text)
    body = text[start:end]
    m_in = re.search(r"^\[input\] (.*)$", body, flags=re.MULTILINE)
    if not m_in:
        continue
    inp = m_in.group(1).strip()
    if any(s in inp for s in (
        "[SYSTEM]", "<task-notification>",
        "<local-command-", "[Request interrupted by user]")):
        continue
    if re.match(r"^(<task-|<command-|<bash-)", inp):
        continue
    # system-reminderタグは除去して中身だけ残す（ログとして見えるように）
    inp = re.sub(r"</?system-reminder[^>]*>", "", inp).strip()
    # サブエージェント / Sonnet 呼び出しのプロンプト先頭パターンを除外
    if re.match(r"^(You are |あなたは|## |# |Your task|Task:|Instructions:|Read the |Generate |Return |Output |Extract |Summarize |Analyze |Given |Here is |The following )", inp):
        continue
    # JSON抽出指示や英語の定型タスク指示も除外
    if "JSON" in inp[:80] and any(w in inp[:200] for w in ("schema","array","format","return","output")):
        continue
    if any(p in inp[:120] for p in ("感情温度をJSON", "を判定してください", "を抽出してください", "を要約してください")):
        continue
    m_sc = re.search(r"^\[scan\] (.*)$", body, flags=re.MULTILINE)
    scan = m_sc.group(1).strip() if m_sc else "(no hit)"
    out.append((ts, inp, scan))

if n > 0:
    out = out[-n:]

if fmt == "--html":
    print("<!DOCTYPE html><html><head><meta charset='utf-8'><title>TrailMem (clean)</title>")
    print("<style>body{background:#0d1118;color:#c8ccd4;font:14px/1.6 'SF Mono',monospace;padding:24px;max-width:900px;margin:auto}")
    print(".ts{color:#6a8aa4;font-size:.85em;margin-top:18px}.in{color:#e6edf3}")
    print(".sc{color:#7a8a98;font-size:.9em}.flash{color:#3fb950}.nohit{color:#555}")
    print("</style></head><body>")
    print(f"<h2>TrailMem hook log ({len(out)} clean turns)</h2>")
    for ts, inp, sc in out:
        sc_cls = "nohit" if sc == "(no hit)" else ("flash" if "フラッシュバック" in sc or "💫" in sc else "sc")
        import html
        print(f"<div class='ts'>=== {ts} ===</div>")
        print(f"<div class='in'>[input] {html.escape(inp)[:300]}</div>")
        print(f"<div class='{sc_cls}'>[scan] {html.escape(sc)[:500]}</div>")
    print("</body></html>")
else:
    for ts, inp, sc in out:
        print(f"=== {ts} ===")
        print(f"[input] {inp[:200]}")
        print(f"[scan] {sc[:400]}")
        print()
print(f"# {len(out)} clean turns", file=sys.stderr)
PYEOF
