# claude-code-limit-wait

*[English README はこちら](README.md)*

**普段ならセッションを終わらせてしまう瞬間を越えて、Claude Code のセッションを
生かしたまま・安く保つ** ための3つの小さなツール群。すべて hooks・Skill・
素の Python スクリプトで組まれており、待機中はモデル推論を一切走らせない:

| ツール | 対応する瞬間 | 停止する代わりに起きること |
| --- | --- | --- |
| **limit-wait** | 5時間または7日のレートリミットに達しようとしている | background のポーラーがリセットを待ち (サーバーが実際に解除したことも検証し)、会話全体を読み込んだままエージェントを再呼び出しする |
| **compact-loop** | context window が埋まってきている | モデルが自分の後継者向けに handoff を書き、自分自身に本物の `/compact` を発火させ、handoff から再開する — 「`/compact` を実行してください」と頼む必要はない |
| **cache-keepalive** | セッションが idle のままで1時間の prompt cache が切れかけている | background の Stop フックが 55 分ごとに1回、一言の返信でモデルを起こすので、次の本番ターンも cache hit のまま |

共通する仕組み: 起こす役目は harness 自身が担う。background の Bash プロセスは
終了時にエージェントを再呼び出しし、`asyncRewake` フックは system reminder で
モデルを起こす。どちらも人間がキーボードに戻ってくることに依存しない。

## なぜこれを作ったか

Claude Code の `/usage` データは、`/loop`・`Skill`・cron 発火・hooks のどれを
経由してもモデルには **届かない**。それを露出する唯一の経路が **statusLine**
— Claude Code はそこに設定されたスクリプトへ、stdin 経由でセッション JSON を
流し込む。そのデータがディスクに落ちれば、hook はセッションを止める代わりに
*待つ* ことを決断できる。残り2つのツールも同じパターンから生まれた —
モデルが自分で自分の context をいつリセットするか判断すること、そして idle
中に hook が cache を温め続けること。

## 構成要素

