# Ticketing: ticket types, discount codes and waiting lists

The contract between the backend and its clients (the public website, the
app, the club dashboard). Code: `backend/app/api/ticketing/`. Migration:
`ticketing`.

Everything here is **additive and backward compatible**. A client that
knows nothing about it keeps working on every event that sells one kind of
ticket, which is every event until its club adds a second type.

All paths are under `/api/v1`. Money is in rupees, as JSON numbers with at
most two decimals, and is always worked out by the server. A client never
sends a price or an amount. If it does, the server ignores it.

---

## 1. The model in one page

- **Ticket type.** Every event has at least one, for example "General",
  "Student" or "VIP". A type has:
  - a name and a description;
  - a price (`0` = free) and a capacity (`null` = no cap of its own);
  - a sales window (`sales_start`, `sales_end`; `null` = with the event's
    registration);
  - hidden or not;
  - a per-person limit;
  - a sort order.
- **Migrated events.** The migration gave every existing event one *default*
  type, "General admission", at its current price and capacity.
- **One type follows the event.** While an event has only its default type,
  that type *follows the event*: its price is the event's `reg_fee` (when
  `has_fee`) and its capacity is the event's `max_participants`. Old clients
  and the old event form keep working unchanged.
- **Several types.** With more than one type, the types decide:
  - the event's `has_fee` and `reg_fee` become "any type is paid" and "the
    lowest price", so lists and cards still show something sensible;
  - the event's `max_participants` becomes an optional **total across all
    types**.
- **Holding a place.** A registration holds a place on its type while it is
  live and either paid or owes nothing.
  - Free tickets, and tickets a code made free, hold their place at once.
  - A paid ticket takes its place when the payment lands. If the type or
    the event filled while it was being paid, the payment is refunded in
    full automatically, as with a full event today.
- **One-per-account is unchanged.** One person still holds at most one live
  registration per event, so a per-person limit of 1 matters across email
  addresses and phone numbers.
- **Discount code.**
  - Scope: one event, or every event of the club (`event_id: null`).
  - Value: a percentage or a fixed amount off, never below zero. A code that
    covers the whole price makes the ticket free, and a free ticket needs no
    payment.
  - Optional limits: some of the event's types only, a validity window, a
    maximum number of uses, uses per person, and active/inactive.
  - Counting: a use counts on a registration that is paid or confirmed
    (owes nothing), and stops counting when that registration is refunded or
    cancelled. While a registration with a code is waiting to be paid, it
    reserves its use for 30 minutes, so two people at checkout can't both
    take the last use.
- **Waiting list.**
  - Joining: a club turns it on per event. When a type, or the event's total,
    is sold out, people can join the list for a type.
  - Offers: when a place frees up (a cancellation, a refund, a raised cap),
    the next person in line is offered it. They get a claim link by email,
    by push when they joined signed in, and by WhatsApp when they ticked it.
    The link holds the place for the event's offer hours (24 by default; the
    club sets 1 to 168).
  - Priority: while an offer stands, or people are waiting for a type, nobody
    new can take that type's places.
  - Expiry: an offer that runs out passes to the next person within a
    minute.
  - Turning the list off stops new joins, but people already in line keep
    their places (the club removes them to free those places).
  - Once the event is over, its open entries are closed (`expired`).

---

## 2. The event page: types on sale and availability

### `GET /events/info/{event_id_or_slug}`

The existing event detail gains one key, `ticketing`. It is `null` only from
a server older than this feature.

### `GET /events/{event_id_or_slug}/ticket-types`

Returns the same object on its own. It takes an optional session; with one,
it includes `viewer_waitlist`.

Query: `ticket_type={id}` also lists a **hidden** type that a link names.
Hidden types are otherwise left out.

```json
{
  "types": [
    {
      "id": 12,
      "name": "Early bird",
      "description": "Until Friday",
      "price": 199.0,
      "currency": "INR",
      "is_free": false,
      "capacity": 100,
      "available": 23,
      "status": "on_sale",
      "sales_start": null,
      "sales_end": "2026-11-01T23:59:00+05:30",
      "per_person_limit": 1,
      "is_hidden": false,
      "waitlist": false
    }
  ],
  "requires_choice": true,
  "available": 40,
  "sold_out": false,
  "waitlist": { "enabled": true, "offer_hours": 24 },
  "viewer_waitlist": []
}
```

