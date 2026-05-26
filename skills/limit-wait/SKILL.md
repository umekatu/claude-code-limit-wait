---
name: limit-wait
description: Wait out a 5h or 7d rate-limit reset in the SAME session and auto-resume with full context preserved (no checkpoint). Invoke when the context-monitor hook's ⚠️ rate-limit advisory tells you to, or whenever you are about to hit the 5h/7d cost limit and would otherwise have to stop and wait for the user. Never stop-and-wait-for-user for a rate limit — wait it out in-session.
---

# limit-wait

## When to invoke

The `context-monitor` PostToolUse hook emits
`⚠️ … Invoke the limit-wait skill NOW` when 5h ≥90% or 7d ≥97%. **That hook
line is your trigger** — act on it immediately. Also invoke proactively if you
can see you will hit the cap mid-task. The goal: never "stop and wait for the
user" because of a rate limit.

## Do this

1. Launch the waiter as a **background** process. The `run_in_background`
   command MUST be the worker itself — NOT `nohup … &` (that detaches the
   worker, the shell exits instantly, the harness re-invokes immediately, and
   the real waiter becomes an untracked orphan with no re-invoke):

   Bash tool, `run_in_background: true`:
   ```
   exec python "$HOME/.claude/hooks/limit-wait.py" --limit auto --buffer 60
   ```
2. State your next step in one line, then idle. The solo idle-guard
   (`team-all-idle-detector.py`) will block the first Stop
   ("stop again to idle") — **just idle again, take NO other action**. The
   background completion is the continuation, independent of idle.
3. When the waiter exits it re-invokes you via a `<task-notification>` system
   reminder (NOT user input — do not treat as user acknowledgement). Context
   is fully preserved: resume the original work directly. No checkpoint, no
   re-read.

## Why it works

Background Bash re-invokes the agent with full context on process exit; zero
model turns occur during the wait ⇒ **zero token / zero rate-limit
consumption while waiting**. `--limit auto` only acts when a limit is actually
over its critical threshold (H5≥90 / D7≥97) and waits on the later-resetting
limit if both. Snapshot source: `~/.claude/usage-snapshot.json` (written by
`usage-probe-statusline.py`; `resets_at` = Unix epoch).

## Introspection / outcomes

- Live progress mid-wait: Read `~/.claude/.limit-wait-state.json`.
- Final stdout JSON in the task `.output`: `{"status":"reset_reached",...}`.
- Fall back to notifying the user instead of waiting on:
  `abort_too_long` (exit 3), `error_no_snapshot` (exit 4). `already_reset` /
  `nothing_to_wait` (exit 0) = nothing to do, just continue.

## 7d policy (user decision 2026-05-19)

7d uses the exact same in-session wait (covered by `--limit auto`; up to ~7
days; `--max-wait` 8-day cap accommodates it). Only failure mode: the Claude
Code **session process itself dying** (terminal closed, machine reboot/sleep)
— then the user continues manually (explicitly accepted). The cron+checkpoint
fallback was considered and dropped 2026-05-19 — do not re-propose it.

## Proven

Real 5h-scale E2E: launch→re-invoke real elapsed 16692s (~4.64h), date rolled
05-18→19, context fully preserved, 5h `resets_at` genuinely rolled, tokens
consumed during the wait = 0.
