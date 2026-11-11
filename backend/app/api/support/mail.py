"""The desk's mail and pushes, each a job (app/core/jobs.py) written in the
transaction of the change it is about, and run once that commits.

- A reply (`support.reply_mail`): from SUPPORT_FROM_ADDRESS, the subject
  `Re: [#1042] <subject>`, a Message-ID of ours and In-Reply-To/References to
  the person's last mail, so their mail client threads it and their answer
  comes back to the same case (inbound.py joins by the tag or the headers).
  Its files go with it. The provider's id is kept: a bounce notice names it
  (`provider_verdict`). An app account also gets a push (`support_reply`).
- A receipt when a case is opened from the app, the website or the
  dashboard, with the number to quote.
- "Did we solve it?" once a case is resolved: yes or no, by a link to the
  website with the case's survey token.
- A reminder on the fifth day Waiting on them.
- The 08:00 digest to each team member, and the owners' note of follow-ups
  three days overdue (sweep.py writes those jobs).

Each handler is safe to run twice: it checks the row it is about first
(a reply already `sent`, a survey already sent) and the provider takes the
same idempotency key once.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select

from app.api.support import text
from app.api.support.models import (
    NOT_DELIVERED,
    OPEN,
    REPLY_IN,
    REPLY_OUT,
    RESOLVED,
    WAITING,
    SupportAttachments,
    SupportCases,
    SupportMessages,
)
from app.config import settings
from app.core import jobs

logger = logging.getLogger(__name__)

REPLY_JOB = "support.reply_mail"
PUSH_JOB = "support.reply_push"
RECEIPT_JOB = "support.receipt_mail"
SURVEY_JOB = "support.survey_mail"
REMINDER_JOB = "support.reminder_mail"
DIGEST_JOB = "support.digest_mail"
OVERDUE_JOB = "support.overdue_mail"
SENDING, SENT, FAILED, BOUNCED, COMPLAINED = "sending", "sent", "failed", "bounced", "complained"
DESK_NAME = "MyOtherApp Support"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def from_address() -> str:
    return (settings.SUPPORT_FROM_ADDRESS or "support@myotherapp.in").strip().lower()


def _domain() -> str:
    return from_address().split("@", 1)[-1] or "myotherapp.in"


def new_message_id(case_number: int) -> str:
    return f"<support.{int(case_number)}.{uuid.uuid4().hex}@{_domain()}>"


def admin_link(path: str) -> str:
    return f"{settings.PUBLIC_ADMIN_URL.rstrip('/')}{path}"


def site_link(path: str) -> str:
    return f"{settings.PUBLIC_SITE_URL.rstrip('/')}{path}"


def where_to_see(case: SupportCases) -> str:
    if case.requester_kind == "club":
        return f"{settings.PUBLIC_WEB_URL.rstrip('/')}/help"
    if case.requester_kind == "user" and case.requester_user_id:
        return site_link("/help")
    return ""


# ─── Enqueueing ──────────────────────────────────────────────────────────


async def enqueue_reply(session, case: SupportCases, msg: SupportMessages) -> None:
    """The reply's mail (when the person has an address) and the app's push
    (when the person is an app account), after the caller's commit."""
    if case.requester_email:
        msg.delivery_status = SENDING
        await jobs.enqueue(session, REPLY_JOB, {"message_id": msg.id}, dedupe_key=f"{REPLY_JOB}:{msg.id}")
    if case.requester_kind == "user" and case.requester_user_id:
        await jobs.enqueue(
            session, PUSH_JOB, {"case_id": case.id, "message_id": msg.id, "user_id": case.requester_user_id},
            dedupe_key=f"{PUSH_JOB}:{msg.id}",
        )


async def enqueue_receipt(session, case: SupportCases) -> None:
    if case.requester_email:
        await jobs.enqueue(session, RECEIPT_JOB, {"case_id": case.id}, dedupe_key=f"{RECEIPT_JOB}:{case.id}")


