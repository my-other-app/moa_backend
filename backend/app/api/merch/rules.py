"""What one specific person is owed for one item, resolved from their own
record. Shared by the status screen and the handover, so what the volunteer
sees is what the handover checks."""
from dataclasses import dataclass
from typing import Optional

from app.api.events.documents import ROLE_LABELS, field_labels_of

# "Role" reads the registration's role; every other field name looks up a guest field.
ROLE_FIELDS = ("role", "ticket type")

COLLECTABLE = "COLLECTABLE"
ALREADY_COLLECTED = "ALREADY_COLLECTED"
NOT_ELIGIBLE = "NOT_ELIGIBLE"
NO_VARIANT_VALUE = "NO_VARIANT_VALUE"
NO_MATCHING_VARIANT = "NO_MATCHING_VARIANT"
OUT_OF_STOCK = "OUT_OF_STOCK"


def norm(v) -> str:
    return (v or "").strip().lower() if isinstance(v, str) else ("" if v is None else str(v).strip().lower())


def role_label(reg) -> str:
    role = getattr(reg.role, "value", reg.role) or "attendee"
    return ROLE_LABELS.get(str(role), str(role).title())


def field_value(reg, event, field: str) -> Optional[str]:
    """Case-insensitive on both sides, including the field name itself; a
    guest field is reachable by its key or its label."""
    if norm(field) in ROLE_FIELDS:
        return role_label(reg)
    details = reg.additional_details if isinstance(reg.additional_details, dict) else {}
    labels = field_labels_of(event)
    for key, value in details.items():
        if norm(key) == norm(field) or norm(labels.get(key)) == norm(field):
            s = str(value).strip() if value is not None else ""
            return s or None
    return None


def field_key(event, field: str) -> str:
    """The key a value is stored under for a field named by key or label: the
    event's own spelling, so the record never holds two spellings of one field."""
    for f in event.additional_details or []:
        if isinstance(f, dict) and f.get("key") and (norm(f.get("key")) == norm(field) or norm(f.get("label")) == norm(field)):
            return f["key"]
    return field


@dataclass
class Resolution:
    status: str
    variant_id: Optional[object] = None
    variant_label: Optional[str] = None
    remaining: Optional[int] = None


def resolve(item, reg, event, already: bool) -> Resolution:
    if already:
        return Resolution(ALREADY_COLLECTED)
    if item.eligibility_field and item.eligibility_value:
        if norm(field_value(reg, event, item.eligibility_field)) != norm(item.eligibility_value):
            return Resolution(NOT_ELIGIBLE)
    if item.variant_field:
        value = field_value(reg, event, item.variant_field)
        if not value:
            return Resolution(NO_VARIANT_VALUE)
        # Labels must match the guest data exactly (case aside): "Large" never matches "L".
        variant = next((v for v in item.variants if norm(v.label) == norm(value)), None)
        if variant is None:
            return Resolution(NO_MATCHING_VARIANT)
        remaining = max(0, variant.stock - variant.collected_count)
        return Resolution(COLLECTABLE if remaining > 0 else OUT_OF_STOCK, variant.id, variant.label, remaining)
    remaining = max(0, (item.stock or 0) - item.collected_count)
    return Resolution(COLLECTABLE if remaining > 0 else OUT_OF_STOCK, None, None, remaining)
