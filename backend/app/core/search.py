"""Meilisearch: the client, its circuit breaker, and what the indexes are.

Off unless APP_SEARCH_URL is set; then every call here is an HTTP request to
the instance (httpx, one client per process, short timeouts), and nothing
else in the app talks to it. Postgres stays the truth and the fallback:

- A search on a request's path is `guarded`: refused while the breaker is
  open, and its outcome counted. After SEARCH_BREAKER_FAILURES failures in a
  row (a timeout, a refused connection, a 5xx, or a 4xx such as a missing
  index) the breaker opens for SEARCH_BREAKER_SECONDS and every search goes
  to Postgres without trying; then one request at a time is let through to
  see whether it is well again. A single failure costs that request one
  timeout, after which it too is answered by Postgres (app.api.search.engine).
- The sync's calls (the outbox drain, the reconcile, a rebuild) are not
  guarded: they run on one process in the background and retry on their own.

The indexes, `{SEARCH_INDEX_PREFIX}events` and `{SEARCH_INDEX_PREFIX}clubs`,
and their settings are defined here (EVENT_SETTINGS, CLUB_SETTINGS) and
applied by `apply_settings`, which changes nothing when they are already so
(app.api.search.sync applies them at startup, scripts/search_reindex.py on
demand). The documents are made in app.api.search.docs.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

EVENTS = "events"
CLUBS = "clubs"
KINDS = (EVENTS, CLUBS)

#: The most hits a search can reach (offset + limit), and what its total
#: counts up to: past discover's COUNT_CAP, so "1,000+" can be told apart,
#: and past its deepest offset (1,000) plus a page.
MAX_TOTAL_HITS = 1200


class SearchError(Exception):
    """A call to Meilisearch failed: not reached, timed out, or refused."""

    def __init__(self, message: str, *, status: Optional[int] = None, code: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.code = code


class Unavailable(SearchError):
    """Not configured, or the breaker is open: not even tried."""


def configured() -> bool:
    return bool(settings.SEARCH_URL)


def index(kind: str) -> str:
    """The live index of a kind ('events', 'clubs')."""
    return f"{settings.SEARCH_INDEX_PREFIX}{kind}"


# ─── The breaker ──────────────────────────────────────────────────────────


class Breaker:
    """Closed: calls go through. After `failures` in a row: open for
    `seconds`, calls refused. Then half open: one trial call at a time
    (a trial that never reports back is given up after `trial_seconds`);
    its success closes the breaker, its failure opens it again."""

    def __init__(self, failures: Optional[int] = None, seconds: Optional[float] = None, clock=time.monotonic):
        self._failures_to_open = failures
        self._seconds = seconds
        self.clock = clock
        self.reset()

    @property
    def failures_to_open(self) -> int:
        return max(1, self._failures_to_open or settings.SEARCH_BREAKER_FAILURES)

    @property
    def seconds(self) -> float:
        return self._seconds if self._seconds is not None else settings.SEARCH_BREAKER_SECONDS

    @property
    def trial_seconds(self) -> float:
        return max(5.0, 3 * settings.SEARCH_TIMEOUT)

    def reset(self) -> None:
        self.failures = 0
        self.open_until: Optional[float] = None
        self.trial_since: Optional[float] = None

    def state(self) -> str:
        if self.open_until is None:
            return "closed"
        return "open" if self.clock() < self.open_until else "half-open"

    def allow(self) -> bool:
        if self.open_until is None:
            return True
        now = self.clock()
        if now < self.open_until:
            return False
        if self.trial_since is not None and now - self.trial_since < self.trial_seconds:
            return False  # one trial at a time
        self.trial_since = now
        return True

    def success(self) -> None:
        if self.open_until is not None:
            logger.info("search is answering again: the breaker is closed")
        self.reset()

    def failure(self) -> None:
        self.trial_since = None
        self.failures += 1
        if self.open_until is not None or self.failures >= self.failures_to_open:
            if self.open_until is None or self.clock() >= self.open_until:
                logger.warning("search failed %s time(s) in a row: Postgres answers for %.0f s", self.failures, self.seconds)
            self.open_until = self.clock() + self.seconds


breaker = Breaker()


# ─── HTTP ─────────────────────────────────────────────────────────────────

_client: Optional[httpx.AsyncClient] = None
_client_loop = None
#: Tests put an httpx.MockTransport here (tests/fake_meili.py).
transport: Optional[httpx.AsyncBaseTransport] = None


def _http() -> httpx.AsyncClient:
    """The process's client, made on first use (and again on another event loop)."""
    global _client, _client_loop
    loop = asyncio.get_running_loop()
    if _client is None or _client.is_closed or _client_loop is not loop:
        headers = {"Content-Type": "application/json"}
        if settings.SEARCH_KEY:
            headers["Authorization"] = f"Bearer {settings.SEARCH_KEY}"
        timeout = httpx.Timeout(settings.SEARCH_TIMEOUT, connect=min(0.5, settings.SEARCH_TIMEOUT))
        _client = httpx.AsyncClient(
            base_url=settings.SEARCH_URL.rstrip("/"), headers=headers, timeout=timeout, transport=transport,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )
        _client_loop = loop
    return _client


