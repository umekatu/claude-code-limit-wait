#!/usr/bin/env python3
"""PreToolUse hook: session_id / home-cwd resolution for scripts that
key on them.

session_id comes straight from the hook's stdin payload. The home cwd
(the session's true launch directory) is resolved by `_home_cwd()`,
which scans the session's own transcript file (payload
`transcript_path`) forward for the first entry carrying a `cwd`
field — NOT read directly from the payload's own `cwd` field, which
tracks the Bash tool's live, `cd`-able subprocess directory and drifts
away from the session's true root over a long, `cd`-heavy session.
Every branch below is keyed on the resolved home cwd, falling back to
the payload's raw `cwd` only if the transcript is unreadable.

(A) Subagent /compact via CronCreate → BLOCK (would compact the leader).
(B) compact-handoff/dump.py missing --session-id / --cwd → BLOCK
    (agent needs the value back to write the resume pointer).
    `--print-path-only` → inject the resolved home cwd (not the raw
    payload cwd) as additionalContext.
(C) watchdog-timer.py / limit-wait.py missing or wrong --session-id →
    REWRITE via hookSpecificOutput.updatedInput.command (transparent
    inject; agent never sees the value because it doesn't need to).
(D) compact-loop/trigger_compact.py → BLOCK for subagents (shared
    process env: the shrunk window would compact the LEADER and every
    same-cwd session), BLOCK when no fresh handoff file exists at the
    home cwd for this session (reset without a handoff loses the
    thread — write it via dump.py first), REWRITE a missing or
    mismatched --cwd to the resolved home cwd via
    hookSpecificOutput.updatedInput.command (same transparent-inject
    idiom as (C) — the shrink must land in the settings file the live
    process actually watches, not wherever the Bash shell's cwd
    currently sits), and BLOCK a rapid-repeat trigger (fired again
    within RAPID_REPEAT_SECONDS with zero commits in cwd since the
    prior trigger) unless the command carries an explicit
    --content-value justification (the skill's Layer 1 content-value
    check is self-administered prose with no structural backstop, and
    was skipped for three direct-continuation bites in a row).
(E) compact-loop/inject_compact.py → same subagent BLOCK and
    fresh-handoff BLOCK as (D), plus the same --cwd REWRITE. The
    command is submitted into the console input buffer, which every
    process sharing the CLI's console reads, so a subagent's run would
    reset the leader. `--dry-run` submits nothing and is always allowed.

Fail-open on any exception.
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Language of the user-facing systemMessage ("en" or "ja"); the agent-facing
# block reason is always English. Set CLAUDE_HOOK_USER_LANG=ja in
# settings.json's "env" block for Japanese.
USER_LANG = os.environ.get("CLAUDE_HOOK_USER_LANG", "en").strip().lower()

HANDOFF_FRESH_SECONDS = 30 * 60
RAPID_REPEAT_SECONDS = 20 * 60
CONTENT_VALUE_MIN_LEN = 10

# Matches only an ACTUAL `python <path>` invocation at the start of a
# command segment (optionally behind env assignments). A mention of a
# guarded script's path anywhere else — a heredoc body, an `echo`/`cat`
# argument, a `python -c` string, or a read-only cmdlet argument from
# the PowerShell tool (`Select-String -Path ...`, `Get-Item ...`) —
# must not dispatch: blocking those corrupts the agent's own
# diagnostics. Append the script-specific path tail when using.
INVOCATION_PREFIX_RE = (
    r"(?:^|[\n;&|])\s*(?:\w+=\S+\s+)*"
    r"\bpython\b(?:\s+-[A-Za-z]\S*)?\s+['\"]?"
    r"(?:\$HOME|~|[A-Za-z]:|/)[^'\"\s|;&]*?"
)


def _home_cwd(transcript_path: str, fallback_cwd: str) -> str:
    """Resolve the session's true launch directory by scanning its
    transcript file forward for the first entry carrying a `cwd` field.
    A post-compact transcript's earliest lines can be summary entries
    without `cwd` — scanning (not just the first line) skips those.
    Falls back to `fallback_cwd` (the payload's own, possibly drifted,
    `cwd`) if the transcript is missing/unreadable/unparseable or no
    entry carries `cwd`."""
    if not transcript_path:
        return fallback_cwd
    tp = Path(transcript_path)
    if not tp.is_file():
        return fallback_cwd
    try:
        with open(tp, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                cwd_val = obj.get("cwd")
                if cwd_val:
                    return cwd_val
    except OSError:
        return fallback_cwd
    return fallback_cwd


def main() -> None:
    # Block reasons carry non-ASCII punctuation. Without UTF-8 stdout the
    # write raises, the outer handler swallows it, and the tool call is
    # silently ALLOWED — a safety guard that fails open and invisibly.
    # Do not rely on the caller exporting PYTHONUTF8/PYTHONIOENCODING.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        return

    tool_name = data.get("tool_name")
    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return

    session_id = data.get("session_id") or ""
    cwd = data.get("cwd") or ""
    transcript_path = data.get("transcript_path") or ""
    agent_id = data.get("agent_id")  # present only for subagent callers
    home_cwd = _home_cwd(transcript_path, cwd)

    # (A) Subagent /compact via CronCreate
    if tool_name == "CronCreate":
        prompt = tool_input.get("prompt") or ""
        if isinstance(prompt, str) and prompt.lstrip().startswith("/compact"):
            if agent_id:
                reason = (
                    "Subagent cannot trigger /compact via CronCreate. Subagents "
                    "share session_id with the leader, so this would compact the "
                    "LEADER's conversation — destroying team state. Only the "
                    "leader / solo agent may initiate the compact-handoff loop. "
                    "If you need to free context as a subagent, hand off to a "
                    "fresh subagent instead."
                )
                reason_ja = (
                    "[compact-handoff-guard] subagent は CronCreate 経由で "
                    "/compact を実行できません — leader と session_id を共有 "
                    "しているため leader の会話まで compact されてしまいます。"
                    "leader/solo agent のみが compact-loop reset を開始できます。"
                )
                _block(reason, reason_ja)
                return
        # any other CronCreate → allow (no-op)
        return

    # (B/C/D/E) Shell invocation guards. Each script gets its own branch;
    # the hook is a no-op for any unrelated command. home_cwd (not the
    # raw payload cwd) is what's keyed on below — see `_home_cwd`.
    # Both shell tools are covered: a guard that watched only one of them
    # would leave the other as an unguarded path to the same scripts.
    if tool_name in ("Bash", "PowerShell"):
        cmd = tool_input.get("command") or ""
        if not isinstance(cmd, str):
            return
        if re.search(
            INVOCATION_PREFIX_RE + r"compact-handoff[\\/]dump\.py", cmd,
        ):
            _guard_dump_py(cmd, session_id, home_cwd)
            return
        m_tc = re.search(
            INVOCATION_PREFIX_RE + r"compact-loop[\\/]trigger_compact\.py",
            cmd,
        )
        if m_tc:
            _guard_trigger_compact(
                cmd, session_id, home_cwd, agent_id, tool_input, m_tc.end(),
            )
            return
        m_ic = re.search(
            INVOCATION_PREFIX_RE + r"compact-loop[\\/]inject_compact\.py",
            cmd,
        )
        if m_ic:
            _guard_inject_compact(
                cmd, session_id, home_cwd, agent_id, tool_input, m_ic.end(),
            )
            return
        # Match only ACTUAL invocations: `python [single-letter flag]
        # <path>watchdog-timer.py` (similar for limit-wait.py). String
        # mentions inside `python -c "..."` / heredocs / `cat`/`echo` args
        # must not trigger — otherwise the hook corrupts the agent's own
        # diagnostics.
        invocation_re = (
            r"\bpython\b(?:\s+-[A-Za-z]\S*)?\s+['\"]?"
            r"(?:\$HOME|~|[A-Za-z]:|/)[^'\"\s|;&]*?[/\\]"
            r"(watchdog-timer|limit-wait|timer-wait)\.py"
        )
        m = re.search(invocation_re, cmd)
        if m:
            _inject_session_id(cmd, session_id, tool_input, m.end())
            return
        return  # unrelated Bash → no-op

    return  # other tools → no-op


def _strip_heredoc_and_redirect(cmd: str) -> str:
    """Trim the heredoc body (`<<EOF ... EOF`) and any input redirect off the
    end of a Bash command so flag-extraction regexes only see the actual
    argv segment. Prevents false matches from prose inside a heredoc body."""
    cmd_args = re.split(r"<<-?\s*['\"]?\w+['\"]?", cmd, maxsplit=1)[0]
    return cmd_args.split("<", 1)[0]


def _guard_dump_py(cmd: str, session_id: str, cwd: str) -> None:
    cmd_args = _strip_heredoc_and_redirect(cmd)

    m_sid = re.search(r"--session-id[=\s]+\"?([^\"\s]+)\"?", cmd_args)
    m_cwd = re.search(r"--cwd[=\s]+\"?([^\"]+?)\"?(\s|$|\|)", cmd_args)

    passed_sid = m_sid.group(1) if m_sid else None
    passed_cwd = m_cwd.group(1).strip() if m_cwd else None
    is_print_only = "--print-path-only" in cmd_args

    # --print-path-only: dump.py exits immediately as a no-op (by design).
    # The hook ALLOWS the call and injects the resolved session_id / cwd /
    # handoff_path via additionalContext. `cwd` here is the transcript-
    # derived home cwd (see `_home_cwd`), immune to both cross-session
    # snapshot leakage and same-session Bash cd drift, and works with
    # zero args.
    if is_print_only and session_id and cwd:
        sep = "\\" if "\\" in cwd else "/"
        resolved_path = (
            f"{cwd.rstrip(sep)}{sep}.work{sep}compact-handoff{sep}"
            f"{session_id}.md"
        )
        info_lines = [
            f"session_id: {session_id}",
            f"cwd: {cwd}",
            f"handoff_path: {resolved_path}",
        ]
        if passed_sid and passed_sid != session_id:
            info_lines.insert(
                0,
                f"(note: --session-id mismatch — passed {passed_sid!r}, "
                f"using real {session_id!r})",
            )
        _inject_context("\n".join(info_lines))
        return

    problems = []
    if not passed_sid:
        problems.append("--session-id is missing")
    elif session_id and passed_sid != session_id:
        problems.append(
            f"--session-id mismatch: passed {passed_sid!r}, real {session_id!r}"
        )
    if not passed_cwd:
        problems.append("--cwd is missing")

    if not problems:
        return  # all good → allow the write

    reason_lines = [
        "compact-handoff dump.py invocation blocked. "
        + "; ".join(problems) + ".",
        "",
        "Reason: dump.py's snapshot fallback (~/.claude/usage-snapshot.json) "
        "is GLOBAL across all running sessions and can return another "
        "session's data. The hook stdin has YOUR real session_id and cwd; "
        "pass them explicitly so the handoff file lands in the right place.",
        "",
        "Re-run with these exact values:",
        f"  --session-id \"{session_id}\"",
        f"  --cwd \"{cwd}\"",
    ]
    reason_ja = (
        "[compact-handoff-guard] compact-handoff dump.py の呼び出しをブロック"
        f"しました ({'; '.join(problems)})。dump.py のフォールバック "
        "(~/.claude/usage-snapshot.json) は全 session 共通で他 session の "
        "データを拾う恐れがあるため、--session-id と --cwd を明示指定して"
        f"再実行してください: --session-id \"{session_id}\" --cwd \"{cwd}\""
    )
    _block("\n".join(reason_lines), reason_ja)


def _guard_trigger_compact(
    cmd: str, session_id: str, cwd: str, agent_id, tool_input: dict,
    script_end: int,
) -> None:
    """(D) Guard trigger_compact.py (self-compact via auto-compact
    threshold shrink). The detached restore worker is spawned by the
    script itself, never via the Bash tool, so `--restore-worker` here
    can only be a manual diagnostic — allow it. ``cwd`` is the resolved
    home cwd (see `_home_cwd`), not the raw payload cwd."""
    if "--restore-worker" in cmd:
        return

    if agent_id:
        _block(
            "Subagent cannot run trigger_compact.py. The shrunk "
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW hot-reloads into the shared "
            "process env, so this would auto-compact the LEADER's "
            "conversation (and any same-cwd session) — destroying team "
            "state. Only the leader / solo agent may trigger the "
            "compact-loop reset. Report context exhaustion to the leader "
            "instead.",
            "[compact-handoff-guard] subagent は trigger_compact.py を実行"
            "できません。CLAUDE_CODE_AUTO_COMPACT_WINDOW の縮小は共有 "
            "process env に反映されるため、leader (と同じ cwd の他 session) "
            "まで auto-compact してしまいます。leader/solo agent のみが "
            "compact-loop reset を実行できます。文脈枯渇は leader に報告して"
            "ください。",
        )
        return

    if not session_id or not cwd:
        return  # fail-open: no authoritative identity to key the check on

    handoff = Path(cwd) / ".work" / "compact-handoff" / f"{session_id}.md"
    try:
        fresh = (
            handoff.exists()
            and time.time() - handoff.stat().st_mtime <= HANDOFF_FRESH_SECONDS
        )
    except OSError:
        return  # fail-open
    if not fresh:
        age_note = (
            "no handoff file exists for this session"
            if not handoff.exists()
            else "the handoff file is older than "
                 f"{HANDOFF_FRESH_SECONDS // 60} minutes"
        )
        _block(
            f"trigger_compact.py blocked: {age_note}.\n"
            f"Expected fresh handoff at: {handoff}\n\n"
            "A self-compact without a fresh handoff loses the thread — the "
            "auto-summary alone is not a reliable carrier. Run the "
            "compact-loop skill's Step 3 (compact-handoff/dump.py with "
            "--session-id and --cwd) to write the handoff, then re-run this "
            "trigger.",
            f"[compact-handoff-guard] trigger_compact.py をブロックしました: "
            f"{age_note}。想定 handoff: {handoff}\n新鮮な handoff なしの "
            "self-compact は文脈を失います — compact-loop skill Step 3 "
            "(compact-handoff/dump.py に --session-id と --cwd を渡す) で "
            "handoff を書いてから再実行してください。",
        )
        return

    if _guard_rapid_repeat_without_artifact(cmd, cwd):
        return  # blocked — a --cwd correction on a blocked call is moot

    _inject_cwd(cmd, cwd, tool_input, script_end)


def _guard_inject_compact(
    cmd: str, session_id: str, cwd: str, agent_id, tool_input: dict,
    script_end: int,
) -> None:
    """(E) Guard inject_compact.py, which submits `/compact` or `/clear`
    into the console input buffer. Same blast radius as (D): every
    process sharing the CLI's console reads that buffer, so a subagent
    running it resets the LEADER. ``cwd`` is the resolved home cwd (see
    `_home_cwd`). `--dry-run` submits nothing, so it is always allowed."""
    if "--dry-run" in cmd:
        return

    if agent_id:
        _block(
            "Subagent cannot run inject_compact.py. The command is written "
            "into the console input buffer shared by the whole CLI process, "
            "so this would submit /compact (or /clear) to the LEADER's "
            "conversation — destroying team state. Only the leader / solo "
            "agent may trigger the compact-loop reset. Report context "
            "exhaustion to the leader instead.",
            "[compact-handoff-guard] subagent は inject_compact.py を実行"
            "できません。コマンドは CLI process 全体で共有される console 入力"
            "バッファに書き込まれるため、leader の会話に /compact (または "
            "/clear) が送信されてしまいます。leader/solo agent のみが "
            "compact-loop reset を実行できます。文脈枯渇は leader に報告して"
            "ください。",
        )
        return

    if not session_id or not cwd:
        return  # fail-open: no authoritative identity to key the check on

    handoff = Path(cwd) / ".work" / "compact-handoff" / f"{session_id}.md"
    try:
        fresh = (
            handoff.exists()
            and time.time() - handoff.stat().st_mtime <= HANDOFF_FRESH_SECONDS
        )
    except OSError:
        return  # fail-open
    if not fresh:
        age_note = (
            "no handoff file exists for this session"
            if not handoff.exists()
            else "the handoff file is older than "
                 f"{HANDOFF_FRESH_SECONDS // 60} minutes"
        )
        _block(
            f"inject_compact.py blocked: {age_note}.\n"
            f"Expected fresh handoff at: {handoff}\n\n"
            "A reset without a fresh handoff loses the thread, and /clear "
            "leaves no summary to fall back on. Run the compact-loop "
            "skill's Step 3 (compact-handoff/dump.py with --session-id and "
            "--cwd) to write the handoff, then re-run this trigger.\n\n"
            "To check console reachability without submitting anything, "
            "re-run with --dry-run.",
            f"[compact-handoff-guard] inject_compact.py をブロックしました: "
            f"{age_note}。想定 handoff: {handoff}\n新鮮な handoff なしの "
            "reset は文脈を失います (/clear には fallback の summary すら"
            "ありません) — compact-loop skill Step 3 (compact-handoff/dump.py "
            "に --session-id と --cwd を渡す) で handoff を書いてから再実行"
            "してください。何も送信せず console 到達性だけ確認したい場合は "
            "--dry-run を付けて再実行してください。",
        )
        return

    _inject_cwd(cmd, cwd, tool_input, script_end)


def _last_trigger_timestamp(log_path: Path) -> float | None:
    """Return the epoch time of the most recent 'set CLAUDE_CODE_AUTO_COMPACT_WINDOW='
    log line (a real trigger fire, not a restore-worker line), or None if the
    log is absent/unparseable."""
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        m = re.match(r"(\S+) set CLAUDE_CODE_AUTO_COMPACT_WINDOW=", line)
        if not m:
            continue
        try:
            return datetime.fromisoformat(m.group(1)).timestamp()
        except ValueError:
            return None
    return None


def _last_commit_timestamp(cwd: str) -> float | None:
    """Return the epoch time of the most recent commit in cwd, or None if
    git is unavailable / cwd is not a repo (fail-open on either)."""
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "log", "-1", "--format=%ct"],
            capture_output=True, text=True, timeout=5,
            # Explicit, because the wiring sets PYTHONUTF8=1: text=True then
            # decodes the child as UTF-8, and a git output carrying the
            # console's native encoding kills the reader thread and returns
            # stdout=None while run() reports success. %ct is digits today,
            # so this is insurance against the format string growing a name
            # or a subject line.
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        return float(out.stdout.strip())
    except ValueError:
        return None


def _guard_rapid_repeat_without_artifact(cmd: str, cwd: str) -> bool:
    """(D continued) Block a rapid-repeat trigger_compact.py fire that
    preserved no commit-visible artifact since the prior fire, unless the
    caller explicitly declares --content-value. Fully mechanical proxy —
    no semantic judgment of what the handoff actually preserves. Returns
    True if it blocked, False otherwise (the caller uses this to decide
    whether to proceed to the --cwd correction step)."""
    cmd_args = _strip_heredoc_and_redirect(cmd)
    m_cv = re.search(
        r'--content-value[=\s]+(?:"([^"]*)"|(\S+))', cmd_args,
    )
    content_value = (m_cv.group(1) or m_cv.group(2) or "").strip() if m_cv else ""
    if len(content_value) >= CONTENT_VALUE_MIN_LEN:
        return False  # explicit justification declared — allow

    log_path = Path(cwd) / ".claude" / "trigger_compact.log"
    prev_ts = _last_trigger_timestamp(log_path)
    if prev_ts is None:
        return False  # first trigger this project — nothing to compare against

    now = time.time()
    if now - prev_ts > RAPID_REPEAT_SECONDS:
        return False  # not a rapid repeat

    commit_ts = _last_commit_timestamp(cwd)
    if commit_ts is None:
        return False  # fail-open: can't determine commit history
    if commit_ts > prev_ts:
        return False  # a commit landed since the prior trigger — real progress

    _block(
        "trigger_compact.py blocked: rapid-repeat compact with no commit "
        f"in {cwd} since the prior trigger "
        f"({int(now - prev_ts) // 60} min ago) — this looks like a "
        "structural-narrative-only wrap on a direct-continuation bite, "
        "not a genuine phase break.\n\n"
        "Before re-running: name concretely what this handoff preserves "
        "that TaskList / git log / CLAUDE.md / memory cannot already "
        "provide. If the next bite is a DIRECT continuation (same data / "
        "same context / same topic), do not compact — continue in the "
        "same cycle instead.\n\n"
        "If this is a genuine dead-end or phase break with no committable "
        "artifact, re-run with an explicit justification: "
        '--content-value "<one-line reason>" (>=10 chars, logged to '
        "trigger_compact.log).",
        f"[compact-handoff-guard] trigger_compact.py をブロックしました: "
        f"{cwd} で前回の trigger 以降 commit なしの rapid-repeat compact です "
        f"({int(now - prev_ts) // 60} 分前)。直接続きの bite なら compact せ"
        "ず同じサイクルで続行してください。本当に dead-end/phase break で "
        "committable な artifact がないなら、明示的な理由を付けて再実行して"
        'ください: --content-value "<一行の理由>" (10 文字以上、'
        "trigger_compact.log に記録)。",
    )
    return True


def _inject_session_id(
    cmd: str, session_id: str, tool_input: dict, script_end: int,
) -> None:
    """Transparent --session-id inject for Bash launches of a session-scoped
    script. ``script_end`` is the offset in cmd immediately after the script
    path (from the caller's invocation match)."""
    if not session_id:
        return

    # Consume an optional closing quote so the boundary lands cleanly
    # between path and args.
    m_q = re.match(r"[\"\']", cmd[script_end:script_end + 1])
    end = script_end + (1 if m_q else 0)
    prefix = cmd[:end]
    rest = cmd[end:]

    # Scope --session-id detection to the invocation segment (after the
    # script path, before the next shell separator). Otherwise prose like
    # `echo '... --session-id ...'` earlier in cmd would false-trigger.
    m_sep = re.search(r"(\s*(?:&&|\|\||;|\||\n))", rest)
    if m_sep:
        invocation, suffix = rest[: m_sep.start()], rest[m_sep.start():]
    else:
        invocation, suffix = rest, ""

    sid_re = r"(--session-id[=\s]+\"?)([^\"\s]+)(\"?)"
    m_sid = re.search(sid_re, invocation)
    passed_sid = m_sid.group(2) if m_sid else None
    if passed_sid == session_id:
        return

    if m_sid:
        new_invocation = re.sub(
            sid_re,
            lambda m: f"{m.group(1)}{session_id}{m.group(3)}",
            invocation, count=1,
        )
        note = f"(guard) replaced --session-id {passed_sid!r} -> {session_id!r}"
    else:
        new_invocation = f' --session-id "{session_id}"' + invocation
        note = f'(guard) injected --session-id "{session_id}"'

    new_cmd = prefix + new_invocation + suffix
    if new_cmd == cmd:
        return

    updated = dict(tool_input)
    updated["command"] = new_cmd
    try:
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "updatedInput": updated,
                "additionalContext": note,
            }
        }, ensure_ascii=False))
    except Exception:
        pass


