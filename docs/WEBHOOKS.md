# Outgoing webhooks

Code: `backend/app/api/webhooks_out`. Contract with the partner API, the dashboards and the developer
docs: `feature-register/integrations-api-contract.md` (Webhooks, and the dated Changes).

When something changes on MyOtherApp -- an event is published, someone registers, a guest is checked in,
a certificate is issued, a payment goes through -- MyOtherApp tells the club's own systems (its website,
its ERP, a college system) or a partner app acting for the club, by POSTing the change to an address
they gave. Whatever made the change (the dashboard, the app, the website, the partner API, a sweep), the
same webhook goes out once. Each is signed, retried when the address does not answer, and logged for
30 days so it can be sent again.

Only for clubs the platform admin has given API access (Admin -> Integrations). Club owners and
admins manage their endpoints in the club dashboard (Settings -> Integrations); a club API key or a
partner app with `webhooks:manage` manages them on the partner API.

## Setup (the owner)

| Setting | What it is |
| --- | --- |
| `APP_WEBHOOK_KEYS_SECRET` | The Fernet key every endpoint's signing secret is encrypted with. **Required**: without it no endpoint can be added, no secret rotated, and nothing is queued; the dashboard says why. Its own key: never reuse `PAYMENT_KEYS_SECRET`, `AI_KEYS_SECRET`, `WHATSAPP_KEYS_SECRET` or `SOCIAL_KEYS_SECRET`. A comma-separated list rotates it (the first encrypts, all decrypt). |
| `APP_WEBHOOKS_SETTLE_SECONDS` | Seconds a change waits before it is built and queued (default 2), so one save's steps go out as the object they left. |

Generate the key on the server and keep a copy somewhere safe (lose it and every endpoint must be
added again):

