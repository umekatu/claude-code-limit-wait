#!/usr/bin/env python
"""On-demand subscription usage probe via GET /api/oauth/usage.

Prints every rate-limit bucket the account exposes, including model-scoped
weekly limits (e.g. the Fable-only weekly budget) that the statusLine
payload does not carry. Dollar figures are not exposed on subscription
plans; utilization percent is the only cost signal.

Usage:
    python ~/.claude/hooks/oauth-usage-probe.py                  # compact summary
    python ~/.claude/hooks/oauth-usage-probe.py --json           # full raw response
    python ~/.claude/hooks/oauth-usage-probe.py --refresh-cache  # write cache file, no output

--refresh-cache writes ~/.claude/.oauth-usage-cache.json as
{"ts": <epoch>, "data": <full response>} for consumers that must not block
on the network (usage-probe-statusline.py displays from it; context-monitor.py
injects from it). On fetch failure the old cache is left untouched.

The OAuth token is read from ~/.claude/.credentials.json and used only in
the Authorization header — it is never printed.
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime

# Run outside settings.json, so nothing supplies PYTHONUTF8: a non-ASCII
# byte in an API response would otherwise raise on a cp932 console. Same
# guard as the other long-lived helpers (limit-wait.py).
try:
    sys.stdout.reconfigure(encoding='utf-8')  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding='utf-8')  # type: ignore[attr-defined]
except Exception:
    pass


CACHE_PATH = os.path.expanduser("~/.claude/.oauth-usage-cache.json")


def fetch_usage():
    cred_path = os.path.expanduser("~/.claude/.credentials.json")
    cred = json.load(open(cred_path, encoding="utf-8"))
    tok = cred.get("claudeAiOauth", {}).get("accessToken") or cred.get("accessToken")
    if not tok:
        raise RuntimeError("no OAuth access token in credentials file")
    req = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        headers={
            "Authorization": "Bearer " + tok,
            "Content-Type": "application/json",
            "anthropic-beta": "oauth-2025-04-20",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def fmt_reset(iso):
    if not iso:
        return "?"
    try:
        dt = datetime.fromisoformat(iso)
        return dt.astimezone().strftime("%m-%d %H:%M %z")  # local time + UTC offset
    except ValueError:
        return iso


def refresh_cache():
    """Fetch and overwrite the cache file. Silent: consumers keep the old
    cache on failure; a temp-file rename keeps readers from seeing a torn
    write."""
    try:
        data = fetch_usage()
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "data": data}, f, ensure_ascii=False)
        os.replace(tmp, CACHE_PATH)
        return 0
    except Exception:
        return 1


def main():
    if "--refresh-cache" in sys.argv:
        return refresh_cache()

    try:
        data = fetch_usage()
    except Exception as e:  # network / auth / parse — report and fail loud
        print(f"usage fetch failed: {e}", file=sys.stderr)
        return 1

    if "--json" in sys.argv:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0

    for lim in data.get("limits") or []:
        kind = lim.get("kind", "?")
        scope = lim.get("scope") or {}
        model = (scope.get("model") or {}).get("display_name")
        label = f"{kind}({model})" if model else kind
        print(
            f"{label:<22} {lim.get('percent', '?'):>3}% used"
            f"  resets {fmt_reset(lim.get('resets_at'))}"
            f"  severity={lim.get('severity', '?')}"
        )

    extra = data.get("extra_usage") or {}
    if extra.get("is_enabled"):
        print(
            f"{'extra_usage':<22} {extra.get('utilization', 0):>3}% used"
            f"  ({extra.get('used_credits', 0)}/{extra.get('monthly_limit', '?')}"
            f" {extra.get('currency', '')})"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
