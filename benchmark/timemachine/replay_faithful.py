#!/usr/bin/env python3
"""replay_faithful.py — 実運用フックと同一粒度のタイムマシン・リプレイ。

旧 replay.py との差（実運用への忠実化）:
  | 項目        | 旧 replay.py        | replay_faithful.py（実運用） |
  |-------------|---------------------|------------------------------|
  | decay/scan  | CHUNK_SIZE 件ごと   | **毎ターン（=毎エピソード）** |
  | scan の種   | 直近チャンク全KW    | **そのターンの入力KWのみ**    |
  | synonyms    | 常に '[]'           | **ingest が付けた synonyms** |
  | cutoff      | 無条件 decay        | trailmem_hook の時間cutoffなし(*) |

(*) ベンチは実時間を進めないため last_recalled の時間 cutoff は使わず、
    実運用 hook の「毎ターン全件 micro-decay」相当（cutoff到達後）を毎ターン適用する。

各エピソードを物語順に1件ずつ insert し、**その直後に**:
  1. micro-decay: trailmem_hook.sh の SQL と同式（floor on/off）
  2. scan/flashback: trailmem-scan.sh と同式。そのエピソードのキーワードを種に
     1ホップ・フラッシュバック（recall_history++ / trail強化）。spread構成では
     連想フラッシュバックも。
これが「会話1ターンごとに hook が回る」実運用と同一の時間発展。

本番 trailmem.db には触らない。引数のベンチ専用DBのみ書き込む。

Usage:
  python3 replay_faithful.py --db kokoro_faithful_B.db \\
      --episodes data/kokoro_pilot_episodes_faithful.json --floor on --spread off
"""
import argparse
import json
import math
import os
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone
from itertools import combinations

HERE = os.path.dirname(os.path.abspath(__file__))

# 時間発展パラメータ（trailmem_hook.sh のデフォルトに一致）。
# 実運用は 0.999/ターンだが、ベンチは時間圧縮のため上書き可（旧 replay 踏襲）。
DECAY_RATE = float(os.environ.get("TM_DECAY_RATE", "0.999"))
N_CONSOLIDATE = int(os.environ.get("TM_N_CONSOLIDATE", "30"))
FLOOR_MIN = float(os.environ.get("TM_FLOOR_MIN", "0.1"))
FLOOR_DECAY = float(os.environ.get("TM_FLOOR_DECAY", "0.99999"))
FLASHBACK_COOLDOWN = 10
FLASHBACK_THRESHOLD = 0.30
RECALL_BOOST = 1.05
# spread（アメーバ網）
BETA = 0.35
GAMMA = 0.15
THETA = float(os.environ.get("TM_SPREAD_THETA", "0.3"))
# commit を毎ターンやると遅いので N ターンごとにまとめる（結果は不変）
COMMIT_EVERY = int(os.environ.get("TM_COMMIT_EVERY", "20"))


def has_table(con, name):
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def insert_episode(con, ep, turn_seq, with_edges):
    """エピソード1件をDBへ。auto-ingest.sh のロジック移植（vec省略, synonyms付与）。"""
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ep_id = f"kokoro-{ep['pid']}"
    source_ref = f"kokoro:{ep['chapter']}:{ep['section']}:{ep['pid']}"
    con.execute("""
        INSERT INTO episodes (id, summary, inner, quote, sentiment_neg,
            sentiment_pos, created_at, source_type, source_ref, created_turn_seq)
        VALUES (?,?,?,NULL,?,?,?, 'kokoro', ?, ?)
    """, (ep_id, ep["summary"], ep["inner"], ep["neg"], ep["pos"],
          created_at, source_ref, turn_seq))

    synmap = ep.get("synonyms", {}) or {}
    kw_norms = []
    for kw in ep["keywords"]:
        k = kw.strip().lower()
        if not k:
            continue
        kw_norms.append(k)
        syn_json = json.dumps(synmap.get(k, []), ensure_ascii=False)
        # 既存キーワードを synonyms 付きで作成。既存なら synonyms をマージ拡張
        # （実運用 add.sh/promise.sh が synonyms をマージするのと同じ精神）。
        row = con.execute("SELECT synonyms FROM keywords WHERE keyword=?",
                          (k,)).fetchone()
        if row is None:
            con.execute(
                "INSERT INTO keywords (keyword, synonyms, created_at)"
                " VALUES (?,?,?)", (k, syn_json, created_at))
        else:
            try:
                existing = json.loads(row[0]) if row[0] else []
            except Exception:
                existing = []
            new_syns = synmap.get(k, [])
            merged = list(dict.fromkeys([*existing, *new_syns]))
            if merged != existing:
                con.execute("UPDATE keywords SET synonyms=? WHERE keyword=?",
                            (json.dumps(merged, ensure_ascii=False), k))
        con.execute(
            "INSERT OR IGNORE INTO episode_keywords"
            " (episode_id, keyword, recall_history, actr_base_level)"
            " VALUES (?, ?, ?, 0.0)", (ep_id, k, json.dumps([turn_seq])))

    if with_edges and kw_norms:
        uniq = sorted(set(kw_norms))
        for a, b in combinations(uniq, 2):
            co_kw = [k for k in uniq if k not in (a, b)][:3]
            row = con.execute(
                "SELECT weight, co_count, context FROM keyword_edges"
                " WHERE kw_a=? AND kw_b=?", (a, b)).fetchone()
            if row is None:
                ctx = json.dumps({"emotion": {"neg": ep["neg"], "pos": ep["pos"]},
                                  "co_kw": co_kw, "last_episode": ep_id},
                                 ensure_ascii=False)
                con.execute(
                    "INSERT INTO keyword_edges (kw_a,kw_b,weight,co_count,"
                    "last_traversed_seq,context,created_at) VALUES (?,?,0.1,1,?,?,?)",
                    (a, b, turn_seq, ctx, created_at))
            else:
                old_w, old_co, old_ctx = row
                new_w = min(1.0, old_w + 0.1)
                new_co = old_co + 1
                try:
                    c = json.loads(old_ctx)
                except Exception:
                    c = {}
                em = c.get("emotion", {"neg": ep["neg"], "pos": ep["pos"]})
                em["neg"] = round((em.get("neg", ep["neg"]) * old_co + ep["neg"]) / new_co)
                em["pos"] = round((em.get("pos", ep["pos"]) * old_co + ep["pos"]) / new_co)
                c["emotion"] = em
                c["co_kw"] = co_kw
                c["last_episode"] = ep_id
                con.execute(
                    "UPDATE keyword_edges SET weight=?,co_count=?,"
                    "last_traversed_seq=?,context=? WHERE kw_a=? AND kw_b=?",
                    (new_w, new_co, turn_seq, json.dumps(c, ensure_ascii=False), a, b))
    return ep_id


