#!/bin/bash
# trailmem-spread.sh — 活性化拡散による想起（アメーバ網）
#
# 設計書 DESIGN-amoeba.md ② 活性化拡散 + ③ ハブ崩壊対策(2層)
# 1. シードに活性 A=1.0 注入
# 2. 1ホップ伝播: A(隣) += β × weight × A(seed) × penalty(隣)   β=0.35
# 3. 2ホップ伝播: β² で減衰しながら滲む
# 4. ハブ対策:
#    静的: degree penalty = 1/log(1+degree)
#    動的: 側抑制(γ=0.15) + L1正規化
# 5. 活性 ≥ θ_spread(0.2) のノードを返す
# 6. 各ノードのエピソードを effective_strength × 活性 で再ランク
# LLMは呼ばない。SQLite + python のグラフ演算のみ。
#
# Usage: bash trailmem-spread.sh seed1 [seed2 ...]
#        TRAILMEM_NOHUB=1 ...   ハブ対策OFF（A/B比較用）
#        TRAILMEM_DB=/path ...
#        TRAILMEM_SPREAD_JSON=1 ... 機械可読JSON出力（scan統合用）
#                                   {"episodes":[{"id","summary","kw","score","act"}]}

set -euo pipefail

DB="${TRAILMEM_DB:-$HOME/.trailmem/trailmem.db}"
BETA="${TRAILMEM_BETA:-0.35}"
GAMMA="${TRAILMEM_GAMMA:-0.15}"
THETA="${TRAILMEM_THETA:-0.2}"
NOHUB="${TRAILMEM_NOHUB:-0}"
EP_LIMIT="${TRAILMEM_EP_LIMIT:-3}"
JSON="${TRAILMEM_SPREAD_JSON:-0}"

if [ $# -eq 0 ]; then
  echo "Usage: bash trailmem-spread.sh seed1 [seed2 ...]"
  echo "  TRAILMEM_NOHUB=1 でハブ対策OFF（A/B比較）"
  exit 1
fi

python3 - "$DB" "$BETA" "$GAMMA" "$THETA" "$NOHUB" "$EP_LIMIT" "$JSON" "$@" <<'PYEOF'
import sqlite3, sys, math, json
from collections import defaultdict

db, beta, gamma, theta, nohub, ep_limit, json_mode = sys.argv[1:8]
beta = float(beta); gamma = float(gamma); theta = float(theta)
nohub = (nohub == "1"); ep_limit = int(ep_limit); json_mode = (json_mode == "1")
seeds = [s.strip().lower() for s in sys.argv[8:] if s.strip()]

conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row

# グラフ読み込み（隣接リスト）
adj = defaultdict(list)  # node -> [(neighbor, weight)]
degree = defaultdict(int)
for r in conn.execute("SELECT kw_a, kw_b, weight FROM keyword_edges"):
    a, b, w = r["kw_a"], r["kw_b"], r["weight"]
    adj[a].append((b, w))
    adj[b].append((a, w))
    degree[a] += 1
    degree[b] += 1

def penalty(node):
    if nohub:
        return 1.0
    d = degree.get(node, 0)
    return 1.0 / math.log(1 + d) if d > 0 else 1.0

# --- ② 活性化拡散 ---
A = defaultdict(float)
for s in seeds:
    A[s] = 1.0

# 1ホップ: シードから
frontier = {s: 1.0 for s in seeds}
hop1 = defaultdict(float)
for node, act in frontier.items():
    for nb, w in adj.get(node, []):
        hop1[nb] += beta * w * act * penalty(nb)
for nb, inc in hop1.items():
    A[nb] += inc

# 2ホップ: 1ホップで温まったノードから、β² 減衰
hop2 = defaultdict(float)
for node, act in hop1.items():
    for nb, w in adj.get(node, []):
        if nb in seeds:
            continue
        hop2[nb] += (beta ** 2) * w * act * penalty(nb)
for nb, inc in hop2.items():
    A[nb] += inc

# --- ③ ハブ対策 動的: 側抑制 + 正規化 ---
# 側抑制と正規化は「拡散で温まったノード(=シード以外)」の競合に対して効かせる。
# シードは注入点であって競合相手ではないため基準から除外する。
spread_nodes = {n: a for n, a in A.items() if n not in seeds}
if not nohub and spread_nodes:
    a_max = max(spread_nodes.values())
    if a_max > 0:
        # 側抑制: 拡散ノードのうち最大でないものを γ×a_max だけ抑える
        for node in spread_nodes:
            if A[node] < a_max:
                A[node] = max(0.0, A[node] - gamma * a_max)
        # 正規化: 拡散ノードの最大が 1.0 になるよう再スケール（θ判定を安定させる）
        a_max2 = max((A[n] for n in spread_nodes), default=0.0)
        if a_max2 > 0:
            for node in spread_nodes:
                A[node] = A[node] / a_max2

# θ以上のノード
warmed = sorted(
    [(n, a) for n, a in A.items() if a >= theta],
    key=lambda x: -x[1]
)

if not json_mode:
    print(f"=== 温まった部分網 (seeds={seeds}, hub={'OFF' if nohub else 'ON'}) ===")
if not warmed:
    if json_mode:
        print(json.dumps({"episodes": []}, ensure_ascii=False))
    else:
        print("(θ_spread を超えるノードなし)")
    conn.close()
    sys.exit(0)

if not json_mode:
    for node, act in warmed:
        tag = " [seed]" if node in seeds else ""
        print(f"  {node}: {act:.3f}{tag}")

# --- ⑥ エピソード再ランク ---
warmed_nodes = {n: a for n, a in warmed}
node_list = list(warmed_nodes.keys())
placeholders = ",".join("?" for _ in node_list)
ep_score = defaultdict(float)   # ep_id -> max(eff*act)
ep_summary = {}
ep_kw = {}
for r in conn.execute(f"""
    SELECT ek.episode_id, ek.keyword, ek.effective_strength, e.summary
    FROM episode_keywords ek
    JOIN episodes e ON e.id = ek.episode_id
    WHERE ek.keyword IN ({placeholders})
      AND ek.is_deleted = 0
""", node_list):
    act = warmed_nodes[r["keyword"]]
    score = r["effective_strength"] * act
    if score > ep_score[r["episode_id"]]:
        ep_score[r["episode_id"]] = score
        ep_kw[r["episode_id"]] = r["keyword"]
    ep_summary[r["episode_id"]] = r["summary"]

top_eps = sorted(ep_score.items(), key=lambda x: -x[1])[:ep_limit]
if json_mode:
    out = {"episodes": [
        {
            "id": ep_id,
            "summary": ep_summary[ep_id],
            "kw": ep_kw[ep_id],
            "score": round(score, 4),
            "act": round(warmed_nodes[ep_kw[ep_id]], 4),
        }
        for ep_id, score in top_eps
    ]}
    print(json.dumps(out, ensure_ascii=False))
else:
    print(f"\n=== 上位エピソード (effective_strength × 活性) ===")
    for ep_id, score in top_eps:
        print(f"  [{score:.3f}] ({ep_kw[ep_id]}) {ep_summary[ep_id][:80]}")

conn.close()
PYEOF
