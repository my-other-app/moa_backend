"""Prepaid message credits: top-up packs, the lots they become, and what
draws on them.

Each month a club's sends first use its plan's allowance -- the plan
credits, counted in service.py, with the plan's 20% cap on promotional
WhatsApp. Past it, a send draws on the club's credit lots, the one that
expires first first (`consume`). Bought credits are not held to the
promotional cap. With nothing left, what is not a ticket, certificate or
payment message is held back (service.held_back); those three always go,
and are counted as `over`, never charged.

A lot is bought -- a pack paid for with a one-time Razorpay order on the
platform's account -- or granted by an admin. Either lasts
VALIDITY_MONTHS from when it was had.

Buying (the club's owner, on Billing):

1. `start_purchase`: the Razorpay order, then a purchase row (`created`)
   holding the pack's channel, quantity and price as they were. Nothing is
   held open while Razorpay answers.
2. The club pays in Razorpay's checkout. `verify` (the dashboard, with
   checkout's ids and signature) and the platform's webhook
   (`handle_platform_event`: payment.captured, order.paid...) both end in
   `apply_payment`, which works only from what Razorpay says when asked:
   once the payment is captured for the order, the purchase is `paid` and
   its lot made -- once, under the purchase's row lock, and the lot's
   purchase id is unique besides.
3. The hourly sweep (`reconcile`) asks Razorpay about checkouts still open
   after RECONCILE_AFTER, for a payment whose webhook never came.

Refunds (an admin's, `refund`): the lot's unused credits are taken back at
once and the purchase is `refunding` (committed) before Razorpay is asked
for the money, by default the unused share of what was paid. Razorpay
refusing or not answering leaves `refund_failed`; asking again checks
Razorpay's figure for what was refunded first, so a refund is never made
twice. A refund made in Razorpay's own dashboard reaches here as
`refund.processed` and takes the unused credits back the same way.

Telling the club (the dashboard's bell and a mail to the owner): when its
bought credits for a channel run under LOW_SHARE_PERCENT of the last pack
it had (`_maybe_warn_low`, once per pack), and EXPIRY_NOTICE before a lot
with credits left expires (`warn_expiring`, the sweep).
"""
from __future__ import annotations

import calendar
import hashlib
import hmac
import logging
import math
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.message_quota import service
from app.api.message_quota.models import (
    LotSource,
    MessageCreditLots,
    MessageCreditPacks,
    MessageCreditPurchases,
    MessageUsage,
    PurchaseStatus as P,
)
from app.config import settings
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

#: How long credits last, from when they were bought or granted.
VALIDITY_MONTHS = 12
#: "Running low": under this share of the last pack the club had.
LOW_SHARE_PERCENT = 10
#: How long before a lot expires the club is told.
EXPIRY_NOTICE = timedelta(days=30)
#: What `remaining` answers for a plan with no limit.
UNLIMITED = 2_147_483_647
#: Pack prices, like plan prices, are what Razorpay charges: GST included.
PRICES_INCLUDE_GST = True
#: The sweep asks Razorpay about a checkout left open this long...
RECONCILE_AFTER = timedelta(minutes=10)
#: ...for this long after it was opened (Razorpay's checkout expires well before).
RECONCILE_WITHIN = timedelta(days=2)
#: Rows per sweep step.
BATCH = 200
#: A refund asked of Razorpay this recently is still in flight: not asked again.
REFUND_IN_FLIGHT = timedelta(minutes=2)