```
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

The migration is `api_webhooks` (three new, empty tables), after the partner API's `partner_api`.
Allocation is the partner API's (Admin -> Integrations, or a plan with `api_access`), and its kill switch
`APP_PARTNER_API_ENABLED` stops webhooks too: off, nothing is queued for anyone.

## Event types

| Type | `data.object` | When |
| --- | --- | --- |
| `event.created` | event | An event is made (a draft, a copy, or published straight away) |
| `event.updated` | event | Its details change: name, dates, place, price, places, ticket types, form, status (unpublished, ended) |
| `event.published` | event | A draft is published |
| `event.cancelled` | event | It is cancelled (or deleted before anyone registered) |
| `registration.created` | registration | Someone registers, or the club adds them (desk, import, walk-up, waiting list, a confirmed request) |
| `registration.updated` | registration | Their name, contact, answers, ticket type, role or payment changes |
| `registration.approved` | registration | The club confirms a registration request |
| `registration.declined` | registration | The club declines one |
| `registration.cancelled` | registration | Cancelled: by the person, by the club, never paid for, or their account deleted |
| `checkin.created` | checkin | Checked in at the gate, a kiosk, by a volunteer or by hand |
| `checkin.deleted` | checkin | A check-in undone |
| `certificate.issued` | certificate | A certificate issued |
| `payment.completed` | payment | A ticket's payment went through (amounts and status only: `amount`, `amount_refunded`, `status` captured / partially_refunded / refunded) |
| `payment.refunded` | payment | Refunded, in full or in part (`amount_refunded`, `status`) |
| `club.updated` | club | The club's public profile: name, tagline, about, links, logo, cover |
| `webhook.test` | test | Sent by "Send test" to one endpoint only |

An endpoint subscribes to a list of types, or `["*"]` for all. A change goes to the club that hosts
the event (not its co-hosts).

## The body

```json
{
  "id": "evt_5f0c2a9d8b7e6f1a2b3c4d5e",
  "type": "registration.updated",
  "created_at": "2026-11-12T18:04:11.204512+05:30",
  "livemode": true,
  "club_id": "45",
  "origin": {"type": "api_key", "client_id": "key_7Hc..."},
  "data": {
    "object": {"object": "registration", "id": "3a15829d-...", "event_id": "1201", "status": "confirmed", "...": "..."},
    "previous": {"person": {"name": "Asha K", "email": "asha@example.com", "phone": null}}
  }
}
```

- `origin.type` is `dashboard`, `app`, `website`, `api_key`, `partner_app` or `system` (a sweep, a
  Razorpay webhook); `client_id` for API changes.
- `data.object` is the object in the shape the partner API's GET returns -- built by the partner API's
  own builders (app/api/partner/shapes.py) through objects.py -- as it stood a moment after the
  change committed. An event deleted before anyone registered reads `status: "cancelled"` (the GET no
  longer finds it). `previous` (updates) names the top-level fields that changed, as
  they were.
- Ids are strings; times are ISO 8601 in India's time with the offset; money is in paise
  (`"amount": 49900`) with `"currency": "INR"`.

### People's details (DPDP)

A registration's `person` (name, email, phone) and `answers`, and a certificate's `name`,
are sent only to an endpoint whose owner may have them -- otherwise the fields are left out of
`data.object` and of `previous`:

- a club's own endpoint: when the club ticked "Our registration form tells people their details are
  shared with <the endpoint's host>" for it (`share_attendee_details` + `dpdp_confirmed`; changing
  the address clears the tick). Through the API, ticking it needs the key's own `attendees:read`;
- a partner's endpoint: when the partner's connection to the club has `attendees:read`.

A partner's endpoint gets each kind of object only while its connection has that object's read scope
(`events:read`, `registrations:read` for registrations, check-ins and payments, `certificates:read`,
`club:read`). Payments carry amounts and status only, never anyone's details.

## Headers and the signature

| Header | |
| --- | --- |
| `MOA-Signature` | `t=<unix seconds>,v1=<hex HMAC-SHA256(secret, "<t>.<raw body>")>` |
| `MOA-Event-Id` | The `id` of the body: keep it to drop a repeat |
| `MOA-Event-Type`, `MOA-Delivery-Id`, `MOA-Delivery-Attempt` | For logs |
| `Content-Type` | `application/json` |
| `User-Agent` | `MyOtherApp-Webhooks/1.0 (+https://myotherapp.in/developers)` |

The HMAC key is the whole secret as shown (`whsec_...`, prefix included). Check the signature over the
**raw** body bytes before parsing it, and reject a `t` more than 5 minutes from now. During a secret
rotation's overlap the header carries a second `v1=`; accept the request when any one matches.

Node:

```js
const crypto = require("crypto");
function verify(rawBody, header, secret) {
  const sigs = [];
  let t = null;
  for (const item of header.split(",")) {
    const [k, v] = item.split("=");
    if (k === "t") t = Number(v); else if (k === "v1") sigs.push(v);
  }
  if (!t || Math.abs(Date.now() / 1000 - t) > 300) return false;
  const expected = crypto.createHmac("sha256", secret).update(`${t}.`).update(rawBody).digest("hex");
  return sigs.some(s => s.length === expected.length && crypto.timingSafeEqual(Buffer.from(s), Buffer.from(expected)));
}
```

Python:

```python
import hashlib, hmac, time

def verify(raw_body: bytes, header: str, secret: str) -> bool:
    t, sigs = None, []
    for item in header.split(","):
        k, _, v = item.strip().partition("=")
        if k == "t" and v.isdigit():
            t = int(v)
        elif k == "v1":
            sigs.append(v)
    if t is None or abs(time.time() - t) > 300:
        return False
    expected = hmac.new(secret.encode(), f"{t}.".encode() + raw_body, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in sigs)
```

PHP:

```php
function moa_verify(string $raw, string $header, string $secret): bool {
    $t = null; $sigs = [];
    foreach (explode(",", $header) as $item) {
        [$k, $v] = array_pad(explode("=", trim($item), 2), 2, "");
        if ($k === "t") $t = (int) $v; elseif ($k === "v1") $sigs[] = $v;
    }
    if (!$t || abs(time() - $t) > 300) return false;
    $expected = hash_hmac("sha256", $t . "." . $raw, $secret);
    foreach ($sigs as $s) if (hash_equals($expected, $s)) return true;
    return false;
}
```

`app/api/webhooks_out/crypto.py` has `sign`, `header` and `verify` (the tests use `verify` exactly as
above).

## Delivery

- **At least once.** Answer 2xx within **10 seconds** and the delivery is done; do the work after
  answering. Drop repeats by `MOA-Event-Id`.
- **Retries**: any other answer, a timeout, a refused connection or a redirect (3xx is not followed)
  is tried again after 1 minute, 5 minutes, 30 minutes, 2 hours, 6 hours, 12 hours and 24 hours --
  eight tries in all, then the delivery has failed (it can still be sent again by hand).
- **In order per object, as far as possible**: a delivery waits (3 seconds, up to 10 times) while an
  older change to the same object has not yet gone to that endpoint. A retried delivery can still
  arrive after a newer one: use the object's `updated_at` to keep the newest.
- **Turned off after three days**: an endpoint whose deliveries have failed for three days with no
  success between is turned off, what it still had to send is failed, and its owner is told (the
  club's bell, `webhook_disabled`, and a mail to the club's owner; for a partner's endpoint the
  partner's contact, or the platform admins until the partner API says who that is). Fix the
  endpoint, turn it back on (`PATCH {enabled: true}`), and send the failed deliveries again from the log.
- **Only public https addresses**: `https://` only, no user name or password, and the host must
  resolve to public addresses only -- never a private range, loopback, link-local (the cloud's
  metadata service), shared or reserved addresses. Checked when the endpoint is added and again at
  every send; the connection goes to the address that was checked (with the host name for TLS), so a
  DNS answer that changes in between cannot reach the server's own network. A refused address is a
  failed try ("Not sent: ...").
- **Test mode**: an endpoint is live or test (`livemode`). Test-mode objects (made with `moa_test_`
  keys) go only to test endpoints, live ones only to live endpoints; never both.
- **No echo**: a change made by a client is not sent to the endpoints that client owns, unless the
  endpoint has `include_own_changes: true`. An endpoint added with an API key belongs to that key, a
  partner's to the partner app; one added in the dashboard belongs to the key named in its
  `client_id` (the key the same system uses), or to nobody.

## Managing endpoints

Club dashboard (`/api/v1/clubs/integrations/webhooks`, the club's owner and admins only):

| | |
| --- | --- |
| `GET /status` | `available` (and why not: `unavailable` without the key, `not_allocated`), the event types |
| `GET /event-types` | Every type with its description |
| `GET ""` / `POST ""` | The club's endpoints, each with `last_response_status` and `last_delivery_at` (a partner's shown read-only, `owner.type == "partner_app"`) / add one: `url`, `events`, `description`, `include_own_changes`, `livemode`, `share_attendee_details` + `dpdp_confirmed`, `client_id`. The answer carries `secret` -- once |
| `GET/PATCH/DELETE /{id}` | One endpoint; change it (`enabled` turns it off and on); delete it with its log |
| `POST /{id}/rotate-secret` | A new secret, once; `keep_previous_hours` (0-72, default 24) the old one still signs too |
| `POST /{id}/test` | A `webhook.test` sent now: `{ok, delivery}` with the answer's status and body |
| `GET /{id}/deliveries` | The log, newest first: `status`, `event_type`, `limit`, `cursor` -> `{data, next_cursor}` |
| `GET /deliveries/{id}` | One delivery with `request_body` as it is sent |
| `POST /deliveries/{id}/retry` | Send it again now, whatever it was (a replay); 409 on an endpoint turned off |

Partner API (`/partner/v1`, `webhooks:manage`; `partner.make_router(require_scope)`, mounted in
app/api/partner/router.py at `# WEBHOOKS_ROUTER_MOUNT` as PartnerRoutes -- the API's error shape, headers,
rate limits and Idempotency-Key -- and listed in `/partner/v1/openapi.json`, which also carries every
webhook's body under OpenAPI 3.1 `webhooks`): `GET/POST /webhook-endpoints`, `GET/PATCH/DELETE
/webhook-endpoints/{id}`, `POST .../rotate-secret`, `POST .../test`, `GET .../deliveries`,
`GET /webhook-deliveries/{id}`, `POST /webhook-deliveries/{id}/retry`, `GET /webhook-event-types`.
A key manages the club's own endpoints, in the key's own mode; a partner only its own for that club (an app's own
token acts for no club: FORBIDDEN_SCOPE, or name the club with `MOA-Club-Id`).

Admin portal (`/api/v1/admin/integrations/webhooks`): `GET /health` (endpoints on, turned off for
failing, failing now; deliveries per day; the busiest clubs; failing endpoints with their last error),
`GET /endpoints` (any club's, `club_id`, `state`), `GET /endpoints/{id}/deliveries`,
`PATCH /endpoints/{id} {enabled}` (moderators: turned off `by_admin`), `GET /events/{id}` (one change
as sent).

Limits: 10 endpoints per club, 5 per partner per club.

## How it works (for the next developer)

- **`emit(session, club_id, type, object_builder, origin=None, *, previous=None)`** (emit.py) is called
  by the change itself, in its transaction. When the club has an endpoint on that wants the type (one
  indexed query; nothing else for a club without one) and is allocated, it adds one `webhook_events`
  row and one `webhooks.fanout` job (app/core/jobs.py) to the caller's transaction -- the durable
  after-commit of app/core/after_commit.py: a rollback or a SAVEPOINT rolled back takes both away, so
  a change that did not happen is never told; a commit cannot lose it. It never raises (each write in
  its own SAVEPOINT).
- `object_builder` is a `Ref(kind, id)`, loaded at the fan-out (objects.LOADERS), or a dict -- the
  object now, for one that will be gone (`checkin.deleted`).
- **signals.py** wraps emit for each change and lists every choke point it is called from. Updates take
  a `before` (the object, or None when nobody wants the type -- then nothing is read) and `changed`
  emits only when something in the object's shape changed, with `previous`.
- **delivery.py**: the fan-out (matching, modes, no echo, partner scopes), one job per try, the claim
  committed before sending, the record, the retry schedule, turning off.
- **sender.py** sends (the SSRF guard: `app/core/net.py` `check_https_url`, `resolve_public`); tests swap
  `TRANSPORT` and `RESOLVE`.
- **seams.py** / **wiring.py**: what is asked of the partner API -- `club_allocated`, `partner_scopes` (the
  connection's, in the endpoint's mode), `partner_contact`, `external_id` -- installed at start-up from doth
  (`wiring.install()`; tests/test_webhooks_e2e.py fails if it is not). Tests swap them with `seams.configure`.
- **openapi.py**: the webhooks in the partner API's OpenAPI document (one POST per type, the GET's schemas).
- **origin.py**: `SurfaceMiddleware` labels each request's surface for `origin` (the partner API's auth
  dependency sets its own with `app.core.api_origin.set_origin`).
- **sweep.py**: hourly, changes past 30 days deleted (their deliveries with them), rotated secrets'
  overlaps emptied.
- To add a type: add it to `types.EVENT_TYPES`, its loader to `objects.LOADERS` if it is a new object,
  a function to signals.py, the call at the change's choke point, a test in
  `tests/test_webhooks_out_wiring.py`, and a dated line in the contract.

### Not told (yet)

- Bulk rewrites with no object to tell about one by one: answers moved from an old question to a new
  one (`missing_answers.move`), admission numbers given out again (`admission.assign`), ticket types
  adopted by old registrations, contact details scrubbed at retention.
- Settings outside the event's shape (the gate, kiosks, ID cards, the store switches, live Q&A,
  retention) and counters (going, views, popularity).
- A club's partners, team and status (approved, rejected, deleted) on its profile.
- Co-host clubs' own endpoints for an event they co-host.
