"""Rupees and paise, exactly.

Every money column is Numeric (rupees, two places) or an integer of paise.
A float never sits between them: `int(float(Decimal("19.99")) * 100)` is
1998, and 4,586 of the prices from ₹0.01 to ₹999.99 lost a paisa that way on
their way to Razorpay -- the capture then fell a paisa short of what was due
and the guest who paid was left without a ticket.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

PAISA = Decimal("0.01")


def rupees(value) -> Decimal:
    """`value` as a Decimal of rupees, to the paisa. A float is read through
    its shortest repr (19.99, not 19.989999999999998436805981327779591083526611328125)."""
    if value is None:
        return Decimal("0.00")
    if isinstance(value, float):
        value = repr(value)
    return Decimal(value).quantize(PAISA, rounding=ROUND_HALF_UP)


def to_paise(value) -> int:
    """Rupees to whole paise, half a paisa rounding up."""
    return int((rupees(value) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def from_paise(paise) -> Decimal:
    """Razorpay's whole paise to rupees."""
    return (Decimal(int(paise or 0)) / 100).quantize(PAISA)


def same(a, b) -> bool:
    """Two amounts are the same money, compared as Decimals."""
    return rupees(a) == rupees(b)