```
hooks/
  usage-probe-statusline.py    statusLine スクリプト。リフレッシュ毎にセッション
                               JSON を ~/.claude/usage-snapshot.json に保存
                               (単一ファイル、上書き、ログは伸びない) し、簡潔な
                               ステータス文字列を UI に返す。oauth cache が
                               あればそこからモデル別の週次リミットを表示する。

  oauth-usage-probe.py         ~/.claude/.credentials.json のトークンで
                               GET /api/oauth/usage を叩く (サブスクリプション
                               ログイン向け)。アカウントが公開する全レート
                               リミットバケットを表示する。--refresh-cache は
                               ~/.claude/.oauth-usage-cache.json に書き込み、
                               ネットワークで絶対にブロックしてはいけない
                               スクリプト向けに使う。トークンは常に header
                               でのみ送信する。

  context-monitor.py           PostToolUse フック。何かが変化した時、ツール
                               呼び出し毎に1行出す:
                                 ℹ️ Context used: NN% | Limits used: 5h XX% in … (rsts …), 7d XX% in …
                               5h ≥95% または 7d ≥99% のとき、limit-wait
                               Skill を今すぐ呼べという ⚠️ アドバイザリを
                               末尾に追記する。context window の 60 / 75 /
                               85 % 到達時には帯域アドバイザリを1回だけ
                               追記する (value zone / 区切りへ向けて舵を
                               切る / compact-loop を今すぐ走らせる)。team
                               leader の場合は subagent が 60/75/85/95 % を
                               跨いだことも報告する。user 向けの行は、
                               CLAUDE_HOOK_USER_LANG=ja が設定されていない
                               限り英語 (Install 参照)。model 向けの
                               テキストは常に英語。

  limit-wait.py                実際の待機プロセス。スナップショットを読み、
                               拘束的なリミット (クリティカル閾値を超えて
                               いる方。両方超えていれば、リセットが遅い方)
                               を選び、resets_at + buffer までスリープした
                               後、バケットが本当に解除を報告するまで
                               サーバーをポーリングする。待機中は
                               early-reset (早期リセット) も監視する。進捗は
                               ~/.claude/.limit-wait-state.json に書く
                               (session_id をキーにした dict)。最終 stdout
                               は機械可読な JSON 1行。

  compact-handoff-guard.py     PreToolUse フック (CronCreate|Bash|PowerShell)。
                               limit-wait.py の起動に --session-id を注入
                               する (モデルは意識しなくてよい)。subagent が
                               leader に対して compact を発火するのを block
                               し、新しい handoff が無い時は trigger_compact.py
                               を block する。--cwd をセッションの実 root
                               へ書き換える。fail-open。その block
                               メッセージも CLAUDE_HOOK_USER_LANG に従う。

  compact-handoff-resume.py    UserPromptSubmit フック。compact の後、次の
                               プロンプトに handoff file へのポインタが
                               additionalContext として注入されるので、
                               再開が auto-summary に依存しない。

  cache-keepalive.py           Stop フック (asyncRewake)。セッションが
                               55 分 idle になるまで待ち、1行の ping と
                               ともに exit 2 する。harness がモデルを
                               起こし、モデルが "ok" と返信して Stop が
                               再び発火する。

skills/limit-wait/SKILL.md     limit-wait.py を background で起動して idle
                               し、task 通知が届いたら再開する。

skills/compact-loop/           SKILL.md (6-step サイクル)、clear-mode.md
                               (/clear 版)、recovery.md (compact が発火
                               しなかった時)、trigger_compact.py
                               (auto-compact を発火させる)、
                               inject_compact.py (Windows 専用の console
                               フォールバック)。

scripts/compact-handoff/dump.py
                               handoff を
                               <cwd>/.work/compact-handoff/<session_id>.md
                               に書く。

tools/install_cache_keepalive.py
                               ~/.claude/settings.json に keep-alive Stop
                               フックを登録 / 解除する (先に backup を取る)。
tools/sync_from_local.py       メンテナ用ツール: ~/.claude/ から repo コピー
                               を再生成する (--check で drift を報告)。

examples/settings.json.example 配線全部を1ファイルに。
```

### ツール別ファイル一覧

| Tool | Hooks | Skill / scripts | settings.json blocks |
| --- | --- | --- | --- |
| limit-wait | `usage-probe-statusline.py`, `oauth-usage-probe.py`, `context-monitor.py`, `limit-wait.py`, `compact-handoff-guard.py` | `skills/limit-wait/SKILL.md` | `statusLine`, `PostToolUse`, `PreToolUse` |
| compact-loop | `usage-probe-statusline.py`, `context-monitor.py` (≥75 % アドバイザリ), `compact-handoff-guard.py`, `compact-handoff-resume.py` | `skills/compact-loop/*`, `scripts/compact-handoff/dump.py` | `statusLine`, `PostToolUse`, `PreToolUse`, `UserPromptSubmit` |
| cache-keepalive | `cache-keepalive.py` | `tools/install_cache_keepalive.py` (任意) | `Stop` |

`usage-probe-statusline.py` は共有: その snapshot が、他のスクリプトが読む
session id・cwd・rate limits・context-window size を運ぶ。
`compact-handoff-guard.py` は最初の2つで共有: 待機プロセス向けに
`--session-id` を注入し、compact trigger を保護する。

## 1. limit-wait

### 連動の流れ

```
 ┌──────────────────────────┐
 │ Claude Code TUI          │
 │  statusLine refresh ─────┼─► usage-probe-statusline.py
 └──────────────────────────┘                │
                                             ▼
                              ~/.claude/usage-snapshot.json
                                             │
       (Bash/Edit/Write の毎ツール呼び出し) ──┴────┐
                                                  ▼
                                         context-monitor.py
                                                  │
                              モデル宛:
                              "ℹ️ Context used: … | Limits used: … | ⚠️ Invoke limit-wait NOW"
                                                  │
                                                  ▼
                                    Skill(name="limit-wait")
                                                  │
                                                  ▼
                              Bash run_in_background:true →  limit-wait.py
                               (compact-handoff-guard.py が --session-id を注入)
                                                  │
                                    resets_at + buffer までスリープ
                                    (モデルターン 0、early-reset 監視は 60 分毎)
                                                  │
                                    /api/oauth/usage をポーリングし
                                    バケットが解除を報告するまで待つ (≤15 分)
                                                  │
                                          プロセス終了
                                                  │
                                                  ▼
                                Claude Code がエージェントを再呼び出し
                                会話コンテキストは完全保持。
                                元の作業をそのまま続行。
```

