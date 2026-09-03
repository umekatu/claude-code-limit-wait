#!/usr/bin/env python3
"""Context self-report hook for Claude Code (PostToolUse).

Reads the hook stdin, locates the calling session's transcript JSONL file, sums
the latest assistant message's ``message.usage`` block to get current context
token count, then emits a ``systemMessage`` of the form::

    ℹ️ Context used: N tokens (model) | Limits used: 5h XX% in 2h40m (rsts MM-DD HH:MM +HHMM), 7d XX% in 5d0h (rsts MM-DD HH:MM +HHMM)

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

Band advisories: when the leader's context % enters a ``CTX_BANDS`` band
(75 steer-to-breakpoint / 85 compact-now), the band's
directive text is appended once (segment ``ctxadv:<agent>`` in the same
notified-set state). Leaving the band — e.g. % drops after a compact —
re-arms it for the next crossing.


Window size is read from ``~/.claude/usage-snapshot.json``
(``parsed.context_window.context_window_size``) which the statusLine probe
already writes — same source as the startup banner's "Opus 4.7 (1M context)"
label, which carries the ``[1m]`` distinction the transcript JSONL strips.
When the snapshot is present, the leader's emission becomes
``N tokens used / 1M (X%)``. The snapshot is leader-scoped (statusLine fires
only for the foreground session), so a subagent's window comes from the
model-family rule and its tokens from its own JSONL under
``<leader_sid>/subagents/`` (path derived from the payload agent_id).
Subagents get their own advisory bands (SUB_CTX_BANDS — report load to the
leader / write the handoff; they cannot compact themselves), and the leader
gets a watch over its ACTIVE subagents' fill (subagent_watch — a
threshold-crossing warning per worker per band, re-armed when the worker
drops below the band; nothing else surfaces a worker's remaining window to
the leader).
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta

# Language of the user-facing systemMessage ("en" or "ja"); the agent-facing
# additionalContext is always English. Set CLAUDE_HOOK_USER_LANG=ja in
# settings.json's "env" block for Japanese.
USER_LANG = os.environ.get("CLAUDE_HOOK_USER_LANG", "en").strip().lower()
# Reset times are rendered in local time; this is the local UTC offset label.
TZ_LABEL = datetime.now().astimezone().strftime("%z")

SNAPSHOT_PATH = os.path.expanduser("~/.claude/usage-snapshot.json")
STATE_DIR = os.path.expanduser("~/.claude/.context-monitor-state")
# Oauth-usage cache written by oauth-usage-probe.py --refresh-cache and kept
# fresh by the statusLine probe (which spawns the refresh detached). This hook
# only READS it — no network on the model's turn path.
OAUTH_CACHE_PATH = os.path.expanduser("~/.claude/.oauth-usage-cache.json")
OAUTH_CACHE_MAX_AGE = 1800  # seconds; older cache → segment omitted

# Critical thresholds — at or above these values, the corresponding Limits
# segment is force-emitted (bypassing dedup) and an advisory is appended
# directing the agent to invoke limit-wait. H5 sits below 100 because this
# hook fires only on PostToolUse: a long Fable turn (or parallel Fable
# sessions) can burn several points between fires. Must equal
# limit-wait.py's H5_CRITICAL — if the warning fires below limit-wait's
# threshold, the skill exits with nothing_to_wait and the advisory loops.
H5_CRITICAL = 95
D7_CRITICAL = 99

# Context-window advisory bands for the LEADER (subagents can't run
# compact-loop and get SUB_CTX_BANDS below instead). When an agent's
# context % crosses INTO a band, the matching advisory is appended
# once, tracked via the notified-set state (segment ``ctxadv:<agent>``).
# Dropping out of a band (e.g. % falls after a compact) re-arms it, so each
# work cycle gets its own nudge. Ordered highest-first; first match wins.
CTX_BANDS = [
    (85, "Context ≥85%: stop taking on new threads — reach a clean breakpoint"
         " and run the compact-loop skill (Skill tool, name=compact-loop) to"
         " re-set in-session and continue."),
    (75, "Context ≥75%: start steering toward a clean breakpoint to re-set"
         " at; don't start heavy chunks that could overshoot 85% mid-task."),
    (60, "Context ≥60%: value-zone note — from here a compact at a clean"
         " breakpoint pays back within ~2 requests (measured 2026-08-30:"
         " post-compact fill ≈7.6% of window, re-accumulation ≈1.8K"
         " tokens/turn). No urgency — finish the thread in hand, and ignore"
         " this if the session is wrapping up; just don't ride past a clean"
         " breakpoint to 75/85% out of inertia."),
]

# JA rendering of the CTX_BANDS advisory text, keyed by the same threshold
# (band[0]). additionalContext keeps the CTX_BANDS EN text verbatim (CLAUDE.md
# documents those exact labels for the agent); this is systemMessage-only.
SUB_CTX_BANDS = [
    (85, "Context ≥85%: stop taking on new threads. Write your handoff /"
         " durable state to disk NOW and tell your leader your load in your"
         " next message — ask to be rotated; you cannot compact yourself."),
    (75, "Context ≥75%: report your context load to your leader in your next"
         " message and begin handoff prep — write durable state to disk as"
         " you go."),
    (60, "Context ≥60%: past the value-zone line — state your current fill in"
         " your next report to your spawner so it can weigh a planned rotation"
         " at a round boundary. Keep working; no urgency."),
]
SUB_CTX_BANDS_JA = {
    85: "コンテキスト使用率 ≥85%: 新規着手を止め、handoff/永続状態を今すぐ"
        "ディスクに書き、次のメッセージでリーダーに使用率を伝えて交代を"
        "求めてください (subagent は自分では compact できません)。",
    75: "コンテキスト使用率 ≥75%: 次のメッセージでリーダーに使用率を報告し、"
        "handoff 準備 (永続状態のディスク書き出し) を始めてください。",
    60: "コンテキスト使用率 ≥60%: バリューゾーンの線を越えました。次の報告で"
        "現在の使用率を spawn 元に伝えてください (区切りでの計画交代の判断"
        "材料になります)。作業はそのまま続けて構いません。",
}

# Leader-side watch over ACTIVE subagents' context fill (transcript mtime
# within SUBWATCH_ACTIVE_S). Nothing else surfaces a worker's remaining
# window to the leader: completion notifications carry cumulative spend,
# and a worker's own ≥75% report is model-compliance. Warned once per
# (worker, band) via the notified-set state (segment ``subctx:<file>``);
# dropping below the band re-arms it.
SUBWATCH_ACTIVE_S = 900
SUBWATCH_TAIL_BYTES = 131072
SUBWATCH_BANDS = [
    (95, "compaction-imminent — direct it to STOP and hand off immediately"),
    (85, "direct it to write its handoff and rotate now"),
    (75, "expect its load report; plan its handoff/rotation at the next"
         " round boundary"),
    (60, "consider a planned rotation at a round boundary: every request"
         " re-bills the whole fill at the cache-read rate, so a successor"
         " built from a handoff typically pays for itself within ~a dozen"
         " requests — weigh that against the implicit context a handoff"
         " cannot carry"),
]
SUBWATCH_BANDS_JA = {
    95: "compaction 目前 — 即時停止と handoff を指示してください",
    85: "handoff を書かせて交代させてください",
    75: "負荷報告が来るはずです。次の区切りで handoff/交代を計画してください",
    60: "区切りでの計画交代を検討してください (毎リクエスト充填全量に"
        " cache-read 料金がかかるため、handoff からの後継は十数リクエスト"
        "程度で元が取れます。handoff に書けない暗黙知の価値と天秤に)",
}

CTX_BANDS_JA = {
    85: "コンテキスト使用率 ≥85%: 新しい thread の着手を止め、きりの良い"
        " breakpoint まで進めてから compact-loop skill (Skill tool,"
        " name=compact-loop) を実行してセッション内で re-set し、続行して"
        "ください。",
    75: "コンテキスト使用率 ≥75%: きりの良い breakpoint に向けて舵を切り"
        "始めてください。途中で 85% を超えかねない重い作業には着手しない"
        "でください。",
    60: "コンテキスト使用率 ≥60%: バリューゾーンに入りました。ここから先は"
        "きりの良い所で compact すれば約 2 リクエストで元が取れます (実測:"
        " compact 直後は窓の約 7.6%、再蓄積は毎 turn 約 1.8K token と緩やか)。"
        "急ぎではありません — 手元の作業を先に片付け、セッションを畳む直前"
        "なら無視して構いません。ただし、きりの良い breakpoint を惰性で"
        "素通りして 75/85% まで乗り続けるのは避けてください。",
}



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


def extract_window(snap: dict | None) -> int | None:
    """Effective context window size in tokens (e.g. 1000000 or 200000) from
    the statusLine snapshot, or ``None`` when unavailable. This is the only
    deterministic source — JSONL ``message.model`` strips the ``[1m]`` suffix
    (verified 2026-04-16 & 2026-05-27), so we can't infer window from
    transcript alone."""
    if not snap:
        return None
    try:
        cw = (snap.get("parsed") or {}).get("context_window") or {}
        size = cw.get("context_window_size")
        return int(size) if size else None
    except Exception:
        return None


def fmt_window(size: int) -> str:
    """1000000 → '1M', 200000 → '200K', else 'N tok'."""
    if size >= 1_000_000:
        return f"{size // 1_000_000}M"
    if size >= 1000:
        return f"{size // 1000}K"
    return f"{size} tok"


def subagent_transcript_path(leader_path: str, agent_id: str) -> str:
    """Subagent JSONLs live at ``<leader_sid>/subagents/agent-<aid>.jsonl``
    relative to the leader's transcript file. PostToolUse passes the leader's
    path even for subagent fires, so we derive the sub path manually."""
    base, _ = os.path.splitext(leader_path)
    return os.path.join(base, "subagents", f"agent-{agent_id}.jsonl")


def usage_from_tail(path: str, max_bytes: int = SUBWATCH_TAIL_BYTES):
    """(tokens, model) from the newest assistant usage entry in the file's
    tail, or (None, "") — the cheap variant of find_latest_usage for the
    leader-side subagent watch, which runs on every leader fire."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - max_bytes))
            blob = f.read()
    except Exception:
        return None, ""
    tokens, model = None, ""
    for line in blob.split(b"\n"):
        if b'"usage"' not in line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") != "assistant":
            continue
        msg = d.get("message") or {}
        u = msg.get("usage") or {}
        t = ((u.get("input_tokens") or 0)
             + (u.get("cache_creation_input_tokens") or 0)
             + (u.get("cache_read_input_tokens") or 0))
        if t:
            tokens = t
            model = str(msg.get("model") or "")
    return tokens, model


