#!/usr/bin/env python3
"""eval_flashback_ab.py — A/B比較: trailmem-scan.sh の門2(収束)×弱い子優先。

既存の想起(recall/spread)ハーネス(eval.py / ab_compare.py)は
scripts/trailmem-recall.sh / trailmem-spread.sh (=想起コマンド) を比べるためのもので、
フラッシュバック*発火*ロジック(trailmem-scan.sh)は評価していない
(bench_natural.py はDBを直接SQLで叩く簡易シミュレーションで、実スクリプトを呼ばない)。
本スクリプトは改修済みの trailmem-scan.sh を実際に subprocess 起動し、
flashback-buffer.jsonl から発火エピソードIDを取って測る、新規の薄い評価。

v1 (ab_flashback_results.json):
  A/B/C/D = DOOR2 × WEAKEST_FIRST の4構成。発見:
  - 門2(収束)単体は cov/prec/MRR を全部上げた
  - 旧・弱い子優先(ゲートスコア昇順)は「閾値をかすっただけの汎用収束
    (先生+私 等)」を最弱として常に選び、門2の的中を打ち消した
v2 (ab_flashback_results_v2.json): scan.sh改修後の再測定。
  - 門2にハブペナルティ: 貢献 = eff × min(1, 1/ln(1+degree))
  - 弱い子優先の意味変更: 発火順 = エピソードの忘れられ度(R=liveリンクの
    max recall_history長)昇順、同着はゲートスコア降順。入場審査と優先順位の分離
  手順: (1) θ2スイープ(ハブペナルティで収束スコアが縮むため),
        (2) 最良θ2で A / B' / D' / E(spread OFF) の4構成

DB: kokoro_floor_on_sp.db を土台に使う。
  - benchmark/timemachine/README.md の既存 ab_compare(floor/spread軸)で
    floor=on・spread edges付きで「加速エイジングで育てた」DB。
  - episode ID (kokoro-pNNNN) が goldset.jsonl と一致する
    (data/kokoro_episodes.json / heuristic ingest 系列。goldset_faithful.jsonl や
    kokoro_faithful_*.db は別のingest系列(ID形式が kokoro-pNNNN-eNN で異なる)なので混在させない)。
  - effective_strength が 0.21〜1.0 まで分散していて(kokoro_ref.dbはほぼ0.46〜0.52で
    ほとんど分化していない)、門1/門2閾値に対して意味のある分岐が出る。
  - keyword_edges を持つ(spread構成)ので、trailmem-scan.sh が既定でONの
    連想フラッシュバック(trailmem-spread.shサブプロセス呼び出し)も実際に機能する
    状態で測れる = 本番のデフォルト挙動に忠実。

質問順とDBリセットについて(必読):
  trailmem-scan.sh は trailmem_meta.turn_seq を自分では進めない(replay.pyだけが
  育成時に進める)。かつ発火したエピソードは TRAILMEM_FLASHBACK_COOLDOWN(既定30)
  ターンの間再発火できない。同じDBコピーを30問ぶん順番に流すと、turn_seqが
  固定(=707のまま)なので「一度発火したエピソードはその後29問ぶん永久にブロック」
  される。goldsetの30問には正解エピソードの重複が多数ある(29個のエピソードIDが
  複数問の正解集合に重複出現)ため、固定順で流すと質問順によって結果が変わる。
  → 本スクリプトは「質問ごとにDBを土台からリセットする」方式を採用
    (turn_seqは常に土台の707で、全構成・全質問で完全に同一)。
  トレードオフ: セッション内の学習(発火によるstrength蓄積)は測れない。
  それは ab_compare.py (floor軸)の役目で、本スクリプトは「1メッセージに対して
  どのエピソードが出て、それは当たりか」というゲーティング純効果だけを見る。

LLM呼び出しは一切ない(trailmem-scan.sh, trailmem-spread.sh ともに
sqlite3 + python標準ライブラリのみ)。

Usage:
  python3 eval_flashback_ab.py --sweep                       # Step1: θ2スイープ
  python3 eval_flashback_ab.py --theta2 0.25                 # Step2: 最終4構成
  python3 eval_flashback_ab.py --theta2 0.25 --sweep \\
      --out ab_flashback_results_v2.json                     # 両方を1ファイルに
"""
import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))  # oss リポジトリのルート
SCAN_SH = os.path.join(REPO_ROOT, "trailmem-scan.sh")

