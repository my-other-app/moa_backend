"""Fill a scratch database with a year-three MyOtherApp: 1,500 clubs, 37,000
events, 60,000 users, 400,000 registrations, 150,000 follows, 20,000 posts.

    createdb moa_bench
    BENCH_DB=moa_bench python -m scripts.bench.seed            # schema by alembic, then data
    BENCH_DB=moa_bench python -m scripts.bench.seed --no-schema

Rows go in through the models' tables in bulk (Core executemany, so the
models' Python-side defaults apply), never one ORM add at a time. Every
number is drawn from a seeded generator, so two runs build the same world
relative to the moment they run. The spread is meant to look like the real
thing: clubs in twenty cities, most with a point and some placed only by
their events; events skewed towards the coming weeks, a few online, some paid;
registrations and follows long-tailed, so a handful of events have thousands
and most have a few.
"""
from __future__ import annotations

import argparse
import itertools
import math
import random
import time
import uuid
from datetime import datetime, timedelta, timezone

from scripts.bench import _env  # noqa: F401  (settings before app)

from sqlalchemy import create_engine, insert, text

from app.api.closeout.models import EventStatus
from app.api.clubs.models import ClubInterestsLink, ClubPartners, Clubs, ClubSocials, ClubTeamMembers, ClubUsersLink
from app.api.events.models import EventCategories, EventInterestsLink, EventRegistrationsLink, Events, EventSpeakers
from app.api.interests.models import InterestCategory, Interests
from app.api.orgs.models import Organizations, OrgTypes
from app.api.sites.models import ClubSites
from app.api.threads.models import ClubThreads, ThreadComments, ThreadLikes, ThreadPollOptions
from app.api.users.models import UserInterests, UserProfiles, Users, UserTypes
from app.config import settings
from app.db import registry  # noqa: F401

SEED = 42
N_CLUBS = 1500
N_UPCOMING = 12000
N_PAST = 25000
N_USERS = 60000
N_REGISTRATIONS = 400000
N_FOLLOWS = 150000
N_THREADS = 20000
N_LIKES = 120000
N_COMMENTS = 40000
BENCH_USER_EMAIL = "bench.user@example.com"
# One bcrypt hash for every seeded account: nobody signs in with a password here.
PASSWORD = "$2b$12$C6UzMDM.H6dfI/f/IKcEeO7cS0x0nY1E6J6y5G7m4FZp3o3QG7uTe"

CITIES = [
    ("Bengaluru", 12.9716, 77.5946), ("Mumbai", 19.0760, 72.8777), ("Delhi", 28.6139, 77.2090),
    ("Hyderabad", 17.3850, 78.4867), ("Chennai", 13.0827, 80.2707), ("Kolkata", 22.5726, 88.3639),
    ("Pune", 18.5204, 73.8567), ("Ahmedabad", 23.0225, 72.5714), ("Jaipur", 26.9124, 75.7873),
    ("Kochi", 9.9312, 76.2673), ("Thiruvananthapuram", 8.5241, 76.9366), ("Kozhikode", 11.2588, 75.7804),
    ("Mysuru", 12.2958, 76.6394), ("Coimbatore", 11.0168, 76.9558), ("Chandigarh", 30.7333, 76.7794),
    ("Lucknow", 26.8467, 80.9462), ("Bhopal", 23.2599, 77.4126), ("Indore", 22.7196, 75.8577),
    ("Bhubaneswar", 20.2961, 85.8245), ("Guwahati", 26.1445, 91.7362),
]
# Bigger cities hold more of everything.
CITY_WEIGHTS = [14, 12, 12, 9, 8, 6, 8, 5, 4, 5, 3, 3, 3, 3, 3, 2, 2, 2, 2, 2]

TOPICS = ["Robotics", "AI", "Machine Learning", "Web Dev", "Startups", "Design", "Photography", "Music", "Dance", "Theatre",
          "Film", "Debate", "Quiz", "Literature", "Poetry", "Chess", "Football", "Cricket", "Basketball", "Badminton",
          "Yoga", "Running", "Cycling", "Trekking", "Climate", "Volunteering", "Finance", "Investing", "Marketing", "Law",
          "Medicine", "Biotech", "Space", "Astronomy", "Electronics", "IoT", "Drones", "Cybersecurity", "Blockchain", "Gaming",
          "Esports", "Anime", "Comics", "Cooking", "Baking", "Fashion", "Art", "Painting", "Sculpture", "Languages",
          "Public Speaking", "Entrepreneurship", "Product", "Data Science", "Open Source", "Mobile Apps", "Cloud", "DevOps", "UX", "Writing"]
FORMATS = ["Workshop", "Meetup", "Hackathon", "Bootcamp", "Talk", "Panel", "Fest", "Jam", "Night", "Summit", "Sprint",
           "Masterclass", "Open Mic", "Tournament", "Expo", "Session", "Circle", "Showcase", "Retreat", "Crash Course"]
ADJECTIVES = ["Annual", "Intro to", "Advanced", "Weekend", "Monsoon", "Winter", "Spring", "Grand", "Mini", "Mega", "Late Night",
              "Hands-on", "Beginner", "Pro", "Community", "Inter-college", "Open", "Flagship", "Rapid", "Deep Dive"]
CLUB_SUFFIX = ["Society", "Club", "Collective", "Circle", "Guild", "League", "Lab", "Crew", "Hub", "Forum", "Network", "Union"]
CLUB_PREFIX = ["", "The ", "IIT ", "NIT ", "College ", "City ", "Campus ", "Young ", "United ", "Open "]
VENUES = ["Auditorium", "Seminar Hall", "Open Air Theatre", "Main Block", "Innovation Centre", "Library Hall", "Convention Centre",
          "Tech Park", "Community Hall", "Cafe", "Co-working Space", "Sports Complex", "Lab 3", "Block B", "Amphitheatre"]
CATEGORIES = [("Tech", "💻"), ("Music", "🎵"), ("Dance", "💃"), ("Sports", "⚽"), ("Arts", "🎨"), ("Business", "💼"),
              ("Science", "🔬"), ("Literature", "📚"), ("Gaming", "🎮"), ("Social", "🤝"), ("Wellness", "🧘"), ("Film", "🎬")]


def zipf_weights(n: int, s: float = 1.1) -> list[float]:
    return [1.0 / (i + 1) ** s for i in range(n)]


def cumulative(weights: list[float]) -> list[float]:
    """For random.choices(cum_weights=...): O(log n) a draw instead of O(n)."""
    return list(itertools.accumulate(weights))


def jitter(rng: random.Random, lat: float, lng: float, km: float) -> tuple[float, float]:
    """A point within about `km` of (lat, lng), uniform over the disc."""
    r = km * math.sqrt(rng.random())
    t = rng.random() * 2 * math.pi
    dlat = (r * math.cos(t)) / 111.2
    dlng = (r * math.sin(t)) / (111.2 * math.cos(math.radians(lat)))
    return round(lat + dlat, 6), round(lng + dlng, 6)


def image_key(rng: random.Random, folder: str) -> str:
    return f"{folder}{uuid.UUID(int=rng.getrandbits(128))}.png"


