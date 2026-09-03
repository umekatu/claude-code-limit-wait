#!/usr/bin/env python3
"""One-shot self-compact trigger for the compact-loop skill.

Shrinks CLAUDE_CODE_AUTO_COMPACT_WINDOW in <cwd>/.claude/settings.local.json.
The live Claude Code process hot-reloads settings env within seconds, and
auto-compact fires before the model's next inference step once the TOTAL
context tokens (same basis as the status-line "Context used" count) exceed
the threshold.

Threshold model (CC v2.1.220, constants version-dependent — re-verify from
the binary after a CC upgrade):
  - Live (reactive) path threshold:
      threshold = window - min(max_output_tokens, 20000) - 13000
    = window - 33000 with default output reserve. Default window 200000
    -> fires at ~167k TOTAL context (live-verified 2026-07-26); window
    140000 -> fires at ~107k (headless probe, same day).
  - The reactive path runs only when the window comes from an EXPLICIT
    env/settings value; sessions without one get no reactive auto-compact.
  - --window < 140000 is refused: its threshold would sit at or below the
    ~90k post-compact floor, so every compact would immediately re-fire.
  - A second, statsig-gated PRECOMPUTE path (default OFF) uses
    min(usable - round(usable*0.2), usable - 13000) with
    usable = window - min(max_output_tokens, 20000) and skips explicit
    windows under 200000. It does not govern default live behavior.
Settings-env hot-reload normally lands within seconds, but the process's
settings watcher can DIE in long sessions (observed 2026-07-26: 11h
session, three shrink cycles, env never updated, zero fires; again
2026-08-02: 10.5h session, two cycles, same signature). To make that
diagnosable in one step, the shrink write also sets a per-run probe
token in env COMPACT_LOOP_WATCHER_PROBE (echoed in stdout). On a
non-fire wake, read the var from a fresh subprocess: token present =
watcher alive (non-fire means context below threshold); token absent
after one confirming re-read = watcher dead — the window shrink can no
longer reach this process; do not re-run. Fall back per recovery.md
(beside this script): the console-input reset does not depend on
hot-reload.

Before returning, the script spawns a DETACHED worker process that restores
the baseline value after --restore-after seconds. The restore therefore does
not depend on the post-compact instance (nor on the compaction firing at all
— if the threshold is never crossed, the shrink is silently undone). The
baseline 900000 doubles as a dead-watcher safety ceiling: the value loaded
at session START governs the process for its whole life (the threshold env
is read fresh from process.env each check), so a session whose watcher dies
still auto-compacts at ~867k TOTAL context.

Deleting the env key from the settings file does NOT unset it in the live
process (hot-reload is add/overwrite only), which is why restore is an
OVERWRITE to a large value, never a delete.

Two launch modes:

BACKGROUND dead-man (default — bare invocation, pre-sleep 30 /
post-sleep 20): launch via background Bash as the last tool call, say
the final words, end the turn. The script sleeps --pre-sleep so the
turn is over before the shrink lands, writes the shrink, then sleeps
--post-sleep so its own completion notification reaches the session
AFTER the hot-reload has landed. The auto-compact check runs before ANY
inference step — including one woken by a background-task completion
notification (live-verified 2026-07-05) — so the notification itself
fires the compact even if the session is fully idle. Dead-man wake: no
reliance on the model taking another step.

FOREGROUND (--pre-sleep 0 --post-sleep 0): the tool-result inference
step right after this script returns is when the compact fires. Works
only if the model takes that step while the shrink is live — an idle
turn kills the trigger.

Usage:
  python trigger_compact.py                          # background dead-man
                                                     # (defaults: pre-sleep 30,
                                                     #  post-sleep 20)
  python trigger_compact.py --pre-sleep 0 --post-sleep 0   # immediate foreground
  python trigger_compact.py --window 130000          # higher threshold
  python trigger_compact.py --restore-after 180      # longer safety window
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

KEY = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
PROBE_KEY = "COMPACT_LOOP_WATCHER_PROBE"


def load_settings(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def set_env(settings_path: Path, updates: dict) -> None:
    data = load_settings(settings_path)
    env = data.setdefault("env", {})
    env.update(updates)
    save_settings(settings_path, data)


def set_window(settings_path: Path, value: int) -> None:
    set_env(settings_path, {KEY: str(value)})


def log_line(settings_path: Path, msg: str) -> None:
    try:
        log = settings_path.parent / "trigger_compact.log"
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except OSError:
        pass


def restore_worker(args: argparse.Namespace) -> None:
    settings_path = Path(args.settings)
    time.sleep(args.restore_after)
    set_window(settings_path, args.restore_value)
    log_line(settings_path, f"restored {KEY}={args.restore_value} "
                            f"(after {args.restore_after}s)")


def main() -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, OSError):
        pass
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--window", type=int, default=200000,
                   help="temporary window; auto-compact fires when TOTAL"
                        " context tokens >= window - 33000."
                        " Values below 140000 are refused (threshold"
                        " would fall under the ~90k post-compact floor"
                        " -> immediate re-fire loop)")
    p.add_argument("--restore-after", type=int, default=120,
                   help="seconds until the detached worker restores the value")
    p.add_argument("--restore-value", type=int, default=900000,
                   help="baseline value the worker writes back. 900000 is"
                        " deliberate: a session that STARTS with it has a"
                        " watcher-independent auto-compact ceiling at"
                        " ~867k TOTAL context (window - 33000), read fresh"
                        " from process.env each check — so even a session"
                        " whose settings watcher later dies still compacts"
                        " autonomously instead of stalling")
    p.add_argument("--cwd", default=None,
                   help="project cwd whose .claude/settings.local.json to"
                        " edit. Normally injected automatically by"
                        " compact-handoff-guard.py's PreToolUse guard; if"
                        " omitted here, this script falls back to the"
                        " current working directory and prints a warning")
    p.add_argument("--pre-sleep", type=int, default=30,
                   help="seconds to sleep BEFORE writing the shrink"
                        " (lets the launching turn end first; 0 = immediate)")
    p.add_argument("--post-sleep", type=int, default=20,
                   help="seconds to sleep AFTER writing the shrink before"
                        " exiting (the completion notification then arrives"
                        " after hot-reload and fires the compact; 0 = none)")
    p.add_argument("--restore-worker", action="store_true",
                   help=argparse.SUPPRESS)
    p.add_argument("--settings", help=argparse.SUPPRESS)
    p.add_argument("--content-value", default="",
                   help="one-line justification for a rapid-repeat trigger"
                        " with no commit since the prior one (>=10 chars)."
                        " Only logged, never validated for truth by this"
                        " script — the compact-handoff-guard.py PreToolUse"
                        " hook is what requires/checks it before allowing"
                        " this invocation through.")
    args = p.parse_args()

    if args.cwd is None:
        args.cwd = os.getcwd()
        if not args.restore_worker:
            print(f"WARNING: --cwd not supplied; defaulting to the current "
                  f"working directory ({args.cwd}). compact-handoff-guard.py "
                  f"normally injects --cwd automatically — verify {args.cwd} "
                  f"is this session's true project root before trusting a "
                  f"subsequent non-fire diagnosis.", flush=True)

    if args.restore_worker:
        restore_worker(args)
        return 0

    if args.window < 140000:
        print(f"REFUSED: --window {args.window} is below 140000. The "
              f"auto-compact threshold is window - 33000 (CC v2.1.220 "
              f"reactive path); below 140000 it falls to the ~90k "
              f"post-compact floor and every compact immediately "
              f"re-fires. Use --window >= 140000 (default 200000 fires "
              f"at ~167k TOTAL context).")
        return 1

    settings_path = Path(args.cwd) / ".claude" / "settings.local.json"

    if args.pre_sleep > 0:
        print(f"pre-sleep {args.pre_sleep}s before shrinking the window "
              f"(end the launching turn now: the shrink must land while "
              f"the session is idle)", flush=True)
        time.sleep(args.pre_sleep)

    probe_token = datetime.now().strftime("%Y%m%dT%H%M%S")
    set_env(settings_path, {KEY: str(args.window), PROBE_KEY: probe_token})
    cv_note = f"; content-value: {args.content_value}" if args.content_value else ""
    log_line(settings_path, f"set {KEY}={args.window} "
                            f"(restore {args.restore_value} in "
                            f"{args.restore_after}s; pre-sleep "
                            f"{args.pre_sleep}s, post-sleep "
                            f"{args.post_sleep}s; probe {probe_token}{cv_note})")

    flags = (subprocess.DETACHED_PROCESS
             | subprocess.CREATE_NEW_PROCESS_GROUP
             | subprocess.CREATE_NO_WINDOW) if os.name == "nt" else 0
    worker = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()),
         "--restore-worker",
         "--settings", str(settings_path),
         "--restore-after", str(args.restore_after),
         "--restore-value", str(args.restore_value)],
        creationflags=flags,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=(os.name != "nt"),
    )

    threshold = args.window - 33000
    print(f"{KEY}={args.window} set in {settings_path}", flush=True)
    print(f"watcher-liveness probe token: {probe_token} "
          f"(written to env {PROBE_KEY} in the same hot-reload)", flush=True)
    print(f"auto-compact threshold now ~{threshold} TOTAL context tokens "
          f"(v2.1.220 reactive model: window - 33000); "
          f"fires before the next inference step if exceeded", flush=True)
    print(f"detached restore worker pid={worker.pid}: writes back "
          f"{args.restore_value} in {args.restore_after}s "
          f"(log: {settings_path.parent / 'trigger_compact.log'})", flush=True)

    if args.post_sleep > 0:
        time.sleep(args.post_sleep)
        recovery_md = Path(__file__).resolve().parent / "recovery.md"
        print(f"post-sleep {args.post_sleep}s done. If you are reading this "
              f"WITHOUT a compact summary above your context, the compact "
              f"did NOT fire on this notification. Diagnose (in order):\n"
              f"1. In trigger_compact.log, compare the `set` line's settings "
              f"path against your session's true project root — a mismatch "
              f"means the shrink landed in a file the live process does not "
              f"watch; re-run with the correct --cwd.\n"
              f"2. Probe watcher liveness in a fresh Bash call: "
              f"echo \"${PROBE_KEY}\"\n"
              f"   - Prints {probe_token}: hot-reload is ALIVE; the non-fire "
              f"means TOTAL context was below ~{threshold} tokens. If a "
              f"reset is still needed and context exceeds ~107k, re-run "
              f"with a lower --window (>= 140000); below that a "
              f"self-compact cannot fire.\n"
              f"   - Empty or different: re-read once on your NEXT inference "
              f"step (a too-early read can race the hot-reload); still not "
              f"{probe_token} = this process's settings watcher is DEAD and "
              f"no settings write can reach it — do NOT re-run this script. "
              f"The console-input reset route does not depend on hot-reload: "
              f"read {recovery_md} from disk and follow its dead-watcher "
              f"steps. If the skill copy in your context lacks that file, "
              f"the copy is stale — the file on disk is the procedure.",
              flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
