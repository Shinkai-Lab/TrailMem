#!/bin/bash
# trailmem-doctor.sh — TrailMem ヘルスチェック＆自己修復ツール
#
# Usage:
#   bash trailmem-doctor.sh                                        # レポートのみ（DBは変更しない）
#   bash trailmem-doctor.sh --fix                                  # カテゴリごとに確認して修復
#   bash trailmem-doctor.sh --fix --yes                            # 無確認で全部修復（セルフダイエット）
#   bash trailmem-doctor.sh set-strength EPISODE_ID KEYWORD VALUE  # 強度を人手で調整 (0.0-1.0)
#
# レポートは9セクション構成:
#   1. 基本統計              2. 強度分布                3. 殿堂入りレビュー(報告のみ)
#   4. 感情値スケール異常[FIX]  5. R外れ値/想起履歴膨張[FIX]  6. 整合性(dangling[FIX] / orphan報告のみ)
#   7. ベクトル欠損[FIX]       8. ファイル肥大[FIX]         9. キーワード衛生(報告のみ)
# [FIX] のついたセクションだけ --fix で修復できる。それ以外は常に報告のみ。
# --fix 単体はカテゴリごとに「修復しますか？ [y/N]」を尋ねる。未検出(0件)のカテゴリは尋ねない。
# --fix --yes は全カテゴリ無確認で修復する（セルフダイエットモード）。
# 1セクションで例外が起きても他のセクションは継続する。レポート/修復コマンドの終了コードは常に0。
#
# 環境変数:
#   TRAILMEM_DB                    DBパス (default $HOME/.trailmem/trailmem.db)
#   TRAILMEM_N_CONSOLIDATE         殿堂入り判定の想起回数しきい値 (default 30)
#   TRAILMEM_FLASHBACK_COOLDOWN    Rの上限(cap)計算に使うクールダウン (default 30)
#     cap = max(5, (turn_seq - created_turn_seq) / COOLDOWN + 5)
#     このcapを超えるrecall_historyは「新しい方からcap件」に切り詰める。

set -euo pipefail

DB="${TRAILMEM_DB:-$HOME/.trailmem/trailmem.db}"
N_CONSOLIDATE="${TRAILMEM_N_CONSOLIDATE:-30}"
FLASHBACK_COOLDOWN="${TRAILMEM_FLASHBACK_COOLDOWN:-30}"

# --- set-strength サブコマンド: 人間による強度の手動調整 ---
if [ "${1:-}" = "set-strength" ]; then
  shift
  EPISODE_ID="${1:?Usage: trailmem-doctor.sh set-strength EPISODE_ID KEYWORD VALUE}"
  KEYWORD="${2:?Usage: trailmem-doctor.sh set-strength EPISODE_ID KEYWORD VALUE}"
  VALUE="${3:?Usage: trailmem-doctor.sh set-strength EPISODE_ID KEYWORD VALUE}"

  python3 - "$DB" "$EPISODE_ID" "$KEYWORD" "$VALUE" <<'PYEOF'
import sqlite3
import sys

db_path, episode_id, keyword, value_raw = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

try:
    value = float(value_raw)
except ValueError:
    print(f"✗ 値が数値ではありません: {value_raw!r}")
    sys.exit(1)

if not (0.0 <= value <= 1.0):
    print(f"✗ 値は 0.0〜1.0 の範囲で指定してください（指定値: {value}）")
    sys.exit(1)

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

row = conn.execute(
    "SELECT effective_strength, is_deleted FROM episode_keywords WHERE episode_id = ? AND keyword = ?",
    (episode_id, keyword),
).fetchone()

if row is None:
    print(f"✗ リンクが見つかりません: episode_id={episode_id} keyword={keyword}")
    conn.close()
    sys.exit(1)

before = row["effective_strength"]
conn.execute(
    "UPDATE episode_keywords SET effective_strength = ? WHERE episode_id = ? AND keyword = ?",
    (value, episode_id, keyword),
)
conn.commit()
conn.close()

deleted_note = " (is_deleted=1)" if row["is_deleted"] else ""
print("✔ 強度を更新しました" + deleted_note)
print(f"  episode_id = {episode_id}")
print(f"  keyword    = {keyword}")
print(f"  strength   = {before:.3f} → {value:.3f}")
PYEOF
  exit $?
fi

# --- レポート / 修復モード ---
FIX=0
YES=0
for arg in "$@"; do
  case "$arg" in
    --fix) FIX=1 ;;
    --yes|-y) YES=1 ;;
    *) echo "unknown option: $arg (ignored)" >&2 ;;
  esac
done

