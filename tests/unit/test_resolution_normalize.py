"""`normalize_entity_name` unit tests. See BLUEPRINT §6.2 / BUILD_ORDER BO-07."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from graphrag.services.resolution.normalize import normalize_entity_name

SUFFIXES = [
    "inc",
    "inc.",
    "ltd",
    "ltd.",
    "llc",
    "corp",
    "corporation",
    "pvt",
    "private limited",
    "gmbh",
    "plc",
    "co",
    "co.",
]
HONORIFICS = ["mr", "mrs", "ms", "dr", "prof", "sir"]


def _norm(name: str) -> str:
    return normalize_entity_name(name, strip_suffixes=SUFFIXES, strip_honorifics=HONORIFICS)


def test_strips_suffixes_and_honorifics() -> None:
    assert _norm("Acme Corp.") == "acme"
    assert _norm("Dr. J. Smith") == "j smith"


def test_does_not_overstrip() -> None:
    """`Corporation Street` keeps `corporation` — suffixes strip only at the END, as whole
    tokens, never mid-string."""
    assert _norm("Corporation Street") == "corporation street"


def test_honorific_only_strips_at_start() -> None:
    assert _norm("Not A Mr Really") == "not a mr really"


def test_three_variants_normalize_identically() -> None:
    assert _norm("Acme Corp.") == _norm("ACME Corporation") == _norm("Acme")


def test_normalize_never_returns_empty() -> None:
    assert _norm("Inc.") != ""
    assert _norm("   ") != ""
    assert _norm("!!!") != ""
    assert _norm("Mr") != ""


@given(st.text(min_size=1, max_size=100))
def test_normalize_entity_idempotent(name: str) -> None:
    once = _norm(name)
    twice = normalize_entity_name(once, strip_suffixes=SUFFIXES, strip_honorifics=HONORIFICS)
    assert once == twice
