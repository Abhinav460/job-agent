# job-agent

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
> Next: additional form variants, site adapters and live verification.

## Security model

This repository is public. The code is shared; the data never is.

Use Linux or WSL for live private data until Windows ACL support is implemented.
The current `0700`/`0600` directory and file controls use POSIX permissions;
Windows `chmod` and `umask` do not establish equivalent private access controls.

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
POST immediately may need manual completion. That guard is removed when control
passes to you for review and manual submission. The journal records
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