| Field | Meaning |
|---|---|
| `types` | The visible types, in the club's order. |
| `types[].status` | `on_sale`, `sold_out`, `not_started` (its own window or the event's registration has not opened), `ended` (its window closed), or `closed` (the event's registration is closed or the event is over). |
| `types[].available` | Places someone new can take now: the lower of the type's and the event's, with the waiting list counted. `null` = no limit. |
| `types[].capacity` | The type's own cap. A type that follows the event shows the event's cap. |
| `types[].waitlist` | `true` when it is sold out and its waiting list can be joined. |
| `requires_choice` | `true` when a registration **must** send `ticket_type_id`, because there is more than one visible type. |
| `available` | The event's places left across all types. `null` = no total. |
| `sold_out` | Every listed type is sold out. |
| `viewer_waitlist` | The signed-in viewer's own places in line (see §5). |

---

## 3. Registering

### `POST /events/registration/{event_id_or_slug}/register`

The request takes two new optional fields:

```json
{ "...": "as before", "ticket_type_id": 12, "discount_code": "EARLY20" }
```

| Field | Rule |
|---|---|
| `ticket_type_id` | Optional when the event has one type, or exactly one visible type, which is then used. Otherwise it is required (422 `TICKET_TYPE_REQUIRED`). A hidden type is sold to a request that names it. |
| `discount_code` | Optional. Case and spaces are ignored. Checked and applied by the server, and refused with a reason (§6). |

The response keeps every existing field and gains these:

```json
{
  "pay_amount": 159.2,
  "ticket_type": { "id": 12, "name": "Early bird" },
  "price": 199.0,
  "discount": { "code": "EARLY20", "amount": 39.8 },
  "is_paid": false
}
```

- `pay_amount` is still what is left to pay: the type's price, less the
  code, plus any store picks. The payment step is unchanged: `POST
  /payments/orders` with `{"source": "event_registration", "payload":
  {"event_id", "event_registration_id"}}`. The order's amount is the server's
  own figure.
- `pay_amount == 0` means **no payment step**. On a paid event,
  `is_paid: true` means the ticket was confirmed at nothing (a free type, or
  a code that covered it) and the ticket email is on its way.
  `POST /payments/orders` on it answers 409 `ALREADY_PAID`.
- **Back to pay.** A signed-in person coming back to the same unpaid
  registration can change its type or add a code: send `ticket_type_id` or
  `discount_code`, and it is priced again. Sending neither leaves it as it
  was, which is what the app does today.
- `EVENT_FULL` (400) now also carries `errors.waitlist: true|false`, which
  says whether the waiting list can be joined instead.

### Check a code: `POST /events/{event_id_or_slug}/discount-codes/check`

Request:

```json
{ "code": "early20", "ticket_type_id": 12, "email": "optional", "phone": "optional" }
```

Response:

```json
{ "code": "EARLY20", "ticket_type_id": 12, "price": 199.0, "discount": 39.8, "amount": 159.2, "free": false }
```

- It returns only the price effect: never a code's limits, its uses, or the
  other events it works on. Every unknown, inactive or other-club code is
  the same 422 `DISCOUNT_CODE_INVALID`.
- `ticket_type_id` follows the same rule as registering: required when the
  event needs a choice.
- `email` and `phone` are only for a code limited per person.
- It is limited to **10 requests a minute per address**. Past that it
  answers 429.

### Where the type shows

The type's name is `ticket_label` on the registration. It is set only when
the event sells more than one type, so single-type tickets look exactly as
before. It shows in these places:

| Place | What shows |
|---|---|
| Designed tickets, certificates and ticket emails | The `{{ticket_type}}` token (a speaker's or staff member's role still wins over it). |
| Apple Wallet pass | A "TICKET" field. |
| Gate scan and search | `attendee.ticket_type`. |
| Attendee list | `ticket_type_id`, `ticket_type_name`. |
| Attendee detail | `ticket_type_id`, `ticket_label`, `discount_amount`. |
| The guest's ticket (`GET /events/tickets/{ticket_id}`) | `ticket_type_id`, `ticket_label`; its `event` also carries `status` and, when cancelled, `cancellation`. |
| My events (`GET /events/list`) | `ticket_label` on each event the caller is registered for (null on the rest). |
| Registrations export | Three new last columns: "Ticket Type", "Discount Code" and "Discount". |