async def enqueue_survey(session, case: SupportCases) -> None:
    if case.requester_email and case.survey_sent_at is None:
        await jobs.enqueue(session, SURVEY_JOB, {"case_id": case.id}, dedupe_key=f"{SURVEY_JOB}:{case.id}")


# ─── Sending ─────────────────────────────────────────────────────────────


def _deliver(to: str, subject: str, text_body: str, html_body: str, *, key: str, headers: Optional[dict] = None,
             files: Optional[list] = None, reply_to: Optional[str] = None, from_name: str = DESK_NAME,
             sender: Optional[str] = None) -> dict:
    from app.core.email import transport

    mail = transport.Mail(
        to=[to], subject=subject, text=text_body, html=html_body, from_name=from_name,
        from_address=sender or from_address(), reply_to=reply_to or sender or from_address(),
        attachments=[transport.Attachment(filename=n, content=c) for n, c in (files or [])],
        idempotency_key=key, check_suppression=False, headers=headers or {},
    )
    return transport.deliver(mail)


async def _suppressed(session, address: str) -> Optional[str]:
    from app.core.mail import suppression

    found = (await suppression.reasons(session, [address])).get(suppression.normal(address))
    return suppression.why(found) if found is not None else None


def _wrap(paragraphs: list[str], *, button: Optional[tuple[str, str]] = None, footer: Optional[str] = None) -> tuple[str, str]:
    from app.core.email import plain

    return plain.text(paragraphs, button=button, footer=footer), plain.render(paragraphs, button=button, footer=footer)


async def _threading(session, case: SupportCases) -> tuple[Optional[str], Optional[str]]:
    """(In-Reply-To, References) for our next mail on the case: the person's
    last mail, and the chain it carried with every id we sent."""
    rows = (await session.execute(
        select(SupportMessages.kind, SupportMessages.email_message_id, SupportMessages.alt_message_id, SupportMessages.references)
        .where(SupportMessages.case_id == case.id, SupportMessages.email_message_id.isnot(None))
        .order_by(SupportMessages.created_at, SupportMessages.id)
    )).all()
    from app.api.club_mailbox import parse

    chain: list[str] = []
    last_in = None
    for kind, mid, alt, refs in rows:
        if kind == REPLY_IN:
            chain += parse.message_ids(refs)
            last_in = mid
        chain.append(mid)
        if alt:
            chain.append(alt)
    chain = list(dict.fromkeys(c for c in chain if c))[-20:]
    return last_in, (" ".join(chain) or None)


