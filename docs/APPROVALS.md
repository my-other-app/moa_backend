# Review registrations (registration approval)

The contract as built: the backend (`backend/app/api/approvals/`), and what
the club dashboard, the website and the app read from it. Migrations:
`registration_approval` (after `admin_ops`), then `approval_stats` for the
full numbers and the loopholes closed (§7), then `review_emails` for the
club's own review emails (§9). All paths are under `/api/v1`.

The words changed on 28 Oct 2026 (§10): it is a **normal registration**. People
register; the club reviews the registrations, confirms some (they get their
ticket by email) and declines the rest. The switch is "Review registrations
before sending tickets". The code, the routes, the JSON keys, the statuses
and the error codes keep their first names (`requires_approval`, "request",
`pending`/`approved`/…), so older app builds keep working; only what people
read changed.

Everything is additive. An event without `requires_approval` behaves exactly
as before, for every client.

---

## 1. What it does

A club turns on **Review registrations before sending tickets** for an event.
Then:

1. Anyone who fills the registration form is registered **under review**: they
   have no ticket yet. The record is a *request* (`pending`), and they get the
   club's "Thanks for registering" email: "Thank you for registering for
   {event}. {Club} reviews every registration and will get back to you soon."
2. The club sees the requests in the dashboard, and confirms or declines them,
   one at a time or in bulk.
3. **Confirmed, free ticket** (a free type, or a code that makes it free): they
   are registered at that moment, and the event's normal ticket email goes out.
4. **Confirmed, paid ticket**: they get a pay link. The place is held for the
   event's offer hours (the waiting-list setting, 24 by default). Paying issues
   the ticket. Not paying in time releases the place, and the request becomes
   `lapsed`.
5. **Declined**: emailed when the club chooses (on by default), with its note
   if it wrote one.

Product rules:

- A request holds **no place**. Places are taken only by a confirmation (free)
  or a payment (paid, inside the hold). Confirming never goes past the event's
  or the ticket type's capacity: the requests that don't fit are skipped, each
  with a reason.
- One open request (pending or approved) per person per event, by address and
  by account. Sending the form again while one is open answers with it.
- Withdrawn: may ask again. Declined: may not (409 `REQUEST_DECLINED`) unless
  the club reopens it.
- Turning the setting off: new people register directly; requests already
  waiting stay until the club decides. A person who gets a ticket another
  way (the form with the setting off, the desk, an import, added by hand, a
  waiting-list offer) has their open request closed
  (`closed_reason: registered_directly`), in the same transaction, with no
  email (§7).
- No separate waiting list on an event that reviews registrations: the
  review queue is the line (§7). The people in the event's waiting list when
  review goes on become registrations under review, each told once (§8).
- The event cancelled: pending requests, and confirmed paid ones not paid yet,
  close (`closed`, `event_cancelled`), their places go back, and their people
  get the cancellation email. The event over (`event_ended`) or deleted
  (`event_deleted`): the same, and the pending ones get "{Club} wasn't able to
  confirm your registration" (`request_unanswered`, once each).
- Club roles owner, admin and editor decide. Door staff may read the queue but
  not decide (`club_access.policy`; `can().decide_requests`).

---

## 2. Data

### `registration_requests` (new)

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| event_id | int fk events, cascade | |
| user_id | int fk users, set null | the account, or the guest made at submission |
| full_name | varchar(100) | |
| email | varchar(100) | as wide as the registration's own column |
| phone | varchar(15) | as wide as the registration's own column |
| ticket_type_id | int fk ticket_types, set null | the type, resolved at submission |
| discount_code | varchar(40) | normalised; checked again on confirmation |
| answers | jsonb | the form's `additional_details` (and `organization`), checked at submission |
| whatsapp_opt_in | bool | "Send my ticket on WhatsApp" |
| club_updates_whatsapp | bool | "Get updates from {club} on WhatsApp", kept on confirmation |
| tracking_link_id | uuid fk tracking_links, set null | credited on confirmation |
| status | varchar(12) | `pending` `approved` `declined` `withdrawn` `lapsed` `closed` |
| token | varchar(64), unique | the status link's secret (`secrets.token_urlsafe(32)`, 43 characters) |
| submitted_at | timestamptz | the queue's order is `(submitted_at, id)` |
| decided_at, decided_by_id, decided_by_name | | who decided, by their own name for a team member |
| decline_note | varchar(300) | |
| amount | numeric(10,2) | a confirmed paid ticket's price, its code taken off |
| registration_id | uuid fk event_registrations_link, set null | set when the registration exists (free: at confirmation; paid: at the claim) |
| waitlist_entry_id | uuid fk waitlist_entries, set null | the paid hold |
| resent_at | timestamptz | when the "received" email last went again |
| closed_reason | varchar(24) | why `closed`: `registered_directly`, `event_ended`, `event_deleted`, `event_cancelled` (check constraint); null otherwise. Migration `approval_stats` |
| created_at, updated_at | timestamptz | |

Indexes:

