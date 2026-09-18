#!/usr/bin/env python3
"""Submit this session's own reset command into the console input buffer
— the same buffer a human's keystrokes land in.

WHAT THIS CAN SEND: the literal string `/compact`, or with --clear, the
literal string `/clear`. Nothing else — both are constants in this file
and no caller-supplied text is ever submitted.

WHERE IT LANDS: the console input buffer of the CLI process that runs
this session. The CLI exports its own pid to every tool subprocess as
`CLAUDE_PID` (and the session id as `CLAUDE_CODE_SESSION_ID`); the
script verifies that pid is a Claude CLI image, cross-checks
`~/.claude/sessions/<pid>.json` against the session id, then detaches
from its own console and attaches to the CLI's (`FreeConsole` +
`AttachConsole`, the sequence the claude-restart skill submits `/exit`
with). The launcher's console therefore does not matter: the Bash tool
and the PowerShell tool both work, on either a Windows Terminal tab or a
plain console. Without `CLAUDE_PID` in the environment the script falls
back to the CLI processes on its own console and requires exactly one.
Run --dry-run first when unsure: it resolves and attaches without
submitting.

WHY IT EXISTS: the window-shrink trigger (`trigger_compact.py`) reaches
a session through settings-env hot-reload, which can stop working
mid-session; console input does not depend on it. `/clear` additionally
has no environment-variable route at all, so this is its only
autonomous path.

WHAT IT DOES NOT DO: carry the handoff to the next session. `/compact`
keeps the session_id, so the post-compact instance reads the handoff at
its known path (and `compact-handoff-resume.py` injects the pointer on
the next user prompt). `/clear` rotates the session_id, so its wake
prompt rides the plain-text cron scheduled in `clear-mode.md` Step 5 —
schedule that BEFORE submitting `/clear` here.

Before submitting it reads the CLI's prompt box off the console (the
claude-restart skill's `prompt_box`: the row holding the console cursor)
and refuses, exit 3, when typed text sits there — the submitted line
would join that text into one submitted line. The CLI's greyed prompt
suggestion is not typed text (caret at the start) and does not refuse.
A human merely being near the keyboard is therefore not a reason to
skip this script; a refusal is the moment to ask them to type the
command instead.

Launch as the LAST tool call of the turn, in the background, then end
the turn: the default --pre-sleep 10 lets the turn finish so the
command is submitted against an idle prompt.

Usage:
  python inject_compact.py
  python inject_compact.py --clear
  python inject_compact.py --dry-run
"""
import argparse
import ctypes
import os
import re
import sys
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

COMPACT_COMMAND = "/compact"
CLEAR_COMMAND = "/clear"
# The native-install auto-updater renames the RUNNING binary to
# claude.exe.old.<ms-epoch> and installs the new claude.exe beside it, so a
# session started before an update keeps serving from the renamed image
# (observed 2026-08-08: pid 40708 running claude.exe.old.1785802690843).
# QueryFullProcessImageNameW returns the renamed basename for such a
# process — the matcher must accept it or a reachable CLI is invisible
# and the script refuses a working injection.
CLI_IMAGE_RE = re.compile(r"^claude(\.exe)?(\.old(\.\d+)?)?$", re.IGNORECASE)

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
KEY_EVENT = 0x0001
VK_RETURN = 0x0D
INVALID_HANDLE = wintypes.HANDLE(-1).value
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class _CharUnion(ctypes.Union):
    _fields_ = [("UnicodeChar", wintypes.WCHAR),
                ("AsciiChar", ctypes.c_char)]


class _KeyEvent(ctypes.Structure):
    _fields_ = [("bKeyDown", wintypes.BOOL),
                ("wRepeatCount", wintypes.WORD),
                ("wVirtualKeyCode", wintypes.WORD),
                ("wVirtualScanCode", wintypes.WORD),
                ("uChar", _CharUnion),
                ("dwControlKeyState", wintypes.DWORD)]


class _EventUnion(ctypes.Union):
    _fields_ = [("KeyEvent", _KeyEvent)]


class _InputRecord(ctypes.Structure):
    _fields_ = [("EventType", wintypes.WORD), ("Event", _EventUnion)]


def log_line(cwd: str, msg: str) -> None:
    try:
        log = Path(cwd) / ".claude" / "trigger_compact.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except OSError:
        pass


# /compact is accepted only within this window after a trigger_compact.py
# attempt in this project (its log line is the proof); the compact-loop skill
# owns the order of steps, so the refusal points back to it.
TRIGGER_WINDOW_SECONDS = 600
TRIGGER_ATTEMPT_RX = re.compile(
    r"^(\S+) (?:set CLAUDE_CODE_AUTO_COMPACT_WINDOW=|update pending )")


