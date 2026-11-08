# Missing answers

A club edits the sign-up questions of an event people have already signed up
for. The platform keeps the answers people already gave, works out who is
missing what, lets the club ask exactly those people for exactly those
answers (app notification, email, the club's own WhatsApp) with a personal
link, and shows progress. People answer on the website or in the app; door
staff can fill a missing answer at check-in.

The cross-repo contract is `feature-register/missing-answers-contract.md`;
the question model (conditions, the wire shape) is
`feature-register/conditional-questions-contract.md`. Code:
`backend/app/api/missing_answers/`.

**Soft, always.** A missing answer never blocks a registration, a ticket or a
check-in. The door only sees it.

## Words

- **Person**: a live registration (not cancelled, ticket not revoked) or an
  open registration request (pending, or confirmed and not yet a live
  registration). A request that became a registration counts once, as the
  registration. Declined, withdrawn, lapsed and closed requests never count.
- **ref**: how a URL names a person: the registration's id, or `q` followed by
  the request's id. Clients pass it back; they never parse it.
- **State** of a current question for a person (from the current form and
  their stored answers, with the conditions' visibility):
  `hidden`, `answered`, `invalid` (answered, but no longer valid: a choice no
  longer offered, a value of the wrong type) or `blank`.
- **Missing**: a required (when shown) question that is blank or invalid.
- **Askable**: blank or invalid, required or not.

## Phase 1: keeping answers safe

- **Stable keys.** The dashboard never re-derives a key on rename. As a
  safety net, `update_event` keeps a stored key when an incoming question has
  no key match but the same normalised label (trimmed, lower case, single
  spaces) as exactly one stored question whose key is gone; a child's
  `show_if` follows. Stored answers are never rewritten by a form save.
- **Options.** A choice question (select, radio, checkbox) needs options on
  update as on create (400).
- **Form versions** (`event_form_revisions`). Written by a form change only:
  the first change writes version 1 (the form as it was, no summary) and
  version 2 (the new form, with the impact summary); later ones 3, 4, ...
  Dashboard payloads (`/events/info/{id}` for the host, a co-host or an admin;
  the create and update responses; `/clubs/events/list`) carry `form_version`
  and `form_changed_at`, null until the first change.
- **Impact preview** `POST /api/v1/events/{id}/form/impact`
  `{"additional_details": [...]}`: people counts, and the questions added
  (with how many could be asked), removed (how many answered), now required
  (how many miss it), options removed (how many chose it), relabelled and
  type changed (how many become invalid). Nothing is saved; a form breaking a
  rule is a 422 with the rule's words.
- **Summary** `GET /api/v1/events/{id}/answers/summary`: people, people
  missing anything, per question answered / blank / invalid / hidden /
  missing, stray keys (answers under keys the form no longer has,
  `organization` excepted) and open asks.
- **Lists.** Registration rows (`/events/registration/{id}/list`, and the
  attendee detail) and request rows (`/events/registration/{id}/requests`)
  carry `missing_answers` (keys, form order). Both lists take `missing=1`
  (anything missing) or `missing=<key>`.
- **Exports.** The registrations export and the requests CSV head answer
  columns with the question's label (a label used twice gets ` (key)`); a
  stray key keeps its key; the last column, **Missing answers**, lists the
  missing labels joined by `; `.
- **Moving stray answers** `POST /api/v1/events/{id}/answers/move`
  `{"from", "to"}` (owners and admins): for every person, copied when `to` is
  blank, then `from` removed; left alone when both are answered (`skipped`).
  `to` must be on the form, `from` must not. Recorded in the audit log.

## Phase 2: asking, answering, tracking

### Asks

Who: owners, admins, editors, and a co-host's team as for the host. Door
staff read only.

- `POST .../answers/asks/preview` `{"questions", "include_requests", "refs"}`
  → people, reach per channel, and whether WhatsApp is on offer (with
  `NO_FEATURE`, `NOT_CONNECTED` or `TEMPLATE_NOT_APPROVED` when not).
- `POST .../answers/asks` → 201, the ask. `questions` 1–20 current keys;
  `channels` from push, email, whatsapp; `message` 1–300 characters;
  `deadline` in the future and not after the event ends; `remind_after_hours`
  12–168 or null; `refs` (at most 500) for "Ask again"; `client_ref` makes a
  double click one ask. 409 `NOTHING_TO_ASK`, 409 `EVENT_ENDED`; 422
  `WHATSAPP_UNAVAILABLE` when WhatsApp is chosen and the club can't send it.
- `GET .../answers/asks` → newest first, each with its counts: people, sent,
  failed, opened, answered, still askable.
- `POST .../answers/asks/{id}/remind`: to those still askable. At most two
  reminders (the automatic one counts), each at least 12 hours after the round
  before: 409 `REMIND_LIMIT`, 409 `REMIND_TOO_SOON` with `retry_after`
  seconds; 409 `CLOSED` for a closed ask.
- `POST .../answers/asks/{id}/close`.

An ask closes by itself at its deadline, when nobody is left to answer, or
when the event ends (`closed_reason`: `deadline`, `done`, `event_ended`;
`closed_by_club` when closed by hand). This is worked out on every read, so
nothing waits on a sweep.

### Sending

One job per person per round (`missing_answers.send`, deduped by ask, person
and round). Each message is a row of `answer_ask_sends` (queued, then sent,
failed or skipped with the reason), claimed before anything goes, so a job run
again sends nobody twice.

- **Push**: app accounts with a device; type `answers_needed`, data
  `{"event_id", "url"}`; title "<Event> needs <n> more details", body the
  club's message.
- **Email**: anyone with an address (guests too) that has not bounced and is
  not suppressed; the club's message, the questions, the deadline and an
  **Add my details** button.
- **WhatsApp**: the club's own number only, with its approved
  `details_needed` template, to people who ticked WhatsApp, hold a live ticket
  or an open request, and have not told the club to stop.

The first send of an ask is free of the club's message quota. Reminders count
like any message (an email reminder is a `reminder`). The club's own WhatsApp
is never counted (the club pays Meta); a push costs nothing. The automatic
reminder is a job of its own (`missing_answers.remind`), run at
`created_at + remind_after_hours`.

### The personal link

`https://myotherapp.in/answers/{event_id}/{ref}?k={sig}`, where `sig` signs
`answers:{event_id}:{ref}` like every emailed link (`app.core.mail.links`).
It works without signing in; the person's own sign-in (an app account or the
guest session) works without `k`.

- `GET /api/v1/events/{event_id}/answers/{ref}?k=` → the status (`open`,
  `done`, `closed`, `nothing`), the event, their first name, the club's
  message and deadline, the full current form (wire shape), `asked` (their
  open asks' still-askable keys plus anything required they are missing),
  their current answers, `ticket_url` and `request_url`. Opening it marks
  their open asks opened. 404 for a bad ref or `k`.
- `POST` the same URL `{"answers": {...}}`: only the asked keys and the
  questions they reveal are taken; checked with the registration form's rules
  (only those required); merged; answers a new answer hides are dropped.
  422 `INVALID_ANSWERS` with `errors: {key: message}`; 409 `CLOSED` after the
  deadline or once the event is over. 20 a minute per link.

### Flags for the person

`answers_needed: {"count", "url", "deadline"}` (or null) on
`GET /events/tickets/{ticket_id}` (its holder), `/user/my-events`,
`/events/list` rows the caller is registered for, the request view
`GET /registration-requests/{token}` and each row of
`GET /registration-requests/mine`. Set only while the person has an open ask
with something left to answer: a missing answer the club never asked for is
not flagged.

## Phase 3: the door

Gate attendee payloads (scan, search, the answers below) carry
`missing_answers: [{"key", "label"}]`.
`POST /api/v1/events/{id}/gate/answers/{registration_id}` (check-in
permission; a door link works) `{"answers": {...}}`: any of the form's
questions; types checked, nothing required, blanks leave an answer as it was;
merged and pruned as above; counts as answered for any open ask. Answers:
`{"missing_answers": [...]}`.

A club correcting a registration's answers from the attendee page counts as
answered too.

## The WhatsApp template

`details_needed`, UTILITY, `en`, submitted by the club from its WhatsApp
templates page beside `ticket_confirmed`, `event_reminder` and
`certificate_ready`:

> Hi {{1}}, {{2}} needs {{3}} more details for your registration. Please add
> them by {{4}}.

({{1}} first name, {{2}} event, {{3}} how many, {{4}} the deadline or "the
event"), with one URL button **Add my details** to
`https://myotherapp.in/answers/{{1}}`, whose suffix is
`{event_id}/{ref}?k={sig}`.

## Tables

`event_form_revisions`, `answer_asks`, `answer_ask_people` (who each ask is
to, `opened_at`, `answered_at`) and `answer_ask_sends`, in the migration
`missing_answers`. Dropping them (downgrade) loses form versions and asks
only; the answers live on the registrations and requests.
