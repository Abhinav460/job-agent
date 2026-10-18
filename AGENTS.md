# Job Agent coding instructions

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
