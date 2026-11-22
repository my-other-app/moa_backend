"""The person's log, and the sections the profile shows, from what `service.gather` read.

Every row comes from a table that already holds the fact, or from the audit
log: nothing is logged for this page. A fact with no time is left out rather
than given one. What the tables cannot say (a check-in taken back is deleted,
a reopened request's earlier decision is cleared, an edit leaves no trace) is
not here either.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

TIMELINE_CAP = 200

#: A card printed by a station is also recorded as a print job, a moment after
#: the station took the request: jobs inside this window of a request are that request's.
PRINT_MATCH_BEFORE = timedelta(seconds=2)
PRINT_MATCH_AFTER = timedelta(minutes=2)

STAGE_WORDS = {
    "REQUEST_RECEIVED": "Request received",
    "REQUEST_APPROVED": "Confirmed",
    "REQUEST_APPROVED_PAY": "Confirmed, pay to keep the place",
    "REQUEST_DECLINED": "Declined",
    "REQUEST_LAPSED": "Place ran out",
    "REQUEST_UNANSWERED": "Not answered before the event",
}
VIA_WORDS = {
    "gate": "QR scan", "offline_sync": "QR scan, offline", "desk": "By hand", "kiosk": "Kiosk",
    "walkup": "Walk-up at the door", "phone": "Phone self check-in", "app": "Volunteer app", "api": "Integration",
    "marked": "Marked present",
}
SOURCE_WORDS = {"kiosk": "a kiosk", "dashboard": "the dashboard", "door": "the door", "batch": "a batch", "test": "a test"}
PRINT_SOURCE = {"kiosk": "kiosk", "manual": "dashboard", "checkin": "door", "walkup": "door", "batch": "batch", "test": "test"}
CLOSED_WORDS = {
    "registered_directly": "They registered another way",
    "event_ended": "The event ended before the request was answered",
    "event_deleted": "The event was deleted",
    "event_cancelled": "The event was cancelled",
}
WALKUP_DOMAIN = "walkup.myotherapp.in"
IST = timezone(timedelta(hours=5, minutes=30))


def aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    # Every time goes out in UTC, whatever zone the column came back in.
    return (value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _value(x):
    return getattr(x, "value", x)


def money(amount) -> str:
    n = float(amount or 0)
    return f"₹{int(n)}" if n == int(n) else f"₹{n:.2f}"


class Log:
    """Rows as they are added; `rows()` is them newest first, capped."""

    def __init__(self):
        self._rows: list[tuple[datetime, int, dict]] = []

    def add(self, at, kind: str, title: str, detail: Optional[str] = None, by: Optional[str] = None, tone: str = "default") -> None:
        at = aware(at)
        if at is None:
            return
        self._rows.append((at, len(self._rows), {"at": at, "kind": kind, "title": title, "detail": detail or None, "by": by or None, "tone": tone}))

    def rows(self) -> tuple[list[dict], bool]:
        ordered = sorted(self._rows, key=lambda r: (r[0], r[1]), reverse=True)
        return [r[2] for r in ordered[:TIMELINE_CAP]], len(ordered) > TIMELINE_CAP


class Names:
    """Who did it: a person's name, or a word for what did."""

    def __init__(self, d):
        self.d = d

    def user(self, user_id) -> Optional[str]:
        return self.d.user_names.get(user_id) if user_id is not None else None

    def device(self, device_id, fallback: str = "a kiosk") -> str:
        found = self.d.devices.get(device_id)
        return found[0] if found else fallback

    def check_in(self, ci) -> str:
        via = _value(ci.via)
        if ci.checked_in_by is not None and self.user(ci.checked_in_by):
            return self.user(ci.checked_in_by)
        if via == "kiosk":
            return self.device(ci.kiosk_device_id)
        if via == "phone":
            return "the guest"
        if via == "api":
            return "an integration"
        return "the door link"


def check_in_of(d, names: Names) -> Optional[dict]:
    reg = d.reg
    if d.check_in is not None:
        ci = d.check_in
        via = _value(ci.via)
        return {"at": aware(ci.checked_in_at), "door": ci.door_label or d.door_name, "method": VIA_WORDS.get(via, via), "via": via, "by": names.check_in(ci)}
    if reg is not None and reg.is_attended and reg.attended_on is not None:
        return {"at": aware(reg.attended_on), "door": None, "method": VIA_WORDS["marked"], "via": "marked", "by": None}
    return None


