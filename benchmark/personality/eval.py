#!/usr/bin/env python3
"""評価ランナー — 人格維持ベンチマーク。

goldset.jsonl の各 prompt に対して想起を実行し、想起結果と正解集合を
突合して指標を計算する:
  - Precision@k, Recall@k (k=3,5,10)
  - MRR (最初の正解が何位で出るか)
  - カバレッジ (全正解のうち1件以上想起できたケースの割合)
  - ノイズ率 (irrelevant指定があれば、想起に含まれた割合)

切替:
  TRAILMEM_DB   想起対象DB (デフォルト ~/.trailmem/trailmem.db, 読み取りのみ)
  RECALL_CMD    想起実装 recall|spread|<path> (デフォルト recall)
  TRAILMEM_LEVEL 想起レベル recall|deep|dig
                 ※現行DBは最大strength<0.5のため baseline では deep を推奨

結果は JSON とコンソール表で出力。
"""
import argparse
import json
import os

import os as _os

from common import load_goldset, GOLDSET, make_scratch_db, DEFAULT_DB
import recall_runner

KS = (3, 5, 10)


def precision_at_k(ranked, relevant, k):
    if k == 0:
        return 0.0
    topk = ranked[:k]
    if not topk:
        return 0.0
    hit = sum(1 for x in topk if x in relevant)
    return hit / len(topk)


def recall_at_k(ranked, relevant, k):
    if not relevant:
        return 0.0
    topk = set(ranked[:k])
    hit = sum(1 for r in relevant if r in topk)
    return hit / len(relevant)


def rr(ranked, relevant):
    for i, x in enumerate(ranked, 1):
        if x in relevant:
            return 1.0 / i
    return 0.0


def evaluate(cases, recall_cmd=None, level=None, limit=10, db_path=None,
            verbose=False):
    per_case = []
    for c in cases:
        relevant = set(c["relevant"])
        irrelevant = set(c.get("irrelevant", []))
        ranked, kws = recall_runner.recall_for_prompt(
            c["prompt"], recall_cmd=recall_cmd, level=level,
            limit=limit, db_path=db_path)
        noise = (sum(1 for x in ranked if x in irrelevant) / len(ranked)
                 if ranked and irrelevant else 0.0)
        rec = {
            "id": c["id"],
            "source": c.get("source"),
            "n_relevant": len(relevant),
            "n_returned": len(ranked),
            "seed_keywords": kws,
            "rr": rr(ranked, relevant),
            "covered": any(x in relevant for x in ranked),
            "noise": noise,
        }
        for k in KS:
            rec[f"p@{k}"] = precision_at_k(ranked, relevant, k)
            rec[f"r@{k}"] = recall_at_k(ranked, relevant, k)
        per_case.append(rec)
        if verbose:
            print(f"  {c['id']} [{c.get('source')}] "
                  f"ret={len(ranked)} rr={rec['rr']:.2f} "
                  f"cov={'Y' if rec['covered'] else '-'} kw={kws[:4]}")
    return per_case


def aggregate(per_case):
    n = len(per_case) or 1
    agg = {"n_cases": len(per_case)}
    for k in KS:
        agg[f"P@{k}"] = sum(r[f"p@{k}"] for r in per_case) / n
        agg[f"R@{k}"] = sum(r[f"r@{k}"] for r in per_case) / n
    agg["MRR"] = sum(r["rr"] for r in per_case) / n
    agg["coverage"] = sum(1 for r in per_case if r["covered"]) / n
    agg["noise_rate"] = sum(r["noise"] for r in per_case) / n
    answered = sum(1 for r in per_case if r["n_returned"] > 0)
    agg["answered_rate"] = answered / n
    return agg


def print_table(agg, label):
    print()
    print(f"=== 評価結果: {label} ===")
    print(f"  ケース数         : {agg['n_cases']}")
    print(f"  想起あり率       : {agg['answered_rate']:.3f}")
    print(f"  カバレッジ       : {agg['coverage']:.3f}")
    print(f"  MRR              : {agg['MRR']:.3f}")
    print(f"  ノイズ率         : {agg['noise_rate']:.3f}")
    print("  ----  P@k    R@k")
    for k in KS:
        print(f"  k={k:<3} {agg[f'P@{k}']:.3f}  {agg[f'R@{k}']:.3f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--goldset", default=GOLDSET)
    ap.add_argument("--recall-cmd", default=None,
                    help="recall|spread|<path> (env RECALL_CMD)")
    ap.add_argument("--level", default=os.environ.get("TRAILMEM_LEVEL"),
                    help="recall|deep|dig")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--out", default=None, help="結果JSON出力先")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    cases = load_goldset(args.goldset)
    name, _ = recall_runner.resolve_cmd(args.recall_cmd)
    label = f"{name}" + (f"/{args.level}" if args.level else "")

    # 想起スクリプトはDBに書き込むため、本番を汚さないよう一時コピーで実行
    scratch = make_scratch_db(DEFAULT_DB)
    try:
        per_case = evaluate(cases, recall_cmd=args.recall_cmd,
                            level=args.level, limit=args.limit,
                            db_path=scratch, verbose=args.verbose)
    finally:
        for ext in ("", "-wal", "-shm"):
            try:
                _os.remove(scratch + ext)
            except OSError:
                pass
    agg = aggregate(per_case)
    print_table(agg, label)

    result = {"config": {"recall_cmd": name, "level": args.level,
                         "limit": args.limit},
              "aggregate": agg, "per_case": per_case}
    out = args.out or os.path.join(
        os.path.dirname(GOLDSET), f"eval_{name}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n  -> 詳細: {out}")


if __name__ == "__main__":
    main()
