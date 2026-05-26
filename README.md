# claude-code-limit-wait

A small set of hooks + a Skill that let **Claude Code wait out its own
rate-limit reset inside the same session, with zero token consumption during
the wait and full context preserved on the other side.**

When the 5-hour (or 7-day) rate-limit creeps over a critical threshold, the
hook pushes Claude into the `limit-wait` Skill. The Skill launches a small
background poller that blocks until the binding limit's `resets_at` epoch has
passed. Because no model turns occur while the poller sleeps, the wait is
**structurally free** — no tokens, no money, no rate-limit budget spent. When
the poller exits, Claude Code re-invokes the agent with the conversation
fully loaded and it picks up exactly where it left off.

No checkpoint files. No "please paste this prompt again". No human babysitting
a long-running task across a quota reset.

## Why this exists

Claude Code's `/usage` dialog data is **not** routed to the model through any
of `/loop`, `Skill`, cron firing, or hooks. The only path that exposes it is
**statusLine**: Claude Code feeds the session JSON via stdin to whatever
script is configured there. Once that data lands on disk, you can do
interesting things with it — like deciding to *wait* for a reset rather than
stopping the session.

This repo packages the smallest set of pieces that get you there.

## Components

```
hooks/
  usage-probe-statusline.py    statusLine script. Captures the session JSON
                               into ~/.claude/usage-snapshot.json on every
                               refresh. Single-snapshot file (overwritten),
                               no log growth. Also prints a compact status
                               string back to the UI.

  context-monitor.py           PostToolUse hook. Reads the snapshot + the
                               assistant's transcript usage block, and emits
                               a one-line systemMessage of the form:
                                 ℹ️ Context: N tokens used | 5h XX% in … | 7d XX% in …
                               When 5h ≥90% or 7d ≥97% it APPENDS a ⚠️
                               advisory directing the model to invoke the
                               limit-wait skill immediately. Per-segment
                               dedup so rapid tool chains don't spam.

  limit-wait.py                The actual waiter. Reads the snapshot, picks
                               the binding limit (the one over its critical
                               threshold; if both, the LATER-resetting one),
                               and polls until target_epoch + buffer. Writes
                               live progress to ~/.claude/.limit-wait-state.json
                               (dict keyed by session_id so concurrent
                               sessions don't stomp each other). Final stdout
                               is one machine-readable JSON line.

skills/limit-wait/SKILL.md     The Skill manifest. When Claude sees the
                               hook's ⚠️ line, it invokes this Skill, which
                               tells it to launch limit-wait.py via Bash
                               with run_in_background:true and then idle.

examples/settings.json.example How to wire it all up in ~/.claude/settings.json
```

## How it fits together

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
                              systemMessage to model:
                              "ℹ️ Context: … | Limits: … | ⚠️ Invoke limit-wait NOW"
                                                  │
                                                  ▼
                                    Skill(name="limit-wait")
                                                  │
                                                  ▼
                              Bash run_in_background:true →  limit-wait.py
                                                  │
                                          (sleeps; 0 model turns)
                                                  │
                                          process exits
                                                  │
                                                  ▼
                                Claude Code re-invokes the agent
                                with full conversation context.
                                Original work resumes inline.
```

## Install

> Tested on Claude Code with Python 3.10+ available on PATH as `python`.
> Paths below assume a standard `~/.claude/` install.

1. **Copy the files** into your Claude Code config tree:
   ```
   hooks/*.py                  → ~/.claude/hooks/
   skills/limit-wait/SKILL.md  → ~/.claude/skills/limit-wait/SKILL.md
   ```

2. **Wire `~/.claude/settings.json`.** See `examples/settings.json.example`
   for the two blocks you need:
   - a `statusLine` entry that runs `usage-probe-statusline.py`
   - a `hooks.PostToolUse` matcher that runs `context-monitor.py`

3. **Verify the snapshot is being written.** Open Claude Code, make any
   tool call, then:
   ```
   python -c "import json,os; print(json.load(open(os.path.expanduser('~/.claude/usage-snapshot.json'),encoding='utf-8'))['parsed']['rate_limits'])"
   ```
   You should see `five_hour` / `seven_day` entries with `used_percentage` and
   `resets_at` (Unix epoch).

4. **Try a dry-run wait** (no real limit needed):
   ```
   python ~/.claude/hooks/limit-wait.py --simulate-seconds 75
   ```
   It will block for ~75 seconds and exit with a JSON status line.

5. **In a real session**, once 5h ≥90% or 7d ≥97%, the model will see the
   ⚠️ advisory on its next tool call and invoke the Skill automatically.

## Thresholds and tuning

| Constant     | File                  | Default | Meaning                                            |
| ------------ | --------------------- | ------- | -------------------------------------------------- |
| `H5_CRITICAL`| `context-monitor.py`, `limit-wait.py` | 90  | 5-hour `used_percentage` ≥ this fires the advisory |
| `D7_CRITICAL`| `context-monitor.py`, `limit-wait.py` | 97  | 7-day `used_percentage` ≥ this fires the advisory  |
| `--buffer`   | `limit-wait.py` CLI   | 60      | Seconds to sleep past `resets_at` before exiting   |
| `--max-wait` | `limit-wait.py` CLI   | 8 days  | Hard sanity cap; longer waits abort with exit 3    |
| `POLL_STEP`  | `limit-wait.py`       | 30      | Seconds between wall-clock re-checks while waiting |

If you tune `H5_CRITICAL` / `D7_CRITICAL`, change them in both files so the
hook fires the advisory at exactly the same point the waiter will agree to
act on it.

## Exit codes (limit-wait.py)

| Code | Status                                       | What the model should do                  |
| ---- | -------------------------------------------- | ----------------------------------------- |
| 0    | `reset_reached` / `already_reset` / `nothing_to_wait` | Continue the original work             |
| 3    | `abort_too_long` (wait > `--max-wait`)       | Fall back to notifying the user           |
| 4    | `error_no_snapshot`                          | Fall back to notifying the user           |

## What this is NOT

- **Not a checkpoint system.** The whole point is that the conversation
  stays loaded in the same process. If Claude Code itself dies (terminal
  closed, machine reboot, OS sleep), the wait dies with it — you'll
  continue manually.
- **Not a way to bypass rate limits.** It just waits for them, autonomously,
  in the background, with zero token consumption during the wait. Same
  amount of quota, no foul.
- **Not specific to Anthropic's API rate limits in general** — this targets
  the Claude Code subscription's 5h and 7d session-budget windows surfaced
  by the `/usage` dialog.

## Background

There's a longer write-up of what was tried and ruled out (Skill route, cron
firing of `/usage`, hook event payloads, CLI flags) that lives in the
project's `usage_probe_statusline.md` and `limit_wait_skill.md` design notes.
The short version: statusLine was the only mechanism that surfaced the
`/usage` dialog data to a process the model could spawn and read from.

## License

MIT — see `LICENSE`.
