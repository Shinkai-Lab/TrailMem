#!/usr/bin/env python3
"""add_themes_kokoro.py — 手動テーマ付与ツール（こころベンチ用）

daemon/ingest.py に追加した「テーマキーワード」(axis='theme') 抽出は本来
記銘時の抽出LLMが行うものだが、本ベンチでは外部LLM呼び出しが禁止されているため、
実行者（Claude）自身が対象エピソードの summary/inner を読んで手動でテーマを判定し、
その結果を theme_assignments_kokoro.json に保存した。本スクリプトは単に
「判定済みのJSONをDBへ適用する」だけで、判定ロジックそのものは持たない
（再現可能性のため、判定結果と適用処理を分離している）。

書き込みルールは daemon/ingest.py の insert_episodes() のテーマ処理と同じ:
- keywords テーブルへ axis='theme' で upsert
  （既存なら synonyms を union、axis が '' なら 'theme' に更新。既に何か入っていれば維持）
- episode_keywords へ INSERT OR IGNORE（強度は全カラムのデフォルト値のまま）
- keyword_edges（アメーバ網）: 今回追加したテーマ語 と その episode の
  既存キーワード（テーマ含む）との新規ペアだけを作成する。
  既存の具体キーワード同士のペアは今回「再抽出」したわけではないので触らない。

Usage:
  python3 add_themes_kokoro.py --db /path/to/kokoro_copy.db
  python3 add_themes_kokoro.py --db ... --include target      # goldsetの正解だけ
  python3 add_themes_kokoro.py --db ... --include dummy       # ダミー30件だけ
  python3 add_themes_kokoro.py --db ... --dry-run             # 書き込まずに統計だけ見る

安全: 対象は必ずDBの「コピー」に対して実行すること（本スクリプトはコピー元の
検証は行わない）。ここでは scratchpad 上のコピーに対してのみ使用している。
"""
import argparse
import itertools
import json
import os
import sqlite3
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ASSIGNMENTS = os.path.join(HERE, "theme_assignments_kokoro.json")


def ensure_axis_column(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(keywords)")}
    if "axis" not in cols:
        conn.execute("ALTER TABLE keywords ADD COLUMN axis TEXT NOT NULL DEFAULT ''")


def get_turn_seq(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM trailmem_meta WHERE key='turn_seq'").fetchone()
    return int(row[0]) if row else 0


def upsert_theme_keyword(conn: sqlite3.Connection, norm: str, syns: list[str], created_at: str) -> str:
    existing = conn.execute(
        "SELECT synonyms, axis FROM keywords WHERE keyword=?", (norm,)
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO keywords(keyword, synonyms, created_at, axis) VALUES (?, ?, ?, 'theme')",
            (norm, json.dumps(syns, ensure_ascii=False), created_at),
        )
        return "inserted"
    old_syns_raw, old_axis = existing
    try:
        old_syns = json.loads(old_syns_raw) or []
        if not isinstance(old_syns, list):
            old_syns = []
    except json.JSONDecodeError:
        old_syns = []
    merged = sorted(set(old_syns) | set(str(s) for s in syns))
    new_axis = old_axis if old_axis else "theme"
    conn.execute(
        "UPDATE keywords SET synonyms=?, axis=? WHERE keyword=?",
        (json.dumps(merged, ensure_ascii=False), new_axis, norm),
    )
    return "merged"


def apply_theme(
    conn: sqlite3.Connection,
    episode_id: str,
    theme: dict,
    turn_seq: int,
    created_at: str,
    stats: dict,
) -> str | None:
    norm = str(theme.get("kw") or "").strip().lower()
    if not norm:
        return None
    syns = [str(s).strip() for s in (theme.get("synonyms") or []) if str(s).strip()]

    if conn.execute("SELECT 1 FROM episodes WHERE id=?", (episode_id,)).fetchone() is None:
        stats["missing_episode"].append(episode_id)
        return None

    action = upsert_theme_keyword(conn, norm, syns, created_at)
    stats[f"keyword_{action}"] = stats.get(f"keyword_{action}", 0) + 1

    cur = conn.execute(
        "INSERT OR IGNORE INTO episode_keywords (episode_id, keyword, recall_history, actr_base_level) "
        "VALUES (?, ?, ?, 0.0)",
        (episode_id, norm, json.dumps([turn_seq])),
    )
    if cur.rowcount > 0:
        stats["links_added"] += 1
    else:
        stats["links_already_present"] += 1
    return norm


def add_amoeba_edges(
    conn: sqlite3.Connection,
    episode_id: str,
    new_kws: list[str],
    created_at: str,
    stats: dict,
) -> None:
    """new_kws を含むペアだけ新規作成する。既存キーワード同士のペアは触らない。"""
    existing_rows = conn.execute(
        "SELECT keyword FROM episode_keywords WHERE episode_id=? AND is_deleted=0",
        (episode_id,),
    ).fetchall()
    all_kws = sorted({r[0] for r in existing_rows} | set(new_kws))
    new_set = set(new_kws)
    for a, b in itertools.combinations(all_kws, 2):
        if a not in new_set and b not in new_set:
            continue
        row = conn.execute(
            "SELECT 1 FROM keyword_edges WHERE kw_a=? AND kw_b=?", (a, b)
        ).fetchone()
        if row is None:
            ctx = json.dumps(
                {"co_kw": [], "last_episode": episode_id, "note": "theme-add"},
                ensure_ascii=False,
            )
            conn.execute(
                "INSERT INTO keyword_edges (kw_a, kw_b, weight, co_count, last_traversed_seq, "
                "context, created_at) VALUES (?, ?, 0.1, 1, 0, ?, ?)",
                (a, b, ctx, created_at),
            )
            stats["edges_created"] += 1
        else:
            stats["edges_already_present"] += 1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, help="適用対象DB（必ずコピー上で実行）")
    ap.add_argument("--assignments", default=DEFAULT_ASSIGNMENTS)
    ap.add_argument(
        "--include", default="target,dummy",
        help="適用するグループ(comma区切り): target,dummy",
    )
    ap.add_argument("--dry-run", action="store_true", help="書き込まずロールバックして統計だけ表示")
    args = ap.parse_args()

    with open(args.assignments, encoding="utf-8") as f:
        payload = json.load(f)

    groups = [g.strip() for g in args.include.split(",") if g.strip()]
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    conn = sqlite3.connect(args.db)
    stats = {
        "keyword_inserted": 0,
        "keyword_merged": 0,
        "links_added": 0,
        "links_already_present": 0,
        "edges_created": 0,
        "edges_already_present": 0,
        "episodes_touched": 0,
        "missing_episode": [],
    }
    try:
        ensure_axis_column(conn)
        turn_seq = get_turn_seq(conn)
        for group in groups:
            entries = payload.get(group, {})
            for ep_id, spec in entries.items():
                new_kws = []
                for theme in spec.get("themes", []):
                    norm = apply_theme(conn, ep_id, theme, turn_seq, created_at, stats)
                    if norm:
                        new_kws.append(norm)
                if new_kws:
                    add_amoeba_edges(conn, ep_id, new_kws, created_at, stats)
                    stats["episodes_touched"] += 1

        if args.dry_run:
            conn.rollback()
            print("[dry-run] rolled back (no changes written). stats would have been:")
        else:
            conn.commit()
    finally:
        conn.close()

    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