def prints_of(d, names: Names) -> tuple[list[dict], set]:
    """Every card put out for them: the print-station queue's requests, and the
    jobs that were not one of those requests' own. Newest first."""
    out: list[dict] = []
    taken: set = set()
    for r in d.print_requests:
        window = (aware(r.claimed_at), aware(r.printed_at) or aware(r.updated_at) or aware(r.claimed_at))
        if window[0] is not None:
            for j in d.print_jobs:
                at = aware(j.created_at)
                if window[0] - PRINT_MATCH_BEFORE <= at <= window[1] + PRINT_MATCH_AFTER:
                    taken.add(j.id)
        station = d.devices.get(r.claimed_by) or d.devices.get(r.printer_id)
        by = names.user(r.requested_by_user_id) or (names.device(r.requested_by_device_id) if r.requested_by_device_id else None)
        done = aware(r.printed_at) if r.status == "printed" else None
        at = done or (aware(r.updated_at) if r.status in ("failed", "cancelled", "printing") else None) or aware(r.created_at)
        out.append({
            # The print-station request's id: what Retry and Cancel act on.
            "id": str(r.id), "at": at, "status": r.status, "source": r.source, "door": station[1] if station else None,
            "printer": station[0] if station else None, "by": by, "error": r.error,
        })
    for j in d.print_jobs:
        if j.id in taken:
            continue
        out.append({
            "id": None, "at": aware(j.created_at), "status": "failed" if j.error else "printed", "source": PRINT_SOURCE.get(_value(j.source), _value(j.source)),
            "door": j.door_label, "printer": None, "by": names.user(j.requested_by), "error": j.error,
        })
    out.sort(key=lambda p: p["at"], reverse=True)
    return out, taken


def merch_of(d, names: Names) -> list[dict]:
    return [
        {"item": item, "variant": variant, "at": aware(c.collected_at), "by": names.user(c.collected_by) or (c.door_label or None)}
        for c, item, variant in d.merch
    ]


