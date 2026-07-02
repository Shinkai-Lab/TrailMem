#!/usr/bin/env python3
"""ゴールドセット生成 — 人格維持ベンチマーク。

2系統で (入力プロンプト -> 関連エピソードID集合) の正例を作る:

  1. 実ログ由来 (source="log", 高信頼)
     trailmem-hook.log をパースし、
       === 日時 ===
       [input] <input文>
       [scan] ... 💫 フラッシュバック: <要約> ...
     のブロックから、(input文 -> フラッシュバックした要約に対応する
     エピソードID) を抽出。要約 -> episode_id は episodes.summary との
     前方一致/正規化部分一致で対応づける。「実運用で実際に想起されたもの」。

  2. 構造由来 (source="struct", 網羅)
     各 prompt のキーワードから episode_keywords を辿り、
     effective_strength 上位のエピソードを関連候補にする。

出力:
  - goldset.jsonl       各行 {"id","prompt","relevant":[...],"source"}
  - goldset_review.html  人間レビュー用（prompt と relevant summary を並べる）

本番DBは読み取りのみ。
"""
import argparse
import html
import json
import os
import re

from common import (HERE, GOLDSET, open_ro, read_log, norm,
                    load_episode_summaries)

REVIEW_HTML = os.path.join(HERE, "goldset_review.html")

# 抽出ノイズになる定型プロンプト（LLM内部呼び出し）を除外
NOISE_PROMPT_RE = re.compile(
    r"(Generate up to|Return ONLY|以下の会話ログから|"
    r"この要約文と内面コメント|neg\(ネガティブ度)")

# フラッシュバック断片を切り出す: "💫 フラッシュバック: <text>" の text は
# 次のキーワード統計 " <kw> (n/m) [..]" or 行末まで
FB_RE = re.compile(
    r"💫\s*フラッシュバック:\s*(.+?)(?=\s+\S+\s*\(\d+/\d+\)\s*\[|$)")


def parse_log_blocks(lines):
    """ログを (input, [scan_lines]) ブロックに分割。"""
    blocks = []
    cur_input = None
    cur_scans = []
    for ln in lines:
        s = ln.strip()
        if s.startswith("=== ") and s.endswith(" ==="):
            if cur_input is not None:
                blocks.append((cur_input, cur_scans))
            cur_input, cur_scans = None, []
        elif s.startswith("[input]"):
            cur_input = ln.split("[input]", 1)[1].strip()
        elif s.startswith("[scan]"):
            cur_scans.append(ln)
    if cur_input is not None:
        blocks.append((cur_input, cur_scans))
    return blocks


def build_summary_index(summaries):
    """要約断片 -> episode_id の高速照合用インデックス。

    断片は途中で切れている可能性があるので、要約の前方一致で照合する。
    """
    # 正規化済み要約 (id, norm_summary)
    norm_sums = [(eid, norm(s)) for eid, s in summaries.items()]

    def lookup(fragment):
        nf = norm(fragment)
        if len(nf) < 12:
            return None
        # 前方一致を優先
        cands = [eid for eid, ns in norm_sums if ns.startswith(nf[:30])]
        if len(cands) == 1:
            return cands[0]
        if cands:
            # 複数 -> 断片全体で最長一致
            best, blen = None, -1
            for eid in cands:
                ns = dict(norm_sums)[eid] if False else None
            # シンプルに: 断片がより長く一致するものを選ぶ
            for eid, ns in norm_sums:
                if eid in cands and ns.startswith(nf):
                    return eid
            return cands[0]
        # 前方一致なし -> 部分一致（断片が要約の途中から始まるケースは稀）
        for eid, ns in norm_sums:
            if nf[:30] in ns:
                return eid
        return None

    return lookup


def build_log_cases(con, max_cases):
    summaries = load_episode_summaries(con)
    lookup = build_summary_index(summaries)
    lines = read_log()
    blocks = parse_log_blocks(lines)

    cases = []
    seen_prompts = set()
    for inp, scans in blocks:
        if not inp or NOISE_PROMPT_RE.search(inp):
            continue
        # 短すぎる相槌は除外
        if len(norm(inp)) < 8:
            continue
        relevant = []
        for sline in scans:
            for m in FB_RE.finditer(sline):
                eid = lookup(m.group(1).strip())
                if eid and eid not in relevant:
                    relevant.append(eid)
        if not relevant:
            continue
        key = norm(inp)[:40]
        if key in seen_prompts:
            continue
        seen_prompts.add(key)
        cases.append({
            "prompt": inp,
            "relevant": relevant,
            "source": "log",
        })
    return cases


def keywords_in_prompt(con, prompt):
    """prompt に部分一致するキーワードを返す（scan.sh流）。"""
    p = norm(prompt)
    kws = []
    for r in con.execute("SELECT keyword, synonyms FROM keywords"):
        kw = r["keyword"]
        if norm(kw) and norm(kw) in p:
            kws.append(kw)
            continue
        try:
            syns = json.loads(r["synonyms"]) if r["synonyms"] else []
        except Exception:
            syns = []
        for s in syns:
            if norm(s) and norm(s) in p:
                kws.append(kw)
                break
    return kws


