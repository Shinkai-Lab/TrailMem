#!/usr/bin/env python3
"""make_db.py — ベンチ専用の空DBを作る。

本番 trailmem.db のスキーマ（episodes / keywords / episode_keywords /
trailmem_meta / keyword_edges）をコピーした空DBを生成する。
ベクトル(episode_vec*)は省略する — このベンチはキーワード想起が主軸で、
sentence-transformers が無くても動くようにするため。

本番DBには一切触らない（読み取りで .schema 相当を再現するだけ）。

Usage:
  python3 make_db.py kokoro_A.db
  python3 make_db.py --edges kokoro_B.db   # keyword_edges も作る
"""
import argparse
import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))

SCHEMA = """
CREATE TABLE schema_version (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE episodes (
  id            TEXT PRIMARY KEY,
  summary       TEXT NOT NULL,
  inner         TEXT NOT NULL,
  quote         TEXT,
  sentiment_neg INTEGER NOT NULL DEFAULT 50 CHECK (sentiment_neg >= 0 AND sentiment_neg <= 100),
  sentiment_pos INTEGER NOT NULL DEFAULT 50 CHECK (sentiment_pos >= 0 AND sentiment_pos <= 100),
  created_at    TEXT NOT NULL,
  source_type   TEXT NOT NULL,
  source_ref    TEXT NOT NULL,
  feeling_intensity REAL NOT NULL DEFAULT 1.0,
  created_turn_seq INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE keywords (
  keyword    TEXT PRIMARY KEY,
  synonyms   TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);
CREATE TABLE episode_keywords (
  episode_id       TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
  keyword          TEXT NOT NULL REFERENCES keywords(keyword),
  shown            INTEGER NOT NULL DEFAULT 0 CHECK (shown >= 0),
  used             INTEGER NOT NULL DEFAULT 0 CHECK (used >= 0 AND used <= shown),
  misled           INTEGER NOT NULL DEFAULT 0 CHECK (misled >= 0 AND misled <= used),
  base_strength    REAL NOT NULL DEFAULT 0.5,
  decay            REAL NOT NULL DEFAULT 1.0 CHECK (decay > 0 AND decay <= 1.0),
  effective_strength REAL NOT NULL DEFAULT 0.5,
  last_recalled    TEXT,
  is_deleted       INTEGER NOT NULL DEFAULT 0 CHECK (is_deleted IN (0, 1)),
  last_recalled_seq INTEGER NOT NULL DEFAULT 0,
  recall_history   TEXT NOT NULL DEFAULT '[]',
  actr_base_level  REAL NOT NULL DEFAULT 0.0,
  PRIMARY KEY (episode_id, keyword)
);
CREATE INDEX idx_ek_recall
  ON episode_keywords(keyword, is_deleted, effective_strength DESC);
CREATE INDEX idx_ek_episode
  ON episode_keywords(episode_id, is_deleted);
CREATE TABLE trailmem_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

EDGES_SCHEMA = """
CREATE TABLE IF NOT EXISTS keyword_edges (
  kw_a TEXT NOT NULL,
  kw_b TEXT NOT NULL,
  weight REAL NOT NULL DEFAULT 0.1 CHECK (weight > 0),
  co_count INTEGER NOT NULL DEFAULT 1,
  last_traversed_seq INTEGER NOT NULL DEFAULT 0,
  context TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (kw_a, kw_b),
  FOREIGN KEY (kw_a) REFERENCES keywords(keyword),
  FOREIGN KEY (kw_b) REFERENCES keywords(keyword)
);
CREATE INDEX IF NOT EXISTS idx_edge_a ON keyword_edges(kw_a, weight DESC);
CREATE INDEX IF NOT EXISTS idx_edge_b ON keyword_edges(kw_b, weight DESC);
"""


def build(path, with_edges=False):
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    if with_edges:
        con.executescript(EDGES_SCHEMA)
    con.execute("INSERT INTO trailmem_meta (key, value) VALUES ('turn_seq','0')")
    con.execute("INSERT INTO schema_version (version) VALUES (1)")
    con.commit()
    con.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--edges", action="store_true",
                    help="keyword_edges テーブルも作る（spread構成用）")
    args = ap.parse_args()
    path = args.path
    if not os.path.isabs(path):
        path = os.path.join(HERE, path)
    build(path, with_edges=args.edges)
    print(f"created empty bench DB: {path} (edges={args.edges})")


if __name__ == "__main__":
    main()
