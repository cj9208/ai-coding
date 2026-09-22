"""Time-sortable identifiers (resolves design Open Question #2).

A dependency-free ULID-style id: 48-bit millisecond timestamp in Crockford
base32 (10 chars, lexically sortable = chronological) plus 80 bits of
randomness (16 chars). ``req_``/``int_``/``route_``/... prefixes keep the
object type visible in every id and in SQLite ordering.
"""

from __future__ import annotations

import os
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32 (no I,L,O,U)


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def new_id(prefix: str) -> str:
    """A lexicographically time-sortable id, e.g. ``req_01JXYZ...``."""
    timestamp = int(time.time() * 1000)
    return f"{prefix}_{_encode(timestamp, 10)}{_encode(int.from_bytes(os.urandom(10), 'big'), 16)}"
