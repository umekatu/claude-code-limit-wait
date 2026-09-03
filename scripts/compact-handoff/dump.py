#!/usr/bin/env python3
"""Write pre-compact handoff to a session-scoped file.

Usage:
    python dump.py --topic "..." < body.md
    python dump.py --topic "..." --body "inline body text"
    echo "body" | python dump.py

Declare Step 2's self-audit findings with --audit-fixed PATH STRING (repeat
per gap you fixed), --audit-pending PATH NOTE (repeat per gap you left), or
--audit-none. Findings are recorded in <session_id>.audit.jsonl; a "fixed"
claim whose STRING is absent from PATH is written into the handoff's
"Documentation gaps" section and exits 3.

Resolves session_id and cwd from ~/.claude/usage-snapshot.json.
Writes to <cwd>/.work/compact-handoff/<session_id>.md (overwrites).
Tracks cycle count at <cwd>/.work/compact-handoff/<session_id>.cycles.
Prints the absolute handoff path on success.

The companion procedure is the compact-loop skill (skills/compact-loop/SKILL.md).
NOT a hook, NOT a skill — invoke explicitly via Bash. No side effects on
other sessions; settings.json is not touched.
"""
import sys
import os
import re
import json
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Force UTF-8 on stdin/stdout regardless of the parent shell's codepage
# (Windows cp932 stdin would otherwise emit lone surrogates for em-dashes
# and other non-ASCII chars). Safe on POSIX where stdin is already utf-8.
try:
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

SNAPSHOT = Path(os.path.expanduser("~/.claude/usage-snapshot.json"))

GAPS_HEAD_RX = re.compile(r"^##[ \t]+Documentation gaps[ \t]*$", re.MULTILINE)
NEXT_HEAD_RX = re.compile(r"^## ", re.MULTILINE)
PLACEHOLDER_RX = re.compile(r"\s*(?:none|n/a[^\n]*)?\s*", re.IGNORECASE)


def verify_audit_fixed(fixed, cwd):
    """Read each --audit-fixed file and look for the string the fix put in it.

    Returns one (path, string, reason) tuple per claim that did not verify.
    """
    misses = []
    for path_s, needle in fixed:
        p = Path(path_s)
        if not p.is_absolute():
            p = Path(cwd) / p
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            misses.append((path_s, needle, f"file unreadable ({e.strerror or e})"))
            continue
        if needle not in text:
            misses.append((path_s, needle, "string not found"))
    return misses