def _inject_cwd(
    cmd: str, home_cwd: str, tool_input: dict, script_end: int,
) -> None:
    """Transparent --cwd inject/correct for trigger_compact.py Bash
    launches, same idiom as `_inject_session_id`. ``script_end`` is the
    offset in cmd immediately after the script path. Without this,
    trigger_compact.py's own `--cwd` default (`os.getcwd()`) follows
    wherever the Bash tool's persistent shell currently sits, which
    drifts away from the session's home over a long, `cd`-heavy session
    and writes the shrink to a settings.local.json the live process does
    not watch."""
    if not home_cwd:
        return

    m_q = re.match(r"[\"\']", cmd[script_end:script_end + 1])
    end = script_end + (1 if m_q else 0)
    prefix = cmd[:end]
    rest = cmd[end:]

    m_sep = re.search(r"(\s*(?:&&|\|\||;|\||\n))", rest)
    if m_sep:
        invocation, suffix = rest[: m_sep.start()], rest[m_sep.start():]
    else:
        invocation, suffix = rest, ""

    cwd_re = r'(--cwd[=\s]+)(?:"([^"]+)"|(\S+))'
    m_cwd = re.search(cwd_re, invocation)
    passed_cwd = (m_cwd.group(2) or m_cwd.group(3)) if m_cwd else None
    if passed_cwd == home_cwd:
        return

    if m_cwd:
        new_invocation = re.sub(
            cwd_re,
            lambda m: f'{m.group(1)}"{home_cwd}"',
            invocation, count=1,
        )
        note = f"(guard) replaced --cwd {passed_cwd!r} -> {home_cwd!r} (drift correction)"
    else:
        new_invocation = f' --cwd "{home_cwd}"' + invocation
        note = f'(guard) injected --cwd "{home_cwd}" (drift correction)'

    new_cmd = prefix + new_invocation + suffix
    if new_cmd == cmd:
        return

    updated = dict(tool_input)
    updated["command"] = new_cmd
    try:
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "updatedInput": updated,
                "additionalContext": note,
            }
        }, ensure_ascii=False))
    except Exception:
        pass


def _block(reason: str, reason_ja: str) -> None:
    try:
        sys.stdout.write(
            json.dumps({"decision": "block", "reason": reason,
                        "systemMessage": reason_ja if USER_LANG == "ja" else reason})
        )
    except Exception:
        pass


def _inject_context(info: str) -> None:
    """Allow the tool to proceed but inject `info` as additionalContext.

    The PreToolUse hookSpecificOutput.additionalContext field is delivered
    to the model as if it were an annotation on the tool call — no error
    framing, no block. Used for --print-path-only where dump.py is a
    no-op and the hook is the sole authority on the resolved values.
    """
    try:
        sys.stdout.write(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "additionalContext": info,
                    }
                },
                ensure_ascii=False,
            )
        )
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
