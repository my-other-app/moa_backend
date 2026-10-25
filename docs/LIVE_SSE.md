# Live room push: the client contract (server-sent events)

Status: server in the backend's `feat/wave2` (from `feat/w2-live`), behind
`APP_LIVE_SSE`; the server's side of turning it on is docs/OPERATIONS.md,
section 12. Clients: the event site (room + quiz, and the event page's live
strip), the Flutter app (room + quiz, when it shows the live room), the clubs
dashboard (host console, big screen, Q&A board, polls). Every client keeps its poll and uses it whenever the stream is
not available: the stream is a faster way to get **the same JSON bodies**.

## 1. One endpoint, one stream per screen

```
GET /api/v1/events/{event_id}/live/stream?view={view}
Accept: text/event-stream
```

| view | replaces this poll (today's interval) | who may open it | body type |
|---|---|---|---|
| `room` | `GET /events/{id}/live` (5 s) | anyone; send the browser key for its own flags | `Feed` |
| `banner` | the event page's live strip, read from `GET /events/{id}/live/banner` | anyone, as `room` (no key: it has no flags) | `BannerFeed` (2a) |
| `quiz` | `GET /events/{id}/live/quiz/play` (2 s) | a player: their player key | `PlayerState` |
| `host` | `GET /events/{id}/live/quiz/host` (2 s while running) | the event's club (or an admin), as the poll | `HostState` |
| `screen` | `GET /events/{id}/live/screen` (3 s) | the event's club, as the poll | `Screen` |
| `questions` | `GET /events/{id}/live/questions` (5 s) | the event's club, as the poll | `QABoard` |
| `polls` | `GET /events/{id}/live/polls` (3 s while one is live) | the event's club, as the poll | `PollOut[]` |

Credentials are the ones the matching poll takes, sent the same way:

- `room`: `X-Live-Key: <browser key>` (optional: without it, or with a malformed one, every flag is false, as the poll).
- `quiz`: `X-Live-Player: <player key>` (required).
- `host`, `screen`, `questions`, `polls`: the club session — `Authorization:
  Bearer <access token>` or the `access_token` cookie. Through the clubs
  dashboard's gateway (`/api/gateway/...`) the gateway adds it, as for the polls.

A browser's native `EventSource` cannot send headers. For it, and only for
it, the same values are accepted in the query string: `?key=` (browser key),
`?player=` (player key), `?last_event_id=` (see 3). Headers win when both are
sent. A client that can send headers (fetch-based streaming, Flutter,
`@microsoft/fetch-event-source`) should. The server keeps these values out
of its logs all the same: its access lines and error reports write `key`,
`player`, `last_event_id` and any `...token...` query value as
`[Filtered]`, and the stream's nginx location logs the path without the
query (6).

## 2. What comes down the stream

`Content-Type: text/event-stream; charset=utf-8`. Frames are separated by a
blank line. The server sends:

```
retry: 3000

id: 1760793812345678
event: snapshot
data: {"event":{"id":68,...},"banner":null,"updates":[...],"poll":{...},"questions":[...],"qa":{...},"quiz":{...}}

event: ping
data: {"t":1760793827345}

event: gone
data: {"status":404,"message":"Event not found"}
```

- **`retry: <ms>`** — the first thing on every stream (3000), and again,
  jittered, just before the server ends a stream on purpose (see 4). Use it as
  the reconnect delay.
- **`event: snapshot`** — `data` is **exactly the JSON body the matching
  poll returns right now** (same fields, same values, on one line; the
  `banner` view has a small body of its own, 2a), *for this client*: the room view carries this browser's own `voted` / `mine` /
  `poll.my_response`, the quiz view this player's `name`, `score`, `rank`,
  `my_answer`, `my_points`. Parse it with the parser the poll uses. Every
  snapshot is complete — never a diff — and the newest always wins.
- **`id: <version>`** — on every snapshot. An integer (it fits a JS number),
  increasing within one stream. Store the last one you saw per (event, view)
  and send it back when you reconnect (3). Treat it as opaque otherwise.
- **`event: ping`** — after 15 s without anything else, and right away when a
  stream opens with nothing new to send. `data.t` is the server's clock in
  ms since the epoch: use it to correct a countdown for a phone whose clock is
  off. Pings carry no `id` (the last id stays).
- **`event: gone`** — the room or the quiz is not there for you any more:
  `status` is what the poll would answer now (404: the event is not shown /
  has no quiz; 401: this player is not in the quiz — join again; 403: no
  longer allowed). The stream then ends. **Do not reconnect**: do what the
  poll does with that status (it is fine to make one poll to confirm).

`time_left` (quiz, host, screen) is counted when the frame is written. Count
down locally from the moment it arrives (corrected by `ping.t` if you like).
When the question's answers close (its clock, then two seconds of grace for
a slow phone) the server sends a fresh snapshot: the quiz view then shows
`correct_key`, the player's `my_points` and `my_correct`, their score, rank
and the board, none of which it shows before (OPERATIONS.md, section 22).

