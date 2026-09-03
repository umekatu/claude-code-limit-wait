# claude-code-limit-wait

*[日本語版 README はこちら](README_ja.md)*

Three small tools that keep a **Claude Code session alive and cheap across the
moments that normally end it** — all built from hooks, a Skill, and plain
Python scripts, with no model inference running during the waits:

| Tool | The moment it handles | What happens instead of stopping |
| --- | --- | --- |
| **limit-wait** | The 5-hour or 7-day rate limit is about to hit | A background poller waits for the reset (and verifies the server actually cleared it), then re-invokes the agent with the whole conversation still loaded |
| **compact-loop** | The context window is filling up | The model writes a handoff for its own successor, fires a real `/compact` on itself, and resumes from the handoff — no "please run /compact" |
| **cache-keepalive** | The session sits idle and the 1-hour prompt cache is about to expire | A background Stop hook wakes the model once every 55 minutes with a one-word reply, so the next real turn is still a cache hit |

Common thread: the harness itself does the waking. Background Bash processes
re-invoke the agent on exit, and `asyncRewake` hooks wake it with a system
reminder, so nothing depends on a human coming back to the keyboard.

## Why this exists

Claude Code's `/usage` data is **not** routed to the model through any of
`/loop`, `Skill`, cron firing, or hooks. The only path that exposes it is
**statusLine**: Claude Code feeds the session JSON via stdin to whatever
script is configured there. Once that data lands on disk, a hook can decide
to *wait* for a reset rather than stopping the session. The other two tools
grew out of the same pattern — the model deciding for itself when to reset
its context, and a hook keeping the cache warm while it idles.

## Components

```
hooks/
  usage-probe-statusline.py    statusLine script. Captures the session JSON
                               into ~/.claude/usage-snapshot.json on every
                               refresh (single file, overwritten, no log
                               growth) and prints a compact status string
                               back to the UI. Shows the model-scoped weekly
                               limit from the oauth cache when present.

  oauth-usage-probe.py         GET /api/oauth/usage with the token from
                               ~/.claude/.credentials.json (subscription
                               logins). Prints every rate-limit bucket the
                               account exposes; --refresh-cache writes
                               ~/.claude/.oauth-usage-cache.json for the
                               scripts that must never block on the network.
                               The token is only ever sent in the header.

  context-monitor.py           PostToolUse hook. Emits one line per tool
                               call when something changed:
                                 ℹ️ Context used: NN% | Limits used: 5h XX% in … (rsts …), 7d XX% in …
                               At 5h ≥95% or 7d ≥99% it appends a ⚠️
                               advisory that tells the model to invoke the
                               limit-wait Skill NOW. At 60 / 75 / 85 % of the
                               context window it appends a one-time band
                               advisory (value zone / steer to a breakpoint /
                               run compact-loop now). For a team leader it
                               also reports a subagent crossing 60/75/85/95 %.
                               The user-facing line is English unless
                               CLAUDE_HOOK_USER_LANG=ja is set (see Install);
                               the model-facing text is always English.

  limit-wait.py                The waiter. Reads the snapshot, picks the
                               binding limit (the one over its critical
                               threshold; if both, the LATER-resetting one),
                               sleeps to resets_at + buffer, then polls the
                               server until the bucket really reports clear.
                               Mid-wait it also watches for an early reset.
                               Live state: ~/.claude/.limit-wait-state.json
                               (keyed by session_id). Final stdout is one
                               JSON line.

  compact-handoff-guard.py     PreToolUse hook (CronCreate|Bash|PowerShell).
                               Injects --session-id into limit-wait.py
                               launches (the model never has to know it);
                               blocks a subagent from firing a compact on the
                               leader; blocks trigger_compact.py when no
                               fresh handoff exists; rewrites --cwd to the
                               session's real root. Fail-open. Its block
                               messages honour CLAUDE_HOOK_USER_LANG too.

  compact-handoff-resume.py    UserPromptSubmit hook. After a compact, the
                               next prompt gets a pointer to the handoff
                               file injected as additionalContext, so the
                               resume does not depend on the auto-summary.

  cache-keepalive.py           Stop hook (asyncRewake). Waits until the
                               session has been idle 55 min, then exits 2
                               with a one-line ping; the harness wakes the
                               model, it replies "ok", Stop fires again.

skills/limit-wait/SKILL.md     Launch limit-wait.py in the background and
                               idle; resume when the task notification lands.

skills/compact-loop/           SKILL.md (the 6-step cycle), clear-mode.md
                               (/clear variant), recovery.md (when the
                               compact does not fire), trigger_compact.py
                               (fires the auto-compact), inject_compact.py
                               (Windows-only console fallback).

scripts/compact-handoff/dump.py
                               Writes the handoff to
                               <cwd>/.work/compact-handoff/<session_id>.md.

tools/install_cache_keepalive.py
                               Registers / removes the keep-alive Stop hook
                               in ~/.claude/settings.json (backup first).
tools/sync_from_local.py       Maintainer tool: regenerates the repo copies
                               from ~/.claude/ (--check reports drift).

examples/settings.json.example All the wiring in one file.
```

