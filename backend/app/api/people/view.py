"""The profile as the contract (§1) shapes it, from what was read."""
from __future__ import annotations

from typing import Optional

from app.api.approvals import service as approvals
from app.api.club_access import policy
from app.api.cohosts import policy as cohost_policy
from app.api.missing_answers.state import BLANK, Form, INVALID, blank
from app.api.people import timeline as tl

OPEN_PRINT = ("queued", "printing")


def stage_of(d) -> str:
    reg, req = d.reg, d.req
    if reg is not None:
        if reg.is_deleted:
            return "cancelled" if d.cancellation is not None else "removed"
        return "confirmed"
    return {
        "pending": "under_review", "approved": "confirmed", "declined": "declined",
        "withdrawn": "cancelled", "lapsed": "cancelled", "closed": "cancelled",
    }.get(req.status, "under_review")


def _photo(user) -> Optional[str]:
    profile = getattr(user, "profile", None)
    if profile is None:
        return None
    for pic in (profile.profile_pic, getattr(profile.avatar, "image", None) if profile.avatar is not None else None):
        if not pic:
            continue
        if isinstance(pic, str):
            return pic
        if isinstance(pic, dict):
            found = pic.get("original") or pic.get("medium") or next((v for v in pic.values() if isinstance(v, str)), None)
            if found:
                return str(found)
    return None


def person_of(d) -> dict:
    reg, req = d.reg, d.req
    src = reg if reg is not None else req
    answers = (reg.additional_details if reg is not None else req.answers) or {}
    org = None
    profile = getattr(d.user, "profile", None)
    if profile is not None and profile.org is not None:
        org = profile.org.name
    if not org and isinstance(answers, dict) and isinstance(answers.get("organization"), str):
        org = answers["organization"].strip() or None
    kind = "none"
    if d.user is not None:
        kind = "guest" if tl._value(d.user.user_type) == "guest" else "app"
    return {
        "full_name": src.full_name or "", "email": src.email or None, "phone": src.phone or None, "institution": org,
        "role": (tl._value(reg.role) if reg is not None else None) or "attendee", "photo_url": _photo(d.user),
        "account": {"kind": kind, "user_id": d.user.id if d.user is not None else None},
    }


def answers_of(d) -> tuple[list[dict], list[str]]:
    """The form's current questions that apply to them, in form order, and which of the required ones are missing."""
    form = Form(d.event.additional_details)
    given = (d.reg.additional_details if d.reg is not None else d.req.answers) or {}
    given = given if isinstance(given, dict) else {}
    states = form.states(given)
    missing = set(form.missing(given))
    out = [
        {"key": k, "label": form.label(k), "value": None if blank(given.get(k)) else given.get(k), "missing": k in missing}
        for k in form.keys if states.get(k) != "hidden"
    ]
    return out, [k for k in form.keys if states.get(k) in (BLANK, INVALID)]


def ticket_of(d, *, door_view: bool) -> Optional[dict]:
    reg = d.reg
    if reg is None:
        return None
    revoked = reg.ticket_revoked_at is not None or reg.is_deleted
    return {
        "code": reg.ticket_id, "type_name": d.type_names.get(reg.ticket_type_id) or reg.ticket_label, "admission_no": reg.admission_no,
        "pdf_url": None if (revoked or door_view) else f"/api/v1/events/tickets/{reg.ticket_id}/pdf",
        "issued_at": tl.aware(reg.created_at), "revoked_at": tl.aware(reg.ticket_revoked_at),
    }


def status_of(d, prints: list[dict], checked_in: bool) -> dict:
    reg, req, event = d.reg, d.req, d.event
    if reg is not None:
        cert = "none"
        if reg.certificate_issued_at is not None:
            cert = "revoked" if reg.certificate_revoked_at is not None else "issued"
        paid = bool(reg.is_paid) if (event.has_fee or float(reg.actual_amount or 0) > 0) else None
        printed = reg.badge_printed_at is not None or any(p["status"] == "printed" for p in prints)
        revoked = reg.ticket_revoked_at is not None
        bounced = reg.email_bounced_at is not None
    else:
        cert, printed, revoked = "none", False, False
        paid = False if req.amount is not None else None
        bounced = False
    return {
        "stage": stage_of(d), "checked_in": checked_in, "card_printed": printed, "certificate": cert, "paid": paid, "ticket_revoked": revoked,
        # The "Email bounced" chip. The provider's reason (it can carry an address) is in the timeline,
        # where door staff see it masked.
        "email_bounced": bounced,
    }


