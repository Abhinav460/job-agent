# Development plan

The product goal is a personal job-application assistant that parses a posting,
tailors a truthful one-page resume, fills known fields, stops for human review,
and tracks the application. It must never submit an application itself.

## Current state

- [x] Private data and secret-scanning scaffolding.
- [x] Resume parsing, bullet retrieval, tailoring, compilation, and checkpointing.
- [x] Durable application journal and retry-safe Google Sheets sync (offline verified).
- [x] Private applicant profile and conservative Greenhouse HTML form filling (offline verified).
- [x] Conservative Lever global/EU hosted form adapter for name and email (offline verified).
- [x] Explicit selection-only capture for JavaScript/login-walled postings (offline verified).
- [ ] Additional form variants and site adapters beyond Greenhouse/Lever.
- [x] End-to-end offline demonstration with a fake job and form.

## Next milestones

1. **Journal and tracker (implemented):** Give each posting a stable application ID, store its
   resume path and review status locally, and sync one row per ID to Sheets.
   A failed sync leaves the local record available for retry. Submission status
   requires explicit user confirmation. Live Sheets access still needs the
   user's own spreadsheet and service-account credentials.
2. **One-site browser flow (implemented):** Validate the private profile, fill only fields
   backed by known facts, upload only the generated PDF, and leave the headed
   browser open for review. Never activate Submit or Apply controls. Test on a
   local fake form before a live site. The initial adapter supports top-level
   Greenhouse `application_form` HTML forms; other variants stop for manual
   completion. Fake-page tests cover deterministic actions and browser lifetime.
   No live application visits have been performed.
3. **More sites and capture (partially implemented):** Lever global/EU hosted
   forms support only uniquely labeled, visible Full name and Email inputs;
   full name joins existing profile facts. Resume upload, custom questions,
   demographic questions and consent require manual completion. Exact origin
   and path are pinned through actions and review handoff. `capture --url` now
   opens a private headed browser for manual navigation/login and saves only
   user-selected job-description text after terminal confirmation. Captures
   require HTTPS URLs and bounded text, and stay under the private home;
   `tailor --capture` passes URL and text directly into the graph without fetch.
   Additional site adapters and form variants remain to be added.
4. **Integration (offline verified):** `tests/test_workflow.py` exercises the real
   CLI, resume graph, journal, browser action executor and review handoff, and
   Sheets HTTP adapter using fictional data and fake external services. A lost
   remote response is retried into one row, resume bytes remain intact, and
   submitted status requires explicit human confirmation. Socket and DNS calls
   are blocked. Both URL and private-capture source variants pass. Live browser
   compatibility, live capture/login behavior and service credentials remain
   unverified; additional adapters remain unimplemented.

Use a single Sol builder for a substantive milestone. Use Luna for bounded
exploration or review, and Astra only for a demonstrated hard problem. The
project config caps workers at two. Check the Codex usage dashboard before
increasing concurrency or choosing a higher-cost model; the repository cannot
see the account's remaining allowance.