def worker_display_name(filename: str) -> str:
    """`agent-atopo-restart-40b9e4a4afdbc3a0.jsonl` -> `topo-restart`;
    an unnamed `agent-a<16hex>.jsonl` keeps its hex id."""
    stem = filename[6:-6] if filename.startswith("agent-") and \
        filename.endswith(".jsonl") else filename
    import re as _re
    m = _re.fullmatch(r"a(.+)-[0-9a-f]{16}", stem)
    return m.group(1) if m else stem


def subagent_watch(leader_path: str, consider, now: float):
    """Threshold-crossing warnings for ACTIVE subagents' context fill.
    Returns (en_lines, ja_lines)."""
    base, _ = os.path.splitext(leader_path)
    subdir = os.path.join(base, "subagents")
    en, ja = [], []
    try:
        names = os.listdir(subdir)
    except Exception:
        return en, ja
    for fn in names:
        if not (fn.startswith("agent-") and fn.endswith(".jsonl")):
            continue
        fp = os.path.join(subdir, fn)
        try:
            if now - os.path.getmtime(fp) > SUBWATCH_ACTIVE_S:
                continue
        except Exception:
            continue
        tokens, model = usage_from_tail(fp)
        if not tokens:
            continue
        window = window_for_subagent(model)
        pct = tokens / window * 100
        band = next(((t, txt) for t, txt in SUBWATCH_BANDS if pct >= t), None)
        if not consider(f"subctx:{fn}", band[0] if band else 0) or not band:
            continue
        name = worker_display_name(fn)
        en.append(f"⚠ Subagent '{name}' context ~{pct:.0f}% "
                  f"(~{tokens // 1000}K/{fmt_window(window)}) crossed "
                  f"{band[0]}% — {band[1]}.")
        ja.append(f"⚠ subagent '{name}' のコンテキストが約{pct:.0f}% "
                  f"(約{tokens // 1000}K/{fmt_window(window)}) で {band[0]}% を"
                  f"超えました — {SUBWATCH_BANDS_JA.get(band[0], '')}。")
    return en, ja