def inject_gaps(body, lines):
    """Put lines into the body's "## Documentation gaps" section.

    A bare none/n/a placeholder there is replaced; existing content is kept and
    appended to; a missing section is added at the end of the body.
    """
    block = "\n".join(lines)
    m = GAPS_HEAD_RX.search(body)
    if not m:
        return body.rstrip() + "\n\n## Documentation gaps\n\n" + block + "\n"
    start = m.end()
    nxt = NEXT_HEAD_RX.search(body, start)
    end = nxt.start() if nxt else len(body)
    section = body[start:end]
    if PLACEHOLDER_RX.fullmatch(section):
        section = "\n\n" + block + "\n\n"
    else:
        section = section.rstrip() + "\n" + block + "\n\n"
    return body[:start] + section + body[end:]


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Write a pre-compact handoff file.",
        epilog=(
            "WARNING: ~/.claude/usage-snapshot.json is a GLOBAL file shared across "
            "all running sessions (overwritten on each statusLine fire). If multiple "
            "sessions are active, snapshot fallback may return another session's data. "
            "Always pass --session-id (and --cwd) explicitly when other sessions are "
            "running. Snapshot is only safe as a fallback when this is the only session."
        ),
    )
    ap.add_argument("--topic", default="", help="Short topic line shown in the header.")
    ap.add_argument("--body", default=None,
                    help="Inline body content. If omitted, body is read from stdin.")
    ap.add_argument("--session-id", default=None,
                    help="REQUIRED when other sessions may be active. The caller's own "
                         "session_id. Falls back to snapshot if omitted (unsafe in "
                         "multi-session scenarios).")
    ap.add_argument("--cwd", default=None,
                    help="Caller's cwd. Required together with --session-id (or "
                         "defaults to snapshot's cwd, which is unsafe in multi-session).")
    ap.add_argument("--print-path-only", action="store_true",
                    help="Skip writing; just resolve and print the handoff path.")
    ap.add_argument("--audit-fixed", nargs=2, action="append", default=[],
                    metavar=("PATH", "STRING"),
                    help="A Step 2 self-audit gap you fixed in place: the file, and a "
                         "string the fix put in it. Verified by reading PATH for "
                         "STRING. Repeatable.")
    ap.add_argument("--audit-pending", nargs=2, action="append", default=[],
                    metavar=("PATH", "NOTE"),
                    help="A Step 2 self-audit gap you did not fix: the file, and a "
                         "one-line description. Repeatable.")
    ap.add_argument("--audit-none", action="store_true",
                    help="Pass alone when the Step 2 self-audit found no gaps.")
    args = ap.parse_args()

    # --print-path-only is owned by the guard hook (it injects
    # session_id / cwd / handoff_path as additionalContext via the hook).
    # dump.py itself does nothing in this mode — no snapshot read, no
    # write, no stdout. Exit 0 immediately so the hook's injected info
    # is the only output the model sees.
    if args.print_path_only:
        return 0

    if args.session_id is not None:
        session_id = args.session_id
        if args.cwd is not None:
            cwd = Path(args.cwd)
        else:
            print("ERROR: --cwd is required when --session-id is given "
                  "(snapshot fallback for cwd is unsafe under multi-session).",
                  file=sys.stderr)
            return 2
        # Optional sanity check against snapshot — if snapshot exists and disagrees,
        # warn but proceed (the explicit args are authoritative).
        if SNAPSHOT.exists():
            try:
                snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))["parsed"]
                if snap.get("session_id") != session_id:
                    print(f"INFO: snapshot session_id ({snap.get('session_id')}) "
                          f"differs from --session-id ({session_id}). Using explicit "
                          "args. This is normal when other sessions are active.",
                          file=sys.stderr)
            except (json.JSONDecodeError, KeyError):
                pass
    else:
        if not SNAPSHOT.exists():
            print(f"ERROR: --session-id not given and snapshot missing at {SNAPSHOT}.",
                  file=sys.stderr)
            return 2
        try:
            snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))["parsed"]
        except (json.JSONDecodeError, KeyError) as e:
            print(f"ERROR: snapshot unreadable: {e}", file=sys.stderr)
            return 2
        session_id = snap["session_id"]
        cwd = Path(snap["cwd"])
        print(f"WARNING: --session-id not given; using snapshot session_id "
              f"({session_id}). UNSAFE if other sessions are active. See --help.",
              file=sys.stderr)

    handoff_dir = cwd / ".work" / "compact-handoff"
    handoff_path = handoff_dir / f"{session_id}.md"
    cycles_path = handoff_dir / f"{session_id}.cycles"

    # Read body FIRST — if stdin pipe fails or yields empty, we want to
    # abort before advancing the cycle counter or touching the filesystem.
    if args.body is not None:
        body = args.body
    else:
        body = sys.stdin.read()
    if not body.strip():
        print("ERROR: handoff body is empty. Pass via stdin or --body.",
              file=sys.stderr)
        return 2

    # Step 2's self-audit findings arrive as flags. A "fixed" claim is checked
    # against the file it names; anything unverified, undeclared or self-
    # contradictory lands in the handoff's own "Documentation gaps" section so
    # the successor applies it first. The handoff is written either way.
    audit_problems = []
    exit_code = 0
    if not (args.audit_fixed or args.audit_pending or args.audit_none):
        audit_problems.append(
            "- Step 2 self-audit findings were not declared to dump.py "
            "(--audit-fixed / --audit-pending / --audit-none). Run both audit "
            "phases and apply or record what they find."
        )
        exit_code = 2
    elif args.audit_none and (args.audit_fixed or args.audit_pending):
        audit_problems.append(
            "- --audit-none was passed together with a named finding. Re-check "
            "which Step 2 gaps exist and record each one."
        )
        exit_code = 2
    for path_s, needle, why in verify_audit_fixed(args.audit_fixed, cwd):
        audit_problems.append(
            f"- `{path_s}` — UNVERIFIED audit claim: \"{needle}\" {why} at "
            "handoff time. Apply this fix first."
        )
        exit_code = 3
    if audit_problems:
        body = inject_gaps(body, audit_problems)

    handoff_dir.mkdir(parents=True, exist_ok=True)

    cycle = 1
    if cycles_path.exists():
        try:
            cycle = int(cycles_path.read_text(encoding="utf-8").strip()) + 1
        except ValueError:
            cycle = 1

    jst = timezone(timedelta(hours=9))
    ts = datetime.now(jst).strftime("%Y-%m-%d %H:%M JST")
    topic_line = f"## Topic: {args.topic}\n" if args.topic else ""

    content = (
        f"# Compact Handoff — cycle {cycle} — {ts}\n"
        f"{topic_line}\n"
        f"{body.rstrip()}\n\n"
        f"---\n"
        f"_cycle {cycle} · session {session_id} · written {ts}_\n"
    )
    handoff_path.write_text(content, encoding="utf-8")
    # Advance the cycle counter only AFTER a successful handoff write,
    # so failed runs don't burn a cycle.
    cycles_path.write_text(str(cycle), encoding="utf-8")

    audit_path = handoff_dir / f"{session_id}.audit.jsonl"
    with audit_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "ts": ts,
            "cycle": cycle,
            "session_id": session_id,
            "fixed": [list(x) for x in args.audit_fixed],
            "pending": [list(x) for x in args.audit_pending],
            "none": bool(args.audit_none),
            "problems": audit_problems,
        }, ensure_ascii=False) + "\n")

    print(str(handoff_path))
    if audit_problems:
        print("AUDIT: handoff written, with these findings unresolved and now "
              "listed under its 'Documentation gaps' section:", file=sys.stderr)
        for line in audit_problems:
            print("  " + line, file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
