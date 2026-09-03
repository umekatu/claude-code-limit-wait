#!/usr/bin/env python
"""cache-keepalive.py — Stop hook (asyncRewake) that keeps the session's
1-hour prompt cache warm while the session is idle.

Registered in settings.json as a Stop hook with "asyncRewake": true and a
"timeout" longer than the idle threshold: the harness backgrounds it and
wakes the model when it exits with code 2, showing its stderr as a system
reminder. Every Stop spawns a fresh instance; the newest instance owns the
session (its pid is in a lock file under ~/.claude/) and older ones exit
quietly the next time they poll. The owner reads the timestamp of the
transcript's last user/assistant record (harness-only records such as the
away recap do not count) and, once the session has been idle for
--idle-seconds, prints a one-line ping to stderr and exits 2. That wake is
one request that re-reads the cached prefix and resets the 1h TTL; the
model answers "ok", Stop fires again, and the cycle repeats. While the
session is active nothing happens.

A run of pings that nobody interrupts ends on its own: after
MAX_CONSECUTIVE_PINGS wakes answered without any tool call or human input,
the next instance exits 0 without a word, so the session simply stays
idle (an announced ending could set the model off doing things). The
HINT_AT_PING-th ping of such a run carries a one-time note that compacting
before idling makes each refresh cheaper.

  python cache-keepalive.py [--idle-seconds 3300] [--poll 30]
  (Stop payload on stdin: session_id, transcript_path)

Exit codes: 2 ping emitted (wakes the model), 0 superseded / no transcript /
run ended.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

try:
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

TAIL_BYTES = 262144
PING_TAG = "[cache-keepalive]"       # the rewakeMessage prefix, present in every wake record
MAX_CONSECUTIVE_PINGS = 12           # ~11 h of unattended idling, then stop silently
HINT_AT_PING = 6
HINT = (" One-time note: if nothing in this context needs to stay loaded, compacting "
        "now and then idling makes each of these refreshes cheaper; otherwise just reply ok.")


def _tail_entries(path):
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - TAIL_BYTES))
        chunk = f.read()
    entries = []
    for line in chunk.split(b"\n"):
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except Exception:
            continue
    return entries


def _user_text(entry):
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text")
    return ""


def last_activity(path):
    """Epoch seconds of the last user/assistant record; file mtime if none is
    found in the tail."""
    try:
        for d in reversed(_tail_entries(path)):
            ts = d.get("timestamp")
            if d.get("type") in ("user", "assistant") and ts:
                return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (OSError, ValueError):
        pass
    return os.stat(path).st_mtime


def consecutive_pings(path):
    """How many of the transcript's trailing turns were keep-alive wakes
    answered without a tool call. Any other user text, or any tool use,
    ends the run."""
    n = 0
    try:
        for d in reversed(_tail_entries(path)):
            t = d.get("type")
            if t == "assistant":
                content = (d.get("message") or {}).get("content")
                if isinstance(content, list) and any(
                        isinstance(b, dict) and b.get("type") == "tool_use" for b in content):
                    break
                continue
            if t == "user":
                txt = _user_text(d)
                if not txt.strip():
                    continue                       # tool_result-only record
                if txt.lstrip().startswith("<task-notification>") and PING_TAG in txt:
                    n += 1
                    continue
                break                              # real input
    except OSError:
        pass
    return n


ap = argparse.ArgumentParser()
ap.add_argument("--idle-seconds", type=int, default=3300)  # 55 min: 5 min under the 1h TTL
ap.add_argument("--poll", type=int, default=30)
a = ap.parse_args()

try:
    payload = json.load(sys.stdin)
except Exception:
    sys.exit(0)
transcript = str(payload.get("transcript_path") or "")
sid = str(payload.get("session_id") or "")
if not sid or not transcript or not os.path.exists(transcript):
    sys.exit(0)

lock = os.path.expanduser(f"~/.claude/.cache-keepalive-{sid}.pid")
me = str(os.getpid())
with open(lock, "w", encoding="utf-8") as f:
    f.write(me)

while True:
    try:
        with open(lock, encoding="utf-8") as f:
            owner = f.read().strip()
    except OSError:
        owner = ""
    if owner != me:
        sys.exit(0)          # a newer Stop spawned a newer instance
    try:
        idle = time.time() - last_activity(transcript)
    except OSError:
        sys.exit(0)
    if idle >= a.idle_seconds:
        try:
            os.remove(lock)
        except OSError:
            pass
        done = consecutive_pings(transcript)
        if done >= MAX_CONSECUTIVE_PINGS:
            sys.exit(0)      # the run is over; say nothing and let the session rest
        msg = (f"just keeping this session's prompt cache warm (idle {int(idle // 60)} min). "
               "Nothing happened and nothing is needed from you or the user. "
               "Reply with the single word ok and go back to idling.")
        if done + 1 == HINT_AT_PING:
            msg += HINT
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()
        sys.exit(2)
    time.sleep(a.poll)