python3 - "$DB" "$N_CONSOLIDATE" "$FLASHBACK_COOLDOWN" "$FIX" "$YES" <<'PYEOF'
import json
import os
import sqlite3
import struct
import sys
from datetime import datetime, timezone

db_path = sys.argv[1]
N_CONSOLIDATE = int(sys.argv[2])
COOLDOWN = int(sys.argv[3])
FIX_MODE = sys.argv[4] == "1"
YES_ALL = sys.argv[5] == "1"

if not os.path.exists(db_path):
    print(f"✗ DBファイルが見つかりません: {db_path}")
    sys.exit(0)

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
conn.isolation_level = None  # 明示的にトランザクションを管理する

totals = {"detected": 0, "fixed": 0}


def table_exists(name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?", (name,)
    ).fetchone() is not None


def scalar(sql, params=(), default=None):
    row = conn.execute(sql, params).fetchone()
    if row is None or row[0] is None:
        return default
    return row[0]


def sec(n, title):
    print()
    print(f"━━━ {n}. {title} " + "━" * max(0, 40 - len(title)))


def run_section(n, title, func):
    sec(n, title)
    try:
        func()
    except Exception as e:
        print(f"  ✗ セクション処理中に例外が発生しました（他セクションは継続します）: {e}")


def ask_confirm(label, count):
    """/dev/tty から直接読む。tty が無い(非対話環境)場合はNo扱い。"""
    try:
        tty = open("/dev/tty", "r")
    except OSError:
        print(f"  (端末が無いため確認できません。未実施)")
        return False
    try:
        print(f"  {label}: {count}件検出。修復しますか？ [y/N] ", end="", flush=True)
        ans = tty.readline().strip().lower()
    finally:
        tty.close()
    return ans in ("y", "yes")


def do_fix(label, count, apply_fn):
    """count>0のときだけ確認/適用。戻り値: 実際に修復した件数。"""
    if count <= 0:
        return 0
    totals["detected"] += count
    if not FIX_MODE:
        print("  → --fix で修復可能")
        return 0
    proceed = YES_ALL or ask_confirm(label, count)
    if not proceed:
        print("  スキップしました")
        return 0
    try:
        conn.execute("BEGIN")
        n = apply_fn()
        conn.commit()
        print(f"  ✔ {n}件修復")
        totals["fixed"] += n
        return n
    except Exception as e:
        conn.rollback()
        print(f"  ✗ 修復中にエラーが発生しロールバックしました: {e}")
        return 0


mode_label = "レポートのみ" if not FIX_MODE else ("セルフダイエット(--fix --yes)" if YES_ALL else "確認付き修復(--fix)")
now = datetime.now(timezone.utc)
print("=" * 64)
print("TrailMem Doctor")
print(f"  DB     : {db_path}")
print(f"  時刻   : {now.strftime('%Y-%m-%d %H:%M:%S')} UTC")
print(f"  モード : {mode_label}")
print("=" * 64)


# ============================================================
# 1. 基本統計
# ============================================================
def sec1():
    n_episodes = scalar("SELECT COUNT(*) FROM episodes", default=0)
    n_keywords = scalar("SELECT COUNT(*) FROM keywords", default=0)
    n_live = scalar("SELECT COUNT(*) FROM episode_keywords WHERE is_deleted = 0", default=0)
    n_deleted = scalar("SELECT COUNT(*) FROM episode_keywords WHERE is_deleted = 1", default=0)
    n_edges = scalar("SELECT COUNT(*) FROM keyword_edges", default=0) if table_exists("keyword_edges") else "-"
    n_vec = scalar("SELECT COUNT(*) FROM episode_vec_rowids", default=0) if table_exists("episode_vec_rowids") else "-"
    turn_seq = scalar("SELECT value FROM trailmem_meta WHERE key = 'turn_seq'", default="-")
    last_maint = scalar("SELECT value FROM trailmem_meta WHERE key = 'last_maintenance'", default="(未記録)")
    last_doctor = scalar("SELECT value FROM trailmem_meta WHERE key = 'last_doctor_run'", default="(未記録)")

    print(f"  episodes         : {n_episodes}")
    print(f"  keywords         : {n_keywords}")
    print(f"  links (live)     : {n_live}")
    print(f"  links (deleted)  : {n_deleted}")
    print(f"  keyword_edges    : {n_edges}")
    print(f"  vectors          : {n_vec}")
    print(f"  turn_seq         : {turn_seq}")
    print(f"  last_maintenance : {last_maint}")
    print(f"  last_doctor_run  : {last_doctor}")


run_section(1, "基本統計", sec1)


