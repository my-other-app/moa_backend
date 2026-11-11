# The Integrations API

Status: built in the backend's `feat/partner-api` (agent A: the core). The
outgoing webhooks are `feat/api-webhooks` (agent B, `docs/WEBHOOKS.md`); the
lead merges and wires the two. The shared truth is
`feature-register/integrations-api-contract.md`.

Other platforms -- a club's own website or ERP, another event tool, a college
system -- work with MyOtherApp both ways: when they publish an event,
register someone, check someone in or issue a certificate, MyOtherApp changes
too (this API); when it happens on MyOtherApp, they are told (webhooks).

- Base URL: `https://api.myotherapp.in/partner/v1` (versioned; v1 is additive only, a breaking change is v2 with six months' overlap).
- The OpenAPI 3.1 document: `GET /partner/v1/openapi.json` (no sign-in). The developer docs at `https://myotherapp.in/developers` render it.
- Code: `backend/app/api/partner/` (each module's docstring says what it does). Tests: `backend/tests/test_partner_api.py`.
- Kill switch: `APP_PARTNER_API_ENABLED` (on; nothing reaches a club unless a platform admin allocated it).

---

## 1. Who gets what

**Clubs** (owner and admins only; editors and door staff never see it):
Club dashboard -> Settings -> Integrations, visible once the MyOtherApp team
allocated API access: API keys (create, shown once, scopes in plain words,
live or test, expiry, IP allowlist, last used, rotate, revoke), Connected apps
(partner platforms, their scopes, who connected them, disconnect), Usage (30
days), the DPDP tick for attendees' details, and per event "Accept
registrations paid outside MyOtherApp" (Places and money).

**Partners** (platforms for many clubs): registered by a platform admin (no
self-registration), with the scopes they may ever ask for. Each club's owner
or admin connects them with OAuth 2.0 (authorization code + PKCE); a partner
the admin allowed `clubs:create` may also set up new clubs, which wait for
approval like a self-signup.

**Platform admins**: Admin portal -> Integrations: allocate API access per
club (on/off, rate tier, max keys, notes), register and suspend partner apps,
see usage.

## 2. The decisions (12 Nov, taken for the owner)

1. A separate, versioned, documented surface (`/partner/v1`); the internal `/api/v1` is never a public contract.
2. Two ways in: club API keys (`moa_live_` / `moa_test_` + 40 characters, sha256 only, shown once) and partner apps (admin-registered, OAuth 2.0 + PKCE + refresh).
3. Allocation: nothing is on by default; the admin's per-club row always wins; plan key `api_access` declared, off on every plan.
4. Least-privilege scopes, attendee contact data separate (`attendees:read`); `clubs:create` partner-only and admin-granted.
5. Money never moves: no refunds, no payouts, no marking an online payment paid. Paid events refuse API registrations (409 `PAYMENT_REQUIRED` with the checkout URL) unless the event accepts payments taken outside MyOtherApp; cancelling an event with online payments is the dashboard's (409 `CANCEL_IN_DASHBOARD`).
6. No echo loops: every change through the API carries its origin (`app/core/api_origin.py`); webhooks name it.
7. External ids: `external_id` (<=128) unique per (client, resource); a second POST is 409 `ALREADY_EXISTS` with the existing id.
8. `Idempotency-Key` on POST and DELETE, 24 hours per client; a different body is 422 `IDEMPOTENCY_MISMATCH`.
9. ETags on every object; PATCH with `If-Match` is 412 `PRECONDITION_FAILED` when stale; lists take `updated_since`.
10. Test mode: test credentials see and change only test objects (`livemode: false`), never public, no emails/WhatsApp/push, no payments.
11. Personal data (DPDP Act 2023): granting `attendees:read` asks the club to tick that its form tells people; contact fields are left out without it; every read of them is counted in the audit log.
12. `clubs:create`: a club pending approval, the owner invited by email, the partner connected; the admin still approves.

## 3. How it is built

- **One route class** (`routing.PartnerRoute`) wraps every `/partner/v1` route: the error shape, `X-Request-Id`, `X-RateLimit-*`, `Cache-Control: no-store`, the idempotent replay, the answer kept for its key, the request counted (`partner_usage_daily`), and the origin reset.
- **One auth dependency** (`auth.partner_caller`, and `require_scope(*names)` on each route): the bearer credential, the club as it is now (exists, approved, allocated, its account active), the scopes, the rate limit, the origin, the Idempotency-Key. It gives a `PartnerCaller(club_id, client_id, client_type, scopes, livemode, ...)`.
- **Live credentials go through the domain services** (`live.py`), never the models: events by `events.service.create_event` / `update_event` (the dashboard's forms), the plan gates the dashboard's routes check, `ticketing.types.follow_event_edit`, publishing by `closeout.service.update_settings`, cancelling by `club_ops.cancel`; registrations by `registration.service.register_event` as the organiser adding a guest (capacity, ticket types, answers, the plan's caps, the waiting list, the ticket email), requests by `approvals.submit`, corrections by `update_registration`, cancelling by `take_off_list` / `approvals.withdraw`; check-ins by `checkins.service.scan` and `undo`; certificates by `certificates.service.issue` (now also for named registrations) and the deliveries queue; the club by `clubs.service.update_club` / `create_club`. So capacity, approvals, waiting lists, emails and webhooks behave the same whoever made the change.
- **Test credentials go to the sandbox** (`sandbox.py`, table `partner_sandbox_objects`): the same shapes and checks, never the real tables. See section 9.
- **Shapes** (`app/api/partner/shapes.py`): plain functions, one per resource; what every GET/POST/PATCH answers and what a webhook's `data.object` should carry: `event_object`, `registration_object` (+ `RegistrationContext`), `request_object`, `ticket_object`, `payment_object` (a registration's money), `payment_log_object` (payment.completed / payment.refunded: amounts and status only), `checkin_object`, `certificate_object`, `club_object`; with a session, `event_payload(session, event)` and `registration_payload(session, reg, event=None, contact=True)`.
- **Audit**: every write is a line (`partner.<resource>.<verb>`, as the club's account, `details.via = "partner_api"` with the client); every read of attendees' details is a `partner.attendees.read` line with the count, never the values.

## 4. Authentication

`Authorization: Bearer <key or access token>`. `GET /partner/v1/me` says who it is.

### Club API keys

Made by the club's owner or an admin, while the club is allocated, at most
`max_keys` live (default 5). Each has scopes, optional expiry, optional IP
allowlist (addresses or CIDR ranges; the caller's address as `app/core/net.py`
reads it). Rotating keeps the key (its id and `client_id`, which webhooks
name as the origin) with a new secret; the old one ends at once.

### Partner apps (OAuth 2.0)

| | |
|---|---|
| Authorize | `GET https://api.myotherapp.in/partner/oauth/authorize?response_type=code&client_id=...&redirect_uri=...&scope=...&state=...&code_challenge=...&code_challenge_method=S256[&mode=test]` |
| Token | `POST https://api.myotherapp.in/partner/oauth/token` (client secret by HTTP Basic or the form) |
| Revoke | `POST https://api.myotherapp.in/partner/oauth/revoke` |

The authorize URL checks the app and its redirect URI, then sends the person
on to the club dashboard's consent page
(`APP_PARTNER_CONSENT_URL`, default `{dashboard}/integrations/connect?<the same query>`).
The page signs the person in as the club (owner or admin), shows the app and
the scopes, asks for the DPDP tick when `attendees:read` is asked, and redirects
back with `code` and `state` (and `iss`).

- `grant_type=authorization_code` (with `code_verifier`; S256 only): a connection (app, club, scopes, live or test) and its tokens. A club's earlier connection to the same app in the same mode ends. A code used twice ends the connection its first use made.
- `grant_type=refresh_token`: rotates; a spent refresh token presented again ends the connection.
- `grant_type=client_credentials`: the app's own token, for no club (`/me`, `GET /clubs`, `POST /clubs` with `clubs:create`), or, with the header `MOA-Club-Id: <id>`, for one of its connected clubs with that connection's scopes (how a partner works for clubs it created). No refresh token.
- Access tokens last `APP_PARTNER_ACCESS_TOKEN_MINUTES` (60); refresh tokens `APP_PARTNER_REFRESH_TOKEN_DAYS` (90).
- Suspending an app refuses every token it holds (403 `APP_SUSPENDED`) until it is resumed; disconnecting ends a connection's tokens at once.

## 5. Scopes

| Scope | Lets a system |
|---|---|
| `club:read` / `club:write` | read / edit the club's profile (name, tagline, about, links, logo; never ownership, team or plan) |
| `events:read` / `events:write` | read events (drafts too) / create (always a draft) and edit them |
| `events:publish` | publish a draft, cancel an event |
| `registrations:read` / `registrations:write` | read registrations (ids, status, tickets, check-ins) / add, correct, cancel them |
| `attendees:read` | see names, emails, phones and answers (the DPDP tick) |
| `checkins:write` | check in, undo |
| `certificates:read` / `certificates:write` | read and verify / issue certificates |
| `webhooks:manage` | the webhook endpoints (agent B) |
| `clubs:create` | partner apps only, admin-granted: create clubs |

A read route takes its `:read` scope or the matching `:write` one. Cancelling
an event takes `events:publish`. `GET /events/{id}/checkins` takes
`registrations:read` or `checkins:write`.

## 6. Every answer

- Success: the object (with its `ETag`), or `{data: [...], next_cursor}` (`limit` <= 100, default 25; `cursor` opaque).
- Errors: status + `{"error": {"code", "message", "details"?, "request_id"}}`. Codes: `UNAUTHENTICATED` 401; `FORBIDDEN_SCOPE`, `NOT_ALLOCATED`, `CLUB_NOT_ACTIVE`, `APP_SUSPENDED`, `NOT_CONNECTED`, `IP_NOT_ALLOWED` 403; `NOT_FOUND` 404; `VALIDATION`, `FIELD_READ_ONLY` 422 (`details.fields`); `ALREADY_EXISTS`, `ALREADY_REGISTERED`, `IDEMPOTENCY_IN_PROGRESS`, `INVALID_STATE`, `PAYMENT_REQUIRED`, `EXTERNAL_PAYMENTS_OFF`, `CANCEL_IN_DASHBOARD`, `EVENT_FULL`, `REGISTRATION_CLOSED`, `ALREADY_CHECKED_IN`, `EVENT_NOT_OPEN`, `TICKET_REVOKED` 409; `IDEMPOTENCY_MISMATCH` 422; `PRECONDITION_FAILED` 412; `RATE_LIMITED` 429. A domain refusal keeps its own code (`TICKET_TYPE_SOLD_OUT`, `TICKET_TYPE_REQUIRED`, the plan's `PLAN_*`).
- Headers: `X-Request-Id` on every answer; `X-RateLimit-Limit/Remaining/Reset` on every authenticated one; `Retry-After` on 429; `Idempotent-Replayed: true` on a replay.
- Rate limits per client and club, a minute: standard 600 reads + 120 writes; high five times that (the club's tier; an app's own token, the app's tier).
- Times ISO 8601 with offset (we answer in +05:30; a time without an offset is refused); money in paise with `currency: "INR"`; ids are strings.

## 7. Syncing both ways

- `external_id` on events, registrations and clubs; `?external_id=` finds the object. Each client's ids are its own.
- `updated_since` on every list: what changed at or after the time, oldest change first (so a page never skips one; an object changed while you page may come twice).
- ETags + `If-Match` on PATCH; `If-None-Match` on GET answers 304.
- Webhooks (agent B) tell you of changes; a change you made is not sent back to your own endpoints unless one opts in.

## 8. The resources

- `GET /me`
- `GET /club`, `PATCH /club`; partners: `GET /clubs`, `POST /clubs` (`clubs:create`)
- `GET /events` (status draft|published|cancelled|ended, starts_after/before, updated_since, external_id), `GET /events/{id}`, `POST /events` (a draft), `PATCH /events/{id}`, `POST /events/{id}/publish`, `POST /events/{id}/cancel` (`{reason}`)
- `GET /events/{id}/registrations` (status confirmed|awaiting_payment|cancelled, or pending_approval|declined|withdrawn for requests under review), `GET /registrations/{id}`, `POST /events/{id}/registrations`, `PATCH /registrations/{id}` (name, answers, external_id), `POST /registrations/{id}/cancel`
- `POST /checkins` (`ticket_code` | `registration_id` | `external_id`, `scanned_at`, `gate`, `device`) -> 201, or 200 `duplicate: true`; `DELETE /checkins/{id}`; `GET /events/{id}/checkins`
- `GET /events/{id}/certificates`, `POST /events/{id}/certificates` (`registration_ids` | `all_checked_in`, `template_id`, `send`) -> 202 job; `GET /certificate-jobs/{id}`; `GET /certificates/{number}`

Notes on the shapes (the OpenAPI document has every field and an example):

- An event's `venue` is written as `{name, address, city, lat, lng}` and kept as one line (`name, address, city`) with a pin; it reads back as `{name, address: null, city: null, lat, lng, map_url}`. `price` null is free. `status` is draft | published | cancelled | ended (closed, archived, or past its end).
- A registration's `status` is confirmed | awaiting_payment | pending_approval | declined | withdrawn | cancelled. `ticket` is `{code, qr_url (signed, a week), status}`. `approval` is set when the event reviews registrations (`approved` with its `request_id`, or `added_directly`). `payment.status` is not_required | awaiting_payment | paid_online | paid_at_door | paid_externally. `person` and `answers` only with `attendees:read`.
- On an event that reviews registrations, `POST .../registrations` makes a request (202, `status: pending_approval`, its id the request's); `GET /registrations/{that id}` follows it once the club confirms it (the registration, with `request_id`).

## 9. Test mode

`moa_test_` keys, and partner connections made with `mode=test` (and app
tokens asked with `mode=test`), work on a per-club sandbox
(`partner_sandbox_objects`): ids start `test_`, every object is
`livemode: false`. Nothing of it is in the real tables, so it is never public,
never in the club's dashboard lists or the plan's monthly counts, and sends no
email, WhatsApp or push. The rules are the live ones: drafts until published,
registration windows, capacity, one registration per address, events that
review registrations, the money rules (a paid test event takes payments
outside MyOtherApp only; `checkout_url` is null), the door refusing a draft,
one check-in per person, certificates once each. `PATCH /club` is refused (the
club is real). `POST /clubs` validates and answers a preview; nothing is made.
Test-object changes are emitted as webhooks with `livemode: false`
(`emitting.py`, once webhooks_out is merged).

## 10. Money and personal data

- `external_payments_allowed` ("Accept registrations paid outside MyOtherApp") is **read-only in the API**: only the club turns it on or off, in the dashboard's event form. A create or edit sending it is 422 `FIELD_READ_ONLY` (live and test); test events always have it off.
- A paid event refuses a registration without `payment` (409 `PAYMENT_REQUIRED`, `details.checkout_url` = the event's page). With the event's `external_payments_allowed` on, `payment: {status: "paid_externally", amount, currency: "INR", reference}` is taken: the paid place is held as a capture holds it (the event's paid count, the ticket type's room), the registration is marked paid at nothing (so finance's income, the ledger and payouts never count it), and the partner's amount is kept in `partner_external_payments`. Off: 409 `EXTERNAL_PAYMENTS_OFF`.
- Cancelling a registration paid online, or at the door, is the dashboard's (409 `CANCEL_IN_DASHBOARD`); one paid outside MyOtherApp is cancelled through the API (its external payment marked cancelled; nothing is refunded by us).
- Cancelling an event with online payments is 409 `CANCEL_IN_DASHBOARD` with the dashboard's link.
- `attendees:read` needs the club's tick ("Our registration form tells people their details are shared with <name>"): on the key (`attendees_disclosure_ack`) or at consent. Partner-created clubs' connections never carry it.

## 11. The dashboards' contract

### Club dashboard (`/api/v1/clubs/integrations`, the club's session; owner and admins)

| | |
|---|---|
| `GET /clubs/integrations` | `{allocated, source, rate_tier, rate_limit, max_keys, live_keys, scopes: [{key, label, description, needs_disclosure}], disclosure, docs_url, api_url}` |
| `GET /clubs/integrations/keys[?include_revoked=true]` | `{keys: [key]}` |
| `POST /clubs/integrations/keys` | `{name, mode: live|test, scopes, expires_at?, ip_allowlist?, attendees_disclosure_ack?}` -> key with `secret` (once). 403 `NOT_ALLOCATED`, 409 `KEY_LIMIT`, 422 `DISCLOSURE_REQUIRED` |
| `POST /clubs/integrations/keys/{id}/rotate` | key with the new `secret` (once) |
| `DELETE /clubs/integrations/keys/{id}` | the revoked key |
| `GET /clubs/integrations/connected-apps` | `{apps: [{id, app: {id, name, logo_url, website, status}, mode, scopes: [{key, label}], via, connected_by, connected_at, last_used_at, attendees_disclosure_ack_at}]}` |
| `DELETE /clubs/integrations/connected-apps/{id}` | `{disconnected: true, id}` |
| `GET /clubs/integrations/usage?days=30` | `{days: [{day, requests, writes, errors, rate_limited, attendee_reads}], clients: [...]}` |
| `GET /clubs/integrations/connect?<the authorize query>` | `{app, club, mode, scopes, redirect_target, disclosure, already_connected}`; 400 `OAUTH_REQUEST_INVALID` with `errors.redirect_to` (send the person there when set, else show the message); 403 `NOT_ALLOCATED` with `errors.redirect_to` |
| `POST /clubs/integrations/connect` | the same fields + `decision: allow|deny`, `scopes: [...]`, `attendees_disclosure_ack` -> `{redirect_to}` (go there) |

A key: `{id, client_id, name, prefix, mode, livemode, scopes, expires_at, ip_allowlist, last_used_at, last_used_ip, created_at, created_by, rotated_at, revoked_at, expired, attendees_disclosure_ack_at}`.

Per event, "Accept registrations paid outside MyOtherApp" is the form field
`external_payments_allowed` on the event's create/update forms (omitted:
unchanged), and is in the event's detail answers.

### Admin portal (`/api/v1/admin/integrations`; reads any admin, changes MANAGE_CONTENT)

`GET /clubs[?enabled=]`, `GET /clubs/{club_id}` (allocation, keys, connected apps, 30 days' usage),
`PUT /clubs/{club_id}` `{enabled, rate_tier, max_keys, notes}`, `DELETE /keys/{key_id}`,
`GET /partner-apps`, `POST /partner-apps` `{name, description?, website?, logo_url?, contact_email?, redirect_uris, allowed_scopes, rate_tier}` -> app with `client_secret` (once),
`GET /partner-apps/{id}` (with connections), `PATCH /partner-apps/{id}`, `POST /partner-apps/{id}/rotate-secret`,
`POST /partner-apps/{id}/suspend` `{reason?}`, `POST /partner-apps/{id}/resume`,
`DELETE /partner-apps/{id}/connections/{connection_id}`, `GET /usage?days=30`.

## 12. Webhooks (agent B)

`app/api/partner/router.py` keeps the marker `# WEBHOOKS_ROUTER_MOUNT`; the
webhook endpoints mount there as PartnerRoutes (same shape, headers and
idempotency):

```python
from app.api.partner.routing import include_partner_routes
from app.api.webhooks_out.partner import make_router as _webhooks_router
include_partner_routes(router, _webhooks_router(require_scope), tags=["webhooks"])
```

`emit(club_id, type, object, origin)` should build `data.object` with
`app/api/partner/shapes.py`, and take `livemode` from the object.

## 13. Settings, data, operations

- `APP_PARTNER_API_ENABLED`, `APP_PARTNER_ACCESS_TOKEN_MINUTES`, `APP_PARTNER_REFRESH_TOKEN_DAYS`, `APP_PARTNER_CONSENT_URL`.
- Migration `partner_api` (after `social_posting`): twelve new empty tables, `events.external_payments_allowed`, `plan_features.api_access`, `checkinvia` gains `api`. Lock-safe (see its docstring).
- Idempotency keys older than a day are cleared as clients make requests; usage is one row per client, club and day.
