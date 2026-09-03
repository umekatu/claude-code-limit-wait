---
name: compact-loop
description: "Self-service context reset — YOU (the model) compact this session yourself; never ask the user to run /compact or to open a new session. Packages the full cycle: self-audit → handoff → trigger_compact.py fires a real auto-compact in-session (no user action needed) → post-reset resume. Invoke at context exhaustion, cognitive dead-ends, or clean phase breaks; the skill triggers both /compact and /clear itself, and asks the user to type the command only when no automatic route reaches this session. Do NOT invoke as a team-mode subagent (it would reset the leader's REPL), nor mid-tool-loop or mid-user-conversation."
---

# compact-loop

**Self-service reset: the model compacts itself.** About to tell the
user 「/compact してください」 / "please compact"? Stop — that request is
this skill's job. Invoke it and do the reset yourself; the only
user-assisted exceptions are the `/clear` variant and the recovery
fallbacks for a session that has lost the ability to self-compact.

Companion files (read only when directed): `clear-mode.md` (`/clear`
variant of Steps 5-6), `recovery.md` (non-fire diagnosis +
console-input reset).

## When to invoke

Three independent layers — keep them separate.

### Layer 1 — Should I reset at all? (trigger: ANY one fires)

**Content-value check first**: name something concrete this handoff
will preserve that the post-compact instance cannot re-derive from the
task list, `git log`, CLAUDE.md, memory, or the codebase. Nothing
concrete → do NOT fire regardless of triggers; use `TaskCreate` /
commits / memory for the preservation need instead.

- **Context pressure** — context ≥ 75% or a context-monitor advisory
  surfaced. Trigger, not gate — <75% is not a precondition; the other
  triggers still fire at any context level.
- **Cognitive dead-end** — same hypotheses recurring, new angles not
  surfacing.
- **Phase break** — clean topic shift; current history would anchor
  the next work to stale framings.

If NONE fire, keep working.

### Layer 2 — Anti-conditions (gate: ALL must be clear)

Never reset when: mid-tool-loop / mid-user-conversation /
session-about-to-end / pre-destructive-action / team-mode subagent —
even if Layer 1 fired.

**Orchestration in flight** — a running Workflow, working subagents, or
background Bash awaiting results means no clean breakpoint exists yet.
Wait, integrate the results, then re-run Layer 1. Detect via
non-terminal `status` in
`~/.claude/projects/<project>/<sid>/workflows/wf_*.json`, active
TaskList entries, or live background Bash.

### Layer 3 — Which reset?

1. **`/compact` (default)** — preserves session_id + summary; fully
   autonomous via `trigger_compact.py` (Step 5).
2. **`/clear` (variant, mechanics in `clear-mode.md`)** — rotates
   session_id, discards the conversation entirely. Use when history
   itself is the liability: cognitive dead-end (rut erasure), phase
   break (stale framings dropped), summary-as-noise (misconception
   baked into past summaries), or CLAUDE.md edited this cycle and the
   new rule must be live next cycle (CLAUDE.md does not hot-reload;
   hooks/references do). The handoff file is the SOLE survivor — if it
   cannot be self-contained, use `/compact`. Valid at any context
   level.
3. **Manual new session** — NOT this skill. Stop the loop and tell the
   user when the handoff cannot be self-contained, the next step needs
   human judgment, or user review should precede resumption.

When in doubt between the two modes: `/compact`. At any other layer:
don't reset.

## Do this

Steps 1-4 are identical across modes. Steps 5-6 branch by mode.

### Step 1 — Resolve session_id + cwd + handoff_path

```
PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \
  python ~/.claude/scripts/compact-handoff/dump.py \
  --print-path-only
```

### Step 2 — Self-audit (mandatory)

Audit whether this cycle's modifications fully cover (a) ripple of
touched files and (b) documentation obligations of the achieved
nature. Write every gap down the moment you find it, before applying
any fix. Fix what you can in place; log the rest to the handoff's
"Ripple updates not yet applied" / "Documentation gaps" sections.

Declare every gap on that list as a flag on the dump.py call that
writes the handoff: one you fixed becomes `--audit-fixed <path> "<a
string the fix put in the file>"`, one you left becomes
`--audit-pending <path> "<one line>"`, and an audit that found nothing
becomes `--audit-none`.

