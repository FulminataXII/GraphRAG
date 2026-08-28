"""BO-02: telemetry/logging.py. See BUILD_ORDER.md."""

from __future__ import annotations

import json

import structlog
from opentelemetry.sdk.trace import TracerProvider

from graphrag.adapters.telemetry.logging import (
    configure_logging,
    redact_secrets,
)
from graphrag.config.settings import Settings


def _log_lines(captured_out: str) -> list[dict]:
    return [json.loads(line) for line in captured_out.splitlines() if line.strip()]


def test_log_has_static_fields(settings: Settings, capsys) -> None:
    configure_logging(settings, service_role="worker")
    structlog.get_logger().info("something_happened")

    (record,) = _log_lines(capsys.readouterr().out)
    assert record["service"] == settings.observability.service_name
    assert record["role"] == "worker"
    assert record["env"] == settings.app.env
    assert record["version"] == settings.app.version
    assert record["config_hash"] == settings.config_hash


def test_log_has_trace_and_span_id_inside_span(settings: Settings, capsys) -> None:
    configure_logging(settings, service_role="api")

    # A local (non-global) SDK TracerProvider is enough: `trace.get_current_span()` reads the
    # current OTel Context, which `start_as_current_span` sets regardless of which provider
    # issued the tracer — no need to touch the process-wide global provider for this test.
    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("unit-test-span"):
        structlog.get_logger().info("inside_span")

    (record,) = _log_lines(capsys.readouterr().out)
    assert len(record["trace_id"]) == 32
    int(record["trace_id"], 16)  # is valid hex
    assert len(record["span_id"]) == 16
    int(record["span_id"], 16)


def test_log_omits_trace_id_outside_span(settings: Settings, capsys) -> None:
    configure_logging(settings, service_role="api")
    structlog.get_logger().info("outside_span")

    (record,) = _log_lines(capsys.readouterr().out)
    assert "trace_id" not in record
    assert "span_id" not in record


def test_secret_redacted_in_logs(settings: Settings, capsys) -> None:
    configure_logging(settings, service_role="api")
    structlog.get_logger().info(
        "credentials_seen",
        api_key="sk-should-not-appear",
        nested={"password": "hunter2", "safe": "ok"},
        items=[{"authorization": "Bearer sk-nope"}, "plain-string"],
    )

    (record,) = _log_lines(capsys.readouterr().out)
    rendered = json.dumps(record)
    assert "sk-should-not-appear" not in rendered
    assert "hunter2" not in rendered
    assert "Bearer sk-nope" not in rendered
    assert record["nested"]["safe"] == "ok"
    assert record["items"][1] == "plain-string"


def test_secret_value_prefix_redacted_regardless_of_key(settings: Settings, capsys) -> None:
    """A value that itself looks like a key (sk-/gsk_/AIza prefix) is redacted even under a
    field name that doesn't match any key pattern."""
    configure_logging(settings, service_role="api")
    structlog.get_logger().info(
        "unlabeled_secret",
        random_field="sk-1234567890abcdef",
        another="gsk_shouldnotshow",
        harmless="skate-not-a-secret",
    )

    (record,) = _log_lines(capsys.readouterr().out)
    assert record["random_field"] == "**********"
    assert record["another"] == "**********"
    assert record["harmless"] == "skate-not-a-secret"


def test_redaction_depth_capped() -> None:
    # Build a structure nested 10 levels deep with a secret key only at the very bottom.
    payload: dict = {"password": "leaked-at-the-bottom"}
    for _ in range(10):
        payload = {"child": payload}

    redacted = redact_secrets(None, "info", {"root": payload})

    rendered = json.dumps(redacted)
    # Depth-capped at 6: a secret nested deeper than that must survive redaction.
    assert "leaked-at-the-bottom" in rendered


def test_redact_secrets_recurses_shallow_dicts_and_lists() -> None:
    event = {
        "token": "sk-abc123",
        "list": [{"secret": "shh"}, {"ok": "fine"}],
    }
    redacted = redact_secrets(None, "info", event)
    assert redacted["token"] == "**********"
    assert redacted["list"][0]["secret"] == "**********"
    assert redacted["list"][1]["ok"] == "fine"