def micro_decay(con, floor_on):
    """trailmem_hook.sh の micro-decay を移植（毎ターン呼ぶ）。"""
    if floor_on:
        con.execute(f"""
            UPDATE episode_keywords
            SET
              decay = MAX(0.01, decay *
                CASE WHEN json_array_length(
                       CASE WHEN json_valid(recall_history) THEN recall_history ELSE '[]' END
                     ) >= {N_CONSOLIDATE}
                     THEN {FLOOR_DECAY} ELSE {DECAY_RATE} END),
              effective_strength = MAX(
                MIN(1.0, {FLOOR_MIN} + (
                  CAST(json_array_length(
                    CASE WHEN json_valid(recall_history) THEN recall_history ELSE '[]' END
                  ) AS REAL) / {N_CONSOLIDATE}) * (1.0 - {FLOOR_MIN})),
                (CAST((used - misled + 1) AS REAL) / (shown + 2)) *
                  MAX(0.01, decay *
                    CASE WHEN json_array_length(
                           CASE WHEN json_valid(recall_history) THEN recall_history ELSE '[]' END
                         ) >= {N_CONSOLIDATE}
                         THEN {FLOOR_DECAY} ELSE {DECAY_RATE} END)
              )
            WHERE is_deleted = 0
        """)
    else:
        con.execute(f"""
            UPDATE episode_keywords
            SET decay = MAX(0.01, decay * {DECAY_RATE}),
                effective_strength =
                  (CAST((used - misled + 1) AS REAL) / (shown + 2)) *
                  MAX(0.01, decay * {DECAY_RATE})
            WHERE is_deleted = 0
        """)


def scan_flashback(con, seed_keywords, turn_seq):
    """trailmem-scan.sh の1ホップ・フラッシュバックを移植。

    実運用 scan.sh は入力テキストに対し keyword+synonyms でマッチした
    キーワードを種にする。ベンチでは「そのターンの入力 = そのエピソードの
    キーワード集合」なので seed_keywords をそのまま使う（同義）。
    戻り値: (発火数, フラッシュバックで出した episode_id 集合)
    """
    fired = 0
    shown = set()
    for kw in seed_keywords:
        row = con.execute("""
            SELECT e.id, e.sentiment_neg, e.sentiment_pos, e.created_turn_seq,
                   ek.effective_strength, ek.last_recalled_seq
            FROM episode_keywords ek
            JOIN episodes e ON e.id = ek.episode_id
            WHERE ek.keyword = ? AND ek.is_deleted = 0
              AND (? - ek.last_recalled_seq) > ?
            ORDER BY ek.effective_strength * (e.sentiment_pos + e.sentiment_neg)/100.0 DESC
            LIMIT 1
        """, (kw, turn_seq, FLASHBACK_COOLDOWN)).fetchone()
        if not row:
            continue
        ep_id, neg, pos, created_seq, strength, last_seq = row
        s = (neg + pos) / 100.0
        decay = 0.995 ** max(0, turn_seq - created_seq)
        score = strength * s * decay
        if score <= FLASHBACK_THRESHOLD:
            continue
        con.execute("""
            UPDATE episode_keywords
            SET last_recalled_seq = ?,
                effective_strength = MIN(1.0, effective_strength * ?),
                recall_history = json_insert(
                    CASE WHEN json_valid(recall_history) THEN recall_history ELSE '[]' END,
                    '$[#]', ?)
            WHERE episode_id = ? AND keyword = ?
        """, (turn_seq, RECALL_BOOST, turn_seq, ep_id, kw))
        fired += 1
        shown.add(ep_id)
    return fired, shown