- **Phase 1 — diff-based ripple check**: list files touched this cycle
  (`git diff` / `git status` / Glob); identify cross-references for
  each (same-topic docs, shared schemas, files citing the same rule);
  Read the targets and fix inconsistencies in place; blocked fixes go
  to "Ripple updates not yet applied".
- **Phase 2 — nature-based documentation audit**: state in one line
  what this cycle achieved; look up the project's `## documentation
  義務` section (`<cwd>/CLAUDE.md`, else `<cwd>/.work/CATALOG.md`);
  cross-compare its obligation targets with Phase 1's touched list;
  untouched obligations get fixed in place or recorded in
  "Documentation gaps". No definition file / no section → Phase 2
  gracefully skips; record "n/a" in the handoff.

**"Nothing missing" is valid — but only after actually running both
phases.** Never self-assert with "I touched it, so OK".

### Step 3 — Write the handoff

Pipe the body below into dump.py with **explicit `--session-id` and
`--cwd`** (from Step 1) and one `--audit-fixed` / `--audit-pending`
flag per gap the self-audit found (`--audit-none` alone when it found
none).

**The successor reads this cold with a limited context budget** —
every line should serve their first few minutes of action. Durable
knowledge belongs in memory (Step 4A); the handoff carries only what's
thread-specific and action-relevant. Do NOT include: task-list state
(rederivable via TaskList), recent commit messages / git status
(rederivable in seconds), cumulative ship counters / reflection
ribbons (CHANGELOG material), or ✓-bullet audit evidence under "none"
sections (write one word: `none` / `n/a`).

```
PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \
  python ~/.claude/scripts/compact-handoff/dump.py \
  --topic "<short topic>" \
  --session-id "<from step 1>" \
  --cwd "<from step 1>" \
  --audit-fixed "<path>" "<string the fix put in the file>" \
  --audit-pending "<path>" "<one line>" <<'EOF'
## Who you are (post-compact instance directive — read first)

You are the **post-compact instance**. This handoff was written by the
now-terminated **pre-compact instance**. Sections "Intent" / "Next
concrete action" = YOUR work. Everything else = state to leverage.
Cross-reference with the task list (`~/.claude/tasks/<session_id>/`).

## Intent
What we are trying to achieve and why. Include behavioral constraints
(safety rules, things NOT to do) as short bullets, and the successor's
decision latitude: where they have discretion vs where they must ask
the user (pending user questions also go in the task list).

## Next concrete action
What to do first and why, with file paths / commands / decision
criteria — actionable from this alone. A single step or a short
ordered sequence when order matters; the rest of the backlog lives in
the task list or memory, not here.

