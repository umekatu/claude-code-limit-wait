#!/usr/bin/env python
"""sync_from_local.py — regenerate the published copies from the files
installed under ~/.claude/, or check whether they are in sync.

    python tools/sync_from_local.py            # rewrite the repo copies
    python tools/sync_from_local.py --check    # report SAME / DIFF / MISSING, change nothing
    python tools/sync_from_local.py --diff     # --check plus the unified diffs

Each repo file maps to one local path (MAPPING). A local file becomes its
published copy in two steps:

1. Private blocks are dropped. A block is everything from a line whose
   stripped text is `--- private:begin <name>` (optionally after a `#`) to
   the matching `--- private:end <name>` line, inclusive. Code that stays
   must remain valid without the block, so any name the rest of the file
   still uses is defined outside the block.
2. The literal substitutions in SUBSTITUTIONS are applied. Each `old` text
   must be present; a missing one aborts the run so a silently stale
   substitution cannot slip through.

Everything else is copied verbatim, so `--check` reporting SAME for every
file means the repo publishes exactly the local install minus the private
blocks and the listed substitutions.
"""
import difflib
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = os.path.expanduser("~")

MAPPING = {
    "hooks/usage-probe-statusline.py": ".claude/hooks/usage-probe-statusline.py",
    "hooks/oauth-usage-probe.py": ".claude/hooks/oauth-usage-probe.py",
    "hooks/context-monitor.py": ".claude/hooks/context-monitor.py",
    "hooks/limit-wait.py": ".claude/hooks/limit-wait.py",
    "hooks/compact-handoff-guard.py": ".claude/hooks/compact-handoff-guard.py",
    "hooks/compact-handoff-resume.py": ".claude/hooks/compact-handoff-resume.py",
    "hooks/cache-keepalive.py": ".claude/hooks/cache-keepalive.py",
    "skills/limit-wait/SKILL.md": ".claude/skills/limit-wait/SKILL.md",
    "skills/compact-loop/SKILL.md": ".claude/skills/compact-loop/SKILL.md",
    "skills/compact-loop/clear-mode.md": ".claude/skills/compact-loop/clear-mode.md",
    "skills/compact-loop/recovery.md": ".claude/skills/compact-loop/recovery.md",
    "skills/compact-loop/trigger_compact.py": ".claude/skills/compact-loop/trigger_compact.py",
    "skills/compact-loop/inject_compact.py": ".claude/skills/compact-loop/inject_compact.py",
    "scripts/compact-handoff/dump.py": ".claude/scripts/compact-handoff/dump.py",
}

# Wording that exists only in the author's install: pointers to memory notes
# and to hooks or skills that are not part of this repo.
SUBSTITUTIONS = {
    "skills/compact-loop/SKILL.md": [
        ("  apology / mistake-recognition this cycle routes through the\n"
         "  `postmortem` skill — do not write that rule yourself.",
         "  apology / mistake-recognition this cycle goes through your\n"
         "  mistake-handling procedure, not this step."),
        ("takes; if the solo-idle-guard blocks the Stop, idle through it.",
         "takes; if a Stop hook of yours blocks the Stop, idle through it."),
        ("waiting loops, no extra tool calls, no watchdog for the ~50 s this",
         "waiting loops, no extra tool calls, no waiter for the ~50 s this"),
        ("Background tasks / subagents / crons / watchdog still running: what",
         "Background tasks / subagents / crons / waiters still running: what"),
        ("   `CLAUDE_CODE_AUTO_COMPACT_WINDOW` must be back at the `900000`\n"
         "   baseline (cross-check the set/restored pair in\n"
         "   `<cwd>/.claude/trigger_compact.log`). Still shrunk → OVERWRITE to\n"
         "   `900000` now; never delete the key (env hot-reload is\n"
         "   add/overwrite only — a delete does not unset the live value).",
         "   `CLAUDE_CODE_AUTO_COMPACT_WINDOW` must be back at your baseline (the\n"
         "   trigger's `--restore-value`, default `900000`; cross-check the\n"
         "   set/restored pair in `<cwd>/.claude/trigger_compact.log`). Still\n"
         "   shrunk → OVERWRITE to that baseline now; never delete the key (env\n"
         "   hot-reload is add/overwrite only — a delete does not unset the live\n"
         "   value)."),
        ("arrives empty — the handoff file is the sole recovery source. Full\n"
         "matrix: memory [[compact-survival-matrix]].",
         "arrives empty — the handoff file is the sole recovery source."),
    ],
    "skills/compact-loop/recovery.md": [
        ("later breakpoint, until the compact lands. Idle behind\n"
         "        `watchdog-timer` only when no work remains.",
         "later breakpoint, until the compact lands. Idle only when no\n"
         "        work remains."),
    ],
    "skills/compact-loop/clear-mode.md": [
        ("3. Idle. The solo-idle-guard will block the first Stop; idle through\n"
         "   it — that is expected here.",
         "3. Idle. If a Stop hook of yours blocks the first Stop, idle through\n"
         "   it — that is expected here."),
    ],
    "skills/limit-wait/SKILL.md": [
        (" For a wait with a specific known target time unrelated to rate limits, use `wake-at` instead.", ""),
        ("\nSibling waiters: `wake-at` (known ETA), `watchdog-timer` (unknown ETA).\n", ""),
    ],
}

