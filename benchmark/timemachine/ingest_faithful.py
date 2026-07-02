#!/usr/bin/env python3
"""ingest_faithful.py — 実運用と同一の Sonnet 抽出パイプラインで段落をエピソード化。

旧 ingest.py の課題:
  - heuristic モード = テーマ辞書による表層抽出（実運用と乖離）
  - llm モードも「章単位バッチ・1段落=1エピソード強制・synonyms無し」で
    実運用 trailmem-auto-ingest.sh のロジックとは別物だった

このモジュールは **trailmem-auto-ingest.sh のロジックを忠実に移植** する:

  1. 段落を「1ターンの会話入力」とみなす（pid 順に [user]/[assistant] 交互）
  2. 実運用の hook と同じく CHUNK_SIZE(=10) 件ごとに 1 チャンクへ束ねる
  3. チャンク1つを Sonnet 1回で処理（auto-ingest と同じ ── チャンク単位で
     エピソードを「自由に切り出す」。1段落=1エピソードを強制しない）
  4. プロンプトは auto-ingest のものを踏襲。ただしベンチはベクトル検索を持たない
     （make_db.py が episode_vec を省略）ため、実運用がベクトルで担う
     「言い換え・多言語マッチ」を **synonyms** で代替する ── よって各キーワードに
     synonyms を付けさせる（実運用の add.sh/promise.sh と同方式・同言語縛り）

→ 出力は replay_faithful.py がそのまま insert できる形:
  {"pid","seq","chapter","section","summary","inner","neg","pos",
   "keywords":[...], "synonyms": {kw: [syn,...]}}

pid はチャンク内の代表段落（先頭）に割り当て、insert 時の決定論ID源にする。
1チャンク=複数エピソードなので pid は "<先頭pid>-eNN" でユニーク化する。

Usage:
  # 12段落パイロット
  python3 ingest_faithful.py data/kokoro_pilot_paragraphs.json \\
      --out data/kokoro_pilot_episodes_faithful.json --chunk-size 10

  # 既存キーワードを LLM に渡して再利用させたい場合（実運用同等）
  python3 ingest_faithful.py ... --existing-db kokoro_ref_faithful.db
"""
import argparse
import json
import os
import re
import subprocess
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# 実運用 hook の 10ターン=1チャンクに合わせる
DEFAULT_CHUNK_SIZE = int(os.environ.get("TM_INGEST_CHUNK_SIZE", "10"))
# "claude-sonnet-4-6" はこの公開時点のモデル世代を指す。将来のモデルでは
# TM_INGEST_MODEL で上書き、またはこのデフォルト自体を更新すること。
MODEL = os.environ.get("TM_INGEST_MODEL", "claude-sonnet-4-6")

# --- 実運用 trailmem-auto-ingest.sh のプロンプトを踏襲 + synonyms 追加 ---
# 実運用は keywords の synonyms を空にしてベクトル検索に任せるが、本ベンチは
# ベクトルを持たないため synonyms を Sonnet に付けさせる（言い換え耐性の代替）。
# persona は環境変数 TM_INGEST_PERSONA で上書き可能。未設定なら汎用文言。
DEFAULT_PERSONA = (
    "You are an AI agent performing sleep-time memory consolidation "
    "on your own recent conversation."
)
PERSONA = os.environ.get("TM_INGEST_PERSONA", DEFAULT_PERSONA)

