import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_pii.py"


def run(tmp_path, content):
    f = tmp_path / "sample.txt"
    f.write_text(content)
    return subprocess.run([sys.executable, SCRIPT, str(f)], capture_output=True, text=True)


@pytest.mark.parametrize(
    "content",
    [
        "reach me at jane.q" + "@" + "gmail.com",
        "cell (973) 555-" + "8812",
        "+44 " + "20 7946 0958",
        "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
    ],
)
def test_flags_pii(tmp_path, content):
    result = run(tmp_path, content)
    assert result.returncode == 1
    # The finding is reported without echoing the matched text.
    assert content.split()[-1] not in result.stdout


@pytest.mark.parametrize(
    "content",
    [
        "jane.doe@example.com",
        "555-0142",
        "version 1.2.3 on 2026-09-26, x = a+123",
        "Co-Authored-By: Claude <noreply@anthropic.com>",
    ],
)
def test_allows_fake_and_benign(tmp_path, content):
    assert run(tmp_path, content).returncode == 0