### Files per tool

| Tool | Hooks | Skill / scripts | settings.json blocks |
| --- | --- | --- | --- |
| limit-wait | `usage-probe-statusline.py`, `oauth-usage-probe.py`, `context-monitor.py`, `limit-wait.py`, `compact-handoff-guard.py` | `skills/limit-wait/SKILL.md` | `statusLine`, `PostToolUse`, `PreToolUse` |
| compact-loop | `usage-probe-statusline.py`, `context-monitor.py` (the ≥75 % advisory), `compact-handoff-guard.py`, `compact-handoff-resume.py` | `skills/compact-loop/*`, `scripts/compact-handoff/dump.py` | `statusLine`, `PostToolUse`, `PreToolUse`, `UserPromptSubmit` |
| cache-keepalive | `cache-keepalive.py` | `tools/install_cache_keepalive.py` (optional) | `Stop` |

`usage-probe-statusline.py` is shared: its snapshot carries the session id,
cwd, rate limits and context-window size that the other scripts read.
`compact-handoff-guard.py` is shared by the first two: it injects
`--session-id` for the waiter and guards the compact trigger.

## 1. limit-wait

### How it fits together

```
 ┌──────────────────────────┐
 │ Claude Code TUI          │
 │  statusLine refresh ─────┼─► usage-probe-statusline.py
 └──────────────────────────┘                │
                                             ▼
                              ~/.claude/usage-snapshot.json
                                             │
       (every Bash/Edit/Write tool call) ────┴────┐
                                                  ▼
                                         context-monitor.py
                                                  │
                              to the model:
                              "ℹ️ Context used: … | Limits used: … | ⚠️ Invoke limit-wait NOW"
                                                  │
                                                  ▼
                                    Skill(name="limit-wait")
                                                  │
                                                  ▼
                              Bash run_in_background:true →  limit-wait.py
                               (compact-handoff-guard.py injects --session-id)
                                                  │
                                    sleeps to resets_at + buffer
                                    (0 model turns; early-reset watch every 60 min)
                                                  │
                                    polls /api/oauth/usage until the
                                    bucket reports clear (≤15 min)
                                                  │
                                          process exits
                                                  │
                                                  ▼
                                Claude Code re-invokes the agent
                                with full conversation context.
                                Original work resumes inline.
```

The Skill launches **two** waiters: a second one with a 7-minute buffer
serves as the retry, because the wake triggered by a completion notification
is one-shot — if the API rejects that single attempt, the backup's later
notification is the second attempt.

### Thresholds and tuning

