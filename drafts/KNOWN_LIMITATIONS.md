# TrailMem — 既知の制限 / Known Limitations

正直ベースで書きます。誇張・希望的観測は避け、確認できている範囲とできていない範囲を分けます。

Written plainly. No hype, no wishful thinking — a hard line between what's
been verified and what hasn't.

---

## 確認済み環境 / Verified environment

- OS: Linux
- クライアント: Claude Code（`~/.claude/projects/**/*.jsonl` を書き出すもの）
- LLMバックエンド: `llm_backend: "cli"`（`claude -p` サブプロセス経由）
- 動作確認・ベンチマークはすべて本リポジトリの開発環境（このリポジトリ自身、および `benchmark/timemachine/` の夏目漱石『こころ』データ）で行っています

- OS: Linux
- Client: Claude Code (the one that writes `~/.claude/projects/**/*.jsonl`)
- LLM backend: `llm_backend: "cli"` (via the `claude -p` subprocess)
- All functional testing and benchmarking has been done in this repo's own
  development environment (this repository itself, plus the *Kokoro*
  dataset under `benchmark/timemachine/`)

これ以外の組み合わせは動く可能性が高いものの、実際に通しで検証はしていません。

Other combinations will likely work, but haven't actually been exercised
end-to-end.

---

## 未確認環境 / Unverified environments

- **Cursor / Cline / Codex 等のjsonl形式**: daemonはツール非依存を謳っていますが（`~/.claude/projects` 以外の `watch_dir` を指す、jsonl形式の会話ログを吐くツールなら動くはず）、これらのツールが実際に吐くjsonlのフィールド構造（`message.role` / `message.content` の形、`isCompactSummary` 等の圧縮マーカーの有無）がClaude Codeと完全に一致する保証はありません。`daemon/ingest.py` の `parse_jsonl_messages` はClaude Codeのjsonl構造を前提にパースを書いているため、他ツールのjsonlでは無言でメッセージを拾えない（0件パースされ、何も記銘されない）可能性があります。
  **Cursor / Cline / Codex jsonl formats**: the daemon is designed to be
  tool-agnostic (any tool writing jsonl conversation logs to a configured
  `watch_dir` should work), but the exact field shapes other tools emit
  (the shape of `message.role` / `message.content`, whether they have a
  compaction marker like `isCompactSummary`) aren't guaranteed to match
  Claude Code's. `daemon/ingest.py`'s `parse_jsonl_messages` is written
  against Claude Code's jsonl shape, so on another tool's jsonl it may
  silently parse zero messages and encode nothing — with no error surfaced.
- **Mac / Windows**: パス処理は `pathlib` ベースで大きくは問題ないと思われますが、実機での動作確認はしていません。`watchdog` のファイル監視挙動もOSごとに差があり得ます。
  **Mac / Windows**: path handling is `pathlib`-based and likely fine in
  principle, but has not been run on real hardware. `watchdog`'s file-watching
  behavior can also differ across OSes.

---

## 既知の弱点 / Known weaknesses

### LLM記銘の失敗が静かに空振りする / Silent encoding failures

`call_llm` がタイムアウト（180秒）・接続エラー・空応答などで失敗した場合、`_call_cli` / `_call_anthropic` / `_call_openai` はいずれも例外を投げず空文字列を返します。`ingest_chunk` はその場合 `insert_episodes` が0件になり、会話が進行中とみなされてバッファにcarryoverされるだけです（`daemon/daemon.py` `_flush_if_due`）。つまり**LLM呼び出しが継続的に失敗していても、daemonのログに「flushしようとした」記録は残るが、ユーザーに向けたエラー通知や、失敗の累積を可視化する仕組みは今のところありません。** `trailmem-doctor.sh` の9セクションのレポートにも「記銘の失敗率」を検出する項目はまだありません。doctor側での改善を予定していますが、現時点では未実装です。

