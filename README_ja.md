# claude-code-limit-wait

**Claude Code が自分のレートリミット (5時間 / 7日) のリセットを、同じセッションのまま、待機中にモデル推論を走らせずに待ち抜けるようにする** ためのフック群と Skill。

5時間ウィンドウ (または7日ウィンドウ) の使用率がクリティカル閾値を超えると、フックが Claude を `limit-wait` Skill に押し込む。Skill は小さなバックグラウンドポーラーを起動し、有効なリミットの `resets_at` (Unix epoch) を過ぎるまでブロックする。ポーラーは単なる `time.sleep` ループなので、待機中はモデルターンが一切発生しない — **待機ウィンドウそのものではトークンもリミット枠も消費しない**。(リセット後に作業を再開するときの通常コストは、他のツール結果と同様に発生する。) ポーラーが終了すると Claude Code がエージェントを再呼び出しし、会話コンテキストが丸ごと残ったまま中断点から続行する。

チェックポイントファイルは不要。「同じプロンプトを貼り直して」も不要。長時間タスクをリセット跨ぎで人間が見守る必要もない。

## なぜこれを作ったか

Claude Code の `/usage` ダイアログのデータは、`/loop`・`Skill`・cron 起動・hooks のどれを経由してもモデルに届かない。唯一データが露出する経路は **statusLine** — Claude Code が statusLine 用に設定されたスクリプトに stdin 経由でセッション JSON を流し込んでくる。それさえディスクに落ちれば、リセットを待つかどうかをモデル側で判断できる。

このリポジトリは、その仕組みを最小構成でパッケージしたもの。

## 構成要素

```
hooks/
  usage-probe-statusline.py    statusLine スクリプト。リフレッシュ毎にセッション
                               JSON を ~/.claude/usage-snapshot.json に保存
                               (上書き、ログは伸びない)。UI には簡潔な
                               ステータス文字列を返す。

  context-monitor.py           PostToolUse フック。スナップショットと
                               アシスタント transcript の usage ブロックを
                               読み、次の形の systemMessage を出す:
                                 ℹ️ Context: N tokens used | 5h XX% in … | 7d XX% in …
                               5h≥90% または 7d≥97% のとき、limit-wait
                               Skill を即座に呼び出せという ⚠️ アドバイザリ
                               を末尾に追記する。セグメント単位の dedup で
                               連続ツール呼び出しでもスパム化しない。

  limit-wait.py                実際の待機プロセス。スナップショットを読み、
                               クリティカル閾値を超えているリミット (両方
                               なら遅い方が拘束的) を選び、target_epoch +
                               buffer までポーリングする。進捗は
                               ~/.claude/.limit-wait-state.json に書く
                               (session_id をキーにした dict なので、複数
                               セッションが並行しても上書きし合わない)。
                               最終 stdout は機械可読な JSON 1行。

skills/limit-wait/SKILL.md     Skill マニフェスト。Claude がフックの ⚠️
                               行を見ると、この Skill を起動する。Skill は
                               「Bash の run_in_background:true で
                               limit-wait.py を立ち上げて idle しろ」と
                               モデルに指示する。

examples/settings.json.example ~/.claude/settings.json への配線テンプレ。
```

## 連動の流れ

```
 ┌──────────────────────────┐
 │ Claude Code TUI          │
 │  statusLine refresh ─────┼─► usage-probe-statusline.py
 └──────────────────────────┘                │
                                             ▼
                              ~/.claude/usage-snapshot.json
                                             │
       (Edit/Write/Bash の毎ツール呼び出し) ──┴────┐
                                                  ▼
                                         context-monitor.py
                                                  │
                              モデル宛 systemMessage:
                              "ℹ️ Context: … | Limits: … | ⚠️ Invoke limit-wait NOW"
                                                  │
                                                  ▼
                                    Skill(name="limit-wait")
                                                  │
                                                  ▼
                              Bash run_in_background:true → limit-wait.py
                                                  │
                                          (スリープ中、モデルターン 0)
                                                  │
                                          プロセス終了
                                                  │
                                                  ▼
                                Claude Code がエージェントを再呼び出し
                                会話コンテキストは完全保持。中断点から続行。
```

## インストール

