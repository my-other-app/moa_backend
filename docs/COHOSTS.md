# Co-hosted events

A club (the **host**) invites up to five other clubs to **co-host** one of its
events, like a collab post. A club that accepts manages the event with the
host and sees everything about it; the event shows on both clubs' pages,
"Hosted by A and B". The host stays the event's club (`events.club_id`)
everywhere, and paid tickets go through the host's payment setup exactly as
before: there are no split payments; the clubs settle any share between
themselves.

Code: `backend/app/api/cohosts/`. Migration: `add_event_cohosts` (after
`review_emails`). The full API contract, with JSON and the dashboard
and app behaviour, is `feature-register/cohosts-contract.md`; this page is
how the server does it.

## 1. The row

`event_cohosts`: one row per club invited to an event.

| Column | |
|---|---|
| `event_id`, `club_id` | The event, and the invited club. |
| `status` | `invited` → `accepted` (a co-host) or `declined`; a co-host may become `left` or `removed`; a pending invite may become `cancelled` (the host withdrew it) or `expired`. A check constraint holds it to these seven. |
| `note` | What the host wrote with it, at most 300 characters. |
| `invited_by_user_id`, `invited_by_member_id` | Who sent it: the host's account, and the team member when one did. |
| `invited_at`, `expires_at` | Sent, and when it lapses: 14 days later or when the event ends, whichever is first. |
| `responded_at`, `ended_at` | Accepted or declined; left, removed, cancelled or expired. |
| `followers_told_at` | When the co-host's followers were told about the event (once). |

`uq_event_cohosts_live` keeps at most one **live** row (`invited` or
`accepted`) per event and club, whatever the races; inviting also locks the
event's row, so two invites sent at once count each other against the limit
of five live rows. A club that declined, left, was removed, or whose invite
was cancelled or lapsed may be invited again: a new row.

Expiry is lazy: any read or write that meets a pending invite past
`expires_at` marks it `expired` (`service.expire_stale`). Nothing sweeps.

## 2. Access: the gate

Every club-side event route used to answer one question, "is the event's
club the caller's club?", in `events.access.ensure_event_access` and a few
module loaders (`deliveries.event_for_club` calls it; nearly every event
route calls that). A co-host must pass all of them, and a route written
later must not be able to forget.

So the decision is made once, in the auth dependency
(`app.core.auth.dependencies.get_current_user` → `cohosts.access.gate`), for
every signed-in request whose path names an event (`{event_id}`):

1. The member's role is checked first, exactly as for any request of theirs
   (`club_access.policy`): an editor at the co-host is an editor on the
   event, door staff get the door only.
2. If the caller is a club, the path names an event, the event is another
   club's and the caller's club co-hosts it (an `accepted` row):
   * a **host-only** request (`cohosts.policy.host_only`) is refused,
     `403 COHOST_HOST_ONLY`, "Only {host}, the host, can do this.";
   * anything else is let through: the grant is noted for this request (a
     context variable, reset at the start of every request), and
     `ensure_event_access`, the gate's door (`checkins.gate_auth`) and the
     notifications routes read it (`access.granted`).
3. A caller that is neither host nor co-host is left to the route's own
   check, as before.