| Constant       | File                                  | Default | Meaning                                                    |
| -------------- | ------------------------------------- | ------- | ---------------------------------------------------------- |
| `H5_CRITICAL`  | `context-monitor.py`, `limit-wait.py` | 95      | 5-hour `used_percentage` ≥ this fires the advisory         |
| `D7_CRITICAL`  | `context-monitor.py`, `limit-wait.py` | 99      | 7-day `used_percentage` ≥ this fires the advisory          |
| `--buffer`     | `limit-wait.py` CLI                   | 60      | Seconds to sleep past `resets_at` before verifying         |
| `--max-wait`   | `limit-wait.py` CLI                   | 8 days  | Hard sanity cap; longer waits abort with exit 3            |
| `--verify-max` | `limit-wait.py` CLI                   | 900     | Seconds to keep polling the server for clearance           |
| `POLL_STEP`    | `limit-wait.py`                       | 30      | Seconds between wall-clock re-checks while waiting         |
| `EARLY_CHECK_SEC` | `limit-wait.py`                    | 3600    | Interval of the mid-wait early-reset check                 |

If you tune `H5_CRITICAL` / `D7_CRITICAL`, change them in both files so the
hook fires the advisory at exactly the same point the waiter will agree to
act on it.

### Exit codes (limit-wait.py)

| Code | Status                                                 | What the model should do        |
| ---- | ------------------------------------------------------ | ------------------------------- |
| 0    | `reset_reached` / `nothing_to_wait`                    | Continue the original work      |
| 3    | `abort_too_long` (wait > `--max-wait`)                 | Fall back to notifying the user |
| 4    | `error_no_snapshot` / `error_no_session_id`            | Fall back to notifying the user |

The final JSON also carries `"verified"` (true / false / null when the probe
was unavailable) and `"early_reset": true` when the server cleared the limit
before the advertised time.

## 2. compact-loop

### Why

Auto-compact fires late and blind. This Skill lets the model reset itself at a
*clean breakpoint*, after writing a handoff for its successor, without asking
the user to type `/compact`.

### How it fits together

```
 context ≥75% (context-monitor advisory)  or  dead end  or  phase break
                          │
                          ▼
              Skill(name="compact-loop")
                          │
   Step 1  resolve session_id / cwd          dump.py --print-path-only
   Step 2  self-audit (ripple + documentation obligations)
   Step 3  write the handoff                 dump.py --topic … < body
           → <cwd>/.work/compact-handoff/<session_id>.md
   Step 4  consolidate (memory / commits / task list)
   Step 5  trigger the reset — LAST tool call of the turn, in the background:
           trigger_compact.py shrinks CLAUDE_CODE_AUTO_COMPACT_WINDOW in
           <cwd>/.claude/settings.local.json; the live process hot-reloads
           it and auto-compact fires before the next inference step; the
           script restores the value ~120 s later either way.
                          │
                          ▼
              /compact runs (session_id, task list, background tasks survive)
                          │
   Step 6  next prompt → compact-handoff-resume.py injects the handoff path
           → verify the env was restored → read the handoff → continue
```

`/clear` mode (`clear-mode.md`) does the same with a wake cron carrying the
handoff path, because `/clear` rotates the session_id. `recovery.md` covers
the case where the compact did not fire (a liveness probe tells you whether
the shrunk window reached the live process) and the console-input fallback
`inject_compact.py`, which types `/compact` into the CLI's own input buffer —
Windows only, and only while nobody is at the keyboard.

### Tuning (trigger_compact.py)

| Flag              | Default | Meaning                                                                                  |
| ----------------- | ------- | ---------------------------------------------------------------------------------------- |
| `--window`        | 200000  | Value written to `CLAUDE_CODE_AUTO_COMPACT_WINDOW`; auto-compact fires at window − 33000 tokens (default output reserve). Minimum 140000 |
| `--restore-value` | 900000  | Baseline written back after the compact. Set it to your model's real window: 900000 keeps 1M-window models from auto-compacting on their own; use 200000 on a 200K model |
| `--restore-after` | 120     | Seconds until the baseline is restored (a detached worker does it even if the session dies) |
| `--pre-sleep`     | 30      | Seconds before the shrink, so the turn that launched the script has ended               |
| `--content-value` | —       | One-line justification required when re-triggering within minutes of the last compact  |

The guard hook refuses `trigger_compact.py` and `inject_compact.py` from a
subagent (the process env is shared, so the shrink would compact the leader)
and when no fresh handoff exists for the session.

## 3. cache-keepalive

### Why

