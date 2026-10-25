from datetime import datetime, timedelta, timezone
import io
import logging
from typing import Optional
from fastapi import Request, UploadFile
from sqlalchemy import and_, delete, exists, select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, joinedload
from sqlalchemy.dialects.postgresql import INTERVAL

from app.response import CustomHTTPException
from app.api.events.access import ensure_event_access
from app.api.events.schemas import (
    EventAdditionalDetail,
    EventCategoryCreate,
    EventEdit,
    EventSpeakerCreate,
)
from app.api.events.models import (
    MAX_EVENT_HOURS,
    EventCategories,
    EventFiles,
    EventInterestsLink,
    EventRegistrationsLink,
    Events,
    EventRatingsLink,
    EventSpeakers,
    ends_at_sql,
)
from app.api.closeout.models import LOCKED_STATUSES, EventStatus
from app.api.discover import geo
from app.api.clubs.models import ClubUsersLink, Clubs
from app.api.users.models import Users, UserProfiles
from app.core.validations.schema import validate_relations
from app.api.interests.models import Interests
from app.core import public_urls
from app.core.utils.keys import generate_slug
from app.core.notifications.triggers import (
    notify_followers_of_new_event,
    notify_users_by_interest,
)


async def _republish_site(session, event_id: int) -> None:
    """A club with a published website gets it remade when an event changes; never in the way of the save."""
    from app.api.sites import service as sites

    await sites.republish_for_event(session, event_id)


def check_duration(duration: Optional[float], *, was: Optional[float] = None) -> None:
    """422 for an event that would run longer than MAX_EVENT_HOURS. One saved
    before the limit (`was`, its length now) keeps that length through an
    edit that leaves it alone; changing it must bring it within the limit."""
    if duration is None or float(duration) <= MAX_EVENT_HOURS:
        return
    if was is not None and float(duration) == float(was):
        return
    raise CustomHTTPException(
        422, message="Invalid request",
        errors={"duration": f"An event can run for at most {MAX_EVENT_HOURS} hours. For something longer, make one event a day."},
    )


