# Job Agent coding instructions

Read `C:\Users\banu_\context-saver\CONTEXT.md` for the shared cross-project
handoff and `PROJECT_CONTEXT.md` for this project's details, then verify both
against the working tree. Update both after meaningful changes so future
assistants can see what changed, what was tested, and what remains.

This public repository contains code and fake examples. Real resumes, profiles,
browser sessions, credentials, checkpoints, and output belong in `JOBAGENT_HOME`
outside the repository. Do not read or print `.env`, `JOBAGENT_HOME`, or other
private applicant data while developing or testing. Use fake fixtures.

## Master and workers

- The master owns the task queue, assigns one bounded deliverable at a time,
  reviews worker changes, and runs the final relevant checks.
- Use the `scout` agent for narrow read-only code searches and the `builder`
  agent for an implementation with clear acceptance criteria. Use `reviewer`
  when a change affects personal data, browser behavior, or external writes.
- Give workers disjoint files. Keep at most two workers active at once and
  normally use one. Reuse an existing worker's findings before starting another.
- Use the project default GPT-6.1 Sol for the master. Reserve Astra for a
  specific hard design or debugging problem after Sol has concrete evidence.
- Follow `docs/USAGE_PLAN.md` for model choice and allowance checks. If the
  dashboard is unavailable, keep one builder and avoid Astra.

## Claude Code and Codex working together

Both assistants may run at the same time. Matching agents exist for each:
Codex in `.codex/agents/*.toml`, Claude Code in `.claude/agents/*.md`
(`scout` read-only search, `builder` one bounded task, `reviewer` read-only review).

- Each assistant works in its own git worktree on its own branch
  (`codex/...` or `claude/...`), e.g. `git worktree add ../job-agent-<task> -b claude/<task>`.
  Do not switch branches, reset, stash, or edit files in another assistant's worktree.
- Never force-push or reset a branch the other assistant has pushed to. Integrate with a
  merge or a PR instead, and leave the other assistant's commits in place.
- Stage only files you changed (`git add <paths>`, never `git add -A`).
- Record which branch/worktree you are using and its status in `PROJECT_CONTEXT.md`
  and the shared `CONTEXT.md`, so the other assistant can see who owns what.

## Work sequence

1. Read the relevant code and tests, and state a concrete outcome.
2. Implement the smallest complete milestone from `docs/DEVELOPMENT_PLAN.md`.
3. Test with fake applicant and job data. Run only the checks relevant to the
   changed behavior, then review the diff for secrets and unintended files.
4. Update the plan with what is done and what remains. Report blockers only
   when credentials, personal facts, or a user decision are truly required.

Never invent applicant facts, click a real application's Submit or Apply
control, or mark an application submitted without user confirmation. Keep
external effects idempotent so retries cannot duplicate an application.
