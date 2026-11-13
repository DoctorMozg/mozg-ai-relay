"""Message ids: ULIDs minted here, and the id check shared with the TS clients' UUIDs."""

from __future__ import annotations

import re
import secrets
import time

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
MAX_MS = (1 << 48) - 1
_TIME_CHARS = 10
_RANDOM_CHARS = 16
_RANDOM_BITS = 80
_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{7,63}")


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        value, digit = divmod(value, 32)
        chars.append(CROCKFORD[digit])
    return "".join(reversed(chars))


def new_id(now_ms: int | None = None) -> str:
    """Returns a 26-char ULID: 48 bits of epoch milliseconds, then 80 random bits."""
    if now_ms is None:
        now_ms = time.time_ns() // 1_000_000
    elif isinstance(now_ms, bool) or not isinstance(now_ms, int) or not 0 <= now_ms <= MAX_MS:
        raise ValueError(f"now_ms must be an int in 0..{MAX_MS}, got {now_ms!r}")
    return _encode(now_ms, _TIME_CHARS) + _encode(secrets.randbits(_RANDOM_BITS), _RANDOM_CHARS)


def valid_id(value: object) -> bool:
    """True for a message id from any client: 8 to 64 letters, digits or dashes, no leading dash."""
    return isinstance(value, str) and _ID_PATTERN.fullmatch(value) is not None