async def create_event(
    session: AsyncSession,
    user_id: int,
    name: str,
    event_datetime: datetime,
    duration: float,
    category_id: int,
    poster: UploadFile | None = None,
    has_fee: bool = False,
    reg_fee: Optional[float] = None,
    location_name: Optional[str] = None,
    location_link: Optional[str] = None,
    has_prize: bool = False,
    prize_amount: Optional[float] = None,
    is_online: bool = False,
    merch_enabled: bool = False,
    swag_enabled: bool = False,
    store_item_ids: Optional[list] = None,
    reg_startdate: datetime = datetime.now(timezone.utc),
    reg_enddate: Optional[datetime] = None,
    images: Optional[list[str]] = None,
    about: Optional[str] = None,
    contact_name: Optional[str] = None,
    contact_phone: Optional[str] = None,
    contact_email: Optional[str] = None,
    url: Optional[str] = None,
    additional_details: Optional[list[EventAdditionalDetail]] = None,
    interest_ids: Optional[list[int]] = None,
    max_participants: Optional[int] = None,
    event_guidelines: Optional[str] = None,
    event_tag: Optional[str] = None,
    ticket_template_id: Optional[int] = None,
    certificate_template_id: Optional[int] = None,

    speakers: Optional[list[EventSpeakerCreate]] = None,
    speaker_photos: Optional[list[UploadFile]] = None,
    status: Optional[str] = None,
    pin: Optional[tuple[float, float]] = None,
    *args,
    **kwargs,
):
    check_duration(duration)
    await validate_relations(
        session,
        {
            "category": (EventCategories, category_id),
            "user": (Users, user_id),
        },
    )
    user = await session.execute(
        select(Users).filter(Users.id == user_id).options(selectinload(Users.club))
    )
    user = user.scalar()

    if not user.club:
        raise CustomHTTPException(403, message="Not authorized to create event")
    for field in additional_details:
        # Same enum-or-string question as at registration time; see
        # _field_type in app/api/events/registration/service.py.
        field_type = getattr(field.field_type, "value", field.field_type)
        if field_type in ("select", "radio", "checkbox"):
            if not field.options:
                raise CustomHTTPException(
                    400, message="Options required for select, radio, checkbox fields"
                )
    slug = generate_slug(name)
    db_event = Events(
        name=name,
        slug=slug,
        event_datetime=event_datetime,
        duration=duration,
        category_id=category_id,
        has_fee=has_fee,
        reg_fee=reg_fee,
        location_name=location_name,
        has_prize=has_prize,
        prize_amount=prize_amount,
        is_online=is_online,
        merch_enabled=bool(merch_enabled),
        swag_enabled=bool(swag_enabled),
        reg_startdate=reg_startdate,
        reg_enddate=reg_enddate,
        images=images or [],
        about=about,
        contact_name=contact_name,
        contact_phone=contact_phone,
        contact_email=contact_email,
        url=url,
        club_id=user.club.id,
        max_participants=max_participants,
        additional_details=[x.model_dump(mode="json") for x in additional_details],
        event_guidelines=event_guidelines,
        event_tag=event_tag,
        ticket_template_id=ticket_template_id,
        certificate_template_id=certificate_template_id,
    )
    if status:
        from app.api.closeout.models import EventStatus

        db_event.lifecycle = EventStatus(status)
    # The venue's point: the pin when one came, else read from its map link
    # now when the link holds one; a short link is followed, or the venue's
    # words geocoded, after the save (see the router).
    geo.apply_place(db_event, link=location_link, manual=pin)
    if poster:
        content = io.BytesIO(await poster.read())
        db_event.poster = {
            "bytes": content,
            "filename": poster.filename,
        }
    session.add(db_event)
    await session.commit()
    await session.refresh(db_event)
    event_id = db_event.id

    if store_item_ids:
        # Checked by the caller (store.service.check_offers): the club's items, within the plan.
        from app.api.store import service as store

        await store.write_offers(session, event_id, store_item_ids)
        await session.commit()

    if interest_ids:
        for interest_id in interest_ids:
            interest_exists = await session.scalar(
                select(exists().where(Interests.id == interest_id))
            )
            if interest_exists:
                link = EventInterestsLink(event_id=event_id, interest_id=interest_id)
                session.add(link)

        await session.commit()
    
    # Add speakers/guests if provided
    if speakers:
        for idx, speaker in enumerate(speakers):
            db_speaker = EventSpeakers(
                event_id=event_id,
                name=speaker.name,
                designation=speaker.designation,
                bio=speaker.bio,
                link=speaker.link,
                display_order=idx,
            )
            
            # Handle photo upload
            # Handle photo upload
            if speaker.photo_index is not None and speaker_photos:
                try:
                    p_index = int(speaker.photo_index)
                    if len(speaker_photos) > p_index:
                        photo_file = speaker_photos[p_index]
                        await photo_file.seek(0)
                        content = io.BytesIO(await photo_file.read())
                        db_speaker.photo = {
                            "bytes": content,
                            "filename": photo_file.filename,
                        }
                except (ValueError, TypeError):
                    pass
            elif speaker.photo_url:
                # If photo_url is provided (e.g. from existing speaker), we might want to handle it.
                # But S3ImageField expects upload. If it's a string, it might be ignored or cause issues depending on implementation.
                # Assuming for now we only handle new uploads or leave as None if not provided.
                pass

            session.add(db_speaker)
        await session.commit()
    
    db_event = await session.execute(
        select(Events)
        .filter(Events.id == event_id)
        .options(
            joinedload(Events.category),
            joinedload(Events.club),
            selectinload(Events.interests).joinedload(Interests.category),
            selectinload(Events.speakers),
        )
        .limit(1)
    )
    created_event = db_event.scalar_one()
    
    # Send push notifications for new event
    try:
        # Trigger 1: Notify followers of the club
        await notify_followers_of_new_event(
            session=session,
            club_id=created_event.club.id,
            club_name=created_event.club.name,
            event_id=created_event.id,
            event_name=created_event.name,
        )
        
        # Trigger 2: people who picked an interest under the event's topics
        # (not its followers). By topic, not by the event's type: the type's
        # id used to be matched against topic ids, so a Workshop reached
        # whoever had picked an interest under the topic sharing its number.
        topics = created_event.topics
        if topics:
            await notify_users_by_interest(
                session=session,
                category_id=[t["id"] for t in topics],
                category_name=topics[0]["name"],
                club_id=created_event.club.id,
                event_id=created_event.id,
                event_name=created_event.name,
            )
    except Exception as e:
        # Don't fail event creation if notification fails
        import logging
        logging.getLogger(__name__).error(f"Failed to send new event notifications: {e}")

    await _republish_site(session, created_event.id)
    return created_event