# ============================================================
# 2. 強度分布
# ============================================================
def sec2():
    row = conn.execute(
        """
        SELECT
          SUM(CASE WHEN effective_strength >= 0.5 THEN 1 ELSE 0 END) AS recall,
          SUM(CASE WHEN effective_strength >= 0.2 AND effective_strength < 0.5 THEN 1 ELSE 0 END) AS deep,
          SUM(CASE WHEN effective_strength < 0.2 THEN 1 ELSE 0 END) AS dig,
          COUNT(*) AS total
        FROM episode_keywords WHERE is_deleted = 0
        """
    ).fetchone()
    recall, deep, dig, total = row["recall"] or 0, row["deep"] or 0, row["dig"] or 0, row["total"] or 0

    if total == 0:
        print("  liveリンクがありません")
        return

    def pct(x):
        return x / total * 100

    print(f"  recall帯 (>=0.5)     : {recall:5d}件 ({pct(recall):5.1f}%)")
    print(f"  deep帯   (0.2-0.5)   : {deep:5d}件 ({pct(deep):5.1f}%)")
    print(f"  dig帯    (<0.2)      : {dig:5d}件 ({pct(dig):5.1f}%)")
    print(f"  合計                 : {total:5d}件")

    if pct(recall) < 1.0:
        print("  ⚠ デフレ気味: recall帯が全体の1%未満です")


run_section(2, "強度分布", sec2)


# ============================================================
# 3. 殿堂入りレビュー (報告のみ)
# ============================================================
def sec3():
    rows = conn.execute(
        """
        SELECT ek.episode_id, ek.keyword, ek.effective_strength, ek.recall_history, e.summary
        FROM episode_keywords ek
        JOIN episodes e ON e.id = ek.episode_id
        WHERE ek.is_deleted = 0
          AND json_valid(ek.recall_history)
          AND json_array_length(ek.recall_history) >= ?
        ORDER BY json_array_length(ek.recall_history) DESC
        """,
        (N_CONSOLIDATE,),
    ).fetchall()

    if not rows:
        print(f"  該当なし（R >= {N_CONSOLIDATE} のリンクはありません）")
        return

    print(f"  R >= {N_CONSOLIDATE} のリンク: {len(rows)}件")
    for r in rows:
        R = json.loads(r["recall_history"]).__len__()
        summary60 = (r["summary"] or "")[:60]
        print(
            f"  [{r['episode_id']}] keyword={r['keyword']} R={R} "
            f"strength={r['effective_strength']:.3f}"
        )
        print(f"      summary: {summary60}")
    print("  強さを調整するには: trailmem-doctor.sh set-strength <episode_id> <keyword> <value>")


run_section(3, "殿堂入りレビュー", sec3)


# ============================================================
# 4. 感情値スケール異常 [FIX対象]
# ============================================================
def sec4():
    fix_rows = conn.execute(
        """
        SELECT id, sentiment_neg, sentiment_pos FROM episodes
        WHERE sentiment_neg <= 1 AND sentiment_pos <= 1
          AND NOT (sentiment_neg = 0 AND sentiment_pos = 0)
        """
    ).fetchall()
    manual_rows = conn.execute(
        """
        SELECT id, sentiment_neg, sentiment_pos FROM episodes
        WHERE sentiment_neg <= 5 AND sentiment_pos <= 5
          AND NOT (
            sentiment_neg <= 1 AND sentiment_pos <= 1
            AND NOT (sentiment_neg = 0 AND sentiment_pos = 0)
          )
        """
    ).fetchall()

    print(f"  0-1スケール混入 (FIX対象): {len(fix_rows)}件")
    for r in fix_rows[:10]:
        print(f"    {r['id']}  neg={r['sentiment_neg']} pos={r['sentiment_pos']}")
    if len(fix_rows) > 10:
        print(f"    ...他{len(fix_rows) - 10}件")

    if manual_rows:
        print(f"  要手動確認 (両方<=5だがFIX対象外): {len(manual_rows)}件")
        for r in manual_rows[:10]:
            print(f"    {r['id']}  neg={r['sentiment_neg']} pos={r['sentiment_pos']}")
        if len(manual_rows) > 10:
            print(f"    ...他{len(manual_rows) - 10}件")

    def apply_fix():
        cur = conn.execute(
            """
            UPDATE episodes
            SET sentiment_neg = MIN(100, CAST(ROUND(sentiment_neg * 100) AS INTEGER)),
                sentiment_pos = MIN(100, CAST(ROUND(sentiment_pos * 100) AS INTEGER))
            WHERE sentiment_neg <= 1 AND sentiment_pos <= 1
              AND NOT (sentiment_neg = 0 AND sentiment_pos = 0)
            """
        )
        return cur.rowcount

    do_fix("感情値スケール異常", len(fix_rows), apply_fix)


