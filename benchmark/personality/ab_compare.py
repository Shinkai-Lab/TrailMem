#!/usr/bin/env python3
"""A/B比較ハーネス — 人格維持ベンチマーク。

複数の想起構成を同一ゴールドセットで回し、設計書の比較表フォーマットで
並べる:

    構成          P@5   R@5   MRR   ノイズ率
    baseline      0.42  0.38  0.51  0.22
    +spread       0.55  0.61  0.63  0.28

各構成は (ラベル, recall_cmd, level) で指定する。
trailmem-spread.sh がまだ存在しない場合、その構成はスキップして警告を出す
（baselineだけで動く）。後で spread が出来たら自動で繋がる。

  設定方法:
   - デフォルト: baseline=recall/deep, 加えて recall/dig, spread/None
   - --configs "ラベル:cmd:level,..." で任意指定
   - 環境変数 AB_CONFIGS でも同形式で指定可

本番DBは各構成ごとに一時コピーを作って実行（読み取りのみ保証）。
"""
import argparse
import json
import os

from common import load_goldset, GOLDSET, make_scratch_db, DEFAULT_DB
import recall_runner
import eval as ev

# (label, recall_cmd, level)
DEFAULT_CONFIGS = [
    ("baseline(recall/deep)", "recall", "deep"),
    ("recall/dig(掘り上限)",   "recall", "dig"),
    ("+spread",                "spread", None),
]


def parse_configs(spec):
    cfgs = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        bits = part.split(":")
        label = bits[0]
        cmd = bits[1] if len(bits) > 1 and bits[1] else "recall"
        level = bits[2] if len(bits) > 2 and bits[2] else None
        cfgs.append((label, cmd, level))
    return cfgs


def run_config(cases, recall_cmd, level, limit):
    """1構成を実行。スクリプト未実装なら None を返す。"""
    name, path = recall_runner.resolve_cmd(recall_cmd)
    if not os.path.exists(path):
        return None, f"未実装: {path}"
    scratch = make_scratch_db(DEFAULT_DB)
    try:
        per_case = ev.evaluate(cases, recall_cmd=recall_cmd, level=level,
                              limit=limit, db_path=scratch)
    except FileNotFoundError as e:
        return None, str(e)
    finally:
        for ext in ("", "-wal", "-shm"):
            try:
                os.remove(scratch + ext)
            except OSError:
                pass
    return ev.aggregate(per_case), None


def print_comparison(results):
    """results: [(label, agg or None, note)]"""
    print()
    print("=== A/B比較 (人格維持ベンチマーク) ===")
    header = f"{'構成':<26} {'P@5':>6} {'R@5':>6} {'MRR':>6} "
    header += f"{'ノイズ率':>8} {'cov':>6}"
    print(header)
    print("-" * len(header))
    valid = []
    for label, agg, note in results:
        if agg is None:
            print(f"{label:<26} {'-- ' + (note or 'skip'):>}")
            continue
        print(f"{label:<26} {agg['P@5']:>6.3f} {agg['R@5']:>6.3f} "
              f"{agg['MRR']:>6.3f} {agg['noise_rate']:>8.3f} "
              f"{agg['coverage']:>6.3f}")
        valid.append((label, agg))

    if len(valid) >= 2:
        print()
        print("各指標の勝者:")
        for metric in ("P@5", "R@5", "MRR", "coverage"):
            best = max(valid, key=lambda x: x[1][metric])
            print(f"  {metric:<9}: {best[0]} ({best[1][metric]:.3f})")
        # ノイズ率は低いほど良い
        best_noise = min(valid, key=lambda x: x[1]["noise_rate"])
        print(f"  {'noise_rate':<9}: {best_noise[0]} "
              f"({best_noise[1]['noise_rate']:.3f}) [低いほど良い]")
    elif len(valid) == 1:
        print()
        print("(比較対象が1つのみ。spread実装後に再実行でA/Bが揃う)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--goldset", default=GOLDSET)
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--configs", default=os.environ.get("AB_CONFIGS"),
                    help='"ラベル:cmd:level,..." 形式')
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    cases = load_goldset(args.goldset)
    configs = parse_configs(args.configs) if args.configs else DEFAULT_CONFIGS

    results = []
    for label, cmd, level in configs:
        agg, note = run_config(cases, cmd, level, args.limit)
        results.append((label, agg, note))
        if agg is None:
            print(f"[skip] {label}: {note}")

    print_comparison(results)

    out = args.out or os.path.join(os.path.dirname(GOLDSET), "ab_compare.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(
            {"configs": [{"label": l, "aggregate": a, "note": n}
                         for l, a, n in results]},
            f, ensure_ascii=False, indent=2)
    print(f"\n  -> {out}")


if __name__ == "__main__":
    main()
