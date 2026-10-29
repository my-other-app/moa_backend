"""Finding an organisation by name or address.

Postgres, not Meilisearch. Every other search in this codebase is already
one normalised column under one trigram index (app.api.search.text), and an
organisation is a short row that changes once in a blue moon: 1,936 of them
today, tens of thousands if every company and community in the country
signs up. A GIN trigram index answers that in single-digit milliseconds, in
the same transaction as the row that was just suggested -- which matters,
because onboarding selects a college the moment it is added, and a
Meilisearch document is only there once the outbox has drained. Nothing
here would be better for a second store to keep in step, so there is none.

What a query has to survive, from the 1,936 rows as imported:

- words in any order, each a part of a word: "appl sci chela" is "College of
  Applied Science, Chelakkara". Each word is one substring test against the
  row's one column, ANDed -- so the words may come in any order, and a word
  need not be whole;
- punctuation: "College of Applied Science (IHRD)" and "College of Applied
  Science IHRD" are two rows in the table. Both sides of the comparison turn
  every run of punctuation into one space (`moa_org_search_norm`), so the
  two names are the same text and either spelling finds either row;
- a typo: "Calicut Univrsity" finds nothing by substring, so a query that
  found nothing is tried again by trigram word similarity (pg_trgm's `%>`,
  over the same index). Only then -- a typo pass that ran first would answer
  "college" with every college in Kerala;
- 1,315 of the rows have no address at all, so the address is only ever
  something a query may also match, never something it must.

Ranking, best first: the name exactly, the name starting with the query, the
query inside the name, every word in the name, and last the rows matched
through their address alone. Verified rows come first inside each of those,
then by how alike the name is, then the shortest name, then alphabetically --
so "Calicut University" is not buried under its fifteen teacher education
centres, and an unverified row somebody suggested never outranks the real
one.
"""
from __future__ import annotations

import re
from typing import Optional, Sequence

from sqlalchemy import and_, case, func, select, text, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.orgs.models import Organizations, OrgTypes
from app.api.search import text as stext

#: At most this many words of a query count; a picker's query is short.
MAX_WORDS = 6
#: A word this short is matched as a whole word, not inside others: it has no
#: trigram for the index to narrow by, and "ba" inside words would find every
#: row. The discover search draws the line in the same place.
WHOLE_WORD_BELOW = 3
#: How alike a word must be to count as a typo of it (pg_trgm's `%>`). The
#: default, 0.6, misses "univrsity" for "university"; the discover search
#: settled on this for the same reason.
FUZZY_THRESHOLD = 0.5

#: The name alone, normalised as search_text is: what the ranking reads. An
#: expression, not a column -- unaccent is STABLE, so no index may hold it,
#: and it is worked out only for the rows a query already matched.
NAME_NORM = func.moa_org_search_norm(Organizations.name)
#: The address alone, normalised, for the duplicate check.
ADDRESS_NORM = func.coalesce(func.moa_org_search_norm(Organizations.address), "")


def _like(q: str) -> str:
    """`q` as a LIKE pattern matching it anywhere, its wildcards defanged."""
    return f"%{_escaped(q)}%"


def _escaped(q: str) -> str:
    return q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def words_of(q: Optional[str]) -> list[str]:
    """The words of a normalised query that count: each once, at most
    MAX_WORDS. A single character is no word on its own, unless it is the
    whole query."""
    raw = [w for w in (q or "").split() if w]
    out: list[str] = []
    for w in raw:
        if len(w) >= 2 and w not in out:
            out.append(w)
    if not out and len(raw) == 1:
        out = raw
    return out[:MAX_WORDS]


def _matches(column, word: str):
    """`column` (normalised the same way) holds `word`: as a substring from
    three characters up, which the trigram index serves; shorter, as a whole
    word, which only filters."""
    if len(word) >= WHOLE_WORD_BELOW:
        return column.like(_like(word), escape="\\")
    return column.op("~")(r"\m" + re.escape(word) + r"\M")