def struct_relevant(con, prompt, top=8):
    """prompt のキーワード -> effective_strength 上位エピソード。"""
    kws = keywords_in_prompt(con, prompt)
    if not kws:
        return []
    ph = ",".join("?" * len(kws))
    rows = con.execute(f"""
        SELECT ek.episode_id AS eid,
               MAX(ek.effective_strength) AS best
        FROM episode_keywords ek
        WHERE ek.keyword IN ({ph}) AND ek.is_deleted = 0
        GROUP BY ek.episode_id
        ORDER BY best DESC
        LIMIT ?
    """, (*kws, top)).fetchall()
    return [r["eid"] for r in rows]


def build_struct_cases(con, log_prompts, max_cases):
    """構造由来ケース。

    既にログ由来で拾った prompt を再利用し、各 prompt に対し
    キーワード経由の関連集合を作る（ログとは別 source として並べる）。
    実ログのリアルな入力文を流用することで「人格維持」の文脈を保つ。
    """
    cases = []
    seen = set()
    for prompt in log_prompts:
        rel = struct_relevant(con, prompt)
        if not rel:
            continue
        key = norm(prompt)[:40]
        if key in seen:
            continue
        seen.add(key)
        cases.append({
            "prompt": prompt,
            "relevant": rel,
            "source": "struct",
        })
        if len(cases) >= max_cases:
            break
    return cases


def write_jsonl(cases, path):
    with open(path, "w", encoding="utf-8") as f:
        for i, c in enumerate(cases, 1):
            c = dict(c)
            c["id"] = f"g{i:03d}"
            # idを先頭に
            ordered = {"id": c["id"], "prompt": c["prompt"],
                       "relevant": c["relevant"], "source": c["source"]}
            f.write(json.dumps(ordered, ensure_ascii=False) + "\n")


def write_review_html(cases, summaries, path):
    rows = []
    for i, c in enumerate(cases, 1):
        cid = f"g{i:03d}"
        rel_items = "".join(
            f'<li><code>{html.escape(eid)}</code> — '
            f'{html.escape(summaries.get(eid, "(要約なし)"))}</li>'
            for eid in c["relevant"]
        )
        rows.append(f"""
        <tr>
          <td class="id">{cid}<br><span class="src">{c['source']}</span></td>
          <td class="prompt">{html.escape(c['prompt'])}</td>
          <td><ul>{rel_items}</ul></td>
          <td class="check"><label><input type="checkbox"> OK</label></td>
        </tr>""")
    doc = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<title>人格維持ベンチマーク ゴールドセット レビュー</title>
<style>
 body {{ font-family: sans-serif; margin: 1.5rem; color:#222; }}
 h1 {{ font-size: 1.3rem; }}
 .meta {{ color:#666; margin-bottom:1rem; }}
 table {{ border-collapse: collapse; width: 100%; }}
 th, td {{ border:1px solid #ccc; padding:8px; vertical-align:top;
          text-align:left; font-size:0.9rem; }}
 th {{ background:#f0f4f8; position:sticky; top:0; }}
 .id {{ white-space:nowrap; font-weight:bold; }}
 .src {{ font-weight:normal; color:#888; font-size:0.75rem; }}
 .prompt {{ width:28%; }}
 code {{ background:#f6f6f6; padding:1px 4px; border-radius:3px;
         font-size:0.8rem; }}
 ul {{ margin:0; padding-left:1.1rem; }}
 li {{ margin-bottom:6px; }}
 .check {{ white-space:nowrap; }}
 tr:nth-child(even) {{ background:#fafafa; }}
</style></head><body>
<h1>人格維持ベンチマーク — ゴールドセット目視レビュー</h1>
<p class="meta">{len(cases)} ケース。各 prompt に対する relevant
エピソードの要約を確認し、誤りがあれば goldset.jsonl を直接修正してください。
OKチェックはローカル確認用（保存はされません）。</p>
<table>
 <thead><tr><th>ID</th><th>prompt（入力）</th>
 <th>relevant エピソード（要約）</th><th>確認</th></tr></thead>
 <tbody>{''.join(rows)}</tbody>
</table>
</body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-log", type=int, default=40,
                    help="ログ由来の最大ケース数")
    ap.add_argument("--max-struct", type=int, default=15,
                    help="構造由来の最大ケース数")
    args = ap.parse_args()

    con = open_ro()
    summaries = load_episode_summaries(con)

    log_cases = build_log_cases(con, args.max_log)
    log_cases = log_cases[:args.max_log]

    struct_cases = build_struct_cases(
        con, [c["prompt"] for c in log_cases], args.max_struct)

    cases = log_cases + struct_cases
    write_jsonl(cases, GOLDSET)
    write_review_html(cases, summaries, REVIEW_HTML)

    n_log = sum(1 for c in cases if c["source"] == "log")
    n_struct = sum(1 for c in cases if c["source"] == "struct")
    print(f"ゴールドセット生成完了: {len(cases)} ケース "
          f"(log={n_log}, struct={n_struct})")
    print(f"  -> {GOLDSET}")
    print(f"  -> {REVIEW_HTML}")
    print()
    print("サンプル:")
    for c in cases[:3]:
        rels = ", ".join(c["relevant"][:2])
        print(f"  [{c['source']}] {c['prompt'][:50]}...")
        for eid in c["relevant"][:2]:
            print(f"      -> {eid}: {summaries.get(eid,'')[:60]}")


if __name__ == "__main__":
    main()
