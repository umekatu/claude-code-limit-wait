# compact-loop — recovery paths

Read this only when SKILL.md sends you here: the self-compact did not
fire, or a reset command must be submitted through the console input
buffer.

## Non-fire diagnosis (`/compact` mode)

You woke to `trigger_compact.py`'s completion notification and there is
NO compact summary above (you are still the pre-compact instance).
Diagnose in order — do not jump to re-running:

1. **Write path** — in `<cwd>/.claude/trigger_compact.log`, compare the
   `set CLAUDE_CODE_AUTO_COMPACT_WINDOW=...` line's settings path
   against your session's true project root (Step 1 output). A mismatch
   means the shrink landed in a `settings.local.json` the live process
   does not watch — re-run so the write lands at the true root.
2. **Liveness probe** — the script printed a one-run token and wrote it
   to env `COMPACT_LOOP_WATCHER_PROBE`. Read it back in a fresh Bash
   call: `echo "$COMPACT_LOOP_WATCHER_PROBE"`.
   - **Token matches** → the shrink reached the live process, so the
     context was simply below the fire point the script printed. Re-run
     with a lower `--window` (minimum 140000) if a reset is still
     needed; if context is below even that fire point, keep working to
     a later breakpoint instead.
   - **Empty / different value** → re-read once on your NEXT inference
     step (a too-early read can race the update). If it still doesn't
     match, **this session can no longer be reset by the window
     shrink** — re-running the script is pure waste. Instead, in order:
     1. Submit `/compact` via the console-input reset below.
     2. If that script reports itself unavailable, or the user is
        active at the keyboard, send a PushNotification asking them in
        plain language to type `/compact` when convenient. A user-typed
        `/compact` always works, the handoff file stays valid, and the
        resume hook re-injects its pointer afterwards.
     3. Add a task-list entry pointing at the handoff path — the task
        list survives a compact byte-for-byte, so the pointer reaches
        the post-compact instance no matter how the compact fires.
     4. Keep working if work remains, refreshing the handoff at each
        later breakpoint, until the compact lands. Idle only when no
        work remains.

The script prints the exact fire point for the window it set. Below
that point a self-compact cannot fire — plan breakpoints accordingly.

## Console-input reset (`inject_compact.py`)

Submits the reset command into the console input buffer — the buffer a
human's keystrokes land in:

```
python ~/.claude/skills/compact-loop/inject_compact.py
python ~/.claude/skills/compact-loop/inject_compact.py --clear
python ~/.claude/skills/compact-loop/inject_compact.py --dry-run
```

No argument submits `/compact`; `--clear` submits `/clear`. Those two
strings are the only things it can send. `--dry-run` reports the
console membership and the command, submitting nothing.

Full dead-watcher path live-verified 2026-08-04: probe mismatch on two
reads → this script launched via the PowerShell tool in background as
the turn's last call → `/compact` fired on the idle prompt, session_id
preserved, post-compact resume ran cleanly from the handoff.

**Launch it with the tool whose subprocesses share the CLI's console.**
The input buffer is per-console: the command reaches exactly the CLI
processes attached to the launching process's console. On a Windows CLI
started from PowerShell, the PowerShell tool shares that console and
the Bash tool does not — it gets a Git-bash console with no CLI
attached, where a submitted line is read by nobody. The script requires
exactly one CLI on its console and names the target pid in its output;
zero or several means it refuses. When unsure of a launcher, `--dry-run`
from it first.

Launch in the background as the LAST tool call of the turn, with
everything owed to the user said in the same message, then end the
turn — the default `--pre-sleep 10` waits for the turn to finish so the
command is submitted against an idle prompt.

**The user must not be at the keyboard — that check is yours, not the
script's.** The submitted line joins whatever text is already in the
prompt box, so injecting while someone types merges the two into one
submitted line. If the user has written in this session within the last
several minutes, ask them to type the command instead.