def every_word(column, words: Sequence[str], *, fuzzy: bool = False):
    """Every word somewhere in `column`, so the words may come in any order
    and "appl sci chela" is one college rather than every applied science
    college and everything in Chelakkara. With `fuzzy`, each word long
    enough to have trigrams is forgiven a typo instead."""
    arms = []
    for w in words:
        if fuzzy and len(w) >= WHOLE_WORD_BELOW:
            arms.append(column.op("%>")(w))
        else:
            arms.append(_matches(column, w))
    return and_(*arms) if arms else true()


def _rank(nq: str, words: Sequence[str]):
    """How well a row answers the query, 0 best: the name exactly, the name
    starting with it, it inside the name, every word in the name, and last
    the rows the address matched."""
    return case(
        (NAME_NORM == nq, 0),
        (NAME_NORM.like(f"{_escaped(nq)}%", escape="\\"), 1),
        (NAME_NORM.like(_like(nq), escape="\\"), 2),
        (every_word(NAME_NORM, words), 3),
        else_=4,
    )


async def forgive_typos(session: AsyncSession) -> None:
    """Use FUZZY_THRESHOLD for `%>` until this request's transaction ends."""
    await session.execute(text(f"SET LOCAL pg_trgm.word_similarity_threshold = {FUZZY_THRESHOLD}"))


def visible(type_: Optional[OrgTypes] = None) -> list:
    """What a sign-up screen may be shown: neither deleted nor blocked,
    whatever its type (companies and communities are coming, so nothing here
    assumes a college)."""
    criteria = [Organizations.is_deleted == False, Organizations.is_blocked == False]  # noqa: E712
    if type_ is not None:
        criteria.append(Organizations.type == type_)
    return criteria


def ordered(query, nq: str, words: Sequence[str]):
    return query.order_by(
        _rank(nq, words),
        Organizations.is_verified.desc(),
        func.similarity(NAME_NORM, nq).desc(),
        func.char_length(Organizations.name),
        Organizations.name,
        Organizations.id,
    )


async def find(
    session: AsyncSession,
    q: Optional[str],
    *,
    type_: Optional[OrgTypes] = None,
    limit: int = 10,
) -> list[Organizations]:
    """The organisations a query finds, best first. An empty query is the
    picker's first screen: the verified ones, alphabetically.

    Two statements at worst -- the substring pass, and the typo pass only
    when the first found nothing."""
    nq = await stext.normalise_org(session, q)
    words = words_of(nq)
    if not words:
        rows = await session.scalars(
            select(Organizations)
            .where(*visible(type_))
            .order_by(Organizations.is_verified.desc(), Organizations.name, Organizations.id)
            .limit(limit)
        )
        return list(rows)

    found = await _hits(session, nq, words, type_=type_, limit=limit)
    if not found and len(nq) >= WHOLE_WORD_BELOW:
        # A trigram needs three characters; below that there is nothing to
        # forgive a typo with.
        await forgive_typos(session)
        found = await _hits(session, nq, words, type_=type_, limit=limit, fuzzy=True)
    return found


async def _hits(
    session: AsyncSession,
    nq: str,
    words: Sequence[str],
    *,
    type_: Optional[OrgTypes],
    limit: int,
    fuzzy: bool = False,
) -> list[Organizations]:
    query = select(Organizations).where(
        *visible(type_), every_word(Organizations.search_text, words, fuzzy=fuzzy)
    )
    rows = await session.scalars(ordered(query, nq, words).limit(limit))
    return list(rows)


async def same_organisation(
    session: AsyncSession, name: str, address: Optional[str]
) -> Optional[Organizations]:
    """A row already there under this name and this address, both normalised:
    what a suggestion must not add a second of. A row with no address counts
    as the same as a suggestion with none -- the pair this table is full of
    ("Calicut University Teacher Education Centre" in five towns) differ by
    their address, and those are five real places, not duplicates.

    Deleted rows are left out: an organisation an admin removed may be
    suggested again."""
    nname = await stext.normalise_org(session, name)
    if not nname:
        return None
    naddress = await stext.normalise_org(session, address)
    return await session.scalar(
        select(Organizations)
        .where(
            Organizations.is_deleted == False,  # noqa: E712
            NAME_NORM == nname,
            ADDRESS_NORM == naddress,
        )
        .order_by(Organizations.is_verified.desc(), Organizations.id)
        .limit(1)
    )