If `call_llm` fails (180s timeout, connection error, empty response — any of
`_call_cli` / `_call_anthropic` / `_call_openai`), none of them raise; they
return an empty string. `ingest_chunk` then gets zero episodes back, and the
chunk is treated as "topic still in progress" and carried over
(`daemon/daemon.py`'s `_flush_if_due`). In other words: **if LLM calls keep
failing, the daemon log shows attempted flushes, but there is no
user-facing error surfaced, and no visibility into an accumulating failure
rate.** None of `trailmem-doctor.sh`'s nine report sections currently detect
an encoding failure rate. Improving `doctor` to catch this is planned, but
not implemented yet.

### キーワードベースゆえの言語をまたいだ想起は不可 / No cross-language recall

想起（`trailmem-recall.sh` / `trailmem-scan.sh` の門1・門2 / `trailmem-spread.sh`）はすべてキーワードの完全一致・部分一致とアメーバ網（`keyword_edges`）の伝播に基づいています。埋め込み類似度を使う `trailmem-vec-search.sh` / hybrid recall はセマンティックな近さを拾えますが、それでも別言語のキーワード同士が自動で結びつくわけではありません。日英バイリンガルで運用した実験では、日本語キーワードと英語キーワードの間に共起エッジ（`keyword_edges`）が**0本**しか生成されないことが実証されています。つまり「日本語で話した記憶」と「同じ内容を英語で話した記憶」は、TrailMem上では別々の記憶網として扱われ、キーワードが言語を越えて自動的に結びつくことはありません。

Recall (`trailmem-recall.sh`, gates 1/2 in `trailmem-scan.sh`,
`trailmem-spread.sh`) is entirely based on exact/partial keyword matching plus
propagation over the amoeba net (`keyword_edges`). The embedding-based
`trailmem-vec-search.sh` / hybrid recall path can catch semantic closeness,
but even that doesn't automatically link keywords across languages. In a
bilingual (Japanese/English) test run, co-occurrence edges (`keyword_edges`)
between Japanese and English keywords came out to **exactly zero**. In
practice, a memory formed in Japanese and the same content discussed in
English end up as two separate memory webs in TrailMem — keywords do not
bridge languages automatically.

### フラッシュバックの的中率は体感3〜4割 / Flashback hit rate feels like 30-40%

これはベンチマークの数値ではなく、本番でTrailMem上で運用しているエージェント自身の申告です（README「使って動いてる側の話」参照）。関連性はあるが「今それ要る？」というフラッシュバックも一定数あります。門1/門2の閾値・`TRAILMEM_MAX_FLASHBACKS`・クールダウンはすべて環境変数でチューニング可能なので、体感が悪ければ「チューニング」セクションの値を動かすことを前提にした設計です。

This isn't a benchmark number — it's the self-reported experience of the
agent actually running on TrailMem in production (see the README's "From the
AI running on it" section). A non-trivial fraction of flashbacks are related
but land more like "do I really need this right now." The gate 1/gate 2
thresholds, `TRAILMEM_MAX_FLASHBACKS`, and cooldown are all env-var tunable —
the design assumes you'll go adjust the Tuning-section values if the feel is
off.

### daemonの死活は要監視 / Daemon uptime needs external monitoring

`daemon.pid` はプロセスが生きている間だけ存在しますが（`daemon/cli.py` `_running_pid` は `os.kill(pid, 0)` で確認）、daemonがクラッシュした場合に自動で再起動する仕組みは同梱していません。`python -m daemon.cli status` で `stopped` になっていないかを外側から定期的に確認する必要があります（cron/systemd等は付属していません）。daemon停止中に書かれた会話ログは、次回start時に「新規行」としては拾われません（`_bootstrap_existing` は未追跡ファイルをEOFまでスキップする設計のため、停止していた間の会話は記銘されずに素通りします）。

`daemon.pid` exists only while the process is alive
(`daemon/cli.py`'s `_running_pid` checks via `os.kill(pid, 0)`), but there's
no bundled auto-restart if the daemon crashes. You need to poll
`python -m daemon.cli status` for `stopped` yourself from the outside (no
cron/systemd unit is shipped). Conversation written while the daemon was down
does **not** get picked up retroactively as "new lines" the next time it
starts — `_bootstrap_existing` only skips-to-EOF for files it hasn't tracked
yet, so anything written during an outage on an already-tracked file is only
caught if the file offset genuinely advanced; a long outage followed by
restart risks silently missing a stretch of conversation from encoding.

---

## そのほか、実装から見える細かい注意点 / Other implementation-level caveats

- `min_chunk_lines`（既定3）未満のメッセージ数のチャンクや、会話テキストが50文字未満のチャンクは記銘されずに捨てられます（`ingest_chunk`）。短いやり取りは意図的に無視される設計です。
  Chunks with fewer than `min_chunk_lines` (default 3) messages, or under 50
  characters of conversation text, are silently dropped without encoding
  (`ingest_chunk`). Short exchanges are intentionally ignored by design.
- エピソードの採否にも足切りがあります: summaryが10文字未満、keywordsが2件未満、salienceが0.2未満のいずれかに該当する候補は挿入されません（`insert_episodes`）。LLMが返した候補の一部が静かに捨てられるのは正常動作です。
  Episode insertion itself has cutoffs: a candidate is dropped if its summary
  is under 10 characters, has fewer than 2 keywords, or salience under 0.2
  (`insert_episodes`). Some LLM-returned candidates being silently discarded
  is expected, normal behavior — not a bug.
- `agent-` で始まるファイル名のjsonlはdaemonの監視対象から除外されます（`_watchdog_handler` の `_should_skip`）。サブエージェントのログを拾わないための意図的な設計です。
  jsonl files whose basename starts with `agent-` are excluded from daemon
  watching (`_should_skip` in `_watchdog_handler`) — an intentional filter to
  avoid ingesting subagent logs.
