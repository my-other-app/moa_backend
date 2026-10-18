from datetime import datetime, timezone
from pydantic import ConfigDict
from sqlalchemy import (
    ARRAY,
    UUID,
    Boolean,
    Column,
    Enum,
    ForeignKey,
    Integer,
    String,
    JSON,
    Float,
    Numeric,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
import sqlalchemy as sa
from sqlalchemy import DDL, event as sa_event

from app.api.templates_audience import RegistrationRole
from app.api.users.models import UserAvatarTypes
from app.api.checkins.models import DoorMode
from app.api.idcards.models import ReprintPolicy
from app.api.kiosk.models import SelfCheckInVerify
from app.api.closeout.models import EventStatus, RetentionMode
from app.db.base import AbstractSQLModel
from app.db.mixins import SoftDeleteMixin, TimestampsMixin
from app.core.storage.fields import S3FileField, S3ImageField
from app.core.utils.db_fields import TZAwareDateTime


class EventCategories(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "event_categories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(50), nullable=False)
    icon = Column(String, nullable=True)
    icon_type = Column(Enum(UserAvatarTypes))
    # Null for a type the product ships; a user for one a club added.
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    created_by = relationship("Users")


class Events(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    slug = Column(String(120), nullable=False, unique=True, index=True)
    name = Column(String(100), nullable=False)
    poster = Column(
        S3ImageField(
            upload_to="clubs/logos/",
            variations={
                "thumbnail": {"width": 150, "height": 150},
                "medium": {"width": 500, "height": 500},
                "large": {"width": 800, "height": 800},
            },
        ),
        nullable=True,
    )
    images = Column(ARRAY(String), nullable=False, default=[])
    event_datetime = Column(TZAwareDateTime(timezone=True), nullable=False)
    has_fee = Column(Boolean, nullable=False, default=False)
    reg_fee = Column(Numeric(10, 2), nullable=True)
    # Merchandise sold at registration, and free swag counted out at the door.
    merch_enabled = Column(Boolean, nullable=False, default=False)
    swag_enabled = Column(Boolean, nullable=False, default=False)
    # Live Q&A: a question waits for the organiser before the room sees it.
    qa_approve_first = Column(Boolean, nullable=False, default=True, server_default=sa.true())
    duration = Column(Float, nullable=False)
    about = Column(String, nullable=True)
    location_name = Column(String, nullable=True)
    location_link = Column(String, nullable=True)
    # Where the venue is, for "near you": read from location_link (a map
    # link) when it holds one, found from location_name by the geocoder when
    # it does not, or pinned by hand on the dashboard's map.
    lat = Column(Float, nullable=True)
    lng = Column(Float, nullable=True)
    # Which of those the point came from: 'manual' > 'link' > 'geocoded'; a
    # weaker one never replaces a stronger (app.api.discover.geo.apply_place).
    geo_source = Column(String(10), nullable=True)
    has_prize = Column(Boolean, nullable=False, default=False)
    prize_amount = Column(Numeric(10, 2), nullable=True)
    contact_name = Column(String, nullable=True)
    contact_phone = Column(String, nullable=True)
    contact_email = Column(String, nullable=True)
    url = Column(String, nullable=True)
    is_online = Column(Boolean, nullable=True)
    reg_startdate = Column(
        TZAwareDateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    reg_enddate = Column(TZAwareDateTime(timezone=True), nullable=True)
    max_participants = Column(Integer, nullable=True)
    additional_details = Column(ARRAY(JSON), nullable=True)
    event_guidelines = Column(String, nullable=True)
    page_views = Column(Integer, nullable=False, default=0)
    # How many are registered and have not cancelled: a trigger on
    # event_registrations_link keeps it, in the same transaction as every
    # insert, cancel, delete or move there (see GOING_COUNT_SQL below), so a
    # list can sort by popularity without counting registrations.
    # Reconciled from the rows by app.api.discover.counters.reconcile.
    going_count = Column(Integer, nullable=False, default=0, server_default="0")
    event_tag = Column(String(50), nullable=True)  # Tag like "Free", "Paid", "Featured", etc.

    category_id = Column(Integer, ForeignKey("event_categories.id"), nullable=False)
    club_id = Column(Integer, ForeignKey("clubs.id"), nullable=True)
    ticket_template_id = Column(Integer, ForeignKey("ticket_templates.id"), nullable=True)
    certificate_template_id = Column(Integer, ForeignKey("certificate_templates.id"), nullable=True)
    # The door. `open` admits anyone with a ticket at any time; `auto` opens a
    # day before the start and closes `close_after_minutes` past the end.
    door_mode = Column(Enum(DoorMode), nullable=False, default=DoorMode.open)
    close_after_minutes = Column(Integer, nullable=False, default=240)
    # Whether a guest registered at the desk is admitted by the same stroke.
    spot_registration_checks_in = Column(Boolean, nullable=False, default=True)
    capture_contact_at_gate = Column(Boolean, nullable=False, default=False)
    require_contact_at_gate = Column(Boolean, nullable=False, default=False)
    # ID cards: who may print a second one, whether a check-in prints, and
    # what the blank stock already carries.
    reprint_policy = Column(Enum(ReprintPolicy), nullable=False, default=ReprintPolicy.anyone)
    auto_print_badges = Column(Boolean, nullable=False, default=False)
    preprinted_carries = Column(JSON, nullable=False, default=list)
    # Self check-in from a guest's phone, via a kiosk's QR.
    self_check_in_enabled = Column(Boolean, nullable=False, default=False)
    self_check_in_verify = Column(Enum(SelfCheckInVerify), nullable=False, default=SelfCheckInVerify.email)
    self_check_in_print = Column(Boolean, nullable=False, default=True)
    # Staff mode on a kiosk; checked on the server, never shipped to the tablet.
    kiosk_pin = Column(String(12), nullable=True)

    # The status clock: published → live at the start, → closed after the
    # end, → archived once retention has run. Stored as "status"; read
    # through the `status` property below, which the app has always used.
    lifecycle = Column("status", Enum(EventStatus), nullable=False, default=EventStatus.published, server_default="published")
    status_clock = Column(Boolean, nullable=False, default=True, server_default=sa.true())
    retention_mode = Column(Enum(RetentionMode), nullable=False, default=RetentionMode.keep, server_default="keep")
    retention_hours = Column(Integer, nullable=False, default=72, server_default="72")
    report_recipients = Column(ARRAY(String), nullable=False, default=list, server_default="{}")
    keep_contacts = Column(Boolean, nullable=False, default=True, server_default=sa.true())

    # How this event numbers its tickets: a prefix, a padded sequence, the
    # event code at the end. Numbers already given out are never rewritten.
    admission_prefix = Column(String(12), nullable=True)
    admission_digits = Column(Integer, nullable=False, default=4, server_default="4")
    admission_include_code = Column(Boolean, nullable=False, default=True, server_default=sa.true())
    admission_seq = Column(Integer, nullable=False, default=0, server_default="0")

    category = relationship("EventCategories")
    club = relationship("Clubs")
    # foreign_keys is required now that the relationship runs both ways:
    # an event points at a club template it was assigned, and an event's
    # own forked design points back at the event. Without naming the column,
    # SQLAlchemy cannot tell which of the two joins is meant.
    ticket_template = relationship(
        "TicketTemplates", foreign_keys=[ticket_template_id]
    )
    certificate_template = relationship(
        "CertificateTemplates", foreign_keys=[certificate_template_id]
    )
    registrations = relationship("EventRegistrationsLink", back_populates="event")
    interests = relationship(
        "Interests",
        secondary="event_interests_link",
        back_populates="events",
        uselist=True,
    )
    files = relationship("EventFiles", back_populates="event")
    ratings = relationship("EventRatingsLink", back_populates="event")
    speakers = relationship("EventSpeakers", back_populates="event", order_by="EventSpeakers.display_order")

    model_config = ConfigDict(from_attributes=True)

    __table_args__ = (
        # What the public lists walk: what is on or to come, soonest first.
        # The predicate is written out, not bound, by the queries that use it
        # (see discover.service._in), so a cached generic plan can use it too.
        sa.Index("ix_events_shown_start", "event_datetime", "id", postgresql_where=sa.text("NOT is_deleted AND status IN ('published', 'live')")),
        # When each event ends, for "not over yet" without reading every row.
        sa.Index(
            "ix_events_ends_at",
            sa.func.timezone(sa.literal_column("'UTC'"), sa.func.timezone(sa.literal_column("'UTC'"), event_datetime) + duration * sa.text("INTERVAL '1 hour'")),
            postgresql_where=sa.text("NOT is_deleted"),
        ),
        sa.Index("ix_events_club_start", "club_id", "event_datetime", postgresql_where=sa.text("NOT is_deleted")),
        sa.Index("ix_events_category_start", "category_id", "event_datetime", postgresql_where=sa.text("NOT is_deleted")),
        # "Near you": a latitude/longitude box first, the exact distance after.
        # sort=newest on the public lists (the feeds): what can be shown, the latest made first.
        sa.Index("ix_events_shown_created", "created_at", "id", postgresql_where=sa.text("NOT is_deleted AND status IN ('published', 'live')")),
        sa.Index("ix_events_shown_point", "lat", "lng", postgresql_where=sa.text("NOT is_deleted AND status IN ('published', 'live') AND lat IS NOT NULL AND lng IS NOT NULL")),
        # Words anywhere in the name or the place: trigram indexes serve ILIKE '%…%' and similarity.
        # What overlaps a stretch of time, held events included: the calendar
        # and the date-range search. The event as the closed range from its
        # start to its end; never inverted, so a bad duration cannot fail a write.
        # Over every row, not partial: the planner only uses an expression
        # index's statistics when the index is not partial, and without them
        # it takes any stretch of days to hold a few hundred events.
        sa.Index(
            "ix_events_span",
            sa.func.tstzrange(
                event_datetime,
                sa.func.greatest(event_datetime, sa.func.timezone(sa.literal_column("'UTC'"), sa.func.timezone(sa.literal_column("'UTC'"), event_datetime) + duration * sa.text("INTERVAL '1 hour'"))),
                sa.literal_column("'[]'"),
            ),
            postgresql_using="gist",
        ),
        sa.Index("ix_events_name_trgm", "name", postgresql_using="gin", postgresql_ops={"name": "gin_trgm_ops"}, postgresql_where=sa.text("NOT is_deleted")),
        sa.Index("ix_events_location_trgm", "location_name", postgresql_using="gin", postgresql_ops={"location_name": "gin_trgm_ops"}, postgresql_where=sa.text("NOT is_deleted")),
        # The same over what can be shown only: the search's typo fallback
        # looks among these, not every event ever held (the status clock
        # moves what is over out of them; discover.service._matching).
        sa.Index("ix_events_shown_name_trgm", "name", postgresql_using="gin", postgresql_ops={"name": "gin_trgm_ops"},
                 postgresql_where=sa.text("NOT is_deleted AND status IN ('published', 'live')")),
        sa.Index("ix_events_shown_location_trgm", "location_name", postgresql_using="gin", postgresql_ops={"location_name": "gin_trgm_ops"},
                 postgresql_where=sa.text("NOT is_deleted AND status IN ('published', 'live')")),
        # The few events longer than MAX_EVENT_HOURS (saved before that limit)
        # by when they end: how far back "what is on" must look while one of
        # them is still running (discover.upcoming).
        sa.Index(
            "ix_events_long_ends_at",
            sa.func.timezone(sa.literal_column("'UTC'"), sa.func.timezone(sa.literal_column("'UTC'"), event_datetime) + duration * sa.text("INTERVAL '1 hour'")),
            postgresql_where=sa.text("NOT is_deleted AND duration > 72"),
        ),
    )

    @property
    def status(self) -> str:
        if getattr(self, "is_deleted", False):
            return "cancelled"
        return (self.lifecycle or EventStatus.published).value


#: The longest an event may run, in hours, checked when one is created, edited
#: or copied (events.service.check_duration). Lists of what is on use it as a
#: lower bound: an event not over yet started at most this long ago, so their
#: walks start near now, not at the oldest event still marked published
#: (discover.upcoming). ix_events_long_ends_at's predicate repeats the number.
MAX_EVENT_HOURS = 72


def ends_at_sql(model=None):
    """When an event ends, start plus `duration` hours, as SQL.

    Written in UTC wall time: `timestamptz + interval` is only STABLE (a day's
    length depends on the session's zone), so Postgres will not index it;
    `timestamp + interval` and `AT TIME ZONE 'UTC'` are IMMUTABLE. For an
    interval of hours, which is all a duration ever is, the two are the same
    instant in every zone. This exact expression is what ix_events_ends_at
    indexes, so "not over yet" can be a range scan."""
    m = model if model is not None else Events
    utc = sa.literal_column("'UTC'")
    return sa.func.timezone(utc, sa.func.timezone(utc, m.event_datetime) + m.duration * sa.text("INTERVAL '1 hour'"))


def span_sql(model=None):
    """The event as a closed range of instants, start to end, as SQL: what
    ix_events_span indexes, so `span && range` is an index probe. Closed, so
    an event of no length is still a point in time; never inverted (a range
    whose end is before its start is an error), whatever the duration."""
    m = model if model is not None else Events
    return sa.func.tstzrange(m.event_datetime, sa.func.greatest(m.event_datetime, ends_at_sql(m)), sa.literal_column("'[]'"))


class EventSpeakers(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    """Speakers and guests for events."""
    __tablename__ = "event_speakers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    name = Column(String(100), nullable=False)
    designation = Column(String(200), nullable=True)
    photo = Column(
        S3ImageField(
            upload_to="events/speakers/",
            variations={
                "thumbnail": {"width": 100, "height": 100},
                "medium": {"width": 200, "height": 200},
            },
        ),
        nullable=True,
    )
    display_order = Column(Integer, nullable=False, default=0)
    # A few lines about them, and one place to find out more; the event page shows both.
    bio = Column(String(600), nullable=True)
    link = Column(String(500), nullable=True)

    event = relationship("Events", back_populates="speakers")


class EventFiles(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "event_files"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
        default=sa.text("gen_random_uuid()"),
    )
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    file = Column(
        S3FileField(
            upload_to="events/files/",
            allowed_extensions=[
                "pdf",
                "doc",
                "docx",
                "ppt",
                "pptx",
                "xls",
                "xlsx",
                "txt",
                "png",
                "jpg",
                "jpeg",
                "gif",
            ],
        ),
        nullable=False,
    )
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)

    event = relationship("Events", back_populates="files")


class EventPageViews(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "event_page_views"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
        default=sa.text("gen_random_uuid()"),
    )
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    user_agent = Column(String, nullable=True)
    ip_address = Column(String, nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    event = relationship("Events")
    user = relationship("Users")


class EventInterestsLink(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "event_interests_link"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    interest_id = Column(Integer, ForeignKey("interests.id"), nullable=False)

    # event = relationship("Events")
    # interest = relationship("Interests")

    __table_args__ = (
        sa.Index("ix_event_interests_event", "event_id", postgresql_where=sa.text("NOT is_deleted")),
        sa.Index("ix_event_interests_interest", "interest_id", "event_id", postgresql_where=sa.text("NOT is_deleted")),
    )


class EventRegistrationsLink(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "event_registrations_link"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
        default=sa.text("gen_random_uuid()"),
    )
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    full_name = Column(String(100), nullable=False)
    email = Column(String(100), nullable=False)
    phone = Column(String(15), nullable=True)

    ticket_id = Column(String(60), nullable=False, unique=True, index=True)

    # What this person is at the event, which picks the ticket and
    # certificate variant they are issued. Everyone is an attendee unless
    # the organiser says otherwise.
    role = Column(
        Enum(RegistrationRole),
        nullable=False,
        default=RegistrationRole.attendee,
        server_default=RegistrationRole.attendee.value,
    )
    is_attended = Column(Boolean, nullable=False, default=False)
    attended_on = Column(TZAwareDateTime(timezone=True), nullable=True)
    # Set when the organiser revokes the ticket: the door refuses it by name
    # from the next scan, and nothing is sent for it until it is reinstated.
    ticket_revoked_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    # A certificate is issued by the organiser and can be taken back. Null
    # issued_at with is_attended keeps the certificate attended guests
    # always had, so turning issuing on removes nothing.
    certificate_issued_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    certificate_revoked_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    certificate_design_version = Column(Integer, nullable=False, default=0, server_default="0")
    # When this guest's ID card last printed; the reprint rule turns on it.
    badge_printed_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    # Set by the provider's bounce or complaint notice: later sends skip the
    # address until it is corrected, which clears it.
    email_bounced_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    email_bounce_reason = Column(String(300), nullable=True)
    wa_bounced_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    wa_bounce_reason = Column(String(300), nullable=True)
    # The admission number the ticket prints beside the scan code.
    admission_no = Column(String(40), nullable=True)
    # The tracking link whose code this sign-up carried, when it came through one.
    tracking_link_id = Column(UUID(as_uuid=True), ForeignKey("tracking_links.id", ondelete="SET NULL"), nullable=True)
    volunteer_id = Column(Integer, ForeignKey("volunteers.id"), nullable=True)

    is_won = Column(Boolean, nullable=False, default=False)
    position = Column(Integer, nullable=True)

    is_paid = Column(Boolean, nullable=False, default=False)
    paid_amount = Column(Numeric(10, 2), nullable=False, default=0)
    actual_amount = Column(Numeric(10, 2), nullable=False, default=0)
    payment_receipt = Column(String, nullable=True)

    additional_details = Column(JSON, nullable=True)

    event = relationship("Events", back_populates="registrations")
    user = relationship("Users")
    volunteer = relationship("Volunteer")

    __table_args__ = (
        UniqueConstraint("event_id", "user_id", "is_deleted"),
        # One person's registrations: the app's ticket lists and the For You feed.
        sa.Index("ix_registrations_user", "user_id", postgresql_where=sa.text("NOT is_deleted")),
        # An address already on an event's list (registration's duplicate
        # check, the public form's): lower(btrim(email)) is what
        # users.service.same_email compares.
        sa.Index(
            "ix_registrations_event_email", "event_id", sa.func.lower(sa.func.btrim(email)),
            postgresql_where=sa.text("NOT is_deleted"),
        ),
        # An event's registrations by when they came, with whether each is
        # paid: the plan's monthly count (this month's, event by event), a
        # paid event's places, and the counter check's slices, all read from
        # the index alone.
        sa.Index(
            "ix_registrations_event_created", "event_id", "created_at",
            postgresql_include=["is_paid"], postgresql_where=sa.text("NOT is_deleted"),
        ),
        # The sign-ups a tracking link brought. Most registrations came
        # through none, so only those that did are indexed.
        sa.Index(
            "ix_event_registrations_link_tracking_link_id", "tracking_link_id",
            postgresql_where=sa.text("tracking_link_id IS NOT NULL"),
        ),
    )

    @property
    def institution(self) -> str | None:
        if self.additional_details and isinstance(self.additional_details, dict):
            return (
                self.additional_details.get("College Name")
                or self.additional_details.get("Institution")
                or self.additional_details.get("college")
            )
        return None

    @property
    def name(self) -> str:
        return self.full_name


class EventRatingsLink(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "event_ratings_link"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
        default=sa.text("gen_random_uuid()"),
    )
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    rating = Column(Float, nullable=False)
    review = Column(String, nullable=True)

    event = relationship("Events", back_populates="ratings")
    user = relationship("Users")

    __table_args__ = (UniqueConstraint("event_id", "user_id", "is_deleted"),)


# ─── events.going_count, kept by the database ─────────────────────────────
#
# A trigger rather than application code: registrations are written from the
# public form, the app, the desk, the kiosk, imports, the guest merge, refunds
# and the retention scrub, and a trigger sees every one of them, raw SQL
# included, in the writer's own transaction. Registration already holds the
# event row (SELECT … FOR UPDATE), so the extra update adds no new waiting.
# The migration discover_perf installs the same function and trigger.

GOING_COUNT_SQL = (
    """
    CREATE OR REPLACE FUNCTION moa_going_count() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'UPDATE' AND OLD.event_id = NEW.event_id AND OLD.is_deleted = NEW.is_deleted THEN
        RETURN NULL;
      END IF;
      IF TG_OP IN ('UPDATE', 'DELETE') AND NOT OLD.is_deleted THEN
        UPDATE events SET going_count = going_count - 1 WHERE id = OLD.event_id;
      END IF;
      IF TG_OP IN ('UPDATE', 'INSERT') AND NOT NEW.is_deleted THEN
        UPDATE events SET going_count = going_count + 1 WHERE id = NEW.event_id;
      END IF;
      RETURN NULL;
    END $$
    """,
    """
    CREATE TRIGGER trg_registrations_going_count
    AFTER INSERT OR DELETE OR UPDATE OF event_id, is_deleted ON event_registrations_link
    FOR EACH ROW EXECUTE FUNCTION moa_going_count()
    """,
)

for _sql in GOING_COUNT_SQL:
    sa_event.listen(EventRegistrationsLink.__table__, "after_create", DDL(_sql).execute_if(dialect="postgresql"))
