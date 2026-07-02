#!/usr/bin/env python3
"""eval.py — 育てたDBに対しゴールドセットで想起テストし、指標を計算。

personality/eval.py の指標(P@k, R@k, MRR, カバレッジ, ノイズ率)を流用。
違い: 想起のシードキーワードは goldset の各問いが持つ keywords を使う
（DBスキャン推定より公平で再現性が高い）。

想起実装は実運用スクリプトを呼ぶ:
  recall : trailmem-recall.sh   （キーワード単層想起 / baseline）
  spread : trailmem-spread.sh   （活性化拡散 / アメーバ網）

DBは引数で受け取り読み取り専用コピーで実行（育てたDBを汚さない）。

Usage:
  python3 eval.py --db kokoro_B.db --recall-cmd recall --level deep
  python3 eval.py --db kokoro_B.db --recall-cmd spread
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
# trailmem-recall.sh / trailmem-spread.sh は oss リポジトリのルート直下にある
# (benchmark/timemachine から2階層上)。別の場所に置いている場合は
# TRAILMEM_SCRIPTS_DIR で上書きできる。
SCRIPTS = os.environ.get(
    "TRAILMEM_SCRIPTS_DIR", os.path.abspath(os.path.join(HERE, "..", ".."))
)
CMD_MAP = {
    "recall": os.path.join(SCRIPTS, "trailmem-recall.sh"),
    "spread": os.path.join(SCRIPTS, "trailmem-spread.sh"),
}
KS = (3, 5, 10)
ID_LINE = re.compile(r"^\[([^\]]+)\]\s")
SPREAD_LINE = re.compile(r"^\s+\[\d+\.\d+\]\s+\([^)]*\)\s+(.+)$")


def precision_at_k(ranked, relevant, k):
    topk = ranked[:k]
    if not topk:
        return 0.0
    return sum(1 for x in topk if x in relevant) / len(topk)


def recall_at_k(ranked, relevant, k):
    if not relevant:
        return 0.0
    topk = set(ranked[:k])
    return sum(1 for r in relevant if r in topk) / len(relevant)


def rr(ranked, relevant):
    for i, x in enumerate(ranked, 1):
        if x in relevant:
            return 1.0 / i
    return 0.0


def scratch_copy(db):
    fd, dst = tempfile.mkstemp(prefix="kokoro_eval_", suffix=".db")
    os.close(fd)
    shutil.copy2(db, dst)
    return dst


def summary_index(db):
    con = __import__("sqlite3").connect(f"file:{db}?mode=ro", uri=True)
    idx = [(r[0], re.sub(r"\s+", "", (r[1] or "")))
           for r in con.execute("SELECT id, summary FROM episodes")]
    con.close()
    return idx


def run_recall(keywords, cmd, level, limit, db, sidx):
    name = cmd
    path = CMD_MAP.get(cmd, cmd)
    if not keywords:
        return []
    env = dict(os.environ)
    env["TRAILMEM_DB"] = db
    env["TRAILMEM_LIMIT"] = str(limit)
    if level:
        env["TRAILMEM_LEVEL"] = level
    kws = [k.lower() for k in keywords]
    try:
        out = subprocess.run(["bash", path, *kws], capture_output=True,
                             text=True, env=env, timeout=120).stdout
    except subprocess.TimeoutExpired:
        return []
    ids = []
    for line in out.splitlines():
        m = ID_LINE.match(line)
        if m:
            cand = m.group(1).strip()
            try:
                float(cand)
            except ValueError:
                ids.append(cand)
                continue
        m2 = SPREAD_LINE.match(line)
        if m2:
            frag = re.sub(r"\s+", "", m2.group(1))[:30]
            if len(frag) >= 8:
                for eid, ns in sidx:
                    if ns.startswith(frag):
                        ids.append(eid)
                        break
    seen = set()
    return [i for i in ids if not (i in seen or seen.add(i))]


def evaluate(cases, cmd, level, limit, db):
    sidx = summary_index(db)
    per = []
    for c in cases:
        relevant = set(c["relevant"])
        ranked = run_recall(c.get("keywords", []), cmd, level, limit, db, sidx)
        rec = {"id": c["id"], "n_relevant": len(relevant),
               "n_returned": len(ranked), "rr": rr(ranked, relevant),
               "covered": any(x in relevant for x in ranked)}
        for k in KS:
            rec[f"p@{k}"] = precision_at_k(ranked, relevant, k)
            rec[f"r@{k}"] = recall_at_k(ranked, relevant, k)
        per.append(rec)
    return per


def aggregate(per):
    n = len(per) or 1
    agg = {"n_cases": len(per)}
    for k in KS:
        agg[f"P@{k}"] = sum(r[f"p@{k}"] for r in per) / n
        agg[f"R@{k}"] = sum(r[f"r@{k}"] for r in per) / n
    agg["MRR"] = sum(r["rr"] for r in per) / n
    agg["coverage"] = sum(1 for r in per if r["covered"]) / n
    agg["answered_rate"] = sum(1 for r in per if r["n_returned"] > 0) / n
    return agg


def print_table(agg, label):
    print(f"\n=== 評価: {label} ===")
    print(f"  ケース数   : {agg['n_cases']}")
    print(f"  想起あり率 : {agg['answered_rate']:.3f}")
    print(f"  カバレッジ : {agg['coverage']:.3f}")
    print(f"  MRR        : {agg['MRR']:.3f}")
    print("  ----  P@k    R@k")
    for k in KS:
        print(f"  k={k:<3} {agg[f'P@{k}']:.3f}  {agg[f'R@{k}']:.3f}")


def run_eval(db, cmd, level, limit, cases):
    """A/B比較から呼ばれる共通エントリ。aggregate を返す。"""
    db = db if os.path.isabs(db) else os.path.join(HERE, db)
    scratch = scratch_copy(db)
    try:
        per = evaluate(cases, cmd, level, limit, scratch)
    finally:
        try:
            os.remove(scratch)
        except OSError:
            pass
    return aggregate(per), per


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--goldset", default=os.path.join(HERE, "goldset.jsonl"))
    ap.add_argument("--recall-cmd", default="recall")
    ap.add_argument("--level", default=None)
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    cases = [json.loads(l) for l in open(args.goldset, encoding="utf-8") if l.strip()]
    agg, per = run_eval(args.db, args.recall_cmd, args.level, args.limit, cases)
    label = f"{os.path.basename(args.db)} / {args.recall_cmd}" + \
            (f"/{args.level}" if args.level else "")
    print_table(agg, label)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"config": {"db": args.db, "cmd": args.recall_cmd,
                                  "level": args.level},
                       "aggregate": agg, "per_case": per},
                      f, ensure_ascii=False, indent=2)
        print(f"  -> {args.out}")


if __name__ == "__main__":
    main()
