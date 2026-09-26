# job-agent

A personal job-application agent. Given a job posting URL or pasted text, it:

1. parses the job description,
2. tailors a LaTeX resume from a bank of **real** bullets and compiles it locally,
3. pre-fills the application form in a browser,
4. **stops for human review** (it never clicks submit), and
5. logs the application to a Google Sheet.

> Status: under construction. Phase 0 (security scaffolding) is in place;
> the pipeline is being built in phases.

## Security model

This repository is public. The code is shared; the data never is.

| Rule | How it is enforced |
| --- | --- |
| No personal data in the repo | Resume, profile, bullets, keys, browser profile, checkpoints and PDFs live in `$JOBAGENT_HOME` (default `~/.jobagent/`), outside the tree. Only fake fixtures in `examples/` are tracked. |
| No secrets in commits | `.gitignore`, a `gitleaks` pre-commit hook, and `scripts/check_pii.py`, which rejects emails, phone numbers, private keys and private file types (`.env`, `*.pdf`, `*.sqlite`, key JSON, ...) even if force-added. |
| No secrets in history | GitHub Actions runs gitleaks over the full history and the PII check on every push and PR. |
| No secrets in logs or the terminal | Config is loaded with pydantic-settings as `SecretStr`, and config objects never print their values. A logging filter redacts key patterns (`sk-ant-`, `AIza`, `lsv2_`, `ya29.`, private keys, ...) and the value of every secret env var. |
| No tracing by default | LangSmith tracing is forced off unless `LANGSMITH_TRACING=true`, because traces contain personal data. |
| Narrow LLM tools | No shell tool. File tools reject paths outside the working dirs, and subprocess calls use fixed argument lists. |
| Human submits | The form filler never clicks submit or apply. On a CAPTCHA or login wall it stops and hands control back. |
| AI coding assistants | `.claude/settings.json` denies reading `.env`, `secrets/`, and `~/.jobagent/`. A hook blocks Bash commands that would print them. |

Found a leak? Rotate the key first, then rewrite history. Deleting the file is
not enough.

## Setup

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/),
[gitleaks](https://github.com/gitleaks/gitleaks), and
[pre-commit](https://pre-commit.com/). Later phases also need TeX Live
(`pdflatex`), poppler (`pdfinfo`), Qdrant, and Ollama.

```bash
git clone <this repo> && cd job-agent
uv sync                     # create .venv and install dependencies
pre-commit install          # installs the pre-commit and commit-msg hooks
cp .env.example .env        # then fill in values in your editor
uv run pytest               # includes redaction and PII-check tests
```

Never paste keys into an issue, a chat, or a terminal command. Put them in
`.env` in your editor.

## License

MIT
