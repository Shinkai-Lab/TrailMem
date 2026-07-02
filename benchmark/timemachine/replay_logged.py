#!/usr/bin/env python3
"""replay_logged.py — フラッシュバック発火ログ付きの時系列リプレイ。

replay_faithful.py の時間発展（毎ターン insert→micro_decay→scan→spread）を
そのまま使いつつ、**各ターンのフラッシュバック発火を JSONL に記録**する。

各ターンのログ行 (1行=1ターン):
  {
    "turn": 5,
    "input_pid": "p0007-e01",
    "input_summary": "...そのターンの段落要約...",
    "input_keywords": [...],          # そのターンの入力キーワード（=種）
    "fired": 1,                       # 0/1: このターンで何か発火したか
    "n_flashback": 2,                 # 1ホップ発火数
    "n_spread": 1,                    # spread連想発火数
    "flashbacks": [                   # 1ホップで浮かんだ記憶
      {"episode_id":"kokoro-...", "summary":"...", "via_keyword":"先生"}, ...
    ],
    "spread_flashbacks": [            # spread連想で浮かんだ記憶
      {"episode_id":"kokoro-...", "summary":"...", "via_keyword":"孤独"}, ...
    ]
  }

本番 trailmem.db には触らない。引数のベンチ専用DBのみ書き込む。

Usage:
  python3 replay_logged.py --db kokoro_log_on_sp.db \
      --episodes data/kokoro_episodes_faithful_full.json \
      --floor on --spread on --log data/flashback_log_D.jsonl
"""
import argparse
import json
import math
import os
import sqlite3
from collections import defaultdict

import make_db
import replay_faithful as rp

HERE = os.path.dirname(os.path.abspath(__file__))


def scan_flashback_logged(con, seed_keywords, turn_seq, self_ep_id=None):
    """rp.scan_flashback と同一ロジック。発火した記憶の詳細も返す。

    self_ep_id: そのターンに insert したばかりのエピソードID。
      ベンチでは「その段落のキーワードで scan」するため、直前に入れた本人が
      必ずトップヒットする（=自己エコー）。これは「過去記憶の想起」ではないので
      候補から除外し、実運用の『会話入力に対する想起』に近づける。
      （除外しても rp 本体の DB 更新側ロジックとは独立。ログ専用の補正。）
    """
    fired = 0
    shown = set()
    details = []
    for kw in seed_keywords:
        # 自己エコーを除外して「過去記憶」のトップ1を取る
        row = con.execute("""
            SELECT e.id, e.sentiment_neg, e.sentiment_pos, e.created_turn_seq,
                   ek.effective_strength, ek.last_recalled_seq, e.summary
            FROM episode_keywords ek
            JOIN episodes e ON e.id = ek.episode_id
            WHERE ek.keyword = ? AND ek.is_deleted = 0
              AND e.id != ?
              AND (? - ek.last_recalled_seq) > ?
            ORDER BY ek.effective_strength * (e.sentiment_pos + e.sentiment_neg)/100.0 DESC
            LIMIT 1
        """, (kw, self_ep_id or "", turn_seq, rp.FLASHBACK_COOLDOWN)).fetchone()
        if not row:
            continue
        ep_id, neg, pos, created_seq, strength, last_seq, summary = row
        s = (neg + pos) / 100.0
        decay = 0.995 ** max(0, turn_seq - created_seq)
        score = strength * s * decay
        if score <= rp.FLASHBACK_THRESHOLD:
            continue
        con.execute("""
            UPDATE episode_keywords
            SET last_recalled_seq = ?,
                effective_strength = MIN(1.0, effective_strength * ?),
                recall_history = json_insert(
                    CASE WHEN json_valid(recall_history) THEN recall_history ELSE '[]' END,
                    '$[#]', ?)
            WHERE episode_id = ? AND keyword = ?
        """, (turn_seq, rp.RECALL_BOOST, turn_seq, ep_id, kw))
        fired += 1
        shown.add(ep_id)
        details.append({"episode_id": ep_id, "summary": summary,
                        "via_keyword": kw, "score": round(score, 4)})
    return fired, shown, details


