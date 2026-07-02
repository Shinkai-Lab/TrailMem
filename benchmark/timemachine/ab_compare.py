#!/usr/bin/env python3
"""ab_compare.py — タイムマシン・ベンチの A/B 比較ハーネス。

4構成を同一ゴールドセットで比較する:
  A  floor=off spread=off   (旧decay・キーワード想起 = baseline)
  B  floor=on  spread=off   (フロア方式・キーワード想起)
  C  floor=off spread=on    (旧decay・活性化拡散)
  D  floor=on  spread=on    (フロア方式・活性化拡散 = フルスタック)

各構成について:
  1. 空DB作成（spread構成は keyword_edges 付き）
  2. replay で時系列に育てる（floor on/off）
  3. eval で想起テスト（spread構成は trailmem-spread.sh, 他は trailmem-recall.sh）
  4. 指標を1表に並べる

「フロアあり vs なし」を spread を固定して比べれば、記憶定着の純効果が見える。

Usage:
  python3 ab_compare.py --episodes data/kokoro_episodes.json \\
      --goldset goldset.jsonl --prefix kokoro --level deep
"""
import argparse
import json
import os

import make_db
import replay as rp
import eval as ev

HERE = os.path.dirname(os.path.abspath(__file__))

# (label, floor, spread)
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
    agg, _per = ev.run_eval(db, cmd, lvl, limit, cases)
    return {"label": label, "db": os.path.basename(db), "floor": floor,
            "spread": spread, "recall_cmd": cmd, "level": lvl,
            "replay_stats": stats, "aggregate": agg}


def print_table(results):
    print("\n=== A/B比較: タイムマシン・ベンチ (こころ) ===")
    h = f"{'構成':<24} {'想起率':>6} {'cov':>6} {'MRR':>6} {'P@5':>6} {'R@5':>6} {'P@10':>6} {'R@10':>6}"
    print(h)
    print("-" * len(h))
    for r in results:
        a = r["aggregate"]
        print(f"{r['label']:<24} {a['answered_rate']:>6.3f} {a['coverage']:>6.3f} "
              f"{a['MRR']:>6.3f} {a['P@5']:>6.3f} {a['R@5']:>6.3f} "
              f"{a['P@10']:>6.3f} {a['R@10']:>6.3f}")

    # フロア効果（spread固定で floor on - off）
    print("\nフロア方式の純効果 (spread固定, B-A と D-C):")
    by = {(r["floor"], r["spread"]): r["aggregate"] for r in results}
    for spread, name in [(False, "spread=off (B-A)"), (True, "spread=on (D-C)")]:
        on = by.get((True, spread)); off = by.get((False, spread))
        if on and off:
            print(f"  {name}: ΔMRR={on['MRR']-off['MRR']:+.3f} "
                  f"Δcov={on['coverage']-off['coverage']:+.3f} "
                  f"ΔR@10={on['R@10']-off['R@10']:+.3f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--episodes", required=True)
    ap.add_argument("--goldset", default=os.path.join(HERE, "goldset.jsonl"))
    ap.add_argument("--prefix", default="kokoro")
    ap.add_argument("--level", default="deep",
                    help="recall構成の想起レベル recall|deep|dig")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--out", default=os.path.join(HERE, "ab_compare.json"))
    args = ap.parse_args()

    with open(args.episodes, encoding="utf-8") as f:
        episodes = json.load(f)
    cases = [json.loads(l) for l in open(args.goldset, encoding="utf-8") if l.strip()]
    print(f"episodes={len(episodes)} goldset={len(cases)} level={args.level}")

    results = []
    for label, floor, spread in CONFIGS:
        print(f"\n>>> {label}")
        r = build_and_eval(label, floor, spread, episodes, cases,
                           args.prefix, args.level, args.limit)
        print(f"    replay: {r['replay_stats']}")
        a = r["aggregate"]
        print(f"    eval: MRR={a['MRR']:.3f} cov={a['coverage']:.3f} "
              f"R@10={a['R@10']:.3f}")
        results.append(r)

    print_table(results)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"episodes": len(episodes), "goldset": len(cases),
                   "level": args.level, "results": results},
                  f, ensure_ascii=False, indent=2)
    print(f"\n  -> {args.out}")


if __name__ == "__main__":
    main()