class Seeder:
    def __init__(self, engine, now: datetime):
        self.engine = engine
        self.now = now
        self.rng = random.Random(SEED)

    def put(self, conn, model_or_table, rows: list[dict], chunk: int = 5000) -> None:
        table = getattr(model_or_table, "__table__", model_or_table)
        for i in range(0, len(rows), chunk):
            conn.execute(insert(table), rows[i:i + chunk])

    def run(self) -> dict:
        rng, now = self.rng, self.now
        t0 = time.perf_counter()
        stamps = {}
        with self.engine.begin() as conn:
            # ── Reference data ──
            # The categories the product ships come with the schema; clubs add their own below.
            shipped = list(conn.execute(text("SELECT id FROM event_categories WHERE created_by_id IS NULL ORDER BY id")).scalars())
            if not shipped:
                self.put(conn, EventCategories, [dict(id=i + 1, name=n, icon=ic, created_by_id=None) for i, (n, ic) in enumerate(CATEGORIES)])
                shipped = list(range(1, len(CATEGORIES) + 1))
            cat_rows = []
            next_cat = max(shipped) + 1
            icat_rows = [dict(id=i + 1, name=f"Group {i + 1}") for i in range(6)]
            self.put(conn, InterestCategory, icat_rows)
            interest_rows = [dict(id=i + 1, name=t, icon="✨", category_id=(i % 6) + 1) for i, t in enumerate(TOPICS)]
            self.put(conn, Interests, interest_rows)
            org_rows = [dict(id=i + 1, name=f"Institute {i + 1}", type=rng.choice([OrgTypes.college, OrgTypes.university, OrgTypes.community])) for i in range(80)]
            self.put(conn, Organizations, org_rows)

            # ── Users ──
            users = []
            for i in range(1, N_USERS + 1):
                email = BENCH_USER_EMAIL if i == 1 else f"user{i}@example.com"
                users.append(dict(
                    id=i, full_name=f"User {i}", username=f"user{i}", email=email, password=PASSWORD,
                    user_type=UserTypes.app_user, created_at=now - timedelta(days=rng.randint(0, 1100)),
                ))
            # Club owners: one account per club, after the app users.
            owner_base = N_USERS
            for j in range(1, N_CLUBS + 1):
                users.append(dict(id=owner_base + j, full_name=f"Owner {j}", username=f"owner{j}", email=f"owner{j}@example.com",
                                  password=PASSWORD, user_type=UserTypes.club))
            self.put(conn, Users, users)
            self.put(conn, UserProfiles, [
                dict(user_id=u["id"], full_name=u["full_name"], org_id=(rng.randint(1, 80) if rng.random() < 0.5 else None))
                for u in users[:N_USERS]
            ])
            ui = []
            for uid in range(1, N_USERS + 1):
                k = 5 if uid == 1 else rng.choice([0, 1, 2, 3, 3, 4, 5, 6])
                for iid in rng.sample(range(1, len(TOPICS) + 1), k):
                    ui.append(dict(user_id=uid, interest_id=iid))
            self.put(conn, UserInterests, ui)
            stamps["users"] = time.perf_counter() - t0

            # ── Categories: the shipped ones, and clubs' own ──
            for j in range(30):
                cat_rows.append(dict(id=next_cat + j, name=f"{rng.choice(TOPICS)} {rng.choice(['Nights', 'Series', 'Specials'])}"[:50],
                                     icon=None, created_by_id=owner_base + rng.randint(1, N_CLUBS)))
            self.put(conn, EventCategories, cat_rows)
            own_cats = [c["id"] for c in cat_rows]

            # ── Clubs ──
            clubs = []
            club_city = {}
            for j in range(1, N_CLUBS + 1):
                ci = rng.choices(range(len(CITIES)), weights=CITY_WEIGHTS)[0]
                city, clat, clng = CITIES[ci]
                club_city[j] = ci
                topic = rng.choice(TOPICS)
                name = f"{rng.choice(CLUB_PREFIX)}{topic} {rng.choice(CLUB_SUFFIX)} {j}"
                own_point = rng.random() < 0.8
                lat, lng = jitter(rng, clat, clng, 15) if own_point else (None, None)
                clubs.append(dict(
                    id=j, slug=f"club-{j}-{topic.lower().replace(' ', '-')}", name=name[:100],
                    logo=image_key(rng, "clubs/logos/") if rng.random() < 0.85 else None,
                    cover=image_key(rng, "clubs/covers/") if rng.random() < 0.5 else None,
                    about=f"We are a {topic.lower()} club in {city}. " * 4,
                    tagline=f"{topic} for everyone in {city}"[:120] if rng.random() < 0.8 else None,
                    location_name=f"{rng.choice(VENUES)}, {city}" if rng.random() < 0.9 else None,
                    location_link=(f"https://maps.google.com/?q={lat},{lng}" if own_point else None),
                    lat=lat, lng=lng, org_id=rng.randint(1, 80) if rng.random() < 0.5 else None,
                    user_id=owner_base + j, contact_email=f"club{j}@example.com",
                    is_verified=rng.random() < 0.95, is_deleted=rng.random() < 0.01,
                    created_at=now - timedelta(days=rng.randint(1, 1100)),
                ))
            self.put(conn, Clubs, clubs)
            ci_rows = []
            for c in clubs:
                for iid in rng.sample(range(1, len(TOPICS) + 1), rng.randint(1, 5)):
                    ci_rows.append(dict(club_id=c["id"], interest_id=iid))
            self.put(conn, ClubInterestsLink, ci_rows)
            self.put(conn, ClubSocials, [dict(club_id=c["id"], instagram=f"club{c['id']}", website=f"https://club{c['id']}.example") for c in clubs if rng.random() < 0.6])
            team = []
            for c in clubs:
                for k in range(rng.randint(0, 8)):
                    team.append(dict(club_id=c["id"], name=f"Member {c['id']}-{k}", role=rng.choice(["Lead", "Co-lead", "Events", "Design", "Tech"]), sort_order=k,
                                     photo=image_key(rng, "clubs/team/") if rng.random() < 0.5 else None))
            self.put(conn, ClubTeamMembers, team)
            self.put(conn, ClubPartners, [dict(club_id=c["id"], name=f"Partner {c['id']}", role="Title partner") for c in clubs if rng.random() < 0.3])
            self.put(conn, ClubSites, [dict(club_id=c["id"], sections={}, published_at=now - timedelta(days=5)) for c in clubs if rng.random() < 0.1])
            stamps["clubs"] = time.perf_counter() - t0

            # ── Events ──
            club_w = cumulative(zipf_weights(N_CLUBS, 0.8))
            club_order = list(range(1, N_CLUBS + 1))
            rng.shuffle(club_order)
            events = []
            ev_interest = []
            for k in range(N_UPCOMING + N_PAST):
                eid = k + 1
                upcoming = k < N_UPCOMING
                club_id = rng.choices(club_order, cum_weights=club_w)[0]
                city, clat, clng = CITIES[club_city[club_id]]
                if upcoming:
                    # Most within the next six weeks; some already under way.
                    if rng.random() < 0.02:
                        start = now - timedelta(minutes=rng.randint(10, 300))
                    else:
                        start = now + timedelta(hours=min(rng.expovariate(1 / (24 * 25)), 24 * 240))
                else:
                    start = now - timedelta(hours=rng.uniform(24 * 3, 24 * 365 * 3))
                long_one = rng.random() < 0.03
                duration = rng.choice([24.0, 48.0, 72.0]) if long_one else rng.choice([1.0, 1.5, 2.0, 2.0, 3.0, 3.0, 4.0, 6.0, 8.0])
                r = rng.random()
                is_online = r < 0.15
                has_point = (not is_online) and r < 0.90
                lat = lng = None
                if has_point:
                    lat, lng = jitter(rng, clat, clng, 25)
                    link = f"https://www.google.com/maps/place/Venue/@{lat},{lng},17z"
                elif not is_online and rng.random() < 0.3:
                    link = f"{rng.choice(VENUES)}, {city}"  # a place written out, not a map link
                else:
                    link = None
                if upcoming:
                    if start < now:
                        status = EventStatus.live
                    else:
                        status = EventStatus.draft if rng.random() < 0.04 else EventStatus.published
                    clock = True
                else:
                    ended_days = (now - start).days
                    if rng.random() < 0.03:
                        status, clock = EventStatus.published, False  # the organiser turned the clock off
                    else:
                        status, clock = (EventStatus.closed if ended_days < 30 else EventStatus.archived), True
                has_fee = rng.random() < 0.3
                topic = rng.choice(TOPICS)
                fmt = rng.choice(FORMATS)
                name = f"{rng.choice(ADJECTIVES)} {topic} {fmt}"
                if rng.random() < 0.3:
                    name += f" {city}"
                name = name[:95] + f" {eid}" if len(name) > 95 else name
                reg_end = None
                if rng.random() < 0.5:
                    reg_end = start - timedelta(hours=rng.choice([1, 6, 12, 24, 48, 72]))
                events.append(dict(
                    id=eid, slug=f"e{eid}-{topic.lower().replace(' ', '-')}-{fmt.lower().replace(' ', '-')}", name=name,
                    poster=image_key(rng, "clubs/logos/") if rng.random() < 0.9 else None,
                    images=[], event_datetime=start, duration=duration, has_fee=has_fee,
                    reg_fee=rng.choice([49, 99, 199, 299, 499, 999]) if has_fee else None,
                    about=f"Join us for a {fmt.lower()} on {topic.lower()}. " * 6,
                    location_name=("Online" if is_online else f"{rng.choice(VENUES)}, {city}"),
                    location_link=link, lat=lat, lng=lng, is_online=is_online,
                    reg_startdate=start - timedelta(days=30), reg_enddate=reg_end,
                    max_participants=rng.choice([None, None, 50, 100, 150, 200, 300, 500, 1000, 2000]),
                    page_views=int(rng.paretovariate(1.3) * 20) if rng.random() < 0.9 else 0,
                    category_id=rng.choice(shipped) if rng.random() < 0.9 else rng.choice(own_cats),
                    club_id=club_id, status=status, status_clock=clock,
                    is_deleted=rng.random() < 0.02,
                    created_at=min(start, now) - timedelta(days=rng.randint(1, 40)),
                    updated_at=min(start, now),
                ))
                for iid in rng.sample(range(1, len(TOPICS) + 1), rng.randint(1, 3)):
                    ev_interest.append(dict(event_id=eid, interest_id=iid))
            self.put(conn, Events, events)
            self.put(conn, EventInterestsLink, ev_interest)
            self.put(conn, EventSpeakers, [
                dict(event_id=e["id"], name=f"Speaker {e['id']}-{s}", designation="Engineer", display_order=s)
                for e in events if rng.random() < 0.25 for s in range(rng.randint(1, 4))
            ])
            stamps["events"] = time.perf_counter() - t0

            # ── Registrations: long-tailed; a few events with thousands ──
            ev_ids = [e["id"] for e in events if not e["is_deleted"]]
            rng.shuffle(ev_ids)
            weights = zipf_weights(len(ev_ids), 1.0)
            total_w = sum(weights)
            regs = []
            n = 0
            for i, eid in enumerate(ev_ids):
                want = int(N_REGISTRATIONS * weights[i] / total_w)
                if i < 40:
                    want = max(want, 1500 + (40 - i) * 60)
                want = min(want, 6000, N_REGISTRATIONS - n)
                if want <= 0:
                    continue
                for uid in rng.sample(range(2, N_USERS + 1), want):
                    regs.append((eid, uid))
                n += want
                if n >= N_REGISTRATIONS:
                    break
            # Top up to the target with one-offs.
            seen = set(regs)
            while len(regs) < N_REGISTRATIONS:
                pair = (rng.choice(ev_ids), rng.randint(2, N_USERS))
                if pair not in seen:
                    seen.add(pair)
                    regs.append(pair)
            # The bench user's own history: twenty events at clubs they will also follow.
            bench_clubs = rng.sample(range(1, N_CLUBS + 1), 12)
            history = [e["id"] for e in events if e["club_id"] in bench_clubs][:20]
            regs.extend((eid, 1) for eid in history if (eid, 1) not in seen)
            by_id = {e["id"]: e for e in events}
            reg_rows = []
            for idx, (eid, uid) in enumerate(regs):
                e = by_id[eid]
                reg_rows.append(dict(
                    event_id=eid, user_id=uid, full_name=f"User {uid}", email=f"user{uid}@example.com",
                    ticket_id=f"MOA-B{idx:07d}", is_paid=bool(e["has_fee"]), paid_amount=e["reg_fee"] or 0, actual_amount=e["reg_fee"] or 0,
                    is_deleted=rng.random() < 0.05,
                    is_attended=e["event_datetime"] < now and rng.random() < 0.6,
                ))
            self.put(conn, EventRegistrationsLink, reg_rows, chunk=10000)
            stamps["registrations"] = time.perf_counter() - t0

            # ── Follows ──
            follow_w = cumulative(zipf_weights(N_CLUBS, 0.9))
            follow_order = list(range(1, N_CLUBS + 1))
            rng.shuffle(follow_order)
            fset = set()
            for cid in bench_clubs[:10]:
                fset.add((cid, 1))
            while len(fset) < N_FOLLOWS:
                fset.add((rng.choices(follow_order, cum_weights=follow_w)[0], rng.randint(2, N_USERS)))
            follows = []
            for cid, uid in fset:
                hidden = uid != 1 and rng.random() < 0.01
                follows.append(dict(club_id=cid, user_id=uid, is_following=not hidden and (uid == 1 or rng.random() < 0.96),
                                    is_pinned=(uid == 1 and cid == bench_clubs[0]) or rng.random() < 0.03, is_hidden=hidden,
                                    is_deleted=uid != 1 and rng.random() < 0.03))
            self.put(conn, ClubUsersLink, follows, chunk=10000)
            stamps["follows"] = time.perf_counter() - t0

            # ── Threads, likes, comments ──
            thread_w = cumulative(zipf_weights(N_CLUBS, 0.7))
            threads = []
            for k in range(N_THREADS):
                threads.append(dict(
                    id=uuid.UUID(int=rng.getrandbits(128)), club_id=rng.choices(follow_order, cum_weights=thread_w)[0],
                    body=f"Post {k}: " + "Something is happening at the club. " * rng.randint(1, 8),
                    image=image_key(rng, "clubs/threads/") if rng.random() < 0.3 else None,
                    kind="poll" if rng.random() < 0.1 else "post", pinned=rng.random() < 0.02, notify_followers=True,
                    created_at=now - timedelta(minutes=rng.randint(1, 60 * 24 * 730)), is_deleted=rng.random() < 0.02,
                ))
            likes = set()
            like_w = cumulative(zipf_weights(N_THREADS, 1.0))
            tids = [t["id"] for t in threads]
            while len(likes) < N_LIKES:
                likes.add((rng.choices(tids, cum_weights=like_w)[0], rng.randint(1, N_USERS)))
            counts: dict = {}
            for tid, _ in likes:
                counts[tid] = counts.get(tid, 0) + 1
            comments = []
            ccounts: dict = {}
            for _ in range(N_COMMENTS):
                tid = rng.choices(tids, cum_weights=like_w)[0]
                comments.append(dict(thread_id=tid, user_id=rng.randint(1, N_USERS), body="Nice one!"))
                ccounts[tid] = ccounts.get(tid, 0) + 1
            for t in threads:
                t["likes_count"] = counts.get(t["id"], 0)
                t["comments_count"] = ccounts.get(t["id"], 0)
                t["views_count"] = t["likes_count"] * 7 + rng.randint(0, 50)
            self.put(conn, ClubThreads, threads)
            self.put(conn, ThreadPollOptions, [dict(thread_id=t["id"], label=f"Option {o}", sort_order=o, votes_count=rng.randint(0, 40))
                                               for t in threads if t["kind"] == "poll" for o in range(3)])
            self.put(conn, ThreadLikes, [dict(thread_id=tid, user_id=uid) for tid, uid in likes], chunk=10000)
            self.put(conn, ThreadComments, comments, chunk=10000)
            stamps["threads"] = time.perf_counter() - t0

            # Sequences after explicit ids.
            for table in ("users", "clubs", "events", "event_categories", "interests", "interest_categories", "organizations"):
                conn.execute(text(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), (SELECT max(id) FROM {table}))"))
        with self.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text("VACUUM ANALYZE"))
        stamps["vacuum"] = time.perf_counter() - t0
        return stamps


def schema(db_url: str) -> None:
    from alembic.config import Config
    from alembic import command

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--no-schema", action="store_true", help="the schema is already there")
    args = p.parse_args()
    if not args.no_schema:
        schema(settings.DATABASE_URL_SYNC)
    engine = create_engine(settings.DATABASE_URL_SYNC)
    now = datetime.now(timezone.utc)
    stamps = Seeder(engine, now).run()
    with engine.connect() as conn:
        sizes = {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in (
            "clubs", "events", "users", "event_registrations_link", "club_users_link", "club_threads", "thread_likes", "thread_comments")}
    print("seeded at", now.isoformat())
    for k, v in stamps.items():
        print(f"  {k:<14} {v:7.1f}s")
    for k, v in sizes.items():
        print(f"  {k:<26} {v:>8}")


if __name__ == "__main__":
    main()