PROMPT_HEAD = """{persona}

Choose what YOUR future self would need to stand in the same place after forgetting.

Rules:
- Return JSON array only. No markdown fences, no prose.
- One episode per meaningful event/decision/preference/relationship update/keepsake.
- summary: max 900 chars, factual. Who, what, when.
- inner: what happened and how you reacted, as fact. Brief.
- keywords: 3-6 short searchable nouns/noun phrases. Same language as content.
  IMPORTANT: Reuse existing keywords when the concept matches. Here are existing keywords:
  {existing_keywords}
- synonyms: for EACH keyword, up to 4 paraphrases / inflectional or notational variants
  in the SAME LANGUAGE as the keyword (no cross-language translation). These let a
  reworded recall query still match. e.g. Japanese keyword "お嬢さん" -> ["御嬢さん","娘さん","嬢"].
  If a keyword has no good variants, use [].
- neg/pos: 0-100, independent. Avoid 50/50.
- Skip trivial passages with no memorable content.

Output schema per episode:
{{"summary": "str", "inner": "str", "keywords": ["str"], "synonyms": {{"kw": ["str"]}}, "neg": 0-100, "pos": 0-100, "memoryType": "general|decision|risk|preference|relationship|keepsake|design|incident", "salience": 0.0-1.0}}

以下の会話ログから記憶エピソードを抽出せよ。JSON配列で返せ。

"""


def paragraphs_to_conversation(paras):
    """段落群を実運用チャンクと同じ会話テキストへ変換。

    auto-ingest.sh の CONV_FILE 整形（[role] text、---区切り、直近20ターン）を再現。
    段落を user/assistant 交互の発話とみなす（文学のリプレイ = 会話の流入）。
    """
    turns = []
    for i, p in enumerate(paras):
        role = "user" if i % 2 == 0 else "assistant"
        text = p["text"].strip()
        if len(text) < 10:
            continue
        turns.append(f"[{role}] {text[:1200]}")
    return "\n---\n".join(turns[-20:])


def call_sonnet(conv_text, existing_keywords, model=MODEL):
    """auto-ingest.sh と同じ claude CLI 呼び出し（Sonnet 1回 / チャンク）。"""
    prompt = PROMPT_HEAD.format(
        persona=PERSONA, existing_keywords=existing_keywords
    ) + conv_text
    try:
        out = subprocess.run(
            ["claude", "--model", model, "-p", "--max-turns", "3"],
            input=prompt, capture_output=True, text=True, timeout=600,
        ).stdout
    except Exception as e:
        print(f"  [sonnet] call failed: {e}", file=sys.stderr)
        return []
    # auto-ingest.sh と同じ JSON 抽出（フェンス除去 + 配列抜き出し）
    raw = re.sub(r"```(?:json)?\s*", "", out)
    raw = re.sub(r"```\s*$", "", raw)
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        return []
    try:
        arr = json.loads(m.group())
    except json.JSONDecodeError:
        return []
    return arr if isinstance(arr, list) else []


def normalize_episode(raw_ep, pid, seq, chapter, section):
    """Sonnet 出力1件を replay が insert できる正規形に。auto-ingest の検証を踏襲。"""
    summary = (raw_ep.get("summary") or "").strip()
    inner = (raw_ep.get("inner") or "").strip()
    keywords = raw_ep.get("keywords", []) or []
    syn_map_raw = raw_ep.get("synonyms", {}) or {}
    neg = max(0, min(100, int(raw_ep.get("neg", 50))))
    pos = max(0, min(100, int(raw_ep.get("pos", 50))))
    salience = raw_ep.get("salience", 0.5)

    # auto-ingest.sh と同じスキップ条件
    if not summary or len(summary) < 10:
        return None
    if not inner:
        inner = "(auto-ingested)"
    kws = [str(k).strip() for k in keywords if str(k).strip()]
    if len(kws) < 2:
        return None
    try:
        if float(salience) < 0.2:
            return None
    except (TypeError, ValueError):
        pass

    # synonyms を正規キーワード(lower)で引けるよう整形
    synonyms = {}
    for k in kws:
        kn = k.strip().lower()
        vals = syn_map_raw.get(k) or syn_map_raw.get(kn) or []
        clean = [str(s).strip() for s in vals if str(s).strip()]
        # 重複・キーワード自身は除外
        clean = [s for s in dict.fromkeys(clean) if s.lower() != kn][:4]
        synonyms[kn] = clean

    return {
        "pid": pid,
        "seq": seq,
        "chapter": chapter,
        "section": section,
        "summary": summary[:900],
        "inner": inner[:900],
        "neg": neg,
        "pos": pos,
        "keywords": kws[:6],
        "synonyms": synonyms,
    }