Claude Code keeps a 1-hour prompt cache on subscription logins. A cache hit
costs a fraction of a fresh read (0.1× the input price on most models, 0.025×
on Fable 5.1), and every request resets the hour. Once the session idles for
60 minutes, the next real turn re-reads the whole context at full price.

### How it fits together

```
 Stop (turn ends)
   │  every Stop spawns a fresh cache-keepalive.py; the newest one owns the
   │  session (pid lock ~/.claude/.cache-keepalive-<session_id>.pid), older
   │  instances exit at their next poll
   ▼
 poll every 30 s: idle = now − timestamp of the last user/assistant record
   │  (harness-only records such as the away recap do not count)
   ▼
 idle ≥ 3300 s → stderr: "just keeping this session's prompt cache warm …
                          Reply with the single word ok" → exit 2
   │
   ▼
 harness wakes the model (system reminder prefixed by rewakeMessage;
 the user's terminal shows rewakeSummary) → "ok" → Stop → repeat
```

An unattended run ends by itself: after 12 consecutive pings answered with
no tool call and no human input, the next instance exits silently (no
"ended" message that could send the model off doing something). The 6th
ping carries a one-time note that compacting first makes each refresh
cheaper. A real user prompt resets the count.

Cost: one ping is one request that reads the cached prefix and produces a
few tokens — on Fable 5.1 roughly 1/70 of the re-cache it prevents; with a
large context, compacting before a long idle makes each ping proportionally
cheaper.

### Tuning (cache-keepalive.py)

| Constant / flag           | Default | Meaning                                                          |
| ------------------------- | ------- | ---------------------------------------------------------------- |
| `--idle-seconds`          | 3300    | Idle time before a ping (55 min = 5 min under the 1 h TTL)       |
| `--poll`                  | 30      | Seconds between idle checks                                      |
| `MAX_CONSECUTIVE_PINGS`   | 12      | Unattended pings before the run stops silently (~11 h)           |
| `HINT_AT_PING`            | 6       | Which ping carries the one-time compaction note                  |
| `timeout` (settings.json) | 4000    | Must exceed `--idle-seconds`: the harness kills an async hook at its timeout, docs notwithstanding |

## Install

> Python 3.10+ on PATH as `python`. Paths below assume a standard `~/.claude/`
> install. On Windows, prefix hook commands with
> `PYTHONUTF8=1 PYTHONIOENCODING=utf-8 ` (see the example) so hook output
> survives a cp932 console.

1. **Copy the files** into your Claude Code config tree:
   ```
   hooks/*.py                       → ~/.claude/hooks/
   skills/limit-wait/SKILL.md       → ~/.claude/skills/limit-wait/SKILL.md
   skills/compact-loop/*            → ~/.claude/skills/compact-loop/
   scripts/compact-handoff/dump.py  → ~/.claude/scripts/compact-handoff/dump.py
   ```

