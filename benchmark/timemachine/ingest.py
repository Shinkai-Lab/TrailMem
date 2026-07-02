#!/usr/bin/env python3
"""ingest.py — 段落 → エピソード（summary/inner/感情/キーワード）抽出。

2モード:
  heuristic : LLM不使用。登場人物名+内容語を正規表現/出現頻度で抽出し、
              感情は感情語の出現でスコア化。コストゼロ・即時。まず動く優先。
  llm       : claude(Sonnet) で章単位にバッチ抽出。コスト配慮で章ごと1回呼ぶ。

このモジュールは「段落リスト -> エピソードdictリスト」を返すライブラリ。
replay.py から呼ばれる。単体実行で抽出結果をJSON保存もできる。

エピソードdict:
  {"pid","seq","chapter","section","summary","inner",
   "neg","pos","keywords":[...]}

Usage:
  python3 ingest.py data/kokoro_pilot_paragraphs.json --mode heuristic \\
      --out data/kokoro_pilot_episodes.json

NOTE: the "claude-sonnet-4-6" default below pins the model generation current
as of this project's public release. Pass --model to override, or update the
default as new model generations ship.
"""
import argparse
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# --- 「こころ」の主要登場人物・固有概念（キーワード辞書） ---
# 表記ゆれを正規キーワードに束ねる。recall は正規キーワードで突合する。
CHARACTERS = {
    "先生": ["先生"],
    "私": ["私", "わたくし"],
    "K": ["Ｋ", "K"],
    "お嬢さん": ["お嬢さん", "御嬢さん", "嬢さん"],
    "奥さん": ["奥さん"],
    "父": ["父", "おとっさん", "御父さん", "親父"],
    "母": ["母", "おっかさん", "御母さん"],
    "兄": ["兄"],
    "叔父": ["叔父", "おじ"],
    "友達": ["友達", "友人"],
}

# 物語上の重要概念（テーマ語）。出現したら拾う。
THEME_WORDS = [
    "鎌倉", "海", "海水浴", "東京", "下宿", "病気", "遺書", "手紙",
    "自殺", "死", "恋", "結婚", "嫉妬", "罪", "裏切り", "金", "財産",
    "墓", "明治", "天皇", "乃木", "孤独", "淋しい", "信用", "卒業",
    "学校", "大学", "酒", "謀叛",
]

POS_WORDS = ["嬉", "楽し", "愉快", "幸福", "安心", "笑", "好き", "愛",
             "美し", "親しい", "満足", "喜", "感謝", "落ち付", "穏やか"]
NEG_WORDS = ["淋し", "寂し", "苦し", "悲し", "不安", "恐", "怖", "孤独",
             "死", "自殺", "罪", "嫉妬", "憎", "後悔", "絶望", "暗い",
             "泣", "病", "疑", "裏切", "失望", "煩悶", "辛"]


def heuristic_episode(p):
    text = p["text"]
    kws = []
    for canon, variants in CHARACTERS.items():
        if any(v in text for v in variants):
            kws.append(canon)
    for w in THEME_WORDS:
        if w in text:
            kws.append(w)
    # 重複除去（順序保持）
    seen = set()
    kws = [k for k in kws if not (k in seen or seen.add(k))]

    pos = 50 + min(40, sum(8 for w in POS_WORDS if w in text))
    neg = 50 + min(40, sum(8 for w in NEG_WORDS if w in text))
    pos = max(5, min(95, pos))
    neg = max(5, min(95, neg))

    # summary = 段落の先頭1〜2文（最大120字）
    sents = re.split(r"(?<=。)", text)
    summary = "".join(sents[:2])[:160].strip()
    if not summary:
        summary = text[:120]

    return {
        "pid": p["pid"],
        "seq": p["seq"],
        "chapter": p["chapter"],
        "section": p["section"],
        "summary": summary,
        "inner": text[:400],
        "neg": neg,
        "pos": pos,
        "keywords": kws[:6],
    }


