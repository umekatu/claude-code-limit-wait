"""
limit-wait.py — autonomous in-session wait across a rate-limit reset.

Run via Bash with run_in_background:true. The script blocks (polling, no model
inference) until the binding rate-limit's reset time has passed, then exits.
When it exits, Claude Code re-invokes the agent WITH FULL CONTEXT PRESERVED
(no checkpoint needed — the conversation is still loaded). Token consumption
during the wait is structurally zero (no model turns occur while it polls).

Reads ~/.claude/usage-snapshot.json for rate_limits (account-level data,
session-agnostic). session_id comes from --session-id, which the
compact-handoff-guard.py PreToolUse hook auto-injects on Bash launches.

Early-reset detection: the server occasionally clears a limit BEFORE the
advertised resets_at (out-of-schedule resets). During the wait loop the
script polls GET /api/oauth/usage every EARLY_CHECK_SEC seconds; a cleared
result is rechecked after EARLY_RECHECK_SEC (guard against transient API
flaps), and once the waited bucket reports cleared EARLY_CONFIRM times in a
row the wait ends immediately instead of sleeping to the original target.
The exit JSON carries "early_reset": true.

Verify phase: reaching resets_at + buffer on the wall clock does NOT
guarantee the server has cleared the limit (observed 2026-07-10: a wake at
reset+61s was rejected with "You've hit your session limit", killing the
one-shot auto-resume — only a user message revived the session). So after
the wall-clock target, the script polls GET /api/oauth/usage (via
oauth-usage-probe.py's fetch) every VERIFY_POLL seconds until the waited
bucket reports cleared — percent below critical OR resets_at advanced past
the original target — and only then exits. Capped at --verify-max seconds;
on cap or persistent fetch failure it exits anyway (a possibly-failing wake
attempt beats never waking) with "verified": false in the status JSON.

Usage (background Bash):
  python ~/.claude/hooks/limit-wait.py --session-id "<id>" --limit auto
  python ~/.claude/hooks/limit-wait.py --session-id "<id>" --limit 5h --buffer 60
  python ~/.claude/hooks/limit-wait.py --session-id "<id>" --simulate-seconds 75

Exit codes:
  0  reset reached (status reset_reached / nothing_to_wait)
  3  aborted: wait longer than --max-wait (caller should fall back to notify)
  4  error_no_session_id / error_no_snapshot

The single final stdout line is a JSON status object — the re-invoked agent
reads it from the background task .output file. Live state is written to
~/.claude/.limit-wait-state.json as a dict keyed by session_id (so concurrent
sessions don't stomp each other), with own entry removed on exit. Stale
entries (now_epoch heartbeat older than STATE_PRUNE_SEC) pruned per write.
"""
import sys, os, json, time, argparse

try:
    sys.stdout.reconfigure(encoding='utf-8')  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding='utf-8')  # type: ignore[attr-defined]
except Exception:
    pass

SNAPSHOT_PATH = os.path.expanduser('~/.claude/usage-snapshot.json')
STATE_PATH = os.path.expanduser('~/.claude/.limit-wait-state.json')

# Critical thresholds mirror context-monitor.py (H5_CRITICAL / D7_CRITICAL);
# keep the two files in sync or the hook's advisory and this script's auto
# mode disagree (advisory fires → nothing_to_wait → advisory loops).
H5_CRITICAL = 95
D7_CRITICAL = 99
MAX_WAIT_DEFAULT = 8 * 24 * 3600  # 8 days hard sanity cap
POLL_STEP = 30  # seconds between wall-clock re-checks
STATE_PRUNE_SEC = 5 * 60  # entry stale if now_epoch heartbeat older than this
VERIFY_POLL = 30  # seconds between server-side clearance checks
VERIFY_MAX_DEFAULT = 15 * 60  # give up verifying and wake anyway after this
EARLY_CHECK_SEC = 60 * 60  # mid-wait server poll interval (early-reset watch)
EARLY_RECHECK_SEC = 5 * 60  # confirmation recheck delay after a cleared result
EARLY_CONFIRM = 2  # consecutive cleared results required to trust an early reset
# GET /api/oauth/usage bucket kinds for the limits this script waits on.
OAUTH_KIND = {'5h': 'session', '7d': 'weekly_all'}


