# Job Agent project context

Updated: 2026-11-01. This is a handoff, not a substitute for checking the
repository. Start with `git status`, this file, `AGENTS.md`,
`docs/DEVELOPMENT_PLAN.md`, and `README.md`.
The general cross-project handoff is `C:\Users\banu_\context-saver\CONTEXT.md`.

## Goal

Build a personal job-application assistant that uses truthful source material
to prepare resumes, fills only known form fields, stops for human review, and
tracks applications. It must never submit an application itself.

## Current state

- Repository: `origin` (public GitHub repository; run `git remote -v` for URL).
- Local branch: `codex/extension-autoapply-sheets`, pushed at `a92db88` on
  2026-11-01. The working tree was clean immediately after that push.
- GitHub `main` merged the previous workflow branch in `515e8cc`.
- The dashboard, extension, private resume catalog, context notes, and tracker
  changes are in that pushed branch. Check `git status` before further work;
  this handoff update itself is a later local documentation change until pushed.
- The original resume pipeline, application journal, conservative Greenhouse
  and Lever adapters, selection-only posting capture, and optional Sheets
  tracker exist. See `docs/DEVELOPMENT_PLAN.md` for detailed scope.

## Recent changes

- Added `jobagent portal`, a loopback browser dashboard for profile facts,
  resume folders, application preparation, review controls, and tracker sync.
- Added a Chrome side panel and `jobagent extension-server`. The local JSON API
  requires a random token and binds to one Chrome extension origin per run.
- Added a private resume catalog and embedded Qdrant index for choosing an
  existing PDF. `index-resumes` preserves entries from earlier CLI folder runs.
- Added reusable context notes in `context_notes.json` under `JOBAGENT_HOME`.
  Embedded Qdrant stores local vectors, opaque IDs, and digests; note text stays
  in the private JSON file. Search returns suggestions for human review.
- Added an extension manifest exception to `.gitignore` and the PII checker.
- Added this shared handoff, pointers in `AGENTS.md` and `CLAUDE.md`, and a home
  project index so future assistants can find the current status.
- Added a separate private context-saver repository for cross-project memory.

## Decisions and boundaries

- This is a public code repository. Never put resumes, profile facts, keys,
  browser data, generated PDFs, or application records in it. Do not read or
  print `.env` or `JOBAGENT_HOME` during development; use fictional fixtures.
- The context note JSON file is the source of truth. Its Qdrant index is derived
  and can be rebuilt. Local feature-hash vectors need no hosted embedding API.
- Saved context may suggest relevant facts but must not fill a form from a fuzzy
  match. Existing custom answers fill only exact, unique supported labels.
- The portal and extension server share embedded indexes: run one at a time.
- The agent must never click Submit or Apply or record `submitted` without an
  explicit user confirmation. Browser review starts with manual actions paused.

## Verification and limitations

- On 2026-10-31, 91 relevant offline tests passed across the context store,
  resume index/catalog, portal, extension API, browser, and tracker. JavaScript
  syntax and Python compilation checks passed.
- On 2026-11-01, the handoff files and README pointers were checked for
  existence and `git diff --check` passed. No code tests were rerun for these
  documentation-only changes.
- A full Windows test run had 2 failures and 5 errors from POSIX permission and
  symlink assumptions and from placing test temp homes inside this public repo.
  Do not present the full suite as passing on Windows.
- Live Chrome, Google Sheets, and WSL behavior has not been verified.
- `portal`, `extension-server`, and `index-resumes` refuse native Windows until
  private ACL support exists. On 2026-10-31, this machine had WSL 2 but only
  `docker-desktop-data`, no Ubuntu distribution. A request for approval to
  install Ubuntu WSL was sent; no approval had arrived by this update.

## Next steps

1. Resolve the runtime platform: install Ubuntu WSL with user approval, or
   implement and review private Windows ACL support. Keep `JOBAGENT_HOME` on a
   private Linux filesystem if using WSL.
2. Smoke-test the dashboard and Chrome side panel with fictional profile,
   resume, posting, and local browser data. Confirm loopback connection and
   review handoff before live use.
3. Review the pushed branch for correctness and private data, run the relevant
   checks in the chosen runtime, then open a PR when ready. Do not assume the
   branch has been merged into `main`.
4. Verify Sheets sync only with the user's own credentials and spreadsheet;
   continue conservative form-adapter work after the local workflow is usable.

## Future instructions

Record new user directions here when they should carry into later sessions.
The current user request is to maintain these shared context files so Claude
Code, Codex, and other assistants can see project status and next steps.

## Handoff rule

At the end of meaningful work, update the date, branch/commit, recent changes,
verification, unresolved issues, and next steps in this file. Remove stale
claims. Keep this file short and factual; put detailed procedures in README or
the development plan. Never write private applicant facts or credentials here.
