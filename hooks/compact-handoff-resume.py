#!/usr/bin/env python3
"""UserPromptSubmit hook: inject pointer to a fresh compact-handoff.

Companion to compact-handoff-guard.py and dump.py. Purpose: make the
post-compact resume mechanism not depend on the auto-summary preserving
the handoff path. After /compact, the next user prompt fires this hook,
which injects a pointer to <cwd>/.work/compact-handoff/<session_id>.md
via additionalContext — provided the handoff is fresher than the
sentinel that marks "already announced".

Files (per session):
  <cwd>/.work/compact-handoff/<session_id>.md         the handoff
  <cwd>/.work/compact-handoff/<session_id>.injected   the sentinel

Algorithm:
  1. Read session_id + cwd + prompt from hook stdin (authoritative).
  2. If the prompt is `/compact` (any form): delete the sentinel and
     exit silently. Compact discards the announcement from conversation
     memory, so the post-compact prompt must re-inject — even if a
     previous pre-compact prompt already touched the sentinel.
  3. If handoff missing → exit silently.
  4. If sentinel exists AND sentinel.mtime >= handoff.mtime → already
     announced for this version, exit silently.
  5. Else → inject additionalContext with the path + a READ instruction,
     then touch the sentinel.

Each new `dump.py` write updates handoff.mtime, invalidating the
sentinel and re-arming the hook for the next user prompt — so multi-
cycle resume works without per-cycle setup.

Fail-open on any exception → silent exit 0. Other sessions are
unaffected because they have no handoff file under their session_id.
"""
import json
import sys
from pathlib import Path


def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        return

    session_id = data.get("session_id") or ""
    cwd = data.get("cwd") or ""
    if not session_id or not cwd:
        return

    handoff_dir = Path(cwd) / ".work" / "compact-handoff"
    handoff = handoff_dir / f"{session_id}.md"
    sentinel = handoff_dir / f"{session_id}.injected"

    # If the user prompt itself is a context-wiping command (/compact or
    # /clear), invalidate the sentinel and bail. Both commands wipe the
    # in-conversation record of the prior announcement, so the next user
    # prompt must be allowed to re-inject regardless of sentinel mtime.
    # /clear additionally may reset session_id — in that case the hook
    # naturally finds nothing on the next prompt because the file is
    # keyed by session_id (handled by the normal "handoff missing" path).
    prompt = data.get("prompt") or ""
    if isinstance(prompt, str):
        head = prompt.lstrip().split(maxsplit=1)[0] if prompt.strip() else ""
        if head in ("/compact", "/clear"):
            try:
                sentinel.unlink(missing_ok=True)
            except (OSError, TypeError):
                pass
            return

    if not handoff.exists():
        return

    try:
        handoff_mtime = handoff.stat().st_mtime
        if sentinel.exists() and sentinel.stat().st_mtime >= handoff_mtime:
            return
    except OSError:
        return

    info = (
        f"Compact-handoff file exists for this session and has not yet "
        f"been announced this turn-cycle:\n"
        f"  {handoff}\n"
        f"If you are the post-compact instance, READ this file now to "
        f"recover the pre-compact intent, established facts, next "
        f"concrete action, and open questions. The auto-summary may "
        f"not contain everything."
    )

    try:
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": info,
            }
        }, ensure_ascii=False))
    except Exception:
        pass

    # Mark this handoff version as announced.
    try:
        handoff_dir.mkdir(parents=True, exist_ok=True)
        sentinel.touch()
    except OSError:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
