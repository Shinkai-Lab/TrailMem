#!/usr/bin/env python3
"""想起ランナー — promptを受けて想起されたエピソードIDの順序付きリストを返す。

抽象化レイヤ。RECALL_CMD 環境変数で想起実装を差し替えられる:
  - RECALL_CMD=recall  : trailmem-recall.sh (現行 / baseline)
  - RECALL_CMD=spread  : trailmem-spread.sh (アメーバ網 / 後で繋がる)
  - RECALL_CMD=/abs/path.sh : 任意スクリプト

想起スクリプトの呼び出し規約:
  bash <script> <kw1> <kw2> ...
  標準出力に "[<episode_id>] (...) ..." 形式の行を出す（recall.sh互換）。

prompt -> シードキーワード抽出は scan.sh と同じ「キーワード文字列が入力に
部分一致するか」ヒューリスティックを Python で再現する。
"""
import os
import re
import subprocess

from common import REPO, DEFAULT_DB, open_ro, norm  # noqa: F401

# trailmem-recall.sh / trailmem-spread.sh は oss リポジトリのルート直下にある。
# 別の場所に置いている場合は TRAILMEM_SCRIPTS_DIR で上書きできる。
SCRIPTS = os.environ.get("TRAILMEM_SCRIPTS_DIR", REPO)

CMD_MAP = {
    "recall": os.path.join(SCRIPTS, "trailmem-recall.sh"),
    "spread": os.path.join(SCRIPTS, "trailmem-spread.sh"),
}

# recall.sh が出力する "[episode-id] (0.42) summary" の id を拾う
# episode_id は uuid / episode-NNN-hash / e-2026... の3形式。score行
# ("  [0.42] (kw) ...") と区別するため、id文字列を緩く判定する。
ID_LINE = re.compile(r"^\[([^\]]+)\]\s")
# spread.sh は "  [score] (kw) summary" で episode_id を出さない。
# その場合 summary -> episode_id を DB から逆引きする。
SPREAD_LINE = re.compile(r"^\s+\[\d+\.\d+\]\s+\([^)]*\)\s+(.+)$")


def _looks_like_id(s):
    """episode_id らしさ（スコア数値ではない）を判定。"""
    s = s.strip()
    try:
        float(s)
        return False  # 純粋な数値 = スコア行
    except ValueError:
        return True


_SUMMARY_PREFIX_INDEX = None


def _summary_lookup(prefix, db_path=None):
    """summary 前方一致で episode_id を引く（spread出力用フォールバック）。"""
    global _SUMMARY_PREFIX_INDEX
    if _SUMMARY_PREFIX_INDEX is None:
        con = open_ro(db_path)
        _SUMMARY_PREFIX_INDEX = [
            (r["id"], norm(r["summary"]))
            for r in con.execute("SELECT id, summary FROM episodes")]
        con.close()
    np = norm(prefix)
    if len(np) < 10:
        return None
    for eid, ns in _SUMMARY_PREFIX_INDEX:
        if ns.startswith(np[:30]):
            return eid
    return None


def resolve_cmd(recall_cmd=None):
    recall_cmd = recall_cmd or os.environ.get("RECALL_CMD", "recall")
    path = CMD_MAP.get(recall_cmd, recall_cmd)
    return recall_cmd, path


_KW_CACHE = None


def load_keywords(db_path=None):
    """[(keyword, [synonyms...]), ...] を返す（キャッシュ）。"""
    global _KW_CACHE
    if _KW_CACHE is not None:
        return _KW_CACHE
    con = open_ro(db_path)
    import json
    rows = []
    for r in con.execute("SELECT keyword, synonyms FROM keywords"):
        try:
            syns = json.loads(r["synonyms"]) if r["synonyms"] else []
        except Exception:
            syns = []
        rows.append((r["keyword"], syns))
    con.close()
    _KW_CACHE = rows
    return rows


def extract_seed_keywords(prompt, db_path=None, max_kw=12):
    """scan.sh と同じく、入力に部分一致するキーワードを抽出。

    アクティブな道(effective_strength>=0.1)を持つキーワードを優先する。
    """
    p = norm(prompt)
    hits = []
    for kw, syns in load_keywords(db_path):
        if norm(kw) and norm(kw) in p:
            hits.append(kw)
            continue
        for s in syns:
            ns = norm(s)
            if ns and ns in p:
                hits.append(kw)
                break
    # 重複除去（順序保持）
    seen = set()
    uniq = [k for k in hits if not (k in seen or seen.add(k))]
    return uniq[:max_kw]


def run_recall(keywords, recall_cmd=None, level=None, limit=10, db_path=None):
    """想起スクリプトを呼び、episode_id のリスト（出力順）を返す。"""
    if not keywords:
        return []
    name, path = resolve_cmd(recall_cmd)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"想起スクリプトが見つかりません: {path} "
            f"(RECALL_CMD={name}). 未実装の可能性。"
        )
    env = dict(os.environ)
    env["TRAILMEM_DB"] = db_path or DEFAULT_DB
    if level:
        env["TRAILMEM_LEVEL"] = level
    env["TRAILMEM_LIMIT"] = str(limit)
    try:
        out = subprocess.run(
            ["bash", path, *keywords],
            capture_output=True, text=True, env=env, timeout=120,
        ).stdout
    except subprocess.TimeoutExpired:
        return []
    ids = []
    for line in out.splitlines():
        m = ID_LINE.match(line)
        if m and _looks_like_id(m.group(1)):
            ids.append(m.group(1).strip())
            continue
        # spread.sh フォールバック: "  [score] (kw) summary" -> 逆引き
        m2 = SPREAD_LINE.match(line)
        if m2:
            eid = _summary_lookup(m2.group(1), db_path)
            if eid:
                ids.append(eid)
    # 重複除去（順序保持）
    seen = set()
    return [i for i in ids if not (i in seen or seen.add(i))]


def recall_for_prompt(prompt, recall_cmd=None, level=None, limit=10,
                      db_path=None):
    kws = extract_seed_keywords(prompt, db_path)
    ids = run_recall(kws, recall_cmd, level, limit, db_path)
    return ids, kws