def load_oauth_fetch():
    """Import fetch_usage() from the sibling oauth-usage-probe.py (dashed
    filename → importlib). Returns the callable, or None when unavailable
    (missing file / credentials) — verify phase then degrades to skipped."""
    try:
        import importlib.util
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         'oauth-usage-probe.py')
        spec = importlib.util.spec_from_file_location('oauth_usage_probe', p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.fetch_usage
    except Exception:
        return None


def limit_cleared(fetch, label, orig_reset_epoch):
    """Ask the server whether the waited limit is really gone.

    True  — bucket shows percent below its critical threshold, OR its
            resets_at has advanced past the original target (new window).
    False — bucket still at/over critical with the old reset time.
    None  — fetch failed this round (network/auth); caller keeps polling.
    A missing bucket counts as cleared (nothing binding)."""
    from datetime import datetime
    try:
        data = fetch()
    except Exception:
        return None
    threshold = H5_CRITICAL if label == '5h' else D7_CRITICAL
    for lim in (data.get('limits') or []):
        if lim.get('kind') != OAUTH_KIND.get(label):
            continue
        pct = lim.get('percent')
        if isinstance(pct, (int, float)) and pct < threshold:
            return True
        try:
            server_reset = datetime.fromisoformat(lim['resets_at']).timestamp()
            if server_reset > orig_reset_epoch + 60:
                return True
        except Exception:
            pass
        return False
    return True


def emit(obj, code):
    """Print one machine-readable JSON line and exit."""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    sys.exit(code)


def update_state(session_id, entry, remove=False):
    """Read state, prune entries whose now_epoch heartbeat is stale, then
    upsert or remove the entry for ``session_id``. State file is a dict keyed
    by session_id so concurrent sessions don't stomp each other.

    Best-effort: any I/O error is swallowed (introspection only — the exit
    JSON via stdout is the authoritative final record)."""
    state = {}
    try:
        with open(STATE_PATH, encoding='utf-8') as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            state = loaded
    except Exception:
        pass
    now = time.time()
    cutoff = now - STATE_PRUNE_SEC
    for k in list(state.keys()):
        e = state.get(k)
        if not isinstance(e, dict):
            del state[k]
            continue
        t = e.get('now_epoch')
        if not isinstance(t, (int, float)) or t < cutoff:
            del state[k]
    if remove:
        state.pop(session_id, None)
    else:
        state[session_id] = entry
    try:
        with open(STATE_PATH, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception:
        pass


def pick_target(limit, buffer):
    """Return (resets_at_epoch, label) for the limit to wait on, or raise.
    Reads rate_limits from the snapshot — account-level data, identical
    across sessions, so no leakage issue."""
    with open(SNAPSHOT_PATH, encoding='utf-8') as f:
        rl = json.load(f)['parsed']['rate_limits']
    h5 = rl.get('five_hour') or {}
    d7 = rl.get('seven_day') or {}

    if limit == '5h':
        return float(h5['resets_at']), '5h'
    if limit == '7d':
        return float(d7['resets_at']), '7d'

    # auto: wait on whichever limit is over its critical threshold; if both,
    # wait on the one that resets LATEST (the binding constraint).
    cands = []
    if h5.get('used_percentage', 0) >= H5_CRITICAL:
        cands.append((float(h5['resets_at']), '5h'))
    if d7.get('used_percentage', 0) >= D7_CRITICAL:
        cands.append((float(d7['resets_at']), '7d'))
    if not cands:
        return None, 'none'
    return max(cands, key=lambda c: c[0])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--session-id',
                    help='session_id to scope state-file write (required)')
    ap.add_argument('--limit', choices=['5h', '7d', 'auto'], default='auto')
    ap.add_argument('--buffer', type=int, default=60,
                    help='seconds to wait past resets_at')
    ap.add_argument('--max-wait', type=int, default=MAX_WAIT_DEFAULT)
    ap.add_argument('--verify-max', type=int, default=VERIFY_MAX_DEFAULT,
                    help='max seconds to poll the server for actual '
                         'clearance after the wall-clock target')
    ap.add_argument('--simulate-seconds', type=int, default=None,
                    help='ignore snapshot; just wait N seconds (test path)')
    args = ap.parse_args()

    if not args.session_id:
        emit({'status': 'error_no_session_id',
              'note': '--session-id is required'}, 4)
    session_id = args.session_id

    now = time.time()

    if args.simulate_seconds is not None:
        target = now + args.simulate_seconds
        label = 'simulate'
    else:
        try:
            resets_at, label = pick_target(args.limit, args.buffer)
        except Exception as e:
            emit({'status': 'error_no_snapshot', 'detail': str(e)}, 4)
        if label == 'none':
            emit({'status': 'nothing_to_wait',
                  'note': 'no limit over critical threshold'}, 0)
        target = resets_at + args.buffer

    wait_s = target - now
    # wait_s <= 0 falls through: the sleep loop exits immediately and the
    # verify phase still runs — a relaunch right after a rejected wake must
    # not exit unverified into the same propagation-lag window.
    if wait_s > args.max_wait:
        emit({'status': 'abort_too_long', 'limit': label,
              'wait_seconds': round(wait_s), 'max_wait': args.max_wait}, 3)

    started = now
    # Early-reset watch: mid-wait server polls catching an out-of-schedule
    # clearance so the session wakes now instead of at the advertised target.
    early_fetch = None
    if args.simulate_seconds is None and label in OAUTH_KIND:
        early_fetch = load_oauth_fetch()
    next_check = started + EARLY_CHECK_SEC
    cleared_streak = 0
    early_reset = False
    while True:
        now = time.time()
        remaining = target - now
        if remaining <= 0:
            break
        if early_fetch is not None and now >= next_check:
            if limit_cleared(early_fetch, label, resets_at) is True:
                cleared_streak += 1
            else:
                cleared_streak = 0
            if cleared_streak >= EARLY_CONFIRM:
                early_reset = True
                break
            # A cleared result gets its confirmation recheck soon; otherwise
            # resume the slow watch cadence.
            next_check = now + (EARLY_RECHECK_SEC if cleared_streak
                                else EARLY_CHECK_SEC)
        update_state(session_id, {
            'pid': os.getpid(), 'session_id': session_id, 'limit': label,
            'status': 'waiting',
            'started_epoch': round(started), 'target_epoch': round(target),
            'now_epoch': round(now), 'remaining_seconds': round(remaining),
        })
        time.sleep(min(POLL_STEP, remaining))

    # Verify phase: wall clock passed the target, but the server may still
    # be enforcing the limit (reset propagation lag). Poll live usage until
    # the bucket really cleared, capped at --verify-max.
    verified = None
    if args.simulate_seconds is None and label in OAUTH_KIND:
        fetch = load_oauth_fetch()
        verify_deadline = time.time() + args.verify_max
        while fetch is not None:
            verified = limit_cleared(fetch, label, resets_at)
            now = time.time()
            if verified is True or now >= verify_deadline:
                break
            update_state(session_id, {
                'pid': os.getpid(), 'session_id': session_id, 'limit': label,
                'status': 'verifying',
                'started_epoch': round(started), 'target_epoch': round(target),
                'now_epoch': round(now),
                'verify_deadline_epoch': round(verify_deadline),
            })
            time.sleep(min(VERIFY_POLL, verify_deadline - now))

    waited = round(time.time() - started)
    update_state(session_id, None, remove=True)
    emit({'status': 'reset_reached', 'limit': label,
          'verified': verified, 'early_reset': early_reset,
          'started_epoch': round(started), 'waited_seconds': waited,
          'target_epoch': round(target)}, 0)


if __name__ == '__main__':
    main()
