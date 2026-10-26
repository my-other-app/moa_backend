# Registration approval ("Confirm each registration")

The contract as built: the backend (`backend/app/api/approvals/`), and what
the club dashboard, the website and the app read from it. Migration:
`registration_approval` (after `admin_ops`). All paths are under `/api/v1`.

Everything is additive. An event without `requires_approval` behaves exactly
as before, for every client.

---

## 1. What it does

A club turns on **Confirm each registration** for an event. Then:

1. Anyone who fills the registration form is **not registered yet**. They get
   a *request*, and an email: "Your registration for {event} was received, and
   you're on the waiting list. {Club} confirms each place and will email your
   ticket if they confirm yours. Nothing else to do: we'll let you know."
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
  waiting stay until the club decides.
- The event cancelled: pending requests, and confirmed paid ones not paid yet,
  close (`closed`), their places go back, and their people get the
  cancellation email. The event over: the same, without an email.
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

### Columns added

- `events.requires_approval boolean not null default false`.
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
  next (`waitlist.fill`).
- "No thanks" on the claim page, or withdrawing from the status link: the hold
  is given back, the request `withdrawn`.

Pending requests are never `WAITING` entries: they are a table of their own,
so the waiting list's automatic "offer the next person" never picks them.

### The minute sweep (`ticketing/sweep.py`)

After the waiting list's expiry and fills: `approvals.close_finished` finds
the events with requests still pending by a loose index scan of
`ix_registration_requests_pending` (one step per event, however many requests
each has), and closes those of events gone, cancelled, closed or over.

### Telling people (`approvals/notify.py`)

One job a message (`approvals.notify`, HIGH), written in the transaction that
decided: nothing goes for a decision that rolled back. Each run reads the
request again and sends only what is still true. Emails carry an idempotency
key, and count against the club's messages like a waiting-list offer
(`ticket` kind).

| kind | when | email | push (app accounts only) |
|---|---|---|---|
| `received` | submitted (and again, at most every 10 minutes, when the form is sent again by someone who can't show it is theirs) | "You're on the waiting list for {event}", status link, withdraw link | — |
| `approved` | confirmed, free | the event's own ticket email (deliveries outbox) | `request_approved` |
| `approved_pay` | confirmed, paid | "{Club} confirmed your place at {event}": pay ₹{amount} by {pay_by}, pay link | `request_approved_pay` |
| `declined` | declined with `notify` | "Sorry, {Club} couldn't offer you a place at {event} this time." + the note | `request_declined` (and an inbox item) |
| `lapsed` | a hold ran out | "Your place was released because payment didn't arrive in time." | — |
| `cancelled` | the event cancelled | "{Club} has cancelled {event}", the reason; nothing was charged | — |

The emails are the club's: its logo, colour and name as the sender, drawn by
the designed-email renderer (a plain email if that fails). They are listed in
the club's email library (`GET /clubs/me/emails/overview` → `automatic`) but
not edited there yet.

Push data (as `waitlist_offer`'s): `type`, `token`, `event_id`, `url` (the
status link); `request_approved` adds `ticket_id` (`""` when none);
`request_approved_pay` adds `claim_token` and `claim_url`. WhatsApp: later
(new Meta templates).

### Lifecycle

- Cancellation (`club_ops.cancel`): its follow-up closes the requests not yet
  settled and emails their people, before the registrants' email.
- The event over: the sweep above.
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
- Overviews:
  - `GET /events/{id}/summary` (the event's overview): `requests_pending`.
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
4. The four request emails are listed in the email library but not
   customisable yet: that needs new journey stages in the editor, the
   dashboard's too.

---

## 6. Running it

See docs/OPERATIONS.md §25.
