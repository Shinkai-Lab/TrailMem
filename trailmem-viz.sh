#!/bin/bash
# trailmem-viz.sh — TrailMem 記憶網ビュワー (キーワード×エピソードの3Dシナプスビュー)
#
# 「人間のシナプスみたいな3D情報網」を、keywords/episode_keywords/keyword_edges から
# 単一の自己完結HTMLとして書き出す。生成されたHTMLは file:// で直接開くだけで動く
# (サーバ不要)。ただしグラフ描画エンジンの3d-force-graphはCDN(unpkg)から読み込むため、
# ★オフライン環境ではグラフ本体は表示できない★ (統計ヘッダやデータ自体は正常に埋め込まれる)。
#
# Usage:
#   TRAILMEM_DB=/path/to/trailmem.db bash trailmem-viz.sh [出力パス.html]
#   (出力パスのデフォルトは ./trailmem-viz.html)
#
# 可視化の対応関係:
#   ノード = keywords (サイズ=liveリンク数/次数, 色: 殿堂入りリンクを持つ=金 / axis='theme'=紫 / 通常=青)
#   エッジ = keyword_edges (太さ・色の明るさ = weight)
#   クリック         → サイドパネルにそのキーワードのエピソード一覧 (effective_strength降順)
#   検索ボックス      → キーワード部分一致でハイライト+カメラ移動
#   統計ヘッダ        → episodes/keywords/links/edges数、強度帯分布、殿堂入り件数
#
# 「リンク」という語はこのTrailMem一式内での用法に合わせている: 1本の episode_keywords
# 行 (episode×keywordの結びつき) を指す。keyword_edges の1行は「エッジ」と呼んで区別する。
# 殿堂入り = そのキーワードが R (recall_history の長さ) >= TRAILMEM_N_CONSOLIDATE の
# リンクを1本以上持つこと (trailmem-doctor.sh の「殿堂入りレビュー」と同じ定義/しきい値)。
#
# 環境変数:
#   TRAILMEM_DB                    DBパス (default $HOME/.trailmem/trailmem.db)
#   TRAILMEM_N_CONSOLIDATE         殿堂入り判定の想起回数しきい値 (default 30, doctor.shと共通)
#   TRAILMEM_VIZ_MIN_EDGE_WEIGHT   このweight未満のkeyword_edgesを間引く (default 0.05)
#
# 強度帯 (trailmem-doctor.sh / trailmem-recall.sh と同じ定義):
#   recall帯 (effective_strength >= 0.5) / deep帯 (0.2-0.5) / dig帯 (< 0.2)
#
# 強度の手動調整:
#   bash trailmem-doctor.sh set-strength <episode_id> <keyword> <value>
#   (このヒントはビュワーのサイドパネルにも表示される)
#
# このスクリプトはDBを読み取り専用で開く。書き込みは一切行わない。

set -euo pipefail

DB="${TRAILMEM_DB:-$HOME/.trailmem/trailmem.db}"
OUT="${1:-./trailmem-viz.html}"
N_CONSOLIDATE="${TRAILMEM_N_CONSOLIDATE:-30}"
MIN_EDGE_WEIGHT="${TRAILMEM_VIZ_MIN_EDGE_WEIGHT:-0.05}"

if [ ! -f "$DB" ]; then
  echo "✗ DBが見つかりません: $DB" >&2
  echo "  TRAILMEM_DB=/path/to/trailmem.db bash trailmem-viz.sh [出力パス.html]" >&2
  exit 1
fi

python3 - "$DB" "$OUT" "$N_CONSOLIDATE" "$MIN_EDGE_WEIGHT" <<'PYEOF'
import json
import math
import os
import sqlite3
import sys
from datetime import datetime, timezone

db_path, out_path, n_consolidate_raw, min_edge_weight_raw = sys.argv[1:5]
n_consolidate = int(n_consolidate_raw)
min_edge_weight = float(min_edge_weight_raw)

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
# 安全策: このプロセスからは絶対に書き込まない (query_only はURIのmode=roと違い
# WAL用の-shmファイル書き込み権限を要求しないため、コピー先ディレクトリの権限に
# 依存せず安全にreadonly強制できる)
conn.execute("PRAGMA query_only = 1;")

# ------------------------------------------------------------------
# 1. keywords テーブル (axis列は存在する場合のみ使う)
# ------------------------------------------------------------------
kw_cols = {row[1] for row in conn.execute("PRAGMA table_info(keywords)")}
has_axis = "axis" in kw_cols

if has_axis:
    kw_rows = conn.execute("SELECT keyword, axis FROM keywords").fetchall()
    axis_by_keyword = {r["keyword"]: (r["axis"] or "") for r in kw_rows}
else:
    kw_rows = conn.execute("SELECT keyword FROM keywords").fetchall()
    axis_by_keyword = {}

all_keywords_from_table = {r["keyword"] for r in kw_rows}

