#!/usr/bin/env python3
"""Context self-report hook for Claude Code (PostToolUse).

Reads the hook stdin, locates the calling session's transcript JSONL file, sums
the latest assistant message's ``message.usage`` block to get current context
token count, then emits a ``systemMessage`` of the form::

    ℹ️ Context: N tokens used (model) | 5h XX% in 2h40m (rsts MM-DD HH:MM JST) | 7d XX% in 5d0h (rsts MM-DD HH:MM JST)

The rate-limit fragment is appended only when ``~/.claude/usage-snapshot.json``
(written by the statusLine probe) is present and parseable. If anything goes
wrong reading it, the message degrades to just the token count — the primary
report must never break because of optional infrastructure.

Dedup: per-session state file at ``~/.claude/.context-monitor-state/<sid>.json``
remembers the last emitted *raw values* (tokens, percentages, reset epochs).
If the new computation matches the saved state exactly, the hook returns
without emitting anything — preventing identical-state spam across rapid tool
chains. The countdown text changes every minute by definition and is excluded
from the comparison so idle minutes don't trigger emissions.

Max window size and percentage are intentionally omitted: Claude Code does not
expose the effective context window to hooks (see
https://github.com/anthropics/claude-code/issues/34340), so any computed
percentage would be unreliable — a 200K Pro session with ``claude-opus-4-6``
looks identical in the transcript to a 1M ``opus[1m]`` session. Agents
self-judge rotation timing against their known session window until that
feature request lands.
"""
import json
import os
import sys
from datetime import datetime, timedelta

SNAPSHOT_PATH = os.path.expanduser("~/.claude/usage-snapshot.json")
STATE_DIR = os.path.expanduser("~/.claude/.context-monitor-state")

# Critical thresholds — at or above these values, the corresponding Limits
# segment is force-emitted (bypassing dedup) and an advisory is appended
# instructing the agent to consider scheduling a wake-up for after the reset.
H5_CRITICAL = 90
D7_CRITICAL = 97