async def update_event(
    session: AsyncSession, 
    event: EventEdit, 
    user_id: int, 
    event_id: int,
    speaker_photos: Optional[list[UploadFile]] = None,
    as_admin: bool = False,
    actor=None,
):
    db_event = await session.execute(
        select(Events)
        .options(joinedload(Events.club))
        .filter(Events.id == event_id, Events.is_deleted == False)
    )
    db_event = db_event.scalar()
    if db_event is None:
        raise CustomHTTPException(404, message="Event not found")
    ensure_event_access(
        db_event, user_id, as_admin, message="Not authorized to update this event"
    )
    check_duration(event.duration, was=db_event.duration)
    if (db_event.lifecycle or EventStatus.published) in LOCKED_STATUSES:
        raise CustomHTTPException(409, message="This event is closed, so its settings are read-only. Move its status back to Live on the closeout page to change them.", error_code="EVENT_LOCKED")
    # The club's plan: free to paid needs tickets; the store and swag switched
    # on need theirs. Not for a grandfathered event, judged by its new date.
    from app.api.subscriptions import gates

    await gates.check_event_update(
        session, db_event, has_fee=event.has_fee, merch_enabled=getattr(event, "merch_enabled", None),
        swag_enabled=getattr(event, "swag_enabled", None), event_datetime=event.event_datetime, actor=actor,
    )
    # The club's store items it offers, when the form sends them: checked
    # before anything changes, against the event as it will be (its new date).
    store_item_ids = None
    if getattr(event, "store_item_ids", None) is not None:
        from types import SimpleNamespace

        from app.api.store import service as store

        scope = SimpleNamespace(id=db_event.id, club_id=db_event.club_id, created_at=db_event.created_at, event_datetime=event.event_datetime)
        store_item_ids = await store.check_offers(
            session, scope, event.store_item_ids, actor=actor, current=await store.offered_ids(session, db_event),
        )

    if event.poster:
        content = io.BytesIO(await event.poster.read())
        db_event.poster = {
            "bytes": content,
            "filename": event.poster.filename,
        }

    await session.execute(
        delete(EventInterestsLink).where(EventInterestsLink.event_id == event_id)
    )
    # Every interest removed is an empty list: the links above are cleared and none added.
    for interest_id in event.interest_ids or []:
        interest_exists = await session.scalar(
            select(exists().where(Interests.id == interest_id))
        )
        if interest_exists:
            link = EventInterestsLink(event_id=event_id, interest_id=interest_id)
            session.add(link)
    db_event.name = event.name
    db_event.event_datetime = event.event_datetime
    db_event.duration = event.duration
    db_event.category_id = event.category_id
    db_event.has_fee = event.has_fee
    db_event.reg_fee = event.reg_fee
    db_event.is_online = event.is_online
    # The place, by geo.apply_place's precedence (a pin, then the map link,
    # then the venue's words). The map link: a new one moves a derived point, a
    # removal clears both, nothing sent keeps them. A pin stays until new pin
    # values come or clear_point drops it.
    if getattr(event, "clear_location_link", False):
        map_link = None
    elif getattr(event, "location_link", None) is not None:
        map_link = event.location_link
    else:
        map_link = geo.KEEP
    geo.apply_place(
        db_event, link=map_link, venue=event.location_name,
        manual=getattr(event, "pin", None), clear_point=bool(getattr(event, "clear_point", False)),
    )
    db_event.has_prize = event.has_prize
    db_event.prize_amount = event.prize_amount
    if getattr(event, "merch_enabled", None) is not None:
        db_event.merch_enabled = event.merch_enabled
    if getattr(event, "swag_enabled", None) is not None:
        db_event.swag_enabled = event.swag_enabled
    db_event.reg_startdate = event.reg_startdate
    db_event.reg_enddate = event.reg_enddate
    db_event.about = event.about
    db_event.contact_name = event.contact_name
    db_event.contact_phone = event.contact_phone
    db_event.contact_email = event.contact_email
    db_event.additional_details = [
        x.model_dump(mode="json") for x in event.additional_details
    ]
    db_event.event_guidelines = event.event_guidelines
    # The label was written on create only, so an edit to it was lost. Like
    # every field here it is assigned as sent: the form leaves an emptied one
    # out, which clears it.
    db_event.event_tag = (getattr(event, "event_tag", None) or "").strip() or None
    db_event.url = event.url
    db_event.max_participants = event.max_participants
    db_event.ticket_template_id = getattr(event, "ticket_template_id", None)
    db_event.certificate_template_id = getattr(event, "certificate_template_id", None)
    
    # Update speakers
    if event.speakers is not None:
        # Get existing speakers
        existing_speakers = await session.execute(
            select(EventSpeakers).where(EventSpeakers.event_id == event_id)
        )
        existing_speakers_map = {s.id: s for s in existing_speakers.scalars().all()}
        
        speakers_data = [
            EventSpeakerCreate(**s) if isinstance(s, dict) else s 
            for s in event.speakers
        ]
        
        # Track IDs to keep
        keep_ids = set()
        
        for idx, speaker in enumerate(speakers_data):
            if speaker.id and speaker.id in existing_speakers_map:
                # Update existing speaker
                db_speaker = existing_speakers_map[speaker.id]
                db_speaker.name = speaker.name
                db_speaker.designation = speaker.designation
                # Only when sent: a client that predates them would otherwise wipe them.
                for field in ("bio", "link"):
                    if field in speaker.model_fields_set:
                        setattr(db_speaker, field, getattr(speaker, field))
                db_speaker.display_order = idx
                keep_ids.add(speaker.id)
                
                # Update photo if provided
                if speaker.photo_index is not None and speaker_photos:
                    try:
                        p_index = int(speaker.photo_index)
                        if len(speaker_photos) > p_index:
                            photo_file = speaker_photos[p_index]
                            await photo_file.seek(0)
                            content = io.BytesIO(await photo_file.read())
                            db_speaker.photo = {
                                "bytes": content,
                                "filename": photo_file.filename,
                            }
                    except (ValueError, TypeError):
                        pass
            else:
                # Create new speaker
                db_speaker = EventSpeakers(
                    event_id=event_id,
                    name=speaker.name,
                    designation=speaker.designation,
                    bio=speaker.bio,
                    link=speaker.link,
                    display_order=idx,
                )
                
                # Handle photo upload for new speaker
                if speaker.photo_index is not None and speaker_photos:
                    try:
                        p_index = int(speaker.photo_index)
                        if len(speaker_photos) > p_index:
                            photo_file = speaker_photos[p_index]
                            await photo_file.seek(0)
                            content = io.BytesIO(await photo_file.read())
                            db_speaker.photo = {
                                "bytes": content,
                                "filename": photo_file.filename,
                            }
                    except (ValueError, TypeError):
                        pass
                
                session.add(db_speaker)
        
        # Delete removed speakers
        for speaker_id, db_speaker in existing_speakers_map.items():
            if speaker_id not in keep_ids:
                await session.delete(db_speaker)

    if store_item_ids is not None:
        await store.write_offers(session, event_id, store_item_ids)

    await session.commit()
    await _republish_site(session, event_id)
    db_event = await session.execute(
        select(Events)
        .filter(Events.id == event_id)
        .options(
            joinedload(Events.category),
            joinedload(Events.club),
            selectinload(Events.interests).joinedload(Interests.category),
        )
        .limit(1)
    )
    return db_event.scalar_one()


