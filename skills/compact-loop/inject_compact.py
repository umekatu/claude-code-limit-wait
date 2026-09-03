#!/usr/bin/env python3
"""Submit this session's own reset command into the console input buffer
— the same buffer a human's keystrokes land in.

WHAT THIS CAN SEND: the literal string `/compact`, or with --clear, the
literal string `/clear`. Nothing else — both are constants in this file
and no caller-supplied text is ever submitted.

WHERE IT LANDS: the console input buffer is per-console, so the command
reaches exactly the CLI processes attached to THIS process's console.
The script requires that to be exactly one, and names it in its output.
Zero means the command would land in a buffer no session reads; more
than one means the target is ambiguous. Either way it refuses.

LAUNCH IT WITH THE TOOL WHOSE SUBPROCESSES SHARE THE CLI'S CONSOLE.
On a Windows CLI started from PowerShell that is the PowerShell tool;
the Bash tool runs under a Git-bash console of its own, where no CLI is
attached and a submitted line would be read by nobody. Run --dry-run
first from any launcher you are unsure about: it reports the console
membership without submitting.

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

It also does not detect whether a human is at the keyboard. The
submitted line joins whatever text is already in the prompt box, so
injecting while someone types merges the two into one submitted line.
That check belongs to the caller.

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
    args = p.parse_args()

    cwd = args.cwd or os.getcwd()
    command = CLEAR_COMMAND if args.clear else COMPACT_COMMAND

    if os.name != "nt":
        print("REFUSED: console injection is implemented for Windows only.")
        return 1

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    pids, clis = console_members(k32)
    print(f"console process list: {pids}")
    for pid, path in clis:
        print(f"  CLI on this console: {pid} {path}")

    if len(clis) != 1:
        if not clis:
            detail = ("no CLI process is attached to this console, so the "
                      "command would be read by nobody. Launch from a tool "
                      "whose subprocesses share the CLI's console (the "
                      "PowerShell tool on a PowerShell-started CLI; the "
                      "Bash tool gets its own Git-bash console).")
        else:
            detail = (f"{len(clis)} CLI processes share this console "
                      f"({[pid for pid, _ in clis]}), so the target is "
                      f"ambiguous and the command could reset the wrong "
                      f"session.")
        print(f"REFUSED: {detail}")
        log_line(cwd, f"inject_compact: REFUSED — {detail}")
        return 1

    target_pid, target_path = clis[0]
    print(f"target: pid {target_pid} ({target_path})")

    if args.dry_run:
        print(f"dry run — would submit: {command}")
        print("dry run: nothing was submitted.")
        return 0

    if args.pre_sleep > 0:
        print(f"pre-sleep {args.pre_sleep}s before submitting {command} "
              f"(end the launching turn now)", flush=True)
        time.sleep(args.pre_sleep)

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
