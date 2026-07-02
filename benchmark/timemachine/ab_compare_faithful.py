#!/usr/bin/env python3
"""ab_compare_faithful.py — タイムマシン・ベンチ A/B 比較（faithfulパイプライン版）。

ab_compare.py との違い:
  - replay ではなく **replay_faithful**（毎ターン decay+scan, synonyms付与）を使う
  - 加速エイジングを環境変数で制御できる（TM_DECAY_RATE / TM_N_CONSOLIDATE）
  - spread構成の eval は TRAILMEM_EP_LIMIT を limit に合わせる（R@10を公平に測る）

4構成を同一ゴールドセットで比較:
  A  floor=off spread=off   (旧decay・キーワード想起 = baseline)
  B  floor=on  spread=off   (フロア方式・キーワード想起)
  C  floor=off spread=on    (旧decay・活性化拡散)
  D  floor=on  spread=on    (フロア方式・活性化拡散 = フルスタック)

Usage:
  python3 ab_compare_faithful.py \
      --episodes data/kokoro_episodes_faithful_full.json \
      --goldset goldset_faithful.jsonl --prefix kokoro_faithful --level deep
"""
import argparse
import json
import os

import make_db
import replay_faithful as rp
import eval as ev

HERE = os.path.dirname(os.path.abspath(__file__))

CONFIGS = [
    ("A floor=off spread=off", False, False),
    ("B floor=on  spread=off", True,  False),
    ("C floor=off spread=on",  False, True),
    ("D floor=on  spread=on",  True,  True),
]


def build_and_eval(label, floor, spread, episodes, cases, prefix, level, limit):
    tag = ("on" if floor else "off") + "_" + ("sp" if spread else "nosp")
    db = os.path.join(HERE, f"{prefix}_{tag}.db")
    make_db.build(db, with_edges=spread)
    stats = rp.replay(db, episodes, floor_on=floor, spread_on=spread)
    cmd = "spread" if spread else "recall"
    lvl = None if spread else level
    # spread構成は EP_LIMIT を limit に合わせる（既定3だとR@10が頭打ちになる）
    prev = os.environ.get("TRAILMEM_EP_LIMIT")
    os.environ["TRAILMEM_EP_LIMIT"] = str(limit)
    try:
        agg, _per = ev.run_eval(db, cmd, lvl, limit, cases)
    finally:
        if prev is None:
            os.environ.pop("TRAILMEM_EP_LIMIT", None)
        else:
            os.environ["TRAILMEM_EP_LIMIT"] = prev
    return {"label": label, "db": os.path.basename(db), "floor": floor,
            "spread": spread, "recall_cmd": cmd, "level": lvl,
            "replay_stats": stats, "aggregate": agg}


def print_table(results):
    print("\n=== A/B比較: タイムマシン・ベンチ (こころ, faithful) ===")
    h = (f"{'構成':<24} {'想起率':>6} {'cov':>6} {'MRR':>6} "
         f"{'P@5':>6} {'R@5':>6} {'P@10':>6} {'R@10':>6}")
    print(h)
    print("-" * len(h))
    for r in results:
        a = r["aggregate"]
        print(f"{r['label']:<24} {a['answered_rate']:>6.3f} {a['coverage']:>6.3f} "
              f"{a['MRR']:>6.3f} {a['P@5']:>6.3f} {a['R@5']:>6.3f} "
              f"{a['P@10']:>6.3f} {a['R@10']:>6.3f}")

    print("\nフロア方式の純効果 (spread固定, B-A と D-C):")
    by = {(r["floor"], r["spread"]): r["aggregate"] for r in results}
    for spread, name in [(False, "spread=off (B-A)"), (True, "spread=on (D-C)")]:
        on = by.get((True, spread)); off = by.get((False, spread))
        if on and off:
            print(f"  {name}: ΔMRR={on['MRR']-off['MRR']:+.3f} "
                  f"Δcov={on['coverage']-off['coverage']:+.3f} "
                  f"ΔR@10={on['R@10']-off['R@10']:+.3f}")

    print("\nspread効果 (floor固定, C-A と D-B):")
    for floor, name in [(False, "floor=off (C-A)"), (True, "floor=on (D-B)")]:
        sp = by.get((floor, True)); nosp = by.get((floor, False))
        if sp and nosp:
            print(f"  {name}: ΔMRR={sp['MRR']-nosp['MRR']:+.3f} "
                  f"Δcov={sp['coverage']-nosp['coverage']:+.3f} "
                  f"ΔR@10={sp['R@10']-nosp['R@10']:+.3f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--episodes", required=True)
    ap.add_argument("--goldset", default=os.path.join(HERE, "goldset_faithful.jsonl"))
    ap.add_argument("--prefix", default="kokoro_faithful")
    ap.add_argument("--level", default="deep")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--out", default=os.path.join(HERE, "ab_compare_faithful.json"))
    args = ap.parse_args()

    with open(args.episodes, encoding="utf-8") as f:
        episodes = json.load(f)
    cases = [json.loads(l) for l in open(args.goldset, encoding="utf-8") if l.strip()]
    decay = os.environ.get("TM_DECAY_RATE", "0.999")
    ncons = os.environ.get("TM_N_CONSOLIDATE", "30")
    print(f"episodes={len(episodes)} goldset={len(cases)} level={args.level} "
          f"DECAY_RATE={decay} N_CONSOLIDATE={ncons}")

    results = []
    for label, floor, spread in CONFIGS:
        print(f"\n>>> {label}")
        r = build_and_eval(label, floor, spread, episodes, cases,
                           args.prefix, args.level, args.limit)
        st = r["replay_stats"]
        print(f"    replay: inserted={st['inserted']} flashbacks={st['flashbacks']} "
              f"assoc={st['assoc_flashbacks']} ({st['replay_seconds']:.2f}s)")
        a = r["aggregate"]
        print(f"    eval: MRR={a['MRR']:.3f} cov={a['coverage']:.3f} "
              f"R@10={a['R@10']:.3f}")
        results.append(r)

    print_table(results)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"episodes": len(episodes), "goldset": len(cases),
                   "level": args.level,
                   "decay_rate": float(decay), "n_consolidate": int(ncons),
                   "results": results},
                  f, ensure_ascii=False, indent=2)
    print(f"\n  -> {args.out}")


if __name__ == "__main__":
    main()
