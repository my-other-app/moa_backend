"""Comparing a secret someone sent with the one expected.

`hmac.compare_digest` takes text only when it is all ASCII and raises
TypeError otherwise, so a signature, token or code sent with other
characters was a 500 rather than a refusal. `same` compares the UTF-8 bytes
of both, in constant time, and is simply False for anything that differs
(or is missing).
"""

from __future__ import annotations

import hmac
from typing import Union

Secret = Union[str, bytes, bytearray, None]


def _bytes(value: Union[str, bytes, bytearray]) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return str(value).encode("utf-8", "surrogatepass")


def same(given: Secret, expected: Secret) -> bool:
    """Whether `given` is `expected`, compared in constant time; never raises."""
    if given is None or expected is None:
        return False
    return hmac.compare_digest(_bytes(given), _bytes(expected))
