# job-agent

For current development status and next steps, see [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md).

A personal job-application agent. Given a job posting URL or pasted text, it:

1. parses the job description,
2. tailors a LaTeX resume from a bank of **real** bullets and compiles it locally,
3. pre-fills the application form in a browser,
4. **stops for human review** (it never clicks submit), and
5. logs the application to a Google Sheet.

> Status: under construction. Done: security scaffolding (Phase 0) and the
> resume pipeline (Phase 1), local application journal and optional Sheets tracker.
> Conservative Greenhouse and Lever form filling is implemented with offline tests.
> Private selection-only posting capture is implemented with offline tests.
> A local browser workspace can select the tracker spreadsheet and sync pending records.
> The workspace can also index existing resumes, copy the best matching PDF, and save profile facts.
> Next: additional form variants, site adapters and live verification.

## Security model

This repository is public. The code is shared; the data never is.

On Linux and WSL, private files use `0700`/`0600` permissions. On native Windows,
commands repair and verify a current-user-only NTFS access control list on the
private home and its existing contents before using them. New files inherit the
private access rule. If protection fails, the command stops. Use a dedicated
`JOBAGENT_HOME` outside the public repository. The default `~/.jobagent`
location is recognized for migration.

| Rule | How it is enforced |
| --- | --- |
| No personal data in the repo | Resume, profile, bullets, keys, browser profile, checkpoints and PDFs live in `$JOBAGENT_HOME` (default `~/.jobagent/`), outside the tree. Only fake fixtures in `examples/` are tracked. |
| No secrets in commits | `.gitignore`, a `gitleaks` pre-commit hook, and `scripts/check_pii.py`, which rejects emails, phone numbers, private keys, your own name, email and phone (`JOBAGENT_OWNER_*`), and private file types (`.env`, `*.pdf`, `*.sqlite`, key JSON, ...) even if force-added. It checks commit messages too. |
| No secrets in history | GitHub Actions runs gitleaks over the full history and the PII check on every push and PR. |
| No secrets in logs or the terminal | Config is loaded with pydantic-settings as `SecretStr`, and config objects never print their values. Validation errors never echo input. A logging filter redacts key patterns (`sk-ant-`, `AIza`, `lsv2_`, `ya29.`, `tskey-`, JWTs, private keys, ...), Tailscale hostnames and IPs, and the value of every secret env var. |
| No tracing by default | LangSmith tracing is forced off unless `LANGSMITH_TRACING=true`, because traces contain personal data. |
| Narrow LLM tools | No shell tool. File tools reject paths outside the working dirs, and subprocess calls use fixed argument lists. |
| Human submits | The form filler never clicks submit or apply. On a CAPTCHA or login wall it stops and hands control back. |
| AI coding assistants | `.claude/settings.json` denies reading `.env`, `secrets/`, and `~/.jobagent/`. A hook blocks Bash commands that would print them. |

Found a leak? Rotate the key first, then rewrite history. Deleting the file is
not enough.