def window_for_subagent(model: str) -> int:
    """Coarse model-family → context-window rule for subagents (the statusLine
    snapshot with the [1m] suffix is leader-scoped, so subs need this fallback).
    1M family: opus, fable, mythos (2026-06-11 fix — fable subs were falling to
    the 200K branch and being told 85-99% at ~170-198K real tokens, causing
    premature wrap-ups), sonnet (2026-07-10 fix — a sonnet-5 sub measured fully
    functional at 105%+ of the assumed 200K ceiling, i.e. ~210-230K real tokens;
    window-probe).
    Haiku stays 200K until measured otherwise.
    Ignores `ANTHROPIC_DEFAULT_*` env-var inheritance — coarse but stable."""
    m = (model or "").lower()
    return 1_000_000 if any(k in m for k in ("opus", "fable", "mythos", "sonnet")) else 200_000


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


def extract_fable_limit(model: str) -> dict | None:
    """Fable-scoped weekly limit (the Fable-only share of the subscription
    weekly budget) from the oauth-usage cache, in the same ``{"pct", "reset"}``
    shape as extract_limits. Returns None — and thus injects nothing — unless
    THIS agent's model is Fable/Mythos: the injection is for the model that is
    actually drawing on the Fable budget."""
    m = (model or "").lower()
    if not ("fable" in m or "mythos" in m):
        return None
    try:
        with open(OAUTH_CACHE_PATH, "r", encoding="utf-8") as f:
            cache = json.load(f)
        import time as _time
        if _time.time() - float(cache.get("ts", 0)) > OAUTH_CACHE_MAX_AGE:
            return None
        for lim in (cache.get("data") or {}).get("limits") or []:
            if lim.get("kind") != "weekly_scoped":
                continue
            name = str(((lim.get("scope") or {}).get("model") or {}).get("display_name") or "")
            if "fable" not in name.lower():
                continue
            reset = datetime.fromisoformat(lim["resets_at"]).astimezone().replace(tzinfo=None)
            if reset <= datetime.now():
                return None  # rolled-over window, value not refreshed yet
            return {"pct": float(lim["percent"]), "reset": reset}
    except Exception:
        pass
    return None


