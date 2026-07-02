#!/usr/bin/env python3
"""build_goldset.py — 「こころ」の登場人物に関する想起テスト問題を生成。

各問いは「登場人物が言いそうなこと / 作中で実際に起きた出来事・感情」を、
作中表現とは変えて言い換えた質問。正解 = その出来事を含む段落(=エピソード)群。

2段階:
  1. Sonnet で N問の質問を生成（作品内容に基づく。作中の地の文と表現を変える）。
     各問いに「キーワード(登場人物+概念)」と「探すべき出来事の説明」を付ける。
  2. 各問いの正解エピソードIDを、段落本文との突合で機械的に決定する:
     - LLMが指定した keyword をすべて含むエピソード本文を候補にし、
       さらに「出来事の説明」と本文のキーワード重なりで上位を正例とする。
     - これにより正解は「実際にDBへ入った段落」へ確実に紐づく（ID整合）。

ゴールドセットは育てる前のDB（全エピソードが入った参照DB）に対して作る。
想起テストはこのIDを使って、育てた各DBで評価する。

出力: goldset.jsonl
  {"id":"g001","prompt":"...","keywords":["先生","K"],
   "relevant":["kokoro-pXXXX-...","..."],"source":"llm","note":"..."}

Usage:
  # 参照DB(全段落入り)をまず作る
  python3 make_db.py kokoro_ref.db
  python3 replay.py --db kokoro_ref.db --episodes data/kokoro_episodes.json --floor on
  python3 build_goldset.py --ref-db kokoro_ref.db \\
      --episodes data/kokoro_episodes.json --n 30 --out goldset.jsonl
"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

GEN_PROMPT = """夏目漱石「こころ」の登場人物に関する記憶想起テストの問題を{n}問作ってください。

目的: AIの記憶システムが、登場人物(先生/私/K/お嬢さん/奥さん/父/母 など)の
出来事・感情・関係性を正しく想起できるかを測る。

各問いの条件:
- 「その人物が言いそうなこと」または「作中で実際に起きた出来事・感情」を問う。
- 必ず作中の地の文・台詞とは表現を変えること（言い換え。固有の文言を避ける）。
- 物語の上中下（先生と私 / 両親と私 / 先生と遺書）から満遍なく。
- 関係性・心情・転機(Kとお嬢さんの恋、先生の罪悪感、先生の自殺、私の父の病気 等)を含める。

各問いに付ける情報:
- prompt: 言い換えた質問文（日本語、1文）。
- keywords: その出来事を探すための語 2-4個（登場人物名+概念。本文に出る語）。
- event: その問いが指している作中の出来事を10-40字で説明（正解の根拠）。

JSON配列のみ返す。マークダウン記法・前置き禁止。
スキーマ: [{{"prompt":"...","keywords":["..."],"event":"..."}}]
"""

# event/本文の重なり判定に使う、ストップワード的に弱い語
WEAK = set("こと もの ため よう こと 私 人 事 様 中 上 下".split())


def gen_questions(n, model):
    prompt = GEN_PROMPT.format(n=n)
    out = subprocess.run(
        ["claude", "--model", model, "-p", "--max-turns", "6"],
        input=prompt, capture_output=True, text=True, timeout=900,
    ).stdout
    raw = re.sub(r"```(?:json)?\s*", "", out)
    raw = re.sub(r"```\s*$", "", raw)
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        print("LLM出力をパースできませんでした:", out[:300], file=sys.stderr)
        return []
    return json.loads(m.group())


def load_ref_episodes(ref_db):
    """参照DBから {episode_id: (summary, inner, source_ref)} を読む。"""
    con = sqlite3.connect(ref_db)
    con.row_factory = sqlite3.Row
    rows = {}
    for r in con.execute("SELECT id, summary, inner, source_ref FROM episodes"):
        rows[r["id"]] = (r["summary"], r["inner"], r["source_ref"])
    # episode -> keywords
    ep_kw = {}
    for r in con.execute("SELECT episode_id, keyword FROM episode_keywords"):
        ep_kw.setdefault(r["episode_id"], set()).add(r["keyword"])
    con.close()
    return rows, ep_kw


def char_grams(s, n=2):
    s = re.sub(r"\s+", "", s)
    return {s[i:i+n] for i in range(len(s) - n + 1)}


def find_relevant(q, ref_eps, ep_kw, top=4):
    """問いの keyword + event 説明に最も合致するエピソードを正例として選ぶ。

    1. keyword（小文字化）の少なくとも1つをトレイルに持つエピソードを候補。
    2. event 説明と本文(summary+inner)の文字bigram重なりで採点。
    3. keyword一致数も加点。上位 top 件を正例。
    """
    kws = [k.strip().lower() for k in q.get("keywords", []) if k.strip()]
    event = q.get("event", "") + " " + q.get("prompt", "")
    eg = char_grams(event)
    scored = []
    for eid, (summary, inner, _ref) in ref_eps.items():
        ekws = ep_kw.get(eid, set())
        kw_hits = sum(1 for k in kws if k in ekws)
        if kw_hits == 0:
            continue
        text = summary + inner
        tg = char_grams(text)
        overlap = len(eg & tg) / (len(eg) + 1)
        score = kw_hits * 0.5 + overlap
        scored.append((score, eid))
    scored.sort(reverse=True)
    return [eid for _s, eid in scored[:top] if _s > 0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref-db", required=True)
    ap.add_argument("--n", type=int, default=30)
    # "claude-sonnet-4-6" はこの公開時点のモデル世代を指す。将来のモデルでは
    # --model で上書き、またはこのデフォルト自体を更新すること。
    ap.add_argument("--model", default="claude-sonnet-4-6")
    ap.add_argument("--out", default=os.path.join(HERE, "goldset.jsonl"))
    ap.add_argument("--questions-cache", default=os.path.join(HERE, "data", "questions_raw.json"),
                    help="生成済み質問JSON。あれば再利用しLLMを呼ばない")
    args = ap.parse_args()

    ref_db = args.ref_db if os.path.isabs(args.ref_db) else os.path.join(HERE, args.ref_db)

    if os.path.exists(args.questions_cache):
        print(f"質問キャッシュを再利用: {args.questions_cache}")
        with open(args.questions_cache, encoding="utf-8") as f:
            questions = json.load(f)
    else:
        print(f"Sonnet で {args.n} 問生成中...")
        questions = gen_questions(args.n, args.model)
        os.makedirs(os.path.dirname(args.questions_cache), exist_ok=True)
        with open(args.questions_cache, "w", encoding="utf-8") as f:
            json.dump(questions, f, ensure_ascii=False, indent=1)
        print(f"  生成: {len(questions)}問 -> {args.questions_cache}")

    ref_eps, ep_kw = load_ref_episodes(ref_db)
    print(f"参照エピソード: {len(ref_eps)}")

    cases = []
    skipped = 0
    for i, q in enumerate(questions, 1):
        rel = find_relevant(q, ref_eps, ep_kw)
        if not rel:
            skipped += 1
            continue
        cases.append({
            "id": f"g{i:03d}",
            "prompt": q.get("prompt", ""),
            "keywords": q.get("keywords", []),
            "relevant": rel,
            "source": "llm",
            "note": q.get("event", ""),
        })

    with open(args.out, "w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    print(f"ゴールド: {len(cases)}問 (正例ゼロでスキップ {skipped}) -> {args.out}")
    for c in cases[:5]:
        print(f"  {c['id']} kw={c['keywords']} rel={len(c['relevant'])} | {c['prompt'][:50]}")


if __name__ == "__main__":
    main()