def actions_of(d, *, role: str, platform_admin: bool, cohost: bool, printers_online: int, prints: list[dict], checked_in: bool, askable: list[str]) -> dict:
    """What the dashboard may offer here: the role's own rules (club_access.policy)
    and a co-host's limits (cohosts.policy), on the very requests each button makes."""
    reg, req, event = d.reg, d.req, d.event
    eid = event.id
    who = "admin" if role == "owner" else role

    def ok(method: str, path: str) -> bool:
        return policy.allows(who, method, path) and not (cohost and cohost_policy.host_only(method, path))

    live = reg is not None and not reg.is_deleted
    ticket_ok = live and reg.ticket_revoked_at is None
    rid = reg.id if reg is not None else None
    pending = reg is None and req is not None and req.status == "pending"
    base = f"/api/v1/events/{eid}"
    can_print = bool(ticket_ok and ok("POST", f"{base}/idcards/print-requests"))
    open_print = any(p["status"] in OPEN_PRINT for p in prints)
    edit = False
    if live:
        edit = ok("PATCH", f"/api/v1/events/registration/{eid}/info/{rid}")
    elif reg is None and req is not None:
        edit = ok("PATCH", f"/api/v1/events/registration/{eid}/requests/{req.id}")
    return {
        "can_print": can_print,
        "can_send_to_station": bool(can_print and printers_online > 0 and not open_print),
        "printers_online": printers_online,
        "can_resend_ticket": bool(ticket_ok and (reg.email or reg.phone) and ok("POST", f"{base}/tickets/{rid}/send")),
        "can_check_in": bool(ticket_ok and not checked_in and ok("POST", f"{base}/gate/check-in/{rid}")),
        "can_undo_check_in": bool(live and checked_in and ok("DELETE", f"{base}/gate/check-in/{rid}")),
        "can_edit": bool(edit),
        "can_confirm": bool(pending and not platform_admin and not approvals.event_closed(event) and ok("POST", f"/api/v1/events/registration/{eid}/requests/approve")),
        "can_decline": bool(pending and not platform_admin and ok("POST", f"/api/v1/events/registration/{eid}/requests/decline")),
        "can_ask_missing": bool(
            askable and ((ticket_ok) or (reg is None and req is not None and req.status in ("pending", "approved"))) and ok("POST", f"{base}/answers/asks")
        ),
        "can_remove": bool(live and ok("DELETE", f"/api/v1/events/registration/{eid}/info/{rid}")),
    }


def build(d, *, role: str, platform_admin: bool, cohost: bool, door_view: bool, printers_online: int) -> dict:
    names = tl.Names(d)
    prints, taken = tl.prints_of(d, names)
    check_in = tl.check_in_of(d, names)
    answers, askable = answers_of(d)
    rows, truncated = tl.build_log(d, names, (prints, taken))
    money_role = policy.can("admin" if role == "owner" else role).get("money", False)
    reg = d.reg
    return {
        "kind": "registration" if reg is not None else "request",
        "registration_id": str(reg.id) if reg is not None else None,
        "request_id": str(d.req.id) if d.req is not None else None,
        "person": person_of(d),
        "status": status_of(d, prints, check_in is not None),
        "answers": answers,
        "ticket": ticket_of(d, door_view=door_view),
        "payment": tl.payment_of(d, with_receipt=money_role),
        "check_in": check_in,
        "prints": prints,
        "merch": tl.merch_of(d, names),
        "deliveries": tl.deliveries_of(d, names),
        "timeline": rows,
        "timeline_truncated": truncated,
        "actions": actions_of(
            d, role=role, platform_admin=platform_admin, cohost=cohost, printers_online=printers_online,
            prints=prints, checked_in=check_in is not None, askable=askable,
        ),
    }