@jobs.handler(REPLY_JOB, max_attempts=6, timeout=120.0, priority=jobs.HIGH)
async def _reply_job(p: dict) -> None:
    from app.api.support import store
    from app.core.email import transport

    async with jobs.scope() as s:
        msg = await s.get(SupportMessages, int(p["message_id"]))
        if msg is None or msg.kind != REPLY_OUT or msg.delivery_status != SENDING:
            return
        case = await s.get(SupportCases, msg.case_id)
        if case is None or not case.requester_email:
            msg.delivery_status = None
            await s.commit()
            return
        to = case.requester_email
        why = await _suppressed(s, to)
        if why:
            await _not_delivered(s, case, msg, f"Not sent: {why}")
            await s.commit()
            return
        in_reply_to, references = await _threading(s, case)
        msg.email_message_id = msg.email_message_id or new_message_id(case.number)
        msg.in_reply_to, msg.references = in_reply_to, references
        msg.from_address, msg.to_addresses = from_address(), [to]
        files = []
        for a in (await s.scalars(select(SupportAttachments).where(SupportAttachments.message_id == msg.id))).all():
            content = await asyncio.to_thread(store.get, a.storage_key)
            if content is not None:
                files.append((a.filename or "attachment", content))
        see = where_to_see(case)
        footer = f"Answer this mail to reply, or see your request at {see}." if see else "Answer this mail to reply."
        body_text = text.plain(msg.body_text) + f"\n--\n{DESK_NAME} · request #{case.number}\n{footer}\n"
        body_html = (msg.body_html or text.markdown_lite(msg.body_text)) + (
            f'<p style="color:#6B7280;font-size:12px;margin-top:24px">{DESK_NAME} · request #{case.number}<br>'
            f"{_esc(footer)}</p>"
        )
        headers = {"Message-ID": msg.email_message_id}
        if in_reply_to:
            headers["In-Reply-To"] = in_reply_to
        if references:
            headers["References"] = references
        subject = text.mail_subject(case.number, case.subject)
        await s.commit()
        try:
            answer = await asyncio.to_thread(
                _deliver, to, subject, body_text, body_html, key=f"support:{msg.id}", headers=headers, files=files,
            )
        except transport.MailDeferred:
            raise
        except Exception as exc:  # noqa: BLE001 - the provider's last word, kept on the message
            code = ""
            response = getattr(exc, "response", None)
            if isinstance(response, dict):
                code = str((response.get("Error") or {}).get("Code") or "")
            logger.warning("support: the reply %s on case %s was refused (%s)", msg.id, case.id, code or type(exc).__name__)
            await _not_delivered(s, case, msg, f"The mail service refused it ({code or type(exc).__name__}).")
            await s.commit()
            return
        msg.delivery_status, msg.delivery_error = SENT, None
        msg.provider = (answer or {}).get("Provider") or None
        msg.provider_message_id = (answer or {}).get("MessageId") or None
        if msg.provider == "ses":
            from app.api.club_mailbox.outbound import ses_message_id

            msg.alt_message_id = ses_message_id(msg.provider_message_id)
        await s.commit()


def _esc(value: str) -> str:
    import html

    return html.escape(value or "")


async def _not_delivered(session, case: SupportCases, msg: SupportMessages, reason: str) -> None:
    """A reply that didn't reach them: the message says why, the case is
    flagged and back in Open, with a line suggesting another way."""
    from app.api.support import cases as cases_mod

    msg.delivery_status = FAILED if not msg.delivery_status or msg.delivery_status in (SENDING, FAILED) else msg.delivery_status
    msg.delivery_error = reason[:400]
    flags = list(case.flags or [])
    if NOT_DELIVERED not in flags:
        case.flags = [*flags, NOT_DELIVERED]
    if case.status in (WAITING, RESOLVED):
        await cases_mod.set_status(session, case, OPEN)
    await cases_mod.system_line(
        session, case, f"Our reply didn't reach {case.requester_email or 'them'}: {reason} "
        "Try another way to reach them -- their phone, or the app if they have it.",
    )


async def provider_verdict(session, provider_id: str, *, bounced: bool, complained: bool, reason: str = "") -> int:
    """What the provider later said of a reply (app/api/hooks/service.py):
    it bounced, or was marked as spam. Returns how many replies were marked."""
    if not provider_id or not (bounced or complained):
        return 0
    rows = (await session.scalars(
        select(SupportMessages).where(SupportMessages.provider_message_id == provider_id, SupportMessages.kind == REPLY_OUT)
    )).all()
    for m in rows:
        if m.delivery_status in (BOUNCED, COMPLAINED):
            continue
        case = await session.get(SupportCases, m.case_id)
        m.delivery_status = COMPLAINED if complained else BOUNCED
        if case is not None:
            await _not_delivered(session, case, m, (reason or ("They marked it as spam." if complained else "It bounced."))[:300])
            m.delivery_status = COMPLAINED if complained else BOUNCED
    return len(rows)


@jobs.handler(PUSH_JOB, max_attempts=4, timeout=60.0, priority=jobs.HIGH)
async def _push_job(p: dict) -> None:
    from app.core.notifications import service as push

    async with jobs.scope() as s:
        case = await s.get(SupportCases, int(p["case_id"]))
        msg = await s.get(SupportMessages, int(p["message_id"]))
        if case is None or msg is None:
            return
        await s.commit()
        data = {"type": "support_reply", "case_id": str(case.id), "number": str(case.number)}
        try:
            await push.send_notification_to_user(
                s, int(p["user_id"]), f"Support answered #{case.number}", text.first_words(text.plain(msg.body_text), 120), data=data,
            )
        except Exception:  # noqa: BLE001 - the mail went; a push that fails is not tried for ever
            logger.warning("support: the push for case %s failed", case.id, exc_info=True)