async def get_event(session: AsyncSession, event_id: str | int, user_id: int | None = None):
    is_event_id = (isinstance(event_id, str) and event_id.isdigit()) or isinstance(
        event_id, int
    )
    if is_event_id:
        event_id = int(event_id)
    
    # Create correlated subqueries for event-specific ratings
    rating_avg_subquery = (
        select(func.coalesce(func.avg(EventRatingsLink.rating), 0.0))
        .where(
            EventRatingsLink.event_id == Events.id,
            EventRatingsLink.is_deleted == False,
        )
        .correlate(Events)
        .scalar_subquery()
    )
    
    rating_count_subquery = (
        select(func.coalesce(func.count(EventRatingsLink.id), 0))
        .where(
            EventRatingsLink.event_id == Events.id,
            EventRatingsLink.is_deleted == False,
        )
        .correlate(Events)
        .scalar_subquery()
    )
    
    db_event = await session.execute(
        select(
            Events,
            rating_avg_subquery.label("rating"),
            rating_count_subquery.label("total_rating"),
        )
        .filter(
            Events.is_deleted == False,
            Events.id == event_id if is_event_id else Events.slug == event_id
        )
        .options(
            selectinload(Events.category),
            selectinload(Events.club),
            selectinload(Events.interests).options(joinedload(Interests.category)),
            selectinload(Events.files),  # Load event files for downloads
            selectinload(Events.speakers),
        )
    )
    db_event = db_event.first()

    if db_event is None:
        raise CustomHTTPException(404, message="Event not found")


    event = db_event[0]
    # A copy: writing the response's speakers and downloads into the instance's
    # own __dict__ swapped its speakers collection for a plain list, and the
    # next flush of that instance in the same session failed on it.
    data = dict(event.__dict__)
    data["rating"] = db_event[1]
    data["total_rating"] = db_event[2]
    data["topics"] = event.topics
    # The status clock as plain strings: `status` is a property the dict
    # cannot see, and the retention column is an enum the response reads as text.
    data["status"] = event.status
    data["retention_mode"] = getattr(event.retention_mode, "value", event.retention_mode) or "keep"
    # Whether the plan's event-scoped locks pass this event by; the dashboard
    # reads this one flag rather than working the rule out itself.
    from app.api.subscriptions import gates

    data["plan_grandfathered"] = await gates.plan_grandfathered(session, event)
    from app.api.payment_gateways import service as payment_gateways

    data["payments_to"] = await payment_gateways.payments_to(session, event.club_id)
    # The club as the response shows it, and whether the registration form
    # offers "Get updates from <club> on WhatsApp" (club_whatsapp.consent.offered).
    if event.club is not None:
        from app.api.club_whatsapp import consent

        data["club"] = {
            "id": event.club.id, "name": event.club.name, "slug": event.club.slug, "logo": event.club.logo,
            "whatsapp_updates": await consent.offered(session, event.club_id),
        }

    # Get event files (downloads) - available to all users
    data["downloads"] = [
        {"name": f.name, "url": f.file} 
        for f in event.files if not f.is_deleted
    ] if event.files else []

    # Get speakers
    data["speakers"] = [
        {
            "id": s.id,
            "name": s.name,
            "designation": s.designation,
            "photo": s.photo,
            "bio": s.bio,
            "link": s.link,
            "display_order": s.display_order
        }
        for s in event.speakers if not s.is_deleted
    ] if event.speakers else []

    # User-specific data (only if user is authenticated)
    if user_id:
        from app.api.live import service as live
        from app.api.live.models import FeedbackSurveys

        # Check registration status, and the event's feedback survey in the
        # same statement (live.SURVEY_STATE): null columns when it has none.
        row = (await session.execute(
            select(EventRegistrationsLink, *live.SURVEY_STATE)
            .outerjoin(FeedbackSurveys, FeedbackSurveys.event_id == EventRegistrationsLink.event_id)
            .where(
                EventRegistrationsLink.event_id == event.id,
                EventRegistrationsLink.user_id == user_id,
                EventRegistrationsLink.is_deleted == False,
            )
        )).first()
        registration = row[0] if row else None
        if registration:
            data["is_registered"] = True
            # What the door checks: a ticket for a paid event is refused
            # (FEE_UNPAID) until it is paid. Null for someone not registered.
            data["is_paid"] = (not event.has_fee) or bool(registration.is_paid)
            # The registration to pay for when it is not paid yet
            # (POST /payments/orders), so the app can finish a payment
            # without sending the form again.
            data["registration_id"] = str(registration.id)
            data["is_attended"] = registration.is_attended
            data["ticket_id"] = registration.ticket_id
            data["user_position"] = registration.position
            # The public site's ticket and certificate pages, by ticket id.
            data["ticket_url"] = public_urls.ticket_url(registration.ticket_id)
            # By the rule the PDF and the image follow, so the app lists and
            # opens exactly the certificates the server will hand over.
            from app.api.events.certificates import availability

            held = availability(registration)
            data["has_certificate"] = held.available
            data["certificate_revoked"] = held.reason == "revoked"
            if held.available:
                data["certificate_url"] = public_urls.certificate_url(registration.ticket_id)
            # The feedback survey, as emailed to whoever was there, for the
            # app's own survey screen: this registration's link only, and
            # only while the survey is open to it (live.feedback_url).
            data["feedback_url"] = live.feedback_url(event, registration, row if row.survey_id is not None else None)
            # People Like You, for the event page's card: on when the club's
            # plan has it. Asked only for someone registered, who can use it.
            data["networking"] = await gates.allows(session, event.club_id, "networking", event=event)
            # Position label based on position
            if registration.position:
                if registration.position == 1:
                    data["position_label"] = "Winner"
                elif registration.position == 2:
                    data["position_label"] = "First Runner Up"
                elif registration.position == 3:
                    data["position_label"] = "Second Runner Up"
                else:
                    data["position_label"] = f"{registration.position}th Place"
        
        # Check user's rating for this event
        user_rating = await session.scalar(
            select(EventRatingsLink).where(
                EventRatingsLink.event_id == event.id,
                EventRatingsLink.user_id == user_id,
                EventRatingsLink.is_deleted == False,
            )
        )
        if user_rating:
            data["user_rating"] = user_rating.rating

    return data