#: The owner's packs (migration message_credits seeds them; admins edit them):
#: (channel, quantity, price in paise, sort order).
DEFAULT_PACKS = (
    (service.EMAIL, 1_000, 9_900, 10),
    (service.EMAIL, 5_000, 44_900, 20),
    (service.EMAIL, 20_000, 159_900, 30),
    (service.WHATSAPP, 100, 15_000, 10),
    (service.WHATSAPP, 500, 69_900, 20),
    (service.WHATSAPP, 2_000, 259_900, 30),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def add_months(at: datetime, months: int) -> datetime:
    """The same instant `months` later; the 31st becomes the month's last day."""
    total = at.month - 1 + months
    year, month = at.year + total // 12, total % 12 + 1
    return at.replace(year=year, month=month, day=min(at.day, calendar.monthrange(year, month)[1]))


def receipt_number(purchase_id: int) -> str:
    return f"MOA-CR-{int(purchase_id):06d}"


def credits_noun(channel: str, n: Optional[int] = None) -> str:
    """'email credits', 'WhatsApp credit'."""
    one, many = ("email credit", "email credits") if channel == service.EMAIL else ("WhatsApp credit", "WhatsApp credits")
    return one if n == 1 else many


def pack_words(channel: str, quantity: int) -> str:
    """'5,000 email credits'."""
    return f"{quantity:,} {credits_noun(channel, quantity)}"


def _channel(channel: str) -> str:
    if channel not in service.CHANNELS:
        raise ValueError(f"channel is 'email' or 'whatsapp', not {channel!r}")
    return channel


# ─── Balances ────────────────────────────────────────────────────────────


def _live(club_id: int, channel: str, at: datetime):
    """A club's lots of one channel with credits left, not expired."""
    return (
        MessageCreditLots.club_id == club_id,
        MessageCreditLots.channel == channel,
        MessageCreditLots.remaining > 0,
        MessageCreditLots.expires_at > at,
        MessageCreditLots.refunded_at.is_(None),
    )


async def bought_remaining(session, club_id: int, channel: str, at: Optional[datetime] = None) -> int:
    """Credits left in the club's lots of this channel (bought or granted)."""
    total = await session.scalar(
        select(func.coalesce(func.sum(MessageCreditLots.remaining), 0)).where(*_live(club_id, _channel(channel), at or _now()))
    )
    return int(total or 0)


async def live_lots(session, club_id: int, channel: str, at: Optional[datetime] = None) -> list[MessageCreditLots]:
    """The club's lots of this channel with credits left, the first to expire first."""
    return list(
        await session.scalars(
            select(MessageCreditLots)
            .where(*_live(club_id, channel, at or _now()))
            .order_by(MessageCreditLots.expires_at, MessageCreditLots.id)
        )
    )


async def _month_row(session, club_id: int, channel: str, at: datetime) -> Optional[MessageUsage]:
    return await session.scalar(
        select(MessageUsage).where(
            MessageUsage.club_id == club_id, MessageUsage.period == service.period_of(at), MessageUsage.channel == channel
        )
    )


async def remaining(session, club_id: int, channel: str) -> int:
    """What the club can still send on `channel` before non-essential
    messages are held back: what is left of this month's plan allowance
    plus its bought (and granted) credits. UNLIMITED when the plan has no
    limit on the channel. Reads only; the club mailbox shows it.

    Contract for other senders (the club mailbox): keep this signature."""
    channel = _channel(channel)
    now = _now()
    terms = await service.terms_for(session, club_id)
    limit = terms.limit(channel)
    if limit is None:
        return UNLIMITED
    row = await _month_row(session, club_id, channel, now)
    plan_left = max(0, limit - (int(row.plan_used or 0) if row is not None else 0))
    return plan_left + await bought_remaining(session, club_id, channel, now)


async def consume(session, terms: service.Terms, channel: str, count: int, at: Optional[datetime] = None) -> int:
    """Take up to `count` credits from the club's lots, the first to
    expire first, under the lots' row locks (the caller's transaction holds
    them to its commit). Returns how many were taken; the rest had nothing
    to come from."""
    if count <= 0:
        return 0
    at = at or _now()
    lots = list(
        await session.scalars(
            select(MessageCreditLots)
            .where(*_live(terms.club_id, channel, at))
            .order_by(MessageCreditLots.expires_at, MessageCreditLots.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    taken = 0
    for lot in lots:
        if taken >= count:
            break
        take = min(int(lot.remaining), count - taken)
        lot.remaining = int(lot.remaining) - take
        taken += take
    if taken:
        await session.flush()
        try:
            await _maybe_warn_low(session, terms.club_id, channel, at)
        except Exception:  # noqa: BLE001 - a warning that cannot go must not undo the count
            logger.warning("club %s: the low %s credits warning could not be raised", terms.club_id, channel, exc_info=True)
    return taken


async def _last_lot(session, club_id: int, channel: str) -> Optional[MessageCreditLots]:
    """The last pack the club had on this channel (bought or granted)."""
    return await session.scalar(
        select(MessageCreditLots)
        .where(MessageCreditLots.club_id == club_id, MessageCreditLots.channel == channel)
        .order_by(MessageCreditLots.bought_at.desc(), MessageCreditLots.id.desc())
        .limit(1)
    )


def _is_low(left: int, last: Optional[MessageCreditLots]) -> bool:
    return last is not None and left * 100 < int(last.quantity) * LOW_SHARE_PERCENT


async def _maybe_warn_low(session, club_id: int, channel: str, at: datetime) -> None:
    """Once per pack: the club's credits on this channel fell under
    LOW_SHARE_PERCENT of the last pack it had. Claimed with one UPDATE ...
    WHERE low_warned_at IS NULL, so of two senders crossing it one tells."""
    last = await _last_lot(session, club_id, channel)
    if last is None or last.low_warned_at is not None:
        return
    left = await bought_remaining(session, club_id, channel, at)
    if not _is_low(left, last):
        return
    claimed = await session.scalar(
        update(MessageCreditLots)
        .where(MessageCreditLots.id == last.id, MessageCreditLots.low_warned_at.is_(None))
        .values(low_warned_at=at)
        .returning(MessageCreditLots.id)
        .execution_options(synchronize_session=False)
    )
    if claimed is None:
        return
    word = credits_noun(channel, left)
    title = f"{left:,} {word} left" if left else f"You've used all your {credits_noun(channel)}"
    body = (
        f"You have {left:,} bought {word} left, under {LOW_SHARE_PERCENT}% of your last pack of {int(last.quantity):,}. "
        if left else "Your bought credits are used up. "
    ) + (
        "Once this month's plan allowance and your credits are used up, announcements, campaigns and reminders "
        "pause; tickets, certificates and payment messages always go. Buy a pack to keep sending."
    )
    await _tell(session, club_id, "message_credits_low", title, body,
                data={"channel": channel, "left": left, "last_pack": int(last.quantity)},
                dedupe_key=f"credits:low:{last.id}")


async def _tell(session, club_id: int, kind: str, title: str, body: str, *, data: dict, dedupe_key: str,
                mail: bool = True) -> None:
    """The bell (with a "Buy credits" link) and, after the commit, a mail to the owner."""
    from app.api import club_alerts
    from app.api.clubs.models import Clubs
    from app.api.subscriptions import notices
    from app.core.after_commit import run_after_commit

    made = await club_alerts.notify(
        session, club_id, kind, title=title, body=body, link=BILLING_LINK,
        data={**data, "action_label": "Buy credits"}, dedupe_key=dedupe_key,
    )
    if made is None or not mail:
        return
    to = await notices.owner_address(session, club_id)
    if not to:
        return
    club = await session.get(Clubs, club_id)
    name = club.name if club is not None else "there"
    run_after_commit(
        session, notices._send, to, title, [f"Hi {name},", body], ["Buy credits", credits_url()], None,
    )


#: Where the dashboard's Credits are.
BILLING_LINK = "/billing#credits"


def credits_url() -> str:
    return f"{settings.PUBLIC_WEB_URL.rstrip('/')}{BILLING_LINK}"


async def warn_expiring(session, now: Optional[datetime] = None) -> int:
    """The sweep: each lot with credits left that expires within
    EXPIRY_NOTICE, told once. Returns how many were told."""
    now = now or _now()
    ids = list(
        await session.scalars(
            select(MessageCreditLots.id)
            .where(
                MessageCreditLots.remaining > 0,
                MessageCreditLots.refunded_at.is_(None),
                MessageCreditLots.expiry_warned_at.is_(None),
                MessageCreditLots.expires_at > now,
                MessageCreditLots.expires_at <= now + EXPIRY_NOTICE,
            )
            .order_by(MessageCreditLots.expires_at)
            .limit(BATCH)
        )
    )
    told = 0
    for lot_id in ids:
        try:
            lot = (await session.execute(
                update(MessageCreditLots)
                .where(MessageCreditLots.id == lot_id, MessageCreditLots.expiry_warned_at.is_(None))
                .values(expiry_warned_at=now)
                .returning(
                    MessageCreditLots.id, MessageCreditLots.club_id, MessageCreditLots.channel,
                    MessageCreditLots.remaining, MessageCreditLots.quantity, MessageCreditLots.bought_at,
                    MessageCreditLots.expires_at,
                )
                .execution_options(synchronize_session=False)
            )).first()
            if lot is not None:
                left = int(lot.remaining)
                when = f"{lot.expires_at.astimezone(service.IST):%d %B %Y}"
                await _tell(
                    session, lot.club_id, "message_credits_expiring",
                    f"{left:,} {credits_noun(lot.channel, left)} expire on {when}",
                    f"{left:,} of the {pack_words(lot.channel, int(lot.quantity))} you had on "
                    f"{lot.bought_at.astimezone(service.IST):%d %B %Y} are unused and expire on {when}. "
                    "Sends past your plan's allowance use the credits that expire first.",
                    data={"channel": lot.channel, "left": left, "lot_id": lot.id, "expires_at": lot.expires_at.isoformat()},
                    dedupe_key=f"credits:expiring:{lot.id}",
                )
                told += 1
            await session.commit()
        except Exception:  # noqa: BLE001
            logger.exception("message credits: the expiry notice for lot %s failed", lot_id)
            await session.rollback()
    return told


# ─── Grants ──────────────────────────────────────────────────────────────


async def grant(session, club, channel: str, quantity: int, *, reason: str, valid_months: int = VALIDITY_MONTHS,
                admin=None, request=None) -> MessageCreditLots:
    """Free credits from MyOtherApp: a lot of their own, audited with the
    admin's reason. The club is told in its bell. Flushes; the caller commits."""
    from app.api import club_alerts
    from app.api.audit import service as audit

    now = _now()
    lot = MessageCreditLots(
        club_id=club.id, channel=_channel(channel), source=LotSource.GRANT, quantity=quantity, remaining=quantity,
        bought_at=now, expires_at=add_months(now, valid_months), reason=reason, granted_by=getattr(admin, "id", None),
    )
    session.add(lot)
    await session.flush()
    await audit.record(
        session, actor=admin, action="messages.credits_granted", entity_type="club", entity_id=club.id,
        summary=f"Gave {club.name} {pack_words(channel, quantity)} for {valid_months} months: {reason}",
        details={"lot_id": lot.id, "channel": channel, "quantity": quantity, "valid_months": valid_months,
                 "reason": reason, "expires_at": lot.expires_at.isoformat()},
        request=request,
    )
    await club_alerts.notify(
        session, club.id, "message_credits_added", title=f"MyOtherApp added {pack_words(channel, quantity)}",
        body=f"Free, valid until {lot.expires_at.astimezone(service.IST):%d %B %Y}. They're used once this month's "
             "plan allowance is.",
        link=BILLING_LINK, data={"channel": channel, "quantity": quantity, "lot_id": lot.id},
        dedupe_key=f"credits:granted:{lot.id}",
    )
    return lot


# ─── Buying ──────────────────────────────────────────────────────────────


def _client():
    from app.api.payments import service as payments

    return payments.razorpay_client


async def packs(session, *, on_sale: bool = True) -> list[MessageCreditPacks]:
    query = select(MessageCreditPacks)
    if on_sale:
        query = query.where(MessageCreditPacks.active.is_(True))
    return list(
        await session.scalars(
            query.order_by(
                (MessageCreditPacks.channel != service.EMAIL), MessageCreditPacks.sort_order,
                MessageCreditPacks.quantity, MessageCreditPacks.id,
            )
        )
    )


def can_buy() -> bool:
    return bool((settings.RAZORPAY_KEY_ID or "").strip())


async def start_purchase(session, club, pack_id: int, user) -> dict:
    """The Razorpay order for one pack, and the purchase that waits for its
    payment. What the dashboard opens Razorpay's checkout with."""
    from app.api.payments import rzp

    if not can_buy():
        raise CustomHTTPException(503, "Buying credits isn't available right now.", error_code="CHECKOUT_UNAVAILABLE")
    pack = await session.get(MessageCreditPacks, pack_id)
    if pack is None or not pack.active:
        raise CustomHTTPException(404, "That pack isn't on sale any more.", error_code="PACK_NOT_FOUND")
    channel, quantity, amount, pack_key = pack.channel, int(pack.quantity), int(pack.price_paise), pack.id
    club_id, club_name = club.id, club.name
    prefill = {"email": club.contact_email or getattr(user, "email", None), "contact": club.contact_phone, "name": club_name}
    user_id = getattr(user, "id", None)
    # Nothing held while Razorpay answers.
    await session.commit()
    try:
        order = await rzp.call(
            _client().order.create,
            data={
                "amount": amount,
                "currency": "INR",
                "receipt": f"credits-{club_id}-{secrets.token_hex(6)}",
                "notes": {"kind": "message_credits", "club_id": str(club_id), "pack_id": str(pack_key),
                          "channel": channel, "quantity": str(quantity)},
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("club %s: Razorpay refused a credits order: %s", club_id, exc)
        raise CustomHTTPException(502, "Razorpay couldn't start the checkout. Try again in a minute.",
                                  error_code="RAZORPAY_ERROR") from None
    order_id = str((order or {}).get("id") or "")
    if not order_id:
        raise CustomHTTPException(502, "Razorpay couldn't start the checkout. Try again in a minute.", error_code="RAZORPAY_ERROR")
    purchase = MessageCreditPurchases(
        club_id=club_id, pack_id=pack_key, channel=channel, quantity=quantity, amount_paise=amount, currency="INR",
        status=P.CREATED, razorpay_order_id=order_id, created_by=user_id,
    )
    session.add(purchase)
    await session.commit()
    return {
        "purchase_id": purchase.id,
        "razorpay_order_id": order_id,
        "amount_paise": amount,
        "currency": "INR",
        "key_id": settings.RAZORPAY_KEY_ID,
        "name": "MyOtherApp",
        "description": pack_words(channel, quantity),
        "receipt_number": receipt_number(purchase.id),
        "prefill": prefill,
    }


def signature_ok(order_id: str, payment_id: str, signature: str) -> bool:
    """Checkout's signature: HMAC-SHA256 of "order|payment" with the key secret."""
    expected = hmac.new(
        (settings.RAZORPAY_KEY_SECRET or "").encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, (signature or "").strip())


async def _locked(session, purchase_id: int) -> Optional[MessageCreditPurchases]:
    return await session.scalar(
        select(MessageCreditPurchases)
        .where(MessageCreditPurchases.id == purchase_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _mismatch() -> CustomHTTPException:
    return CustomHTTPException(400, "This payment does not belong to that checkout.", error_code="PAYMENT_ORDER_MISMATCH")


async def apply_payment(session, purchase_id: int, details: dict, *, now: Optional[datetime] = None) -> MessageCreditPurchases:
    """Razorpay's word on a payment for this purchase. Captured, for the
    order's amount: the purchase is paid and its lot made, once. Anything
    else (authorised, failed) leaves it waiting. Commits."""
    from app.api.audit import service as audit

    now = now or _now()
    purchase = await _locked(session, purchase_id)
    if purchase is None:
        raise CustomHTTPException(404, "Purchase not found", error_code="PURCHASE_NOT_FOUND")
    if details.get("order_id") != purchase.razorpay_order_id:
        raise _mismatch()
    payment_id = str(details.get("id") or "")
    if purchase.status != P.CREATED:
        if payment_id and purchase.razorpay_payment_id and payment_id != purchase.razorpay_payment_id \
                and details.get("status") == "captured":
            logger.error("credits purchase %s: a second captured payment %s on its order", purchase.id, payment_id)
        await session.commit()
        return purchase
    if details.get("status") != "captured":
        await session.commit()
        return purchase
    if int(details.get("amount") or 0) < int(purchase.amount_paise):
        logger.error("credits purchase %s: payment %s captured %s of %s paise; not applied",
                     purchase.id, payment_id, details.get("amount"), purchase.amount_paise)
        await session.commit()
        return purchase
    purchase.status = P.PAID
    purchase.razorpay_payment_id = payment_id or None
    purchase.payment_method = (details.get("method") or None) and str(details.get("method"))[:40]
    purchase.paid_at = now
    await session.execute(
        pg_insert(MessageCreditLots)
        .values(
            club_id=purchase.club_id, channel=purchase.channel, source=LotSource.PURCHASE,
            quantity=purchase.quantity, remaining=purchase.quantity, bought_at=now,
            expires_at=add_months(now, VALIDITY_MONTHS), purchase_id=purchase.id, created_at=now, updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=["purchase_id"])
    )
    await audit.record(
        session, actor=None, action="messages.credits_bought", entity_type="club", entity_id=purchase.club_id,
        summary=f"Bought {pack_words(purchase.channel, purchase.quantity)} for {service.rupees(purchase.amount_paise)}",
        details={"purchase_id": purchase.id, "receipt": receipt_number(purchase.id), "channel": purchase.channel,
                 "quantity": purchase.quantity, "amount_paise": purchase.amount_paise,
                 "razorpay_order_id": purchase.razorpay_order_id, "razorpay_payment_id": payment_id},
    )
    await _mail_receipt(session, purchase)
    await session.commit()
    return purchase


async def _mail_receipt(session, purchase: MessageCreditPurchases) -> None:
    """The receipt, to the club's owner, after the commit."""
    from app.api.clubs.models import Clubs
    from app.api.subscriptions import notices
    from app.core.after_commit import run_after_commit

    try:
        to = await notices.owner_address(session, purchase.club_id)
        if not to:
            return
        club = await session.get(Clubs, purchase.club_id)
        lot_expires = add_months(purchase.paid_at, VALIDITY_MONTHS)
        lines = [
            f"Hi {club.name if club is not None else 'there'},",
            f"Thanks: {pack_words(purchase.channel, purchase.quantity)} are now on your club, valid until "
            f"{lot_expires.astimezone(service.IST):%d %B %Y}.",
            f"Receipt {receipt_number(purchase.id)} · {service.rupees(purchase.amount_paise)} (GST included) · "
            f"Razorpay payment {purchase.razorpay_payment_id}.",
            "Each month your plan's allowance is used first, then your credits, the ones that expire first first.",
        ]
        run_after_commit(
            session, notices._send, to, f"Receipt {receipt_number(purchase.id)}: {pack_words(purchase.channel, purchase.quantity)}",
            lines, ["See your credits", credits_url()], None,
        )
    except Exception:  # noqa: BLE001
        logger.warning("credits purchase %s: the receipt mail could not be queued", purchase.id, exc_info=True)


async def verify(session, club_id: int, order_id: str, payment_id: str, signature: Optional[str]) -> MessageCreditPurchases:
    """The dashboard's word that checkout finished: checked against the
    signature, then Razorpay is asked about the payment, and that is what
    is applied. Safe to call again, and alongside the webhook."""
    from razorpay.errors import BadRequestError

    from app.api.payments import rzp

    purchase = await session.scalar(
        select(MessageCreditPurchases).where(
            MessageCreditPurchases.razorpay_order_id == order_id, MessageCreditPurchases.club_id == club_id
        )
    )
    if purchase is None:
        raise CustomHTTPException(404, "Purchase not found", error_code="PURCHASE_NOT_FOUND")
    if signature is not None and not signature_ok(order_id, payment_id, signature):
        raise CustomHTTPException(400, "Razorpay's signature on this payment doesn't match.", error_code="BAD_SIGNATURE")
    if purchase.status != P.CREATED:
        if purchase.razorpay_payment_id not in (None, payment_id):
            raise _mismatch()
        return purchase
    purchase_id = purchase.id
    await session.commit()
    try:
        details = await rzp.call(_client().payment.fetch, payment_id) or {}
    except BadRequestError:
        raise CustomHTTPException(404, "Payment not found", error_code="PAYMENT_NOT_FOUND") from None
    if details.get("order_id") != order_id:
        raise _mismatch()
    return await apply_payment(session, purchase_id, details)


#: The platform webhook's payment events that may finish a purchase.
PAYMENT_EVENTS = ("payment.captured", "payment.authorized", "order.paid", "payment.failed")


async def handle_platform_event(session, event_name: str, data: dict) -> bool:
    """A platform webhook event about a purchase (True: handled here, the
    caller does nothing more). Only a hint: Razorpay is asked about the
    payment, and that is applied."""
    from app.api.payments import rzp

    payload = data.get("payload") or {}
    if event_name == "refund.processed":
        refund = (payload.get("refund") or {}).get("entity") or {}
        payment_id = refund.get("payment_id")
        if not payment_id:
            return False
        purchase = await session.scalar(
            select(MessageCreditPurchases).where(MessageCreditPurchases.razorpay_payment_id == payment_id)
        )
        if purchase is None:
            return False
        purchase_id = purchase.id
        await session.commit()
        fetched = await rzp.call(_client().payment.fetch, payment_id) or {}
        await _refunded_at_razorpay(session, purchase_id, int(fetched.get("amount_refunded") or 0),
                                    refund_id=refund.get("id"), outside=True)
        return True
    if event_name not in PAYMENT_EVENTS:
        return False
    payment = (payload.get("payment") or {}).get("entity") or {}
    order_id = payment.get("order_id") or ((payload.get("order") or {}).get("entity") or {}).get("id")
    if not order_id:
        return False
    purchase = await session.scalar(
        select(MessageCreditPurchases).where(MessageCreditPurchases.razorpay_order_id == order_id)
    )
    if purchase is None:
        return False
    purchase_id, payment_id = purchase.id, payment.get("id")
    await session.commit()
    if event_name == "payment.failed" or not payment_id:
        return True
    fetched = await rzp.call(_client().payment.fetch, payment_id) or {}
    if fetched.get("order_id") != order_id:
        logger.warning("Platform webhook: Razorpay says payment %s is not for credits order %s", payment_id, order_id)
        return True
    await apply_payment(session, purchase_id, fetched)
    return True


async def reconcile(session, now: Optional[datetime] = None) -> int:
    """The sweep: checkouts still open after RECONCILE_AFTER (and opened
    within RECONCILE_WITHIN), asked of Razorpay for a captured payment the
    webhook never brought. Returns how many were paid now."""
    from app.api.payments import rzp

    now = now or _now()
    open_ones = list(
        (await session.execute(
            select(MessageCreditPurchases.id, MessageCreditPurchases.razorpay_order_id)
            .where(
                MessageCreditPurchases.status == P.CREATED,
                MessageCreditPurchases.created_at < now - RECONCILE_AFTER,
                MessageCreditPurchases.created_at > now - RECONCILE_WITHIN,
            )
            .order_by(MessageCreditPurchases.id)
            .limit(BATCH)
        )).all()
    )
    await session.commit()
    paid = 0
    for purchase_id, order_id in open_ones:
        try:
            answer = await rzp.call(_client().order.payments, order_id) or {}
            captured = [p for p in (answer.get("items") or []) if p.get("status") == "captured"]
            if not captured:
                continue
            row = await apply_payment(session, purchase_id, captured[0], now=now)
            paid += int(row.status == P.PAID)
        except Exception:  # noqa: BLE001
            logger.exception("message credits: reconciling purchase %s failed", purchase_id)
            await session.rollback()
    return paid


# ─── Refunds ─────────────────────────────────────────────────────────────


def default_refund(purchase: MessageCreditPurchases, unused: int) -> int:
    """The unused share of what was paid, rounded down to the paisa."""
    if purchase.quantity <= 0:
        return 0
    share = math.floor(int(purchase.amount_paise) * max(0, unused) / int(purchase.quantity))
    return min(share, int(purchase.amount_paise) - int(purchase.refunded_paise or 0))


async def _lot_of(session, purchase_id: int, *, lock: bool = False) -> Optional[MessageCreditLots]:
    query = select(MessageCreditLots).where(MessageCreditLots.purchase_id == purchase_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return await session.scalar(query)


async def _take_back(session, purchase_id: int, now: datetime) -> int:
    """The purchase's lot's unused credits, taken back. Returns how many."""
    lot = await _lot_of(session, purchase_id, lock=True)
    if lot is None or lot.refunded_at is not None:
        return 0
    unused = int(lot.remaining) if lot.expires_at > now else 0
    lot.remaining = 0
    lot.refunded_at = now
    return unused


async def refund(session, purchase_id: int, *, reason: str, amount_paise: Optional[int] = None, admin=None,
                 request=None) -> MessageCreditPurchases:
    """An admin's refund of a purchase: its unused credits go now, then
    Razorpay gives back `amount_paise` (default: the unused share of what
    was paid). A `refund_failed` one is asked again, after checking
    Razorpay's own figure, so the money is never given twice."""
    from razorpay.errors import BadRequestError

    from app.api.audit import service as audit
    from app.api.payments import rzp

    now = _now()
    purchase = await _locked(session, purchase_id)
    if purchase is None:
        raise CustomHTTPException(404, "Purchase not found", error_code="PURCHASE_NOT_FOUND")
    if purchase.status == P.REFUNDING and purchase.refund_requested_at and now - purchase.refund_requested_at < REFUND_IN_FLIGHT:
        # Another admin's refund is being asked of Razorpay right now.
        raise CustomHTTPException(409, "This refund is being made right now. Look again in a minute.",
                                  error_code="REFUND_IN_PROGRESS")
    retrying = purchase.status in (P.REFUND_FAILED, P.REFUNDING)
    if not retrying:
        if purchase.status != P.PAID or int(purchase.refunded_paise or 0) >= int(purchase.amount_paise):
            raise CustomHTTPException(409, "Only a paid purchase that hasn't been refunded can be refunded.",
                                      error_code="NOT_REFUNDABLE")
        lot = await _lot_of(session, purchase.id)
        unused = int(lot.remaining) if lot is not None and lot.refunded_at is None and lot.expires_at > now else 0
        amount = default_refund(purchase, unused) if amount_paise is None else int(amount_paise)
        refundable = int(purchase.amount_paise) - int(purchase.refunded_paise or 0)
        if amount <= 0 or amount > refundable:
            raise CustomHTTPException(
                422,
                "Nothing to give back by default: none of its credits are unused. Give an amount to refund."
                if amount_paise is None else f"The amount must be more than ₹0 and at most {service.rupees(refundable)}.",
                error_code="BAD_AMOUNT",
            )
        taken = await _take_back(session, purchase.id, now)
        purchase.status = P.REFUNDING
        purchase.refund_amount_paise = amount
        purchase.refund_reason = reason
        purchase.refund_requested_at = now
        purchase.refunded_by = getattr(admin, "id", None)
        purchase.refund_error = None
        await audit.record(
            session, actor=admin, action="messages.credits_refunded", entity_type="club", entity_id=purchase.club_id,
            summary=f"Refunding {service.rupees(amount)} of {receipt_number(purchase.id)} "
                    f"({pack_words(purchase.channel, purchase.quantity)}); {taken:,} unused credits taken back",
            details={"purchase_id": purchase.id, "amount_paise": amount, "credits_taken_back": taken, "reason": reason,
                     "razorpay_payment_id": purchase.razorpay_payment_id},
            request=request,
        )
    else:
        purchase.refund_requested_at = now
        if reason and not purchase.refund_reason:
            purchase.refund_reason = reason
    payment_id, before, amount = purchase.razorpay_payment_id, int(purchase.refunded_paise or 0), int(purchase.refund_amount_paise or 0)
    purchase.status = P.REFUNDING
    # The credits are gone and the ask is on disk before Razorpay is asked.
    await session.commit()

    async def razorpay_has_it() -> Optional[bool]:
        try:
            fetched = await rzp.call(_client().payment.fetch, payment_id) or {}
        except Exception:  # noqa: BLE001
            return None
        return int(fetched.get("amount_refunded") or 0) >= before + amount

    if retrying and await razorpay_has_it():
        return await _refunded(session, purchase_id, before + amount, refund_id=None, now=now)
    try:
        answer = await rzp.call(
            _client().payment.refund, payment_id,
            {"amount": amount, "notes": {"kind": "message_credits", "purchase_id": str(purchase_id),
                                         "receipt": receipt_number(purchase_id)}},
        )
    except BadRequestError as exc:
        await _refund_failed(session, purchase_id, f"Razorpay refused the refund: {str(exc)[:200]}")
        raise CustomHTTPException(502, f"Razorpay refused the refund: {str(exc)[:200]}", error_code="RAZORPAY_ERROR") from None
    except Exception as exc:  # noqa: BLE001 - a timeout: it may have been made
        if await razorpay_has_it():
            return await _refunded(session, purchase_id, before + amount, refund_id=None, now=now)
        await _refund_failed(session, purchase_id, f"Razorpay did not answer ({exc.__class__.__name__}); ask again to retry")
        raise CustomHTTPException(502, "Razorpay did not answer. Ask again: it is checked before anything is refunded twice.",
                                  error_code="RAZORPAY_ERROR") from None
    return await _refunded(session, purchase_id, before + amount,
                           refund_id=(answer or {}).get("id") if isinstance(answer, dict) else None, now=now)


async def _refund_failed(session, purchase_id: int, error: str) -> None:
    purchase = await _locked(session, purchase_id)
    if purchase is not None and purchase.status == P.REFUNDING:
        purchase.status = P.REFUND_FAILED
        purchase.refund_error = error[:300]
    await session.commit()


async def _refunded(session, purchase_id: int, total: int, *, refund_id: Optional[str], now: datetime) -> MessageCreditPurchases:
    purchase = await _locked(session, purchase_id)
    purchase.refunded_paise = min(int(purchase.amount_paise), max(int(purchase.refunded_paise or 0), total))
    purchase.status = P.REFUNDED
    purchase.refunded_at = purchase.refunded_at or now
    purchase.refund_amount_paise = None
    purchase.refund_error = None
    if refund_id:
        purchase.razorpay_refund_id = str(refund_id)[:64]
    await session.commit()
    return purchase


async def _refunded_at_razorpay(session, purchase_id: int, total: int, *, refund_id: Optional[str], outside: bool) -> None:
    """`refund.processed`: Razorpay's running total of what was refunded on
    the payment. One made in Razorpay's dashboard (the purchase still
    `paid`) takes the unused credits back too."""
    from app.api.audit import service as audit

    now = _now()
    purchase = await _locked(session, purchase_id)
    if purchase is None or total <= 0:
        await session.commit()
        return
    if purchase.status == P.PAID and outside:
        taken = await _take_back(session, purchase.id, now)
        await audit.record(
            session, actor=None, action="messages.credits_refunded", entity_type="club", entity_id=purchase.club_id,
            summary=f"{receipt_number(purchase.id)} was refunded in Razorpay; {taken:,} unused credits taken back",
            details={"purchase_id": purchase.id, "refunded_paise": total, "credits_taken_back": taken,
                     "razorpay_refund_id": refund_id},
        )
    if purchase.status in (P.PAID, P.REFUNDING, P.REFUND_FAILED, P.REFUNDED):
        purchase.refunded_paise = min(int(purchase.amount_paise), max(int(purchase.refunded_paise or 0), total))
        purchase.status = P.REFUNDED
        purchase.refunded_at = purchase.refunded_at or now
        purchase.refund_amount_paise = None
        purchase.refund_error = None
        if refund_id and not purchase.razorpay_refund_id:
            purchase.razorpay_refund_id = str(refund_id)[:64]
    await session.commit()


# ─── The sweep ───────────────────────────────────────────────────────────


async def run(session, now: Optional[datetime] = None) -> dict:
    """The hourly billing sweep's credit steps."""
    now = now or _now()
    out = {"credits_expiry_told": 0, "credits_reconciled": 0}
    try:
        out["credits_expiry_told"] = await warn_expiring(session, now)
    except Exception:  # noqa: BLE001
        logger.exception("message credits: the expiry notices failed")
        await session.rollback()
    try:
        out["credits_reconciled"] = await reconcile(session, now)
    except Exception:  # noqa: BLE001
        logger.exception("message credits: reconciling checkouts failed")
        await session.rollback()
    return out


# ─── What the dashboards read ────────────────────────────────────────────


def pack_out(pack: MessageCreditPacks, *, admin: bool = False, sold: int = 0) -> dict:
    out = {"id": pack.id, "channel": pack.channel, "quantity": int(pack.quantity), "price_paise": int(pack.price_paise),
           "sort_order": int(pack.sort_order or 0)}
    if admin:
        out.update(active=bool(pack.active), updated_at=pack.updated_at, sold=sold)
    return out


def lot_status(lot: MessageCreditLots, now: datetime) -> str:
    if lot.refunded_at is not None:
        return "refunded"
    if lot.expires_at <= now:
        return "expired"
    if lot.remaining <= 0:
        return "used"
    return "active"


def lot_out(lot: MessageCreditLots, now: datetime, *, admin: bool = False, granted_by: Optional[dict] = None) -> dict:
    out = {"id": lot.id, "channel": lot.channel, "source": lot.source, "quantity": int(lot.quantity),
           "remaining": int(lot.remaining), "bought_at": lot.bought_at, "expires_at": lot.expires_at,
           "purchase_id": lot.purchase_id, "reason": lot.reason if admin else None}
    if admin:
        out.update(status=lot_status(lot, now), granted_by=granted_by)
    return out


def purchase_out(purchase: MessageCreditPurchases, lot: Optional[MessageCreditLots], *, admin: bool = False) -> dict:
    out = {
        "id": purchase.id, "receipt_number": receipt_number(purchase.id), "channel": purchase.channel,
        "quantity": int(purchase.quantity), "amount_paise": int(purchase.amount_paise), "currency": purchase.currency,
        "status": purchase.status, "created_at": purchase.created_at, "paid_at": purchase.paid_at,
        "razorpay_order_id": purchase.razorpay_order_id, "razorpay_payment_id": purchase.razorpay_payment_id,
        "payment_method": purchase.payment_method, "refunded_paise": int(purchase.refunded_paise or 0),
        "refunded_at": purchase.refunded_at,
        "credits_remaining": int(lot.remaining) if lot is not None else None,
        "expires_at": lot.expires_at if lot is not None else None,
    }
    if admin:
        out.update(refund_reason=purchase.refund_reason, refund_error=purchase.refund_error,
                   razorpay_refund_id=purchase.razorpay_refund_id)
    return out


async def _channel_balances(session, club_id: int, now: datetime, *, with_lots: bool) -> dict:
    terms = await service.terms_for(session, club_id)
    rows = {
        r.channel: r
        for r in await session.scalars(
            select(MessageUsage).where(MessageUsage.club_id == club_id, MessageUsage.period == service.period_of(now))
        )
    }
    out = {}
    for ch in service.CHANNELS:
        limit = terms.limit(ch)
        row = rows.get(ch)
        used = int(row.plan_used or 0) if row is not None else 0
        plan_left = None if limit is None else max(0, limit - used)
        lots = await live_lots(session, club_id, ch, now)
        bought = sum(int(lot.remaining) for lot in lots)
        entry = {
            "plan": {"limit": limit, "used": used, "remaining": plan_left},
            "bought": {
                "remaining": bought,
                "next_expiry": lots[0].expires_at if lots else None,
                "low": _is_low(bought, await _last_lot(session, club_id, ch)),
            },
            "total_remaining": None if plan_left is None else plan_left + bought,
        }
        if with_lots:
            entry["lots"] = [lot_out(lot, now) for lot in lots]
        out[ch] = entry
    return out


async def _purchases(session, club_id: int, *, paid_only: bool, limit: int = 50) -> list[tuple]:
    query = select(MessageCreditPurchases, MessageCreditLots).outerjoin(
        MessageCreditLots, MessageCreditLots.purchase_id == MessageCreditPurchases.id
    ).where(MessageCreditPurchases.club_id == club_id)
    if paid_only:
        query = query.where(MessageCreditPurchases.status.in_(P.PAID_ONES))
    return list((await session.execute(query.order_by(MessageCreditPurchases.id.desc()).limit(limit))).all())


async def club_view(session, club_id: int, now: Optional[datetime] = None) -> dict:
    """GET /clubs/messages/credits."""
    now = now or _now()
    period = service.period_of(now)
    return {
        "period": service.key(period),
        "period_label": service.label(period),
        "prices_include_gst": PRICES_INCLUDE_GST,
        "validity_months": VALIDITY_MONTHS,
        "can_buy": can_buy(),
        "channels": await _channel_balances(session, club_id, now, with_lots=True),
        "packs": [pack_out(p) for p in await packs(session)],
        "purchases": [purchase_out(p, lot) for p, lot in await _purchases(session, club_id, paid_only=True)],
    }


async def admin_view(session, club, now: Optional[datetime] = None) -> dict:
    """GET /admin/messages/clubs/{id}/credits: every lot and purchase."""
    from app.api.users.models import Users

    now = now or _now()
    lots = list(
        await session.scalars(
            select(MessageCreditLots).where(MessageCreditLots.club_id == club.id)
            .order_by(MessageCreditLots.bought_at.desc(), MessageCreditLots.id.desc()).limit(200)
        )
    )
    admin_ids = {lot.granted_by for lot in lots if lot.granted_by}
    names = {}
    if admin_ids:
        for uid, full_name, email in (await session.execute(
            select(Users.id, Users.full_name, Users.email).where(Users.id.in_(admin_ids))
        )).all():
            names[uid] = {"id": uid, "name": full_name or email}
    return {
        "club_id": club.id,
        "club_name": club.name,
        "channels": await _channel_balances(session, club.id, now, with_lots=False),
        "lots": [lot_out(lot, now, admin=True, granted_by=names.get(lot.granted_by)) for lot in lots],
        "purchases": [purchase_out(p, lot, admin=True) for p, lot in await _purchases(session, club.id, paid_only=False, limit=200)],
    }


async def receipt(session, club, purchase_id: int) -> dict:
    """GET /clubs/messages/credits/purchases/{id}/receipt."""
    from app.api.subscriptions import notices

    purchase = await session.get(MessageCreditPurchases, purchase_id)
    if purchase is None or purchase.club_id != club.id or purchase.status not in P.PAID_ONES:
        raise CustomHTTPException(404, "Purchase not found", error_code="PURCHASE_NOT_FOUND")
    lot = await _lot_of(session, purchase.id)
    return {
        "receipt_number": receipt_number(purchase.id),
        "issued_at": purchase.paid_at,
        "status": purchase.status,
        "seller": {"name": settings.MAIL_FROM_NAME or "MyOtherApp", "website": settings.PUBLIC_SITE_URL,
                   "email": settings.MAIL_FROM_ADDRESS},
        "club": {"id": club.id, "name": club.name, "email": await notices.owner_address(session, club.id)},
        "item": f"{pack_words(purchase.channel, purchase.quantity)}, valid {VALIDITY_MONTHS} months",
        "channel": purchase.channel,
        "quantity": int(purchase.quantity),
        "amount_paise": int(purchase.amount_paise),
        "currency": purchase.currency,
        "prices_include_gst": PRICES_INCLUDE_GST,
        "razorpay_payment_id": purchase.razorpay_payment_id,
        "payment_method": purchase.payment_method,
        "refunded_paise": int(purchase.refunded_paise or 0),
        "refunded_at": purchase.refunded_at,
        "expires_at": lot.expires_at if lot is not None else None,
    }