def heuristic_ingest(paragraphs):
    eps = []
    for p in paragraphs:
        ep = heuristic_episode(p)
        if not ep["keywords"]:
            ep["keywords"] = ["私"]  # 「私」の語りなので最低限のフォールバック
        eps.append(ep)
    return eps


# --- LLM モード（章単位バッチ） ---
LLM_PROMPT = """あなたは記憶整理を行うアシスタントです。夏目漱石「こころ」の本文段落から、登場人物の記憶エピソードを抽出します。

ルール:
- JSON配列のみ返す。マークダウン記法・前置きは禁止。
- 入力の各段落に対し1エピソード。配列の順番は入力順を保つ。
- summary: 誰が何をした/感じたか、最大120字。本文の言い換えで簡潔に。
- keywords: 3-6個。登場人物名(先生/私/K/お嬢さん/奥さん/父/母 など)と重要概念(恋/死/遺書/嫉妬 等)。本文の語そのまま。
- neg/pos: 0-100の独立スコア。50/50は避ける。

各段落の出力スキーマ:
{"pid":"pXXXX","summary":"...","keywords":["..."],"neg":0-100,"pos":0-100}

以下の段落群をJSON配列で処理せよ:
"""


def llm_ingest(paragraphs, model="claude-sonnet-4-6", batch_chapter=True):
    """章ごとにまとめてSonnetで抽出。失敗時はheuristicにフォールバック。"""
    # 章単位にグループ化
    groups = {}
    order = []
    for p in paragraphs:
        key = p["chapter"]
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(p)

    by_pid = {}
    for key in order:
        chunk = groups[key]
        payload = [{"pid": p["pid"], "text": p["text"][:800]} for p in chunk]
        prompt = LLM_PROMPT + json.dumps(payload, ensure_ascii=False)
        try:
            out = subprocess.run(
                ["claude", "--model", model, "-p", "--max-turns", "1"],
                input=prompt, capture_output=True, text=True, timeout=600,
            ).stdout
            raw = re.sub(r"```(?:json)?\s*", "", out)
            raw = re.sub(r"```\s*$", "", raw)
            m = re.search(r"\[.*\]", raw, re.DOTALL)
            arr = json.loads(m.group()) if m else []
            for item in arr:
                by_pid[item.get("pid")] = item
        except Exception as e:
            print(f"  [llm] chapter '{key[:20]}' failed: {e}; "
                  f"falling back to heuristic for it", file=sys.stderr)

    # マージ: LLM結果があればそれ、なければheuristic
    eps = []
    for p in paragraphs:
        h = heuristic_episode(p)
        llm = by_pid.get(p["pid"])
        if llm:
            kws = [str(k).strip() for k in llm.get("keywords", []) if str(k).strip()]
            eps.append({
                "pid": p["pid"], "seq": p["seq"],
                "chapter": p["chapter"], "section": p["section"],
                "summary": (llm.get("summary") or h["summary"])[:200],
                "inner": p["text"][:400],
                "neg": max(0, min(100, int(llm.get("neg", h["neg"])))),
                "pos": max(0, min(100, int(llm.get("pos", h["pos"])))),
                "keywords": (kws or h["keywords"])[:6],
            })
        else:
            eps.append(h)
    return eps


def ingest(paragraphs, mode="heuristic", model="claude-sonnet-4-6"):
    if mode == "llm":
        return llm_ingest(paragraphs, model=model)
    return heuristic_ingest(paragraphs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paragraphs")
    ap.add_argument("--mode", choices=["heuristic", "llm"], default="heuristic")
    ap.add_argument("--model", default="claude-sonnet-4-6")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    with open(args.paragraphs, encoding="utf-8") as f:
        paras = json.load(f)
    eps = ingest(paras, mode=args.mode, model=args.model)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(eps, f, ensure_ascii=False, indent=1)

    from collections import Counter
    kwc = Counter(k for e in eps for k in e["keywords"])
    print(f"episodes: {len(eps)} (mode={args.mode})")
    print(f"top keywords: {kwc.most_common(12)}")
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
