"""People who told the club to stop writing to them on WhatsApp.

The store (`club_whatsapp_optouts`) is the club's, keyed by the person's
WhatsApp id and/or BSUID, one row per scope:

- `all`: they wrote STOP (STOP ALL, UNSUBSCRIBE, OPT OUT, OPTOUT -- the whole
  message, trimmed, any case, trailing punctuation ignored), or the team
  recorded it on their word. Nothing is sent to them from the club's number
  but a reply inside a 24-hour window they open themselves: no template --
  not the inbox's, not a broadcast -- and their tickets, certificates and
  reminders for the club's events skip WhatsApp altogether, from the club's
  number and the platform's alike (the email still goes; the WhatsApp row
  says "opted out of <club> on WhatsApp"). START, SUBSCRIBE, OPT IN or
  UNSTOP opts them back in.
- `marketing`: they stopped marketing in WhatsApp's own settings (Meta's
  `user_preferences` webhook, `marketing_messages` stop; `resume` clears
  it), or Meta failed a marketing template to them with 131050. No
  MARKETING template goes to them.

STOP and START are answered at once, in the window their message just
opened, with words the club can change (inbox settings), as a reply to it --
by a job after the webhook's commit, kept in the thread as an automatic
reply. Only when the opt-out changed: a second STOP is not answered again.

Retention never deletes an opt-out: it lasts until they (or, on their word,
the team) opt back in, which deletes the row. The team's opt-outs and
opt-ins are in the audit log (`club_whatsapp.optout`/`optin`), with no
message content.

`blocked()` is the store's one question for the rest of the code base (the
broadcasts read it): keep its name and signature.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.club_whatsapp.inbox_models import (
    ALL,
    AUTO_REPLY_MAX,
    KEYWORD,
    MARKETING,
    META_131050,
    SCOPES,
    TEAM,
    USER_PREFERENCES,
    ClubWhatsAppInboxSettings,
    ClubWhatsAppOptOuts,
)
from app.api.club_whatsapp.models import ClubWhatsAppConversations, ClubWhatsAppMessages
from app.core import jobs
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

#: The whole message, as the person wrote it (trimmed, case-folded, trailing punctuation dropped).
STOP_WORDS = frozenset({"stop", "stop all", "unsubscribe", "opt out", "optout"})
START_WORDS = frozenset({"start", "subscribe", "opt in", "unstop"})
STOP, START = "stop", "start"

#: An automatic reply's `via`, beside inbox | delivery: not the team's answer.
VIA_AUTO = "auto"
AUTO_NAME = "Automatic reply"
AUTO_JOB = "club_whatsapp.auto_reply"

#: Meta: a marketing template to someone who stopped marketing (sent, then failed by webhook).
MARKETING_STOPPED = "131050"

DEFAULT_REPLY = {
    STOP: "You won't get WhatsApp messages from {club} any more. Reply START if you change your mind.",
    START: "You'll get WhatsApp messages from {club} again. Reply STOP any time to stop them.",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _refuse(code: int, message: str, error_code: str, errors: Optional[dict] = None) -> CustomHTTPException:
    return CustomHTTPException(status_code=code, message=message, error_code=error_code, errors=errors)


# ─── Who ─────────────────────────────────────────────────────────────────


def wa_key(raw) -> Optional[str]:
    """A person's WhatsApp id: digits with the country code, as Meta writes
    it ("919876543210"). A number as typed ("+91 98765 43210", "98765
    43210") is read as a registration's is (Indian without a country code)."""
    from app.api.club_whatsapp import inbox

    text = str(raw or "").strip()
    digits = re.sub(r"\D", "", text)
    if not digits:
        return None
    if text.startswith("+") or text.startswith("00") or len(digits) <= 10 or digits.startswith("0"):
        return inbox.wa_of(text)
    return digits[:20]


def bsuid_key(raw) -> Optional[str]:
    from app.api.club_whatsapp import inbox

    return inbox.bsuid_of(raw)


def _matching(model, wa: Optional[str], bsuid: Optional[str]):
    keys = []
    if wa:
        keys.append(model.wa_id == wa)
    if bsuid:
        keys.append(model.bsuid == bsuid)
    return or_(*keys)


# ─── The contract ────────────────────────────────────────────────────────


async def blocked(session, club_id: int, *, wa_id: Optional[str] = None, bsuid: Optional[str] = None,
                  scope: str = "all") -> bool:
    """Whether the club must not send this person what `scope` is:

    - "all": anything the club starts -- a template from the inbox, a
      broadcast. True when they opted out of the club's WhatsApp (STOP, or
      the team on their word).
    - "marketing": a MARKETING template. True for an `all` opt-out too, and
      when they stopped marketing in WhatsApp (or Meta said so with 131050).

    Either way, also True when the club blocked them in its inbox (here or
    at Meta). `wa_id` is their WhatsApp id (digits with the country code; a
    number as typed is read too) and/or `bsuid` their business-scoped user
    id; with neither, False. Read-only; the caller's session.

    The broadcasts call this (feat/club-whatsapp-reach): keep its name and
    signature."""
    if scope not in SCOPES:
        raise ValueError(f"scope is one of {', '.join(SCOPES)}")
    wa, b = wa_key(wa_id), bsuid_key(bsuid)
    if not (wa or b):
        return False
    scopes = (ALL,) if scope == ALL else (ALL, MARKETING)
    O, C = ClubWhatsAppOptOuts, ClubWhatsAppConversations
    hit = await session.scalar(
        select(O.id).where(O.club_id == club_id, O.scope.in_(scopes), _matching(O, wa, b)).limit(1)
    )
    if hit is not None:
        return True
    return await session.scalar(
        select(C.id).where(C.club_id == club_id, C.blocked_at.isnot(None), _matching(C, wa, b)).limit(1)
    ) is not None


async def opted_out(session, club_id: int, *, wa_id: Optional[str] = None, bsuid: Optional[str] = None,
                    scopes: tuple = (ALL,)) -> Optional[ClubWhatsAppOptOuts]:
    """Their opt-out of one of `scopes` (the store alone, not a block), or None."""
    wa, b = wa_key(wa_id), bsuid_key(bsuid)
    if not (wa or b):
        return None
    O = ClubWhatsAppOptOuts
    rows = (await session.scalars(
        select(O).where(O.club_id == club_id, O.scope.in_(scopes), _matching(O, wa, b))
    )).all()
    # `all` says more than `marketing`.
    return sorted(rows, key=lambda r: (r.scope != ALL, r.id))[0] if rows else None


async def statuses(session, club_id: int, convs: list) -> dict[int, dict]:
    """Each conversation's opt-out, for its banner: {conversation id: {scope, source, at}}."""
    out: dict[int, dict] = {}
    was = {c.wa_id for c in convs if c.wa_id}
    bs = {c.bsuid for c in convs if c.bsuid}
    if not (was or bs):
        return out
    O = ClubWhatsAppOptOuts
    clauses = ([O.wa_id.in_(was)] if was else []) + ([O.bsuid.in_(bs)] if bs else [])
    rows = (await session.scalars(select(O).where(O.club_id == club_id, or_(*clauses)))).all()
    for c in convs:
        mine = [r for r in rows if (c.wa_id and r.wa_id == c.wa_id) or (c.bsuid and r.bsuid == c.bsuid)]
        if mine:
            r = sorted(mine, key=lambda r: (r.scope != ALL, r.id))[0]
            out[c.id] = {"scope": r.scope, "source": r.source, "at": r.at, "by": r.by_name}
    return out


# ─── Changing it ─────────────────────────────────────────────────────────


async def set_optout(session, club_id: int, *, wa_id: Optional[str], bsuid: Optional[str], scope: str, source: str,
                     by_user_id: Optional[int] = None, by_member_id: Optional[int] = None,
                     by_name: Optional[str] = None) -> bool:
    """Record the opt-out (an insert that does nothing when another made it
    first; an existing one takes the newest source and time, and whichever
    key it lacked). True when they weren't opted out of `scope` before."""
    wa, b = wa_key(wa_id), bsuid_key(bsuid)
    if not (wa or b):
        return False
    O = ClubWhatsAppOptOuts
    now = _now()
    for _ in range(2):
        row = await session.scalar(
            select(O).where(O.club_id == club_id, O.scope == scope, _matching(O, wa, b)).order_by(O.id).limit(1).with_for_update()
        )
        if row is not None:
            row.source, row.at = source, now
            row.by_user_id, row.by_member_id, row.by_name = by_user_id, by_member_id, (by_name or None) and by_name[:120]
            for field, value in (("wa_id", wa), ("bsuid", b)):
                if value and getattr(row, field) != value:
                    taken = await session.scalar(
                        select(O.id).where(O.club_id == club_id, O.scope == scope, getattr(O, field) == value, O.id != row.id)
                    )
                    if taken is None:
                        setattr(row, field, value)
            await session.flush()
            return False
        made = await session.scalar(
            pg_insert(O.__table__).values(
                club_id=club_id, wa_id=wa, bsuid=b, scope=scope, source=source, at=now,
                by_user_id=by_user_id, by_member_id=by_member_id, by_name=(by_name or None) and by_name[:120],
            ).on_conflict_do_nothing().returning(O.__table__.c.id)
        )
        if made is not None:
            return True
    return False


async def clear_optout(session, club_id: int, *, wa_id: Optional[str], bsuid: Optional[str], scope: str) -> bool:
    """Opt them back in to `scope`. True when they had opted out of it."""
    wa, b = wa_key(wa_id), bsuid_key(bsuid)
    if not (wa or b):
        return False
    O = ClubWhatsAppOptOuts
    gone = await session.execute(
        delete(O).where(O.club_id == club_id, O.scope == scope, _matching(O, wa, b)).returning(O.id)
    )
    return bool(gone.all())


# ─── What people say ─────────────────────────────────────────────────────


def keyword(text: Optional[str]) -> Optional[str]:
    """STOP or START, when the whole message is one of their words: trimmed,
    case-folded, its inner spaces made one, trailing punctuation ignored."""
    words = " ".join(str(text or "").split()).casefold()
    while words and (unicodedata.category(words[-1]).startswith("P") or words[-1].isspace()):
        words = words[:-1]
    if words in STOP_WORDS:
        return STOP
    if words in START_WORDS:
        return START
    return None


async def on_inbound(session, account, conv_id: int, message: dict) -> Optional[str]:
    """A message kept in the club's inbox: when it is STOP or START, the
    person is opted out (or back in) and, when that changed something, an
    automatic answer is queued -- a job, run once the webhook commits. The
    keyword, or None."""
    if str(message.get("type") or "") != "text":
        return None
    said = keyword(((message.get("text") or {}) if isinstance(message.get("text"), dict) else {}).get("body"))
    if said is None:
        return None
    C = ClubWhatsAppConversations
    who = (await session.execute(select(C.wa_id, C.bsuid, C.blocked_at).where(C.id == conv_id))).first()
    if who is None:
        return None
    wa, b, blocked_at = who
    if said == STOP:
        changed = await set_optout(session, account.club_id, wa_id=wa, bsuid=b, scope=ALL, source=KEYWORD)
    else:
        changed = await clear_optout(session, account.club_id, wa_id=wa, bsuid=b, scope=ALL)
    wamid = str(message.get("id") or "")[:128]
    # Not to someone the club blocked: it sends them nothing.
    if changed and wamid and blocked_at is None:
        await jobs.enqueue(
            session, AUTO_JOB, {"club_id": account.club_id, "conversation_id": conv_id, "kind": said, "wamid": wamid},
            dedupe_key=f"{AUTO_JOB}:{wamid}",
        )
    return said


async def user_preferences(session, account, value: dict) -> int:
    """Meta's `user_preferences` webhook: someone stopped (or resumed)
    marketing messages from the business in WhatsApp's own settings. A
    `marketing` opt-out is set (or cleared). Only for the club's own number.
    How many changed."""
    number = str(((value.get("metadata") or {}).get("phone_number_id")) or "")
    if number and number != account.phone_number_id:
        return 0
    changed = 0
    for p in value.get("user_preferences") or []:
        if not isinstance(p, dict) or str(p.get("category") or "") != "marketing_messages":
            continue
        wa, b = wa_key(p.get("wa_id")), bsuid_key(p.get("user_id"))
        what = str(p.get("value") or "").lower()
        if what == "stop":
            changed += await set_optout(session, account.club_id, wa_id=wa, bsuid=b, scope=MARKETING, source=USER_PREFERENCES)
        elif what == "resume":
            changed += await clear_optout(session, account.club_id, wa_id=wa, bsuid=b, scope=MARKETING)
    return changed


async def on_status(session, account, st: dict) -> bool:
    """A status Meta sent for the club's number: a marketing template failed
    with 131050 means they stopped marketing -- a `marketing` opt-out. Who:
    the message's conversation, else Meta's recipient. True when it was set."""
    if str(st.get("status") or "").lower() != "failed":
        return False
    errors = st.get("errors") if isinstance(st.get("errors"), list) else []
    if not any(isinstance(e, dict) and str(e.get("code")) == MARKETING_STOPPED for e in errors):
        return False
    M, C = ClubWhatsAppMessages, ClubWhatsAppConversations
    who = (await session.execute(
        select(C.wa_id, C.bsuid).join(M, M.conversation_id == C.id)
        .where(M.wamid == str(st.get("id") or ""), M.club_id == account.club_id)
    )).first()
    wa, b = (who[0], who[1]) if who is not None else (st.get("recipient_id"), st.get("recipient_user_id"))
    return await set_optout(session, account.club_id, wa_id=wa, bsuid=b, scope=MARKETING, source=META_131050)


# ─── What it stops ───────────────────────────────────────────────────────


async def check_send(session, conv: ClubWhatsAppConversations, *, kind: str, category: Optional[str] = None) -> None:
    """Before the inbox sends anything: never to someone the club blocked;
    no template (`kind` "template", or "start" for a new conversation) to
    someone opted out of everything, nor a MARKETING one to someone who
    stopped marketing. Free text and attachments go by the window alone --
    which only the person opens."""
    if conv.blocked_at is not None:
        raise _refuse(409, "You blocked them, so nothing can be sent. Unblock them first.", "CONTACT_BLOCKED")
    if kind not in ("template", "start"):
        return
    scopes = (ALL, MARKETING) if (category or "").upper() == "MARKETING" else (ALL,)
    row = await opted_out(session, conv.club_id, wa_id=conv.wa_id, bsuid=conv.bsuid, scopes=scopes)
    if row is None:
        return
    if row.scope == ALL:
        raise _refuse(409, "They opted out of your WhatsApp messages, so no template can be sent to them. You can answer "
                           "in words within 24 hours of a message they send.", "OPTED_OUT", {"scope": ALL, "source": row.source})
    raise _refuse(409, "They stopped marketing messages from you in WhatsApp, so a marketing template won't reach them. "
                       "Pick a utility template.", "OPTED_OUT_MARKETING", {"scope": MARKETING, "source": row.source})


async def delivery_skip(session, club_id: Optional[int], club_name: Optional[str], phone: Optional[str]) -> Optional[str]:
    """Why a ticket, certificate or reminder for the club's event skips
    WhatsApp -- from the club's number or the platform's -- or None: the
    guest's number opted out of the club's WhatsApp."""
    if club_id is None:
        return None
    wa = wa_key(phone)
    if wa is None:
        return None
    if await opted_out(session, club_id, wa_id=wa) is None:
        return None
    return f"opted out of {club_name or 'the club'} on WhatsApp"[:300]


# ─── The automatic answer ────────────────────────────────────────────────


async def replies_of(session, club_id: int) -> dict[str, Optional[str]]:
    """The club's own words for STOP and START, or None for the default."""
    row = await session.get(ClubWhatsAppInboxSettings, club_id)
    return {STOP: row.stop_reply if row else None, START: row.start_reply if row else None}


def reply_text(kind: str, own: Optional[str], club_name: str) -> str:
    words = (own or "").strip() or DEFAULT_REPLY[kind]
    return words.replace("{club}", club_name or "us")[:AUTO_REPLY_MAX]


async def send_auto_reply(session, club_id: int, conversation_id: int, kind: str, wamid: str) -> Optional[ClubWhatsAppMessages]:
    """The answer to STOP or START, as a reply to it, in the window it just
    opened -- once (a second run finds it kept). Nothing when the number
    can't send (not active, the plan lapsed, the keys unreadable), the
    conversation is on another number, or the club blocked them."""
    from app.api.clubs.models import Clubs
    from app.api.club_whatsapp import crypto, inbox, live
    from app.api.club_whatsapp import service as accounts
    from app.api.club_whatsapp.models import ACTIVE, OUT, PENDING

    conv = await session.get(ClubWhatsAppConversations, conversation_id)
    if conv is None or conv.club_id != club_id or conv.blocked_at is not None or kind not in (STOP, START):
        return None
    M = ClubWhatsAppMessages
    if await session.scalar(select(M.id).where(M.conversation_id == conv.id, M.via == VIA_AUTO, M.context_wamid == wamid)):
        return None
    account = await accounts.live_account(session, club_id)
    if (account is None or account.status != ACTIVE or account.phone_number_id != conv.business_phone_id
            or not inbox.window_open(conv) or not await accounts.plan_allows(session, club_id)):
        return None
    try:
        keys = accounts.keys_of(account)
    except crypto.Unavailable:
        return None
    club_name = await session.scalar(select(Clubs.name).where(Clubs.id == club_id)) or ""
    text = reply_text(kind, (await replies_of(session, club_id))[kind], club_name)
    row = M(conversation_id=conv.id, club_id=club_id, direction=OUT, type="text", body=text, status=PENDING, via=VIA_AUTO,
            context_wamid=wamid, sent_by_name=AUTO_NAME)
    # Kept first: a run that stops after Meta took it doesn't send it twice.
    session.add(row)
    await session.commit()
    body = {"messaging_product": "whatsapp", "recipient_type": "individual", **inbox.recipient(conv), "type": "text",
            "text": {"preview_url": False, "body": text}, "context": {"message_id": wamid}}
    row = await inbox._send(session, account, keys, conv, body, row)
    await session.commit()
    await live.changed(club_id, conv.id, "message")
    return row


@jobs.handler(AUTO_JOB, max_attempts=4, timeout=60.0, priority=jobs.HIGH)
async def auto_reply_job(payload: dict) -> None:
    async with jobs.scope() as session:
        await send_auto_reply(session, int(payload["club_id"]), int(payload["conversation_id"]), str(payload["kind"]),
                              str(payload.get("wamid") or ""))


# ─── The team, on the person's word ──────────────────────────────────────


async def team_change(session, club_id: int, conv: ClubWhatsAppConversations, *, opt_out: bool, scope: str,
                      actor, user, request) -> bool:
    """The team opting someone out (or back in) on their word, audited with
    no message content. A `marketing` stop the person set in WhatsApp (or
    Meta's 131050) is theirs to undo: 409 OPTOUT_IS_THEIRS."""
    from app.api.audit import service as audit

    if scope not in SCOPES:
        raise _refuse(422, "scope is all or marketing", "INVALID_SCOPE")
    if opt_out:
        changed = await set_optout(session, club_id, wa_id=conv.wa_id, bsuid=conv.bsuid, scope=scope, source=TEAM,
                                   by_user_id=actor.user_id, by_member_id=actor.member_id, by_name=actor.name)
    else:
        row = await opted_out(session, club_id, wa_id=conv.wa_id, bsuid=conv.bsuid, scopes=(scope,))
        if row is not None and row.source in (USER_PREFERENCES, META_131050):
            raise _refuse(409, "They stopped marketing messages in WhatsApp's own settings; only they can turn them back on.",
                          "OPTOUT_IS_THEIRS")
        changed = await clear_optout(session, club_id, wa_id=conv.wa_id, bsuid=conv.bsuid, scope=scope)
    if changed:
        await audit.record(
            session, actor=user, action=f"club_whatsapp.{'optout' if opt_out else 'optin'}", entity_type="club", entity_id=club_id,
            summary=f"{'Opted out' if opt_out else 'Opted back in'} a WhatsApp contact ({scope})",
            details={"conversation_id": conv.id, "scope": scope, "member_id": actor.member_id}, request=request,
        )
    return changed
