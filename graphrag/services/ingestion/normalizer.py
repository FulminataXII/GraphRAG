"""Light cleanup for storage/display. See BLUEPRINT §6.1.

Distinct from `core.ids.normalize_for_hash`: that function produces a canonical form used ONLY
to compute a stable hash and is never stored or shown. This one produces the text that actually
gets stored in `Chunk.text` and displayed to a user, so it must preserve case, punctuation, and
meaningful whitespace — it only removes things that are unambiguously noise (control characters,
inconsistent line endings).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

# C0/C1 control characters, excluding \t (0x09) and \n (0x0A), which are meaningful whitespace.
# \r (0x0D) is excluded too: it's handled by the newline-normalization step below, not stripped.
_CONTROL_CHARS_RE: Final[re.Pattern[str]] = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def normalize_display(text: str) -> str:
    """Light cleanup for STORAGE and display: fix mojibake, normalize newlines, strip control
    chars. Preserves case, punctuation and meaningful whitespace.

    Contract:
        - NFC unicode normalization (composes combining characters; unlike NFKC this does not
          fold compatibility variants, so visually distinct characters stay distinct).
        - CRLF and lone CR are normalized to LF.
        - Control characters other than \\t and \\n are stripped.
        - Whitespace RUNS are left untouched — this is the storage/display form, not the hash
          form; `core.ids.normalize_for_hash` owns whitespace collapsing.

    NOT the same as `core.ids.normalize_for_hash` — never use one where the other belongs.
    """
    result = unicodedata.normalize("NFC", text)
    result = result.replace("\r\n", "\n").replace("\r", "\n")
    result = _CONTROL_CHARS_RE.sub("", result)
    return result