async def list_events(
    session: AsyncSession,
    user_id: int | None = None,
    limit: int = 10,
    offset: int = 0,
    is_following: bool | None = None,
    is_registered: bool | None = None,
    is_ended: bool | None = None,
    interest_ids: list[int] | None = None,
    search: str | None = None,
):
    """List events with filters and search."""

    # Base query - filter out soft-deleted events. Narrow: the page is chosen
    # on ids and dates, then only its events are read in full (below), rather
    # than sorting thousands of whole rows with their club and category.
    query = select(Events.id).where(Events.is_deleted == False)

    # Words: every word somewhere the event is found by (its name, place,
    # club, category, interests and topics), as discover's search matches
    # them: one trigram-indexed column, events.search_text. With Meilisearch
    # on and well, a search here with nothing of the person's own to filter
    # by asks it instead (app.api.search.engine), with the same rules.
    if search and search.strip():
        from app.api.discover import service as discover
        from app.api.search import engine
        from app.api.search import text as stext

        if is_following is None and is_registered is None and offset + limit <= engine.MAX_HITS:
            found = await engine.list_events(session, search, limit=limit, offset=offset, is_ended=is_ended, interest_ids=interest_ids)
            if found is not None:
                return found
        query = query.filter(discover._event_words(await stext.normalise(session, search)))

    # Filter by interests: an event with any of them, once. A semi-join
    # (EXISTS) rather than a join and DISTINCT: DISTINCT over every column of
    # events cannot compare additional_details (json[]), which made this
    # filter fail outright, and it would sort every row to de-duplicate.
    if interest_ids:
        query = query.filter(
            exists(
                select(EventInterestsLink.id).where(
                    EventInterestsLink.event_id == Events.id,
                    EventInterestsLink.interest_id.in_(interest_ids),
                    EventInterestsLink.is_deleted == False,
                )
            )
        )

    # Filter by following clubs
    if is_following and user_id:
        query = (
            query.join(Clubs)
            .join(ClubUsersLink)
            .filter(
                ClubUsersLink.user_id == user_id,
                ClubUsersLink.is_following == True,
                ClubUsersLink.is_deleted == False,
            )
        )

    # Filter by registration status
    if is_registered is not None and user_id:
        if is_registered:
            query = query.join(EventRegistrationsLink).filter(
                EventRegistrationsLink.user_id == user_id,
                EventRegistrationsLink.is_deleted == False,
            )
        else:
            query = query.outerjoin(
                EventRegistrationsLink,
                and_(
                    EventRegistrationsLink.event_id == Events.id,
                    EventRegistrationsLink.user_id == user_id,
                    EventRegistrationsLink.is_deleted == False,
                ),
            ).filter(EventRegistrationsLink.id == None)

    # Archived events leave the public lists; a guest's own registrations still
    # show them. So do drafts and the events of clubs the public may not see
    # (unverified, deleted, under a blocked organisation): every public rule
    # is clubs.listed, as on the discover site.
    if not is_registered:
        query = query.filter(
            Events.lifecycle.notin_((EventStatus.archived, EventStatus.draft)),
            Events.club_id.isnot(None),
            Events.club_id.notin_(select(Clubs.id).where(Clubs.listed == False)),  # noqa: E712
        )

    # event_end_time = event_datetime + duration hours
    if is_ended is not None:
        # event_end_time = event_datetime + duration * 1 hour, in the form
        # ix_events_ends_at indexes (see ends_at_sql).
        event_end_time = ends_at_sql(Events)
        if is_ended:
            # Past events: event has ended (end_time < now)
            query = query.filter(event_end_time < func.now())
        else:
            # Upcoming/ongoing events: event hasn't ended yet (end_time >= now),
            # and so started at most the longest an event runs ago: the walk in
            # date order then starts near now (discover.upcoming).
            from app.api.discover import upcoming

            floor = await upcoming.hours(session, datetime.now(timezone.utc))
            query = query.filter(event_end_time >= func.now(), Events.event_datetime >= func.now() - timedelta(hours=floor))

    # Order by event_datetime instead of created_at
    # Past events: most recently ended first
    # Upcoming events: soonest first
    # The id settles ties, so a page boundary cannot fall between two events
    # at the same instant differently from one request to the next.
    if is_ended:
        query = query.order_by(Events.event_datetime.desc(), Events.id.desc())
    else:
        query = query.order_by(Events.event_datetime.asc(), Events.id.asc())

    query = query.limit(limit).offset(offset)
    ids = list(dict.fromkeys((await session.scalars(query)).all()))
    if not ids:
        return []
    loaded = await session.scalars(
        select(Events).where(Events.id.in_(ids)).options(
            joinedload(Events.category),
            joinedload(Events.club),
        )
    )
    by_id = {e.id: e for e in loaded.unique().all()}
    return [by_id[i] for i in ids if i in by_id]


