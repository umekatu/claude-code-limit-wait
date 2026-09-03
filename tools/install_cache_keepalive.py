#!/usr/bin/env python
"""install_cache_keepalive.py — register ~/.claude/hooks/cache-keepalive.py as a
Stop hook with asyncRewake in ~/.claude/settings.json (backup taken first).

  python install_cache_keepalive.py          # production: ping after 55 idle minutes
  python install_cache_keepalive.py --test   # trial: ping after 60 idle seconds
  python install_cache_keepalive.py --remove # unregister

The entry carries "timeout": 4000 (seconds): an async hook is killed at its
timeout like any other hook, and the Stop default of 600 s would end the
instance before the 55-minute ping.

Re-running replaces the existing cache-keepalive entry, so switching
between --test and production is one command each.
"""
import json
import os
import shutil
import sys
import time

settings_path = os.path.expanduser("~/.claude/settings.json")
script = os.path.expanduser("~/.claude/hooks/cache-keepalive.py").replace("\\", "/")
base_cmd = "PYTHONUTF8=1 PYTHONIOENCODING=utf-8 python " + script
mode = sys.argv[1] if len(sys.argv) > 1 else ""
cmd = base_cmd + (" --idle-seconds 60 --poll 5" if mode == "--test" else "")

if not os.path.exists(script):
    sys.exit(f"missing {script}")
with open(settings_path, encoding="utf-8") as f:
    settings = json.load(f)
backup = settings_path + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
shutil.copyfile(settings_path, backup)

stop_groups = settings.setdefault("hooks", {}).setdefault("Stop", [])
for g in stop_groups:
    g["hooks"] = [h for h in g.get("hooks", []) if "cache-keepalive.py" not in h.get("command", "")]
if mode != "--remove":
    if not stop_groups:
        stop_groups.append({"matcher": ".*", "hooks": []})
    stop_groups[0]["hooks"].append({
        "type": "command",
        "command": cmd,
        "timeout": 4000,
        "asyncRewake": True,
        "rewakeMessage": "[cache-keepalive] ℹ️ Routine cache refresh, nothing to do: ",
        "rewakeSummary": "ℹ️ prompt cache keep-alive (just reply ok)",
    })
with open(settings_path, "w", encoding="utf-8") as f:
    json.dump(settings, f, ensure_ascii=False, indent=2)
    f.write("\n")
print("backup:", backup)
print("removed cache-keepalive Stop hook" if mode == "--remove" else "Stop hook registered (timeout 4000s): " + cmd)
