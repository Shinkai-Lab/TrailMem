#!/usr/bin/env python3
"""replay.py — タイムマシン・リプレイ（時系列で記憶を育てるエンジン）。

「こころ」のエピソードを物語順に1件ずつDBへ流し込み、各ステップで
実運用フック(trailmem_hook.sh)と同じ時間発展を再現する:

  1. エピソードを insert（キーワード・トレイル・感情。spread構成ではエッジも）
  2. CHUNK_SIZE 件ごとに「ターン経過」イベント:
     a. micro-decay: 全トレイルの decay/effective_strength を更新
        - floor=ON : 想起回数Rに応じたフロアを effective_strength の下限にする
                     （設計書v2「記憶定着 — フロア方式」と同式）
        - floor=OFF: 旧hook相当。フロアなし、ただ decay×DECAY_RATE で沈む
     b. scan/flashback: 直近エピソードのキーワードを種に、感情の濃い古い
        記憶を1件フラッシュバック → recall_history に push（R++）、trail強化
        （これが「読み進めながら過去の場面を思い出す」時間発展を生む）
        spread構成では連想フラッシュバックも発火

設計の肝: フロア方式の効果は「育てる過程」でしか出ない。何度もフラッシュ
バックした記憶ほどRが増えてフロアが上がり、後半まで沈まず残る。育て終えた
DBをゴールドセットで想起テストして、フロアあり/なしの想起精度差を測る。

本番trailmem.dbには一切触らない。引数で渡されたベンチ専用DBのみ書き込む。

Usage:
  python3 replay.py --db kokoro_B.db --episodes data/kokoro_pilot_episodes.json \\
      --floor on --spread on
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

# --- 時間発展パラメータ（hook/設計書のデフォルトに合わせる） ---
# 環境変数で上書き可能。ベンチは「時間を圧縮」する必要があるため、
# DECAY_RATE を実運用(0.999/ターン)より強めて1作品で長期忘却を再現する。
# 例: TM_DECAY_RATE=0.97 で1チャンクごとに3%沈む = 加速エイジング。
CHUNK_SIZE = int(os.environ.get("TM_CHUNK_SIZE", "10"))
DECAY_RATE = float(os.environ.get("TM_DECAY_RATE", "0.999"))
N_CONSOLIDATE = int(os.environ.get("TM_N_CONSOLIDATE", "30"))
FLOOR_MIN = float(os.environ.get("TM_FLOOR_MIN", "0.1"))
FLOOR_DECAY = float(os.environ.get("TM_FLOOR_DECAY", "0.99999"))
FLASHBACK_COOLDOWN = 10    # 同じトレイルの再フラッシュ抑制(seq差)
FLASHBACK_THRESHOLD = 0.30 # フラッシュバック発火の閾値
RECALL_BOOST = 1.05        # フラッシュバックでのトレイル強化
# spread（アメーバ網）パラメータ
BETA = 0.35
GAMMA = 0.15
THETA = 0.3


def has_table(con, name):
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def insert_episode(con, ep, turn_seq, with_edges):
    """エピソード1件をDBへ。auto-ingest.sh のロジックを移植（vec省略）。"""
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # 決定論的ID: pidから一意に決まる。これにより参照DBと評価DBで同一段落が
    # 同一IDになり、ゴールドセットの正解IDがどの構成のDBでも突合できる。
    ep_id = f"kokoro-{ep['pid']}"
    source_ref = f"kokoro:{ep['chapter']}:{ep['section']}:{ep['pid']}"
    con.execute("""
        INSERT INTO episodes (id, summary, inner, quote, sentiment_neg,
            sentiment_pos, created_at, source_type, source_ref, created_turn_seq)
        VALUES (?,?,?,NULL,?,?,?, 'kokoro', ?, ?)
    """, (ep_id, ep["summary"], ep["inner"], ep["neg"], ep["pos"],
          created_at, source_ref, turn_seq))

    kw_norms = []
    for kw in ep["keywords"]:
        k = kw.strip().lower()
        if not k:
            continue
        kw_norms.append(k)
        con.execute(
            "INSERT OR IGNORE INTO keywords (keyword, synonyms, created_at)"
            " VALUES (?, '[]', ?)", (k, created_at))
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


def micro_decay(con, turn_seq, floor_on):
    """毎チャンクの減衰。trailmem_hook.sh の micro-decay を移植。

    floor_on=True : フロア方式（設計書v2）。R=recall_history長に応じた下限。
    floor_on=False: 旧hook相当。フロアなし、単純減衰。
    """
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
        # 旧方式: フロアなし。decay×rate して effective を従来式で再計算するだけ。
        con.execute(f"""
            UPDATE episode_keywords
            SET decay = MAX(0.01, decay * {DECAY_RATE}),
                effective_strength =
                  (CAST((used - misled + 1) AS REAL) / (shown + 2)) *
                  MAX(0.01, decay * {DECAY_RATE})
            WHERE is_deleted = 0
        """)


def flashback(con, seed_keywords, turn_seq):
    """scan.sh の1ホップ・フラッシュバックを移植。

    各シードキーワードについて、感情×強度×時間減衰が閾値超なら最も濃い
    トレイルを1件フラッシュバック → recall_history に turn_seq を push(R++),
    effective_strength を強化。育てる過程でRを稼がせるのが目的。
    """
    fired = 0
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
    return fired


def spread_flashback(con, seed_keywords, turn_seq, already):
    """scan.sh の連想フラッシュバック（spread統合）を移植。

    keyword_edges 上で活性化拡散（β/γ/θ + ハブ対策）を1回。温まった部分網の
    上位エピソードを連想想起 → R++/強化。spread構成でのみ呼ぶ。
    """
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
    recent_keywords = []   # 直近チャンクのエピソードキーワード（フラッシュバック種）
    stats = {"inserted": 0, "flashbacks": 0, "assoc_flashbacks": 0, "chunks": 0}

    for i, ep in enumerate(episodes, 1):
        turn_seq += 1
        insert_episode(con, ep, turn_seq, with_edges)
        recent_keywords.extend(ep["keywords"])
        stats["inserted"] += 1

        if i % CHUNK_SIZE == 0 or i == len(episodes):
            stats["chunks"] += 1
            micro_decay(con, turn_seq, floor_on)
            seeds = list(dict.fromkeys(k.lower() for k in recent_keywords))
            fb = flashback(con, seeds, turn_seq)
            stats["flashbacks"] += fb
            if spread_on:
                af = spread_flashback(con, seeds, turn_seq, already=set())
                stats["assoc_flashbacks"] += af
            con.execute("UPDATE trailmem_meta SET value=? WHERE key='turn_seq'",
                        (str(turn_seq),))
            con.commit()
            if verbose:
                print(f"  step@{i}: seq={turn_seq} fb={fb} "
                      f"assoc={stats['assoc_flashbacks']}")
            recent_keywords = []

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
    args = ap.parse_args()

    db = args.db if os.path.isabs(args.db) else os.path.join(HERE, args.db)
    with open(args.episodes, encoding="utf-8") as f:
        eps = json.load(f)

    floor_on = args.floor == "on"
    spread_on = args.spread == "on"
    print(f"replay: db={os.path.basename(db)} episodes={len(eps)} "
          f"floor={args.floor} spread={args.spread}")
    t0 = time.time()
    stats = replay(db, eps, floor_on, spread_on, verbose=args.verbose)
    dt = time.time() - t0
    print(f"  inserted={stats['inserted']} chunks={stats['chunks']} "
          f"flashbacks={stats['flashbacks']} "
          f"assoc_flashbacks={stats['assoc_flashbacks']} ({dt:.1f}s)")


if __name__ == "__main__":
    main()
