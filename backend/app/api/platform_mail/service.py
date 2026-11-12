"""The platform inbox's addresses: settings.PLATFORM_INBOX_ADDRESSES.

A mailbox is an address's local part (`info` for info@myotherapp.in): what
each thread and message is filed under, and what the admin picks to send
from. Plus-addressing reaches the base (`info+press@` is `info`).
Empty setting: no platform inbox -- nothing is kept, nothing can be sent.
"""
from __future__ import annotations

from typing import Optional

from app.config import settings
from app.response import CustomHTTPException

ACTION = "platform_mail"


def addresses() -> dict[str, str]:
    """{mailbox: address}, in the order of the setting's local parts (sorted)."""
    out: dict[str, str] = {}
    for address in sorted(settings.platform_inbox_addresses):
        local = address.split("@", 1)[0]
        out.setdefault(local, address)
    return out


def on() -> bool:
    return bool(settings.platform_inbox_addresses)


def require_on() -> None:
    if not on():
        raise CustomHTTPException(status_code=404, message="The platform inbox has no addresses (APP_PLATFORM_INBOX_ADDRESSES).",
                                  error_code="PLATFORM_MAIL_OFF")


def mailbox_for(recipient: Optional[str]) -> Optional[str]:
    """The mailbox a recipient address is, or None: `Info+x@MyOtherApp.in` -> `info`."""
    value = (recipient or "").strip().lower()
    local, at, host = value.rpartition("@")
    if not at or not local:
        return None
    base = f"{local.split('+', 1)[0]}@{host}"
    return base.split("@", 1)[0] if base in settings.platform_inbox_addresses else None


def address_of(mailbox: str) -> Optional[str]:
    return addresses().get(mailbox)


def own_addresses() -> list[str]:
    """Every platform address (a reply-all leaves them out)."""
    return list(addresses().values())


def require_mailbox(mailbox: Optional[str]) -> str:
    """The mailbox, if it is one of the platform's; else 422 INVALID_MAILBOX."""
    value = (mailbox or "").strip().lower().split("@", 1)[0]
    if value not in addresses():
        raise CustomHTTPException(
            status_code=422, message="Send from one of the platform's addresses.", error_code="INVALID_MAILBOX",
            errors={"field": "from_mailbox", "allowed": list(addresses())},
        )
    return value


def sender_name() -> str:
    return (settings.MAIL_FROM_NAME or "MyOtherApp")[:80]