Skill は **2つ** の待機プロセスを起動する: 2つ目は 7分の buffer を持ち、
リトライ役を務める — 完了通知で起きるタイミングは一発勝負なので、API が
その1回の試行を拒否した場合に備え、バックアップの後発通知が2回目の試行に
なる。

### 閾値とチューニング

| 定数           | 場所                                   | デフォルト | 意味                                                        |
| -------------- | -------------------------------------- | ------- | ---------------------------------------------------------- |
| `H5_CRITICAL`  | `context-monitor.py`, `limit-wait.py`  | 95      | 5時間 `used_percentage` ≥ これでアドバイザリ発火            |
| `D7_CRITICAL`  | `context-monitor.py`, `limit-wait.py`  | 99      | 7日 `used_percentage` ≥ これでアドバイザリ発火              |
| `--buffer`     | `limit-wait.py` CLI                    | 60      | `resets_at` を過ぎてから検証前にスリープする秒数            |
| `--max-wait`   | `limit-wait.py` CLI                    | 8 days  | 厳格なサニティ上限。これを超える待機は exit 3 で中止         |
| `--verify-max` | `limit-wait.py` CLI                    | 900     | 解除確認のためサーバーをポーリングし続ける秒数              |
| `POLL_STEP`    | `limit-wait.py`                        | 30      | 待機中の壁時計再チェック間隔 (秒)                            |
| `EARLY_CHECK_SEC` | `limit-wait.py`                     | 3600    | 待機途中の early-reset チェックの間隔                        |

`H5_CRITICAL` / `D7_CRITICAL` を調整する場合は両ファイルで揃えること。hook が
アドバイザリを出す地点と、待機プロセスが「対応するに値する」と判断する地点を
ぴったり一致させるため。

### 終了コード (limit-wait.py)

| Code | Status                                       | モデルがとるべき動作                       |
| ---- | --------------------------------------------- | ------------------------------------------- |
| 0    | `reset_reached` / `nothing_to_wait`           | 元の作業を続行                              |
| 3    | `abort_too_long` (待機が `--max-wait` 超過)   | ユーザーに通知する側にフォールバック         |
| 4    | `error_no_snapshot` / `error_no_session_id`   | ユーザーに通知する側にフォールバック         |

最終 JSON にはさらに `"verified"` (true / false、probe が使えなかった場合は
null) と、サーバーが公称時刻より前にリミットを解除した場合の
`"early_reset": true` も含まれる。

## 2. compact-loop

### なぜ

Auto-compact は発火が遅く、盲目的。この Skill は、後継者向けの handoff を
書いた後、ユーザーに `/compact` を打たせることなく、モデル自身が *きれいな
区切り* で自分をリセットできるようにする。

### 連動の流れ

```
 context ≥75% (context-monitor アドバイザリ) または dead end または phase break
                          │
                          ▼
              Skill(name="compact-loop")
                          │
   Step 1  session_id / cwd を確定           dump.py --print-path-only
   Step 2  self-audit (波及確認 + documentation 義務)
   Step 3  handoff を書く                     dump.py --topic … < body
           → <cwd>/.work/compact-handoff/<session_id>.md
   Step 4  consolidate (memory / commit / task list)
   Step 5  reset を発火 — その turn 最後の tool call、background で:
           trigger_compact.py が <cwd>/.claude/settings.local.json の
           CLAUDE_CODE_AUTO_COMPACT_WINDOW を縮小し、動作中のプロセスが
           それを hot-reload、次の推論ステップの前に auto-compact が発火
           する。スクリプトは結果に関わらず ~120 秒後に値を復元する。
                          │
                          ▼
              /compact が実行される (session_id・task list・background task は survive)
                          │
   Step 6  次のプロンプト → compact-handoff-resume.py が handoff path を注入
           → env が復元されたか検証 → handoff を読む → 続行
```

