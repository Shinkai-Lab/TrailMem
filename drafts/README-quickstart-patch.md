# README差し込み案: TL;DR直後に「⚡ 5分で動かす」節

配置場所: `README.md` の `## TL;DR` セクション（`---` の直前）と `## 🔭 触れるデモ` の間に挿入。`README.en.md` にも対の節を英語で挿入。

Insertion point: between the `## TL;DR` section (right before its closing
`---`) and `## 🔭 触れるデモ` in `README.md`. Insert the matching English
section in `README.en.md` too.

---

## ⚠️ 検証結果: 既存インストール節のコマンド列に実行順の不備あり

`## インストール / セットアップ` の既存コードブロックをそのまま順番に実行すると失敗することを実機で確認しました。

```bash
cd trailmem/daemon
pip install -r requirements.txt
python -m daemon.cli start      # ← ここで ModuleNotFoundError: No module named 'daemon.config'; 'daemon' is not a package
```

原因: `daemon/cli.py` / `daemon/daemon.py` は `from .config import ...`（パッケージ相対importとしての `daemon.cli` / `daemon.daemon`）を前提にしており、`python -m daemon.cli` はカレントディレクトリが **`daemon/`パッケージの親**（つまりリポジトリのルート）である必要があります。`cd trailmem/daemon` した状態で `python -m daemon.cli` を叩くと、Pythonは `<cwd>/daemon/cli.py`（＝存在しない `daemon/daemon/cli.py`）を探しにいってしまい失敗します。

`pip install -r requirements.txt` は `requirements.txt` が `daemon/` 直下にあるため `cd trailmem/daemon` した状態で実行する分には問題ありませんが、続く `python -m daemon.cli ...` はリポジトリルート（`trailmem/`）に戻ってから実行する必要があります。下記の「5分で動かす」節はこの実行順で書いています。**既存のインストール節本体もこの直し（`cd trailmem/daemon` → `cd ..` を挟むか、`pip install -r daemon/requirements.txt` に統一してルートに留まるか）が必要**なので、CTOレビュー時に本体側の修正も検討してください。

---

## 差し込み案 本文（日本語）

## ⚡ 5分で動かす

```bash
# 1. clone してリポジトリルートへ
git clone https://github.com/Shinkai-Lab/trailmem.git
cd trailmem

# 2. 依存インストール（daemon/requirements.txt を参照。cwdはリポジトリルートのまま）
pip install -r daemon/requirements.txt

# 3. daemon起動（初回はオプトイン確認あり。監視ディレクトリ・DB保存先・
#    送信先LLMバックエンドが表示される。-y で確認をスキップ）
python -m daemon.cli start -y

# 4. 起動確認
python -m daemon.cli status

# 5. 想起パイプラインの動作確認（この時点ではDBは空なので「発火なし」の
#    統計出力が返れば正常。門1/門2の閾値やキーワード統計がここで見える）
bash trailmem-scan.sh "今日は晴れていてTrailMemの話をした"
```

これで `~/.claude/projects/**/*.jsonl` の監視が始まっています。Claude Codeでの会話が `chunk_size`（既定10ターン）たまるか、`silence_minutes`（既定5分）の無音が続くと、自動でエピソードが記銘されます（既定の `llm_backend: "cli"` はローカルの `claude -p` を呼ぶだけなので追加のAPI課金は発生しません）。実際に記憶が育ったかは再度 `python -m daemon.cli status` の `episodes:` を見るか、`bash trailmem-scan.sh "..."` を会話後にもう一度叩いて確認してください。

止める・一時停止するには:

```bash
python -m daemon.cli pause   # 記銘を一時停止(既に溜まった分はflushを延期)
python -m daemon.cli resume
python -m daemon.cli stop
```

より詳しい設定（`~/.trailmem/config.json` の各項目、方式B: Claude Codeフック方式）は下記「インストール / セットアップ」を参照してください。

---

## Draft body (English, for README.en.md)

## ⚡ Up and running in 5 minutes

```bash
# 1. Clone and stay at the repo root
git clone https://github.com/Shinkai-Lab/trailmem.git
cd trailmem

# 2. Install dependencies (requirements.txt lives under daemon/; stay at repo root)
pip install -r daemon/requirements.txt

# 3. Start the daemon (asks for opt-in confirmation the first time — shows
#    the watch dir, DB path, and which LLM backend will receive text.
#    -y skips the prompt)
python -m daemon.cli start -y

# 4. Confirm it's running
python -m daemon.cli status

# 5. Sanity-check the recall pipeline (the DB is empty at this point, so a
#    "no flashback" stats printout is the expected, correct result — you'll
#    see the gate-1/gate-2 thresholds and keyword stats here)
bash trailmem-scan.sh "sunny today, talked about TrailMem"
```

At this point the daemon is watching `~/.claude/projects/**/*.jsonl`. Once a
Claude Code conversation accumulates `chunk_size` turns (default 10) or goes
`silence_minutes` quiet (default 5), an episode gets encoded automatically
(the default `llm_backend: "cli"` just calls the local `claude -p`, so this
doesn't incur extra API charges). To check whether memory actually grew,
either re-run `python -m daemon.cli status` and look at `episodes:`, or run
`bash trailmem-scan.sh "..."` again after a conversation.

To pause or stop:

```bash
python -m daemon.cli pause   # defer encoding (buffered writes wait)
python -m daemon.cli resume
python -m daemon.cli stop
```

For deeper configuration (every `~/.trailmem/config.json` field, or Method B:
the Claude Code hook), see "Installation / Setup" below.