# ------------------------------------------------------------------
# 2. live な episode_keywords (= 「リンク」) を keyword ごとにグルーピング
#    episodes と INNER JOIN するので、orphan (episodesに実体がない) リンクは
#    自然に除外される → ノードの次数(degree)とパネルのエピソード件数が必ず一致する
# ------------------------------------------------------------------
link_rows = conn.execute(
    """
    SELECT ek.keyword AS keyword,
           ek.episode_id AS episode_id,
           ek.effective_strength AS strength,
           CASE WHEN json_valid(ek.recall_history)
                THEN json_array_length(ek.recall_history)
                ELSE 0 END AS r_count,
           e.summary AS summary,
           e.created_at AS created_at
    FROM episode_keywords ek
    JOIN episodes e ON e.id = ek.episode_id
    WHERE ek.is_deleted = 0
    ORDER BY ek.keyword ASC, ek.effective_strength DESC
    """
).fetchall()

episodes_by_keyword = {}
for row in link_rows:
    kw = row["keyword"]
    summary = row["summary"] or ""
    summary80 = summary[:80] + ("…" if len(summary) > 80 else "")
    date = (row["created_at"] or "")[:10]
    is_hof = row["r_count"] >= n_consolidate
    episodes_by_keyword.setdefault(kw, []).append({
        "id": row["episode_id"],
        "summary": summary80,
        "strength": round(row["strength"], 3),
        "R": row["r_count"],
        "date": date,
        "hof": is_hof,
    })

# ------------------------------------------------------------------
# 3. ノード集合を確定する。keywords テーブル ∪ live リンクに出てくるkeyword
#    (dangling: keywordsテーブルに実体がないが episode_keywords に残っているもの
#    も、実データとして存在するので描画対象に含める)
# ------------------------------------------------------------------
node_ids = all_keywords_from_table | set(episodes_by_keyword.keys())

nodes = []
hof_link_total = 0
for kw in sorted(node_ids):
    eps = episodes_by_keyword.get(kw, [])
    degree = len(eps)
    has_hof = any(e["hof"] for e in eps)
    hof_link_total += sum(1 for e in eps if e["hof"])
    axis_val = axis_by_keyword.get(kw, "")
    is_theme = has_axis and axis_val == "theme"

    if has_hof:
        color = "#ffd54f"       # 金: 殿堂入りリンクを持つ
        color_class = "gold"
    elif is_theme:
        color = "#c88cff"       # 紫: axis='theme'
        color_class = "purple"
    else:
        color = "#63d6ff"       # 青: 通常
        color_class = "blue"

    nodes.append({
        "id": kw,
        "label": kw,
        "degree": degree,
        "hof": has_hof,
        "axis": axis_val if has_axis else None,
        "axisTheme": is_theme,
        "color": color,
        "colorClass": color_class,
        "val": max(1, degree),
        "episodes": eps,
    })

# ------------------------------------------------------------------
# 4. エッジ (keyword_edges) — weight が閾値未満のものは間引く
# ------------------------------------------------------------------
edge_rows = conn.execute(
    "SELECT kw_a, kw_b, weight, co_count FROM keyword_edges"
).fetchall()

edges_total = len(edge_rows)
edges = []
for r in edge_rows:
    if r["weight"] < min_edge_weight:
        continue
    # keyword_edges の端点がノード集合に無いケースを防御 (通常は起こらないはずだが、
    # 手動編集されたDBなどで欠落していても描画が壊れないようにする)
    if r["kw_a"] not in node_ids or r["kw_b"] not in node_ids:
        continue
    edges.append({
        "source": r["kw_a"],
        "target": r["kw_b"],
        "weight": r["weight"],
        "coCount": r["co_count"],
    })

edges_rendered = len(edges)
edges_pruned = edges_total - edges_rendered

# ------------------------------------------------------------------
# 5. 強度帯分布・殿堂入り件数 (trailmem-doctor.sh の「強度分布」「殿堂入りレビュー」と同じ定義)
# ------------------------------------------------------------------
band_row = conn.execute(
    """
    SELECT
      SUM(CASE WHEN effective_strength >= 0.5 THEN 1 ELSE 0 END) AS recall,
      SUM(CASE WHEN effective_strength >= 0.2 AND effective_strength < 0.5 THEN 1 ELSE 0 END) AS deep,
      SUM(CASE WHEN effective_strength < 0.2 THEN 1 ELSE 0 END) AS dig,
      COUNT(*) AS total
    FROM episode_keywords WHERE is_deleted = 0
    """
).fetchone()
strength_bands = {
    "recall": band_row["recall"] or 0,
    "deep": band_row["deep"] or 0,
    "dig": band_row["dig"] or 0,
    "total": band_row["total"] or 0,
}

episodes_total = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
links_total = conn.execute(
    "SELECT COUNT(*) FROM episode_keywords WHERE is_deleted = 0"
).fetchone()[0]

conn.close()

