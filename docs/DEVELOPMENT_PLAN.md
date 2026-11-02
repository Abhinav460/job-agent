# Development plan

The product goal is a personal job-application assistant that parses a posting,
tailors a truthful one-page resume, fills known fields, stops for human review,
and tracks the application. It must never submit an application itself.

## Current state

- [x] Private data and secret-scanning scaffolding.
- [x] Resume parsing, bullet retrieval, tailoring, compilation, and checkpointing.
- [x] Durable application journal and retry-safe Google Sheets sync (offline verified).
- [x] Local browser tracker controls for choosing a spreadsheet and syncing pending records (offline verified).
- [x] Local resume catalog and embedded Qdrant index for selecting an existing PDF (offline verified).
- [x] Portal profile entry, resume selection, and application preparation with an unchanged PDF copy (offline verified).
- [x] Chrome side panel and token-protected loopback API for dashboard actions (offline verified).
- [x] Private reusable context notes with embedded Qdrant search (offline verified).
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
5. **Tracker workspace (offline verified):** The local portal can save a private
   spreadsheet selection, show pending records for that destination, sync one
   record or all pending records, and execute a small set of explicit tracker
   instructions. The CLI's `GOOGLE_SHEET_ID` setting remains independent. Live
   Google Sheets access still needs the owner's credentials and spreadsheet.
6. **Existing-resume preparation (offline verified):** A private local catalog
   extracts text from PDF, DOCX, ODT and TXT files, indexes local vectors in
   embedded Qdrant, and selects a matching existing PDF for a company-named
   byte-for-byte copy. The portal opens a local dashboard tab with profile,
   resume, preparation, review, and tracker controls. It records the
   application and can open a supported form for human review; network writes
   stay blocked until the user explicitly enables manual actions. This flow
   does not tailor the selected resume; the separate LaTeX pipeline does that.
   Live browser compatibility is unverified. Native Windows private ACL setup
   is implemented and tested with fictional files; live dashboard use remains
   unverified.
7. **Side panel and reusable context (offline verified):** The Chrome panel
   connects to a token-protected loopback server and can manage profile facts,
   resume folders, preparation, review handoff, status, and Sheet sync. Applicant
   notes are saved under the private home and indexed by a separate embedded
   Qdrant collection using local vectors, opaque IDs, and digests. Search returns
   suggestions for manual review; fuzzy matches never fill form answers.
   Resume indexing from separate CLI folder runs preserves prior entries.
   Live Chrome, WSL, and Google API behavior remain unverified. Native Windows
   storage now has fail-closed ACL setup, tested with fictional files. The side panel
   rejects selected descriptions over 30,000 characters so it cannot prepare
   from silently truncated job requirements.
8. **Native Windows private storage (offline verified):** Before private commands
   use data, repair and verify a current-user-only NTFS DACL on the dedicated
   home and existing contents. Reject broad homes, unrecognized existing custom
   homes, reparse points, and hard-linked files. New files inherit private
   access. Startup fails if protection cannot be verified. Live dashboard and
   Chrome behavior with fictional data remain to be checked.

Use a single Sol builder for a substantive milestone. Use Luna for bounded
exploration or review, and Astra only for a demonstrated hard problem. The
project config caps workers at two. Check the Codex usage dashboard before
increasing concurrency or choosing a higher-cost model; the repository cannot
see the account's remaining allowance.