@jobs.handler(RECEIPT_JOB, max_attempts=4, timeout=60.0, priority=jobs.HIGH)
async def _receipt_job(p: dict) -> None:
    async with jobs.scope() as s:
        case = await s.get(SupportCases, int(p["case_id"]))
        if case is None or not case.requester_email or await _suppressed(s, case.requester_email):
            return
        await s.commit()
        see = where_to_see(case)
        name = (case.requester_name or "").split(" ")[0] or "there"
        paragraphs = [
            f"Hi {name},",
            f"We have your request #{case.number}: “{case.subject}”. Someone from the team will answer soon, "
            "on working days between 09:30 and 18:30 India time.",
            "To add anything, answer this mail. Please never send passwords, OTPs or card numbers.",
        ]
        body_text, body_html = _wrap(paragraphs, button=("See your request", see) if see else None, footer=f"{DESK_NAME} · request #{case.number}")
        await asyncio.to_thread(
            _deliver, case.requester_email, text.mail_subject(case.number, case.subject, reply=False), body_text, body_html,
            key=f"support-receipt:{case.id}", headers={"Message-ID": new_message_id(case.number)},
        )


@jobs.handler(SURVEY_JOB, max_attempts=4, timeout=60.0)
async def _survey_job(p: dict) -> None:
    async with jobs.scope() as s:
        case = await s.get(SupportCases, int(p["case_id"]))
        if case is None or case.status != RESOLVED or case.survey_sent_at is not None or not case.requester_email:
            return
        if await _suppressed(s, case.requester_email):
            return
        case.survey_token = case.survey_token or secrets.token_urlsafe(24)
        case.survey_sent_at = _now()
        token = case.survey_token
        await s.commit()
        name = (case.requester_name or "").split(" ")[0] or "there"
        yes, no = site_link(f"/help/survey/{token}?solved=yes"), site_link(f"/help/survey/{token}?solved=no")
        paragraphs = [
            f"Hi {name},",
            f"We marked your request #{case.number} (“{case.subject}”) as solved. Did we solve it?",
            f"Yes: {yes}",
            f"No: {no}",
            "If something is still wrong, just answer this mail within a week and we'll pick it up again.",
        ]
        body_text, body_html = _wrap(paragraphs, footer=f"{DESK_NAME} · request #{case.number}")
        await asyncio.to_thread(
            _deliver, case.requester_email, text.mail_subject(case.number, f"Did we solve it? {case.subject}", reply=False),
            body_text, body_html, key=f"support-survey:{case.id}", headers={"Message-ID": new_message_id(case.number)},
        )


@jobs.handler(REMINDER_JOB, max_attempts=4, timeout=60.0)
async def _reminder_job(p: dict) -> None:
    async with jobs.scope() as s:
        case = await s.get(SupportCases, int(p["case_id"]))
        if case is None or case.status != WAITING or not case.requester_email or await _suppressed(s, case.requester_email):
            return
        in_reply_to, references = await _threading(s, case)
        await s.commit()
        name = (case.requester_name or "").split(" ")[0] or "there"
        paragraphs = [
            f"Hi {name},",
            f"We answered your request #{case.number} a few days ago and haven't heard back. "
            "If you still need help, answer this mail. Otherwise we'll close it in two days.",
        ]
        body_text, body_html = _wrap(paragraphs, footer=f"{DESK_NAME} · request #{case.number}")
        headers = {"Message-ID": new_message_id(case.number)}
        if in_reply_to:
            headers["In-Reply-To"] = in_reply_to
        if references:
            headers["References"] = references
        await asyncio.to_thread(
            _deliver, case.requester_email, text.mail_subject(case.number, case.subject), body_text, body_html,
            key=f"support-reminder:{case.id}:{int(case.waiting_since.timestamp()) if case.waiting_since else 0}", headers=headers,
        )