# ------------------------------------------------------------------
# 6. 「常時ラベル表示」にするハブキーワードのしきい値を決める。
#    データ規模に関わらずラベルが散乱しすぎないよう、常時表示ノード数が
#    HUB_LABEL_CAP件を超えない最小の次数しきい値を選ぶ (下限5)。
# ------------------------------------------------------------------
HUB_LABEL_CAP = 150
degree_list = [n["degree"] for n in nodes]
label_always_degree = 5
if degree_list:
    uniq_desc = sorted(set(degree_list), reverse=True)
    best = uniq_desc[0]
    for d in uniq_desc:
        cnt = sum(1 for x in degree_list if x >= d)
        if cnt <= HUB_LABEL_CAP:
            best = d
        else:
            break
    label_always_degree = max(5, best)

meta = {
    "generatedAt": datetime.now(timezone.utc).isoformat(),
    "dbFile": os.path.basename(db_path),
    "nConsolidate": n_consolidate,
    "minEdgeWeight": min_edge_weight,
    "hasAxisColumn": has_axis,
    "labelAlwaysDegree": label_always_degree,
    "counts": {
        "episodes": episodes_total,
        "keywordsNodes": len(nodes),
        "linksLive": links_total,
        "edgesTotal": edges_total,
        "edgesRendered": edges_rendered,
        "edgesPruned": edges_pruned,
    },
    "strengthBands": strength_bands,
    "hallOfFameLinks": hof_link_total,
}

payload = {"meta": meta, "nodes": nodes, "edges": edges}

# JSONを<script>内に安全に埋め込むため "</" を "<\/" にエスケープしておく
# (episodeのsummaryなどに "</script" という文字列が万一含まれていても
#  HTMLパーサがタグを閉じたと誤認しないようにするための定番の対策)
json_text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
json_text_safe = json_text.replace("</", "<\\/")

# ==================================================================
# HTMLテンプレート
# ==================================================================
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TrailMem 記憶網ビュワー</title>
<!--
  TrailMem 記憶網ビュワー (自己完結HTML / file://で直接開けます)

  ★重要: このHTMLはグラフ描画に 3d-force-graph (unpkg CDN) を読み込みます。
         オフライン環境ではグラフ本体(3D球体+線)は表示されません。
         統計ヘッダとサイドパネル用のデータ自体はこのファイル内に完全に
         埋め込まれているので、CDNが読めなくても壊れているわけではありません。
  CDN: https://unpkg.com/3d-force-graph@1.80.0/dist/3d-force-graph.min.js
-->
<style>
:root {
  --bg: #05070a;
  --panel-bg: #0d1219;
  --card: #131920;
  --border: #1e2a35;
  --accent: #63d6ff;
  --warn: #ff7676;
  --ok: #7deb9a;
  --watch: #ffc85c;
  --gold: #ffd54f;
  --purple: #c88cff;
  --text: #e0dcd6;
  --dim: #8b9bb0;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body { background: var(--bg); color: var(--text); font: 13px/1.5 'Inter', system-ui, -apple-system, "Hiragino Sans", "Noto Sans JP", sans-serif; overflow: hidden; width: 100%; height: 100%; }

#graph { position: fixed; inset: 0; z-index: 0; }

#label-layer { position: fixed; inset: 0; z-index: 5; pointer-events: none; overflow: hidden; }
.hub-label {
  position: absolute; left: 0; top: 0;
  transform: translate(-50%, -160%);
  color: #cfe9ff; font-size: 11px; font-weight: 600;
  text-shadow: 0 0 4px rgba(0,0,0,0.9), 0 0 8px rgba(0,0,0,0.7);
  white-space: nowrap; pointer-events: none; letter-spacing: 0.02em;
}

#header {
  position: fixed; top: 0; left: 0; right: 0; z-index: 10;
  background: linear-gradient(180deg, rgba(5,7,10,0.96) 0%, rgba(5,7,10,0.85) 70%, rgba(5,7,10,0) 100%);
  padding: 12px 16px 24px;
  display: flex; flex-wrap: wrap; align-items: center; gap: 14px;
  pointer-events: none;
}
#header * { pointer-events: auto; }
#title { font-size: 1.05em; font-weight: 700; color: var(--accent); letter-spacing: 0.02em; white-space: nowrap; }
#title small { display: block; font-size: 0.65em; font-weight: 400; color: var(--dim); }

.stat-chip {
  background: rgba(19,25,32,0.85); border: 1px solid var(--border); border-radius: 8px;
  padding: 5px 10px; text-align: center; min-width: 58px;
}
.stat-chip .num { font-size: 1.05em; font-weight: 700; color: var(--accent); line-height: 1.1; }
.stat-chip .label { font-size: 0.68em; color: var(--dim); white-space: nowrap; }
.stat-chip.gold .num { color: var(--gold); }

#band-bar { display: flex; height: 18px; width: 220px; border-radius: 4px; overflow: hidden; border: 1px solid var(--border); }
#band-bar div { height: 100%; }
#band-bar .b-recall { background: var(--ok); }
#band-bar .b-deep { background: var(--accent); }
#band-bar .b-dig { background: var(--watch); }
#band-legend { font-size: 0.68em; color: var(--dim); display: flex; gap: 8px; margin-top: 3px; }
#band-legend span.sw { display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin-right: 3px; vertical-align: -1px; }

#search-wrap { position: relative; margin-left: auto; }
#search-box {
  background: rgba(13,18,25,0.9); border: 1px solid var(--border); color: var(--text);
  border-radius: 8px; padding: 7px 12px; font-size: 0.9em; width: 220px; outline: none;
}
#search-box:focus { border-color: var(--accent); }
#search-result { font-size: 0.7em; color: var(--dim); margin-top: 3px; }