How soon a change arrives: what the host does (next question, a question
approved or pinned, a poll launched or closed, an update pushed, a schedule
shift), at once; what the audience does (votes, poll answers, quiz answers,
questions asked, players joining), within about a third of a second, many
taps folded into one snapshot -- except on the `room` view, whose snapshot
is ~30 KB: there the audience's changes come at most once every 2 s (the
host's still at once, with whatever the audience did meanwhile). A busy
quiz may send a few snapshots a second; a client that reads slowly is sent
only the latest, never a backlog.

### 2a. The `banner` view

A few hundred bytes where the room's `Feed` is ~30 KB: what the event page's
live strip shows, and nothing else.

```
{"event": {"id": 68, "name": "...", "status": "live", "starts_at": "2026-10-18T13:30:00Z", "ends_at": "2026-10-18T16:30:00Z"},
 "banner": null,
 "poll": {"id": "...", "kind": "choice", "prompt": "Which track?"},
 "quiz": {"live": false, "title": "Hack Night quiz"},
 "qa": {"open": true, "has_questions": true}}
```

`event` is the `Feed`'s `event` with the event's start and end; `banner` is
the `Feed`'s `banner` (the latest update, for three hours after it was
pushed, or `null`); `poll` the poll that is up, or `null`; `quiz` the
`Feed`'s `quiz`; `qa` whether the floor is open and whether its board has a
question. A snapshot comes when one of them changes, not when the audience
votes or asks: a quiet strip gets pings. Gone (404) where the room is. The
same body is `GET /events/{id}/live/banner`, for a client that polls (every
30 s; public, weak ETag).

## 3. Reconnecting and resuming

- Reconnect with **`Last-Event-ID: <last id>`** (native `EventSource` does
  this itself on its automatic reconnects; after a page reload pass
  `?last_event_id=`). If nothing changed since that version the server sends
  no snapshot (a `ping` instead); otherwise the current snapshot, once.
- Without a last id the first frame after `retry` is always a snapshot.
- Delay: `retry` ms for the first reconnect, then double per consecutive
  failure up to 30 s, each with 0–100 % random jitter added (a whole hall
  reconnects at once after a deploy).
- A connection counts as failed if no frame (snapshot or ping) arrives within
  20 s of opening it. While connected, if nothing arrives for **45 s** (three
  missed pings), abort it and reconnect.
- Close the stream when the page or app is hidden for more than ~30 s, and
  reconnect (with `Last-Event-ID`) when it comes back.

## 4. When the server ends a stream