def deliveries_of(d, names: Names) -> list[dict]:
    """What was sent to them, each as its latest state, newest first."""
    out: list[dict] = []
    for x in d.deliveries:
        status = _value(x.status)
        status = {"sending": "pending", "dead": "failed"}.get(status, status)
        out.append({
            "what": _value(x.kind), "channel": _value(x.channel), "status": status, "outcome": _value(x.outcome) if x.outcome else None,
            "at": aware(x.sent_at) or aware(x.updated_at), "reason": x.reason or x.error,
        })
    for rec, stage, _name, _by in d.review_sends:
        out.append({
            "what": "review_email", "channel": "email", "status": rec.status, "outcome": None,
            "at": aware(rec.sent_at) or aware(rec.created_at), "reason": rec.error or STAGE_WORDS.get(stage, stage),
        })
    for m, subject, kind, _by in d.messages:
        status = {"sending": "pending", "dead": "failed"}.get(_value(m.status), _value(m.status))
        out.append({
            "what": "message", "channel": _value(m.channel), "status": status, "outcome": _value(m.outcome) if m.outcome else None,
            "at": aware(m.sent_at) or aware(m.created_at), "reason": m.reason or m.error or subject,
        })
    for n in d.notices:
        out.append({"what": "notice", "channel": n.channel, "status": n.status, "outcome": None, "at": aware(n.updated_at) or aware(n.created_at), "reason": n.error})
    for s in d.ask_sends:
        out.append({"what": "answers_ask", "channel": s.channel, "status": s.status, "outcome": None, "at": aware(s.sent_at) or aware(s.created_at), "reason": s.error})
    for f in d.feedback_sends:
        out.append({
            "what": "feedback", "channel": f.channel or "none", "status": "sent" if f.delivered_at else "skipped" if f.skipped_reason else "pending",
            "outcome": None, "at": aware(f.delivered_at) or aware(f.sent_at), "reason": f.skipped_reason,
        })
    out.sort(key=lambda x: x["at"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return out


def payment_of(d, *, with_receipt: bool) -> Optional[dict]:
    reg = d.reg
    logs = [lg for lg in d.logs if _value(lg.status) in ("captured", "refunded", "disputed")]
    if logs:
        lg = logs[-1]
        order = next((o for o in d.orders if o.id == lg.order_id), None)
        refunded = _value(lg.status) == "refunded" or bool(d.refunds)
        whole = _value(lg.status) == "refunded" or sum(float(r.amount) for r in d.refunds) >= float(lg.amount)
        status = "refunded" if refunded and whole else "partly refunded" if refunded else "disputed" if _value(lg.status) == "disputed" else "paid"
        return {
            "amount": float(lg.amount), "currency": (order.currency if order else None) or "INR", "method": lg.payment_method,
            "status": status, "paid_at": aware(lg.created_at), "receipt": reg.payment_receipt if (with_receipt and reg) else None,
        }
    if reg is not None and reg.is_paid and float(reg.paid_amount or 0) > 0:
        # Paid without an online payment on record: at the desk, or before payments were kept.
        return {"amount": float(reg.paid_amount), "currency": "INR", "method": "recorded by the club", "status": "paid", "paid_at": None, "receipt": None}
    failed = [lg for lg in d.logs if _value(lg.status) == "failed"]
    if failed:
        lg = failed[-1]
        return {"amount": float(lg.amount), "currency": "INR", "method": lg.payment_method, "status": "failed", "paid_at": None, "receipt": None}
    if reg is not None and float(reg.actual_amount or 0) > 0:
        return {"amount": float(reg.actual_amount), "currency": "INR", "method": None, "status": "unpaid", "paid_at": None, "receipt": None}
    open_request = next((r for r in reversed(d.requests) if r.amount is not None and r.status == "approved"), None)
    if reg is None and open_request is not None:
        return {"amount": float(open_request.amount), "currency": "INR", "method": None, "status": "unpaid", "paid_at": None, "receipt": None}
    return None


def build_log(d, names: Names, printed: tuple[list[dict], set]) -> tuple[list[dict], bool]:
    log = Log()
    reg = d.reg
    _requests(d, log, names)
    if reg is not None:
        _registration(d, log, names)
        _check_in(d, log, names)
        _prints(d, log, names, printed)
        _merch(d, log, names)
        _payments(d, log, names)
        _documents(d, log, names)
        _messages(d, log, names)
        _feedback(d, log, names)
    _waitlist(d, log)
    _asks(d, log)
    _audit(d, log, names)
    return log.rows()


# ─── Sources ────────────────────────────────────────────────────────────


def _requests(d, log: Log, names: Names) -> None:
    for r in d.requests:
        kind_name = d.type_names.get(r.ticket_type_id)
        log.add(r.submitted_at, "request_submitted", "Asked to register", kind_name, "the guest", "info")
        if r.resent_at is not None:
            log.add(r.resent_at, "request_received_again", "The request-received email went again", None, "the system", "default")
        by = r.decided_by_name or names.user(r.decided_by_id)
        status = r.status
        if status == "declined":
            log.add(r.decided_at, "request_declined", "Declined", r.decline_note, by, "danger")
        elif status in ("approved", "lapsed") or (status == "closed" and r.decided_at is not None) or (status == "withdrawn" and r.decided_at is not None):
            paid = f"Ticket {money(r.amount)}, held until paid" if r.amount is not None else None
            log.add(r.decided_at, "request_confirmed", "Confirmed by the club", paid, by, "success")
        if status == "lapsed":
            log.add(r.updated_at, "request_lapsed", "The held place ran out unpaid", None, "the system", "warning")
        elif status == "withdrawn":
            log.add(r.updated_at, "request_withdrawn", "Withdrew the request", None, "the guest", "warning")
        elif status == "closed":
            log.add(r.updated_at, "request_closed", "Request closed", CLOSED_WORDS.get(r.closed_reason or "", None), "the system", "default")
    for rec, stage, by_name, by_id in d.review_sends:
        title = STAGE_WORDS.get(stage, "Review email")
        if rec.status in ("failed", "dead"):
            log.add(rec.updated_at, "review_email", f"Email could not be sent: {title}", rec.error, by_name or names.user(by_id), "danger")
        elif rec.sent_at is not None:
            log.add(rec.sent_at, "review_email", f"Emailed: {title}", None, by_name or names.user(by_id) or "the system", "info")


def _registration(d, log: Log, names: Names) -> None:
    reg = d.reg
    where = None
    if (reg.email or "").endswith(WALKUP_DOMAIN):
        where, who = "Registered at the door", "the door link"
    elif d.tracking_link:
        where, who = f"Through the link “{d.tracking_link}”", "the guest"
    else:
        who = None
    account = d.user
    kind = None
    if account is not None:
        kind = "with an app account" if _value(account.user_type) == "app_user" else "as a guest, without an app account"
    came = next((r for r in d.requests if r.registration_id == reg.id), None)
    detail = " · ".join(x for x in (where, kind, "after the club confirmed the request" if came is not None else None) if x)
    log.add(reg.created_at, "registered", "Registered", detail, who, "success")
    log.add(reg.created_at, "ticket_issued", "Ticket issued", d.type_names.get(reg.ticket_type_id), "the system", "success")
    if reg.whatsapp_opt_in_at is not None:
        log.add(reg.whatsapp_opt_in_at, "whatsapp_opt_in", "Agreed to WhatsApp updates", None, "the guest", "info")
    if reg.ticket_revoked_at is not None:
        log.add(reg.ticket_revoked_at, "ticket_revoked", "Ticket cancelled: the door refuses it", None, None, "danger")
    if reg.email_bounced_at is not None:
        log.add(reg.email_bounced_at, "email_bounced", "Their email bounced", reg.email_bounce_reason, "the mail provider", "danger")
    if reg.wa_bounced_at is not None:
        log.add(reg.wa_bounced_at, "whatsapp_bounced", "WhatsApp could not reach them", reg.wa_bounce_reason, "WhatsApp", "danger")
    if d.cancellation is not None:
        log.add(d.cancellation.cancelled_at, "ticket_given_back", "Gave their ticket back", d.cancellation.reason, "the guest", "warning")
    elif reg.is_deleted:
        log.add(reg.deleted_at, "removed", "Taken off the list", None, None, "danger")
    if reg.certificate_issued_at is not None:
        log.add(reg.certificate_issued_at, "certificate_issued", "Certificate issued", None, None, "success")
    if reg.certificate_revoked_at is not None:
        log.add(reg.certificate_revoked_at, "certificate_revoked", "Certificate taken back", None, None, "danger")
    if d.check_in is None and reg.is_attended and reg.attended_on is not None:
        log.add(reg.attended_on, "checked_in", "Marked as attended", None, None, "success")


def _check_in(d, log: Log, names: Names) -> None:
    ci = d.check_in
    if ci is None:
        return
    via = _value(ci.via)
    door = ci.door_label or d.door_name
    log.add(ci.checked_in_at, "checked_in", f"Checked in at {door}" if door else "Checked in", VIA_WORDS.get(via, via), names.check_in(ci), "success")


def _prints(d, log: Log, names: Names, printed: tuple[list[dict], set]) -> None:
    for r in d.print_requests:
        source = SOURCE_WORDS.get(r.source, r.source)
        by = names.user(r.requested_by_user_id) or (names.device(r.requested_by_device_id) if r.requested_by_device_id else None)
        station = d.devices.get(r.claimed_by) or d.devices.get(r.printer_id)
        printer = station[0] if station else None
        log.add(r.created_at, "print_queued", f"Card queued from {source}", f"for {printer}" if r.printer_id and printer else None, by, "info")
        if r.claimed_at is not None:
            attempt = f"attempt {r.attempts}" if (r.attempts or 0) > 1 else None
            log.add(r.claimed_at, "print_printing", f"Printing on {printer}" if printer else "Printing", attempt, printer, "info")
        if r.status == "printed":
            log.add(r.printed_at, "print_printed", "Card printed", f"on {printer}" if printer else None, printer, "success")
        elif r.status == "failed":
            log.add(r.updated_at, "print_failed", "Card could not be printed", r.error, printer, "danger")
        elif r.status == "cancelled":
            log.add(r.updated_at, "print_cancelled", "Card taken out of the queue", None, None, "warning")
    taken = printed[1]
    for j in d.print_jobs:
        if j.id in taken:
            continue
        source = SOURCE_WORDS.get(PRINT_SOURCE.get(_value(j.source), _value(j.source)), _value(j.source))
        where = f"at {j.door_label}" if j.door_label else None
        by = names.user(j.requested_by)
        if j.error:
            log.add(j.created_at, "print_failed", "Card could not be printed", j.error, by, "danger")
        else:
            log.add(j.created_at, "print_printed", f"Card printed from {source}", where, by, "success")


def _merch(d, log: Log, names: Names) -> None:
    for c, item, variant in d.merch:
        log.add(c.collected_at, "merch_collected", f"Collected {item}" + (f" ({variant})" if variant else ""), f"at {c.door_label}" if c.door_label else None, names.user(c.collected_by), "success")


def _payments(d, log: Log, names: Names) -> None:
    for o in d.orders:
        log.add(o.created_at, "payment_started", "Started a payment", money(o.amount), "the guest", "info")
    refunded_logs = {r.payment_log_id for r in d.refunds}
    for lg in d.logs:
        status = _value(lg.status)
        if status in ("captured", "refunded"):
            log.add(lg.created_at, "payment_paid", f"Paid {money(lg.amount)}", lg.payment_method, "the guest", "success")
        elif status == "failed":
            log.add(lg.created_at, "payment_failed", "Payment failed", lg.payment_method, "the guest", "danger")
        elif status == "disputed":
            log.add(lg.updated_at, "payment_disputed", "The payment was disputed", None, "the bank", "danger")
        if status == "refunded" and lg.id not in refunded_logs:
            log.add(lg.updated_at, "payment_refunded", f"Refunded {money(lg.amount)}", lg.refund_reason, "the system", "warning")
        elif lg.refund_requested_at is not None and lg.id not in refunded_logs and status == "captured":
            log.add(lg.refund_requested_at, "payment_refund_started", "A refund was started", lg.refund_reason, "the system", "warning")
    for r in d.refunds:
        log.add(r.created_at, "payment_refunded", f"Refunded {money(r.amount)}", r.reason, names.user(r.created_by) or "the club", "warning")


def _documents(d, log: Log, names: Names) -> None:
    """Ticket and certificate sends, each send and what the provider said of it."""
    attempts: dict = {}
    for a, kind, channel in d.attempts:
        attempts.setdefault((a.delivery_id), []).append((a, _value(kind), _value(channel)))
    for x in d.deliveries:
        kind, channel = _value(x.kind), _value(x.channel)
        noun = {"ticket": "Ticket", "certificate": "Certificate", "reminder": "Reminder"}.get(kind, kind.title())
        via = "by WhatsApp" if channel == "whatsapp" else "by email"
        sends = sorted(attempts.get(x.id, []), key=lambda t: aware(t[0].created_at))
        if sends:
            for n, (a, _, _) in enumerate(sends):
                log.add(a.created_at, f"{kind}_sent", f"{noun} sent {via}" if n == 0 else f"{noun} sent again {via}", None, "the system", "info")
                if a.outcome is not None:
                    _outcome(log, a.outcome_at or a.created_at, kind, noun, via, _value(a.outcome), a.reason)
        elif x.sent_at is not None:
            log.add(x.sent_at, f"{kind}_sent", f"{noun} sent {via}", None, "the system", "info")
            if x.outcome is not None:
                _outcome(log, x.outcome_at or x.sent_at, kind, noun, via, _value(x.outcome), x.reason)
        if _value(x.status) in ("failed", "dead"):
            log.add(x.updated_at, f"{kind}_failed", f"{noun} could not be sent {via}", x.error, "the system", "danger")


def _outcome(log: Log, at, kind: str, noun: str, via: str, outcome: str, reason: Optional[str]) -> None:
    words = {
        "delivered": ("was delivered", "success"), "read": ("was opened", "success"), "undelivered": ("was not delivered", "danger"),
        "bounced": ("bounced", "danger"), "complained": ("was marked as spam", "danger"),
    }.get(outcome, (outcome, "default"))
    log.add(at, f"{kind}_{outcome}", f"{noun} {words[0]}", reason, "the mail provider" if via.endswith("email") else "WhatsApp", words[1])


def _messages(d, log: Log, names: Names) -> None:
    for m, subject, _kind, by in d.messages:
        status = _value(m.status)
        what = f"“{subject}”" if subject else "a message"
        if status in ("failed", "dead"):
            log.add(m.updated_at, "message_failed", f"Message could not be sent: {what}", m.error, names.user(by) or "the club", "danger")
        elif m.sent_at is not None:
            log.add(m.sent_at, "message_sent", f"Message sent: {what}", None, names.user(by) or "the club", "info")
            if m.outcome is not None and _value(m.outcome) in ("bounced", "undelivered", "complained"):
                log.add(m.outcome_at or m.sent_at, "message_bounced", f"Message not delivered: {what}", m.reason, "the mail provider", "danger")
    for n in d.notices:
        what = "that the event was cancelled" if n.ref.startswith("cancel") else "about a refund"
        if n.status == "sent":
            log.add(n.updated_at, "notice_sent", f"Told {what}", f"by {n.channel}", "the system", "info")
        elif n.status == "failed":
            log.add(n.updated_at, "notice_failed", f"Could not tell them {what}", n.error, "the system", "danger")


def _feedback(d, log: Log, names: Names) -> None:
    for f in d.feedback_sends:
        if f.delivered_at is not None:
            log.add(f.delivered_at, "feedback_asked", "Asked for feedback", f"by {f.channel}" if f.channel else None, "the system", "info")
    for r in d.feedback_responses:
        log.add(r.submitted_at, "feedback_given", "Gave feedback", None, "the guest", "success")
    if d.rating is not None:
        review = (d.rating.review or "").strip()
        log.add(d.rating.created_at, "rating_given", f"Rated the event {d.rating.rating:g} out of 5", review[:200] or None, "the guest", "success")


def _waitlist(d, log: Log) -> None:
    for w in d.waitlist:
        if not w.via_request:
            log.add(w.created_at, "waitlist_joined", "Joined the waiting list", d.type_names.get(w.ticket_type_id), "the guest", "info")
        if w.offered_at is not None:
            until = f"until {aware(w.offer_expires_at).astimezone(IST):%d %b, %I:%M %p} IST" if w.offer_expires_at is not None else None
            log.add(w.offered_at, "waitlist_offered", "Held a place for them" if w.via_request else "Offered a place", until, "the system", "info")
        if w.status == "claimed":
            log.add(w.updated_at, "waitlist_claimed", "Took the place", None, "the guest", "success")
        elif w.status == "expired":
            log.add(w.offer_expires_at or w.updated_at, "waitlist_expired", "The offer ran out", None, "the system", "warning")
        elif w.status in ("removed", "left"):
            log.add(w.closed_at or w.updated_at, "waitlist_left", "Left the waiting list" if w.status == "left" else "Taken off the waiting list", None, "the guest" if w.status == "left" else None, "warning")


def _asks(d, log: Log) -> None:
    for person, ask in d.ask_people:
        labels = ", ".join(str(q.get("label") or q.get("key")) for q in (ask.questions or []) if isinstance(q, dict))
        log.add(ask.created_at, "answers_asked", "Asked for answers", labels or None, ask.created_by_name or "the club", "info")
        if person.opened_at is not None:
            log.add(person.opened_at, "answers_opened", "Opened the link to answer", None, "the guest", "default")
        if person.answered_at is not None:
            log.add(person.answered_at, "answers_answered", "Answered what was asked", None, "the guest", "success")
    for s in d.ask_sends:
        who = "the system"
        if s.status == "sent":
            round_ = f"Reminder {s.round} sent" if s.round else "Ask sent"
            log.add(s.sent_at, "answers_ask_sent", f"{round_} by {s.channel}", None, who, "info")
        elif s.status == "failed":
            log.add(s.created_at, "answers_ask_failed", f"Could not ask by {s.channel}", s.error, who, "danger")


def _audit(d, log: Log, names: Names) -> None:
    for a in d.audit:
        guest = (a.action or "").endswith("_link.resend")
        by = a.actor_name or names.user(a.actor_id) or ("the guest" if guest else "the system")
        title = (a.summary or a.action or "Recorded").strip()
        tone = "warning" if a.action and "refund" in a.action else "default"
        log.add(a.created_at, "audit", title, None, by, tone)