| index | on | for |
|---|---|---|
| `ix_registration_requests_queue` | `(event_id, status, submitted_at, id)` | the list for any status, and the counts |
| `ix_registration_requests_pending` | `(event_id, submitted_at, id) WHERE status = 'pending'` | the queue's pages and the confirmation's lock (no sort); the sweep's walk of events with requests waiting |
| `uq_registration_requests_open_email` | unique `(event_id, lower(btrim(email))) WHERE status IN ('pending','approved')` | one open request per address |
| `uq_registration_requests_open_user` | unique `(event_id, user_id) WHERE status IN (...) AND user_id IS NOT NULL` | one open request per account |
| `uq_registration_requests_token` | unique `(token)` | the status link |
| `ix_registration_requests_user` | `(user_id, submitted_at) WHERE user_id IS NOT NULL` | "my requests" |
| `ix_registration_requests_hold` | `(waitlist_entry_id) WHERE waitlist_entry_id IS NOT NULL` | a claim finding its request, an expiry lapsing it |
| `ix_registration_requests_registration` | `(registration_id) WHERE registration_id IS NOT NULL` | the registration's deletion (the FK's SET NULL); telling a confirmed registration from one added directly (`approval_stats`) |

### Columns added

- `events.requires_approval boolean not null default false`.
- `events.high_demand_label boolean not null default false` (`approval_stats`):
  the club's public "High demand" label (§7).
- `waitlist_entries.via_request boolean not null default false`: marks the
  offers that are confirmations' holds. The club's waiting list (its entries,
  counts, export, offer and remove), "my places in line" and deleting a ticket
  type leave them out. They still hold their places like any offer.

Requests never live in `event_registrations_link`, so everything that reads
registrations (the door, certificates, journeys, exports, counts, the ledger)
never sees one until it is confirmed.

---

## 3. How it works

### Submitting (`approvals.service.submit`)

Under the address's advisory lock, as a registration takes it
(`registration.service.hold_address`):

1. The event is open: not cancelled, inside its registration window, not over
   (400 `REGISTRATIONS_CLOSED` with `closed_reason`).
2. The answers are checked against the event's questions; a WhatsApp tick
   needs a number WhatsApp can reach (422 `WHATSAPP_NUMBER`).
3. An open request for this address or account: answered with it.
4. A declined one: 409 `REQUEST_DECLINED`.
5. The ticket type, its sales window, the per-person limit and the code are
   checked as a registration checks them (`ticketing.quote`, without a lock
   and without the places: a request holds none).
6. The public form gets a guest of its own (as a registration does), and a
   session for it.
7. The row is inserted in a SAVEPOINT; the "received" email is a job in the
   same transaction.

### Confirming (`approvals.service.approve`)

`ids` (at most 500) or `all_pending` with `limit` (the oldest first). In
chunks of at most 100, one transaction each:

1. `SELECT … WHERE id = ANY(:ids) AND event_id = :e AND status = 'pending'
   ORDER BY submitted_at, id FOR UPDATE SKIP LOCKED`. An id not returned is
   skipped `not_pending`: decided already, another person is confirming it
   right now, or not this event's.
2. The event gone, cancelled or over: every row skipped `event_closed`. Full
   already (an earlier chunk or row): `event_full` without more work.
3. The addresses' locks, sorted, then the rows' discount codes (by id), before
   the event's row: the order every registration takes them in.
