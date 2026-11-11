"""Mail to support@ and sales@, through the inbound path the club mailbox
uses (SES receipt rule -> the inbound bucket -> SNS -> /hooks/mailbox/ses).

Dormant until SUPPORT_INBOUND_ADDRESSES / SALES_INBOUND_ADDRESSES name the
addresses: until then nothing here reads a mail, whatever arrives.

For a recipient in SUPPORT_INBOUND_ADDRESSES (plus-addressing allowed):

- a virus: dropped unread, logged;
- SES called it spam: kept as a closed case tagged `spam` -- the Spam tab,
  never a queue, never assigned;
- otherwise it joins its case by its thread headers (a Message-ID we sent
  or received), or by the `[#1042]` in its subject when the sender is the
  case's own person; or the same sender's case on the same subject within
  the hour; else it opens a case (channel `email`), the sender matched to an
  app account or a club, and flagged `unknown_sender` when neither.
  Joining a resolved case reopens it; a closed one starts a new linked case.

For a recipient in SALES_INBOUND_ADDRESSES (often a BCC): an `email` line on
each lead with a contact among the people on the mail (ours left out: the
desk's addresses and the admins'), or a new lead (source `inbound_email`).

The HTML is cleaned and the files kept privately as the club mailbox does
(its parse and sanitise); card-like numbers are masked. Each mail is kept
once, however often SNS delivers its notice. Nothing a mail says reaches a
log.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from app.api.club_mailbox import parse, providers
from app.api.support import cases, files, text
from app.api.support.models import (
    CLOSED,
    REPLY_IN,
    SPAM_TAG,
    UNKNOWN_SENDER,
    SupportCases,
    SupportMessages,
)
from app.config import settings
from app.core.mail.suppression import masked

logger = logging.getLogger(__name__)

#: At most this many files, this heavy together, are kept of one mail.
MAX_FILES = 20
MAX_FILE_BYTES = 25 * 1024 * 1024


def base_address(address: str) -> str:
    """support+billing@x -> support@x."""
    local, _, domain = (address or "").strip().lower().partition("@")
    return f"{local.split('+', 1)[0]}@{domain}"


def ours() -> set[str]:
    from app.api.support import mail

    return {*settings.support_inbound_addresses, *settings.sales_inbound_addresses, mail.from_address(),
            (settings.SALES_FROM_ADDRESS or "").strip().lower()} - {""}


async def receive(session, received: providers.Received, reader: Callable[[providers.Received], bytes]) -> dict:
    """Keep the mail for the desk and the sales team, as its recipients say.
    {"support": outcome, "sales": outcome} for the parts that applied; {}
    when it is for neither (or the desk's mail is dormant). Raises
    providers.Unreadable when the raw mail can't be read now."""
    support, sales = settings.support_inbound_addresses, settings.sales_inbound_addresses
    if not support and not sales:
        return {}
    bases = {base_address(r) for r in received.recipients}
    for_support, for_sales = bool(bases & support), bool(bases & sales)
    if not (for_support or for_sales):
        return {}
    if received.virus:
        logger.warning("desk: mail %s dropped: SES found a virus", received.provider_message_id[:24])
        return {"dropped": "virus"}
    try:
        raw = await asyncio.to_thread(reader, received)
    except providers.TooLarge:
        logger.warning("desk: mail %s dropped: too large", received.provider_message_id[:24])
        return {"dropped": "too_large"}
    try:
        parsed = parse.parse(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("desk: mail %s couldn't be parsed (%s); dropped", received.provider_message_id[:24], type(exc).__name__)
        return {"dropped": "unparseable"}
    out: dict = {}
    if for_support:
        out["support"] = await keep_support(session, received, parsed)
    if for_sales:
        out["sales"] = await keep_sales(session, received, parsed)
    return out


# ─── support@ ────────────────────────────────────────────────────────────


async def _seen(session, received: providers.Received) -> bool:
    return await session.scalar(
        select(SupportMessages.id).where(
            SupportMessages.kind == REPLY_IN, SupportMessages.provider == received.provider,
            SupportMessages.provider_message_id == received.provider_message_id,
        )
    ) is not None


async def by_headers(session, ids: list[str]) -> Optional[SupportCases]:
    ids = [i for i in ids if i][-50:]
    if not ids:
        return None
    case_id = await session.scalar(
        select(SupportMessages.case_id).where(or_(SupportMessages.email_message_id.in_(ids), SupportMessages.alt_message_id.in_(ids)))
        .order_by(SupportMessages.id.desc()).limit(1)
    )
    return await session.get(SupportCases, case_id) if case_id else None


async def by_tag(session, subject: str, sender: str) -> Optional[SupportCases]:
    """The case its subject's [#n] names, when the sender is the case's own
    person (or wrote on it before): a number alone is not a key."""
    number = text.number_in(subject)
    if number is None:
        return None
    case = await cases.by_number(session, number)
    if case is None:
        return None
    if (case.requester_email or "").lower() == sender:
        return case
    wrote = await session.scalar(
        select(SupportMessages.id).where(SupportMessages.case_id == case.id, SupportMessages.kind == REPLY_IN,
                                         func.lower(SupportMessages.from_address) == sender).limit(1)
    )
    return case if wrote else None


async def requester_for(session, sender: str, display: Optional[str]) -> tuple[cases.Requester, bool]:
    """(who it is, whether we know them): an app account with the address, a
    club whose contact or account it is, else a guest."""
    from app.api.clubs.models import Clubs
    from app.api.users.models import UserTypes, Users

    user = await session.scalar(
        select(Users).where(func.lower(func.btrim(Users.email)) == sender, Users.user_type == UserTypes.app_user).limit(1)
    )
    if user is not None:
        return cases.Requester(kind="user", user_id=user.id, email=sender, name=display or user.full_name), True
    club = await session.scalar(
        select(Clubs).outerjoin(Users, Users.id == Clubs.user_id)
        .where(or_(func.lower(Clubs.contact_email) == sender, func.lower(Users.email) == sender)).limit(1)
    )
    if club is not None:
        return cases.Requester(kind="club", club_id=club.id, email=sender, name=display or club.name), True
    return cases.Requester(kind="guest", email=sender, name=display or sender.split("@")[0]), False


async def _files(session, parsed: parse.Parsed) -> list:
    rows, total = [], 0
    for part in parsed.attachments[:MAX_FILES]:
        if part.inline and part.content_type.startswith("image/") and len(part.content) < 2048:
            continue  # a tracking pixel or a signature's spacer
        total += len(part.content)
        if total > MAX_FILE_BYTES or not part.content:
            break
        kind = files.sniff(part.content) or "application/octet-stream"
        rows.append(await files.keep(session, part.content, kind, files.safe_name(part.filename, "attachment")))
    return rows


async def keep_support(session, received: providers.Received, parsed: parse.Parsed) -> str:
    """One mail for the desk, kept and committed. Returns what became of it:
    joined, opened, spam, duplicate, loop or no_sender."""
    if await _seen(session, received):
        return "duplicate"
    sender = (parsed.reply_to or parsed.from_address or "").strip().lower()
    if not sender:
        return "no_sender"
    if base_address(sender) in ours():
        logger.info("desk: mail %s from our own address dropped", received.provider_message_id[:24])
        return "loop"
    body = text.mask_cards(parsed.text or "")[:200_000] or "(no text)"
    html = text.mask_cards(parsed.html) if parsed.html else None
    mail = cases.MailFields(
        from_address=sender, to_addresses=list(parsed.to)[:50], email_message_id=parsed.message_id, in_reply_to=parsed.in_reply_to,
        references=" ".join(parsed.references) or None, provider=received.provider, provider_message_id=received.provider_message_id,
    )
    keys: list[str] = []
    try:
        async with session.begin_nested():
            attachments = await _files(session, parsed)
            keys = [a.storage_key for a in attachments]
            requester, known = await requester_for(session, sender, parsed.from_name)
            if received.spam:
                case, _ = await cases.open_case(
                    session, requester=requester, topic=None, subject=parsed.subject, body_text=body, body_html=html, channel="email",
                    mail=mail, attachment_rows=attachments, tags=[SPAM_TAG], status=CLOSED, assign=False, topic_required=False,
                )
                outcome = "spam"
            else:
                case = await by_headers(session, [*parsed.references, *([parsed.in_reply_to] if parsed.in_reply_to else [])])
                case = case or await by_tag(session, parsed.subject, sender)
                case = case or await cases.recent_same(session, requester, parsed.subject)
                if case is not None:
                    case, _, new = await cases.requester_reply(
                        session, case, body_text=body, body_html=html, author_name=parsed.from_name or sender,
                        author_user_id=requester.user_id, mail=mail, attachment_rows=attachments,
                    )
                    outcome = "opened" if new else "joined"
                else:
                    case, _ = await cases.open_case(
                        session, requester=requester, topic="other", subject=parsed.subject, body_text=body, body_html=html,
                        channel="email", mail=mail, attachment_rows=attachments, flags=[] if known else [UNKNOWN_SENDER],
                        links={"user_id": requester.user_id} if requester.user_id else ({"club_id": requester.club_id} if requester.club_id else {}),
                        topic_required=False,
                    )
                    outcome = "opened"
            await cases.audit(session, None, f"case.mail_{outcome}", case, f"Case #{case.number}: mail from {masked(sender)}",
                              {"channel": "email"})
        await session.commit()
    except IntegrityError:
        await session.rollback()
        from app.api.support import store

        await asyncio.to_thread(store.delete, keys)
        return "duplicate"
    except Exception:
        await session.rollback()
        from app.api.support import store

        await asyncio.to_thread(store.delete, keys)
        raise
    return outcome


# ─── sales@ ──────────────────────────────────────────────────────────────


async def _admin_addresses(session, addresses: list[str]) -> dict[str, int]:
    from app.api.users.models import UserTypes, Users

    if not addresses:
        return {}
    rows = (await session.execute(
        select(func.lower(Users.email), Users.id).where(func.lower(Users.email).in_(addresses), Users.user_type == UserTypes.admin)
    )).all()
    return dict(rows)


async def keep_sales(session, received: providers.Received, parsed: parse.Parsed) -> str:
    """One mail for the sales team: a line on the leads it is about, or a new
    lead. Returns logged, new_lead, duplicate or nobody."""
    from app.api.leads import service as leads
    from app.api.leads.models import LeadActivities, LeadContacts, Leads
    from app.api.support import teams
    from app.api.support.models import SALES

    everyone = [a for a in dict.fromkeys([(parsed.from_address or "").lower(), *parsed.to, *parsed.cc]) if a]
    mine = ours()
    admins = await _admin_addresses(session, everyone)
    outside = [a for a in everyone if base_address(a) not in mine and a not in admins]
    if not outside:
        return "nobody"
    sender = (parsed.from_address or "").lower()
    direction = "out" if sender in admins else "in"
    body = text.mask_cards(parsed.text or "")[:100_000]
    details = {"subject": parsed.subject[:300], "from": sender, "to": list(parsed.to)[:20], "direction": direction}
    contact_rows = (await session.execute(
        select(LeadContacts.lead_id, LeadContacts.id, func.lower(LeadContacts.email))
        .join(Leads, Leads.id == LeadContacts.lead_id)
        .where(func.lower(LeadContacts.email).in_(outside), Leads.merged_into_id.is_(None))
    )).all()
    keys: list[str] = []
    try:
        async with session.begin_nested():
            lead_ids = list(dict.fromkeys(r[0] for r in contact_rows))
            if not lead_ids:
                who = outside[0]
                domain = leads.domain_of(who)
                display = parsed.from_name if direction == "in" else None
                name = (domain.split(".")[0].title() if domain else None) or display or who.split("@")[0]
                found = await leads.duplicates(session, name, None, [domain])
                if found:
                    lead = found[0]
                    session.add(LeadContacts(lead_id=lead.id, name=display or who, email=who))
                else:
                    owner = admins.get(sender) if sender in admins and admins[sender] in await teams.members_of(session, SALES) else None
                    owner = owner or await teams.next_member(session, SALES)
                    lead = await leads.make_lead(
                        session, name=name, type="other", owner_id=owner, source="inbound_email",
                        contacts=[{"name": display or who, "email": who}], note="From mail to the sales address.",
                    )
                    await leads.add_follow_up(session, lead=lead, kind="email", title=f"Answer {display or who}",
                                              due_at=await leads.soon(session, 4.0), owner_id=owner)
                lead_ids = [lead.id]
                outcome = "new_lead"
            else:
                outcome = "logged"
            logged = 0
            for lead_id in lead_ids:
                if await session.scalar(select(LeadActivities.id).where(
                    LeadActivities.lead_id == lead_id, LeadActivities.provider == received.provider,
                    LeadActivities.provider_message_id == received.provider_message_id,
                )):
                    continue
                lead = await session.get(Leads, lead_id)
                contact_id = next((cid for lid, cid, _ in contact_rows if lid == lead_id), None)
                await leads.log(session, lead, "email", f"{parsed.subject}\n\n{body}".strip(), contact_id=contact_id, details=details,
                                provider=received.provider, provider_message_id=received.provider_message_id,
                                occurred_at=parsed.date, author_name=parsed.from_name or sender)
                for part in parsed.attachments[:5]:
                    if part.content and len(part.content) <= 10 * 1024 * 1024 and not part.inline:
                        f = await leads.store_file(session, lead, part.content, files.sniff(part.content) or "application/octet-stream",
                                                   files.safe_name(part.filename, "attachment"))
                        keys.append(f.storage_key)
                await leads.audit(session, None, "mail", lead, f"Mail {direction} logged on {lead.name}", {"direction": direction})
                logged += 1
            if not logged and outcome == "logged":
                outcome = "duplicate"
        await session.commit()
    except IntegrityError:
        await session.rollback()
        from app.api.support import store

        await asyncio.to_thread(store.delete, keys)
        return "duplicate"
    except Exception:
        await session.rollback()
        from app.api.support import store

        await asyncio.to_thread(store.delete, keys)
        raise
    return outcome