## Setup

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/),
[gitleaks](https://github.com/gitleaks/gitleaks), and
[pre-commit](https://pre-commit.com/), plus Qdrant and Ollama for the bullet
bank. TeX Live and poppler for compiling (Debian/Ubuntu):

```bash
sudo apt install texlive-latex-extra texlive-fonts-recommended texlive-fonts-extra lmodern poppler-utils
```

```bash
git clone <this repo> && cd job-agent
uv sync                     # create .venv and install dependencies
pre-commit install          # installs the pre-commit and commit-msg hooks
cp .env.example .env        # then fill in values in your editor
uv run pytest               # includes redaction and PII-check tests
```

Never paste keys into an issue, a chat, or a terminal command. Put them in
`.env` in your editor.

## Usage (resume pipeline)

```bash
uv run jobagent init      # creates ~/.jobagent/ (0700) with FAKE example files
# replace ~/.jobagent/bullets.yaml and resume.tex with your own, then:
uv run jobagent doctor    # checks setup; prints statuses only, never values
uv run jobagent ingest    # embeds bullets.yaml into the Qdrant bullet bank
uv run jobagent tailor --url https://example.com/jobs/123
uv run jobagent tailor --file posting.txt     # or --file - to read stdin
```

The PDF lands in `~/.jobagent/output/<company>_<role>_<application-id>/`. If a run is
interrupted, running the same command again resumes from the SQLite checkpoint
(`--fresh` starts over).

Successful resume runs also save a local application record and print its stable
`app-...` ID. New output directories include that ID so distinct postings with
the same company and role keep separate files. Records live in
`$JOBAGENT_HOME/checkpoints/applications.sqlite`; the record stores a relative
resume path, posting URL, company, role and status, without profile answers.

### Application tracking

The local journal works without Google credentials. View or update a record:

```bash
uv run jobagent status --application app-...
uv run jobagent status --application app-... --set review_needed
uv run jobagent status --application app-... --set blocked
# Only after you have submitted the application yourself:
uv run jobagent status --application app-... --set submitted --confirm-submitted
```

The initial status is `resume_ready`. Regenerating the resume preserves an
existing status. Nothing marks an application submitted automatically.

For optional Sheets sync, install `uv sync --extra tracker`, enable the Google
Sheets API in your Google Cloud project, and share your spreadsheet with the
service account as an editor. Create an empty worksheet named `Applications`.
Set `GOOGLE_SHEET_ID` in your local environment and place the service-account
key at `$JOBAGENT_HOME/secrets/service_account.json` (or configure
`GOOGLE_SERVICE_ACCOUNT_FILE` to a file inside `$JOBAGENT_HOME`). Then:

```bash
uv run jobagent track --application app-...  # sync one local record
uv run jobagent sync                        # retry all pending changes
```

The tracker writes fixed columns with literal values using Sheets `RAW` mode;
formula-like posting text cannot become a spreadsheet formula. Repeated writes
update the row identified by the application ID. A failed remote call leaves
the local record pending and preserves the PDF. Each retry reads remote IDs,
including after a write whose response was lost. Use one local journal per
worksheet: independent machines writing concurrently to the same worksheet
are not supported. Keep the generated headers and application IDs intact;
unexpected headers or duplicate IDs stop syncing. Runtime credentials and a
live spreadsheet are required for live API verification; the automated tests
exercise a fake service and request arguments offline.

### Local application workspace

Run the local browser workspace on Linux, WSL, or native Windows. On Windows,
use an NTFS volume and a dedicated private home, such as `~/.jobagent`. The
first run may take longer while Job Agent repairs and verifies existing file
permissions. Reparse points and hard-linked files in that home stop startup;
move those files out of the private home before trying again. An existing
custom Windows home without Job Agent's private-home marker is rejected so a
mistaken path cannot change unrelated files. WSL remains an option if you
prefer a Linux runtime; keep its private home on the Linux filesystem.

```bash
uv sync --extra browser --extra tracker
uv run playwright install chromium
```

Start the workspace:

```bash
uv run jobagent portal
```

The command opens the dashboard in your browser and also prints its loopback
URL if automatic opening is unavailable. The URL contains a random access
token; keep it private and close the portal when finished. Enter your verified
profile facts, then add a folder of existing resumes outside this public repo.
The portal extracts content from PDF, DOCX, ODT, and TXT files. PDF extraction
uses `pypdf` (installed by default) or `pdftotext`; scanned or encrypted PDFs
need a readable copy. Indexing and matching use local Qdrant and local text
matching by default. The private index stores vectors, opaque IDs, and content
digests; extracted text and file paths are not stored in Qdrant. You can also
index a folder without opening the portal:

```bash
uv run jobagent index-resumes --folder /path/to/private/resumes
```

Running `index-resumes` for another folder keeps entries from earlier folders.
The portal refreshes its index from all folders configured in the workspace.

For **Prepare an application**, enter a company, role, posting URL, and job
description. The portal ranks the content of existing resumes, copies the
best matching readable PDF into the private output folder under a company name,
and records the application. It preserves the original PDF bytes. This flow
selects an existing resume; it does not rewrite or compile a tailored resume.
Use `jobagent tailor` for the LaTeX tailoring pipeline. Review the selected PDF
and every form field yourself before submission. Extra profile answers are
filled only when an exact, unique question label matches a saved answer and the
field is a supported text input. Never save an answer you cannot verify.

For Google Sheets syncing, install the `tracker` extra and configure the
private service-account key above. In **Tracker**, paste a Google
Sheets spreadsheet ID or its `docs.google.com/spreadsheets/d/.../edit` URL and
choose it. The choice is saved in `$JOBAGENT_HOME/tracker_config.json` and takes
precedence over `GOOGLE_SHEET_ID` for the portal; the CLI still uses the
environment setting. Each destination has separate pending-sync state. Use
**Sync pending applications** or the per-application **Sync sheet** button.
The page shows a short suffix of the selected ID and a pending count. Sync
failures leave local records pending.

The tracker instruction box accepts `use spreadsheet <ID or URL>`, `sync all`,
`sync app-...`, and `show pending`. It accepts only these commands; it does not
interpret arbitrary prose. The worksheet is named `Applications` and has the
fixed columns documented above. The portal never submits applications and
requires a separate click to record that you submitted one yourself.

### Chrome side panel

The optional side panel uses the same private local workspace. Run on Linux or
WSL after installing the dependencies above:

```bash
uv run jobagent extension-server
```

Run either `portal` or `extension-server` at a time. Their embedded Qdrant
indexes lock the private data directory while a server is open.

In Chrome 116 or newer, open `chrome://extensions`, turn on Developer mode,
choose **Load unpacked**, and select this repository's `extension/` directory.
If the server checkout is in WSL, the matching `extension/` directory can be
loaded from a Windows checkout of the same commit.
Click the Job Agent icon to open the side panel. Paste the exact loopback URL
printed by `extension-server` into **Connect**. The server URL contains a
random token, stays in Chrome session storage, and expires when the server
stops. The server accepts the first authenticated extension origin for that
run and rejects website origins. Keep the URL private. The extension connects
only to `127.0.0.1`.

The panel can save verified profile facts, add a private resume folder, use
selected job text from an HTTPS tab, prepare an application, choose a Google
Sheet, sync records, and open a form for review. Browser review begins with
manual actions paused. Click **Enable manual actions** only when ready to
complete the form yourself. **Record submitted** requires a separate explicit
confirmation after you have submitted the application. The agent never clicks
Submit or Apply.

Save reusable experience and answer context under **Reusable context notes**.
Notes live in `$JOBAGENT_HOME/context_notes.json`; a private embedded Qdrant
index stores local vectors and opaque IDs for retrieval. Search returns
suggestions for you to inspect; it never fills a form from a fuzzy match.
Resume matching uses a separate local Qdrant index. Both indexes work without
a hosted vector database or embedding API.

### On-page autofill and automatic tracking

With the side panel connected, an **Autofill** button appears on any HTTPS page
that looks like an application form (including embedded ATS iframes):

- Profile fields and exact saved answers fill first (green outline).
- If `GROQ_API_KEY` is set in `.env`, Groq fills remaining fields from your saved
  profile facts only (amber outline). Choice answers must be one of the field's
  options and free-text answers must appear in your facts; anything else stays
  blank. Your profile facts and the form's labels are sent to Groq.
- Fields you already typed in, file uploads, and passwords are never touched.
- It never clicks Submit or Apply. When **you** click Submit/Apply on a page you
  autofilled, the application is recorded as submitted and synced to your Sheet
  (or kept pending if the Sheet is unavailable). Logged once per page.

Custom dropdown widgets that are not real `<select>` elements are not filled yet.

### Browser filling and human review

Install `uv sync --extra browser` and `uv run playwright install chromium`.
Create `$JOBAGENT_HOME/profile.yaml` in your editor using this schema and your
own facts; keep it private. The required fields are `first_name`, `last_name`
and `email`. Optional fields are `phone`, `location`, `country`, `linkedin`
and `website`. Values must be single-line strings; email and URL formats are
validated, unknown keys are rejected, and diagnostics omit the private values.

```yaml
first_name: Fake
last_name: Applicant
email: fake@example.invalid
country: Canada
```

After tailoring a posting from its supported hosted form URL, run in an interactive
terminal:

```bash
uv run jobagent fill --application app-...
```

Greenhouse URLs are HTTPS `boards.greenhouse.io/<company>/jobs/<number>` and
`job-boards.greenhouse.io/<company>/jobs/<number>`. Its adapter supports
top-level HTML forms with `id="application_form"`. Embedded forms, modern custom
widgets, unsupported URLs, CAPTCHA and login challenges stop automatic filling.
The browser remains open for manual review, including when a page is blocked.
Press Enter in the terminal when finished to close the browser. A persistent
browser profile is stored only inside `$JOBAGENT_HOME/browser_profile`.

Only exact known labels receive profile facts. A native country select requires
one exact option match. Duplicate labels or IDs, unknown questions, missing
facts, consent controls, demographic questions and screening questions are
skipped. Only the journal's generated PDF inside `$JOBAGENT_HOME/output` can be
attached to the resume input. The agent never clicks controls or presses keys.

Lever supports HTTPS `jobs.lever.co/<site>/<posting-uuid>/apply` and
`jobs.eu.lever.co/<site>/<posting-uuid>/apply`, with one top-level form. Only
uniquely labeled, visible **Full name** and **Email** text inputs are filled;
full name joins your first and last name. The expected field names must also
match. Resume uploads, phone, custom questions, demographic questions and
consent remain for manual completion. This avoids Lever's upload parsing
requests. Missing or unfamiliar form structure is skipped or blocked. The
origin and posting path must stay exactly the same throughout automatic filling.
The adapter follows Lever's [hosted-form documentation](https://github.com/lever/postings-api)
and public [form-label evidence](https://jobs.lever.co/reply/77d1b33a-b294-4fc0-a707-e496058bbc3f/apply).
Selectors are conservative; live compatibility remains unverified.

During automatic filling it blocks network write requests; upload widgets that
POST immediately may need manual completion. The guard stays active after
filling until you explicitly enable manual actions in the terminal or portal.
You can close the review browser without enabling them. The journal records
`review_needed` or `blocked`; only your explicit status command can record
`submitted`.

Posting URLs saved to the journal and Sheets omit credentials, query strings
and fragments, which can carry tokens. Their full source is used only to hash
the stable application ID. No live applications are visited by the automated
tests: fake pages verify actions, challenge handling and browser lifetime.
Playwright APIs follow the official [input actions](https://playwright.dev/python/docs/input)
and [persistent browser context](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context)
documentation.

### Offline workflow demonstration

Run `uv run pytest tests/test_workflow.py -q` after installing the development
dependencies. This test runs the real CLI and resume graph, saves the journal
record, fills a fake form through the browser executor, waits for a simulated
human review, retries a Sheets write whose response was lost, and records
submitted status only after explicit confirmation. It checks that one remote
row remains and that sync failures preserve the PDF.

Every posting, profile and application in this demonstration is fictional.
Fetching, models, compiler, browser and Sheets HTTP are fakes, and socket/DNS
calls fail the test. It needs no keys, running Qdrant/Ollama, TeX, Chromium or
Google account. Passing it verifies integration and retry behavior; live site
compatibility and external setup still require separate verification.

### Capture a posting that needs JavaScript or login

With the browser extra and Chromium installed, run in an interactive terminal:

```bash
uv run jobagent capture --url https://example.com/jobs/123
# The command prints an opaque cap-... ID after you confirm the selection.
uv run jobagent tailor --capture cap-...
```

The headed browser uses your private persistent browser profile. Navigate or
log in yourself in the opened tab, select **only the job-description text**,
then press Enter in the terminal to save the selection and close the browser.
Capture performs no form actions and reads only
`window.getSelection().toString()` after that confirmation. It never reads the
page body, input values, HTML or screenshots. Empty selections and descriptions
outside 200–30000 characters are rejected with messages that omit the text.
Copy text out of an embedded frame into a local text file if it cannot be
selected from the opened page.

Captures live only in `$JOBAGENT_HOME/captures`, containing the final HTTPS
posting URL at confirmation time and selected text. If login or manual
navigation changes the page, the confirmed page URL becomes the source.
URLs must omit embedded login credentials.
Query strings remain private in the capture; journal and Sheets URLs are
sanitized. The `--capture`, `--url` and `--file` tailoring sources are mutually
exclusive. Tailoring from a capture passes both URL and selected text into the
resume graph and skips fetching the page again. It still uses the configured
models and resume services. Automated tests use fake browsers and cover both
direct URL and captured-posting workflow variants without network access.

### Development and usage allowance

See [the usage plan](docs/USAGE_PLAN.md) for the project's coding-agent allowance
policy and development workflow.

```
parse_job -> retrieve_bullets -> tailor_resume -> compile_resume -> verify_one_page
                                                       ^                  |
                                                       +--- trim_resume <-+  (over one page)
                                                                  |
                                                        report_overflow  (all entries at 2 bullets)
```

- **parse_job**: Sonnet (or Gemini, if enabled) extracts title, company, skills
  and domains. The posting is treated as untrusted data.
- **retrieve_bullets**: embeds the posting with Ollama `nomic-embed-text` and
  ranks your bullets in Qdrant. Qdrant stores only vectors and ids; the text
  stays in `bullets.yaml`.
- **tailor_resume**: Opus selects and orders bullets and skills. Code then
  enforces the rules. Unknown ids are dropped, every job is kept, skills must
  come from your vocabulary, and a rephrasing that adds a number, skill, tool
  or LaTeX command is reverted to your original text.
- **compile_resume**: replaces only the `% BEGIN:X` / `% END:X` blocks, checks
  brace balance and `\resumeItem{...}` arguments, and runs `pdflatex` twice
  with `-no-shell-escape`.
- **verify_one_page**: `pdfinfo` page count. If it's over one page, the least
  relevant bullet is dropped and the resume recompiled. A job or project never
  loses its last 2 bullets. If it still doesn't fit, the run stops and names
  the sections that run past page 1 (found with `pdftotext`).

Template conventions: Jake's resume macros (`\resumeSubheading`,
`\resumeItem`, `\resumeProjectHeading`). Bullets may be written as
`\resumeItem{\justify{text}}`: the checks accept exactly one `\justify{...}`
wrapping the whole bullet, and generated bullets keep the wrapper when your
template uses it. `examples/resume.tex` (extarticle, 9pt) shows the pattern.

## License

MIT
