"""
StatusLine probe — captures the JSON Claude Code feeds into stdin and writes a
single-snapshot file (overwritten on each fire) so the model / hooks can read
the latest cost / rate-limit / context-window state without growing a log.

Also emits a one-line status string back to stdout so the user sees something
useful in their UI.

Revert: remove the `statusLine` block from the relevant settings.json.
Snapshot path: ~/.claude/usage-snapshot.json
"""
import sys, json, os, time

SNAPSHOT_PATH = os.path.expanduser('~/.claude/usage-snapshot.json')

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
    model = (parsed.get('model') or {}).get('display_name') or (parsed.get('model') or {}).get('id')
    if model:
        parts.append(str(model))
    sys.stdout.write(' | '.join(parts) if parts else '(usage-probe active)')