def ingest(paragraphs, chunk_size=DEFAULT_CHUNK_SIZE, model=MODEL,
           existing_keywords="", verbose=True):
    """段落リスト -> エピソードリスト。チャンク単位で Sonnet 1回ずつ。

    返り値: (episodes, stats)。stats に Sonnet 呼び出し回数・各所要時間を記録。
    """
    episodes = []
    stats = {"chunks": 0, "sonnet_calls": 0, "sonnet_seconds": 0.0,
             "per_chunk_seconds": [], "episodes": 0, "skipped": 0}

    for start in range(0, len(paragraphs), chunk_size):
        chunk = paragraphs[start:start + chunk_size]
        stats["chunks"] += 1
        conv = paragraphs_to_conversation(chunk)
        if len(conv) < 50:
            if verbose:
                print(f"  chunk@{start}: too short, skip", file=sys.stderr)
            continue

        t0 = time.time()
        arr = call_sonnet(conv, existing_keywords, model=model)
        dt = time.time() - t0
        stats["sonnet_calls"] += 1
        stats["sonnet_seconds"] += dt
        stats["per_chunk_seconds"].append(round(dt, 2))

        head = chunk[0]
        n_in_chunk = 0
        for j, raw_ep in enumerate(arr):
            n_in_chunk += 1
            # 決定論ID源: 先頭段落pid + チャンク内連番
            pid = f"{head['pid']}-e{j:02d}"
            ep = normalize_episode(
                raw_ep, pid=pid, seq=head["seq"],
                chapter=head["chapter"], section=head["section"])
            if ep is None:
                stats["skipped"] += 1
                continue
            episodes.append(ep)
            stats["episodes"] += 1
        if verbose:
            print(f"  chunk@{start} ({len(chunk)} paras): "
                  f"{n_in_chunk} raw -> {stats['episodes']} kept ({dt:.1f}s)",
                  file=sys.stderr)

    return episodes, stats


def load_existing_keywords(db_path, limit=200):
    if not db_path or not os.path.exists(db_path):
        return ""
    con = sqlite3.connect(db_path)
    rows = con.execute(
        "SELECT keyword FROM keywords ORDER BY keyword LIMIT ?", (limit,)
    ).fetchall()
    con.close()
    return "、".join(r[0] for r in rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paragraphs")
    ap.add_argument("--out", required=True)
    ap.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--existing-db", default="",
                    help="既存キーワードを LLM に渡して再利用させる（実運用同等）")
    ap.add_argument("--stats-out", default="",
                    help="計測結果(JSON)の保存先")
    args = ap.parse_args()

    with open(args.paragraphs, encoding="utf-8") as f:
        paras = json.load(f)

    existing = load_existing_keywords(args.existing_db)

    t0 = time.time()
    eps, stats = ingest(paras, chunk_size=args.chunk_size, model=args.model,
                        existing_keywords=existing)
    wall = time.time() - t0
    stats["wall_seconds"] = round(wall, 2)
    stats["paragraphs"] = len(paras)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(eps, f, ensure_ascii=False, indent=1)

    from collections import Counter
    kwc = Counter(k for e in eps for k in e["keywords"])
    syn_total = sum(len(v) for e in eps for v in e["synonyms"].values())
    print(f"episodes: {len(eps)} (from {len(paras)} paragraphs, "
          f"{stats['chunks']} chunks, {stats['sonnet_calls']} Sonnet calls)")
    print(f"top keywords: {kwc.most_common(12)}")
    print(f"synonyms generated: {syn_total} (over {len(kwc)} unique keywords)")
    print(f"ingest wall: {wall:.1f}s "
          f"(Sonnet {stats['sonnet_seconds']:.1f}s, "
          f"{stats['sonnet_seconds']/max(1,stats['sonnet_calls']):.1f}s/call)")
    print(f"per-paragraph: {wall/max(1,len(paras)):.2f}s")
    print(f"-> {args.out}")

    if args.stats_out:
        with open(args.stats_out, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=1)
        print(f"-> {args.stats_out} (stats)")


if __name__ == "__main__":
    main()