SCRATCH = os.environ.get(
    "TM_SCRATCH",
    os.path.join(tempfile.gettempdir(), "trailmem-timemachine"),
)
WORKDIR = os.path.join(SCRATCH, "tm_dbs")

# 0.15-0.5 は指示された範囲。ハブペナルティ後の収束スコアの実レンジ
# (汎用ペア≈0.09 / 特定ペア≈0.15-0.3) がその下にあると分かったため
# 0.05 / 0.1 / 0.125 も追加で掃く
SWEEP_THRESHOLDS = ["0.05", "0.1", "0.125", "0.15", "0.2", "0.25", "0.3", "0.4", "0.5"]


def load_goldset(path):
    cases = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


def run_scan(db_path, buffer_path, prompt, door2, weakest_first,
             theta2="0.5", spread="1"):
    """改修済み trailmem-scan.sh を1問ぶん実行し、発火バッファ(list of dict)を返す。"""
    if os.path.exists(buffer_path):
        os.remove(buffer_path)
    env = os.environ.copy()
    env["TRAILMEM_DB"] = db_path
    env["TRAILMEM_FLASHBACK_BUFFER"] = buffer_path
    env["TRAILMEM_DOOR2"] = door2
    env["TRAILMEM_WEAKEST_FIRST"] = weakest_first
    env["TRAILMEM_DOOR2_THRESHOLD"] = theta2
    env["TRAILMEM_SPREAD_FLASHBACK"] = spread
    # 明示指定(スクリプト既定と同値。将来デフォルトが変わっても本ベンチの前提が
    # ズレないようにするための固定):
    env["TRAILMEM_FLASHBACK_THRESHOLD"] = "0.42"
    env["TRAILMEM_FLASHBACK_COOLDOWN"] = "30"
    env["TRAILMEM_MAX_FLASHBACKS"] = "3"
    env.pop("TRAILMEM_SCRIPT_DIR", None)  # scan.sh自身の場所から自動解決させる

    proc = subprocess.run(
        ["bash", SCAN_SH, prompt],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    records = []
    if os.path.exists(buffer_path):
        with open(buffer_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        os.remove(buffer_path)
    return records, proc.returncode, proc.stderr


def rr(fired_ids, relevant):
    for i, eid in enumerate(fired_ids, 1):
        if eid in relevant:
            return 1.0 / i
    return 0.0


def eval_config(label, base_db, cases, door2, weakest_first,
                theta2="0.5", spread="1", desc=""):
    safe = "".join(ch if ch.isalnum() else "_" for ch in label)[:40]
    db_copy = os.path.join(WORKDIR, f"cfg_{safe}.db")
    buf_path = os.path.join(WORKDIR, f"cfg_{safe}_buffer.jsonl")
    per_case = []
    door_type_counts = {"emotion": 0, "convergence": 0, "spread": 0}
    n_errors = 0
    t0 = time.time()
    for c in cases:
        shutil.copy2(base_db, db_copy)  # 毎問、育ったDBの土台へ完全リセット
        records, rc, stderr = run_scan(db_copy, buf_path, c["prompt"],
                                       door2, weakest_first, theta2, spread)
        if rc != 0:
            n_errors += 1
        fired_ids = [r["episode_id"] for r in records]
        for r in records:
            d = r.get("door", "?")
            door_type_counts[d] = door_type_counts.get(d, 0) + 1
        relevant = set(c["relevant"])
        hit_ids = [eid for eid in fired_ids if eid in relevant]
        n_fired = len(fired_ids)
        n_hit = len(hit_ids)
        per_case.append({
            "id": c["id"],
            "prompt": c["prompt"],
            "n_relevant": len(relevant),
            "fired": fired_ids,
            "doors": [r.get("door") for r in records],
            "n_fired": n_fired,
            "n_hit": n_hit,
            "coverage": 1 if n_hit > 0 else 0,
            "recall_item": (n_hit / len(relevant)) if relevant else None,
            "precision": (n_hit / n_fired) if n_fired > 0 else None,
            "rr": rr(fired_ids, relevant),
            "stderr": stderr.strip()[:300] if rc != 0 else "",
        })
    dt = time.time() - t0
    for p in (db_copy, buf_path):
        try:
            os.remove(p)
        except OSError:
            pass
    return {
        "label": label, "door2": door2, "weakest_first": weakest_first,
        "theta2": theta2, "spread": spread,
        "desc": desc, "n_errors": n_errors, "elapsed_sec": round(dt, 2),
        "door_type_counts": door_type_counts, "per_case": per_case,
    }


def aggregate(result):
    per = result["per_case"]
    n = len(per) or 1
    fired_counts = [p["n_fired"] for p in per]
    zero_fire = sum(1 for f in fired_counts if f == 0)
    precisions = [p["precision"] for p in per if p["precision"] is not None]
    recalls = [p["recall_item"] for p in per if p["recall_item"] is not None]
    uniq = set()
    for p in per:
        uniq.update(p["fired"])
    agg = {
        "n_cases": len(per),
        "total_fired": sum(fired_counts),
        "avg_fired_per_prompt": sum(fired_counts) / n,
        "zero_fire_rate": zero_fire / n,
        "unique_fired_episodes": len(uniq),
        "coverage": sum(p["coverage"] for p in per) / n,
        "recall_item_avg": (sum(recalls) / len(recalls)) if recalls else 0.0,
        "precision_at_fired_avg": (sum(precisions) / len(precisions)) if precisions else None,
        "precision_denominator_n": len(precisions),
        "MRR": sum(p["rr"] for p in per) / n,
        "door_type_counts": result["door_type_counts"],
        "n_errors": result["n_errors"],
        "elapsed_sec": result["elapsed_sec"],
    }
    return agg


def fmt_row(label, a, width=44):
    prec = a["precision_at_fired_avg"]
    prec_s = f"{prec:.3f}" if prec is not None else "  n/a"
    return (f"{label:<{width}} {a['avg_fired_per_prompt']:>7.3f} {a['zero_fire_rate']:>7.3f} "
            f"{a['total_fired']:>6d} {a['unique_fired_episodes']:>5d} {a['coverage']:>6.3f} "
            f"{a['recall_item_avg']:>7.3f} {prec_s:>6} {a['MRR']:>6.3f}")


def print_table(title, rows, width=44):
    hdr = (f"{'構成':<{width}} {'発火/問':>7} {'ゼロ率':>7} {'総発火':>6} {'一意':>5} "
           f"{'cov':>6} {'recall':>7} {'prec':>6} {'MRR':>6}")
    print(f"\n=== {title} ===")
    print(hdr)
    print("-" * len(hdr))
    for label, a in rows:
        print(fmt_row(label, a, width))


def print_diffs(base_label, base, others):
    print(f"\n差分 (vs {base_label}):")
    for label, a in others:
        dp = (a["precision_at_fired_avg"] - base["precision_at_fired_avg"]
              if a["precision_at_fired_avg"] is not None
              and base["precision_at_fired_avg"] is not None else None)
        dp_s = f"{dp:+.3f}" if dp is not None else "n/a"
        print(f"  {label}:")
        print(f"    Δ発火/問={a['avg_fired_per_prompt']-base['avg_fired_per_prompt']:+.3f} "
              f"Δゼロ率={a['zero_fire_rate']-base['zero_fire_rate']:+.3f} "
              f"Δ総発火={a['total_fired']-base['total_fired']:+d} "
              f"Δ一意={a['unique_fired_episodes']-base['unique_fired_episodes']:+d}")
        print(f"    Δcov={a['coverage']-base['coverage']:+.3f} "
              f"Δrecall={a['recall_item_avg']-base['recall_item_avg']:+.3f} "
              f"Δprec={dp_s} ΔMRR={a['MRR']-base['MRR']:+.3f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-db", default=os.path.join(HERE, "kokoro_floor_on_sp.db"))
    ap.add_argument("--goldset", default=os.path.join(HERE, "goldset.jsonl"))
    ap.add_argument("--out", default=os.path.join(HERE, "ab_flashback_results_v2.json"))
    ap.add_argument("--sweep", action="store_true",
                    help="Step1: DOOR2=1 WEAKEST=0 でθ2をスイープ")
    ap.add_argument("--theta2", default=None,
                    help="Step2: このθ2で最終4構成 (A/B'/D'/E) を走らせる")
    args = ap.parse_args()

    os.makedirs(WORKDIR, exist_ok=True)
    if not os.path.exists(SCAN_SH):
        raise SystemExit(f"trailmem-scan.sh not found at {SCAN_SH}")
    if not os.path.exists(args.base_db):
        raise SystemExit(f"base db not found: {args.base_db}")
    if not args.sweep and args.theta2 is None:
        raise SystemExit("--sweep か --theta2 (または両方) を指定してください")

    cases = load_goldset(args.goldset)
    print(f"goldset: {len(cases)}問  base_db={os.path.basename(args.base_db)}  "
          f"scan_sh={SCAN_SH}")

    t_total0 = time.time()
    out = {"meta": {
        "goldset": os.path.basename(args.goldset),
        "n_cases": len(cases),
        "base_db": os.path.basename(args.base_db),
        "scan_sh": SCAN_SH,
        "scan_sh_version": "v2 (door2 hub penalty eff*min(1,1/ln(1+degree)); "
                           "weakest_first = neglect R asc, gate score desc)",
        "design": "per-question DB reset (turn_seq fixed at grown value for all configs/questions)",
        "llm_calls": 0,
    }}

    # ---- Step 1: θ2 スイープ ----
    if args.sweep:
        sweep_rows = []
        sweep_raw = {}
        print("\n>>> Step1: θ2スイープ (DOOR2=1, WEAKEST_FIRST=0, spread=on)")
        for th in SWEEP_THRESHOLDS:
            r = eval_config(f"theta2={th}", args.base_db, cases,
                            door2="1", weakest_first="0", theta2=th,
                            desc=f"DOOR2=1 WEAKEST=0 theta2={th}")
            a = aggregate(r)
            sweep_raw[th] = r
            sweep_rows.append((f"theta2={th}", a))
        print_table("θ2スイープ (B'系: DOOR2=1 WEAKEST=0)", sweep_rows, width=14)
        out["sweep"] = {th: aggregate(r) for th, r in sweep_raw.items()}
        out["sweep_raw"] = sweep_raw

    # ---- Step 2: 最終4構成 ----
    if args.theta2 is not None:
        th = args.theta2
        configs = [
            ("A: DOOR2=0 WEAKEST=0 (旧挙動)",
             dict(door2="0", weakest_first="0", theta2="0.5", spread="1")),
            (f"B': DOOR2=1(hub,θ2={th}) WEAKEST=0",
             dict(door2="1", weakest_first="0", theta2=th, spread="1")),
            (f"D': DOOR2=1(hub,θ2={th}) WEAKEST=1(忘れられ度)",
             dict(door2="1", weakest_first="1", theta2=th, spread="1")),
            (f"E: DOOR2=1(hub,θ2={th}) WEAKEST=1 spread=OFF",
             dict(door2="1", weakest_first="1", theta2=th, spread="0")),
        ]
        final_rows = []
        final_raw = {}
        for label, kw in configs:
            print(f"\n>>> {label}")
            r = eval_config(label, args.base_db, cases, desc=label, **kw)
            a = aggregate(r)
            final_raw[label] = r
            final_rows.append((label, a))
            print("    " + fmt_row("", a, width=1))
        print_table(f"最終4構成 (θ2={th}, 質問毎DBリセット)", final_rows)
        print_diffs(final_rows[0][0], final_rows[0][1], final_rows[1:])
        # B'→D' の弱い子優先効果
        bp = final_rows[1][1]
        print_diffs(final_rows[1][0], bp, final_rows[2:])
        out["final"] = {label: aggregate(r) for label, r in final_raw.items()}
        out["final_raw"] = final_raw
        out["meta"]["theta2_selected"] = th

    total_dt = time.time() - t_total0
    out["meta"]["total_elapsed_sec"] = round(total_dt, 2)
    print(f"\n総実行時間: {total_dt:.1f}s / LLM呼び出し: 0")

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"  -> {args.out}")


if __name__ == "__main__":
    main()