@jobs.handler(DIGEST_JOB, max_attempts=4, timeout=120.0)
async def _digest_job(p: dict) -> None:
    """One team member's day: follow-ups due and overdue, their open cases.
    Nothing to say, nothing sent."""
    from app.api.support import reports
    from app.api.users.models import Users

    async with jobs.scope() as s:
        user = await s.get(Users, int(p["user_id"]))
        if user is None or not user.email or getattr(user, "is_active", True) is False:
            return
        day = await reports.my_day(s, user.id)
        await s.commit()
        due, overdue, mine = day["follow_ups_due"], day["follow_ups_overdue"], day["cases_mine_open"]
        if not (due or overdue or mine):
            return
        lines = [f"Good morning {(user.full_name or '').split(' ')[0] or 'there'},", "Here is your day on the desk."]
        if overdue:
            lines.append("Overdue: " + "; ".join(f"{f['title']} ({f['about']})" for f in overdue[:15]))
        if due:
            lines.append("Due today: " + "; ".join(f"{f['title']} ({f['about']})" for f in due[:15]))
        if mine:
            lines.append(f"Your open cases: {len(mine)}" + (", " + ", ".join(f"#{c['number']}" for c in mine[:15])))
        body_text, body_html = _wrap(lines, button=("Open My day", admin_link("/teamwork/my-day")))
        subject = f"Your day: {len(due)} due, {len(overdue)} overdue, {len(mine)} open cases"
        await asyncio.to_thread(
            _deliver, user.email, subject, body_text, body_html, key=f"support-digest:{user.id}:{p.get('date')}",
            from_name="MyOtherApp Team work",
        )


@jobs.handler(OVERDUE_JOB, max_attempts=4, timeout=120.0)
async def _overdue_job(p: dict) -> None:
    """The admin owners told of follow-ups three days overdue (each once: the sweep marks them)."""
    from app.api.leads.models import FollowUps, Leads
    from app.api.support import teams as teams_mod
    from app.api.users.models import AdminRoles, UserTypes, Users

    ids = [int(i) for i in p.get("follow_up_ids") or []]
    if not ids:
        return
    async with jobs.scope() as s:
        rows = (await s.execute(
            select(FollowUps, Leads.name, SupportCases.number)
            .outerjoin(Leads, Leads.id == FollowUps.lead_id).outerjoin(SupportCases, SupportCases.id == FollowUps.case_id)
            .where(FollowUps.id.in_(ids), FollowUps.done.is_(False))
        )).all()
        owners = (await s.scalars(select(Users).where(
            Users.user_type == UserTypes.admin, Users.admin_role == AdminRoles.owner, Users.is_active.is_(True),
        ))).all()
        names = await teams_mod.names(s, [f.owner_id for f, _, _ in rows])
        await s.commit()
    if not rows or not owners:
        return
    items = [
        f"{f.title} -- {('lead ' + lead_name) if lead_name else ('case #' + str(number))}, "
        f"{names.get(f.owner_id, 'no owner')}, due {f.due_at:%d %b}"
        for f, lead_name, number in rows
    ]
    paragraphs = [f"{len(items)} follow-up{'s are' if len(items) != 1 else ' is'} three days or more overdue:", *items[:40]]
    body_text, body_html = _wrap(paragraphs, button=("Open Team work", admin_link("/teamwork/my-day")))
    for owner in owners:
        if not owner.email:
            continue
        await asyncio.to_thread(
            _deliver, owner.email, f"{len(items)} follow-up{'s' if len(items) != 1 else ''} overdue by 3 days", body_text, body_html,
            key=f"support-overdue:{p.get('batch')}:{owner.id}", from_name="MyOtherApp Team work",
        )