`/clear` モード (`clear-mode.md`) も同じことを行うが、`/clear` は session_id
を回転させるため、handoff path を運ぶ wake cron を使う。`recovery.md` は
compact が発火しなかった場合 (縮小した window が動作中のプロセスに届いたかを
liveness probe で確認する) と、console-input フォールバック `inject_compact.py`
(CLI 自身の入力バッファに `/compact` を打ち込む — Windows 専用、かつ誰も
キーボードの前にいない時のみ) をカバーする。

### チューニング (trigger_compact.py)

| Flag               | デフォルト | 意味                                                                                        |
| ------------------- | ------- | ------------------------------------------------------------------------------------------- |
| `--window`         | 200000  | `CLAUDE_CODE_AUTO_COMPACT_WINDOW` に書き込む値。auto-compact は window − 33000 トークン (デフォルトの output reserve) で発火する。最小 140000 |
| `--restore-value`  | 900000  | compact 後に書き戻すベースライン。自分のモデルの実際の window に合わせて設定すること: 900000 は 1M window のモデルが自発的に auto-compact しないようにし、200K のモデルには 200000 を使う |
| `--restore-after`  | 120     | ベースラインが復元されるまでの秒数 (detached worker がセッションが死んでも実行する)          |
| `--pre-sleep`      | 30      | 縮小前の秒数。スクリプトを起動した turn が終わっているようにするため                        |
| `--content-value`  | —       | 直前の compact から数分以内に再発火する時に必要な、1行の正当化理由                          |

guard hook は、subagent からの `trigger_compact.py` と `inject_compact.py` の
実行を拒否する (process env は共有されているため、縮小すると leader が
compact されてしまう)。同様に、そのセッションに新しい handoff が存在しない
時も拒否する。

## 3. cache-keepalive

### なぜ

Claude Code はサブスクリプションログインで1時間の prompt cache を保持する。
cache hit は fresh read のごく一部のコストで済み (ほとんどのモデルで input
価格の 0.1倍、Fable 5.1 では 0.025倍)、リクエストのたびにその1時間がリセット
される。セッションが60分 idle になると、次の本番ターンは context 全体を
full price で読み直すことになる。

### 連動の流れ

```
 Stop (turn 終了)
   │  Stop のたびに新しい cache-keepalive.py が spawn される。最新の
   │  インスタンスがセッションを所有し (pid lock
   │  ~/.claude/.cache-keepalive-<session_id>.pid)、古いインスタンスは
   │  次の poll で終了する
   ▼
 30秒ごとに poll: idle = 現在時刻 − 最後の user/assistant レコードの timestamp
   │  (away recap のような harness 専用レコードはカウントしない)
   ▼
 idle ≥ 3300 秒 → stderr: "just keeping this session's prompt cache warm …
                          Reply with the single word ok" → exit 2
   │
   ▼
 harness がモデルを起こす (rewakeMessage を前置きした system reminder。
 ユーザーのターミナルには rewakeSummary が表示される) → "ok" → Stop → 繰り返し
```

放置された run は自分で終わる: tool call も人間の入力もないまま 12 回連続で
ping に応答すると、次のインスタンスは無言で終了する (モデルを何か行動に
駆り立てかねない「終了しました」的なメッセージは出さない)。6回目の ping には、
先に compact しておけば毎回の refresh が安く済むという1回限りの note が
添えられる。実際のユーザープロンプトはカウントをリセットする。

コスト: 1回の ping は cache された prefix を読み、わずかなトークンを生成する
1リクエスト — Fable 5.1 では、それが防ぐ re-cache のおよそ 1/70 に相当する。
context が大きい場合、長い idle の前に compact しておけば、ping 1回あたりの
コストもそれに比例して安くなる。

