# TrailMem タイムマシン・ベンチマーク

文学作品を時系列で記憶に流し込んで「育て」、登場人物の記憶・関係性の想起精度を
測る、人格維持ベンチマーク。

## なぜ作ったか

従来の人格維持ベンチ（`benchmark/personality/`）は **「既に育ちきったDBの
スナップショット」** に対して recall vs spread を比べるだけだった。
しかし TrailMem の核である **記憶定着（フロア方式 / 殿堂入り）** の効果は、
生ログを時系列で流して **記憶を育てる過程** を再現しないと測れない。

何度も思い出した記憶ほど沈みにくくなる——この「定着」は、時間発展（減衰 +
フラッシュバックによる想起の積み重ね）の中でしか現れない。スナップショット
比較では原理的に測れない。

文学作品（著作権切れ）を使えば:

- **正解が明確** — 作中の出来事・関係性は誰が読んでも一致する
- **著作権フリー** — 青空文庫の夏目漱石「こころ」を使用
- **言い換えで問える** — 作中表現を変えた質問にでき、表層一致でない想起を測れる

## パイプライン

```
青空文庫(こころ)
   │ parse_aozora.py    ルビ・注記除去 → プレーン化 → 段落分割
   ▼
data/kokoro_paragraphs.json   (707段落)
   │ ingest.py          段落 → エピソード(summary/感情/キーワード)
   ▼
data/kokoro_episodes.json
   │ make_db.py         空のベンチDB作成（trailmem.db のスキーマ）
   │ replay.py          ★時系列リプレイ：1件ずつinsert + 各チャンクで
   │                      decay + flashback（実運用フックと同じ時間発展）
   ▼
kokoro_*.db             育ったDB（構成ごとに別DB）
   │ build_goldset.py   Sonnetで登場人物の想起テスト問題を生成
   │                      正解 = 該当段落エピソードID
   ▼
goldset.jsonl
   │ eval.py            育てたDBで想起 → P@k/R@k/MRR/カバレッジ
   │ ab_compare.py      ★4構成(フロア×spread)を並べて比較
   ▼
ab_compare.json + 比較表
```

★ = このベンチの新規部分。`replay.py`（時間発展の再現）と
`ab_compare.py`（フロア方式の純効果を分離）が肝。

## 4構成（アブレーション軸）

| 構成 | floor | spread | 意味 |
|------|-------|--------|------|
| A | off | off | 旧decay + キーワード想起 = baseline |
| B | on  | off | フロア方式 + キーワード想起 |
| C | off | on  | 旧decay + 活性化拡散（アメーバ網） |
| D | on  | on  | フルスタック |

- **B − A**（spread=off固定）と **D − C**（spread=on固定）が
  **フロア方式の純効果**。
- **C − A** と **D − B** が **spread の純効果**。

## 使い方

```bash
cd benchmark/timemachine

# 1. テキスト取得（初回のみ）。SHIFT_JISのまま data/kokoro_raw_sjis.txt に置く
#    （青空文庫: 夏目漱石「こころ」 card 773）
#    既に data/kokoro_raw_sjis.txt がある前提

# 2. パース（段落分割）
python3 parse_aozora.py                      # フル(707段落)
python3 parse_aozora.py --max-sections 2 \   # パイロット(最初の2節)
    --out-paras data/kokoro_pilot_paragraphs.json \
    --out-plain data/kokoro_pilot_plain.txt

# 3. ingest（段落→エピソード）
python3 ingest.py data/kokoro_paragraphs.json --mode heuristic \
    --out data/kokoro_episodes.json
#   --mode llm で Sonnet 抽出（章単位バッチ、コスト配慮）も可

# 4. ゴールドセット生成（Sonnet）
python3 make_db.py kokoro_ref.db
python3 replay.py --db kokoro_ref.db --episodes data/kokoro_episodes.json --floor on
python3 build_goldset.py --ref-db kokoro_ref.db \
    --episodes data/kokoro_episodes.json --n 30 --out goldset.jsonl
#   生成済み質問は data/questions_raw.json にキャッシュされ再利用される

# 5. A/B比較（4構成を育てて評価）
#    フロア効果を見たいときは時間を圧縮する（加速エイジング）:
TM_DECAY_RATE=0.95 python3 ab_compare.py \
    --episodes data/kokoro_episodes.json --goldset goldset.jsonl \
    --prefix kokoro --level deep
```

### 単体評価

```bash
python3 eval.py --db kokoro_on_nosp.db --recall-cmd recall --level deep
python3 eval.py --db kokoro_on_sp.db   --recall-cmd spread
```

## 時間圧縮（加速エイジング）について

実運用フックの減衰は `0.999/ターン` と非常に緩い。1作品（707エピソード ≈
71チャンク）を流しても減衰の累積は約7%にとどまり、フロア（下限0.1）まで
沈む記憶がほとんど出ない＝フロア方式の効果が顕在化しない。

ベンチは「数年分の忘却を1作品で再現」するため、`TM_DECAY_RATE` を強める。
これは **時間の圧縮** であって不正ではない（実運用は月単位decayも併用する）。

