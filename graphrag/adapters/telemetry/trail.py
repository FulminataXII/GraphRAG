"""Debug trail bundle assembly. See BLUEPRINT §4.6 / ARCHITECTURE §3.4.

Merges logs (queried from Loki) and spans (queried from Tempo) into one paste-ready markdown
bundle for a single correlation_id.

A `TrailBuilder` is single-use: it owns an `httpx.AsyncClient` (unless one is injected — the
knob unit tests use to point it at an `httpx.MockTransport`) and closes it at the end of
`build()`, matching the CLI's "each command builds its own dependencies and closes them" pattern
(BLUEPRINT §7.3).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict

from graphrag.adapters.telemetry.logging import redact_secrets

if TYPE_CHECKING:
    from graphrag.config.settings import Settings

_LOOKBACK = timedelta(hours=24)


class TrailEvent(BaseModel):
    """One merged, time-ordered record — a log line or a span."""

    model_config = ConfigDict(frozen=True)

    at: AwareDatetime
    source: Literal["log", "span"]
    level: str
    message: str
    attributes: dict[str, Any]
    trace_id: str | None = None
    span_id: str | None = None


class TrailBundle(BaseModel):
    correlation_id: str
    trace_ids: list[str]
    config_hash: str
    events: list[TrailEvent]
    truncated: bool
    sources_failed: list[str]

    def to_markdown(self) -> str:
        """Header (cid, config_hash, span count, failures) then a time-ordered event table,
        then full detail for every event with level >= ERROR."""
        span_count = sum(1 for e in self.events if e.source == "span")
        lines = [
            f"# Debug trail — {self.correlation_id}",
            "",
            f"- config_hash: {self.config_hash}",
            f"- trace_ids: {', '.join(self.trace_ids) or '(none)'}",
            f"- events: {len(self.events)} "
            f"({span_count} spans, {len(self.events) - span_count} logs)",
            f"- truncated: {self.truncated}",
            f"- sources_failed: {', '.join(self.sources_failed) or '(none)'}",
            "",
            "## Timeline",
            "",
            "| time | source | level | message |",
            "|---|---|---|---|",
        ]
        for event in self.events:
            message = event.message.replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {event.at.isoformat()} | {event.source} | {event.level} | {message} |")

        error_events = [e for e in self.events if e.level.upper() in {"ERROR", "CRITICAL"}]
        if error_events:
            lines += ["", "## Failures", ""]
            for event in error_events:
                lines.append(f"### {event.at.isoformat()} — {event.source} — {event.level}")
                lines.append("")
                lines.append(event.message)
                lines.append("")
                lines.append("```json")
                lines.append(json.dumps(event.attributes, indent=2, default=str, sort_keys=True))
                lines.append("```")
                lines.append("")
        return "\n".join(lines)


def _parse_otlp_attributes(attrs: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for attr in attrs:
        key = attr.get("key")
        if key is None:
            continue
        value = attr.get("value", {})
        for value_key in ("stringValue", "intValue", "doubleValue", "boolValue"):
            if value_key in value:
                result[key] = value[value_key]
                break
        else:
            result[key] = value
    return result


class TrailBuilder:
    """Assemble a paste-ready debug bundle for one correlation_id.

    Contract:
        - Queries Loki with {service="graphrag"} | json | correlation_id="<cid>"
          and Tempo with TraceQL { .app.correlation_id = "<cid>" }.
        - Merges into one list ordered by timestamp; spans and logs interleaved.
        - Truncates every field to observability.trail.truncate_field_chars.
        - Applies redact_secrets to every record before rendering.
        - Caps output at observability.trail.max_events; notes the truncation if hit.
        - If either backend is unreachable, renders what it has and states which source failed.
          Never raises.
    """

    def __init__(self, settings: Settings, *, http_client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._trail = settings.observability.trail
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(timeout=10.0)

    async def build(self, correlation_id: str) -> TrailBundle:
        now = datetime.now(UTC)
        start = now - _LOOKBACK

        sources_failed: list[str] = []
        log_events: list[TrailEvent] = []
        span_events: list[TrailEvent] = []
        trace_ids: set[str] = set()

        try:
            log_events = await self._query_loki(correlation_id, start, now)
        except Exception:
            sources_failed.append("loki")

        try:
            span_events, trace_ids = await self._query_tempo(correlation_id, start, now)
        except Exception:
            sources_failed.append("tempo")

        events = sorted([*log_events, *span_events], key=lambda e: e.at)

        truncated = len(events) > self._trail.max_events
        if truncated:
            events = events[: self._trail.max_events]

        events = [self._sanitize(event) for event in events]

        if self._owns_client:
            await self._client.aclose()

        return TrailBundle(
            correlation_id=correlation_id,
            trace_ids=sorted(trace_ids),
            config_hash=self._settings.config_hash,
            events=events,
            truncated=truncated,
            sources_failed=sources_failed,
        )

    def _sanitize(self, event: TrailEvent) -> TrailEvent:
        truncate = self._trail.truncate_field_chars
        redacted_attrs = redact_secrets(None, "info", dict(event.attributes))
        redacted_attrs = {
            key: (value[:truncate] if isinstance(value, str) else value)
            for key, value in redacted_attrs.items()
        }
        return event.model_copy(
            update={"message": event.message[:truncate], "attributes": redacted_attrs}
        )

    async def _query_loki(
        self, correlation_id: str, start: datetime, end: datetime
    ) -> list[TrailEvent]:
        # Loki's OTLP ingestion promotes the `service.name` RESOURCE attribute to a stream
        # label named `service_name` (dots aren't legal in label names) — verified against the
        # otel-lgtm image's default OTLP-to-Loki label mapping. `{service="..."}` (as ARCHITECTURE
        # §3.4's illustrative query shows it) matches no stream at all.
        service_name = self._settings.observability.service_name
        query = f'{{service_name="{service_name}"}} | json | correlation_id="{correlation_id}"'
        response = await self._client.get(
            f"{self._trail.loki_url}/loki/api/v1/query_range",
            params={
                "query": query,
                "start": str(int(start.timestamp() * 1_000_000_000)),
                "end": str(int(end.timestamp() * 1_000_000_000)),
                "limit": str(self._trail.max_events),
            },
        )
        response.raise_for_status()
        payload = response.json()
        events: list[TrailEvent] = []
        for stream in payload.get("data", {}).get("result", []):
            for ts_ns, line in stream.get("values", []):
                events.append(self._parse_log_line(int(ts_ns), line))
        return events

    def _parse_log_line(self, ts_ns: int, line: str) -> TrailEvent:
        at = datetime.fromtimestamp(ts_ns / 1_000_000_000, tz=UTC)
        try:
            fields = json.loads(line)
            if not isinstance(fields, dict):
                fields = {"event": line}
        except (json.JSONDecodeError, TypeError):
            fields = {"event": line}
        message = str(fields.pop("event", None) or fields.pop("message", None) or line)
        level = str(fields.pop("level", "info")).upper()
        trace_id = fields.get("trace_id")
        span_id = fields.get("span_id")
        fields.pop("timestamp", None)
        return TrailEvent(
            at=at,
            source="log",
            level=level,
            message=message,
            attributes=fields,
            trace_id=trace_id,
            span_id=span_id,
        )

    async def _query_tempo(
        self, correlation_id: str, start: datetime, end: datetime
    ) -> tuple[list[TrailEvent], set[str]]:
        query = f'{{ .app.correlation_id = "{correlation_id}" }}'
        response = await self._client.get(
            f"{self._trail.tempo_url}/api/search",
            params={
                "q": query,
                "start": str(int(start.timestamp())),
                "end": str(int(end.timestamp())),
                "limit": str(self._trail.max_events),
            },
        )
        response.raise_for_status()
        payload = response.json()
        trace_ids = {t["traceID"] for t in payload.get("traces", []) if t.get("traceID")}

        events: list[TrailEvent] = []
        for trace_id in trace_ids:
            events.extend(await self._fetch_trace_spans(trace_id))
        return events, trace_ids

    async def _fetch_trace_spans(self, trace_id: str) -> list[TrailEvent]:
        response = await self._client.get(f"{self._trail.tempo_url}/api/traces/{trace_id}")
        response.raise_for_status()
        payload = response.json()
        events: list[TrailEvent] = []
        resource_spans = payload.get("resourceSpans") or payload.get("batches") or []
        for resource_span in resource_spans:
            scope_spans = (
                resource_span.get("scopeSpans")
                or resource_span.get("instrumentationLibrarySpans")
                or []
            )
            for scope_span in scope_spans:
                for span in scope_span.get("spans", []):
                    events.append(self._parse_span(trace_id, span))
        return events

    def _parse_span(self, trace_id: str, span: dict[str, Any]) -> TrailEvent:
        start_ns = int(span.get("startTimeUnixNano", 0) or 0)
        at = (
            datetime.fromtimestamp(start_ns / 1_000_000_000, tz=UTC)
            if start_ns
            else datetime.now(UTC)
        )
        attributes = _parse_otlp_attributes(span.get("attributes", []))
        status = span.get("status") or {}
        status_code = status.get("code")
        level = "ERROR" if status_code in (2, "STATUS_CODE_ERROR") else "INFO"
        return TrailEvent(
            at=at,
            source="span",
            level=level,
            message=str(span.get("name", "")),
            attributes=attributes,
            trace_id=trace_id,
            span_id=span.get("spanId"),
        )
