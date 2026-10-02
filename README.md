# job-agent

A personal job-application agent. Given a job posting URL or pasted text, it:

1. parses the job description,
2. tailors a LaTeX resume from a bank of **real** bullets and compiles it locally,
3. pre-fills the application form in a browser,
4. **stops for human review** (it never clicks submit), and
5. logs the application to a Google Sheet.

> Status: under construction. Done: security scaffolding (Phase 0) and the
> resume pipeline (Phase 1). Next: Sheets tracker, form filler, multi-site support.

## Security model

This repository is public. The code is shared; the data never is.

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

The PDF lands in `~/.jobagent/output/<company>_<role>/`. If a run is
interrupted, running the same command again resumes from the SQLite checkpoint
(`--fresh` starts over).

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