---

## 4. The claim link

When a place frees up, the next person gets an offer with a link. The link
is the public site's page:

```
{PUBLIC_SITE_URL}/claim/{token}        e.g. https://myotherapp.in/claim/Xy3...
```

The token is the only credential: about 32 URL-safe characters, new for
every offer.

- **Push.** The offer goes to the app as a push with `data = {"type":
  "waitlist_offer", "event_id": "<id>", "url": "<the link>", "token":
  "<token>"}`.
- **WhatsApp.** The WhatsApp message uses the template `waitlist_offer`, whose
  URL button is `https://myotherapp.in/claim/{{1}}`. The template must be
  approved at Meta before it sends; until then the WhatsApp copy is skipped
  and email and push still go.

- **What it costs the club.** The email and the WhatsApp message are counted
  as the club's messages, as a ticket's are: the plan's monthly allowance
  first, then its prepaid credits. Like a ticket, an offer is never held back
  when both are used up.

### `GET /waitlist/offers/{token}`

What the claim page shows:

```json
{
  "id": "uuid",
  "status": "offered",
  "event": { "id": 7, "slug": "...", "name": "...", "event_datetime": "...", "location_name": "...", "poster": {} },
  "ticket_type": { "id": 12, "name": "Early bird", "price": 199.0, "currency": "INR" },
  "full_name": "Asha K",
  "email": "as•••@gmail.com",
  "phone": "•••••3210",
  "expires_at": "2026-11-02T18:00:00+05:30",
  "needs_email": false,
  "needs_payment": true,
  "registration_id": null
}
```

- `status` is one of `offered`, `expired` (it ran out; the place went on),
  `claimed`, `left` or `removed`. 404 `OFFER_NOT_FOUND` means an unknown
  token.
- `needs_email`: the person joined with a phone number only, so the claim
  must send `email`.
- `registration_id`: a claim was started and is waiting for its payment.
- `reason`: `"waitlist"`, or `"approved"` for a club's confirmation of a
  registration request (docs/APPROVALS.md), which also carries `amount` (the
  confirmed price). `event.club` is `{name, slug}` on every offer.

### `POST /waitlist/offers/{token}/claim`

It takes an optional session. Request (every field optional):

```json
{
  "full_name": "...",
  "email": "...",
  "phone": "...",
  "additional_details": {},
  "organization": "...",
  "discount_code": "...",
  "whatsapp_opt_in": false
}
```

- It registers the person the place was offered to, on the offer's type, at
  the offer's email address. `email` is used only when the offer has none.
- The response is exactly the registration response of §3. For a guest it
  includes `auth_token`, a session for the guest the claim made, to pay with.
- A free ticket, or one a code made free, is claimed at once.
- A paid ticket continues with `POST /payments/orders` using
  `event_registration_id`. The place stays theirs until the offer's
  `expires_at`; the claim is complete when the payment lands.
- Claiming the same offer again while its payment is pending returns the
  same registration, to pay for.
- A payment that lands after the offer ran out still gets the place if one
  is free. If none is, it is refunded in full automatically.
- It is limited to 10 requests a minute per address.

### `POST /waitlist/offers/{token}/decline`

Gives the place to the next person in line. It is no longer the person's.

---

## 5. Joining the waiting list

### `POST /events/{event_id_or_slug}/waitlist`

It takes an optional session. Request:

```json
{ "ticket_type_id": 12, "full_name": "Asha K", "email": "asha@example.com", "phone": "9876543210", "whatsapp_opt_in": false }
```

- The person needs `email` or `phone`. A signed-in person's own email is used
  when they send none.
- `ticket_type_id` follows the same rule as registering.
- `whatsapp_opt_in` needs a number WhatsApp can reach; otherwise it answers
  422 `WHATSAPP_NUMBER`.

Response:

```json
{
  "id": "uuid",
  "ticket_type_id": 12,
  "ticket_type_name": "Early bird",
  "status": "waiting",
  "position": 3,
  "offer_expires_at": null,
  "joined_at": "...",
  "created": true
}
```

- Joining again with the same email, phone or account returns the same
  place, with `created: false`.
- `position` is 1 for the next person in line.
- It is limited to 10 requests a minute per address.

