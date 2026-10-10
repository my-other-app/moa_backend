"""How an event numbers its tickets and certificates.

The default reads MOA-0041-KF26: a prefix, a sequence, and the event code
that keeps two events apart. Numbers already given out are never rewritten.
"""

ADMISSION_PREFIX_DEFAULT = "MOA"
CERTIFICATE_PREFIX = "CERT"


def admission_number(fmt: dict, code: str, seq: int, seat=None) -> str:
    """The number one ticket carries. Seat-only events use the seat itself."""
    with_code = fmt.get("includeCode", True) if fmt else True
    if seat:
        return f"{seat}-{code}" if with_code else seat
    prefix = (fmt or {}).get("prefix") or ADMISSION_PREFIX_DEFAULT
    digits = (fmt or {}).get("digits") or 4
    body = f"{prefix}-{str(seq).zfill(digits)}"
    return f"{body}-{code}" if with_code else body


def certificate_number(fmt: dict, code: str, seq: int) -> str:
    """A certificate's number, in the shape of this event's tickets, and never readable as one."""
    return admission_number({**(fmt or {}), "prefix": CERTIFICATE_PREFIX}, code, seq)