2. **Wire `~/.claude/settings.json`.** `examples/settings.json.example` has
   every block; keep the ones for the tools you want:
   - `statusLine` → `usage-probe-statusline.py` (needed by all three: the
     snapshot carries session_id, cwd, rate limits, context window)
   - `hooks.PostToolUse` → `context-monitor.py`
   - `hooks.PreToolUse` → `compact-handoff-guard.py` (limit-wait's
     `--session-id` injection and compact-loop's safety checks)
   - `hooks.UserPromptSubmit` → `compact-handoff-resume.py` (compact-loop)
   - `hooks.Stop` → `cache-keepalive.py` with `asyncRewake: true` and
     `timeout: 4000` — or run `python tools/install_cache_keepalive.py`,
     which writes exactly that entry (backup taken first; `--test` pings
     after 60 s idle for a trial, `--remove` unregisters).
   - `env.CLAUDE_HOOK_USER_LANG` → `ja` if you want the terminal lines of
     `context-monitor.py` and `compact-handoff-guard.py` in Japanese.
     Unset or `en` gives English. The text the model receives is English
     either way.

3. **Verify the snapshot is being written.** Open Claude Code, make any
   tool call, then:
   ```
   python -c "import json,os; print(json.load(open(os.path.expanduser('~/.claude/usage-snapshot.json'),encoding='utf-8'))['parsed']['rate_limits'])"
   ```
   You should see `five_hour` / `seven_day` entries with `used_percentage`
   and `resets_at` (Unix epoch).

4. **Try a dry-run wait** (no real limit needed):
   ```
   python ~/.claude/hooks/limit-wait.py --session-id test --simulate-seconds 75
   ```
   It blocks for ~75 seconds and exits with a JSON status line. Inside a
   session, omit `--session-id`: the guard hook injects it.

5. **Try the keep-alive**: `python tools/install_cache_keepalive.py --test`,
   end a turn, wait a minute — the terminal shows the `rewakeSummary` line
   and the model answers `ok`. Then re-run without `--test`.

6. **compact-loop** needs nothing beyond the two hooks. The Skill is
   invoked by the model when context-monitor's ≥75 % advisory appears, at a
   dead end, or at a phase break. The handoff lands in
   `<project>/.work/compact-handoff/` — add that directory to your
   `.gitignore` if the project is a repo.

## Differences from the author's install

The repo files are generated from the author's live `~/.claude/` copies by
`tools/sync_from_local.py`, which drops blocks marked private and applies a
short list of wording substitutions. What that removes:

- `context-monitor.py`: a model-specific "delegation tip" segment that
  encodes the author's own subagent workflow (about 250 lines). Everything
  else — the usage line, the limit advisory, the context bands, the subagent
  watch — is identical.
- `compact-loop/SKILL.md`, `recovery.md`, `clear-mode.md`,
  `limit-wait/SKILL.md`: pointers to the author's memory notes and to hooks
  or skills that are not in this repo (an idle guard, other waiters, a
  postmortem skill) are dropped or worded generically, and the baseline
  window is described as "your `--restore-value`" instead of a fixed
  number.
- `compact-handoff-guard.py`: the docstring's memory links are dropped.

`tools/install_cache_keepalive.py` is repo-only; its `rewakeSummary` is the
user-facing terminal line, localize it freely.

## What this is NOT

- **Not a checkpoint system.** The whole point is that the conversation
  stays loaded in the same process. If Claude Code itself dies (terminal
  closed, machine reboot, OS sleep), the wait dies with it — you'll
  continue manually.
- **Not a way to bypass rate limits.** limit-wait just waits for them,
  autonomously, in the background, without running model inference during
  the wait itself. Same amount of quota, no foul.
- **Not specific to Anthropic's API rate limits in general** — this targets
  the Claude Code subscription's 5h and 7d session-budget windows surfaced
  by the `/usage` dialog. `oauth-usage-probe.py` needs a subscription login
  (`~/.claude/.credentials.json`); on an API-key setup the waiter still
  works, it just cannot verify clearance (`"verified": null`).
- **Not free.** The keep-alive spends one cached request per hour of idling;
  compact-loop spends one summary. Both are small next to what they save,
  but they are requests.

## Background

Measured facts these tools rest on, as of Claude Code 2.1.258:

- `asyncRewake: true` on a hook entry makes the harness background the hook
  and, on exit code 2, wake the model with the hook's stderr as a system
  reminder (a new turn when idle, the next tool result when working) —
  verified for Stop and PostToolUse hooks. The entry's `timeout` kills the
  background process too, so it must exceed the hook's longest wait.
- `CLAUDE_CODE_AUTO_COMPACT_WINDOW` in `<cwd>/.claude/settings.local.json`
  hot-reloads into the live process; auto-compact fires at window − 33000
  (with the default output reserve) before the next inference step, and only
  when the window comes from an explicit env/settings value.
- A wall-clock reset time is advisory: waking at `resets_at + 61 s` was once
  rejected with "You've hit your session limit", which is why the waiter
  verifies against `/api/oauth/usage` before exiting, and why the Skill
  launches a backup waiter.
- The transcript's last user/assistant record is the idle clock; Claude Code
  writes an away-recap record during idle time, so file mtime is not.

## License

MIT — see `LICENSE`.