def last_trigger_attempt(cwd: str):
    """Seconds since trigger_compact.py last shrank the window or reported a
    pending update in this project (its lines in .claude/trigger_compact.log);
    None when the log has no such line."""
    log = Path(cwd) / ".claude" / "trigger_compact.log"
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        m = TRIGGER_ATTEMPT_RX.match(line)
        if m:
            try:
                return (datetime.now() - datetime.fromisoformat(m.group(1))).total_seconds()
            except ValueError:
                return None
    return None


def _image_path(k32, pid: int) -> str:
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if not k32.QueryFullProcessImageNameW(handle, 0, buf,
                                              ctypes.byref(size)):
            return ""
        return buf.value
    finally:
        k32.CloseHandle(handle)


def console_members(k32):
    """(all pids on this console, [(pid, image path) for CLI processes])"""
    buf = (wintypes.DWORD * 64)()
    n = k32.GetConsoleProcessList(buf, 64)
    pids = list(buf[:n])
    clis = []
    for pid in pids:
        path = _image_path(k32, pid)
        if CLI_IMAGE_RE.match(path.rsplit("\\", 1)[-1]):
            clis.append((pid, path))
    return pids, clis


def _session_of_pid(pid: int):
    """sessionId recorded in ~/.claude/sessions/<pid>.json, or None."""
    try:
        import json
        path = Path.home() / ".claude" / "sessions" / f"{pid}.json"
        return json.loads(path.read_text(encoding="utf-8")).get("sessionId")
    except (OSError, ValueError, AttributeError):
        return None


def resolve_target(k32, console_pids, console_clis):
    """The CLI to submit to: ((pid, image path), detail) or (None, reason).

    Preferred source is CLAUDE_PID, which the CLI exports to its tool
    subprocesses; its image must be a CLI binary, and when both the env
    session id and sessions/<pid>.json are readable they must agree.
    Without CLAUDE_PID, the CLI processes on this console are used and
    must number exactly one."""
    env_pid = os.environ.get("CLAUDE_PID")
    if env_pid:
        try:
            pid = int(env_pid)
        except ValueError:
            return None, f"CLAUDE_PID={env_pid!r} is not a pid."
        path = _image_path(k32, pid)
        if not path:
            return None, (f"CLAUDE_PID={pid} names no running process; the "
                          f"session's CLI may have exited.")
        if not CLI_IMAGE_RE.match(path.rsplit("\\", 1)[-1]):
            return None, (f"CLAUDE_PID={pid} runs {path}, not a Claude CLI "
                          f"image.")
        env_sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
        file_sid = _session_of_pid(pid)
        if env_sid and file_sid and env_sid != file_sid:
            return None, (f"CLAUDE_PID={pid} is registered to session "
                          f"{file_sid}, not this session ({env_sid}).")
        where = ("on this console" if pid in console_pids
                 else "on another console, attaching")
        return (pid, path), f"from CLAUDE_PID, {where}"
    if len(console_clis) == 1:
        return console_clis[0], "the only CLI on this console (no CLAUDE_PID)"
    if not console_clis:
        return None, ("CLAUDE_PID is not set and no CLI process is attached "
                      "to this console, so the command would be read by "
                      "nobody.")
    return None, (f"CLAUDE_PID is not set and {len(console_clis)} CLI "
                  f"processes share this console "
                  f"({[pid for pid, _ in console_clis]}); the target is "
                  f"ambiguous.")


def _key(ch: str, down: bool) -> _InputRecord:
    rec = _InputRecord()
    rec.EventType = KEY_EVENT
    ev = rec.Event.KeyEvent
    ev.bKeyDown = down
    ev.wRepeatCount = 1
    ev.wVirtualKeyCode = VK_RETURN if ch == "\r" else 0
    ev.wVirtualScanCode = 0
    ev.uChar.UnicodeChar = ch
    ev.dwControlKeyState = 0
    return rec


def submit(k32, command: str):
    """Write `command` + Enter into the console input buffer."""
    handle = k32.CreateFileW("CONIN$", GENERIC_READ | GENERIC_WRITE,
                             FILE_SHARE_READ | FILE_SHARE_WRITE,
                             None, OPEN_EXISTING, 0, None)
    if handle == INVALID_HANDLE:
        return False, f"CreateFileW(CONIN$) error {ctypes.get_last_error()}"
    records = []
    for ch in list(command) + ["\r"]:
        records.append(_key(ch, True))
        records.append(_key(ch, False))
    arr = (_InputRecord * len(records))(*records)
    written = wintypes.DWORD(0)
    ok = k32.WriteConsoleInputW(handle, arr, len(records),
                                ctypes.byref(written))
    if not ok or written.value != len(records):
        return False, (f"WriteConsoleInputW wrote {written.value}/"
                       f"{len(records)} records, error "
                       f"{ctypes.get_last_error()}")
    return True, f"{written.value} records"


