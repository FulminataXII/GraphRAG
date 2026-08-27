from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_no_crlf_in_repo() -> None:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    tracked = [line for line in result.stdout.splitlines() if line]

    offenders = []
    for rel_path in tracked:
        path = REPO_ROOT / rel_path
        if not path.is_file():
            continue
        try:
            content = path.read_bytes()
        except OSError:
            continue
        if b"\r\n" in content:
            offenders.append(rel_path)

    assert offenders == [], f"CRLF line endings found in: {offenders}"