async def create_event_category(
    session: AsyncSession, category: EventCategoryCreate, user_id: int
):
    db_category = EventCategories(**category.model_dump(), created_by_id=user_id)
    session.add(db_category)
    await session.commit()
    await session.refresh(db_category)
    return db_category


async def list_event_categories(session: AsyncSession):
    result = await session.execute(select(EventCategories))
    return result.scalars().all()


async def rate_event(
    session: AsyncSession,
    event_id: int,
    user_id: int,
    rating: float,
    review: str | None = None,
) -> EventRatingsLink:
    """Rate an event and update the corresponding club's rating."""
    # Validate rating is between 0 and 5
    if rating < 0 or rating > 5:
        raise CustomHTTPException(400, message="Rating must be between 0 and 5")
    
    await validate_relations(
        session,
        {
            "event": (Events, event_id),
            "user": (Users, user_id),
        },
    )

    # Get event and club
    event = await session.scalar(
        select(Events)
        .where(Events.id == event_id, Events.is_deleted == False)
        .options(joinedload(Events.club))
    )
    if not event:
        raise CustomHTTPException(404, "Event not found")

    if not event.club:
        raise CustomHTTPException(400, "Event not associated with a club")
    
    # Check if event has ended
    from datetime import timedelta
    event_end_time = event.event_datetime + timedelta(hours=event.duration) if event.duration else event.event_datetime
    # Handle both timezone-aware and timezone-naive datetimes
    now = datetime.now(timezone.utc)
    if event_end_time.tzinfo is None:
        # Event datetime is naive, assume UTC
        event_end_time = event_end_time.replace(tzinfo=timezone.utc)
    if now < event_end_time:
        raise CustomHTTPException(400, message="You can only rate events after they have ended")
    
    # Check if user was registered for the event
    user_registration = await session.scalar(
        select(EventRegistrationsLink).where(
            EventRegistrationsLink.event_id == event_id,
            EventRegistrationsLink.user_id == user_id,
            EventRegistrationsLink.is_deleted == False,
        )
    )
    if not user_registration:
        raise CustomHTTPException(400, message="You must be registered for this event to rate it")
    
    # For paid events, check if user has paid
    if event.has_fee and not user_registration.is_paid:
        raise CustomHTTPException(400, message="You must complete payment to rate this event")

    # Check if user has already rated
    existing_rating = await session.scalar(
        select(EventRatingsLink).where(
            EventRatingsLink.event_id == event_id,
            EventRatingsLink.user_id == user_id,
            EventRatingsLink.is_deleted == False,
        )
    )

    if existing_rating:
        event_rating = existing_rating
        event_rating.rating = rating
        event_rating.review = review
    else:
        # Create rating
        event_rating = EventRatingsLink(
            event_id=event_id,
            user_id=user_id,
            rating=rating,
            review=review,
        )
        session.add(event_rating)

    # Flush to ensure the rating is in the transaction before counting
    await session.flush()

    # Update club rating
    club = event.club
    total_ratings = await session.scalar(
        select(func.count()).where(
            EventRatingsLink.event_id.in_(
                select(Events.id).where(
                    Events.club_id == club.id,
                    Events.is_deleted == False
                )
            ),
            EventRatingsLink.is_deleted == False,
        )
    )
    avg_rating = await session.scalar(
        select(func.avg(EventRatingsLink.rating)).where(
            EventRatingsLink.event_id.in_(
                select(Events.id).where(
                    Events.club_id == club.id,
                    Events.is_deleted == False
                )
            ),
            EventRatingsLink.is_deleted == False,
        )
    )

    club.total_ratings = total_ratings if total_ratings else 0
    club.rating = avg_rating if avg_rating else 0

    await session.commit()
    await session.refresh(event_rating)

    # Check for new badges after rating
    try:
        from app.api.badges.service import check_and_award_user_badges, check_and_award_club_badges
        await check_and_award_user_badges(session, user_id=user_id)
        await check_and_award_club_badges(session, club_id=event.club.id)
    except Exception as e:
        logging.getLogger(__name__).error(f"Badge check failed after rating: {e}")

    return event_rating
    
    