4. Each row in a SAVEPOINT of its own:
   - Already registered (by account or address): `already_registered`.
   - Priced again (`ticketing.quote` with `window=False`: a type whose sales
     ended since may still be confirmed). A code that no longer applies:
     `code_invalid` (the club decides; never charged full price behind the
     person's back). A code with a limit on its uses counts the holds of
     earlier paid confirmations as uses, so its last use is never promised
     twice.
   - Owes nothing: registered through `registration.service.add_registration`
     (the same function every registration uses: the plan's caps, the event's
     row in one `UPDATE … WHERE going_count < cap`, the type's row). Refusals
     map to `event_full`, `type_sold_out`, `type_unavailable`.
   - Owes money: a hold. The event's row and its types' rows are locked (as
     `waitlist.fill` does), the room counted with the waiting list, and an
     offer made (`via_request`, its own claim token and expiry). The person's
     own place in that type's line, if any, becomes the hold.
5. The jobs that tell people, and (for free tickets) the deliveries outbox's
   ticket rows and their send job, are written; the chunk commits.

A type that sold out skips the rest of that type's rows without more work;
the event full skips the rest of the call.

### Concurrency

- Two people confirming the same rows: `SKIP LOCKED`, and the status read under
  the row lock. The registrations' own unique key `(event, user)` is the last
  line. Tested for real in `tests/test_approvals_races.py`.
- A confirmation and a public registration for the last place: both take the
  event's row (`take_place`, `take_paid_place` or `lock_event`); one finds no
  room. Tested for real, both orders.
- Paid holds from two confirmations at once never outnumber the places (the
  event's row is held while each counts).
- A cancellation closes requests from its follow-up job, after its commit and
  off the event's row, because a confirmation holds request rows before the
  event's.

### Paid tickets: the hold, the claim, the payment

The hold is an offer in the waiting-list machinery (`ticketing/waitlist.py`),
so its place is counted (`places.lines`), it runs out (`waitlist.expire`, the
minute sweep), and it is claimed and paid like any offer:

- `GET /waitlist/offers/{token}` (the claim page) adds `reason: "approved"`
  (`"waitlist"` otherwise), `amount` (the confirmed price) and
  `event.club {name, slug}` (on every offer).
- `POST /waitlist/offers/{token}/claim` registers what the request said:
  its name, phone, answers (not checked again: the form may have changed),
  code and WhatsApp tick, whatever the page sends. The guest the request made
  is the claimant (and gets a session to pay with) unless the account owner
  is signed in. The registration is linked to the request.
- The payment lands (`places.capture_room`): the request is `approved` with
  its `registration_id`, also when it had lapsed and a place was still free.
- The hold runs out: in the same transaction as the expiry, the request
  becomes `lapsed`, the person is emailed, and the place goes to whoever is
  next (`waitlist.fill`). The registration a claim made and nobody paid for
  is retired after that commits (job `approvals.retire_lapsed`): soft
  deleted, so it stops counting as going and as owed. Never once money
  moved or the person was checked in; a checkout opened in the last 30
  minutes is waited for (the job comes back when that window has passed).
- "No thanks" on the claim page, or withdrawing from the status link: the hold
  is given back, the request `withdrawn`.

Pending requests are never `WAITING` entries: they are a table of their own,
so the waiting list's automatic "offer the next person" never picks them.

### The minute sweep (`ticketing/sweep.py`)

After the waiting list's expiry and fills: `approvals.close_finished` finds
the events with requests still pending by a loose index scan of
`ix_registration_requests_pending` (one step per event, however many requests
each has), and closes those of events gone (`event_deleted`), cancelled
(`event_cancelled`, no email: the cancellation sends its own), closed or
over (`event_ended`). The pending ones closed `event_ended` or
`event_deleted` get `request_unanswered`.

### Telling people (`approvals/notify.py`)

One job a message (`approvals.notify`, HIGH), written in the transaction that
decided: nothing goes for a decision that rolled back. Each run reads the
request again and sends only what is still true. Emails carry an idempotency
key, and count against the club's messages like a waiting-list offer
(`ticket` kind).

| kind | when | email (library key; default subject) | push (app accounts only) |
|---|---|---|---|
| `received` | submitted (and again, at most every 10 minutes, when the form is sent again by someone who can't show it is theirs); once for each person moved in from the waiting list (§8) | `request_received`: "Thanks for registering for {event}", status link, cancel link | — |
| `approved` | confirmed, free | `request_approved`: "Your ticket for {event}", the ticket email of confirmed people (deliveries outbox), ticket attached | `request_approved` "{Club} confirmed your registration" |
| `approved_pay` | confirmed, paid | `request_approved_pay`: "Your registration for {event} is confirmed": pay {amount} by {pay_by}, pay link | `request_approved_pay` "{Club} confirmed your registration" |
| `declined` | declined with `notify` | `request_declined`: "Your registration for {event}": "{Club} couldn't confirm your registration this time." + the note | `request_declined` "Your registration for {event}" (and an inbox item) |
| `lapsed` | a hold ran out | `request_lapsed`: "Your registration for {event} has expired": it wasn't paid in time | — |
| `cancelled` | the event cancelled | (not in the library) "Cancelled: {event}", the reason; "Your registration is closed. Nothing was charged." | — |
| `unanswered` | a pending request closed because its event ended or was deleted (once a request: the job's key is the request's) | `request_unanswered`: "Your registration for {event}": "{Club} wasn't able to confirm your registration for {event}." | — |

The emails are the club's: its logo, colour and name as the sender, drawn by
the designed-email renderer (a plain email if that fails). Since
`review_emails` the six keyed ones are templates of the club's email library,
which the club edits (§9); a club that never opened its library gets
MyOtherApp's defaults. A ticket for a registration that came from a request
(free at confirmation, paid after the payment) goes in the club's
`request_approved` email instead of the event's invite.

Push data (as `waitlist_offer`'s): `type`, `token`, `event_id`, `url` (the
status link); `request_approved` adds `ticket_id` (`""` when none);
`request_approved_pay` adds `claim_token` and `claim_url`. WhatsApp: later
(new Meta templates).

### Lifecycle

- Cancellation (`club_ops.cancel`): its follow-up closes the requests not yet
  settled and emails their people, before the registrants' email.
- The event over: the sweep above.
- The event deleted (`DELETE /events/delete/{id}`, allowed: requests are not
  registrations): its requests not yet settled close `event_deleted` in the
  same transaction, holds given back, and the pending ones are told
  (`request_unanswered`).
- Retention scrub (`closeout.scrub`): every request of the event loses its
  name, address (replaced by `removed-<id>@removed.invalid`), phone, answers
  and the club's note. The retention preview counts them (`requests`).
- Guest merge (`users.service.merge_guest_into_user`): the guests' requests
  move to the account (`requests_transferred`), but an open one stays with its
  guest if the account has an open one for that event already.
- Duplicating an event copies `requires_approval`.

---

## 4. API

### Public event data

- `GET /events/info/{id|slug}`: `requires_approval: bool`.
- The `ticketing` object (in the event info, and `GET
  /events/{id}/ticket-types`): `requires_approval: bool`, and for a signed-in
  viewer `viewer_request`: `{id, status, token, status_url}` for their open
  request (pending, or confirmed with a hold not paid yet), else null.
- `GET /discover/events/{ref}`: `event.requires_approval: bool`.

### Register: `POST /events/registration/{event}/register`

Body gains `accepts_approval: bool` (default false).

On an event that requires approval:

- Already registered: answered as before (409 `ALREADY_REGISTERED`,
  `REGISTRATION_AWAITING_PAYMENT`, back to pay for a signed-in person), for
  every client.
- `accepts_approval` false or missing: 409 `APPROVAL_REQUIRED` ("This event
  needs the club to confirm each place. Update the app, or register on
  myotherapp.in.").
- `store` picks: 422 `ADD_ONS_NOT_ALLOWED` (a request holds none).
- Declined before: 409 `REQUEST_DECLINED`.
- Otherwise 200:

```json
{
  "approval": {
    "id": "uuid", "status": "pending", "submitted_at": "…",
    "token": "…", "status_url": "https://myotherapp.in/requests/<token>"
  },
  "event_registration_id": null,
  "event_id": 1,
  "event_name": "…",
  "auth_token": { "…": "only for a new guest, as today" }
}
```

`token` and `status_url` are null in one case: the form sent again, signed
out (or as another person), for an address that already has an open request.
Whoever typed the address has not shown it is theirs; the status link goes to
the address by email instead (at most every 10 minutes). The request's owner
(the account, or the guest whose session made it) always gets them.

### The guest's request

- `GET /registration-requests/{token}`: public, `Cache-Control: no-store`, 30
  a minute per address.

```json
{
  "id": "uuid", "status": "pending|approved|declined|withdrawn|lapsed|closed",
  "event": { "id": 1, "slug": "…", "name": "…", "event_datetime": "…", "poster": "https://…", "club": { "name": "…", "slug": "…" } },
  "ticket_type": { "id": 3, "name": "Student" },
  "full_name": "…", "submitted_at": "…", "decided_at": null,
  "decline_note": null,
  "ticket_id": null,
  "pay": { "amount": 199.0, "pay_by": "…", "claim_token": "…", "claim_url": "https://myotherapp.in/claim/…" },
  "token": "…", "status_url": "https://myotherapp.in/requests/…"
}
```

  `ticket_id` once registered and the ticket holds its place (free, or paid).
  `pay` only for a confirmed paid request inside its hold. `decline_note`
  only when declined. 404 `REQUEST_NOT_FOUND` for an unknown token.
- `POST /registration-requests/{token}/withdraw`: a pending request, or a
  confirmed paid one not paid yet (its hold goes back). 200 with the request;
  withdrawing again is the same. Anything else: 409
  `REQUEST_NOT_WITHDRAWABLE`, with the request as it stands in
  `errors.request`. 10 a minute per address.
- `GET /registration-requests/mine` (signed in, guests included): `{ "items":
  [the same shape] }`, open ones first (pending, confirmed and unpaid), each
  part newest first, at most 200. An account whose address is verified also
  sees its guests' requests under that address.

### The club's (club auth, the event's club)

- `GET /events/registration/{event_id}/requests?status=pending&q=&after=&limit=50`
  (`status`: any of the six, or `all`; `limit` up to 200; `q`: name, email or
  phone contains). Pending is the queue, oldest first; any other status (and
  `all`) newest first. Keyset pages: `next` is the cursor for `after`.

```json
{
  "items": [{
    "id": "uuid", "status": "pending", "full_name": "…", "email": "…", "phone": "…",
    "ticket_type": { "id": 3, "name": "Student" }, "answers": { "…": "…" },
    "submitted_at": "…", "decided_at": null, "decided_by": null,
    "decline_note": null, "registration_id": null, "pay_by": null,
    "amount": null, "discount_code": null, "whatsapp_opt_in": false
  }],
  "next": "cursor|null",
  "counts": { "pending": 0, "approved": 0, "declined": 0, "withdrawn": 0, "lapsed": 0, "closed": 0 },
  "places_left": 40
}
```

  `answers` are keyed by the event's questions, as a registration's
  `additional_details`. `decided_by`: `{id, name}`. `places_left`: the event's
  places someone could still be confirmed into (or, with no total, the types'
  together when every type has a cap); null for no cap.
- `POST …/requests/approve`: `{ "ids": […] }` (at most 500) or `{
  "all_pending": true, "limit": 500 }` → `{ "approved": [{ "id",
  "registration_id", "pay_by" }], "skipped": [{ "id", "reason" }],
  "places_left": 12 }`. Reasons: `not_pending`, `event_full`,
  `type_sold_out`, `type_unavailable`, `already_registered`, `code_invalid`,
  `event_closed`.
- `POST …/requests/decline`: `{ "ids": […], "notify": true, "note": "≤300" }`,
  or `{ "all_pending": true, "limit": 500, "notify": …, "note": … }` →
  `{ "declined": ["uuid"], "skipped": [{ "id", "reason": "not_pending" }] }`.
- `POST …/requests/reopen`: `{ "ids": […] }` → `{ "reopened": [...],
  "skipped": [{ "id", "reason" }] }`, reasons `not_reopenable` (not declined
  or lapsed) and `already_open` (the person has an open request since). A
  reopened request keeps its place in the queue.
- Event settings: `POST /events/create` and `PUT /events/update/{id}` take
  `requires_approval` (form field; omitted leaves it as it is) and return it.
- `GET …/requests/export`: every request as CSV (§7).
- Overviews:
  - `GET /events/{id}/summary` (the event's overview): `requests_pending`,
    and since §7 `requests` and `places`.
  - `GET /clubs/dashboard`: an attention item `REQUESTS_PENDING` (amber) for
    each of the three events whose requests have waited longest.
  - `GET /clubs/overview`: each event (`next_event`, `upcoming`,
    `needs_attention`, `reach`) has `requires_approval` and `requests_pending`.
  - The club's events list: each row `requests_pending`; the figures'
    `attention.requests_pending`: `[{id, name, count}]`.
- `GET /clubs/me/emails/overview`: `automatic`, the emails that go by
  themselves, each `{key, name, subject, when, group, customisable}`
  (`customisable: false` for now).
- `GET /clubs/access/me` (`can`): `decide_requests` (owner, admin, editor).

---

## 5. Contract changes (from the agreed contract)

1. The register response's `approval.token` and `status_url` are null for a
   form sent again by someone who can't show the request is theirs (signed
   out, or as someone else). Returning the token would hand anyone who types
   an address the power to read and withdraw that person's request, the hole
   closed for registrations in October. The link is emailed to the address
   instead. Every other request object carries them.
2. Approved requests whose ticket is settled (registered: free, or paid) stay
   `approved` when the event ends or is cancelled; only pending requests and
   unpaid holds close. Their people are registrants, and get the registrants'
   cancellation email.
3. Additions beyond the contract, all optional to read: `amount`,
   `discount_code` and `whatsapp_opt_in` on the club's list items;
   `event_id`/`event_name` on the register response; `claim_url` in `pay`;
   `amount` on the claim view; the `reopen` response's shape.
4. The request emails were listed in the email library but not customisable
   at first. They are since `review_emails` (§9).

---

## 6. Running it

See docs/OPERATIONS.md §25.

---

## 7. Full numbers for confirm-each events, and the loopholes closed

Contract: `feature-register/approval-stats-contract.md` (agreed 28 Oct
2026). Migration `approval_stats` (after `registration_approval`). Every
field is added; none changes meaning (but `offered` in the sales and types
lists, below).

### Owner decisions, as built

1. **No request numbers in public.** A club may switch on a "High demand"
   label per event (`events.high_demand_label`), shown as a chip, never a
   number.
2. **No separate waiting list** on a confirm-each event: `waitlist.enabled`
   is false whatever the setting says, every type's `waitlist` is false, and
   joining is refused (409 `WAITLIST_OFF_ON_APPROVAL_EVENTS`). People already
   in the line from before the switch are left there, but nothing offers
   them a place (not the sweep's `fill`, not the club's "Offer now": the same
   409), and they hold no place ahead of anyone (`places.compute` leaves
   them out on such an event). Offers already standing still hold, and may
   be claimed or sent again. Turning confirming off brings the line back as
   it was (the setting is kept).
3. **Switching confirm-each off keeps people waiting.** If a waiting person
   gets a ticket another way, their open request (pending, or a paid hold not
   paid yet) is closed `registered_directly` in the registration's own
   transaction (`approvals.settle_direct`, called by
   `registration.service.add_registration`, which every way in uses: the
   form, the desk's walk-up, imports, by hand, a waiting-list claim). A hold
   they had goes back (`left`). No email. The request a confirmation is
   registering, and the hold a claim pays for, are left alone. The request
   rows are taken `SKIP LOCKED` (a registration holds the address and the
   event's row by then, and a confirmation takes request rows first): one a
   confirmation holds at that moment is left to it, and the confirmation,
   finding the registration, skips it `already_registered` and closes it
   `registered_directly` itself.
4. **Unanswered requests get a polite email** (`request_unanswered`) when
   the event ends or is deleted: "{Club} wasn't able to confirm your place
   for {event}." Once a request (its job's key is the request's alone, and a
   closed request never closes again). Only pending ones: a confirmed hold
   closed with its event was answered. A cancellation keeps its own email.

### `GET /events/{id}/summary` gains

```json
"requests": {
  "enabled": true,
  "total": 412, "pending": 37, "approved": 310, "declined": 40,
  "withdrawn": 8, "lapsed": 17, "closed": 0,
  "approved_paid": 281, "approved_awaiting_payment": 12,
  "awaiting_payment_amount": 2388.0, "earliest_hold_expires_at": "…|null",
  "checked_in": 240,
  "oldest_pending_at": "…|null",
  "avg_decision_hours": 20.5,
  "added_directly": 15
},
"places": { "capacity": 300, "taken": 281, "held": 12, "left": 7 }
```

- `requests`: null for an event that does not confirm each place and has no
  requests. Three grouped statements over the event's requests
  (`approvals.stats`), whatever their number.
  - `approved_paid`: confirmed, their registration paid (an amount above 0).
  - `approved_awaiting_payment`, `awaiting_payment_amount`,
    `earliest_hold_expires_at`: confirmed paid ones whose hold still stands
    and is not paid (claimed or not).
  - `checked_in`: confirmed ones whose registration was admitted (marked, or
    a check-in).
  - `avg_decision_hours`: submitted to decided, over every request decided.
  - `added_directly`: the event's live registrations that came from no
    request. The registrations that did and these are `registered` exactly.
- `places` (`ticketing.places.summary`, the ticket picker's own sums):
  `capacity` (the event's cap, or with none the types' together when every
  type has one; null: no cap), `taken` (what the cap counts: paid tickets on
  a paid event, every registration on a free one), `held` (standing offers:
  confirmed requests' holds, and any waiting-list offers), `left` (what
  someone new could have, never below 0; null: no cap). Pending requests
  hold nothing.

### Requests

- `GET …/requests`: `counts.total`. List items and the guest's view:
  `closed_reason` (null unless `closed`).
- `GET /events/registration/{event_id}/requests/export`: CSV of every
  request, newest first. Columns: `Status, Closed reason, Full name, Email,
  Phone, Ticket type, Discount code`, one per answer (the event's questions
  first, in order), then `Submitted at, Decided at, Decided by, Decline
  note, Amount, Payment, Pay by, Ticket ID, WhatsApp`. `Payment`: `free`,
  `paid`, `awaiting payment`, `not paid` (a hold that ran out),
  `not paid in time` (lapsed), or blank. A cell starting with `= + - @` is
  quoted with `'`. Same permission as the list (club roles may read it, door
  staff included; another club's event is 404).
- `closed_reason`: `registered_directly`, `event_ended`, `event_deleted`,
  `event_cancelled`. Requests closed before §7 were given theirs from their
  event by the migration.
- A lapsed hold's unpaid registration is retired (§3, "Paid tickets").

### Lists and pages

- The club's events list (`GET /clubs/events/list`) rows: `requires_approval`,
  `high_demand_label`, `requests_pending` (it was computed but dropped by the
  schema), `requests_total`; one grouped statement for the page
  (`approvals.totals`), never a subquery a row. Figures
  (`GET /clubs/events/figures`): `requires_approval` (any upcoming event),
  `requests_pending`, `requests_total` (upcoming events).
- Admin events list (`GET /events/admin/list`) rows: `requires_approval`,
  `high_demand_label`, `requests_pending`, `requests_total` (one grouped
  statement). The admin's event detail reads the summary: `requests`.
  `GET /admin/overview`: `requests: {received, waiting, avg_decision_hours}`
  (the average over the last 30 days; one statement, in the minute's cache).
- `GET /clubs/overview`: every event (`next_event`, `upcoming`,
  `needs_attention`, `reach`) has `requests_total` beside `registrations`.
  `GET /clubs/dashboard`: `live[]` has `requires_approval`, `requests_total`,
  `requests_pending`.
- Event analytics (`GET /events/registration/{id}/analytics`):
  `requests_total`, `requests_per_view` (requests ÷ page views, null without
  views), `requests_by_day` (`[{date, count}]`, IST days, by when they asked).
- Closeout figures and PDF: `requests` (the summary's block, dates as ISO
  strings; a "Registration requests" table in the PDF).
- Sales by ticket type (`GET /events/{id}/ticketing/sales`) and the types
  list (`GET /events/{id}/ticketing/types`): `held` per type and in the
  totals, places held for confirmed requests; `offered` is now the waiting
  list's offers only (it counted the holds too, which the club's waiting
  list never showed). Both hold their places.
- Finance (`GET /events/{id}/finance`, `GET /clubs/finance`): per event and
  in the figures, `expected_from_holds` and `expected_from_holds_count`:
  confirmed paid requests inside their hold and not claimed yet. A claimed
  hold has an unpaid registration, which is `outstanding` already; it is
  never in both.
- Event delete: allowed with requests (they are not registrations); see §3,
  Lifecycle. The delete confirmation reads `requests_pending` from the
  events list row (club) or the summary (admin).
- MCP `event_stats`: `requires_approval`, and `requests` (the counts by
  status and `total`; null when the event never had any).

### Public (website and app)

- Event fields: `high_demand_label` (form field on `POST /events/create` and
  `PUT /events/update/{id}`, omitted leaves it as it is; returned by both and
  by `GET /events/info/{id}`, and copied when an event is duplicated).
- `GET /events/info/{id}` and the app's lists (`EventListResponse`):
  `high_demand`. `GET /discover/events/{ref}` (`event`), discover cards
  (search, home, club pages, near you, `/search`): `requires_approval`,
  `high_demand`.
- Public `seats_left` and `status: "full"` (the event page and every card)
  come from the places calculation: holds count, and a paid event's cap
  counts paid tickets (an unpaid registration made it read "full" before).
  `registration_open` on the event page follows them. The cards' places are
  one statement for the whole page, and only when it has events not started
  yet (their types with their open waiting-list entries;
  `ticketing.places.places_many`).
- Ticketing: on a confirm-each event `waitlist.enabled: false`,
  `types[].waitlist: false`, and `POST /events/{id}/waitlist` is 409
  `WAITLIST_OFF_ON_APPROVAL_EVENTS`.

### Email

`request_unanswered` (club-branded, in the email library with the other
review emails, §9): "Your registration for {event}" — "{Club} wasn't able to
confirm your registration for {event}. Your registration is closed. Nothing
was charged."

### Contract changes

1. `request_unanswered` goes to requests that were pending when closed, not
   to confirmed paid holds closed with their event: those were answered
   (and "wasn't able to confirm your place" would not be true).
2. `offered` in the sales and ticket-types lists no longer includes the
   holds, now that `held` reports them (the contract's "separate from
   waiting-list offered").
3. Additions beyond the contract, all optional to read: `high_demand_label`
   on list rows and the event info; `requests_pending`/`requires_approval` on
   the events figures and the dashboard's `live[]`; `places` on the summary
   always (not only on confirm-each events); `expected_from_holds_count`;
   `closed_reason` on the guest's view.


---

## 8. The waiting list, once review goes on

Contract: `feature-register/review-wording-contract.md` §3.3 (28 Oct 2026).
An event that reviews registrations has no waiting list (§7): nobody in its
line is ever offered a place. So the people in line become registrations
under review, which the club can confirm (`approvals.service.from_waiting_list`).

- **When.** In the transaction that switches review on
  (`events.service.update_event`, when `requires_approval` goes from false to
  true). The event's row is written first; joining the line re-reads the
  setting under a share lock (`waitlist.join`). So a join racing the switch
  either lands before the line is read, and is converted, or is refused with
  `WAITLIST_OFF_ON_APPROVAL_EVENTS`.
- **Who.** Every entry `waiting` (not `via_request`), in line order, row-locked.
  Each becomes a `pending` request:
  - `submitted_at` is when they joined, so they come before anyone who
    registers later;
  - they keep their ticket type, name, phone (when it fits 15 characters),
    account and WhatsApp choice;
  - their entry becomes `removed`, so it no longer counts in the line or its
    figures.
- **Told once.** Each new request gets `request_received`, a job keyed on the
  request, run after the commit.
- **Nobody twice.**
  - Someone with an open request on the event already, or a live registration,
    is not made another one. Their entry just closes.
  - A concurrent request from the same account is caught by the unique
    indexes (a SAVEPOINT per row).
- **Left alone.**
  - An entry with no address to write to stays in line, untouched, counted as
    `kept_no_email`. That covers a phone-only entry, and an address longer
    than a registration takes (100).
  - A standing offer (`offered`) stays: it can be claimed until it runs out.
  - An event that can't confirm anyone any more (deleted, called off, over)
    is left as it is.
- **Events switched on before this.** The backfill
  `python -m scripts.waitlist_to_review` (dry run; `--apply` to do it) runs the
  same function for every event that reviews registrations and still has
  people waiting. It runs one transaction per event, with the event's row held
  first, and is idempotent. See docs/OPERATIONS.md §25.

Counts it returns (and the backfill prints): `moved`, `already_under_review`,
`already_registered`, `kept_no_email`. Clients need nothing new: the people
moved appear as `ticketing.viewer_request` and in
`/registration-requests/mine`, and their `viewer_waitlist` entry is gone.

---

## 9. The review emails are the club's to edit (migration `review_emails`)

`EmailStage` gains six values, one per library email: `REQUEST_RECEIVED`,
`REQUEST_APPROVED`, `REQUEST_APPROVED_PAY`, `REQUEST_DECLINED`,
`REQUEST_LAPSED` and `REQUEST_UNANSWERED`. The template key is the stage in
lower case (`request_received`, …), the keys this document always used.

- **Where they live.** In `email_templates`, as **library rows only**
  (`event_id` null), one per club per stage. `uq_email_templates_library_stage`
  enforces that.
  - `ensure_library` makes them from the starters (`app/core/mail/starters.py`)
    the first time the library is read. The library's GET routes commit, so
    the ids they answer with stay.
  - The send path reads the club's row, else the starter. It never writes.
- **No event copies.**
  - `POST /events/{id}/emails` with a request stage → 422 "These emails belong
    to the club's library: edit them in Emails."
  - Creating one in the library by hand, duplicating one, or deleting one is
    refused too (422).
  - An event's own Emails tab never lists them.
- **Editing** (the library's own permission: club auth, the user's club):
  `PUT /clubs/me/emails/{id}`. Words only:
  - `enabled` stays true, `trigger` stays `MANUAL`, `audience` stays ALL, and
    `channels` stay email;
  - attachments are the ticket for `REQUEST_APPROVED` and nothing for the
    others.
  - Validation is every email's: tokens, conditionals, own images.
  - The request tokens are held to their stages: `{{pay_url}}` in a reminder
    or in the "thanks" email → 422.
- **Reset.** **NEW** `POST /clubs/me/emails/{id}/reset` puts any non-custom
  library email back to MyOtherApp's default and bumps `version`. A custom
  email → 422 "A custom email has no default to go back to."
- **The editor.**
  - `GET /clubs/me/emails/context` lists the request tokens (group
    "Registration review"), each with `stages`, and the six stages' meta
    (`label`, `note`, `when`).
  - `POST /clubs/me/emails/preview` fills in their samples.
  - `GET /clubs/me/emails/overview` lists the six among `templates`, each with
    `key`, `group` and `when`. `automatic` is now empty.
- **Tokens of the review emails** (beside every usual one). `REQUEST_APPROVED`
  is a ticket email, and takes the ticket email's tokens.

| Token | Stages |
|---|---|
| `status_url` (their registration's page) | all but `REQUEST_APPROVED` |
| `cancel_url` (`status_url?withdraw=1`) | `REQUEST_RECEIVED`, `REQUEST_APPROVED_PAY` |
| `pay_url`, `pay_amount`, `pay_by` | `REQUEST_APPROVED_PAY` |
| `decline_note` | `REQUEST_DECLINED` |
| `event_url` | all but `REQUEST_APPROVED` |

- **Escaping.** Values are filled in and HTML-escaped, as every token's are.
  What people type (the club's decline note, the person's name) is a value,
  never template text: `{{event_name}}` typed in a note arrives as typed. A
  heading or paragraph that renders empty (an `{% if decline_note %}` with no
  note) is left out of the email, so it leaves no gap.
- **The confirmed ticket.** The deliveries outbox sends a ticket for a
  registration that came from a request (one probe of
  `ix_registration_requests_registration`) in the club's `request_approved`
  email. Every other ticket goes in the event's invite, as before.
- **Migration.** `review_emails` (after `add_event_partners`) adds the six enum
  values with `ADD VALUE IF NOT EXISTS` in autocommit, which touches only the
  catalogue. It writes no rows. Downgrade deletes the rows in those stages;
  the values stay in the type, unused.

---

## 10. The words (owner, 28 Oct 2026)

Contract: `feature-register/review-wording-contract.md`. People never read
"request a place", "your request", "waiting list" or "place" (a seat) on
these events.

| status | attendees read | the club reads |
|---|---|---|
| `pending` | Under review | Under review |
| `approved` (ticket) | Confirmed | Confirmed |
| `approved` (paid, unpaid) | Confirmed · pay by {time} to get your ticket | Confirmed |
| `declined` | Not confirmed | Declined |
| `withdrawn` | Cancelled ("Cancel my registration") | Cancelled |
| `lapsed` | Expired | Expired |
| `closed` | Closed | Closed |

The backend's own words:

- **API messages.** The codes are unchanged; only the `message` changed:
  - `APPROVAL_REQUIRED` "This event reviews every registration. Update the
    app, or register on myotherapp.in."
  - `ADD_ONS_NOT_ALLOWED` "Add-ons can't be picked when registrations are
    reviewed."
  - `REQUEST_DECLINED` "The organisers couldn't confirm your registration for
    this event, so you can't register again with this email."
  - `REQUEST_NOT_WITHDRAWABLE` "This registration can't be cancelled any
    more."
  - `WAITLIST_OFF_ON_APPROVAL_EVENTS` "This event has no waiting list: the
    organisers review every registration. Register instead, and they'll get
    back to you."
- **Pushes.**
  - "{Club} confirmed your registration" (`request_approved`,
    `request_approved_pay`).
  - "Your registration for {event}" / "{Club} couldn't confirm your
    registration this time." (`request_declined`).
- **Requests CSV.**
  - Status in the club's words.
  - "Registered at", "Reviewed at", "Reviewed by".
- **Closeout PDF.** The section is "REGISTRATION REVIEW", with rows "Received",
  "Confirmed", "Cancelled by them", "Expired (not paid in time)", "Closed …"
  and "Average time to review".