> Claude Code 上で動作確認。PATH に `python` (3.10+) が必要。
> 以下のパスは標準の `~/.claude/` 配置を前提。

1. **ファイルをコピー** して Claude Code の設定ツリーに置く:
   ```
   hooks/*.py                  → ~/.claude/hooks/
   skills/limit-wait/SKILL.md  → ~/.claude/skills/limit-wait/SKILL.md
   ```

2. **`~/.claude/settings.json` に配線**。必要なのは2ブロック (詳細は `examples/settings.json.example`):
   - `usage-probe-statusline.py` を実行する `statusLine` エントリ
   - `context-monitor.py` を実行する `hooks.PostToolUse` マッチャ

3. **スナップショットが書き込まれているか確認**。Claude Code を開いて何かツールを呼んだあと:
   ```
   python -c "import json,os; print(json.load(open(os.path.expanduser('~/.claude/usage-snapshot.json'),encoding='utf-8'))['parsed']['rate_limits'])"
   ```
   `five_hour` / `seven_day` エントリに `used_percentage` と `resets_at` (Unix epoch) が見えれば OK。

4. **ドライランで待機を試す** (リミットに達していなくても可):
   ```
   python ~/.claude/hooks/limit-wait.py --simulate-seconds 75
   ```
   75秒ブロックして JSON 1行を吐いて終了する。

5. **実セッションでは**、5h≥90% または 7d≥97% に達した直後のツール呼び出しでモデルが ⚠️ アドバイザリを見て、自動的に Skill を呼ぶ。

## 閾値とチューニング

| 定数         | 場所                  | デフォルト | 意味                                                |
| ------------ | --------------------- | ---------- | --------------------------------------------------- |
| `H5_CRITICAL`| `context-monitor.py`, `limit-wait.py` | 90  | 5h `used_percentage` ≥ これでアドバイザリ発火        |
| `D7_CRITICAL`| `context-monitor.py`, `limit-wait.py` | 97  | 7d `used_percentage` ≥ これでアドバイザリ発火        |
| `--buffer`   | `limit-wait.py` CLI   | 60         | `resets_at` を過ぎてから余分に寝る秒数              |
| `--max-wait` | `limit-wait.py` CLI   | 8 days     | サニティ上限。これを超える待機は exit 3 で中止       |
| `POLL_STEP`  | `limit-wait.py`       | 30         | 待機中の壁時計再チェック間隔 (秒)                   |

`H5_CRITICAL` / `D7_CRITICAL` を変える場合は両ファイルで揃えること。フックがアドバイザリを出すラインと、ウェイターが「待つに値する」と判断するラインを一致させるため。

## 終了コード (limit-wait.py)

| Code | Status                                       | モデルがとるべき動作                       |
| ---- | -------------------------------------------- | ------------------------------------------ |
| 0    | `reset_reached` / `already_reset` / `nothing_to_wait` | 元の作業を続行                          |
| 3    | `abort_too_long` (待機が `--max-wait` 超過)  | ユーザーに通知する側にフォールバック       |
| 4    | `error_no_snapshot`                          | ユーザーに通知する側にフォールバック       |

## これが「やらない」こと

- **チェックポイント機構ではない**。会話を同じプロセスに居続けさせるのが本質。Claude Code 自体が落ちる (ターミナルを閉じた・マシンを再起動した・OS スリープ) と待機ごと死ぬ — その場合は手動で続行することになる。
- **リミット回避ツールではない**。自律的に・バックグラウンドで・待機中にモデル推論を走らせずに待つだけ。消費する枠の総量は変わらない。
- **Anthropic API 全般のレートリミット向けではない** — Claude Code サブスクの 5h / 7d セッション予算 (`/usage` ダイアログで露出している方) を対象にしている。

## 背景

何を試して何がダメだったか (Skill ルート、`/usage` の cron 起動、フックイベントのペイロード、CLI フラグ) はプロジェクト内の `usage_probe_statusline.md` と `limit_wait_skill.md` 設計ノートに残っている。要点: `/usage` ダイアログのデータをモデルが spawn・read できるプロセスに流す唯一の経路が statusLine だった。

## ライセンス

MIT — `LICENSE` を参照。