def prompt_box_state(k32):
    """(text, caret_at_start) of the attached console's prompt box, via the
    claude-restart skill's reader; ('', False) when it cannot be read."""
    try:
        import importlib.util
        rs_path = (Path.home() / ".claude" / "skills" / "claude-restart"
                   / "restart_session.py")
        spec = importlib.util.spec_from_file_location("_rs", rs_path)
        rs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rs)
        return rs.prompt_box(k32)
    except Exception:
        return "", False


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--clear", action="store_true",
                   help="submit /clear instead of /compact. Schedule the"
                        " plain-text wake cron first (clear-mode.md Step 5):"
                        " /clear rotates the session_id, so nothing else"
                        " carries the handoff path to the new session")
    p.add_argument("--pre-sleep", type=int, default=10,
                   help="seconds to wait before submitting, so the launching"
                        " turn ends first and the command stands alone")
    p.add_argument("--dry-run", action="store_true",
                   help="report console membership and the command that"
                        " would be submitted, then exit")
    p.add_argument("--cwd", default=None,
                   help="project root whose .claude/trigger_compact.log"
                        " receives the log line (default: current dir)")
    p.add_argument("--force", metavar="REASON",
                   help="submit /compact although trigger_compact.py was not"
                        f" attempted here in the last {TRIGGER_WINDOW_SECONDS // 60}"
                        " min; the reason is written to the log")
    args = p.parse_args()

    cwd = args.cwd or os.getcwd()
    command = CLEAR_COMMAND if args.clear else COMPACT_COMMAND

    # /compact only: /clear has no trigger step (clear-mode.md).
    if not args.clear:
        age = last_trigger_attempt(cwd)
        if (age is None or age > TRIGGER_WINDOW_SECONDS) and not args.force:
            when = "never" if age is None else f"{int(age // 60)} min ago"
            print(f"REFUSED: trigger_compact.py was last attempted in this project "
                  f"{when}; this route accepts /compact only within "
                  f"{TRIGGER_WINDOW_SECONDS // 60} min of such an attempt. Re-read "
                  f"the compact-loop skill (Skill tool, name=compact-loop) and "
                  f"follow its steps in their order. --force \"<reason>\" overrides.")
            log_line(cwd, f"inject_compact: REFUSED — no trigger_compact attempt within "
                          f"{TRIGGER_WINDOW_SECONDS}s (last: {when})")
            return 2
        if args.force:
            log_line(cwd, f"inject_compact: --force {args.force!r}")

    if os.name != "nt":
        print("REFUSED: console injection is implemented for Windows only.")
        return 1

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    pids, clis = console_members(k32)
    print(f"console process list: {pids}")
    for pid, path in clis:
        print(f"  CLI on this console: {pid} {path}")

    target, detail = resolve_target(k32, pids, clis)
    if target is None:
        print(f"REFUSED: {detail}")
        log_line(cwd, f"inject_compact: REFUSED — {detail}")
        return 1
    target_pid, target_path = target
    print(f"target: pid {target_pid} ({target_path}) — {detail}")

    if target_pid not in pids:
        k32.FreeConsole()
        if not k32.AttachConsole(target_pid):
            err = ctypes.get_last_error()
            detail = (f"AttachConsole({target_pid}) failed with error {err}; "
                      f"the CLI's console could not be reached from this "
                      f"process.")
            print(f"REFUSED: {detail}")
            log_line(cwd, f"inject_compact: REFUSED — {detail}")
            return 1
        print(f"attached to the console of pid {target_pid}")

    if args.dry_run:
        text, caret_at_start = prompt_box_state(k32)
        state = ("empty" if not text else
                 f"the CLI's suggestion {text!r} (not typed)" if caret_at_start
                 else f"typed text {text!r} (would refuse)")
        print(f"dry run — prompt box: {state}")
        print(f"dry run — would submit: {command}")
        print("dry run: nothing was submitted.")
        return 0

    if args.pre_sleep > 0:
        print(f"pre-sleep {args.pre_sleep}s before submitting {command} "
              f"(end the launching turn now)", flush=True)
        time.sleep(args.pre_sleep)

    text, caret_at_start = prompt_box_state(k32)
    if text and not caret_at_start:
        detail = f"prompt box holds typed text {text!r}; nothing submitted"
        print(f"REFUSED: {detail}. Ask the user to type {command} instead"
              + (" (with the absolute handoff path in the same message)."
                 if args.clear else "."))
        log_line(cwd, f"inject_compact: REFUSED — {detail}")
        return 3
    if text and caret_at_start:
        print(f"prompt box shows the CLI's suggestion {text!r} (caret at the "
              f"start); not typed, going on")

    ok, detail = submit(k32, command)
    if not ok:
        print(f"FAILED to submit {command}: {detail}")
        log_line(cwd, f"inject_compact: FAILED {detail}")
        return 1

    print(f"{command} submitted to pid {target_pid} ({detail}). If no reset "
          f"follows, the command was appended to text already sitting in "
          f"the prompt box instead of standing alone.", flush=True)
    log_line(cwd, f"inject_compact: {command} submitted to pid {target_pid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
