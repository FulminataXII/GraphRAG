from __future__ import annotations

import pytest

from graphrag.config import validate
from tests.unit._settings_helpers import set_required_secrets


def test_validate_cli_exit_codes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The autouse tests/conftest.py fixture already isolates us from the repo's real .env
    # (which legitimately carries a real admin key for local dev), so the "missing secret"
    # case below isn't silently backfilled by it.
    set_required_secrets(monkeypatch)
    assert validate.main() == 0
    out = capsys.readouterr().out
    assert "config_hash=" in out

    monkeypatch.delenv("GRAPHRAG_SECRETS__ADMIN_API_KEY", raising=False)
    assert validate.main() == 1
    err = capsys.readouterr().err
    assert "admin_api_key" in err
    assert "sk-" not in err
