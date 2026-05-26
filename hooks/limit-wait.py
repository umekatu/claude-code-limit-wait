"""
limit-wait.py — autonomous in-session wait across a rate-limit reset.

Run via Bash with run_in_background:true. The script blocks (polling, no model
inference) until the binding rate-limit's reset time has passed, then exits.
When it exits, Claude Code re-invokes the agent WITH FULL CONTEXT PRESERVED
(no checkpoint needed — the conversation is still loaded). Token consumption
during the wait is structurally zero (no model turns occur while it polls).

Reads ~/.claude/usage-snapshot.json (written by usage-probe-statusline.py).

Usage (background Bash):
  python ~/.claude/hooks/limit-wait.py --limit auto
  python ~/.claude/hooks/limit-wait.py --limit 5h --buffer 60
  python ~/.claude/hooks/limit-wait.py --simulate-seconds 75   # test path

Exit codes:
  0  reset reached (status reset_reached / already_reset / nothing_to_wait)
  3  aborted: wait longer than --max-wait (caller should fall back to notify)
  4  error: snapshot missing/unreadable (caller should fall back to notify)

The single final stdout line is a JSON status object — the re-invoked agent
reads it from the background task .output file. Live state is written to
~/.claude/.limit-wait-state.json as a dict keyed by session_id (so concurrent
sessions don't stomp each other), with own entry removed on exit. Stale
entries (now_epoch heartbeat older than STATE_PRUNE_SEC) pruned per write.
"""
import sys, os, json, time, argparse

SNAPSHOT_PATH = os.path.expanduser('~/.claude/usage-snapshot.json')
STATE_PATH = os.path.expanduser('~/.claude/.limit-wait-state.json')

# Critical thresholds mirror context-monitor.py (H5_CRITICAL / D7_CRITICAL).
H5_CRITICAL = 90
D7_CRITICAL = 97
MAX_WAIT_DEFAULT = 8 * 24 * 3600  # 8 days hard sanity cap
POLL_STEP = 30  # seconds between wall-clock re-checks
STATE_PRUNE_SEC = 5 * 60  # entry stale if now_epoch heartbeat older than this


def emit(obj, code):
    """Print one machine-readable JSON line and exit."""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    sys.exit(code)


def get_session_id():
    try:
        with open(SNAPSHOT_PATH, encoding='utf-8') as f:
            d = json.load(f)
        return (d.get('parsed') or {}).get('session_id')
    except Exception:
        return None


def update_state(session_id, entry, remove=False):
    """Read state, prune entries whose now_epoch heartbeat is stale, then
    upsert or remove the entry for ``session_id``. State file is a dict keyed
    by session_id so concurrent sessions don't stomp each other.

    Best-effort: any I/O error is swallowed (introspection only — the exit
    JSON via stdout is the authoritative final record). If ``session_id`` is
    falsy, write is skipped entirely (limit-wait still runs, just without
    idle-guard integration)."""
    if not session_id:
        return
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
    """Return (resets_at_epoch, label) for the limit to wait on, or raise."""
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
    ap.add_argument('--limit', choices=['5h', '7d', 'auto'], default='auto')
    ap.add_argument('--buffer', type=int, default=60,
                    help='seconds to wait past resets_at')
    ap.add_argument('--max-wait', type=int, default=MAX_WAIT_DEFAULT)
    ap.add_argument('--simulate-seconds', type=int, default=None,
                    help='ignore snapshot; just wait N seconds (test path)')
    args = ap.parse_args()

    session_id = get_session_id()

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
    if wait_s <= 0:
        emit({'status': 'already_reset', 'limit': label,
              'target_epoch': target}, 0)
    if wait_s > args.max_wait:
        emit({'status': 'abort_too_long', 'limit': label,
              'wait_seconds': round(wait_s), 'max_wait': args.max_wait}, 3)

    started = now
    while True:
        now = time.time()
        remaining = target - now
        if remaining <= 0:
            break
        update_state(session_id, {
            'pid': os.getpid(), 'session_id': session_id, 'limit': label,
            'status': 'waiting',
            'started_epoch': round(started), 'target_epoch': round(target),
            'now_epoch': round(now), 'remaining_seconds': round(remaining),
        })
        time.sleep(min(POLL_STEP, remaining))

    waited = round(time.time() - started)
    update_state(session_id, None, remove=True)
    emit({'status': 'reset_reached', 'limit': label,
          'started_epoch': round(started), 'waited_seconds': waited,
          'target_epoch': round(target)}, 0)


if __name__ == '__main__':
    main()