def spread_flashback(con, seed_keywords, turn_seq, already):
    """trailmem-scan.sh の連想フラッシュバック（spread統合）を移植。"""
    if not has_table(con, "keyword_edges"):
        return 0
    adj = defaultdict(list)
    degree = defaultdict(int)
    for a, b, w in con.execute("SELECT kw_a,kw_b,weight FROM keyword_edges"):
        adj[a].append((b, w)); adj[b].append((a, w))
        degree[a] += 1; degree[b] += 1
    if not adj:
        return 0
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
            hop1[nb] += BETA * w * 1.0 * penalty(nb)
    for nb, inc in hop1.items():
        A[nb] += inc
    hop2 = defaultdict(float)
    for node, act in hop1.items():
        for nb, w in adj.get(node, []):
            if nb in seeds:
                continue
            hop2[nb] += (BETA ** 2) * w * act * penalty(nb)
    for nb, inc in hop2.items():
        A[nb] += inc

    spread_nodes = {n: a for n, a in A.items() if n not in seeds}
    if spread_nodes:
        a_max = max(spread_nodes.values())
        if a_max > 0:
            for n in spread_nodes:
                if A[n] < a_max:
                    A[n] = max(0.0, A[n] - GAMMA * a_max)
            a_max2 = max((A[n] for n in spread_nodes), default=0.0)
            if a_max2 > 0:
                for n in spread_nodes:
                    A[n] = A[n] / a_max2

    warmed = {n: a for n, a in A.items() if a >= THETA}
    if not warmed:
        return 0
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
    for ep_id, _ in top:
        if ep_id in already:
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
        """, (turn_seq, RECALL_BOOST, turn_seq, ep_id, kw))
        fired += 1
    return fired


def replay(db_path, episodes, floor_on, spread_on, verbose=False):
    con = sqlite3.connect(db_path)
    with_edges = spread_on and has_table(con, "keyword_edges")
    turn_seq = 0
    stats = {"inserted": 0, "flashbacks": 0, "assoc_flashbacks": 0, "turns": 0,
             "replay_seconds": 0.0, "per_turn_seconds": []}

    for ep in episodes:
        t0 = time.time()
        turn_seq += 1
        stats["turns"] += 1
        # 1ターン = 1エピソードの流入
        insert_episode(con, ep, turn_seq, with_edges)
        stats["inserted"] += 1
        # --- 毎ターン: micro-decay → scan/flashback（実運用 hook と同一） ---
        micro_decay(con, floor_on)
        seeds = list(dict.fromkeys(k.lower() for k in ep["keywords"]))
        fb, shown = scan_flashback(con, seeds, turn_seq)
        stats["flashbacks"] += fb
        if spread_on:
            af = spread_flashback(con, seeds, turn_seq, already=shown)
            stats["assoc_flashbacks"] += af
        con.execute("UPDATE trailmem_meta SET value=? WHERE key='turn_seq'",
                    (str(turn_seq),))
        if turn_seq % COMMIT_EVERY == 0:
            con.commit()
        dt = time.time() - t0
        stats["replay_seconds"] += dt
        stats["per_turn_seconds"].append(round(dt, 4))
        if verbose:
            print(f"  turn@{turn_seq}: insert={ep['pid']} fb={fb} "
                  f"assoc={stats['assoc_flashbacks']} ({dt*1000:.0f}ms)")

    con.execute("UPDATE trailmem_meta SET value=? WHERE key='turn_seq'",
                (str(turn_seq),))
    con.commit()
    con.close()
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--episodes", required=True)
    ap.add_argument("--floor", choices=["on", "off"], default="on")
    ap.add_argument("--spread", choices=["on", "off"], default="off")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--stats-out", default="")
    args = ap.parse_args()

    db = args.db if os.path.isabs(args.db) else os.path.join(HERE, args.db)
    with open(args.episodes, encoding="utf-8") as f:
        eps = json.load(f)

    floor_on = args.floor == "on"
    spread_on = args.spread == "on"
    print(f"replay_faithful: db={os.path.basename(db)} episodes={len(eps)} "
          f"floor={args.floor} spread={args.spread} (per-turn decay+scan)")
    t0 = time.time()
    stats = replay(db, eps, floor_on, spread_on, verbose=args.verbose)
    wall = time.time() - t0
    stats["wall_seconds"] = round(wall, 3)
    per_turn = wall / max(1, stats["turns"])
    print(f"  inserted={stats['inserted']} turns={stats['turns']} "
          f"flashbacks={stats['flashbacks']} "
          f"assoc_flashbacks={stats['assoc_flashbacks']} ({wall:.2f}s)")
    print(f"  per-turn(decay+scan): {per_turn*1000:.1f}ms")

    if args.stats_out:
        with open(args.stats_out, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=1)
        print(f"-> {args.stats_out} (stats)")


if __name__ == "__main__":
    main()
