#!/usr/bin/env python3
"""analyze_injection.py — 注入率の集計とノイズ判定用ペアの抽出。"""
import json
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIGS = ["A", "B", "C", "D"]
LABELS = {"A": "floor=off spread=off", "B": "floor=on  spread=off",
          "C": "floor=off spread=on", "D": "floor=on  spread=on"}


def load(cfg):
    p = os.path.join(HERE, "data", f"flashback_log_{cfg}.jsonl")
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]


def injection_table():
    print("=== 注入率（self-echo除外）===")
    print(f"{'構成':<22} {'総ターン':>6} {'発火T(1hop)':>10} {'注入率1hop':>10} "
          f"{'発火T(spread込)':>13} {'注入率spread込':>12}")
    rows = []
    for cfg in CONFIGS:
        log = load(cfg)
        turns = len(log)
        fired_1hop = sum(1 for r in log if r["n_flashback"] > 0)
        fired_all = sum(1 for r in log if r["fired"] == 1)
        n_fb = sum(r["n_flashback"] for r in log)
        n_sp = sum(r["n_spread"] for r in log)
        rows.append({"cfg": cfg, "turns": turns,
                     "fired_1hop": fired_1hop, "fired_all": fired_all,
                     "inj_1hop": fired_1hop / turns, "inj_all": fired_all / turns,
                     "n_flashback": n_fb, "n_spread": n_sp})
        print(f"{cfg+' '+LABELS[cfg]:<22} {turns:>6} {fired_1hop:>10} "
              f"{fired_1hop/turns:>10.3f} {fired_all:>13} {fired_all/turns:>12.3f}")
    return rows


def extract_pairs(cfg, max_pairs=50, seed=42):
    """発火した (ターン文脈, 浮かんだ記憶) ペアを抽出。"""
    log = load(cfg)
    pairs = []
    for r in log:
        ctx = {"turn": r["turn"], "input_summary": r["input_summary"],
               "input_keywords": r["input_keywords"]}
        for fb in r["flashbacks"]:
            pairs.append({"cfg": cfg, "type": "1hop", "turn": r["turn"],
                          "input_summary": r["input_summary"],
                          "input_keywords": r["input_keywords"],
                          "via_keyword": fb["via_keyword"],
                          "recalled_episode_id": fb["episode_id"],
                          "recalled_summary": fb["summary"]})
        for sp in r["spread_flashbacks"]:
            pairs.append({"cfg": cfg, "type": "spread", "turn": r["turn"],
                          "input_summary": r["input_summary"],
                          "input_keywords": r["input_keywords"],
                          "via_keyword": sp["via_keyword"],
                          "recalled_episode_id": sp["episode_id"],
                          "recalled_summary": sp["summary"]})
    total = len(pairs)
    sampled = False
    if total > max_pairs:
        rng = random.Random(seed)
        pairs = rng.sample(pairs, max_pairs)
        sampled = True
    return pairs, total, sampled


if __name__ == "__main__":
    rows = injection_table()
    with open(os.path.join(HERE, "data", "injection_rates.json"), "w",
              encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    # ノイズ判定用ペアを各構成から抽出
    all_pairs = {}
    for cfg in CONFIGS:
        pairs, total, sampled = extract_pairs(cfg, max_pairs=50)
        all_pairs[cfg] = {"total_fires": total, "sampled": sampled,
                          "n_judged": len(pairs), "pairs": pairs}
        print(f"{cfg}: total_fires={total} judged={len(pairs)} sampled={sampled}")
    with open(os.path.join(HERE, "data", "noise_pairs.json"), "w",
              encoding="utf-8") as f:
        json.dump(all_pairs, f, ensure_ascii=False, indent=2)
    print("-> data/injection_rates.json, data/noise_pairs.json")
