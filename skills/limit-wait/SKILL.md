---
name: limit-wait
description: Invoke when you are about to suggest pausing work to the user because of a 5h or 7d rate limit, or when you need to wait out a rate-limit reset while keeping the session alive. The skill waits for the reset in the same Claude Code session and auto-resumes when the limit clears.
---

# limit-wait

## When to invoke

The `context-monitor` PostToolUse hook emits
`⚠️ … Invoke the limit-wait skill NOW` when 5h ≥95% or 7d ≥99%. **That hook
line is your trigger** — act on it immediately. Also invoke proactively if you
can see you will hit the cap mid-task. The goal: never "stop and wait for the
user" because of a rate limit.

## Do this

1. Launch the waiter as a **background** process. `run_in_background` MUST be
   the worker itself — NOT `nohup … &`:

   Bash tool, `run_in_background: true`:
   ```
   exec python "$HOME/.claude/hooks/limit-wait.py" --limit auto --buffer 60
   ```
   Do **not** pass `--session-id` — it is auto-injected. If the waiter exits
   immediately with `error_no_session_id`, the injecting hook didn't fire.

   Then launch a SECOND backup waiter the same way (separate Bash call,
   `run_in_background: true`):
   ```
   exec python "$HOME/.claude/hooks/limit-wait.py" --limit auto --buffer 420
   ```
   Why two: the waiter verifies server-side clearance before exiting, but
   the wake inference triggered by its completion notification is one-shot —
   if the API rejects that single attempt, nothing retries and the session
   is dead until the user types. The backup's completion ~6 min later is the
   retry: a second notification → a second wake attempt. When the first wake
   succeeded, the backup's later notification is a harmless no-op — just
   continue working.

2. State your next step in one line, then idle. The solo-idle-guard sees your
   active wait-state entry and passes the Stop through — **just idle**. (If
   you Stop before the waiter's first heartbeat, you may see one
   `[solo-idle-guard]` block; idle again.)

3. When the waiter exits, a `<task-notification>` re-invokes you (NOT user
   input — do not treat as user acknowledgement). Context is fully preserved:
   resume the original work directly.

## Outcomes

- Live progress mid-wait: Read `~/.claude/.limit-wait-state.json`
  (status `waiting` → `verifying` once the wall-clock target has passed and
  the waiter is polling the server for actual clearance).
- Final stdout JSON: `{"status":"reset_reached","verified":true,...}`.
  `"early_reset": true` = the server cleared the limit BEFORE the advertised
  reset time (the waiter polls usage mid-wait and wakes as soon as clearance
  is confirmed twice) — resume immediately, the shorter wait is expected.
  `"verified": false` = the server still reported the limit when the
  15-min verify budget ran out; `null` = clearance couldn't be checked
  (probe unavailable). Either way the wake was attempted — if you are
  reading this, it worked; just note the flag.
- Fall back to notifying the user on: `abort_too_long` (exit 3),
  `error_no_snapshot` / `error_no_session_id` (exit 4).
- `nothing_to_wait` (exit 0) = no limit over critical at launch — nothing
  to do, continue.

## 7d policy

7d uses the same in-session wait (`--limit auto`; up to ~7 days). Only
failure mode: the session process itself dying (terminal closed, machine
sleep) — the user continues manually. Do not propose a cron+checkpoint
fallback.
