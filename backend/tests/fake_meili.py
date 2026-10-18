"""An in-memory Meilisearch, for the tests.

`install(monkeypatch)` turns search on (a URL and a key in the settings),
points app.core.search's client at this fake through httpx's MockTransport,
and returns the fake. Nothing reaches the network; a request the fake does
not know fails the test.

It behaves like Meilisearch where the backend depends on it:

- indexes with a primary key, documents added (replaced whole) and deleted,
  settings stored and read back, index swaps, tasks that are done at once
  (GET /tasks/{uid} says "succeeded"), /health, /multi-search;
- search: every query word must match (matchingStrategy "all"): a word
  matches a token of a searchable attribute that equals it, or starts with
  it when it is the query's last word, or is one of its synonyms, or is one
  typo away (two for long words) at the instance's minimum word sizes; stop
  words are ignored. Hits are ordered by `sort` when given, else by how
  well they match (fewer typos, then the earlier attribute), then by id;
  `offset`/`limit`, or `page`/`hitsPerPage` with an exhaustive `totalHits`,
  both capped at pagination.maxTotalHits;
- filters: the grammar the backend writes -- `=`, `!=`, `<`, `<=`, `>`,
  `>=`, `IN [..]`, `NOT`, `AND`, `OR`, parentheses, `_geoRadius(lat, lng,
  metres)` -- with Meilisearch's meaning: `attr = v` on an array matches
  when any element does, strings compare case-blind, booleans as
  "true"/"false". `_geoRadius` measures with geo.km (the fake's Earth is
  ours; the real one's may differ by a little, which the backend allows for).

`down` makes every request fail as an unreachable instance would; `slow`
makes it time out; `fail_status` answers every request with that status.
`requests` records (method, path, body) for the tests to look at.
"""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Optional

import httpx

from app.api.discover import geo
from app.config import settings
from app.core import search

URL = "http://meili.test"
KEY = "test-meili-key"


def _fold(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)).lower()


