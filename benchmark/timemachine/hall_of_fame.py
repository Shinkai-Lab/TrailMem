#!/usr/bin/env python3
"""hall_of_fame.py — floor on のDBから「殿堂入り」記憶を抽出。

繰り返し想起されて定着した記憶 = 想起回数R(recall_history長)と
effective_strength の上位。「こころ」を読み終えた TrailMem に何が残ったか。

Usage:
  python3 hall_of_fame.py --db kokoro_faithful_on_nosp.db --top 20
"""
import argparse
import json
import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    db = args.db if os.path.isabs(args.db) else os.path.join(HERE, args.db)
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)

    rows = con.execute("""
        SELECT ek.episode_id, ek.keyword,
               json_array_length(
                 CASE WHEN json_valid(ek.recall_history)
                      THEN ek.recall_history ELSE '[]' END) AS R,
               ek.effective_strength AS strength,
               e.summary, e.sentiment_neg, e.sentiment_pos
        FROM episode_keywords ek
        JOIN episodes e ON e.id = ek.episode_id
        WHERE ek.is_deleted = 0
        ORDER BY R DESC, strength DESC
    """).fetchall()

    # キーワード行単位の上位（最も想起された (episode, keyword) ペア）
    top_kw = rows[:args.top]

    # 最も想起されたキーワード（recall_history合計でランク）
    kw_total = {}
    for ep_id, kw, R, strength, summary, neg, pos in rows:
        kw_total[kw] = kw_total.get(kw, 0) + R
    top_kw_agg = sorted(kw_total.items(), key=lambda x: -x[1])[:args.top]

    print(f"=== 殿堂入り記憶: (episode, keyword)ペア 上位{args.top} (DB={os.path.basename(db)}) ===")
    print(f"{'R':>4} {'strength':>9}  {'kw':<10} summary")
    print("-" * 100)
    result = []
    for ep_id, kw, R, strength, summary, neg, pos in top_kw:
        s = (summary or "")[:70]
        print(f"{R:>4} {strength:>9.3f}  {kw:<10} {s}")
        result.append({"episode_id": ep_id, "keyword": kw, "R": R,
                       "strength": round(strength, 4), "summary": summary,
                       "neg": neg, "pos": pos})

    print(f"\n=== 最も繰り返し想起されたキーワード 上位{args.top} (recall合計) ===")
    for kw, tot in top_kw_agg:
        print(f"  {tot:>5}  {kw}")

    con.close()

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"db": os.path.basename(db),
                       "top_episode_keyword": result,
                       "top_keyword_by_recall": top_kw_agg},
                      f, ensure_ascii=False, indent=2)
        print(f"\n  -> {args.out}")


if __name__ == "__main__":
    main()