## In-flight work & timers
Background tasks / subagents / crons / waiters still running: what
each is, where its output lands on disk, what to do when it completes.
(Task records don't survive the reset — capture outputs to files.)

## Established facts that risk being dropped
Facts needed FOR THE NEXT ACTION that the summarizer might lose and
that aren't already in memory or the codebase. Not a knowledge dump.

## Ripple updates not yet applied
Step 2 Phase 1 findings not yet applied: (file path + 1-line needed
change) each; successor applies these first. "none" if nothing.

## Documentation gaps
Step 2 Phase 2 obligation targets not yet touched: (file path + 1-line
description) each. "n/a (no per-project mapping defined)" if the
project defines none.

## Free-form notes
Anything else worth carrying — hunches, off-hand user remarks,
reminders to self.
EOF
```

Intent + Next concrete action are mandatory — in `/clear` mode doubly
so (no auto-summary fallback). The other sections may be "none"/"n/a"
only after actually running Step 2.

**`/clear` mode pre-flight (skip in `/compact` mode):** verify the
handoff file exists and is non-empty before Step 5 — a failed write
means total work loss.

### Step 4 — Consolidate & tidy (BEFORE the reset)

**This is NOT a true shutdown — the work continues.** Touch only what
is done and won't be needed after the reset; when in doubt, leave it
and note it in the handoff.

- **4A — Knowledge**: promote durable learnings to memory — verified
  facts that took effort, user feedback (with Why / How to apply),
  dead-ends, project state not derivable from code. Update existing
  files rather than duplicating; refresh `MEMORY.md` pointers; rules
  belong in CLAUDE.md / references (INDEX.md in sync), not memory.
  **Self-promotion guardrail**: a lesson derived from your OWN
  apology / mistake-recognition this cycle goes through your
  mistake-handling procedure, not this step.
- **4B — Workspace**: for done work only — remove finished subagents'
  worktrees, commit and push completed work (feature branch only,
  never force-push; diverging or default-branch pushes get reported in
  the handoff instead), finalize repo records.
- **4C — Task list leak check**: promote tasks/issues that exist only
  in the handoff or conversation into the task list — the handoff is
  overwritten every cycle; the task list is disk-persisted and
  survives the compact byte-for-byte.

Each of 4A/4B/4C: "nothing to do" is valid — but only after actually
looking (`git status`, `git worktree list`, handoff scan).

### Step 5 — Trigger the reset

**`/compact` mode:** as the **LAST tool call of the turn**, launch via
background Bash (`run_in_background: true`):

```
PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \
  python ~/.claude/skills/compact-loop/trigger_compact.py
```

Run it bare — defaults handle the timing; override a flag only when a
default demonstrably fails. Say everything owed to the user in the
SAME message (compaction replaces your next inference step — there is
no "after"), then end the turn. **Idle is the correct state** — no
waiting loops, no extra tool calls, no waiter for the ~50 s this
takes; if a Stop hook of yours blocks the Stop, idle through it. The
script arms the compact, exits, and its completion notification wakes
the session where the compact fires; settings are restored by the
script either way.

**If you wake to the script's notification and NO compact summary is
above** (still the pre-compact instance): read `recovery.md` and
follow its diagnosis order.

**`/clear` mode:** read `clear-mode.md` and follow its Step 5
(wake-cron then `/clear` submission; console-input mechanics in
`recovery.md`).

### Step 6 — Post-reset

**`/compact` mode**, in order:

1. **Verify the env restore FIRST.** In
   `<cwd>/.claude/settings.local.json`,
   `CLAUDE_CODE_AUTO_COMPACT_WINDOW` must be back at your baseline (the
   trigger's `--restore-value`, default `900000`; cross-check the
   set/restored pair in `<cwd>/.claude/trigger_compact.log`). Still
   shrunk → OVERWRITE to that baseline now; never delete the key (env
   hot-reload is add/overwrite only — a delete does not unset the live
   value).
2. Read the handoff at `<cwd>/.work/compact-handoff/<session_id>.md`
   directly — do not wait for the resume hook.

**`/clear` mode:** follow `clear-mode.md` Step 6.

**Both modes — before resuming work:**
- `ToolSearch` any deferred tools you need (loaded schemas reset).
- **Recon gate**: ~2-min topic-scoped check of the project's own
  records (known-issues, prior audit notes, existing scripts) before
  launching any investigation or building any tool.
- **Prior-cycle integrity verify**: Read each file named in the
  handoff's "Established facts" / "In-flight work" / "Ripple updates
  not yet applied" / "Documentation gaps" sections and cross-check
  (path / content / ref chain). Pending "Ripple" / "Documentation
  gaps" items are applied with top priority this cycle.
- Do NOT re-acknowledge the summary or the handoff to the user.

Then continue from the handoff's Next concrete action.

## Hard rules

- **Subagents must NEVER invoke this skill (either mode)** — shared
  process env means the shrunk window would compact the LEADER and
  every same-cwd session (a guard hook blocks subagent runs of
  `trigger_compact.py`, and blocks a missing or >30-min-stale
  handoff). A context-exhausted subagent reports to its leader.
- **Never trigger with orchestration in flight** — blast radius is
  cwd-wide: in-flight subagents / Workflows / background Bash can be
  compacted too (the trigger's own background launch is the sole
  exception). The guard hook cannot see your in-flight orchestration —
  that check is yours (Layer 2).
- **Step 4 runs BEFORE the trigger, never after**, and cleans only
  DONE work.
- **Always pass `--session-id`, `--cwd`, and one audit flag per gap
  the self-audit found, explicitly** in Step 3.
- **The trigger launch is the LAST tool call of its turn.**
- **Post-reset, verify the env restore before any other work.**
- `/clear`-specific hard rules (handoff pre-flight, hard-coded
  session_id + path in the wake prompt) live in `clear-mode.md` and
  bind whenever that mode is chosen.

## What survives the reset

`/compact` keeps session_id, summary, crons, completion notifications,
background Bash (keeps running), and the task list (disk-persisted,
byte-for-byte); it drops loaded tool schemas and subagent task
records. `/clear` additionally rotates session_id, so the task list
arrives empty — the handoff file is the sole recovery source.

The loop ends when the task list is empty or the user stops it.