run_section(4, "感情値スケール異常 [FIX対象]", sec4)


# ============================================================
# 5. R外れ値 (想起履歴の膨張) [FIX対象]
# ============================================================
def sec5():
    rows = conn.execute(
        """
        SELECT ek.episode_id, ek.keyword, ek.recall_history, e.created_turn_seq
        FROM episode_keywords ek
        JOIN episodes e ON e.id = ek.episode_id
        WHERE ek.is_deleted = 0 AND json_valid(ek.recall_history)
        """
    ).fetchall()

    turn_seq = int(scalar("SELECT COALESCE(value, '0') FROM trailmem_meta WHERE key = 'turn_seq'", default=0) or 0)

    outliers = []  # (episode_id, keyword, R, cap)
    for r in rows:
        hist = json.loads(r["recall_history"])
        if not isinstance(hist, list):
            continue
        R = len(hist)
        cap = int(max(5, (turn_seq - int(r["created_turn_seq"] or 0)) / COOLDOWN + 5))
        if R > cap:
            outliers.append((r["episode_id"], r["keyword"], R, cap))

    print(f"  cap = max(5, (turn_seq - created_turn_seq)/{COOLDOWN} + 5)  (turn_seq={turn_seq})")
    print(f"  R > cap のリンク: {len(outliers)}件")
    for episode_id, keyword, R, cap in sorted(outliers, key=lambda x: -x[2])[:10]:
        print(f"    [{episode_id}] keyword={keyword} R={R} cap={cap}")
    if len(outliers) > 10:
        print(f"    ...他{len(outliers) - 10}件")

    def apply_fix():
        fixed = 0
        for r in rows:
            hist = json.loads(r["recall_history"])
            if not isinstance(hist, list):
                continue
            R = len(hist)
            cap = int(max(5, (turn_seq - int(r["created_turn_seq"] or 0)) / COOLDOWN + 5))
            if R > cap:
                trimmed = hist[-cap:]
                conn.execute(
                    "UPDATE episode_keywords SET recall_history = ? WHERE episode_id = ? AND keyword = ?",
                    (json.dumps(trimmed), r["episode_id"], r["keyword"]),
                )
                fixed += 1
        return fixed

    do_fix("R外れ値", len(outliers), apply_fix)


run_section(5, "R外れ値（想起履歴の膨張） [FIX対象]", sec5)


# ============================================================
# 6. 整合性 (dangling[FIX] / orphan報告のみ)
# ============================================================
def sec6():
    dangling = conn.execute(
        "SELECT episode_id, keyword FROM episode_keywords WHERE episode_id NOT IN (SELECT id FROM episodes)"
    ).fetchall()
    print(f"  dangling (episodesに存在しないepisode_idを持つ episode_keywords): {len(dangling)}件")
    for r in dangling[:10]:
        print(f"    episode_id={r['episode_id']} keyword={r['keyword']}")
    if len(dangling) > 10:
        print(f"    ...他{len(dangling) - 10}件")

    def apply_fix():
        cur = conn.execute("DELETE FROM episode_keywords WHERE episode_id NOT IN (SELECT id FROM episodes)")
        return cur.rowcount

    do_fix("dangling episode_keywords", len(dangling), apply_fix)

    orphans = conn.execute(
        """
        SELECT e.id, e.summary FROM episodes e
        WHERE NOT EXISTS (
          SELECT 1 FROM episode_keywords ek WHERE ek.episode_id = e.id AND ek.is_deleted = 0
        )
        """
    ).fetchall()
    print(f"  orphan (liveリンクが1本もないエピソード、報告のみ): {len(orphans)}件")
    for r in orphans[:5]:
        print(f"    [{r['id']}] {(r['summary'] or '')[:60]}")
    if len(orphans) > 5:
        print(f"    ...他{len(orphans) - 5}件")


run_section(6, "整合性 [FIX対象: dangling]", sec6)