### チューニング (cache-keepalive.py)

| 定数 / Flag                | デフォルト | 意味                                                                                       |
| --------------------------- | ------- | ------------------------------------------------------------------------------------------- |
| `--idle-seconds`           | 3300    | ping を打つまでの idle 時間 (55分 = 1時間 TTL の5分前)                                      |
| `--poll`                   | 30      | idle チェックの間隔 (秒)                                                                     |
| `MAX_CONSECUTIVE_PINGS`    | 12      | run が無言で止まるまでの放置 ping 回数 (約11時間)                                            |
| `HINT_AT_PING`              | 6       | 何回目の ping に compaction の note を1回だけ添えるか                                        |
| `timeout` (settings.json)  | 4000    | `--idle-seconds` を上回る必要がある: ドキュメントの記述に関わらず、harness は async hook を timeout で強制終了させる |

## インストール

> `python` として PATH 上に Python 3.10+ が必要。以下のパスは標準の
> `~/.claude/` 配置を前提とする。Windows では hook コマンドの先頭に
> `PYTHONUTF8=1 PYTHONIOENCODING=utf-8 ` を付けること (example 参照) —
> hook の出力が cp932 コンソールでも生き残るようにするため。

1. **ファイルをコピー** して Claude Code の設定ツリーに置く:
   ```
   hooks/*.py                       → ~/.claude/hooks/
   skills/limit-wait/SKILL.md       → ~/.claude/skills/limit-wait/SKILL.md
   skills/compact-loop/*            → ~/.claude/skills/compact-loop/
   scripts/compact-handoff/dump.py  → ~/.claude/scripts/compact-handoff/dump.py
   ```

2. **`~/.claude/settings.json` に配線する。** `examples/settings.json.example`
   に全ブロックがある。欲しいツールの分だけ残せばよい:
   - `statusLine` → `usage-probe-statusline.py` (3つ全部が必要とする:
     snapshot が session_id・cwd・rate limits・context window を運ぶ)
   - `hooks.PostToolUse` → `context-monitor.py`
   - `hooks.PreToolUse` → `compact-handoff-guard.py` (limit-wait の
     `--session-id` 注入と compact-loop の安全確認)
   - `hooks.UserPromptSubmit` → `compact-handoff-resume.py` (compact-loop)
   - `hooks.Stop` → `asyncRewake: true` と `timeout: 4000` を付けた
     `cache-keepalive.py` — もしくは `python tools/install_cache_keepalive.py`
     を実行するとちょうどそのエントリを書いてくれる (先に backup を取る。
     `--test` は 60 秒 idle 後に ping する試運転、`--remove` で登録解除)。
   - `env.CLAUDE_HOOK_USER_LANG` → `context-monitor.py` と
     `compact-handoff-guard.py` のターミナル向けの行を日本語にしたいなら
     `ja`。未設定または `en` なら英語になる。モデルが受け取るテキストは
     どちらの場合も英語。

3. **スナップショットが書き込まれているか確認する。** Claude Code を開いて
   何かツールを呼んだあと:
   ```
   python -c "import json,os; print(json.load(open(os.path.expanduser('~/.claude/usage-snapshot.json'),encoding='utf-8'))['parsed']['rate_limits'])"
   ```
   `five_hour` / `seven_day` エントリに `used_percentage` と `resets_at`
   (Unix epoch) が見えれば OK。

4. **ドライランで待機を試す** (リミットに達していなくても可):
   ```
   python ~/.claude/hooks/limit-wait.py --session-id test --simulate-seconds 75
   ```
   約75秒ブロックして JSON のステータス行を吐いて終了する。セッション内では
   `--session-id` を省略してよい — guard hook が注入する。

5. **keep-alive を試す**: `python tools/install_cache_keepalive.py --test`
   を実行し、turn を終えて1分待つ — ターミナルに `rewakeSummary` の行が
   表示され、モデルが `ok` と答える。その後 `--test` なしで再実行する。

