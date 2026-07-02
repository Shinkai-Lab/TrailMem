#!/usr/bin/env python3
"""共有ユーティリティ — 人格維持ベンチマーク。

本番DB (trailmem.db) は読み取り専用で開く。書き込みは一切しない。
"""
import json
import os
import re
import shutil
import sqlite3
import tempfile
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
# oss リポジトリのルート (benchmark/personality から2階層上)。
# trailmem-recall.sh / trailmem-spread.sh はここに置かれている想定。
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))

DEFAULT_DB = os.environ.get(
    "TRAILMEM_DB", os.path.expanduser("~/.trailmem/trailmem.db")
)
DEFAULT_LOG = os.environ.get(
    "TRAILMEM_LOG", os.path.expanduser("~/.trailmem/trailmem-hook.log")
)
GOLDSET = os.path.join(HERE, "goldset.jsonl")


def open_ro(db_path=None):
    """DBを読み取り専用で開く。"""
    db_path = db_path or DEFAULT_DB
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def make_scratch_db(src=None):
    """想起スクリプトは想起時にDBへ書き込む(けもの道強化)。

    本番の trailmem.db を絶対に汚さないため、評価用に一時コピーを作って返す。
    呼び出し側は使い終わったら os.remove する。
    """
    src = src or DEFAULT_DB
    fd, dst = tempfile.mkstemp(prefix="trailmem_bench_", suffix=".db")
    os.close(fd)
    shutil.copy2(src, dst)
    # WAL/SHM があればそれもコピー（一貫性のため）
    for ext in ("-wal", "-shm"):
        if os.path.exists(src + ext):
            shutil.copy2(src + ext, dst + ext)
    return dst


def read_log(path=None):
    """trailmem-hook.log を読む。

    NEL(U+0085)等の不正バイトが混入しているため errors='replace' で読み、
    .splitlines() で行分割する（bash grep では正しく切れない）。
    """
    path = path or DEFAULT_LOG
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read().splitlines()


def norm(s):
    """比較用に正規化（NFKC + 小文字 + 空白畳み込み）。"""
    if s is None:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("�", "")  # replacement char 除去
    s = re.sub(r"\s+", "", s)
    return s.lower()


def load_episode_summaries(con):
    """{episode_id: summary} を返す。"""
    return {r["id"]: r["summary"] for r in con.execute(
        "SELECT id, summary FROM episodes")}


def load_goldset(path=None):
    path = path or GOLDSET
    cases = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases
