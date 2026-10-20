"""The showcase club: one demo club with something in every corner of the
clubs dashboard, for the owner to walk a new club through.

The seed (seed.py) gives every club its page, events, people, tickets,
certificates, store, feedback, live room and posts. For the showcase club
(`--showcase`, Respawn Room by default) this adds what a club running for
a year would also have:

- its committee on Access (an admin, an editor, door staff, one invite still
  open) and their sign-ins and sessions on Security;
- organisers on each event, doors and a kiosk, volunteers checking people
  in at their doors, ID cards printed at check-in;
- walk-ups registered at the desk, paying cash there for the store, and one
  store order oversold (paid for after the last of its size went);
- the game jam as a competition: tracks with sponsors, teams, submissions,
  judges' scores, a shortlist and finalists, mentors and booked slots;
- a quiz in the live room and the updates sent on the day;
- announcements, tracking links with their clicks (and the registrations
  and follows they brought), the journey emails, messages and the
  delivery log, its finance (expenses, sponsorship and grant income), the
  ID card and programme designs, and closeout reports.

Every row is made through the service that makes it in the app, or
inserted as that service inserts it where the service would send
something; quiet mode (quiet.py) is on throughout. Nothing here takes
money: the store's and the tickets' money is cash at the desk.
"""

from __future__ import annotations

import hashlib
import random
import uuid
from datetime import datetime, timedelta, timezone

from scripts.demo.schema import DEMO_DOMAIN

UAS = [
    "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 13; SM-A346E) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
]
REFERRERS = ["https://www.instagram.com/", "https://l.instagram.com/", "https://web.whatsapp.com/", "https://www.google.com/",
             "https://www.linkedin.com/", None, None]


def address_of(name: str, slug: str) -> str:
    """A committee member's or volunteer's demo address: never a real inbox."""
    parts = [p for p in "".join(ch if ch.isalpha() or ch == " " else " " for ch in name.lower()).split() if p not in ("prof", "dr")]
    local = ".".join(parts[:2]) or "member"
    return f"{local}@{slug}.{DEMO_DOMAIN}"


class Showcase:
    def __init__(self, seeder, club_data, ctx):
        self.sd = seeder
        self.s = seeder.s
        self.c = club_data
        self.club = ctx["club"]
        self.owner = ctx["owner"]
        self.founded = ctx["founded"]
        self.brand = ctx["brand"]
        self.now = seeder.now
        self.rnd = random.Random(f"showcase:{club_data.slug}")
        self.members: dict[str, object] = {}
        self.logins: list[tuple[str, str, str]] = []
        self.team_rows: list = []
        self.counts = seeder.report.counts

    async def commit(self):
        await self.s.commit()

    # ── Access and security ──────────────────────────────────────────────

    async def access(self) -> None:
        """The committee on the dashboard: an admin, an editor, two at the
        door, all signed in; one more invited and not yet in."""
        from sqlalchemy import select, update

        from app.api.club_access import service as access
        from app.api.club_access.models import ClubMembers
        from app.api.clubs.models import ClubTeamMembers

        self.team_rows = list((await self.s.scalars(select(ClubTeamMembers).where(
            ClubTeamMembers.club_id == self.club.id).order_by(ClubTeamMembers.sort_order))).all())
        people = [m for m in self.c.team[1:]]  # the first is the owner's own login
        roles = ["admin", "editor", "door", "door", "editor"]
        for i, (m, role) in enumerate(zip(people, roles)):
            email = address_of(m.name, self.c.slug)
            member, token = await access.invite(self.s, self.club, email=email, full_name=m.name, role=role)
            await self.commit()
            invited = self.founded + timedelta(days=10 + i * 9)
            if i < 4:
                await access.accept(self.s, token, full_name=m.name, password=self.sd.club_password)
                await self.commit()
                await self.s.execute(update(ClubMembers).where(ClubMembers.id == member.id).values(
                    invited_at=invited, accepted_at=invited + timedelta(hours=self.rnd.randint(2, 40)),
                    last_seen_at=self.now - timedelta(hours=self.rnd.randint(2, 120)), created_at=invited, updated_at=invited))
                self.logins.append((m.name, role, email))
            else:
                await self.s.execute(update(ClubMembers).where(ClubMembers.id == member.id).values(
                    invited_at=self.now - timedelta(days=2), created_at=self.now - timedelta(days=2)))
            self.members[role if role not in self.members else f"{role}2"] = member
        await self.commit()
        self.counts["showcase_access_members"] = len(people)

    async def security(self) -> None:
        """Sign-ins to the club's own account over two months, and the
        sessions still open (the owner's laptop, the admin's phone)."""
        from app.api.auth.sessions import UserSessions
        from app.api.security.models import SignInAttempts

        places = ["Kottayam, Kerala, India", "Kottayam, Kerala, India", "Kochi, Kerala, India"]
        rows = []
        for i in range(14):
            at = self.now - timedelta(days=self.rnd.uniform(0.5, 58), hours=self.rnd.randint(0, 12))
            result = "wrong_password" if i == 5 else "signed_in"
            rows.append(SignInAttempts(user_id=self.owner.id, identifier=self.owner.email.lower(), ip_address=f"117.{200 + i % 50}.{i * 7 % 250}.{20 + i}",
                                       user_agent=self.rnd.choice(UAS), method="password", result=result, place=self.rnd.choice(places),
                                       created_at=at, updated_at=at))
        self.s.add_all(rows)
        admin = self.members.get("admin")
        self.s.add_all([
            UserSessions(user_id=self.owner.id, user_agent=UAS[4], ip_address="117.216.40.12", last_seen_at=self.now - timedelta(hours=3),
                         expires_at=self.now + timedelta(days=6), created_at=self.now - timedelta(days=1), updated_at=self.now - timedelta(hours=3)),
            UserSessions(user_id=self.owner.id, user_agent=UAS[1], ip_address="106.208.11.90", last_seen_at=self.now - timedelta(days=1),
                         expires_at=self.now + timedelta(days=5), member_id=admin.id if admin else None,
                         created_at=self.now - timedelta(days=2), updated_at=self.now - timedelta(days=1)),
            UserSessions(user_id=self.owner.id, user_agent=UAS[3], ip_address="117.230.9.4", last_seen_at=self.now - timedelta(days=9),
                         revoked=True, expires_at=self.now - timedelta(days=2), created_at=self.now - timedelta(days=16), updated_at=self.now - timedelta(days=9)),
        ])
        await self.commit()
        self.counts["showcase_sign_ins"] = len(rows)

    # ── An event's crew ──────────────────────────────────────────────────

    async def crew(self, e, ev_ctx) -> list:
        """Doors, a kiosk on the bigger days, and volunteers at the doors:
        the (actor, door) pairs check-ins and the swag table rotate through."""
        from app.api.checkins.doors import create_door
        from app.api.checkins.gate_auth import GateActor
        from app.api.checkins.models import DoorKind
        from app.api.events.volunteer.models import Volunteer
        from app.api.kiosk import service as kiosk
        from app.api.organisers import service as organisers
        from app.api.organisers.schemas import OrganiserIn

        ev = ev_ctx["event"]
        rnd = random.Random(f"crew:{e.slug}")
        gate = await create_door(self.s, ev, name="Main gate", kind=DoorKind.door)
        desk = await create_door(self.s, ev, name="Registration desk", kind=DoorKind.desk)
        await self.commit()
        if e.capacity >= 80 or e.when.duration_hours >= 8:
            await kiosk.create_device(self.s, ev, name="Lab entrance kiosk", door_label=None, door_id=gate.id, shift_hours=12, created_by=self.owner.id)
            ev.self_check_in_enabled = True
            await self.commit()
            self.counts["showcase_kiosks"] += 1
        # Organisers: the committee members who ran it.
        leads = rnd.sample(self.team_rows[:6], k=min(3, len(self.team_rows)))
        await organisers.replace(self.s, ev, [OrganiserIn(member_id=m.id, role=m.role) for m in leads])
        await self.commit()
        # Volunteers from the people who come to everything, at the two doors.
        crew = []
        for i, person in enumerate(rnd.sample(self.sd.pool[:150], k=4)):
            door = gate if i % 2 == 0 else desk
            v = Volunteer(email=person.email, event_id=ev.id, user_id=person.user_id, club_id=None, is_approved=True,
                          can_check_in=True, can_walkup=door is desk, can_merch=i in (1, 2), door_id=door.id,
                          created_at=ev_ctx["made"] + timedelta(days=3), updated_at=ev_ctx["made"] + timedelta(days=3))
            self.s.add(v)
            await self.s.flush()
            from app.api.users.models import Users

            user = await self.s.get(Users, person.user_id)
            crew.append((GateActor(kind="user", name=person.full_name, user=user, volunteer=v, can_check_in=True,
                                   can_walkup=door is desk, can_merch=True), door.name))
        await self.commit()
        self.counts["showcase_volunteers"] += len(crew)
        self.counts["showcase_doors"] += 2
        owner = GateActor(kind="user", name=self.owner.full_name, user=self.owner, full=True)
        return crew + [(owner, desk.name)]

    # ── The day after ────────────────────────────────────────────────────

    async def walkups(self, e, ev_ctx, crew) -> int:
        """People who turned up without registering, registered at the desk
        (no address given, so nothing is sent), some buying from the store in cash."""
        from sqlalchemy import select, update

        from app.api.checkins import service as checkins
        from app.api.checkins.models import CheckIns
        from app.api.checkins.schemas import WalkupBody
        from app.api.events.models import EventRegistrationsLink as Reg
        from app.api.store.models import EventStoreItems
        from app.api.store.schemas import StorePick
        from app.api.users.models import Users

        ev, start = ev_ctx["event"], ev_ctx["start"]
        room = max(0, e.capacity - len(ev_ctx["regs"]))
        if not room:
            return 0
        rnd = random.Random(f"walkup:{e.slug}")
        desk = [a for a in crew if a[1] == "Registration desk"]
        items = list((await self.s.scalars(select(EventStoreItems).where(EventStoreItems.id.in_([i.id for i in ev_ctx["items"]])))).all()) if ev_ctx["store_open"] else []
        taken = {r.user_id for r in ev_ctx["regs"]}
        made = 0
        for i in range(min(room, 6)):
            first = rnd.choice([n.split()[0] for n in self.sd.people.attendee_names])
            last = rnd.choice([n.split()[-1] for n in self.sd.people.attendee_names])
            store = None
            if items and i % 2 == 0:
                item = rnd.choice(items)
                variant = rnd.choice(item.variants).id if item.variants else None
                store = [StorePick(item_id=item.id, variant_id=variant, qty=1)]
            body = WalkupBody(name=f"{first} {last}", phone=f"9{rnd.randint(100000000, 999999999)}", client_ref=uuid.uuid4().hex,
                              door="Registration desk", custom_fields=self.sd.answers(e, self.sd.pool[0], rnd), confirm_new=True, store=store)
            out = await checkins.walkup(self.s, ev, body, desk[i % len(desk)][0] if desk else crew[0][0])
            reg_id = (out.get("attendee") or {}).get("id") or out.get("registration_id")
            reg = await self.s.get(Reg, uuid.UUID(str(reg_id))) if reg_id else None
            if reg is None:
                continue
            at = start + timedelta(minutes=rnd.randint(20, 120))
            # The walk-up's guest account takes a demo address, so the
            # suppression guard covers it and remove.py finds it.
            address = f"walkup.{ev.id}.{i + 1}@{DEMO_DOMAIN}"
            if reg.user_id not in taken:
                await self.s.execute(update(Users).where(Users.id == reg.user_id).values(email=address, created_at=at, updated_at=at))
            reg.email = address
            reg.created_at = at
            await self.s.execute(update(CheckIns).where(CheckIns.registration_id == reg.id).values(checked_in_at=at, created_at=at, updated_at=at))
            reg.attended_on = at
            await self.commit()
            made += 1
        self.counts["showcase_walkups"] += made
        return made

    async def oversold(self, e, ev_ctx) -> bool:
        """One store order paid for after its size ran out: the organiser
        trimmed the stock to what was already sold while the order waited
        at the desk, so the payment finds nothing left -- `oversold`, for the
        organiser to settle."""
        from sqlalchemy import select

        from app.api.ledger import book as ledger_book
        from app.api.ledger import service as ledger
        from app.api.store import service as store
        from app.api.store.models import EventStoreItems, EventStoreOrders, EventStoreVariants
        from app.api.store.schemas import StorePick

        ev = ev_ctx["event"]
        items = list((await self.s.scalars(select(EventStoreItems).where(EventStoreItems.id.in_([i.id for i in ev_ctx["items"]])))).all())
        sized = [i for i in items if i.variants]
        if not sized:
            return False
        with_order = set((await self.s.scalars(select(EventStoreOrders.registration_id).where(EventStoreOrders.event_id == ev.id))).all())
        buyer = next((r for r in ev_ctx["attended"] if r.id not in with_order), None)
        if buyer is None:
            return False
        item = sized[0]
        variant = max(item.variants, key=lambda v: v.sold_count)
        order = await store.place_order(self.s, ev, buyer, [StorePick(item_id=item.id, variant_id=variant.id, qty=1)])
        await self.s.flush()
        await store.release(self.s, order)
        v = await self.s.get(EventStoreVariants, variant.id)
        v.stock = v.sold_count + v.reserved_count
        await self.s.flush()
        status = await store.mark_paid(self.s, order)
        await ledger.sync_registration(self.s, buyer, kind=ledger_book.DESK_SALE, channel=ledger.DESK, at=ev_ctx["start"] + timedelta(hours=2))
        await self.commit()
        self.counts["showcase_oversold_orders"] += status == "oversold"
        return status == "oversold"

    async def print_cards(self, e, ev_ctx) -> None:
        """An ID card printed for everyone checked in, at their door."""
        from sqlalchemy import select

        from app.api.checkins.models import CheckIns
        from app.api.idcards.models import PrintJobs, PrintSource

        ev = ev_ctx["event"]
        ev.auto_print_badges = True
        rows = (await self.s.execute(select(CheckIns.registration_id, CheckIns.door_label, CheckIns.checked_in_by, CheckIns.checked_in_at)
                                     .where(CheckIns.event_id == ev.id))).all()
        regs = {r.id: r for r in ev_ctx["regs"]}
        for reg_id, door, by, at in rows:
            when = (at or ev_ctx["start"]) + timedelta(seconds=40)
            self.s.add(PrintJobs(event_id=ev.id, registration_id=reg_id, source=PrintSource.checkin, door_label=door, requested_by=by,
                                 cards=1, design_version=1, created_at=when, updated_at=when))
            if reg_id in regs:
                regs[reg_id].badge_printed_at = when
        await self.commit()
        self.counts["showcase_id_cards_printed"] += len(rows)

    async def competition(self, e, ev_ctx) -> None:
        """The jam as a competition, from teams to finalists."""
        from app.api.competitions import service as comp

        ev, start = ev_ctx["event"], ev_ctx["start"]
        rnd = random.Random(f"competition:{e.slug}")
        c = await comp.get_or_create(self.s, ev)
        await comp.patch_settings(self.s, c, {
            "enabled": True, "team_min": 2, "team_max": 4, "leaderboard_public": True, "shortlist_size": 5, "final_size": 3,
            "slot_minutes": 20, "score_method": "mean",
            "requirements": [
                {"key": "build", "label": "Playable build (itch.io page)", "required": True},
                {"key": "video", "label": "Gameplay video, two minutes at most", "required": True},
                {"key": "repo", "label": "Source repository", "required": False},
            ],
            "rubric": [
                {"key": "fun", "label": "Fun", "weight": 35},
                {"key": "theme", "label": "Use of the theme", "weight": 25},
                {"key": "art", "label": "Art and sound", "weight": 20},
                {"key": "polish", "label": "Polish", "weight": 20},
            ],
        })
        await self.commit()
        partners = [p.name for p in self.c.partners] or [None]
        tracks = [
            await comp.add_track(self.s, c, {"name": "Open track", "description": "Any genre, any engine, the monsoon theme.", "sponsor": partners[0], "prize": "₹10,000 and a month of free hours"}),
            await comp.add_track(self.s, c, {"name": "Malayalam story track", "description": "A game told in Malayalam.", "sponsor": partners[min(1, len(partners) - 1)], "prize": "₹5,000"}),
        ]
        await self.commit()
        names = ["Kayal Pixels", "Thattukada Studios", "Monsoon Arcade", "Ottamooli Games", "Kappa Engine", "Chenda Beats",
                 "Vaikom Vector", "Backwater Bits", "Palm Leaf Labs", "Mazha Mode"]
        pool = [r for r in ev_ctx["attended"]]
        rnd.shuffle(pool)
        teams = []
        for i, name in enumerate(names):
            size = rnd.choice([2, 3, 3, 4])
            if len(pool) < size:
                break
            members, pool = pool[:size], pool[size:]
            team = await comp.create_team(self.s, c, ev, name, tracks[i % 2].id, [m.id for m in members])
            await self.commit()
            teams.append(team)
        for team in teams:
            slug = team.name.lower().replace(" ", "-")
            await comp.put_submission(self.s, c, team, {
                "build": f"https://{slug}.itch.io/monsoon", "video": f"https://youtu.be/{hashlib.md5(slug.encode()).hexdigest()[:11]}",
                **({"repo": f"https://github.com/{slug}/monsoon-jam"} if rnd.random() < 0.7 else {}),
            })
        await self.commit()
        await comp.close_submissions(self.s, c, True)
        await self.commit()
        judges = [await comp.add_judge(self.s, c, {"name": n, "email": address_of(n, self.c.slug)})
                  for n in ("Anu Mathew", "Rohit Varghese", "Sneha Iyer")]
        await self.commit()
        quality = {t.id: rnd.uniform(4.5, 9.2) for t in teams}
        for j in judges:
            for t in teams:
                base = quality[t.id]
                scores = {k: max(0, min(10, round(base + rnd.uniform(-1.4, 1.4)))) for k in ("fun", "theme", "art", "polish")}
                await comp.put_score(self.s, c, j, t, scores, rnd.choice([None, "Strong core loop.", "Lovely art; the ending needs work.", "Ran well on the lab machines."]))
            await self.commit()
        for stage, size in (("shortlisted", 5), ("finalist", 3)):
            try:
                await comp.advance(self.s, c, stage, size)
                await self.commit()
            except Exception:  # a tie at the cut is the organiser's call, not the seed's
                await self.s.rollback()
        mentors = [await comp.add_mentor(self.s, c, {"name": "Sebin Kurian", "topic": "Scope and finishing"}),
                   await comp.add_mentor(self.s, c, {"name": "Lekha Ramanathan", "topic": "Game feel and juice"})]
        await self.commit()
        for m in mentors:
            await comp.make_slots(self.s, c, m, start + timedelta(hours=18), start + timedelta(hours=21))
            await self.commit()
            await self.s.refresh(m, ["slots"]) if hasattr(m, "slots") else None
            from sqlalchemy import select

            from app.api.competitions.models import CompetitionSlots

            slots = list((await self.s.scalars(select(CompetitionSlots).where(CompetitionSlots.mentor_id == m.id).order_by(CompetitionSlots.starts_at))).all())
            for slot, team in zip(slots[::2], teams):
                await comp.set_slot(self.s, c, slot, team.id)
            await self.commit()
        self.counts["showcase_competition_teams"] = len(teams)

    async def quiz(self, e, ev_ctx) -> None:
        """A finished quiz in the live room, with its leaderboard."""
        from app.api.live.models import LiveQuizAnswers, LiveQuizPlayers, LiveQuizQuestions, LiveQuizzes, QuizStatus

        ev, start = ev_ctx["event"], ev_ctx["start"]
        rnd = random.Random(f"quiz:{e.slug}")
        questions = [
            ("Which year did the first Valorant Champions take place?", ["2019", "2020", "2021", "2022"], "C"),
            ("In Counter-Strike, how many rounds win a regulation match?", ["13", "15", "16", "12"], "A"),
            ("Which studio made Hollow Knight?", ["Team Cherry", "Supergiant", "Motion Twin", "ConcernedApe"], "A"),
            ("What does LAN stand for?", ["Large Area Node", "Local Area Network", "Linked Access Net", "Low Ack Network"], "B"),
            ("Which of these is a Kerala-made game studio's favourite snack at Respawn?", ["Kappa", "Puffs", "Pazhampori", "All of them"], "D"),
        ]
        opened = start + timedelta(hours=4, minutes=30)
        q = LiveQuizzes(event_id=ev.id, title="Lunch-break trivia", intro="Five questions, twenty seconds each.", status=QuizStatus.ended,
                        current_index=len(questions) - 1, ended_at=opened + timedelta(minutes=6), created_at=opened - timedelta(days=1), updated_at=opened)
        self.s.add(q)
        await self.s.flush()
        rows = []
        for i, (prompt, options, correct) in enumerate(questions):
            row = LiveQuizQuestions(quiz_id=q.id, prompt=prompt, options=[{"key": k, "label": o} for k, o in zip("ABCD", options)],
                                    correct_key=correct, seconds=20, sort_order=i)
            self.s.add(row)
            rows.append(row)
        await self.s.flush()
        players = rnd.sample(ev_ctx["attended"], k=min(28, len(ev_ctx["attended"])))
        for reg in players:
            skill = rnd.uniform(0.3, 0.95)
            player = LiveQuizPlayers(quiz_id=q.id, name=reg.full_name.split()[0], player_key=uuid.uuid4().hex, registration_id=reg.id, score=0)
            self.s.add(player)
            await self.s.flush()
            for i, row in enumerate(rows):
                right = rnd.random() < skill
                key = row.correct_key if right else rnd.choice([k for k in "ABCD" if k != row.correct_key])
                secs = rnd.uniform(2, 19)
                points = round(1000 * (1 - secs / 40)) if right else 0
                player.score += points
                self.s.add(LiveQuizAnswers(question_id=row.id, player_id=player.id, option_key=key, correct=right, points=points,
                                           answered_at=opened + timedelta(seconds=i * 60 + secs)))
        await self.commit()
        self.counts["showcase_quiz_players"] = len(players)

    async def live_updates(self, e, ev_ctx) -> None:
        """What the hosts told the room on the day, as the room's updates and the guests' inbox lines."""
        from app.api.live.models import Audience, LiveUpdates, UpdateKind
        from app.api.notifications.models import Notifications, NotificationStatus

        ev, start = ev_ctx["event"], ev_ctx["start"]
        lines = [
            (UpdateKind.room, "Matches move to Lab 2 after lunch: the projector in Lab 1 is resting.", 3.5),
            (UpdateKind.schedule, "Semi-finals start 15 minutes late. Grab chai.", 6.0),
            (UpdateKind.general, "Grand final is live on the stream. Seats left near the front.", 8.1),
        ] if e.when.duration_hours >= 6 else [(UpdateKind.general, "We're starting in ten minutes. Find a seat!", 0.0)]
        attended = ev_ctx["attended"]
        for kind, text, hours in lines:
            at = start + timedelta(hours=hours)
            self.s.add(LiveUpdates(event_id=ev.id, kind=kind, text=text, audience=Audience.checked_in, recipients=len(attended),
                                   pushed=len(attended), sent_by=self.owner.id, sent_at=at))
            for reg in attended:
                self.s.add(Notifications(user_id=reg.user_id, title=ev.name, description=text, type="event_live_update",
                                         from_club_id=self.club.id, event_id=ev.id, data={"kind": kind.value},
                                         status=NotificationStatus.read if self.rnd.random() < 0.7 else NotificationStatus.unread,
                                         created_at=at, updated_at=at))
        await self.commit()
        self.counts["showcase_live_updates"] += len(lines)

    async def mail_history(self, e, ev_ctx) -> None:
        """The event's journey emails, as the Emails page makes them, and
        what went out: tickets on registration, certificates after, the
        journey's sends and one message to everyone -- every row already
        sent, so nothing waits for the sweep."""
        from sqlalchemy import select

        from app.api.deliveries.models import DeliveryChannel, DeliveryKind, DeliveryOutcome, DeliveryStatus, Deliveries
        from app.api.emails import service as emails
        from app.api.emails.models import EmailStage, MessageDeliveries, MessageKind, Messages, MessageStatus

        ev, start, end = ev_ctx["event"], ev_ctx["start"], ev_ctx["end"]
        rnd = random.Random(f"mail:{e.slug}")
        templates = await emails.ensure_event_templates(self.s, ev)
        await self.commit()
        regs = ev_ctx["regs"]

        def outcome():
            roll = rnd.random()
            if roll < 0.02:
                return DeliveryOutcome.bounced
            return DeliveryOutcome.read if roll < 0.62 else DeliveryOutcome.delivered

        for reg in regs:
            sent = (reg.created_at or start) + timedelta(seconds=rnd.randint(4, 90))
            o = outcome()
            self.s.add(Deliveries(event_id=ev.id, registration_id=reg.id, kind=DeliveryKind.ticket, channel=DeliveryChannel.email,
                                  status=DeliveryStatus.sent, outcome=o, outcome_at=sent + timedelta(minutes=rnd.randint(1, 600)),
                                  attempts=1, tries=1, sent_at=sent, queued_at=sent, design_version=1, created_at=sent, updated_at=sent))
            if e.past and reg.certificate_issued_at is not None:
                at = reg.certificate_issued_at + timedelta(minutes=rnd.randint(2, 30))
                self.s.add(Deliveries(event_id=ev.id, registration_id=reg.id, kind=DeliveryKind.certificate, channel=DeliveryChannel.email,
                                      status=DeliveryStatus.sent, outcome=outcome(), outcome_at=at + timedelta(hours=2), attempts=1, tries=1,
                                      sent_at=at, queued_at=at, design_version=1, created_at=at, updated_at=at))
        await self.commit()
        self.counts["showcase_deliveries"] += len(regs)

        async def message(kind, subject, body, at, template=None, audience="ALL", scheduled=False, to=None):
            to = regs if to is None else to
            m = Messages(event_id=ev.id, kind=kind, template_id=template.id if template else None, scheduled=scheduled, subject=subject,
                         body=body, audience=audience, roles=[], channels=["email"], status=MessageStatus.SENT, recipients=len(to),
                         sent_count=len(to), created_by=self.owner.id, created_at=at, updated_at=at)
            self.s.add(m)
            await self.s.flush()
            for reg in to:
                o = outcome()
                self.s.add(MessageDeliveries(message_id=m.id, registration_id=reg.id, channel=DeliveryChannel.email, status=DeliveryStatus.sent,
                                             outcome=o, outcome_at=at + timedelta(minutes=rnd.randint(1, 300)), sent_at=at, attempts=1,
                                             created_at=at, updated_at=at))
            return m

        if e.past:
            by_stage = {t.stage: t for t in templates}
            for stage, when in ((EmailStage.PRE_EVENT, start - timedelta(days=1)), (EmailStage.REMINDER, start - timedelta(minutes=30)),
                                (EmailStage.POST_EVENT, end + timedelta(hours=20))):
                t = by_stage.get(stage)
                if t is not None:
                    await message(MessageKind.TEMPLATE, t.subject or t.name, "", when, template=t, scheduled=True,
                                  to=ev_ctx["attended"] if stage is EmailStage.POST_EVENT else regs)
            await message(MessageKind.BROADCAST, f"{ev.name}: entry, parking and what to bring",
                          "Doors open 30 minutes early. Bring your college ID and a charged phone; parking is behind the main block.",
                          start - timedelta(days=2, hours=3))
            self.counts["showcase_messages"] += 4
        else:
            # Upcoming: the journey stays as the Emails page sets it up. Its
            # timed sends go to registrants at the demo domain only, which the
            # mailer skips; with anyone else on the list, it is switched off.
            if any(not r.email.lower().endswith(DEMO_DOMAIN) for r in regs):
                for t in templates:
                    t.enabled = False
            await message(MessageKind.BROADCAST, f"{ev.name}: you're in", "Thanks for registering. Your ticket is in the app; see you there.",
                          min(self.now - timedelta(hours=6), start - timedelta(days=1)))
            self.counts["showcase_messages"] += 1
        await self.commit()
        _ = select

    async def closeout(self, e, ev_ctx) -> None:
        from app.api.closeout import service as closeout

        await closeout.generate(self.s, ev_ctx["event"], self.owner)
        await self.commit()
        self.counts["showcase_closeout_reports"] += 1

    # ── Club-wide ────────────────────────────────────────────────────────

    async def links(self, followers: list[int], events: list[tuple]) -> None:
        """Tracking links -- on the club page and on the events to come --
        with their clicks over six weeks, and the follows and registrations
        they brought."""
        from sqlalchemy import select, update

        from app.api.clubs.models import ClubUsersLink
        from app.api.events.models import EventRegistrationsLink as Reg
        from app.api.links import service as links
        from app.api.links.models import TrackingClicks, TrackingLinks
        from app.api.links.schemas import LinkCreate

        made = []
        for name, src, med in (("Instagram bio", "instagram", "social"), ("College WhatsApp groups", "whatsapp", "chat")):
            made.append((await links.create(self.s, self.club, None, LinkCreate(name=name, utm_source=src, utm_medium=med), self.owner), None))
        for e, ev_ctx in events:
            if not e.past:
                ev = ev_ctx["event"]
                made.append((await links.create(self.s, self.club, ev, LinkCreate(name="Poster QR", utm_source="poster", utm_medium="print",
                                                                                   utm_campaign=e.slug[:60]), self.owner), ev_ctx))
                made.append((await links.create(self.s, self.club, ev, LinkCreate(name="Instagram story", utm_source="instagram", utm_medium="story",
                                                                                   utm_campaign=e.slug[:60]), self.owner), ev_ctx))
        await self.commit()
        website = await self.s.scalar(select(TrackingLinks).where(TrackingLinks.club_id == self.club.id, TrackingLinks.kind == "website"))
        if website is not None:
            made.append((website, None))
        clicks = 0
        rnd = self.rnd
        follow_rows = list((await self.s.execute(select(ClubUsersLink.id, ClubUsersLink.created_at).where(
            ClubUsersLink.club_id == self.club.id, ClubUsersLink.created_at > self.now - timedelta(days=40)))).all())
        rnd.shuffle(follow_rows)
        for link, ev_ctx in made:
            start = max(self.now - timedelta(days=42), link.created_at or self.founded)
            if ev_ctx is not None:
                start = max(start, ev_ctx["made"])
            n = rnd.randint(60, 260)
            visitors = [hashlib.sha256(f"{link.id}:{i}".encode()).hexdigest()[:32] for i in range(int(n * 0.7))]
            for _ in range(n):
                at = start + (self.now - start) * rnd.random()
                self.s.add(TrackingClicks(link_id=link.id, visitor=rnd.choice(visitors), referrer=rnd.choice(REFERRERS),
                                          user_agent=rnd.choice(UAS), created_at=at))
            clicks += n
            if ev_ctx is not None:
                # Registrations the link brought: some of those made after it went up.
                regs = [r for r in ev_ctx["regs"] if r.created_at and r.created_at > start]
                for reg in rnd.sample(regs, k=min(len(regs), rnd.randint(4, 14))):
                    await self.s.execute(update(Reg).where(Reg.id == reg.id).values(tracking_link_id=link.id))
            else:
                for row_id, _at in follow_rows[:rnd.randint(8, 30)]:
                    await self.s.execute(update(ClubUsersLink).where(ClubUsersLink.id == row_id).values(tracking_link_id=link.id))
                follow_rows = follow_rows[30:]
        await self.commit()
        self.counts["showcase_tracking_links"] = len(made)
        self.counts["showcase_link_clicks"] = clicks

    async def announcements(self, events: list[tuple]) -> None:
        """Announcements to each event's people, sent over the last weeks,
        most of them read -- as the Announcements page counts them."""
        from sqlalchemy import update

        from app.api.notifications.models import Notifications, NotificationStatus
        from app.api.notifications.service import create_notifications_batch

        texts = {
            True: [("Doors at 9:30 tomorrow", "Bring your college ID; the lab opens at 9:30 and matches start at 10 sharp."),
                   ("Thank you for coming!", "Photos and the full bracket are up on our page. See you next season.")],
            False: [("Registrations are open", "Seats are going fast. Register in the app and bring your squad."),
                    ("Schedule is out", "The full run of show is on the event page. Plan your evening around the final.")],
        }
        sent = 0
        for e, ev_ctx in events:
            ev, start = ev_ctx["event"], ev_ctx["start"]
            people = [r.user_id for r in ev_ctx["regs"]]
            for i, (title, body) in enumerate(texts[e.past]):
                if e.past:
                    at = start - timedelta(hours=14) if i == 0 else ev_ctx["end"] + timedelta(hours=20)
                else:
                    at = max(ev_ctx["made"] + timedelta(days=1), self.now - timedelta(days=12 - i * 5))
                    if at >= self.now:
                        continue
                at = at.replace(second=0, microsecond=0)
                await create_notifications_batch(self.s, people, f"{ev.name}: {title}"[:200], body, "club_announcement",
                                                 data={"audience": "all", "has_image": False}, from_club_id=self.club.id, event_id=ev.id)
                await self.s.execute(update(Notifications).where(
                    Notifications.event_id == ev.id, Notifications.type == "club_announcement", Notifications.description == body
                ).values(created_at=at, updated_at=at))
                await self.s.execute(update(Notifications).where(
                    Notifications.event_id == ev.id, Notifications.type == "club_announcement", Notifications.description == body,
                    Notifications.user_id.in_([u for u in people if hash((u, title)) % 10 < 7]),
                ).values(status=NotificationStatus.read, updated_at=at + timedelta(minutes=self.rnd.randint(3, 50))))
                await self.commit()
                sent += 1
        self.counts["showcase_announcements"] = sent

    async def finance(self, events: list[tuple]) -> None:
        """The money that is not tickets: what each event cost, and the
        sponsorship and grant that paid for some of it."""
        from sqlalchemy import update

        from app.api.finance import service as finance
        from app.api.finance.models import ClubExpenses
        from app.api.finance.schemas import ExpenseCreate

        partners = [p.name for p in self.c.partners] or ["A sponsor"]
        rows = []
        for e, ev_ctx in events:
            ev, start = ev_ctx["event"], ev_ctx["start"]
            day = start.date() if e.past else min(self.now.date(), start.date())
            big = e.capacity >= 80
            items = [
                ("expense", "food", "Lunch and chai from Kappa Corner" if big else "Chai and snacks", 18 * e.capacity if big else 9 * e.capacity),
                ("expense", "printing", "Posters, wristbands and ID card stock", 2400 if big else 900),
                ("expense", "equipment", "Router, switches and spare headsets", 3800 if big else 0),
                ("expense", "prizes", "Trophies and winners' prize money", 15000 if big and e.fee else 0),
                ("expense", "marketing", "Instagram promotion", 1500),
                ("income", "sponsorship", f"Sponsorship from {partners[0]}", 15000 if big else 5000),
            ]
            if not e.past:
                items = items[1:3] + items[-1:]
            for kind, category, title, amount in items:
                if amount <= 0:
                    continue
                rows.append((ev.id, kind, category, title, amount, day - timedelta(days=self.rnd.randint(0, 6))))
        rows.append((None, "income", "grant", "College fest fund, semester grant", 25000, self.now.date() - timedelta(days=70)))
        rows.append((None, "expense", "equipment", "Two new monitors for the lab", 21800, self.now.date() - timedelta(days=55)))
        rows.append((None, "income", "sponsorship", f"Season sponsorship from {partners[min(1, len(partners) - 1)]}", 30000, self.now.date() - timedelta(days=40)))
        for event_id, kind, category, title, amount, on in rows:
            out = await finance.create_expense(self.s, self.club, ExpenseCreate(event_id=event_id, kind=kind, title=title, category=category,
                                                                                amount=amount, incurred_on=on), self.owner)
            at = datetime.combine(on, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=12)
            await self.s.execute(update(ClubExpenses).where(ClubExpenses.id == uuid.UUID(out["id"])).values(created_at=at, updated_at=at))
        await self.commit()
        self.counts["showcase_finance_entries"] = len(rows)

    async def designs(self) -> None:
        from scripts.demo import designs

        keys = self.sd.design_keys.get(self.club.id) or {}
        await designs.save_more_designs(self.s, club=self.club, owner=self.owner, brand=self.c.brand, mark_key=keys["mark"])
        self.counts["designs"] += 2


def page_views(registrations: int, rnd: random.Random) -> int:
    """How many saw the event page for this many registrations (about one in
    six to one in twelve register)."""
    return int(registrations * rnd.uniform(6, 12)) + rnd.randint(30, 140)

