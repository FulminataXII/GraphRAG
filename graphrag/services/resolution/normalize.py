"""`normalize_entity_name`. See BLUEPRINT §6.2.

Deliberately a *different* normalization from `core.ids.normalize_for_hash`: that one is generic
(NFKC/casefold/whitespace, used for content-addressed hashing everywhere) and never strips
tokens. This one additionally strips honorifics/legal suffixes, which is specific to matching
entity *names* against each other and would be wrong to apply to chunk-text hashing.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from typing import Final

_PUNCTUATION_RE: Final[re.Pattern[str]] = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WHITESPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")


def _clean(text: str) -> str:
    """NFKC -> casefold -> punctuation to spaces -> collapse whitespace -> strip."""
    result = unicodedata.normalize("NFKC", text).casefold()
    result = _PUNCTUATION_RE.sub(" ", result)
    return _WHITESPACE_RE.sub(" ", result).strip()


def _token_sequence(phrase: str) -> tuple[str, ...]:
    return tuple(_clean(phrase).split(" ")) if _clean(phrase) else ()


def normalize_entity_name(
    name: str, *, strip_suffixes: Sequence[str], strip_honorifics: Sequence[str]
) -> str:
    """Contract (BLUEPRINT §6.2):
    - NFKC -> casefold -> strip punctuation -> collapse whitespace.
    - Remove honorifics only at the START, as whole tokens.
    - Remove legal suffixes only at the END, as whole tokens.
    - Whole-token matching is mandatory: "Corporation Street" keeps "corporation".
    - Idempotent. Never returns an empty string; falls back to the casefolded original.
    """
    cleaned = _clean(name)
    if not cleaned:
        # The name was entirely whitespace/punctuation/empty to begin with. There is nothing
        # non-empty to fall back to except the raw input itself.
        return name.casefold().strip() or name

    tokens = list(cleaned.split(" "))

    honorifics = [seq for phrase in strip_honorifics if (seq := _token_sequence(phrase))]
    suffixes = [seq for phrase in strip_suffixes if (seq := _token_sequence(phrase))]

    changed = True
    while changed:
        changed = False
        for seq in honorifics:
            n = len(seq)
            if len(tokens) > n and tuple(tokens[:n]) == seq:
                tokens = tokens[n:]
                changed = True
                break

    changed = True
    while changed:
        changed = False
        for seq in suffixes:
            n = len(seq)
            if len(tokens) > n and tuple(tokens[-n:]) == seq:
                tokens = tokens[:-n]
                changed = True
                break

    result = " ".join(tokens).strip()
    return result or cleaned


__all__ = ["normalize_entity_name"]
