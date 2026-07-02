#!/usr/bin/env python3
"""parse_aozora.py — 青空文庫テキストをプレーン化し、段落単位に分割する。

青空文庫の記法を除去する:
  《...》     ルビ      → 除去（ルビ対象の漢字は残す）
  ｜          ルビ開始位置記号 → 除去
  ［＃...］   入力者注・組版指示 → 除去
  ※［＃...］  外字注記 → 除去（中身が読めないので落とす）

出力:
  data/kokoro_plain.txt        プレーンテキスト全文（章見出し付き）
  data/kokoro_paragraphs.json  段落分割JSON
      [{"pid": "p0001", "chapter": "上 先生と私", "section": "一",
        "seq": 1, "text": "..."}]

段落 = 1エピソード候補。短すぎる段落（MIN_CHARS未満）は前の段落に結合する。
章/節の見出しは段落にせず、以降の段落のメタ情報として持たせる。

Usage:
  python3 parse_aozora.py                      # フル
  python3 parse_aozora.py --max-sections 4     # パイロット（最初のN節のみ）
"""
import argparse
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
RAW_SJIS = os.path.join(DATA, "kokoro_raw_sjis.txt")
PLAIN = os.path.join(DATA, "kokoro_plain.txt")
PARAS = os.path.join(DATA, "kokoro_paragraphs.json")

MIN_CHARS = 60   # これ未満の段落は前の段落に結合


def decode_raw(path):
    with open(path, "rb") as f:
        data = f.read()
    return data.decode("shift_jis", errors="replace")


def strip_markup(line):
    """青空文庫記法を1行から除去。"""
    # 外字注記 ※［＃...］ は中身ごと落とす
    line = re.sub(r"※［＃[^］]*］", "", line)
    # 入力者注・組版指示 ［＃...］ を落とす
    line = re.sub(r"［＃[^］]*］", "", line)
    # ルビ 《...》 を落とす
    line = re.sub(r"《[^》]*》", "", line)
    # ルビ開始記号 ｜ を落とす
    line = line.replace("｜", "")
    return line


# 見出し検出（元行に［＃...大見出し/中見出し］が付いている）
RE_BIG_HEAD = re.compile(r"［＃.*大見出し")
RE_MID_HEAD = re.compile(r"［＃.*中見出し")


def parse(raw):
    lines = raw.replace("\r\n", "\n").replace("", "\n").split("\n")

    # ヘッダ（凡例ブロック）をスキップ: 区切り線 ---- が2回現れるまで
    dash_count = 0
    body_start = 0
    for i, ln in enumerate(lines):
        if ln.startswith("----"):
            dash_count += 1
            if dash_count == 2:
                body_start = i + 1
                break
    body = lines[body_start:]

    # 末尾の底本情報（［＃本文終わり］以降や「底本：」ブロック）を落とす
    cut = len(body)
    for i, ln in enumerate(body):
        if ln.startswith("底本：") or ln.startswith("底本:"):
            cut = i
            break
    body = body[:cut]

    paragraphs = []
    plain_lines = []
    chapter = ""
    section = ""
    seq = 0

    for ln in body:
        raw_line = ln
        is_big = bool(RE_BIG_HEAD.search(raw_line))
        is_mid = bool(RE_MID_HEAD.search(raw_line))
        text = strip_markup(raw_line).strip()
        if not text:
            continue

        if is_big:
            chapter = text
            section = ""
            plain_lines.append(f"\n\n# {text}\n")
            continue
        if is_mid:
            section = text
            plain_lines.append(f"\n## {text}\n")
            continue

        # 通常段落
        if len(text) < MIN_CHARS and paragraphs:
            # 短すぎ → 前の段落に結合（同じ章節のときだけ）
            prev = paragraphs[-1]
            if prev["chapter"] == chapter and prev["section"] == section:
                prev["text"] += text
                plain_lines.append(text)
                continue
        seq += 1
        paragraphs.append({
            "pid": f"p{seq:04d}",
            "chapter": chapter,
            "section": section,
            "seq": seq,
            "text": text,
        })
        plain_lines.append(text)

    return paragraphs, "\n".join(plain_lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-sections", type=int, default=0,
                    help="最初のN節だけ出力（パイロット用, 0=全部）")
    ap.add_argument("--raw", default=RAW_SJIS)
    ap.add_argument("--out-paras", default=PARAS)
    ap.add_argument("--out-plain", default=PLAIN)
    args = ap.parse_args()

    raw = decode_raw(args.raw)
    paragraphs, plain = parse(raw)

    if args.max_sections > 0:
        # 章+節の組で先頭Nブロックに絞る
        seen = []
        keep = []
        for p in paragraphs:
            key = (p["chapter"], p["section"])
            if key not in seen:
                seen.append(key)
            if len(seen) > args.max_sections:
                break
            keep.append(p)
        paragraphs = keep

    os.makedirs(os.path.dirname(args.out_paras), exist_ok=True)
    with open(args.out_paras, "w", encoding="utf-8") as f:
        json.dump(paragraphs, f, ensure_ascii=False, indent=1)
    with open(args.out_plain, "w", encoding="utf-8") as f:
        f.write(plain)

    chapters = sorted({p["chapter"] for p in paragraphs})
    sections = sorted({(p["chapter"], p["section"]) for p in paragraphs})
    print(f"段落数: {len(paragraphs)}")
    print(f"章: {chapters}")
    print(f"節数: {len(sections)}")
    print(f"-> {args.out_paras}")
    print(f"-> {args.out_plain}")


if __name__ == "__main__":
    main()
