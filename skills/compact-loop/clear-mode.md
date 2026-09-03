# compact-loop — `/clear` mode

Read this when SKILL.md Layer 3 selected `/clear`. Steps 1-4 of SKILL.md
are already done at this point; this file replaces Steps 5-6's
`/compact`-mode procedure with the `/clear` equivalents.

## Step 5 — Trigger the reset

`/clear` discards the conversation and rotates the session_id, so the
wake cron is the only thing that carries the handoff path across.
Schedule it BEFORE the reset. After the step 3 pre-flight:

1. Schedule ONE plain-text wake cron (`recurring`: false, pin only the
   MINUTE — `<(now_min+N)%60> * * * *`, wildcard the rest) with the
   path-embedded template below, with enough lead time to land after
   the reset.
2. Submit `/clear` — with `inject_compact.py --clear`, launched as
   `recovery.md`'s "Console-input reset" section describes and only
   while the user is away from the keyboard. If that script refuses,
   or the user is present, ask them to type `/clear` instead,
   including the absolute handoff path in the same message.
3. Idle. If a Stop hook of yours blocks the first Stop, idle through
   it — that is expected here.

**Wake-prompt template.** Render human-readable lines in your reply
language — the wake prompt anchors the resumed instance's reply
language, and a mismatch drifts autonomous runs. `"Next concrete
action"` stays verbatim (it's the handoff's section heading). English
rendering:
```
[compact-loop /clear resume — cycle <N>]
Pre-/clear session_id was <OLD-SESSION-ID>.
Read the handoff at: <ABSOLUTE-HANDOFF-PATH>
Act on its "Next concrete action" section directly. Do not re-acknowledge the handoff — just resume.
```
e.g. Japanese rendering:
```
[compact-loop /clear 復帰 — サイクル <N>]
/clear 前の session_id は <OLD-SESSION-ID> でした。
ハンドオフを次の場所で読んでください: <ABSOLUTE-HANDOFF-PATH>
その「Next concrete action」セクションに直接従って作業を再開してください。ハンドオフを改めて確認報告せず、そのまま再開すること。
```

## Step 6 — Post-reset (`/clear`)

Read the handoff from the path embedded in the wake prompt (or the
user's message) and continue from its Next concrete action. Then run
SKILL.md Step 6's "Both modes — before resuming work" checklist.

Note `/clear` rotates the session_id, so the task list arrives empty —
the handoff file is the sole recovery source.

## `/clear`-specific hard rules

- **Verify the handoff file is non-empty before triggering the
  reset.** No summary fallback; empty handoff = total work loss.
- **Hard-code OLD session_id + absolute handoff path in the wake
  prompt.**
