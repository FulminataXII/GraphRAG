"""Content addressing and correlation IDs. See BLUEPRINT §3.1.

Pure utilities: the only "I/O" is reading the wall clock and OS randomness for
`new_correlation_id`, which is intrinsic to what a correlation ID *is* (a real timestamp, real
entropy) rather than business-logic time that must flow through the injected Clock port.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import time
import unicodedata
from typing import Final, Literal
from uuid import UUID

CHUNK_ID_VERSION: Final[int] = 5

_ZERO_WIDTH_CHARS: Final[frozenset[str]] = frozenset(
    {
        "\u200b",  # zero width space
        "\u200c",  # zero width non-joiner
        "\u200d",  # zero width joiner
        "\u2060",  # word joiner
        "\ufeff",  # zero width no-break space / BOM
    }
)

_WHITESPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")

# Crockford base32: excludes I, L, O, U to avoid confusion with 1, 1, 0, V.
_CROCKFORD_ALPHABET: Final[str] = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def normalize_for_hash(
    text: str,
    *,
    unicode_form: Literal["NFC", "NFD", "NFKC", "NFKD"] = "NFKC",
    casefold: bool = True,
    strip_zero_width: bool = True,
) -> str:
    """Canonical form used ONLY for hashing. Never stored, never displayed.

    Contract:
        - Idempotent: f(f(x)) == f(x) for all x.
        - Deterministic across processes and platforms.
        - Order: strip zero-width -> unicode normalize -> collapse all whitespace runs to a
          single U+0020 -> strip -> casefold (if enabled).
    """
    result = text
    if strip_zero_width:
        result = "".join(ch for ch in result if ch not in _ZERO_WIDTH_CHARS)
    result = unicodedata.normalize(unicode_form, result)
    result = _WHITESPACE_RE.sub(" ", result).strip()
    if casefold:
        result = result.casefold()
    return result


def content_hash(text: str) -> str:
    """sha256 hex of normalize_for_hash(text). 64 chars. Stored as payload.content_hash."""
    return hashlib.sha256(normalize_for_hash(text).encode("utf-8")).hexdigest()


def chunk_id(text: str) -> UUID:
    """Deterministic content-addressed chunk identifier.

    Contract:
        - Returns uuid.UUID(bytes=sha256(normalize_for_hash(text)).digest()[:16], version=5)
        - MUST pass version=5. Omitting it leaves the RFC 4122 version nibble as an accidental
          hash artefact.
        - Identical normalized text ALWAYS yields the same UUID; this is the sole dedup
          mechanism.
        - Do not substitute a 64-bit integer ID.
    """
    digest = hashlib.sha256(normalize_for_hash(text).encode("utf-8")).digest()
    return UUID(bytes=digest[:16], version=CHUNK_ID_VERSION)


def entity_id(canonical_name: str, entity_type: str) -> UUID:
    """Deterministic entity ID from normalized name + type. Same construction as chunk_id."""
    combined = f"{normalize_for_hash(canonical_name)}\x1f{entity_type}"
    digest = hashlib.sha256(combined.encode("utf-8")).digest()
    return UUID(bytes=digest[:16], version=CHUNK_ID_VERSION)


def new_correlation_id() -> str:
    """26-char Crockford base32 ULID. Sortable by creation time.

    Contract:
        - 48-bit millisecond timestamp + 80 bits of randomness (standard ULID layout).
        - Stateless: reads the real wall clock and OS randomness on every call. Two IDs
          generated within the same millisecond are not guaranteed to sort relative to each
          other — a monotonic counter would require module-level mutable state, which core/
          may not hold (BLUEPRINT §0). Callers needing strict same-millisecond ordering should
          space calls.
    """
    timestamp_ms = int(time.time() * 1000)
    randomness = secrets.token_bytes(10)
    value = timestamp_ms.to_bytes(6, "big") + randomness
    number = int.from_bytes(value, "big")
    chars = [_CROCKFORD_ALPHABET[(number >> (5 * (25 - i))) & 0x1F] for i in range(26)]
    return "".join(chars)