6. **compact-loop** は上記2つの hook 以外に何も要らない。Skill は、
   context-monitor の ≥75 % アドバイザリが出た時、dead end に達した時、
   または phase break の時にモデル自身が呼び出す。handoff は
   `<project>/.work/compact-handoff/` に置かれる — project が repo なら、
   そのディレクトリを `.gitignore` に加えること。

## 作者の実運用版との違い

このリポジトリのファイルは、`tools/sync_from_local.py` が作者の実運用
`~/.claude/` コピーから生成したもので、private 指定のブロックを落とし、
短い文言置換のリストを適用する。それが取り除くもの:

- `context-monitor.py`: 作者自身の subagent workflow をエンコードした
  モデル固有の「delegation tip」セグメント (約250行)。それ以外 — usage
  の行・limit アドバイザリ・context 帯域・subagent watch — は同一。
- `compact-loop/SKILL.md`、`recovery.md`、`clear-mode.md`、
  `limit-wait/SKILL.md`: 作者の memory note や、このリポジトリに無い hook /
  skill (idle guard・他の待機プロセス・postmortem skill) へのポインタは
  削除するか、汎用的な言い回しに書き換えてある。baseline window は固定の
  数値ではなく「あなたの `--restore-value`」という表現にしてある。
- `compact-handoff-guard.py`: docstring 内の memory link を落としてある。

`tools/install_cache_keepalive.py` はこのリポジトリ限定。`rewakeSummary` は
ユーザー向けのターミナル行なので、自由にローカライズしてよい。

## これが「やらない」こと

- **チェックポイント機構ではない。** 会話が同じプロセスに載ったまま生き
  続けることが本質。Claude Code 自体が落ちれば (ターミナルを閉じた・
  マシン再起動・OS スリープ)、待機もろとも死ぬ — その場合は手動で続行する
  ことになる。
- **リミット回避手段ではない。** limit-wait は待機中にモデル推論を走らせる
  ことなく、自律的に、background で、ただそれを待つだけ。消費する quota の
  総量は変わらない。何もズルはしていない。
- **Anthropic の API レートリミット全般を対象にしたものではない** —
  `/usage` ダイアログに出る Claude Code サブスクリプションの 5h と 7d の
  セッション予算 windows を対象にしている。`oauth-usage-probe.py` は
  サブスクリプションログイン (`~/.claude/.credentials.json`) を必要とする。
  API-key setup でも待機プロセス自体は動くが、解除の検証はできない
  (`"verified": null`)。
- **無料ではない。** keep-alive は idle 1時間ごとに cache された1リクエスト
  を消費し、compact-loop は summary 1回分を消費する。どちらも節約できる量に
  比べれば小さいが、リクエストであることに変わりはない。

## 背景

これらのツールが前提にしている、Claude Code 2.1.258 時点での実測事実:

- hook エントリに `asyncRewake: true` を付けると、harness はその hook を
  background 実行し、exit code 2 で hook の stderr を system reminder として
  モデルを起こす (idle 中なら新しい turn、作業中なら次の tool 結果) — Stop
  フックと PostToolUse フックの両方で検証済み。エントリの `timeout` は
  background プロセスも kill するので、hook の最長待機時間より長くしておく
  必要がある。
- `<cwd>/.claude/settings.local.json` の `CLAUDE_CODE_AUTO_COMPACT_WINDOW`
  は動作中のプロセスに hot-reload され、次の推論ステップの前に
  window − 33000 (デフォルトの output reserve 込み) で auto-compact が発火
  する。ただし window が明示的な env / settings の値から来ている時に限る。
- 壁時計のリセット時刻はあくまで目安: `resets_at + 61秒` で起きたら
  「You've hit your session limit」と一度拒否されたことがある。そのため
  待機プロセスは終了前に `/api/oauth/usage` で検証し、Skill はバックアップの
  待機プロセスも起動する。
- transcript の最後の user/assistant レコードが idle 時計になる。Claude
  Code は idle 中に away-recap レコードを書き込むので、file mtime は idle
  時計にならない。

## License

MIT — `LICENSE` を参照。