# ============================================================
# 7. ベクトル欠損 [FIX対象]
# ============================================================
def sec7():
    if not table_exists("episode_vec_rowids"):
        print("  episode_vec_rowids テーブルが存在しないためスキップします")
        return

    missing = conn.execute(
        "SELECT rowid, id, summary, inner FROM episodes WHERE rowid NOT IN (SELECT rowid FROM episode_vec_rowids)"
    ).fetchall()
    print(f"  embedding未生成のエピソード: {len(missing)}件")

    can_vec = False
    can_st = False
    try:
        import sqlite_vec  # noqa: F401
        can_vec = True
    except Exception:
        pass
    try:
        from sentence_transformers import SentenceTransformer  # noqa: F401
        can_st = True
    except Exception:
        pass

    if missing and not (can_vec and can_st):
        missing_libs = []
        if not can_vec:
            missing_libs.append("sqlite_vec")
        if not can_st:
            missing_libs.append("sentence_transformers")
        print(f"  skip: ライブラリ未導入 ({', '.join(missing_libs)})")
        return

    def apply_fix():
        import sqlite_vec
        from sentence_transformers import SentenceTransformer

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)

        model = SentenceTransformer("all-MiniLM-L6-v2")
        texts = [f"{r['summary']} {r['inner']}" for r in missing]
        embeddings = model.encode(texts, show_progress_bar=False)
        for i, r in enumerate(missing):
            vec_bytes = struct.pack(f"{len(embeddings[i])}f", *embeddings[i].tolist())
            conn.execute("INSERT INTO episode_vec(rowid, embedding) VALUES (?, ?)", (r["rowid"], vec_bytes))
        return len(missing)

    do_fix("ベクトル欠損", len(missing), apply_fix)


run_section(7, "ベクトル欠損 [FIX対象]", sec7)


# ============================================================
# 8. ファイル肥大 [FIX対象]
# ============================================================
def sec8():
    ROTATE_THRESHOLD = 5 * 1024 * 1024  # 5MB
    db_dir = os.path.dirname(os.path.abspath(db_path))
    targets = [
        os.path.join(db_dir, "flashback-buffer.jsonl"),
        os.path.join(db_dir, "noise.txt"),
        "/tmp/trailmem-shown.json",
    ]

    def human_size(n):
        f = float(n)
        for unit in ("B", "KB", "MB", "GB"):
            if f < 1024 or unit == "GB":
                return f"{f:.1f}{unit}" if unit != "B" else f"{n}B"
            f /= 1024
        return f"{f:.1f}TB"

    oversized = []
    for path in targets:
        if os.path.exists(path):
            size = os.path.getsize(path)
            flag = " ⚠ 5MB超" if size > ROTATE_THRESHOLD else ""
            print(f"  {path}: {human_size(size)}{flag}")
            if size > ROTATE_THRESHOLD:
                oversized.append(path)
        else:
            print(f"  {path}: (ファイルなし)")

    def apply_fix():
        rotated = 0
        for path in oversized:
            try:
                os.replace(path, path + ".1")
                rotated += 1
            except OSError as e:
                print(f"    ✗ {path} のローテーションに失敗: {e}")
        return rotated

    do_fix("ファイル肥大 (5MB超のローテーション)", len(oversized), apply_fix)


run_section(8, "ファイル肥大 [FIX対象]", sec8)


# ============================================================
# 9. キーワード衛生 (報告のみ)
# ============================================================
def sec9():
    onechar = conn.execute("SELECT keyword FROM keywords WHERE LENGTH(keyword) = 1 ORDER BY keyword").fetchall()
    print(f"  1文字キーワード: {len(onechar)}件")
    if onechar:
        shown = [r["keyword"] for r in onechar[:20]]
        print(f"    {', '.join(shown)}" + (" ..." if len(onechar) > 20 else ""))

    hubs = conn.execute(
        """
        SELECT keyword, COUNT(*) AS c FROM episode_keywords
        WHERE is_deleted = 0
        GROUP BY keyword ORDER BY c DESC LIMIT 10
        """
    ).fetchall()
    print("  ハブ上位10 (liveリンク数順):")
    for r in hubs:
        print(f"    {r['keyword']}: {r['c']}件")


run_section(9, "キーワード衛生", sec9)


# ============================================================
# 締め
# ============================================================
print()
print("=" * 64)
if FIX_MODE:
    print(f"修復サマリ: 検出 {totals['detected']}件 / 修復 {totals['fixed']}件")
    # 注意: 'last_maintenance' はdaemonの月次メンテ(ingest.maintenance)の
    # 30日ゲート用キー。doctorは月次減衰を行わないため、そこに書くと
    # 本来走るべき月次メンテがスキップされてしまう。別キーに記録する。
    try:
        conn.execute("BEGIN")
        conn.execute(
            "INSERT OR REPLACE INTO trailmem_meta (key, value) VALUES ('last_doctor_run', ?)",
            (now.strftime("%Y-%m-%dT%H:%M:%SZ"),),
        )
        conn.commit()
    except Exception:
        conn.rollback()
else:
    print("レポートのみ（DBは変更していません）")
print("=" * 64)

conn.close()
sys.exit(0)
PYEOF