| 環境変数 | 意味 | デフォルト |
|----------|------|-----------|
| `TM_DECAY_RATE`   | チャンクごとの減衰 | 0.999 |
| `TM_CHUNK_SIZE`   | 何件insertごとに時間発展 | 10 |
| `TM_N_CONSOLIDATE`| 殿堂入りに要する想起回数 | 30 |
| `TM_FLOOR_MIN`    | 1回想起のフロア | 0.1 |
| `TM_FLOOR_DECAY`  | 殿堂入り後の超緩減衰 | 0.99999 |

## 指標

`benchmark/personality/eval.py` と同じ:

- **Precision@k / Recall@k** (k=3,5,10)
- **MRR** — 最初の正解が何位で出るか
- **カバレッジ** — 正解を1件以上想起できたケースの割合
- **想起あり率** — 想起が空でなかった割合

## ファイル一覧

| ファイル | 役割 |
|----------|------|
| `parse_aozora.py`   | 青空文庫テキスト → プレーン化 + 段落分割 |
| `ingest.py`         | 段落 → エピソード（heuristic / llm） |
| `make_db.py`        | trailmem.db スキーマの空ベンチDB生成 |
| `replay.py`         | ★時系列リプレイ（insert + decay + flashback）。floor/spread on/off |
| `build_goldset.py`  | Sonnetで想起テスト問題生成 + 正解ID紐付け |
| `eval.py`           | 育てたDBで想起 → 指標計算 |
| `ab_compare.py`     | ★4構成を育てて比較。フロア純効果を分離 |
| `data/`             | 中間生成物（プレーンテキスト・段落・エピソード・問題・goldset） |

## 安全

- 本番 `trailmem.db` には一切触らない。ベンチ専用DB（`kokoro_*.db`）のみ生成・書込。
- 想起スクリプトは想起時にDBへ書く（けもの道強化）ため、評価は一時コピー上で実行。
- 想起実装は実運用スクリプト（リポジトリルート直下の `trailmem-recall.sh` /
  `trailmem-spread.sh`）をそのまま呼ぶ＝ベンチと実運用が乖離しない。

## データ出典（青空文庫）

`data/kokoro_raw_sjis.txt` は夏目漱石『こころ』（著作権切れ・パブリックドメイン）を
青空文庫から取得したものです。ファイル末尾に入力者・校正者クレジットと底本情報が
含まれています。再配布時もこのクレジット表記は残してください（入力: j.utiyama /
校正: 伊藤時也 / 底本: 集英社文庫）。

## これまでの結果（707段落・30問・heuristic ingest）

### フロア方式の純効果が見える条件

実運用のデフォルト（DECAY_RATE=0.999, N_CONSOLIDATE=30）では、1作品を流しても
減衰が緩く、最大想起回数 R≈13 が殿堂入り(30)に届かないため **フロアがほぼ
発火しない**（B≈A）。フロアの効果を測るには時間圧縮が要る。

加速エイジング（`TM_DECAY_RATE=0.95 TM_N_CONSOLIDATE=8`, level=deep）:

| 構成 | 想起率 | cov | MRR | R@10 |
|------|-------|-----|-----|------|
| A floor=off spread=off | 0.933 | 0.300 | 0.152 | 0.108 |
| B floor=on  spread=off | 1.000 | **0.367** | 0.152 | **0.150** |
| C floor=off spread=on  | 1.000 | 0.100 | 0.067 | 0.025 |
| D floor=on  spread=on  | 1.000 | **0.167** | **0.094** | **0.042** |

**フロア純効果（spread固定）**:
- B−A: カバレッジ **+0.067**, R@10 **+0.042**, 想起あり率 0.93→1.00
- D−C: MRR **+0.028**, カバレッジ **+0.067**, R@10 **+0.017**

→ **フロア方式は減衰圧の下で想起カバレッジ・再現率を一貫して改善する。**
何度も思い出した記憶が下限で保護され、忘却の波に沈まずに想起圏内へ残る。
この効果はスナップショット比較では出ず、時系列リプレイで初めて測れた。

### 注意すべき相互作用（要チューニング）

- フロアが強すぎる（高R記憶が strength 上限に張り付く）と、spread の再ランクが
  頼る strength の差が潰れ、汎用的な「私」の地の文ばかり上位に出て精度が落ちる
  （実運用デフォルトの floor=on + spread で観測）。フロアと spread の係数は
  セットでチューニングが必要。
- 想起レベル（recall≥0.5 / deep 0.2-0.5）とフロアが生む strength 帯が
  ずれると効果が相殺する。フロアが効くのは「フロア値 ≥ 想起閾値」になる帯。

## 公開について

このベンチは「人格維持を測るベンチ」のフレームワークとして公開可能。
データ（こころ）は著作権切れなので同梱できる。コードは汎用で、別の文学作品や
別の記憶システムにも `replay.py` の insert/recall インタフェースを差し替えれば
適用できる。