#legend-wrap { display: flex; gap: 10px; align-items: center; font-size: 0.7em; color: var(--dim); }
#legend-wrap .sw { width: 9px; height: 9px; border-radius: 50%; display: inline-block; margin-right: 4px; vertical-align: -1px; }
#legend-title { cursor: pointer; user-select: none; font-weight: 600; }
#legend-wrap.collapsed .legend-item { display: none; }

#label-toggle { display: flex; align-items: center; gap: 5px; font-size: 0.72em; color: var(--dim); white-space: nowrap; }

#lang-toggle {
  position: fixed; top: 10px; right: 16px; z-index: 15;
  display: flex; gap: 2px; background: rgba(13,18,25,0.85); border: 1px solid var(--border);
  border-radius: 8px; padding: 3px;
}
#lang-toggle button {
  background: none; border: none; color: var(--dim); font-size: 0.72em; font-weight: 700;
  padding: 4px 10px; border-radius: 6px; cursor: pointer;
}
#lang-toggle button.active { background: var(--accent); color: #05070a; }

#side-panel {
  position: fixed; top: 0; right: -420px; width: 400px; height: 100%;
  background: var(--panel-bg); border-left: 1px solid var(--border);
  z-index: 20; transition: right 0.25s ease; overflow-y: auto;
  box-shadow: -12px 0 40px rgba(0,0,0,0.5);
}
#side-panel.open { right: 0; }
#panel-inner { padding: 20px 18px 40px; }
#panel-close {
  position: absolute; top: 12px; right: 14px; background: none; border: none;
  color: var(--dim); font-size: 1.3em; cursor: pointer; line-height: 1;
}
#panel-close:hover { color: var(--text); }
#panel-title { font-size: 1.25em; font-weight: 700; color: var(--text); margin-bottom: 2px; word-break: break-word; padding-right: 24px; }
#panel-badges { display: flex; gap: 6px; margin: 8px 0 4px; flex-wrap: wrap; }
.badge { font-size: 0.7em; padding: 2px 8px; border-radius: 10px; border: 1px solid var(--border); color: var(--dim); }
.badge.gold { color: var(--gold); border-color: var(--gold); }
.badge.purple { color: var(--purple); border-color: var(--purple); }
.badge.blue { color: var(--accent); border-color: var(--accent); }
#panel-hint {
  margin: 12px 0 16px; padding: 8px 10px; background: rgba(255,255,255,0.04);
  border-left: 3px solid var(--watch); border-radius: 4px; font-size: 0.75em; color: var(--dim);
}
#panel-hint code { color: var(--watch); font-family: monospace; word-break: break-all; }
#panel-episodes-title { font-size: 0.8em; color: var(--dim); margin: 14px 0 8px; text-transform: uppercase; letter-spacing: 0.05em; }
.ep-row { border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; margin-bottom: 8px; background: rgba(255,255,255,0.02); }
.ep-head { display: flex; align-items: baseline; gap: 8px; margin-bottom: 4px; flex-wrap: wrap; }
.ep-strength { font-family: monospace; color: var(--accent); font-weight: 700; }
.ep-r { font-size: 0.75em; color: var(--dim); }
.ep-date { font-size: 0.75em; color: var(--dim); margin-left: auto; }
.ep-hof { font-size: 0.95em; }
.ep-summary { font-size: 0.85em; color: var(--text); line-height: 1.5; word-break: break-word; }
.ep-id { font-size: 0.65em; color: var(--dim); margin-top: 4px; word-break: break-all; opacity: 0.6; }
.empty-note { color: var(--dim); font-size: 0.85em; padding: 10px 0; }

#footer-hint {
  position: fixed; bottom: 10px; left: 16px; z-index: 10; font-size: 0.68em; color: var(--dim);
  background: rgba(13,18,25,0.7); padding: 4px 8px; border-radius: 6px; pointer-events: none;
}

::-webkit-scrollbar { width: 8px; }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
</style>
</head>
<body>

<div id="graph"></div>
<div id="label-layer"></div>