A healthy stream is ended by the server after about 10 minutes (5 for the
club's views), with a jittered `retry:` first; a deploy ends them all. Both
are a normal end of the response: reconnect after `retry` ms with
`Last-Event-ID`. Through the clubs gateway, Vercel also ends the function at
its maximum duration: the same thing.

## 5. When to poll instead

Try the stream first; poll exactly as today whenever it is not available:

| the stream answers | meaning | do |
|---|---|---|
| **404**, **405** or **501**, or a 2xx that is not `text/event-stream` | streams are off (`error_code: "LIVE_STREAM_OFF"`), an older server, or no such room | poll as today for the rest of this page/session (re-try the stream after 10 min at most once); the poll itself says whether the room exists |
| **503** `LIVE_STREAM_FULL`, or **429** | too many streams on that server, or too many opens | poll; try the stream again after `Retry-After` seconds (60 if absent). A page on another origin can read it: the API's CORS exposes `Retry-After` |
| **401** / **403** | as the poll: quiz → join again; club views → refresh the token / sign in | handle as the poll does, then reconnect |
| **422** | a `view` that is not one of the seven | a client bug: poll |
| network error, or 3 failed reconnects in a row (3) | | poll; try the stream again after 5 minutes |

While polling as a fallback, use today's intervals (room 5 s, quiz 2 s, host
2 s, screen 3 s, questions 5 s, polls 3 s). The writes (vote, ask, respond,
join, answer, the host's actions) are unchanged REST calls either way, and
their responses are still the immediate feedback for the tap; the stream then
carries the room's view of it.

## 6. Headers and proxies

The server sends `Cache-Control: no-cache, no-transform` and
`X-Accel-Buffering: no` (nginx then passes frames through unbuffered even
where `proxy_buffering` is on). For the API's nginx, a location for the
stream (the server's switch-on notes have the full block):

```
location ~ ^/api/v1/events/\d+/live/stream$ {
    proxy_pass http://localhost:8001;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_cache off;
    gzip off;
    proxy_read_timeout 120s;   # pings every 15 s
    send_timeout 60s;          # a client that stops reading is let go
    access_log /var/log/nginx/access.log moa_stream;   # the path only: no keys from the query
    # + the usual Host / X-Real-IP / X-Forwarded-* lines
}
```

(`moa_stream` is a `log_format` with `$uri` where the default has
`$request`; the repository's nginx.conf defines it.)

and `worker_connections` (and `worker_rlimit_nofile`) large enough for two
connections per open stream. Cloudflare passes `text/event-stream` through;
its 100 s idle limit is far above the 15 s ping.

## 7. Limits

Per server process: a ceiling on open streams, one per event, and one per
address sized for a full hall on one Wi-Fi (3,000). Opening a stream counts
against the room's per-person and per-address limits (20 opens a minute per
browser or player key, 6,000 per address). Over a ceiling the answer is 503
`LIVE_STREAM_FULL` with `Retry-After`; poll meanwhile.

## 8. Rollout

1. Server deployed with `APP_LIVE_SSE` unset: the stream answers 404, every
   client keeps polling.
2. nginx gets the location above (and the connection limits).
3. Clients ship with "stream first, poll on 404". Nothing changes yet.
4. `APP_LIVE_SSE=true` on the backend: clients move to the stream as they
   reconnect. Unsetting it puts them all back on the poll (404).

## 9. Notes from the clients

- The website and the app open a stream only while an event is live; before
  it starts and after it ends they do not.
- When the stream is not available, the app falls back to a single fetch of
  the view, not to a polling timer.

## Client sketches

Browser (native, room):
```js
const url = `${API}/events/${id}/live/stream?view=room&key=${key}` + (last ? `&last_event_id=${last}` : "");
const es = new EventSource(url);
es.addEventListener("snapshot", (e) => { last = e.lastEventId; render(JSON.parse(e.data)); });
es.addEventListener("gone", (e) => { es.close(); pollOnce(); });
es.onerror = () => { /* EventSource retries itself; count failures, fall back after 3 (5) */ };
```
`EventSource` cannot read the status of a failed open: open with `fetch`
first (or use fetch-based streaming) when you need to tell 404 from 503.

Flutter (`package:http`):
```dart
final req = http.Request('GET', Uri.parse('$api/events/$id/live/stream?view=quiz'))
  ..headers.addAll({'Accept': 'text/event-stream', 'X-Live-Player': playerKey, if (last != null) 'Last-Event-ID': last});
final res = await client.send(req);
if (res.statusCode != 200 || !(res.headers['content-type'] ?? '').startsWith('text/event-stream')) { /* section 5 */ }
res.stream.transform(utf8.decoder).transform(const LineSplitter()).listen(onLine); // fields: event, id, data, retry; blank line ends a frame
```