Routes that find their event without `{event_id}` ask on their own:
`GET /events/tickets/{ticket_id}` (and its image, PDF and wallet pass), the
volunteer routes that take the event in a body, and the club-level expense
and discount-code routes (which refuse a co-hosted event with
`403 COHOST_HOST_ONLY` rather than "not found": the money and the codes are
the host's).

Access ends at the next request when the row stops being `accepted`.

### Host-only

| What | Routes |
|---|---|
| Refunds and retrying them | `POST /events/{id}/payments/{pid}/refund`, `POST /events/{id}/refunds/...` |
| Cancelling the event (it refunds everyone) and its retry | `POST /events/{id}/cancel`, `POST /events/{id}/cancel/retry` |
| Deleting it | `DELETE /events/delete/{id}` |
| Duplicating it (the copy is a new event of the host club) | `POST /events/{id}/duplicate` |
| Running retention now (irreversibly scrubs attendees' data) | `POST /events/{id}/closeout/retention/run` |
| Inviting, cancelling and removing co-hosts, and looking for clubs | `POST /events/{id}/cohosts`, `DELETE /events/{id}/cohosts/{invite_id}`, `GET /events/{id}/cohosts/search` |

Payout and gateway settings, the club's finance page, its contacts, its
discount codes, store catalogue and email library are club-level: a co-host
only ever reaches its own. The event's money is read-only to a co-host (its
owner and admins, as at the host: `GET /events/{id}/finance`, the
statement, `GET /events/{id}/payments`, `GET /events/{id}/cancel`).

### Roles for co-hosting itself (`club_access.policy`)

Every role reads `GET /events/{id}/cohosts` and its club's invitations.
Owners and admins (at the host) invite, cancel, remove and search; (at the
co-host) accept, decline and leave. `can()` gains `manage_cohosts`.

### What belongs to whom

A route on the event creates the event's things, which are the host's:
email templates, tracking links, store items offered on it, designs,
doors, kiosks. Two exceptions take the co-host's own club, because they are
about the club's own collection: **adding sponsors from the club's
partners** (`POST /events/{id}/partners/from-club`) takes the co-host's
partners, and **saving an event email to the library** puts it in the
co-host's own library. Messaging attendees counts against the host's
message quota and plan (the event's club), as the host's own messages do.
Event emails, tickets and certificates keep the host's templates and
branding.

## 3. The route table

`tests/test_cohost_routes.py` lists every route a club account may call
whose path names an event (244 of them when written), each classified
ALLOW, HOST_ONLY, COHOST_ONLY or PUBLIC. It:

* fails when a route is added (or removed) without being classified;
* checks the table against `cohosts.policy.host_only`;
* calls every route three times -- a stranger club on one event, the
  co-host on that event, the host on its twin -- with the path, query and
  JSON body made from the OpenAPI schema, and asserts the co-host's answer
  is the host's (status, error code, message), the stranger's a refusal,
  and a host-only route's a `403 COHOST_HOST_ONLY` for the co-host.

A new event route is therefore covered by the gate the day it is written,
and the table makes its author decide whether a co-host may use it.

## 4. Where co-hosts show

* `cohosts: [{club_id, name, slug, logo}]` -- the accepted ones, in the
  order they accepted -- on `GET /events/info/{id}`, the app's event lists
  (`/events/list`, `/events/for-you`, `/home/feed/events`,
  `/clubs/{club_id}/events`), the dashboard's `/clubs/events/list`, the
  admin's `/events/admin/list`, and every `/discover` event card and the
  event page (`logo` there is one URL, as the club's beside it).
* `GET /events/info/{id}` gains `viewer_role`: "host", "cohost" or null.
  The dashboard's list rows gain `viewer_role`, the host as `club`, and the
  query `role=all|hosted|cohosted`.
* A club's events -- its dashboard list and figures, its public pages
  (`/discover/clubs/{slug}`, `/discover/clubs/{slug}/events`,
  `/clubs/{club_id}/events`), its website, its card's upcoming count, and
  the followers' feed (`/home/feed/events?is_following=true`) -- include
  the events it co-hosts, each once. Discover and search list each event
  once (they read `events`; the co-hosts are a filter, never a join).
* `GET /clubs/events/figures`: the counts include co-hosted events, with
  `cohosted_upcoming`; `collected` and `outstanding` are the club's own
  events only (a co-hosted event's money went to its host). Platform and
  admin totals count each event once.
* Documents: the `organiser` token (aliases `organizer`, `host`,
  `hosted_by`) is the "hosted by" line: "Design Society with Robotics Club"
  (`events.documents.hosted_by`). `org_name` stays the host's name (the
  branding: the sender's name, the logo's), and the certificate starters'
  "Organiser" line now prints `{{organiser}}`. The loaders that render
  tickets, certificates, badges and event emails read the co-hosts
  (`selectinload(Events.cohost_clubs)`); a loader that does not simply
  prints the host.
* The public site is told to render the event's page and the co-host's
  page again when an invite is accepted, left or ended
  (`core.site_revalidate`), and both clubs' websites are made again.

## 5. Telling people

* **The invited club**: a bell notice (`cohost_invite`, link `/invitations`)
  and an email to the club's own address and every active admin, once the
  invite is committed.
* **The host**: a bell notice when an invite is accepted or declined, or a
  co-host leaves (`cohost_update`, link `/events/{id}/cohosts`).
* **A removed co-host**: a bell notice (`cohost_update`).
* The bell shows `cohost_invite` and `cohost_update` to owners and admins
  only.
* **Followers**: when a co-host accepts an event that is published (or
  live) and still to come, or a draft with co-hosts is published, each
  co-host's followers get the same `new_event` line and push the host's
  followers got (job `notifications.cohost_new_event`, then
  `notifications.cohost_push` to exactly the people that line reached).
  Nobody twice: anyone following the host (unmuted) is left out, and so is
  anyone already holding a `new_event` line about the event (told by
  another co-host); `followers_told_at` makes it once per co-host.
* **Activity**: `cohost.invite`, `cohost.cancel`, `cohost.remove`,
  `cohost.accept`, `cohost.decline`, `cohost.leave` in the audit log,
  entity `event`, with both clubs, the row and the team member who acted.

## 6. Not covered

* Clubs' connected AI assistants (MCP tools) still act on their own club's
  events only.
* The club's announcement history page and overview (`/clubs/overview`,
  `/clubs/dashboard`) count the club's own events.
* A co-host makes no discount codes or expenses for the event (they are the
  host's books), and duplicating the event is the host's.

## 7. Migration

`add_event_cohosts` creates the table and its indexes (nothing backfilled,
nothing rewritten; a brief lock on the referenced tables under
`lock_timeout 3s`); downgrade drops them. Proven up, down and up through
alembic on a scratch database built from base. Any other open migration
written after `review_emails` must be re-pointed by whichever merges second.