async def reset(new_transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
    """Forget the client and the breaker's state (tests, and a settings change)."""
    global _client, _client_loop, transport
    if _client is not None and not _client.is_closed and _client_loop is asyncio.get_running_loop():
        await _client.aclose()
    _client = None
    _client_loop = None
    transport = new_transport
    breaker.reset()


async def call(method: str, path: str, *, json: Any = None, params: Optional[dict] = None, timeout: Optional[float] = None, guarded: bool = False) -> Any:
    """One request; the parsed JSON answer. Raises SearchError (Unavailable
    when not configured, or `guarded` and the breaker is open)."""
    if not configured():
        raise Unavailable("search is not configured")
    if guarded and not breaker.allow():
        raise Unavailable("search is resting after failures")
    try:
        r = await _http().request(method, path, json=json, params=params, timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT)
    except httpx.HTTPError as e:
        if guarded:
            breaker.failure()
        raise SearchError(f"{method} {path}: {type(e).__name__}") from e
    if r.status_code >= 400:
        if guarded:
            breaker.failure()
        code = None
        try:
            code = r.json().get("code")
        except ValueError:
            pass
        raise SearchError(f"{method} {path}: {r.status_code} {r.text[:300]}", status=r.status_code, code=code)
    if guarded:
        breaker.success()
    return r.json() if r.content else None


# ─── Reads (on a request's path) ──────────────────────────────────────────


async def search(index_uid: str, body: dict) -> dict:
    return await call("POST", f"/indexes/{index_uid}/search", json=body, guarded=True)


async def multi_search(queries: list[dict]) -> list[dict]:
    """Several searches in one round trip; each query names its `indexUid`."""
    got = await call("POST", "/multi-search", json={"queries": queries}, guarded=True)
    return got["results"]


# ─── The sync's calls ─────────────────────────────────────────────────────

#: How long the sync waits for one task (an indexing batch, a swap).
TASK_WAIT_SECONDS = 120.0


async def healthy() -> bool:
    try:
        got = await call("GET", "/health")
    except SearchError:
        return False
    return (got or {}).get("status") == "available"


async def wait_task(task_uid: int, timeout: float = TASK_WAIT_SECONDS) -> dict:
    """Poll a task until it is done; its record. Raises SearchError when it
    failed, or is still not done after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    pause = 0.02
    while True:
        task = await call("GET", f"/tasks/{task_uid}")
        status = task.get("status")
        if status == "succeeded":
            return task
        if status in ("failed", "canceled"):
            error = task.get("error") or {}
            raise SearchError(f"task {task_uid} {status}: {error.get('message')}", code=error.get("code"))
        if time.monotonic() > deadline:
            raise SearchError(f"task {task_uid} still {status} after {timeout:.0f} s")
        await asyncio.sleep(pause)
        pause = min(pause * 2, 0.5)


def _task(answer: dict) -> int:
    return answer["taskUid"]


async def get_index(uid: str) -> Optional[dict]:
    try:
        return await call("GET", f"/indexes/{uid}")
    except SearchError as e:
        if e.status == 404:
            return None
        raise


async def create_index(uid: str) -> None:
    """Make an index (primary key `id`); nothing if it is there."""
    if await get_index(uid) is not None:
        return
    try:
        await wait_task(_task(await call("POST", "/indexes", json={"uid": uid, "primaryKey": "id"})))
    except SearchError as e:
        if e.code != "index_already_exists":
            raise


async def delete_index(uid: str) -> None:
    if await get_index(uid) is None:
        return
    await wait_task(_task(await call("DELETE", f"/indexes/{uid}")))


async def swap(a: str, b: str) -> None:
    """Swap two indexes' contents, atomically: a rebuild goes live."""
    await wait_task(_task(await call("POST", "/swap-indexes", json=[{"indexes": [a, b]}])))


async def stats(uid: str) -> Optional[dict]:
    try:
        return await call("GET", f"/indexes/{uid}/stats")
    except SearchError as e:
        if e.status == 404:
            return None
        raise


async def add_documents(uid: str, docs: list[dict]) -> Optional[int]:
    """Add or replace documents (whole); the task's uid, None for none."""
    if not docs:
        return None
    return _task(await call("POST", f"/indexes/{uid}/documents", params={"primaryKey": "id"}, json=docs, timeout=30.0))


async def delete_documents(uid: str, ids: list[int]) -> Optional[int]:
    if not ids:
        return None
    return _task(await call("POST", f"/indexes/{uid}/documents/delete-batch", json=list(ids), timeout=30.0))


async def fetch_documents(uid: str, *, filter: Optional[str] = None, fields: Optional[list[str]] = None, limit: int = 1000, offset: int = 0) -> dict:
    """{"results": [...], "total": n} of the documents matching `filter`."""
    body: dict = {"limit": limit, "offset": offset}
    if filter:
        body["filter"] = filter
    if fields:
        body["fields"] = fields
    return await call("POST", f"/indexes/{uid}/documents/fetch", json=body, timeout=30.0)


# ─── What the indexes are ─────────────────────────────────────────────────

#: Words people use for the same thing, in Indian English: each group's
#: words find each other (a word found also finds the others).
SYNONYM_GROUPS = (
    ("hackathon", "hack", "hackfest"),
    ("fest", "festival"),
    ("techfest", "tech fest"),
    ("bengaluru", "bangalore", "blore", "b'lore", "blr"),
    ("thiruvananthapuram", "trivandrum", "tvm"),
    ("mumbai", "bombay"),
    ("chennai", "madras"),
    ("kolkata", "calcutta"),
    ("kochi", "cochin"),
    ("kozhikode", "calicut"),
    ("thrissur", "trichur"),
    ("kollam", "quilon"),
    ("kannur", "cannanore"),
    ("alappuzha", "alleppey"),
    ("mysuru", "mysore"),
    ("mangaluru", "mangalore"),
    ("hubballi", "hubli"),
    ("belagavi", "belgaum"),
    ("gurugram", "gurgaon"),
    ("pune", "poona"),
    ("vadodara", "baroda"),
    ("visakhapatnam", "vizag"),
    ("tiruchirappalli", "trichy"),
    ("puducherry", "pondicherry", "pondy"),
    ("varanasi", "benares", "banaras"),
    ("prayagraj", "allahabad"),
    ("hyderabad", "hyd"),
    ("delhi", "new delhi"),
    ("ai", "artificial intelligence"),
    ("ml", "machine learning"),
    ("ux", "user experience"),
    ("iot", "internet of things"),
    ("vr", "virtual reality"),
    ("mun", "model united nations"),
    ("esports", "e-sports"),
    ("startup", "start-up"),
    ("meetup", "meet-up"),
    ("quiz", "quizzing"),
    ("competition", "contest"),
    ("coding", "programming"),
)


def synonyms() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for group in SYNONYM_GROUPS:
        for word in group:
            out[word] = [w for w in group if w != word]
    return out


#: Ignored in a query: they would only make every word of it harder to match.
STOP_WORDS = ("a", "an", "the", "of", "in", "on", "at", "for", "to", "and", "or", "with", "by", "from", "is", "are")

_COMMON = {
    # "sort" first: a list sorted by date, popularity or distance is in that
    # order, as the Postgres path orders it; relevance only breaks its ties,
    # and decides alone when no sort is asked for.
    "rankingRules": ["sort", "words", "typo", "proximity", "attribute", "exactness"],
    "typoTolerance": {"enabled": True, "minWordSizeForTypos": {"oneTypo": 5, "twoTypos": 9}},
    "pagination": {"maxTotalHits": MAX_TOTAL_HITS},
}

#: The events index. Searchable in order of weight: a word in the name
#: outranks one in the club's name, then interests, topics, category, tags,
#: the place. Filters: what discover and the app's list narrow by. An event
#: is in the index unless it is deleted or a draft; whether its club is
#: listed and whether it is over are filters, so a block or the clock never
#: rewrites a document.
EVENT_SETTINGS = {
    **_COMMON,
    "searchableAttributes": ["name", "club_name", "interests", "topics", "category", "tags", "location", "club_location"],
    "filterableAttributes": [
        "id", "club_id", "category_id", "topic_ids", "interest_ids", "city", "is_online", "has_fee", "fee", "has_point",
        "starts_at", "ends_at", "created_at", "lifecycle", "listed", "_geo",
    ],
    "sortableAttributes": ["starts_at", "created_at", "popularity", "id", "_geo"],
}

#: The clubs index: every club not deleted; `listed` is a filter.
CLUB_SETTINGS = {
    **_COMMON,
    "searchableAttributes": ["name", "tagline", "interests", "topics", "location"],
    "filterableAttributes": ["id", "listed", "topic_ids", "interest_ids", "city", "has_place", "created_at", "follower_count", "_geo"],
    "sortableAttributes": ["follower_count", "name_sort", "created_at", "id", "_geo"],
}


def settings_for(kind: str) -> dict:
    base = EVENT_SETTINGS if kind == EVENTS else CLUB_SETTINGS
    return {**base, "synonyms": synonyms(), "stopWords": list(STOP_WORDS)}


_UNORDERED = ("filterableAttributes", "sortableAttributes", "stopWords")


def _names(items) -> set[str]:
    """The attribute names of a list setting, as plain strings or as the
    newer {"attributePatterns": [...]} objects."""
    out: set[str] = set()
    for x in items or ():
        if isinstance(x, dict):
            out.update(str(p).lower() for p in x.get("attributePatterns") or ())
        else:
            out.add(str(x).lower())
    return out


def _same(key: str, want: Any, have: Any) -> bool:
    """Whether a setting the instance reports is what we want: lists whose
    order does not matter as sets, synonyms lowercased, nested settings by
    the keys we set."""
    if have is None:
        return False
    if key in _UNORDERED:
        return _names(want) == _names(have)
    if key == "synonyms":
        norm = lambda d: {k.lower(): sorted(v.lower() for v in vs) for k, vs in d.items()}  # noqa: E731
        return norm(want) == norm(have)
    if isinstance(want, dict):
        return isinstance(have, dict) and all(_same(k, v, have.get(k)) for k, v in want.items())
    return want == have


async def apply_settings(uid: str, kind: str) -> bool:
    """Make an index's settings ours; True when anything had to change
    (and the change has been applied). Idempotent: run it at every start."""
    want = settings_for(kind)
    have = await call("GET", f"/indexes/{uid}/settings")
    changed = {k: v for k, v in want.items() if not _same(k, v, (have or {}).get(k))}
    if not changed:
        return False
    await wait_task(_task(await call("PATCH", f"/indexes/{uid}/settings", json=changed)), timeout=600.0)
    return True