### `GET /events/{event_id_or_slug}/waitlist/me` and `DELETE …/waitlist/me`

For a signed-in person: their places in line, and leaving them. `DELETE`
returns `{"left": n}`.

---

## 6. Error codes

All errors keep the API's usual shape: `{message, error_code, errors}`.
Fields a form shows are keyed in `errors` by field name: `ticket_type_id`,
`discount_code`, `email`, `phone`.

| Status | `error_code` | When |
|---|---|---|
| 422 | `TICKET_TYPE_REQUIRED` | Several types and none named. `errors.ticket_types` lists the visible ids. |
| 422 | `TICKET_TYPE_NOT_FOUND` | Not a type of this event, or one since removed. |
| 409 | `TICKET_TYPE_NOT_ON_SALE` | Outside its sales window. `errors.closed_reason` is `not_started` (with `opens_at`) or `ended` (with `closed_at`). |
| 409 | `TICKET_TYPE_SOLD_OUT` | Nothing left of this type. `errors: {ticket_type_id, waitlist}`. |
| 409 | `TICKET_TYPE_LIMIT` | This person (account, email or phone) already holds the type's per-person limit. |
| 400 | `EVENT_FULL` | As before; `errors.waitlist` is new. |
| 422 | `DISCOUNT_CODE_INVALID` | Unknown, inactive or deleted code; another club's; or the club's plan no longer includes codes. |
| 409 | `DISCOUNT_CODE_NOT_STARTED` | Before the code's start. |
| 409 | `DISCOUNT_CODE_EXPIRED` | After the code's end. |
| 409 | `DISCOUNT_CODE_NOT_FOR_TYPE` | The code is held to other ticket types. |
| 409 | `DISCOUNT_CODE_USED_UP` | No uses left. |
| 409 | `DISCOUNT_CODE_LIMIT` | This person has used it as often as it allows. |
| 409 | `WAITLIST_CLOSED` | The event has no waiting list: off, its plan lacks it, or registration has closed. |
| 409 | `WAITLIST_NOT_NEEDED` | Tickets are still available. `errors.available`. |
| 422 | `WAITLIST_CONTACT_REQUIRED` | Neither email nor phone was sent. |
| 409 | `ALREADY_REGISTERED` | Joining or claiming while already registered for the event. |
| 404 | `OFFER_NOT_FOUND` | Unknown claim token. |
| 410 | `OFFER_EXPIRED` | The offer ran out; the place went to the next person. |
| 409 | `OFFER_CLAIMED` | The offer was claimed already. |
| 422 | `EMAIL_REQUIRED` | Claiming a phone-only offer without `email`. |
| 429 | (rate limit) | Too many code checks, joins or claims from one address. |

---

## 7. What the website and the app must change

### Event page

- Read `ticketing` from the event detail, or from `GET
  /events/{slug}/ticket-types`.
- When `requires_choice` is true, show the types with their price, status
  and `available`, and send the chosen `ticket_type_id`.
- A type with `status: sold_out` and `waitlist: true` gets a "Join the
  waiting list" form (`POST /events/{id}/waitlist`).
- `not_started` shows `sales_start`.
- A link with `?ticket_type={id}` shows that hidden type.

### Registration form

- Add a "Discount code" field. Check it live with `POST
  …/discount-codes/check` for the price preview, then send it as
  `discount_code`. The registration answer is the final word.
- Show the field errors keyed by `ticket_type_id` and `discount_code`.

### After registering

- When `pay_amount == 0`, skip checkout and show the ticket, even on a paid
  event.
- Otherwise checkout is unchanged.

### Errors

- Handle the new codes in §6. `EVENT_FULL` and `TICKET_TYPE_SOLD_OUT` with
  `errors.waitlist: true` offer the waiting list.

### Claim page and deep links

- Add `/claim/:token` on the site: `GET /waitlist/offers/{token}`, then
  `POST …/claim`. Store `auth_token` as the public form does, and go to
  checkout when `pay_amount > 0`. Also offer "No thanks" (decline).
- The app should open `https://myotherapp.in/claim/*` links (AASA and
  assetlinks), and the `waitlist_offer` push (`data.url`).

### App: my tickets and the event screen

- Show `ticket_type.name` on the ticket when present.
- Show `viewer_waitlist` (position, or "your place is held until …").

