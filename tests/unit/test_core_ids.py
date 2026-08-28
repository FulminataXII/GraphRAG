from __future__ import annotations

import time
import uuid

from hypothesis import given
from hypothesis import strategies as st

from graphrag.core.ids import chunk_id, new_correlation_id, normalize_for_hash


@given(text=st.text())
def test_normalize_idempotent(text: str) -> None:
    once = normalize_for_hash(text)
    assert normalize_for_hash(once) == once


def test_normalize_collapses_whitespace() -> None:
    # NBSP, tabs, and newlines are WHITESPACE: they collapse to a single U+0020.
    whitespace_variants = [
        "hello world",
        "hello\u00a0world",  # NBSP
        "hello\tworld",
        "hello\nworld",
        "hello   world",
    ]
    assert {normalize_for_hash(v) for v in whitespace_variants} == {"hello world"}

    # Zero-width characters are stripped outright -- they are not whitespace, so no space is
    # left behind where they used to sit.
    assert normalize_for_hash("hello\u200bworld") == "helloworld"
    assert normalize_for_hash("hello\u200c\u200d\u2060\ufeffworld") == "helloworld"


def test_chunk_id_is_rfc4122_valid() -> None:
    cid = chunk_id("hello world")
    assert cid.version == 5
    assert cid.variant == uuid.RFC_4122
    assert chunk_id("hello world") == cid


def test_chunk_id_stable_across_whitespace() -> None:
    assert chunk_id("hello   world") == chunk_id("hello world")
    assert chunk_id("hello\tworld\n") == chunk_id("hello world")


def test_chunk_id_differs_on_content() -> None:
    assert chunk_id("hello world") != chunk_id("hello worlds")


def test_correlation_id_sortable() -> None:
    ids = []
    for _ in range(6):
        ids.append(new_correlation_id())
        time.sleep(0.002)  # force distinct millisecond timestamps
    assert ids == sorted(ids)
    assert all(len(cid) == 26 for cid in ids)