def find_latest_usage(transcript_path: str) -> tuple[int | None, str]:
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return None, ""

    for line in reversed(lines):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") != "assistant":
            continue
        msg = d.get("message", {})
        usage = msg.get("usage")
        if not usage:
            continue
        tokens = (
            usage.get("input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
        )
        return tokens, msg.get("model", "")
    return None, ""


def read_snapshot() -> dict | None:
    try:
        with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def fmt_rel(td: timedelta) -> str:
    s = int(td.total_seconds())
    if s < 0:
        return "now"
    if s < 3600:
        return f"in {s // 60}m"
    if s < 86400:
        h, m = divmod(s, 3600)
        m //= 60
        return f"in {h}h{m}m" if m else f"in {h}h"
    d, rem = divmod(s, 86400)
    h = rem // 3600
    return f"in {d}d{h}h" if h else f"in {d}d"


def extract_limits(snap: dict | None) -> tuple[dict | None, dict | None]:
    """Returns ``(h5, d7)`` dicts with parsed ``pct`` / ``reset`` (datetime),
    or ``None`` when the snapshot is missing or unparseable."""
    if not snap:
        return None, None
    try:
        r = (snap.get("parsed") or {}).get("rate_limits") or {}
        h5_raw = r.get("five_hour") or {}
        d7_raw = r.get("seven_day") or {}
        h5 = (
            {"pct": float(h5_raw["used_percentage"]),
             "reset": datetime.fromtimestamp(h5_raw["resets_at"])}
            if "used_percentage" in h5_raw and "resets_at" in h5_raw else None
        )
        d7 = (
            {"pct": float(d7_raw["used_percentage"]),
             "reset": datetime.fromtimestamp(d7_raw["resets_at"])}
            if "used_percentage" in d7_raw and "resets_at" in d7_raw else None
        )
        return h5, d7
    except Exception:
        return None, None


def fmt_limit(label: str, data: dict) -> str:
    """e.g. '5h 30% in 2h25m (rsts 05-11 00:40 JST)'"""
    now = datetime.now()
    return (
        f"{label} {data['pct']:.0f}% {fmt_rel(data['reset'] - now)}"
        f" (rsts {data['reset'].strftime('%m-%d %H:%M')} JST)"
    )


def collect_state(tokens: int, h5: dict | None, d7: dict | None) -> dict:
    """Per-segment raw values used for dedup. Excludes time-relative fields.

    Three independent segments:
      ``ctx`` — context-window tokens
      ``h5``  — 5-hour rate-limit (percentage + reset epoch)
      ``d7``  — 7-day rate-limit (percentage + reset epoch)
    Each segment is dedup'd independently so a 5h tick doesn't redundantly
    re-emit the slow-moving 7d figure (or vice-versa).
    """
    return {
        "ctx": {"t": tokens},
        "h5": {"p": round(h5["pct"]), "r": int(h5["reset"].timestamp())} if h5 else None,
        "d7": {"p": round(d7["pct"]), "r": int(d7["reset"].timestamp())} if d7 else None,
    }


def state_path_for(session_id: str) -> str:
    """One shared state file per session. Content is per-segment shaped
    ``{"value": ..., "notified": [agent_keys]}``; see ``main()`` for the
    notified-set logic that delivers each value change to leader and every
    subagent exactly once. (History 2026-05-20: pre-fix shared file used
    flag-based dedup which let whichever agent fired first ``consume`` the
    change so later agents missed it; an interim per-agent-file fix was
    tried; current single-file + notified-set design is the final form.)"""
    safe = "".join(c for c in (session_id or "default") if c.isalnum() or c in "-_")
    return os.path.join(STATE_DIR, f"{safe or 'default'}.json")


def main() -> None:
    try:
        hook_input = json.loads(sys.stdin.read())
    except Exception:
        return

    transcript_path = hook_input.get("transcript_path")
    if not transcript_path:
        return

    tokens, model = find_latest_usage(transcript_path)
    if tokens is None:
        return

    snap = read_snapshot()
    h5, d7 = extract_limits(snap)
    current = collect_state(tokens, h5, d7)

    agent_key = hook_input.get("agent_id") or "leader"
    is_subagent = "agent_id" in hook_input
    sp = state_path_for(hook_input.get("session_id", ""))
    try:
        with open(sp, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = {}
    if not isinstance(state, dict):
        state = {}

    # Single shared state file per session (2026-05-20 redesign per user).
    # Per-segment entry shape: {"value": <cur>, "notified": [agent_keys]}.
    # Emit a segment for THIS agent when either:
    #   (a) value changed since last save → reset notified=[agent_key] and
    #       emit (effectively "clear on value change" per user),
    #   (b) value unchanged but this agent hasn't been notified yet → add
    #       agent_key to notified and emit.
    # This delivers each value change to each agent (leader + every
    # subagent) exactly once, fixing the prior shared-flag dedup miss.
    def consider(seg_name: str, cur_val):
        """Mutate ``state[seg_name]``; return True iff THIS agent should
        receive the segment now."""
        if cur_val is None:
            return False
        st = state.get(seg_name) or {}
        saved = st.get("value")
        notified = list(st.get("notified") or [])
        if cur_val != saved:
            state[seg_name] = {"value": cur_val, "notified": [agent_key]}
            return True
        if agent_key not in notified:
            notified.append(agent_key)
            state[seg_name] = {"value": cur_val, "notified": notified}
            return True
        return False

    # Subagent ctx is suppressed entirely: subagent PostToolUse carries the
    # LEADER's transcript_path (verified 2026-05-19), so the token count is
    # the leader's, not the subagent's — misleading "info" to a teammate.
    # Do NOT touch ctx state from a subagent fire.
    emit_ctx = (not is_subagent) and consider("ctx", current["ctx"])
    emit_h5  = consider("h5", current["h5"])
    emit_d7  = consider("d7", current["d7"])

    h5_critical = h5 is not None and h5["pct"] >= H5_CRITICAL
    d7_critical = d7 is not None and d7["pct"] >= D7_CRITICAL

    if not (emit_ctx or emit_h5 or emit_d7 or h5_critical or d7_critical):
        return  # nothing new for this agent and no critical threshold

    segments: list[str] = []
    if emit_ctx:
        segments.append(f"Context: {tokens:,} tokens used")

    rl_parts: list[str] = []
    if (emit_h5 or h5_critical) and h5:
        rl_parts.append(fmt_limit("5h", h5))
    if (emit_d7 or d7_critical) and d7:
        rl_parts.append(fmt_limit("7d", d7))
    if rl_parts:
        segments.append("Limits: " + ", ".join(rl_parts))

    advisory_parts: list[str] = []
    if h5_critical:
        advisory_parts.append(f"5h limit ≥{H5_CRITICAL}%")
    if d7_critical:
        advisory_parts.append(f"7d limit ≥{D7_CRITICAL}%")
    if advisory_parts:
        segments.append("⚠️ " + ", ".join(advisory_parts) + ". Invoke the limit-wait skill NOW (Skill tool, name=limit-wait) to wait out the reset in-session and auto-resume — do NOT stop for the user.")

    if not segments:
        return

    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(sp, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception:
        pass

    msg = "ℹ️ " + " | ".join(segments)
    sys.stdout.write(json.dumps({
        "systemMessage": msg,
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": msg,
        },
    }))


if __name__ == "__main__":
    main()