def fmt_limit(label: str, data: dict) -> str:
    """e.g. '5h 30% in 2h25m (rsts 05-11 00:40 +0900)' — local time + UTC offset"""
    now = datetime.now()
    return (
        f"{label} {data['pct']:.0f}% {fmt_rel(data['reset'] - now)}"
        f" (rsts {data['reset'].strftime('%m-%d %H:%M')} {TZ_LABEL})"
    )


def collect_state(tokens: int, window: int | None,
                  h5: dict | None, d7: dict | None,
                  f7d: dict | None = None) -> dict:
    """Per-segment raw values used for dedup. Excludes time-relative fields.

    Four independent segments:
      ``ctx`` — context-window usage (integer % when window is known, raw
                tokens otherwise). Integer-% dedup means opus 1M re-emits
                roughly every 10K tokens instead of every tool call.
      ``h5``  — 5-hour rate-limit (percentage + reset epoch)
      ``d7``  — 7-day rate-limit (percentage + reset epoch)
      ``f7d`` — Fable-scoped weekly rate-limit (Fable/Mythos agents only)
    Each segment is dedup'd independently so a 5h tick doesn't redundantly
    re-emit the slow-moving 7d figure (or vice-versa).
    """
    ctx = {"p": int(round(tokens / window * 100))} if window else {"t": tokens}
    return {
        "ctx": ctx,
        "h5": {"p": round(h5["pct"]), "r": int(h5["reset"].timestamp())} if h5 else None,
        "d7": {"p": round(d7["pct"]), "r": int(d7["reset"].timestamp())} if d7 else None,
        "f7d": {"p": round(f7d["pct"]), "r": int(f7d["reset"].timestamp())} if f7d else None,
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

    agent_key = hook_input.get("agent_id") or "leader"
    is_subagent = "agent_id" in hook_input

    # PostToolUse hands us the LEADER's transcript_path even for subagent
    # fires (verified 2026-05-19). Swap to the sub's own JSONL so tokens and
    # model reflect the actual subagent, not the leader.
    read_path = (
        subagent_transcript_path(transcript_path, hook_input["agent_id"])
        if is_subagent else transcript_path
    )
    tokens, model = find_latest_usage(read_path)
    if tokens is None:
        return

    snap = read_snapshot()
    h5, d7 = extract_limits(snap)
    # Window source differs by agent: leader uses the statusLine snapshot
    # (which carries the [1m] suffix the JSONL strips); subagent falls back
    # to a model-family rule (opus → 1M, else → 200K) since the snapshot is
    # leader-scoped.
    window = window_for_subagent(model) if is_subagent else extract_window(snap)
    f7d = extract_fable_limit(model)
    current = collect_state(tokens, window, h5, d7, f7d)
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

    # Per-agent ctx: leader reads its own transcript, subagent reads its own
    # JSONL under <leader_sid>/subagents/ (re-enabled 2026-05-28). Dedup is
    # already per-agent via agent_key, but ctx values rarely collide across
    # agents anyway — they're independent token streams.
    emit_ctx = consider(f"ctx:{agent_key}", current["ctx"])
    emit_h5  = consider("h5", current["h5"])
    emit_d7  = consider("d7", current["d7"])
    emit_f7d = consider("f7d", current["f7d"])

    h5_critical = h5 is not None and h5["pct"] >= H5_CRITICAL
    d7_critical = d7 is not None and d7["pct"] >= D7_CRITICAL

    # Context band advisory (leader only, window required). consider() fires
    # once per band entry; band 0 (below all thresholds) carries no text but
    # still updates state so re-entering a band re-emits its advisory.
    band_text = None
    band_text_ja = None
    pct_now = tokens / window * 100 if window else None
    if pct_now is not None:
        bands = SUB_CTX_BANDS if is_subagent else CTX_BANDS
        bands_ja = SUB_CTX_BANDS_JA if is_subagent else CTX_BANDS_JA
        band = next(((t, txt) for t, txt in bands if pct_now >= t), None)
        if consider(f"ctxadv:{agent_key}", band[0] if band else 0) and band:
            band_text = band[1]
            band_text_ja = bands_ja.get(band[0])

    # Leader-side watch over active subagents' context fill.
    watch_en: list[str] = []
    watch_ja: list[str] = []
    if not is_subagent:
        try:
            watch_en, watch_ja = subagent_watch(
                transcript_path, consider, time.time())
        except Exception:
            watch_en, watch_ja = [], []

    tip_text = None
    tip_text_ja = None
    tip_dirty = False

    if not (emit_ctx or emit_h5 or emit_d7 or emit_f7d or h5_critical
            or d7_critical or band_text or tip_text or watch_en):
        # Nothing to say, but a moved delegtip baseline must still persist.
        if tip_dirty:
            try:
                os.makedirs(STATE_DIR, exist_ok=True)
                with open(sp, "w", encoding="utf-8") as f:
                    json.dump(state, f)
            except Exception:
                pass
        return  # nothing new for this agent and no critical threshold

    segments: list[str] = []
    segments_ja: list[str] = []
    if emit_ctx:
        if window:
            pct = tokens / window * 100
            segments.append(f"Context used: {pct:.0f}%")
            segments_ja.append(f"コンテキスト使用率: {pct:.0f}%")
        else:
            segments.append(f"Context used: {tokens:,} tokens")
            segments_ja.append(f"コンテキスト使用量: {tokens:,} トークン")

    rl_parts: list[str] = []
    if (emit_h5 or h5_critical) and h5:
        rl_parts.append(fmt_limit("5h", h5))
    if (emit_d7 or d7_critical) and d7:
        rl_parts.append(fmt_limit("7d", d7))
    if emit_f7d and f7d:
        rl_parts.append(fmt_limit("Fable7d", f7d))
    if rl_parts:
        # fmt_limit's own output (percentages, "in Xh Ym", "(rsts ... +HHMM)")
        # stays identical in both languages — only the segment label differs.
        segments.append("Limits used: " + ", ".join(rl_parts))
        segments_ja.append("使用量: " + "、".join(rl_parts))

    advisory_parts: list[str] = []
    advisory_parts_ja: list[str] = []
    if h5_critical:
        advisory_parts.append(f"5h limit ≥{H5_CRITICAL}%")
        advisory_parts_ja.append(f"5h 制限 ≥{H5_CRITICAL}%")
    if d7_critical:
        advisory_parts.append(f"7d limit ≥{D7_CRITICAL}%")
        advisory_parts_ja.append(f"7d 制限 ≥{D7_CRITICAL}%")
    if advisory_parts:
        segments.append("⚠️ " + ", ".join(advisory_parts) + ". Invoke the limit-wait skill NOW (Skill tool, name=limit-wait) to wait out the reset in-session and auto-resume — do NOT stop for the user.")
        segments_ja.append("⚠️ " + "、".join(advisory_parts_ja) + "。今すぐ limit-wait skill を起動してください (Skill tool, name=limit-wait) — セッション内で reset を待って自動再開します。user 待ちで止まらないでください。")

    if band_text:
        segments.append("🔄 " + band_text)
        segments_ja.append("🔄 " + (band_text_ja or band_text))

    if tip_text:
        segments.append("💡 " + tip_text)
        segments_ja.append("💡 " + (tip_text_ja or tip_text))

    segments.extend(watch_en)
    segments_ja.extend(watch_ja)

    if not segments:
        return

    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(sp, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception:
        pass

    msg = "ℹ️ " + " | ".join(segments)
    msg_ja = "ℹ️ " + " | ".join(segments_ja)
    sys.stdout.write(json.dumps({
        "systemMessage": msg_ja if USER_LANG == "ja" else msg,
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": msg,
        },
    }))


if __name__ == "__main__":
    main()