<div id="header">
  <div id="title"><span id="title-text">TrailMem 記憶網ビュワー</span><small id="db-file-note">-</small></div>

  <div class="stat-chip"><div class="num" id="stat-episodes">-</div><div class="label">episodes</div></div>
  <div class="stat-chip"><div class="num" id="stat-keywords">-</div><div class="label">keywords</div></div>
  <div class="stat-chip"><div class="num" id="stat-links">-</div><div class="label">links</div></div>
  <div class="stat-chip"><div class="num" id="stat-edges">-</div><div class="label">edges</div></div>
  <div class="stat-chip gold"><div class="num" id="stat-hof">-</div><div class="label" id="stat-hof-label">殿堂入り</div></div>

  <div>
    <div id="band-bar">
      <div class="b-recall" id="band-recall"></div>
      <div class="b-deep" id="band-deep"></div>
      <div class="b-dig" id="band-dig"></div>
    </div>
    <div id="band-legend">
      <span><span class="sw" style="background:var(--ok)"></span>recall</span>
      <span><span class="sw" style="background:var(--accent)"></span>deep</span>
      <span><span class="sw" style="background:var(--watch)"></span>dig</span>
    </div>
  </div>

  <div id="legend-wrap">
    <span id="legend-title" onclick="document.getElementById('legend-wrap').classList.toggle('collapsed')">凡例</span>
    <span class="legend-item"><span class="sw" style="background:var(--gold)"></span><span id="legend-hof-text">殿堂入りリンク保持</span></span>
    <span class="legend-item" id="legend-purple"><span class="sw" style="background:var(--purple)"></span><span id="legend-theme-text">axis:theme</span></span>
    <span class="legend-item"><span class="sw" style="background:var(--accent)"></span><span id="legend-normal-text">通常</span></span>
  </div>

  <label id="label-toggle"><input type="checkbox" id="hub-label-checkbox" checked> <span id="hub-label-text">ハブラベル常時表示</span></label>

  <div id="search-wrap">
    <input id="search-box" type="text" placeholder="キーワードで検索…" autocomplete="off">
    <div id="search-result"></div>
  </div>
</div>

<div id="lang-toggle">
  <button type="button" id="lang-btn-ja" data-lang="ja">JA</button>
  <button type="button" id="lang-btn-en" data-lang="en">EN</button>
</div>

<div id="footer-hint"><span id="footer-hint-text">ドラッグ=回転 / スクロール=ズーム / クリック=詳細 / 背景クリック=閉じる</span></div>

<div id="side-panel">
  <div id="panel-inner">
    <button id="panel-close" title="閉じる">×</button>
    <div id="panel-title">-</div>
    <div id="panel-badges"></div>
    <div id="panel-hint">
      <span id="panel-hint-text">強度の手動調整は:</span><br>
      <code>bash trailmem-doctor.sh set-strength &lt;episode_id&gt; &lt;keyword&gt; &lt;value&gt;</code>
    </div>
    <div id="panel-episodes-title">紐づくエピソード (強度降順)</div>
    <div id="panel-episodes"></div>
  </div>
</div>

<script type="application/json" id="trailmem-data">__TRAILMEM_JSON__</script>

<!--
  3d-force-graph は必須CDN依存です。オフラインだとここで失敗し、グラフ領域が
  空白のままになります(ヘッダの統計自体はJSONから取れているはずなので表示されます)。
