"""
StatusLine probe — captures the JSON Claude Code feeds into stdin and writes a
single-snapshot file (overwritten on each fire) so the model / hooks can read
the latest cost / rate-limit / context-window state without growing a log.

Also emits a one-line status string back to stdout so the user sees something
useful in their UI.

The Fable-scoped weekly limit is NOT in the stdin payload — it is displayed
from ~/.claude/.oauth-usage-cache.json (written by oauth-usage-probe.py
--refresh-cache). When the cache is stale this script spawns the refresh as a
detached background process; the status line itself never blocks on network.

Revert: remove the `statusLine` block from the relevant settings.json.
Snapshot path: ~/.claude/usage-snapshot.json
"""
import sys, json, os, time

SNAPSHOT_PATH = os.path.expanduser('~/.claude/usage-snapshot.json')
CACHE_PATH = os.path.expanduser('~/.claude/.oauth-usage-cache.json')
REFRESH_STAMP = os.path.expanduser('~/.claude/.oauth-usage-refresh-stamp')
PROBE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'oauth-usage-probe.py')
CACHE_TTL = 300        # spawn a refresh when the cache is older than this
CACHE_MAX_AGE = 1800   # hide the segment entirely when older than this
SPAWN_COOLDOWN = 60    # min seconds between refresh spawn attempts


def _spawn_cache_refresh(now):
    """Detached, rate-limited background refresh of the oauth-usage cache."""
    try:
        try:
            last = os.path.getmtime(REFRESH_STAMP)
        except OSError:
            last = 0
        if now - last < SPAWN_COOLDOWN:
            return
        with open(REFRESH_STAMP, 'w') as f:
            f.write(str(now))
        import subprocess
        flags = 0
        if os.name == 'nt':
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
        subprocess.Popen(
            [sys.executable, PROBE_PATH, '--refresh-cache'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=flags)
    except Exception:
        pass


def fable_weekly_pct():
    """Fable-scoped weekly percent from the oauth-usage cache, or None when
    the cache is missing, too old, or the limit window already rolled over."""
    now = time.time()
    pct = None
    age = None
    try:
        with open(CACHE_PATH, encoding='utf-8') as f:
            cache = json.load(f)
        age = now - float(cache.get('ts', 0))
        if age <= CACHE_MAX_AGE:
            from datetime import datetime, timezone
            for lim in (cache.get('data') or {}).get('limits') or []:
                if lim.get('kind') != 'weekly_scoped':
                    continue
                name = str(((lim.get('scope') or {}).get('model') or {}).get('display_name') or '')
                if 'fable' not in name.lower():
                    continue
                ra = lim.get('resets_at')
                if ra:
                    try:
                        if datetime.fromisoformat(ra) <= datetime.now(timezone.utc):
                            break  # rolled-over window, value not refreshed yet
                    except ValueError:
                        pass
                pct = lim.get('percent')
                break
    except Exception:
        pass
    if age is None or age > CACHE_TTL:
        _spawn_cache_refresh(now)
    return pct

raw = sys.stdin.read()
ts = time.strftime('%Y-%m-%dT%H:%M:%S')

try:
    parsed = json.loads(raw)
except Exception:
    parsed = None

# Drop stale rate_limits entries: if resets_at is already past, the upstream
# value is from a rolled-over window and not yet refreshed. Suppressing them
# here keeps both the snapshot (read by limit-wait.py) and the status line
# display from acting on outdated values.
if isinstance(parsed, dict) and isinstance(parsed.get('rate_limits'), dict):
    _now = time.time()
    _rl = parsed['rate_limits']
    for _k in list(_rl.keys()):
        _entry = _rl.get(_k) or {}
        _ra = _entry.get('resets_at')
        try:
            if _ra is not None and float(_ra) <= _now:
                del _rl[_k]
        except (TypeError, ValueError):
            pass

snapshot = {'ts': ts, 'parsed': parsed, 'raw': raw if parsed is None else None}
try:
    with open(SNAPSHOT_PATH, 'w', encoding='utf-8') as f:
        json.dump(snapshot, f, ensure_ascii=False)
except Exception:
    pass

def fmt_pct(v):
    try:
        return f"{float(v):.0f}%"
    except Exception:
        return f"{v}"

def fmt_cost(v):
    try:
        return f"${float(v):.3f}"
    except Exception:
        return f"${v}"

if parsed is None:
    sys.stdout.write('(usage-probe parse-fail)')
else:
    parts = []
    cost = (parsed.get('cost') or {}).get('total_cost_usd')
    if cost is not None:
        parts.append(fmt_cost(cost))
    rl = parsed.get('rate_limits') or {}
    if isinstance(rl, dict):
        if 'five_hour' in rl:
            parts.append(f"5h:{fmt_pct((rl['five_hour'] or {}).get('used_percentage'))}")
        if 'seven_day' in rl:
            parts.append(f"7d:{fmt_pct((rl['seven_day'] or {}).get('used_percentage'))}")
    f7d = fable_weekly_pct()
    if f7d is not None:
        parts.append(f"F7d:{fmt_pct(f7d)}")
    model = (parsed.get('model') or {}).get('display_name') or (parsed.get('model') or {}).get('id')
    if model:
        parts.append(str(model))
    sys.stdout.write(' | '.join(parts) if parts else '(usage-probe active)')