def _tokens(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [t for v in value for t in _tokens(v)]
    return [t for t in re.split(r"[^\w]+", _fold(str(value))) if t]


def _typos(a: str, b: str, limit: int) -> int:
    """Levenshtein distance, or limit + 1 when over it."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


# ─── Filters ──────────────────────────────────────────────────────────────

_TOKEN = re.compile(r"\s*(>=|<=|!=|=|<|>|\(|\)|\[|\]|,|'[^']*'|\"[^\"]*\"|[^\s()\[\],=<>!]+)")


class _Filter:
    def __init__(self, source: str):
        self.toks = [t for t in _TOKEN.findall(source) if t.strip()]
        self.i = 0
        self.tree = self._or()
        if self.i != len(self.toks):
            raise ValueError(f"unparsed filter: {self.toks[self.i:]}")

    def _peek(self, *words) -> bool:
        return self.i < len(self.toks) and self.toks[self.i].upper() in words

    def _take(self) -> str:
        t = self.toks[self.i]
        self.i += 1
        return t

    def _or(self):
        node = self._and()
        while self._peek("OR"):
            self._take()
            node = ("or", node, self._and())
        return node

    def _and(self):
        node = self._not()
        while self._peek("AND"):
            self._take()
            node = ("and", node, self._not())
        return node

    def _not(self):
        if self._peek("NOT"):
            self._take()
            return ("not", self._not())
        return self._primary()

    def _primary(self):
        if self._peek("("):
            self._take()
            node = self._or()
            assert self._take() == ")"
            return node
        attr = self._take()
        if attr.startswith("_geoRadius"):
            assert self._take() == "("
            lat = float(self._take())
            assert self._take() == ","
            lng = float(self._take())
            assert self._take() == ","
            metres = float(self._take())
            assert self._take() == ")"
            return ("geo", lat, lng, metres)
        op = self._take().upper()
        if op == "IN":
            assert self._take() == "["
            values = []
            while not self._peek("]"):
                values.append(_value(self._take()))
                if self._peek(","):
                    self._take()
            self._take()
            return ("in", attr, values)
        if op == "EXISTS":
            return ("exists", attr)
        return ("cmp", attr, op, _value(self._take()))

    def match(self, doc: dict) -> bool:
        return _eval(self.tree, doc)


def _value(tok: str):
    if tok[:1] in "'\"":
        return tok[1:-1]
    try:
        return float(tok) if any(c in tok for c in ".eE") else int(tok)
    except ValueError:
        return tok


def _as_filter(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return float(v)
    return _fold(str(v))


def _eval(node, doc: dict) -> bool:
    kind = node[0]
    if kind == "or":
        return _eval(node[1], doc) or _eval(node[2], doc)
    if kind == "and":
        return _eval(node[1], doc) and _eval(node[2], doc)
    if kind == "not":
        return not _eval(node[1], doc)
    if kind == "geo":
        g = doc.get("_geo")
        return bool(g) and geo.km(node[1], node[2], g["lat"], g["lng"]) * 1000.0 <= node[3]
    if kind == "exists":
        return node[1] in doc
    attr = node[1]
    raw = doc.get(attr)
    values = raw if isinstance(raw, list) else ([] if raw is None else [raw])
    have = [_as_filter(v) for v in values]
    if kind == "in":
        want = {_as_filter(v) for v in node[2]}
        return any(h in want for h in have)
    _, _, op, target = node
    target = _as_filter(target)
    if op == "=":
        return target in have
    if op == "!=":
        return target not in have
    nums = [h for h in have if isinstance(h, float)]
    if not isinstance(target, float):
        return False
    return any({"<": h < target, "<=": h <= target, ">": h > target, ">=": h >= target}[op] for h in nums)


# ─── The fake ─────────────────────────────────────────────────────────────


class FakeMeili:
    def __init__(self):
        self.indexes: dict[str, dict] = {}
        self.tasks: dict[int, dict] = {}
        self.requests: list[tuple[str, str, Any]] = []
        self.down = False
        self.slow = False
        self.fail_status: Optional[int] = None

    # ── What the tests look at ──────────────────────────────────────────

    def docs(self, kind: str) -> dict[int, dict]:
        return self.indexes.get(search.index(kind), {}).get("docs", {})

    def searches(self) -> list:
        return [r for r in self.requests if r[1].endswith("/search") or r[1] == "/multi-search"]

    # ── Tasks ───────────────────────────────────────────────────────────

    def _task(self, kind: str, index_uid: Optional[str] = None, error: Optional[dict] = None) -> httpx.Response:
        uid = len(self.tasks) + 1
        self.tasks[uid] = dict(uid=uid, indexUid=index_uid, type=kind, status="failed" if error else "succeeded", error=error)
        return httpx.Response(202, json={"taskUid": uid, "indexUid": index_uid, "status": "enqueued", "type": kind})

    def _index(self, uid: str) -> Optional[dict]:
        return self.indexes.get(uid)

    def _new_index(self, uid: str, primary: str = "id") -> dict:
        self.indexes[uid] = dict(uid=uid, primaryKey=primary, docs={}, settings={
            "searchableAttributes": ["*"], "filterableAttributes": [], "sortableAttributes": [], "synonyms": {}, "stopWords": [],
            "rankingRules": ["words", "typo", "proximity", "attribute", "sort", "exactness"],
            "typoTolerance": {"enabled": True, "minWordSizeForTypos": {"oneTypo": 5, "twoTypos": 9}},
            "pagination": {"maxTotalHits": 1000},
        })
        return self.indexes[uid]

    # ── The API ─────────────────────────────────────────────────────────

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path
        self.requests.append((request.method, path, body))
        if self.down:
            raise httpx.ConnectError("meilisearch is down", request=request)
        if self.slow:
            raise httpx.ReadTimeout("meilisearch is slow", request=request)
        if self.fail_status:
            return httpx.Response(self.fail_status, json={"message": "failing", "code": "internal"})
        assert request.headers.get("authorization") == f"Bearer {KEY}", "the key went missing"
        m = request.method
        if path == "/health":
            return httpx.Response(200, json={"status": "available"})
        if m == "GET" and (t := re.fullmatch(r"/tasks/(\d+)", path)):
            return httpx.Response(200, json=self.tasks[int(t.group(1))])
        if path == "/indexes" and m == "POST":
            if body["uid"] in self.indexes:
                return self._task("indexCreation", body["uid"], error={"code": "index_already_exists", "message": "exists"})
            self._new_index(body["uid"], body.get("primaryKey") or "id")
            return self._task("indexCreation", body["uid"])
        if path == "/swap-indexes" and m == "POST":
            for pair in body:
                a, b = pair["indexes"]
                ia, ib = self.indexes.get(a), self.indexes.get(b)
                if ia is None or ib is None:
                    return self._task("indexSwap", None, error={"code": "index_not_found", "message": "missing"})
                self.indexes[a], self.indexes[b] = ib, ia
                ia["uid"], ib["uid"] = b, a
            return self._task("indexSwap")
        if path == "/multi-search" and m == "POST":
            results = []
            for q in body["queries"]:
                idx = self._index(q["indexUid"])
                if idx is None:
                    return httpx.Response(404, json={"code": "index_not_found", "message": "no index"})
                results.append(dict(self._search(idx, q), indexUid=q["indexUid"]))
            return httpx.Response(200, json={"results": results})
        found = re.fullmatch(r"/indexes/([^/]+)(/.*)?", path)
        if not found:
            raise AssertionError(f"the fake does not know {m} {path}")
        uid, rest = found.group(1), found.group(2) or ""
        idx = self._index(uid)
        if rest == "" and m == "GET":
            return httpx.Response(200, json={"uid": uid, "primaryKey": idx["primaryKey"]}) if idx else httpx.Response(404, json={"code": "index_not_found", "message": "no"})
        if rest == "" and m == "DELETE":
            if idx is None:
                return httpx.Response(404, json={"code": "index_not_found", "message": "no"})
            del self.indexes[uid]
            return self._task("indexDeletion", uid)
        if rest == "/documents" and m == "POST":
            idx = idx or self._new_index(uid)
            for d in body:
                idx["docs"][int(d["id"])] = d
            return self._task("documentAdditionOrUpdate", uid)
        if idx is None:
            return httpx.Response(404, json={"code": "index_not_found", "message": "no index"})
        if rest == "/documents/delete-batch" and m == "POST":
            for i in body:
                idx["docs"].pop(int(i), None)
            return self._task("documentDeletion", uid)
        if rest == "/documents/fetch" and m == "POST":
            flt = _Filter(body["filter"]) if body.get("filter") else None
            hits = [d for _, d in sorted(idx["docs"].items()) if flt is None or flt.match(d)]
            page = hits[body.get("offset", 0): body.get("offset", 0) + body.get("limit", 20)]
            fields = body.get("fields")
            return httpx.Response(200, json={"results": [{k: d.get(k) for k in fields} if fields else d for d in page], "total": len(hits),
                                             "offset": body.get("offset", 0), "limit": body.get("limit", 20)})
        if rest == "/settings" and m == "GET":
            return httpx.Response(200, json=idx["settings"])
        if rest == "/settings" and m == "PATCH":
            for k, v in body.items():
                idx["settings"][k] = {**idx["settings"].get(k, {}), **v} if isinstance(v, dict) and k in ("typoTolerance", "pagination") else v
            return self._task("settingsUpdate", uid)
        if rest == "/stats" and m == "GET":
            return httpx.Response(200, json={"numberOfDocuments": len(idx["docs"]), "isIndexing": False})
        if rest == "/search" and m == "POST":
            return httpx.Response(200, json=self._search(idx, body))
        raise AssertionError(f"the fake does not know {m} {path}")

    # ── Search ──────────────────────────────────────────────────────────

    def _search(self, idx: dict, q: dict) -> dict:
        st = idx["settings"]
        stop = {_fold(w) for w in st.get("stopWords") or []}
        synonyms = {_fold(k): {_fold(x) for x in v} for k, v in (st.get("synonyms") or {}).items()}
        words = [w for w in _tokens(q.get("q") or "") if w not in stop]
        attrs = st.get("searchableAttributes") or ["*"]
        tol = st.get("typoTolerance") or {}
        one, two = (tol.get("minWordSizeForTypos") or {}).get("oneTypo", 5), (tol.get("minWordSizeForTypos") or {}).get("twoTypos", 9)
        flt = _Filter(q["filter"]) if q.get("filter") else None
        scored = []
        for doc in idx["docs"].values():
            if flt is not None and not flt.match(doc):
                continue
            score = self._match(doc, words, attrs, synonyms, one, two)
            if score is not None:
                scored.append((score, doc))
        sort = q.get("sort")
        if sort:
            # "sort" is the first ranking rule: the order asked for decides, relevance only breaks ties.
            scored.sort(key=lambda sd: (_sort_key(sort, sd[1]), sd[0], sd[1]["id"]))
        else:
            scored.sort(key=lambda sd: (sd[0], sd[1]["id"]))
        cap = (st.get("pagination") or {}).get("maxTotalHits", 1000)
        hits = [d for _, d in scored][:cap]
        fields = q.get("attributesToRetrieve")

        def shown(d):
            return {k: d.get(k) for k in fields} if fields and "*" not in fields else d

        if "page" in q or "hitsPerPage" in q:
            per, page = q.get("hitsPerPage", 20), q.get("page", 1)
            return {"hits": [shown(d) for d in hits[(page - 1) * per: page * per]], "totalHits": len(hits), "page": page, "hitsPerPage": per,
                    "totalPages": -(-len(hits) // per) if per else 0}
        off, lim = q.get("offset", 0), q.get("limit", 20)
        return {"hits": [shown(d) for d in hits[off: off + lim]], "estimatedTotalHits": len(hits), "offset": off, "limit": lim}

    @staticmethod
    def _match(doc, words, attrs, synonyms, one, two) -> Optional[tuple]:
        if not words:
            return (0, 0)
        fields = [doc.get(a) for a in attrs] if attrs != ["*"] else [v for k, v in doc.items() if isinstance(v, (str, list))]
        tokens = [(rank, t) for rank, f in enumerate(fields) for t in _tokens(f)]
        typos_total, best_rank = 0, 0
        for n, w in enumerate(words):
            last = n == len(words) - 1
            allowed = 2 if len(w) >= two else 1 if len(w) >= one else 0
            best = None
            for rank, t in tokens:
                if t == w or t in synonyms.get(w, ()) or (last and t.startswith(w)):
                    cost = 0
                elif allowed:
                    cost = _typos(w, t, allowed)
                    if cost > allowed:
                        continue
                else:
                    continue
                if best is None or (cost, rank) < best:
                    best = (cost, rank)
            # A synonym of several words ("artificial intelligence") is found as a phrase.
            if best is None:
                text = " ".join(t for _, t in tokens)
                for syn in synonyms.get(w, ()):
                    if " " in syn and syn in text:
                        best = (0, 0)
                        break
            if best is None:
                return None
            typos_total += best[0]
            best_rank = max(best_rank, best[1])
        return (typos_total, best_rank)


def _sort_key(rules: list[str], doc: dict) -> list:
    """A document's place in the order `rules` asks for: numbers both ways,
    strings ascending, and a document without the attribute (or, for
    _geoPoint, without a point) after every one with it."""
    key: list = []
    for rule in rules:
        if rule.startswith("_geoPoint"):
            lat, lng = (float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", rule.split(")")[0]))
            g = doc.get("_geo")
            key += [g is None, geo.km(lat, lng, g["lat"], g["lng"]) if g else 0.0]
            continue
        attr, direction = rule.split(":")
        v = doc.get(attr)
        if v is None:
            key += [True, 0]
        elif direction == "desc":
            key += [False, -v]
        else:
            key += [False, v]
    return key


async def install(monkeypatch) -> FakeMeili:
    """Search on, answered by a fresh fake; the breaker closed, readiness forgotten."""
    from app.api.search import engine, sync

    fake = FakeMeili()
    monkeypatch.setattr(settings, "SEARCH_URL", URL)
    monkeypatch.setattr(settings, "SEARCH_KEY", KEY)
    monkeypatch.setattr(settings, "SEARCH_INDEX_PREFIX", "t_")
    await search.reset(httpx.MockTransport(fake.handle))
    engine.reset()
    sync.reset()
    return fake