-->
<script src="https://unpkg.com/3d-force-graph@1.80.0/dist/3d-force-graph.min.js"></script>
<script>
(function () {
  "use strict";

  var DATA = JSON.parse(document.getElementById('trailmem-data').textContent);
  var META = DATA.meta;
  var NODES = DATA.nodes;
  var EDGES = DATA.edges;

  // ---- i18n (JA/EN) ----
  var I18N = {
    ja: {
      pageTitle: 'TrailMem 記憶網ビュワー',
      title: 'TrailMem 記憶網ビュワー',
      statHof: '殿堂入り',
      legendTitle: '凡例',
      legendHof: '殿堂入りリンク保持',
      legendTheme: 'axis:theme',
      legendNormal: '通常',
      hubLabelToggle: 'ハブラベル常時表示',
      searchPlaceholder: 'キーワードで検索…',
      footerHint: 'ドラッグ=回転 / スクロール=ズーム / クリック=詳細 / 背景クリック=閉じる',
      panelClose: '閉じる',
      panelHintPrefix: '強度の手動調整は:',
      panelEpisodesTitle: '紐づくエピソード (強度降順)',
      emptyNote: 'このキーワードに紐づくliveなエピソードはありません。',
      badgeDegree: '次数',
      badgeHof: '👑 殿堂入りリンク保持',
      badgeTheme: 'axis: theme',
      badgeNormal: '通常',
      nodeHof: '👑殿堂入り',
      edgePruned: '間引き',
      matches: '件マッチ',
      bandTotal: '合計',
      axisNote: 'keywordsテーブルにaxis列がないため今回は未使用（全キーワードが通常/殿堂入り扱い）',
      epHofTitle: '殿堂入り (R>='
    },
    en: {
      pageTitle: 'TrailMem Memory Web Viewer',
      title: 'TrailMem Memory Web Viewer',
      statHof: 'Hall of Fame',
      legendTitle: 'Legend',
      legendHof: 'Holds hall-of-fame link',
      legendTheme: 'axis:theme',
      legendNormal: 'Normal',
      hubLabelToggle: 'Always show hub labels',
      searchPlaceholder: 'Search keywords…',
      footerHint: 'Drag = rotate / Scroll = zoom / Click = details / Click background = close',
      panelClose: 'Close',
      panelHintPrefix: 'To manually adjust strength:',
      panelEpisodesTitle: 'Linked episodes (by strength)',
      emptyNote: 'No live episodes are linked to this keyword.',
      badgeDegree: 'Degree',
      badgeHof: '👑 Holds hall-of-fame link',
      badgeTheme: 'axis: theme',
      badgeNormal: 'Normal',
      nodeHof: '👑 Hall of Fame',
      edgePruned: 'pruned',
      matches: ' matches',
      bandTotal: 'total',
      axisNote: 'No axis column in the keywords table, so this is unused for this run (all keywords are treated as normal/hall-of-fame).',
      epHofTitle: 'Hall of fame (R>='
    }
  };
  var LANG = (navigator.language || '').toLowerCase().indexOf('ja') === 0 ? 'ja' : 'en';
  function t(key) {
    return (I18N[LANG] && I18N[LANG][key] != null) ? I18N[LANG][key] : key;
  }
  var STATIC_TEXT_MAP = {
    'title-text': 'title', 'stat-hof-label': 'statHof', 'legend-title': 'legendTitle',
    'legend-hof-text': 'legendHof', 'legend-theme-text': 'legendTheme', 'legend-normal-text': 'legendNormal',
    'hub-label-text': 'hubLabelToggle', 'footer-hint-text': 'footerHint',
    'panel-hint-text': 'panelHintPrefix', 'panel-episodes-title': 'panelEpisodesTitle'
  };

  // ---- ヘッダ統計を埋める (言語非依存の部分) ----
  document.getElementById('db-file-note').textContent = META.dbFile + ' / ' + META.generatedAt.slice(0, 19).replace('T', ' ') + ' UTC';
  document.getElementById('stat-episodes').textContent = META.counts.episodes;
  document.getElementById('stat-keywords').textContent = META.counts.keywordsNodes;
  document.getElementById('stat-links').textContent = META.counts.linksLive;
  document.getElementById('stat-hof').textContent = META.hallOfFameLinks;

  var bands = META.strengthBands;
  var bandTotal = Math.max(1, bands.total);
  document.getElementById('band-recall').style.width = (bands.recall / bandTotal * 100) + '%';
  document.getElementById('band-deep').style.width = (bands.deep / bandTotal * 100) + '%';
  document.getElementById('band-dig').style.width = (bands.dig / bandTotal * 100) + '%';

  function updateEdgeStat() {
    var edgeLabel = META.counts.edgesRendered;
    if (META.counts.edgesPruned > 0) {
      edgeLabel += ' (' + t('edgePruned') + ' ' + META.counts.edgesPruned + '/' + META.counts.edgesTotal + ')';
    }
    document.getElementById('stat-edges').textContent = edgeLabel;
  }

  function updateBandTitle() {
    document.getElementById('band-bar').title =
      'recall(>=0.5)=' + bands.recall + ' / deep(0.2-0.5)=' + bands.deep + ' / dig(<0.2)=' + bands.dig + ' / ' + t('bandTotal') + '=' + bands.total;
  }

  function updateAxisNote() {
    if (!META.hasAxisColumn) {
      var lp = document.getElementById('legend-purple');
      lp.style.opacity = '0.35';
      lp.title = t('axisNote');
    }
  }

  // ---- HTMLエスケープ ----
  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  // ---- サイドパネル ----
  var sidePanel = document.getElementById('side-panel');
  var panelTitle = document.getElementById('panel-title');
  var panelBadges = document.getElementById('panel-badges');
  var panelEpisodes = document.getElementById('panel-episodes');

  function badgeHtml(node) {
    var out = '';
    out += '<span class="badge">' + t('badgeDegree') + ' ' + node.degree + '</span>';
    if (node.hof) out += '<span class="badge gold">' + t('badgeHof') + '</span>';
    if (node.axisTheme) out += '<span class="badge purple">' + t('badgeTheme') + '</span>';
    if (!node.hof && !node.axisTheme) out += '<span class="badge blue">' + t('badgeNormal') + '</span>';
    return out;
  }

  var currentPanelNode = null;

  function openPanel(node) {
    currentPanelNode = node;
    panelTitle.textContent = node.label;
    panelBadges.innerHTML = badgeHtml(node);
    if (!node.episodes.length) {
      panelEpisodes.innerHTML = '<div class="empty-note">' + escapeHtml(t('emptyNote')) + '</div>';
    } else {
      var html = '';
      node.episodes.forEach(function (ep) {
        html += '<div class="ep-row">';
        html += '<div class="ep-head">';
        html += '<span class="ep-strength">' + ep.strength.toFixed(3) + '</span>';
        if (ep.hof) html += '<span class="ep-hof" title="' + escapeHtml(t('epHofTitle') + META.nConsolidate + ')') + '">👑</span>';
        html += '<span class="ep-r">R=' + ep.R + '</span>';
        html += '<span class="ep-date">' + escapeHtml(ep.date || '-') + '</span>';
        html += '</div>';
        html += '<div class="ep-summary">' + escapeHtml(ep.summary) + '</div>';
        html += '<div class="ep-id">id: ' + escapeHtml(ep.id) + '</div>';
        html += '</div>';
      });
      panelEpisodes.innerHTML = html;
    }
    sidePanel.classList.add('open');
  }

  function closePanel() {
    currentPanelNode = null;
    sidePanel.classList.remove('open');
  }
  document.getElementById('panel-close').addEventListener('click', closePanel);

  // ---- グラフ本体 ----
  var elGraph = document.getElementById('graph');
  var highlightSet = new Set();

  var weights = EDGES.length ? EDGES.map(function (e) { return e.weight; }) : [0, 1];
  var minW = Math.min.apply(null, weights);
  var maxW = Math.max.apply(null, weights);
  function normW(w) {
    return maxW > minW ? (w - minW) / (maxW - minW) : 0.5;
  }
  function widthScale(w) {
    return 0.4 + normW(w) * 2.6;
  }
  var EDGE_COLD = [42, 59, 77];   // 弱いリンク: くすんだ紺
  var EDGE_HOT = [126, 232, 255]; // 強いリンク: 明るいシアン
  function colorScale(w) {
    var t = normW(w);
    var r = Math.round(EDGE_COLD[0] + (EDGE_HOT[0] - EDGE_COLD[0]) * t);
    var g = Math.round(EDGE_COLD[1] + (EDGE_HOT[1] - EDGE_COLD[1]) * t);
    var b = Math.round(EDGE_COLD[2] + (EDGE_HOT[2] - EDGE_COLD[2]) * t);
    return 'rgb(' + r + ',' + g + ',' + b + ')';
  }

  var Graph = ForceGraph3D()(elGraph)
    .graphData({ nodes: NODES, links: EDGES })
    .backgroundColor('#05070a')
    .showNavInfo(false)
    .nodeLabel(function (n) {
      return escapeHtml(n.label) + ' (' + t('badgeDegree') + ' ' + n.degree + ')' + (n.hof ? ' ' + t('nodeHof') : '');
    })
    .nodeColor(function (n) { return highlightSet.has(n.id) ? '#ffffff' : n.color; })
    .nodeVal(function (n) { return n.val; })
    .nodeRelSize(3)
    .nodeResolution(8)
    .nodeOpacity(0.92)
    .linkWidth(function (l) { return widthScale(l.weight); })
    .linkColor(function (l) { return colorScale(l.weight); })
    .linkOpacity(0.45)
    .linkCurvature(0.15)
    .linkDirectionalParticles(function (l) {
      var s = typeof l.source === 'object' ? l.source : null;
      var t = typeof l.target === 'object' ? l.target : null;
      var srcHof = s ? s.hof : false;
      var tgtHof = t ? t.hof : false;
      return (srcHof || tgtHof) ? 2 : 0;
    })
    .linkDirectionalParticleWidth(1.6)
    .linkDirectionalParticleSpeed(0.004)
    .linkDirectionalParticleColor(function () { return '#ffe9a8'; })
    .onNodeClick(function (n) { openPanel(n); })
    .onBackgroundClick(function () { closePanel(); })
    .onNodeHover(function (n) { elGraph.style.cursor = n ? 'pointer' : 'default'; });

  function resize() {
    Graph.width(elGraph.clientWidth).height(elGraph.clientHeight);
  }
  window.addEventListener('resize', resize);
  resize();

  // ---- 検索 + カメラフォーカス ----
  var searchBox = document.getElementById('search-box');
  var searchResult = document.getElementById('search-result');

  function focusNode(node) {
    var x = node.x || 0.01, y = node.y || 0.01, z = node.z || 0.01;
    var distance = 90;
    var distRatio = 1 + distance / Math.hypot(x, y, z);
    Graph.cameraPosition(
      { x: x * distRatio, y: y * distRatio, z: z * distRatio },
      node,
      1200
    );
  }

  function runSearch() {
    var q = searchBox.value.trim().toLowerCase();
    if (!q) {
      highlightSet = new Set();
      searchResult.textContent = '';
      Graph.refresh();
      return null;
    }
    var matches = NODES.filter(function (n) { return n.label.toLowerCase().indexOf(q) !== -1; });
    highlightSet = new Set(matches.map(function (n) { return n.id; }));
    searchResult.textContent = matches.length + t('matches');
    Graph.refresh();
    return matches;
  }
  searchBox.addEventListener('input', function () {
    var matches = runSearch();
    if (matches && matches.length) {
      matches.sort(function (a, b) { return b.degree - a.degree; });
      focusNode(matches[0]);
    }
  });

  // ---- ハブキーワードの常時ラベル (DOMオーバーレイ + 手動スクリーン投影) ----
  // 3d-force-graphはノードラベルをホバー時のみ表示する。「一定次数以上は常時表示」の
  // ためにthree-spritetext等の追加CDNは使わず、camera.matrixWorldInverse /
  // projectionMatrix から手計算でスクリーン座標に投影したDOM要素を重ねている
  // (追加のCDN依存を増やさないための実装選択)。
  var labelLayer = document.getElementById('label-layer');
  var hubThreshold = META.labelAlwaysDegree;
  var hubNodes = NODES.filter(function (n) { return n.degree >= hubThreshold; });
  var hubEls = new Map();
  hubNodes.forEach(function (n) {
    var div = document.createElement('div');
    div.className = 'hub-label';
    div.textContent = n.label;
    labelLayer.appendChild(div);
    hubEls.set(n.id, div);
  });

  var labelsEnabled = true;
  document.getElementById('hub-label-checkbox').addEventListener('change', function (e) {
    labelsEnabled = e.target.checked;
    if (!labelsEnabled) {
      hubEls.forEach(function (div) { div.style.display = 'none'; });
    }
  });

  function projectToScreen(camera, x, y, z, width, height) {
    var ev = camera.matrixWorldInverse.elements;
    var ep = camera.projectionMatrix.elements;
    var vx = ev[0] * x + ev[4] * y + ev[8] * z + ev[12];
    var vy = ev[1] * x + ev[5] * y + ev[9] * z + ev[13];
    var vz = ev[2] * x + ev[6] * y + ev[10] * z + ev[14];
    var vw = ev[3] * x + ev[7] * y + ev[11] * z + ev[15];
    var cx = ep[0] * vx + ep[4] * vy + ep[8] * vz + ep[12] * vw;
    var cy = ep[1] * vx + ep[5] * vy + ep[9] * vz + ep[13] * vw;
    var cw = ep[3] * vx + ep[7] * vy + ep[11] * vz + ep[15] * vw;
    if (cw <= 0.001) return null;
    var ndcX = cx / cw, ndcY = cy / cw;
    return {
      x: (ndcX * 0.5 + 0.5) * width,
      y: (1 - (ndcY * 0.5 + 0.5)) * height
    };
  }

  function updateHubLabels() {
    if (labelsEnabled && hubNodes.length) {
      var width = elGraph.clientWidth, height = elGraph.clientHeight;
      var camera = Graph.camera();
      hubNodes.forEach(function (n) {
        var div = hubEls.get(n.id);
        if (n.x === undefined || n.y === undefined || n.z === undefined) {
          div.style.display = 'none';
          return;
        }
        var p = projectToScreen(camera, n.x, n.y, n.z, width, height);
        if (!p || p.x < -80 || p.x > width + 80 || p.y < -40 || p.y > height + 40) {
          div.style.display = 'none';
        } else {
          div.style.display = 'block';
          div.style.left = p.x.toFixed(1) + 'px';
          div.style.top = p.y.toFixed(1) + 'px';
        }
      });
    }
    requestAnimationFrame(updateHubLabels);
  }
  requestAnimationFrame(updateHubLabels);

  // ---- 言語切り替え (JA/EN) ----
  function applyStaticText() {
    Object.keys(STATIC_TEXT_MAP).forEach(function (id) {
      var el = document.getElementById(id);
      if (el) el.textContent = t(STATIC_TEXT_MAP[id]);
    });
    document.title = t('pageTitle');
    document.documentElement.lang = LANG;
    searchBox.placeholder = t('searchPlaceholder');
    document.getElementById('panel-close').title = t('panelClose');
  }

  function setLang(lang) {
    LANG = I18N[lang] ? lang : 'en';
    document.getElementById('lang-btn-ja').classList.toggle('active', LANG === 'ja');
    document.getElementById('lang-btn-en').classList.toggle('active', LANG === 'en');
    applyStaticText();
    updateEdgeStat();
    updateBandTitle();
    updateAxisNote();
    runSearch();
    if (currentPanelNode) openPanel(currentPanelNode);
  }

  document.getElementById('lang-btn-ja').addEventListener('click', function () { setLang('ja'); });
  document.getElementById('lang-btn-en').addEventListener('click', function () { setLang('en'); });
  setLang(LANG);

  if (window.innerWidth < 700) {
    document.getElementById('legend-wrap').classList.add('collapsed');
  }
})();
</script>
</body>
</html>
"""

html_out = HTML_TEMPLATE.replace("__TRAILMEM_JSON__", json_text_safe)

with open(out_path, "w", encoding="utf-8") as f:
    f.write(html_out)

size_kb = os.path.getsize(out_path) / 1024
print(f"✔ 記憶網ビュワーを書き出しました: {out_path} ({size_kb:.1f} KB)")
print(f"  episodes={episodes_total}  keywords(node)={len(nodes)}  links={links_total}  "
      f"edges={edges_rendered}/{edges_total} (間引き{edges_pruned}件, 閾値{min_edge_weight})")
print(f"  殿堂入りリンク(R>={n_consolidate}): {hof_link_total}件  "
      f"axis列: {'あり' if has_axis else 'なし(通常扱い)'}  "
      f"常時ラベルしきい値: 次数>={label_always_degree}")
print("  file://" + os.path.abspath(out_path) + " をブラウザで開いてください (3Dグラフ表示にはネット接続が必要です)")
PYEOF
