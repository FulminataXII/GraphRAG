"""Entrypoint: `python -m graphrag.config.validate`.

Contract:
    - Exit 0 and print the config_hash on success.
    - Exit 1 and print the full Pydantic error tree on failure.
    - Never prints a secret value.
Used as the compose init gate before api/worker start.
"""

from __future__ import annotations

import sys

from pydantic import ValidationError

from graphrag.config.settings import Settings


def main() -> int:
    try:
        settings = Settings()
    except ValidationError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"config_hash={settings.config_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
