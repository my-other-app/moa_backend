"""The club blocking someone in its WhatsApp inbox.

At Meta (`POST /{number}/block_users`): they can no longer write to the
club's number or see it online, and the number can't write to them. Meta
allows it only for someone who wrote in the last 24 hours (131047 for
anyone else) -- the page then says so and offers a block here instead.

Here only (a "local" block): nothing reaches Meta. The conversation leaves
the inbox for its Blocked filter, nobody is alerted of what they send, and
nothing can be sent to them (409 CONTACT_BLOCKED) until it is lifted.
Either way the club's broadcasts skip them (optout.blocked).

Meta answers per person (a block can succeed for some and fail for others):
139100 is that partial failure, with each one's own error in
`failed_users[].errors`; 139101 a full blocklist (64,000); 139102 the list
changing while it was read; 139103 Meta's own error (try again). A parent
BSUID (".ENT.") can't be blocked at Meta. Blocking and unblocking are
audited (`club_whatsapp.block`/`unblock`), with no message content.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from app.api.club_whatsapp import graph, inbox
from app.api.club_whatsapp import service as accounts
from app.api.club_whatsapp.models import ClubWhatsAppConversations
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

#: Meta's per-person and per-call answers about the blocklist.
REENGAGE = "131047"
SELF = "131021"
PARTIAL = "139100"
FULL = "139101"
CHANGED = "139102"
INTERNAL = "139103"
RATE = "130429"
AGAIN = frozenset({CHANGED, INTERNAL, RATE})

LOCAL_OFFER = "You can block them here instead: they can still write, but their messages go to Blocked, nobody is " \
              "alerted, and nothing is sent to them."


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _refuse(code: int, message: str, error_code: str, errors: Optional[dict] = None) -> CustomHTTPException:
    return CustomHTTPException(status_code=code, message=message, error_code=error_code, errors=errors)


def who(conv: ClubWhatsAppConversations) -> Optional[dict]:
    """The block_users entry for them: their number (Meta's `user`, which wins),
    else their BSUID (`user_id`) -- never a parent BSUID, which Meta refuses."""
    if conv.wa_id:
        return {"user": f"+{conv.wa_id}"}
    if conv.bsuid and ".ENT." not in conv.bsuid:
        return {"user_id": conv.bsuid}
    return None


def _entries(answer: dict, key: str) -> list[dict]:
    lists = answer.get("block_users") if isinstance(answer.get("block_users"), dict) else {}
    return [e for e in lists.get(key) or [] if isinstance(e, dict)]


def _first_error(entry: dict) -> tuple[Optional[str], str]:
    errors = entry.get("errors") if isinstance(entry.get("errors"), list) else []
    error = errors[0] if errors and isinstance(errors[0], dict) else {}
    details = (error.get("error_data") or {}).get("details") if isinstance(error.get("error_data"), dict) else None
    code = None if error.get("code") is None else str(error.get("code"))
    return code, str(details or error.get("message") or "Meta didn't say why")[:300]


def outcome(answer: dict, error: Optional[graph.GraphError], done_key: str) -> tuple[bool, Optional[str], str]:
    """(done, Meta's code, its words) for our one person, from Meta's answer:
    `done_key` added_users (a block) or removed_users (an unblock); a
    failed_users entry's own code before the call's (139100 says only that
    some failed)."""
    if _entries(answer, done_key):
        return True, None, ""
    failed = _entries(answer, "failed_users")
    if failed:
        code, words = _first_error(failed[0])
        return False, code, words
    if error is not None:
        return False, error.code, error.message
    top = answer.get("error") if isinstance(answer.get("error"), dict) else None
    if top:
        return False, None if top.get("code") is None else str(top.get("code")), str(top.get("message") or "")[:300]
    # Meta answered with neither list: taken as done (the call itself succeeded).
    return True, None, ""


async def _call(session, account, keys, conv, method: str) -> tuple[bool, Optional[str], str]:
    entry = who(conv)
    if entry is None:
        raise _refuse(409, "Meta can't block someone known to it only by a parent id. " + LOCAL_OFFER, "BLOCK_NOT_POSSIBLE",
                      {"local_available": True})
    body = {"messaging_product": "whatsapp", "block_users": [entry]}
    answer, error = {}, None
    try:
        answer = await graph.call(keys, method, f"{keys.phone_number_id}/block_users", json=body)
    except graph.GraphError as exc:
        answer, error = exc.body or {}, exc
        if not _entries(answer, "failed_users") and not _entries(answer, "added_users" if method == "POST" else "removed_users"):
            if exc.unreachable and exc.code not in AGAIN:
                raise _refuse(502, "Couldn't reach WhatsApp to block them. Try again.", "META_UNREACHABLE") from None
            if graph.is_token_problem(exc):
                _status, code, words = graph.explain(exc)
                await accounts.mark_failed(session, account, words, exc.code, source=accounts.READ)
                await session.commit()
                raise _refuse(409, f"Meta didn't accept your number's keys. {words}", code) from None
    return outcome(answer, error, "added_users" if method == "POST" else "removed_users")


def _explain(code: Optional[str], words: str, *, unblock: bool = False) -> CustomHTTPException:
    verb = "unblock" if unblock else "block"
    errors = {"meta_code": code, "reason": words, "local_available": not unblock}
    if code == REENGAGE:
        return _refuse(409, f"Meta only lets you {verb} someone who wrote to you in the last 24 hours."
                       + ("" if unblock else " " + LOCAL_OFFER), "BLOCK_WINDOW_CLOSED", errors)
    if code == SELF:
        return _refuse(409, "That's your own number: it can't be blocked.", "BLOCK_REFUSED", {**errors, "local_available": False})
    if code == FULL:
        return _refuse(409, "Your number's blocklist is full (Meta allows 64,000). " + LOCAL_OFFER, "BLOCKLIST_FULL", errors)
    if code in AGAIN:
        return _refuse(503, f"WhatsApp couldn't {verb} them just now. Try again in a minute.", "BLOCK_TRY_AGAIN", errors)
    return _refuse(422, f"Meta wouldn't {verb} them: {words}"[:300], "BLOCK_REFUSED", errors)


async def block(session, club_id: int, conv: ClubWhatsAppConversations, *, local_only: bool, me, user, request,
                actor_user=None) -> None:
    """Block them at Meta (and here), or -- `local_only` -- here alone."""
    from app.api.audit import service as audit
    from app.api.club_whatsapp import alerts_inbox

    if conv.blocked_at is not None and (conv.blocked_meta or local_only):
        return
    meta = False
    if not local_only:
        account, keys = await inbox._sending_account(session, club_id, actor_user)
        if conv.business_phone_id != account.phone_number_id:
            raise _refuse(409, "This conversation was on another of your numbers. " + LOCAL_OFFER, "OTHER_NUMBER",
                          {"local_available": True})
        if not inbox.window_open(conv):
            raise _explain(REENGAGE, "They haven't written in the last 24 hours.")
        await session.commit()
        done, code, words = await _call(session, account, keys, conv, "POST")
        if not done:
            raise _explain(code, words)
        meta = True
    conv.blocked_at = conv.blocked_at or _now()
    conv.blocked_meta = meta or bool(conv.blocked_meta)
    conv.blocked_by_name = me.name[:120]
    await audit.record(
        session, actor=user, action="club_whatsapp.block", entity_type="club", entity_id=club_id,
        summary=f"Blocked a WhatsApp contact{' at Meta' if meta else ' in the inbox'}",
        details={"conversation_id": conv.id, "meta": meta, "member_id": me.member_id}, request=request,
    )
    await session.flush()
    await alerts_inbox.sync_conversation(session, club_id, conv.id)


async def unblock(session, club_id: int, conv: ClubWhatsAppConversations, *, local_only: bool, me, user, request,
                  actor_user=None) -> None:
    """Lift the block: at Meta too when it was blocked there (`local_only`:
    forget it here alone -- it was lifted in WhatsApp Manager, say)."""
    from app.api.audit import service as audit
    from app.api.club_whatsapp import alerts_inbox

    if conv.blocked_at is None:
        return
    at_meta = bool(conv.blocked_meta) and not local_only
    if at_meta:
        account, keys = await inbox._sending_account(session, club_id, actor_user)
        if conv.business_phone_id != account.phone_number_id:
            raise _refuse(409, "This conversation was on another of your numbers; lift the block there, then here.", "OTHER_NUMBER")
        await session.commit()
        done, code, words = await _call(session, account, keys, conv, "DELETE")
        if not done:
            raise _explain(code, words, unblock=True)
    conv.blocked_at = None
    conv.blocked_meta = False
    conv.blocked_by_name = None
    await audit.record(
        session, actor=user, action="club_whatsapp.unblock", entity_type="club", entity_id=club_id,
        summary="Unblocked a WhatsApp contact", details={"conversation_id": conv.id, "meta": at_meta, "member_id": me.member_id},
        request=request,
    )
    await session.flush()
    await alerts_inbox.sync_conversation(session, club_id, conv.id)
