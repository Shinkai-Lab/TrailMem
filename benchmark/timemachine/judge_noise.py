#!/usr/bin/env python3
"""judge_noise.py — 発火ペアを Sonnet で relevant/noise 判定（逐次保存・再開対応）。

発火セットは A==B（1hop）、C==D（1hop+spread）で重複するため、判定対象を
distinct な発火ペアに絞る:
  - 1hop プール（全構成共通）から最大50
  - spread プール（C/D共通）から最大50
1件判定するごとに data/noise_judgments.jsonl へ追記。再実行時は判定済みを
スキップして続きから（kill されても進捗が消えない）。
最後に data/noise_results.json へ集計を書く。
"""
import json
import os
import random
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
JLOG = os.path.join(HERE, "data", "noise_judgments.jsonl")
RESULTS = os.path.join(HERE, "data", "noise_results.json")

PROMPT = """あなたは小説『こころ』(夏目漱石)を読んだAIの「連想記憶」が自然かを評価します。

ある場面（今このターンで語られていること）に対し、AIの心に過去の記憶がふと
浮かびました（フラッシュバック）。その記憶が、この場面の文脈に関連して浮かぶのが
自然か、それとも文脈と無関係な雑音(ノイズ)かを判定してください。

【今の場面】
要約: {input_summary}
キーワード: {input_keywords}

【浮かんだ過去の記憶】（きっかけのキーワード: 「{via_keyword}」）
要約: {recalled_summary}

この記憶が今の場面で浮かぶのは自然(relevant)か、無関係な雑音(noise)か?
テーマ・人物・感情・状況のいずれかで意味的につながっていれば relevant。
単に同じ単語が偶然かぶっただけで文脈的に無関係なら noise。
必ず relevant か noise の一語だけで答えてください。"""


def judge(pair):
    prompt = PROMPT.format(
        input_summary=pair["input_summary"],
        input_keywords="、".join(pair["input_keywords"]),
        via_keyword=pair["via_keyword"],
        recalled_summary=pair["recalled_summary"])
    r = subprocess.run(["claude", "-p", "--model", "sonnet"],
                       input=prompt, capture_output=True, text=True, timeout=120)
    out = r.stdout.strip().lower()
    if "noise" in out and "relevant" not in out:
        return "noise"
    if "relevant" in out:
        return "relevant"
    return "noise" if "noise" in out else "relevant"


def collect_distinct(ptype, max_n=50, seed=7):
    src = "A" if ptype == "1hop" else "C"
    log = [json.loads(l) for l in
           open(os.path.join(HERE, "data", f"flashback_log_{src}.jsonl"),
                encoding="utf-8") if l.strip()]
    pool = {}
    for r in log:
        fbs = r["flashbacks"] if ptype == "1hop" else r["spread_flashbacks"]
        for fb in fbs:
            key = f"{ptype}:{r['turn']}:{fb['episode_id']}:{fb['via_keyword']}"
            pool[key] = {"key": key, "type": ptype, "turn": r["turn"],
                         "input_summary": r["input_summary"],
                         "input_keywords": r["input_keywords"],
                         "via_keyword": fb["via_keyword"],
                         "recalled_episode_id": fb["episode_id"],
                         "recalled_summary": fb["summary"]}
    items = list(pool.values())
    total = len(items)
    sampled = total > max_n
    if sampled:
        items = random.Random(seed).sample(items, max_n)
    return items, total, sampled


def load_done():
    done = {}
    if os.path.exists(JLOG):
        for l in open(JLOG, encoding="utf-8"):
            if l.strip():
                r = json.loads(l)
                done[r["key"]] = r
    return done


def main():
    targets = {}
    meta = {}
    for ptype in ["1hop", "spread"]:
        items, total, sampled = collect_distinct(ptype, max_n=50)
        meta[ptype] = {"total_fires": total, "sampled": sampled,
                       "n_target": len(items)}
        for it in items:
            targets[it["key"]] = it

    done = load_done()
    todo = [k for k in targets if k not in done]
    print(f"targets={len(targets)} done={len(done)} todo={len(todo)}", flush=True)

    lock = threading.Lock()
    fout = open(JLOG, "a", encoding="utf-8")
    counter = {"n": 0}

    def work(key):
        p = dict(targets[key])
        try:
            p["verdict"] = judge(p)
        except Exception as e:
            p["verdict"] = "relevant"
            p["error"] = str(e)
        with lock:
            fout.write(json.dumps(p, ensure_ascii=False) + "\n")
            fout.flush()
            counter["n"] += 1
            print(f"[{counter['n']}/{len(todo)}] {p['type']} turn{p['turn']} "
                  f"via「{p['via_keyword']}」 -> {p['verdict']}", flush=True)

    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(work, todo))
    fout.close()

    # 集計
    done = load_done()
    results = {}
    for ptype in ["1hop", "spread"]:
        ps = [v for k, v in done.items() if v["type"] == ptype and k in targets]
        n_noise = sum(1 for v in ps if v["verdict"] == "noise")
        results[ptype] = {**meta[ptype], "n_judged": len(ps),
                          "n_noise": n_noise,
                          "noise_rate": n_noise / max(1, len(ps)),
                          "pairs": ps}
        print(f"{ptype}: noise {n_noise}/{len(ps)} = "
              f"{n_noise/max(1,len(ps)):.3f}")
    with open(RESULTS, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"-> {RESULTS}")


if __name__ == "__main__":
    main()