# The compact-loop skill ends with a paragraph of memory pointers; everything
# from this sentence on is dropped.
TAIL_CUT = {
    "skills/compact-loop/SKILL.md": "Rationale, mechanism details, and verification history: memory",
}

PRIVATE_BEGIN = re.compile(r"^\s*#?\s*--- private:begin (\S+)\s*$")
PRIVATE_END = re.compile(r"^\s*#?\s*--- private:end (\S+)\s*$")


def strip_private(text: str, rel: str) -> str:
    out = []
    open_name = None
    for line in text.splitlines(keepends=True):
        if open_name is None:
            m = PRIVATE_BEGIN.match(line)
            if m:
                open_name = m.group(1)
                continue
            out.append(line)
        else:
            m = PRIVATE_END.match(line)
            if m:
                if m.group(1) != open_name:
                    raise SystemExit(f"{rel}: private:end {m.group(1)} closes {open_name}")
                open_name = None
    if open_name is not None:
        raise SystemExit(f"{rel}: private block {open_name} never closed")
    return "".join(out)


def publish_text(rel: str) -> str:
    local_path = os.path.join(HOME, MAPPING[rel])
    text = open(local_path, encoding="utf-8").read()
    text = strip_private(text, rel)
    for old, new in SUBSTITUTIONS.get(rel, []):
        if old not in text:
            raise SystemExit(f"{rel}: substitution source text not found:\n{old[:80]!r}")
        text = text.replace(old, new)
    cut = TAIL_CUT.get(rel)
    if cut:
        i = text.find(cut)
        if i < 0:
            raise SystemExit(f"{rel}: tail-cut marker not found: {cut!r}")
        text = text[:i].rstrip() + "\n"
    return text


def main() -> int:
    check = "--check" in sys.argv or "--diff" in sys.argv
    show_diff = "--diff" in sys.argv
    drift = 0
    for rel in MAPPING:
        repo_path = os.path.join(HERE, rel)
        local_path = os.path.join(HOME, MAPPING[rel])
        if not os.path.exists(local_path):
            print(f"MISSING  {rel}  (no {MAPPING[rel]})")
            drift += 1
            continue
        new = publish_text(rel)
        if check:
            old = open(repo_path, encoding="utf-8").read() if os.path.exists(repo_path) else ""
            if old == new:
                print(f"SAME     {rel}")
                continue
            drift += 1
            a, b = old.splitlines(), new.splitlines()
            diff = list(difflib.unified_diff(a, b, "repo/" + rel, "local(sanitized)/" + rel, lineterm=""))
            plus = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
            minus = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
            print(f"DIFF     {rel}  (+{plus} -{minus})")
            if show_diff:
                print("\n".join(diff))
                print()
        else:
            os.makedirs(os.path.dirname(repo_path), exist_ok=True)
            with open(repo_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(new)
            print(f"WROTE    {rel}")
    return 1 if (check and drift) else 0


if __name__ == "__main__":
    raise SystemExit(main())