async def list_event_ratings(
    session: AsyncSession,
    event_id: int,
    user_id: int,
    limit: int = 10,
    offset: int = 0,
    as_admin: bool = False,
):
    """List detailed ratings for an event (Club Admin only)."""
    # Verify event ownership
    event = await session.scalar(
        select(Events)
        .where(Events.id == event_id, Events.is_deleted == False)
        .options(joinedload(Events.club))
    )
    if not event:
        raise CustomHTTPException(404, "Event not found")
    
    ensure_event_access(
        event, user_id, as_admin, message="Not authorized to view ratings for this event"
    )
        
    query = (
        select(EventRatingsLink)
        .where(
            EventRatingsLink.event_id == event_id,
            EventRatingsLink.is_deleted == False,
        )
        .options(
            joinedload(EventRatingsLink.user).options(
                joinedload(Users.profile).options(
                    joinedload(UserProfiles.avatar),
                    joinedload(UserProfiles.org)
                )
            )
        )
        .order_by(EventRatingsLink.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    
    result = await session.execute(query)
    ratings = result.scalars().all()
    
    # Manually serialize to avoid Pydantic validation errors with nested SQLAlchemy models
    serialized_ratings = []
    for rating in ratings:
        try:
            # Serialize user profile
            user_data = None
            if rating.user:
                profile_data = None
                if rating.user.profile:
                    org_data = None
                    if rating.user.profile.org:
                        org_data = {
                            "id": rating.user.profile.org.id,
                            "name": rating.user.profile.org.name,
                            "type": rating.user.profile.org.type.value if hasattr(rating.user.profile.org.type, 'value') else str(rating.user.profile.org.type),
                        }
                    
                    avatar_data = None
                    if rating.user.profile.avatar:
                        # UserAvatars has 'image' S3ImageField which is a dict with variations
                        avatar_image = rating.user.profile.avatar.image
                        if avatar_image and isinstance(avatar_image, dict):
                            avatar_data = avatar_image
                        else:
                            avatar_data = {"id": rating.user.profile.avatar.id}
                    
                    profile_data = {
                        "full_name": rating.user.profile.full_name or "",
                        "avatar": avatar_data,
                        # The photo they uploaded, which wins over the avatar
                        # (reports.avatar). It was left out, so a reviewer
                        # with a photo showed as initials.
                        "profile_pic": rating.user.profile.profile_pic,
                        "org": org_data,
                    }
                
                user_data = {
                    "id": rating.user.id,
                    "username": rating.user.username or rating.user.email or "",
                    "email": rating.user.email or "",
                    "full_name": rating.user.profile.full_name if rating.user.profile else "",
                    "profile": profile_data,
                }
            
            serialized_ratings.append({
                "id": str(rating.id),
                "rating": float(rating.rating),
                "review": rating.review,
                "user": user_data,
                "created_at": rating.created_at.isoformat() if rating.created_at else None,
            })
        except Exception as e:
            logging.error(f"Error serializing rating {rating.id}: {e}")
            continue
    
    return serialized_ratings


async def get_ticket_details(session: AsyncSession, ticket_id: str | int):
    is_ticket_id = (isinstance(ticket_id, str) and ticket_id.isdigit()) or isinstance(
        ticket_id, int
    )
    if is_ticket_id:
        ticket_id = int(ticket_id)
    registration = await session.execute(
        select(EventRegistrationsLink)
        .filter(
            EventRegistrationsLink.is_deleted == False,
            EventRegistrationsLink.id == ticket_id
            if is_ticket_id
            else EventRegistrationsLink.ticket_id == ticket_id
        )
        .options(
            joinedload(EventRegistrationsLink.event).options(
                joinedload(Events.club), joinedload(Events.category)
            ),
            joinedload(EventRegistrationsLink.user),
        )
    )
    registration = registration.scalar()
    if registration is None:
        raise CustomHTTPException(404, "Ticket not found")
    return registration


async def list_deleted_events(session: AsyncSession, limit: int = 20, offset: int = 0):
    """Soft-deleted events, so a deletion can be found and undone."""
    # include_deleted on each: the session hides deleted rows from every ORM
    # query (app.db.listeners), so without it this finds none.
    total = await session.scalar(
        select(func.count(Events.id)).where(Events.is_deleted == True).execution_options(include_deleted=True)
    )
    rows = await session.scalars(
        select(Events)
        .where(Events.is_deleted == True)
        .execution_options(include_deleted=True)
        # interests included: the response model reads it, and a lazy load
        # during serialisation happens outside the async context and raises
        # MissingGreenlet.
        .options(
            selectinload(Events.category),
            selectinload(Events.club),
            selectinload(Events.interests).joinedload(Interests.category),
        )
        .order_by(Events.deleted_at.desc().nulls_last(), Events.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(rows), (total or 0)


async def restore_event(session: AsyncSession, event_id: int):
    event = await session.scalar(
        select(Events)
        .where(Events.id == event_id, Events.is_deleted == True)
        .execution_options(include_deleted=True)
        .options(
            selectinload(Events.category),
            selectinload(Events.club),
            selectinload(Events.interests).joinedload(Interests.category),
        )
    )
    if not event:
        raise CustomHTTPException(404, "No deleted event with that id")

    event.is_deleted = False
    event.deleted_at = None
    await session.commit()
    return event


async def delete_event(
    session: AsyncSession, event_id: int, user_id: int, as_admin: bool = False
):
    db_event = await session.execute(
        select(Events).filter(Events.id == event_id).options(joinedload(Events.club))
    )
    db_event = db_event.scalar()
    if db_event is None:
        raise CustomHTTPException(404, message="Event not found")
    ensure_event_access(
        db_event, user_id, as_admin, message="Not authorized to delete this event"
    )
    registration_count = await session.scalar(
        select(func.count()).filter(
            EventRegistrationsLink.event_id == event_id,
            EventRegistrationsLink.is_deleted == False,
        )
    )
    if registration_count > 0:
        raise CustomHTTPException(400, message="Cannot delete event with registrations")
    db_event.soft_delete()
    await session.commit()
    return None


async def increment_event_page_view(
    session: AsyncSession,
    request: Request,
    event_id: int | str,
    user_id: int | None = None,
):
    """Count a look at the event's page: once per visitor a day, the club's
    own looks not at all, written in batches (app/api/events/views.py).
    404 for no such event."""
    from app.api.events import views

    await views.record(session, request, event_id, user_id)
    return None