def spread_flashback_logged(con, seed_keywords, turn_seq, already, self_ep_id=None):
    """rp.spread_flashback と同一ロジック。発火した記憶の詳細も返す。
    自己エコー（直前 insert したエピソード）は候補から除外する。"""
    if not rp.has_table(con, "keyword_edges"):
        return 0, []
    adj = defaultdict(list)
    degree = defaultdict(int)
    for a, b, w in con.execute("SELECT kw_a,kw_b,weight FROM keyword_edges"):
        adj[a].append((b, w)); adj[b].append((a, w))
        degree[a] += 1; degree[b] += 1
    if not adj:
        return 0, []
    seeds = [k.lower() for k in seed_keywords]

    def penalty(n):
        d = degree.get(n, 0)
        return 1.0 / math.log(1 + d) if d > 0 else 1.0

    A = defaultdict(float)
    for s in seeds:
        A[s] = 1.0
    hop1 = defaultdict(float)
    for node in seeds:
        for nb, w in adj.get(node, []):
            hop1[nb] += rp.BETA * w * 1.0 * penalty(nb)
    for nb, inc in hop1.items():
        A[nb] += inc
    hop2 = defaultdict(float)
    for node, act in hop1.items():
        for nb, w in adj.get(node, []):
            if nb in seeds:
                continue
            hop2[nb] += (rp.BETA ** 2) * w * act * penalty(nb)
    for nb, inc in hop2.items():
        A[nb] += inc

    spread_nodes = {n: a for n, a in A.items() if n not in seeds}
    if spread_nodes:
        a_max = max(spread_nodes.values())
        if a_max > 0:
            for n in spread_nodes:
                if A[n] < a_max:
                    A[n] = max(0.0, A[n] - rp.GAMMA * a_max)
            a_max2 = max((A[n] for n in spread_nodes), default=0.0)
            if a_max2 > 0:
                for n in spread_nodes:
                    A[n] = A[n] / a_max2

    warmed = {n: a for n, a in A.items() if a >= rp.THETA}
    if not warmed:
        return 0, []
    placeholders = ",".join("?" for _ in warmed)
    ep_score = {}
    ep_kw = {}
    for r in con.execute(f"""
        SELECT ek.episode_id, ek.keyword, ek.effective_strength
        FROM episode_keywords ek
        WHERE ek.keyword IN ({placeholders}) AND ek.is_deleted = 0
    """, list(warmed.keys())):
        score = r[2] * warmed[r[1]]
        if score > ep_score.get(r[0], -1):
            ep_score[r[0]] = score
            ep_kw[r[0]] = r[1]
    top = sorted(ep_score.items(), key=lambda x: -x[1])[:3]
    fired = 0
    details = []
    for ep_id, sc in top:
        if ep_id in already or ep_id == self_ep_id:
            continue
        kw = ep_kw[ep_id]
        con.execute("""
            UPDATE episode_keywords
            SET last_recalled_seq = ?,
                effective_strength = MIN(1.0, effective_strength * ?),
                recall_history = json_insert(
                    CASE WHEN json_valid(recall_history) THEN recall_history ELSE '[]' END,
                    '$[#]', ?)
            WHERE episode_id = ? AND keyword = ?
        """, (turn_seq, rp.RECALL_BOOST, turn_seq, ep_id, kw))
        summary = con.execute("SELECT summary FROM episodes WHERE id=?",
                              (ep_id,)).fetchone()[0]
        fired += 1
        details.append({"episode_id": ep_id, "summary": summary,
                        "via_keyword": kw, "activation_score": round(sc, 4)})
    return fired, details


def replay_logged(db_path, episodes, floor_on, spread_on, log_path):
    con = sqlite3.connect(db_path)
    with_edges = spread_on and rp.has_table(con, "keyword_edges")
    turn_seq = 0
    stats = {"inserted": 0, "flashbacks": 0, "assoc_flashbacks": 0,
             "turns": 0, "fired_turns": 0, "fired_turns_1hop": 0}

    logf = open(log_path, "w", encoding="utf-8")
    for ep in episodes:
        turn_seq += 1
        stats["turns"] += 1
        rp.insert_episode(con, ep, turn_seq, with_edges)
        stats["inserted"] += 1
        rp.micro_decay(con, floor_on)
        self_ep_id = f"kokoro-{ep['pid']}"
        seeds = list(dict.fromkeys(k.lower() for k in ep["keywords"]))
        fb, shown, fb_details = scan_flashback_logged(
            con, seeds, turn_seq, self_ep_id=self_ep_id)
        stats["flashbacks"] += fb
        sp_fired = 0
        sp_details = []
        if spread_on:
            sp_fired, sp_details = spread_flashback_logged(
                con, seeds, turn_seq, already=shown, self_ep_id=self_ep_id)
            stats["assoc_flashbacks"] += sp_fired

        total_fired = fb + sp_fired
        if total_fired > 0:
            stats["fired_turns"] += 1
        if fb > 0:
            stats["fired_turns_1hop"] += 1

        rec = {
            "turn": turn_seq,
            "input_pid": ep["pid"],
            "input_summary": ep["summary"],
            "input_keywords": seeds,
            "input_neg": ep["neg"],
            "input_pos": ep["pos"],
            "fired": 1 if total_fired > 0 else 0,
            "n_flashback": fb,
            "n_spread": sp_fired,
            "flashbacks": fb_details,
            "spread_flashbacks": sp_details,
        }
        logf.write(json.dumps(rec, ensure_ascii=False) + "\n")

        con.execute("UPDATE trailmem_meta SET value=? WHERE key='turn_seq'",
                    (str(turn_seq),))
        if turn_seq % rp.COMMIT_EVERY == 0:
            con.commit()

    con.execute("UPDATE trailmem_meta SET value=? WHERE key='turn_seq'",
                (str(turn_seq),))
    con.commit()
    con.close()
    logf.close()
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--episodes", required=True)
    ap.add_argument("--floor", choices=["on", "off"], default="on")
    ap.add_argument("--spread", choices=["on", "off"], default="off")
    ap.add_argument("--log", required=True)
    args = ap.parse_args()

    db = args.db if os.path.isabs(args.db) else os.path.join(HERE, args.db)
    eps = json.load(open(args.episodes, encoding="utf-8"))
    floor_on = args.floor == "on"
    spread_on = args.spread == "on"

    # 毎回まっさらなDBから（育成済みDBの汚染を避ける）
    make_db.build(db, with_edges=spread_on)

    print(f"replay_logged: db={os.path.basename(db)} episodes={len(eps)} "
          f"floor={args.floor} spread={args.spread}")
    stats = replay_logged(db, eps, floor_on, spread_on, args.log)
    inj_all = stats["fired_turns"] / max(1, stats["turns"])
    inj_1hop = stats["fired_turns_1hop"] / max(1, stats["turns"])
    print(f"  turns={stats['turns']} fired_turns={stats['fired_turns']} "
          f"(1hop_only={stats['fired_turns_1hop']})")
    print(f"  flashbacks={stats['flashbacks']} assoc={stats['assoc_flashbacks']}")
    print(f"  injection_rate(all)={inj_all:.3f} injection_rate(1hop)={inj_1hop:.3f}")
    print(f"  -> {args.log}")
    return stats


if __name__ == "__main__":
    main()
