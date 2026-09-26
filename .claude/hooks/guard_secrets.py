#!/usr/bin/env python3
"""Claude Code PreToolUse hook: block Bash commands that would expose secrets.

The Read/Edit deny rules in .claude/settings.json only cover Claude's file
tools; `cat .env` through Bash would bypass them. This hook closes the obvious
Bash routes. It is best-effort pattern matching, not a sandbox.

Exit code 2 blocks the command and shows the reason to Claude.
"""

from __future__ import annotations

import json
import re
import sys

RULES = [
    # .env, .env.local, path/to/.env ... but not .env.example
    (r"(^|[\s'\"=/<>|;&(])\.env(\.(?!example\b)[\w.-]+)?(?=$|[\s'\"<>|;&)])", ".env file"),
    (r"(~|\$\{?HOME\}?|/home/[^/\s]+|/root)/\.jobagent\b", "~/.jobagent (personal data)"),
    (r"\$\{?JOBAGENT_HOME\b", "$JOBAGENT_HOME (personal data)"),
    (r"(^|[\s'\"=/])secrets/", "secrets/ directory"),
    (r"(^|[;&|(]\s*)(printenv|env)\s*($|[|;&>)])", "dumping the environment"),
    (r"(^|[;&|(]\s*)(export\s+-p|declare\s+-x|set)\s*($|[|;&>)])", "dumping the environment"),
    (r"\$\{?\w*(API_KEY|TOKEN|SECRET|PASSWORD|OWNER_(NAME|EMAIL|PHONE))\w*", "secret environment variable"),
    (r"/proc/[^\s]*/environ", "process environment"),
]


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    command = (payload.get("tool_input") or {}).get("command") or ""
    for pattern, what in RULES:
        if re.search(pattern, command):
            print(
                f"Blocked by .claude/hooks/guard_secrets.py: command touches {what}. "
                "Secrets and personal data must not be read by the assistant. "
                "Ask the user to run it themselves if it is truly needed.",
                file=sys.stderr,
            )
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