### Nothing else breaks

- An app that sends neither `ticket_type_id` nor `discount_code` keeps
  working on every event with one visible type.
- On events with several, it gets 422 `TICKET_TYPE_REQUIRED` and should send
  people to the website until it is updated.

---

## 8. The club dashboard's API

Every route needs the club's session. Team members follow the club access
rules: editors and admins change things, door staff read.

### Ticket types

| Method | Path | What |
|---|---|---|
| GET | `/events/{id}/ticketing/types` | The editor's list: every type, hidden ones too, with `sold`, `available`, `waiting`, `offered` and `has_registrations`. It also returns `follows_event`, `event_capacity`, `event_available` and `max_types` (the plan's cap; `null` = none). |
| PUT | `/events/{id}/ticketing/types` | Saves the editor's whole list: `{"types": [{id?, name, description, price, capacity, sales_start, sales_end, is_hidden, per_person_limit}], "event_capacity"?: int\|null}`. A type with `id` is changed, one without is made, and one left out is removed. A type people hold is refused (409 `TICKET_TYPE_HAS_REGISTRATIONS`; hide it instead). The list order is the sort order. `event_capacity` is the total across types, left unchanged when not sent. |
| POST | `/events/{id}/ticketing/types` | Adds one type. |
| PATCH | `/events/{id}/ticketing/types/{type_id}` | Changes one type. |
| DELETE | `/events/{id}/ticketing/types/{type_id}` | Removes one type (409 `TICKET_TYPE_HAS_REGISTRATIONS`, 409 `LAST_TICKET_TYPE`). |
| PUT | `/events/{id}/ticketing/types/order` | `{"ids": [...]}` |
| GET | `/events/{id}/ticketing/sales` | Per type: `sold`, `paid`, `revenue`, `pending` (registered, not yet paid), `discount_total`, `available`, `waiting` and `offered`. Also `totals`, and the codes used at the event with their uses. |

### Discount codes

| Method | Path | What |
|---|---|---|
| GET | `/clubs/discount-codes` | Every code (`?event_id=` for the codes an event takes, `?scope=club` for club-wide only), each with `uses`, `discount_total` and `status` (`active`, `scheduled`, `expired`, `used_up` or `inactive`). |
| GET | `/events/{id}/ticketing/codes` | The codes this event takes. |
| POST | `/clubs/discount-codes` | `{code, kind: "percent"\|"amount", value, event_id?, ticket_type_ids?, starts_at?, ends_at?, max_uses?, per_person_limit?, is_active?}`. A code's text is unique per club (409 `DISCOUNT_CODE_TAKEN`) and never changes. |
| PATCH | `/clubs/discount-codes/{id}` | Changes anything but the text. |
| DELETE | `/clubs/discount-codes/{id}` | Deletes the code. |

### Waiting list

| Method | Path | What |
|---|---|---|
| GET | `/events/{id}/ticketing/waitlist` | `{settings: {enabled, offer_hours, allowed}, counts, entries, total}`. `?status=` filters. Each entry has its `position`, `offer_url` while an offer stands, contact details and timestamps. |
| PUT | `/events/{id}/ticketing/waitlist/settings` | `{enabled?, offer_hours?}` |
| POST | `/events/{id}/ticketing/waitlist/{entry_id}/offer` | Offers this person a place now, ahead of the line or again after an offer ran out. Only a free place: 409 `WAITLIST_NO_ROOM` otherwise. Raise a cap first. |
| DELETE | `/events/{id}/ticketing/waitlist/{entry_id}` | Takes the person off the list. |
| GET | `/events/{id}/ticketing/waitlist/export` | The list as CSV. |

### Plan gates

The usual 403 `PLAN_FEATURE_LOCKED` and `PLAN_LIMIT_REACHED`, with
`errors.feature` or `errors.limit`:

- `ticket_types_per_event`: adding a type past the cap.
- `tickets`: a paid type on a free event.
- `coupons`: making or switching on a code.
- `coupons_per_event`: codes usable at one event, its own plus the club-wide
  ones.
- `waitlist`: turning the list on.

The migration switches `coupons` and `waitlist` on for Starter, Growth,
Pro, Enterprise and (legacy) Campus. Free has neither. They can be changed
in Admin → Plans.
