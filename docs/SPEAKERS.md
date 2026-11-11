# Speakers directory

Every speaker a club adds to an event is a profile in one central list in the
main database (`app/api/speakers`). A person who spoke at five events is one
profile with five talks. The shared contract with the dashboards, the website
and the app is `feature-register/speakers-contract.md`.

## The data

| Table | Holds |
| --- | --- |
| `speaker_profiles` | name, `name_key` (how names are compared), slug, headline, bio (≤600), photo, links `{website, linkedin, x, github, other}`, city, topics (topic ids), status `active / hidden / merged`, `merged_into_id`, `claimed_by_user_id`, `created_by_club_id`, `removed_at` |
| `speaker_contacts` | one row per profile and club: email, phone, notes, when and by whom the speaker's agreement was ticked |
| `speaker_merge_candidates` | possible duplicates (`a_id < b_id`), reason, score, `open / merged / dismissed` |
| `speaker_history` | every change: who (admin, club, speaker, system), what, before and after |
| `speaker_follows` | app users following a profile |
| `speaker_claims` | invite links (sha256 of the token only), email, expiry, used, replaced |
| `agenda_item_speakers` | an agenda session's speakers: the event's own speaker rows, in order |

`event_speakers` keeps its own copy of what the event page shows (name,
designation, photo, bio, link) and gains `speaker_profile_id` and `talk_title`.
Agenda sessions keep their free-text `speaker`.

A photo is stored in the event copies' folder (`events/speakers/`) with the
same sizes, so a key copied between a profile and an event row keeps its
thumbnails both ways.

## Who sees what

- **A club's own speakers**: profiles linked to a speaker row on one of the
  club's events (any status), or made by its form. The picker suggests only
  these, then (by name only) profiles their speakers have claimed.
- **Contacts** are the entering club's. Admins see the email and phone of each
  club's contact, never a club's notes. No other club ever sees them, and they
  are not exported for anyone but that club. The history says a contact
  changed, never what it is.
- **The public** sees an active profile with a talk at a published, listed
  event (or a claimed one), and only those talks. A profile with only a draft
  has no page.
- **Hidden**: not suggested, no public page, `profile_slug` null on event
  payloads; event pages keep the name the club published.
- **Removed on request**: hidden, every club's contact deleted, followers and
  open invites gone, a history line. Event pages keep the club's copy.

Club routes are behind the role rule `_SPEAKERS` (club_access/policy.py):
owner, admins and editors; door staff not at all. Every plan.

## Linking an event's speakers

On every create and update of an event (`events.service`), each speaker row
is linked (`speakers.links.link_event_speakers`):

1. `speaker_profile_id` sent and different from the row's: that profile, if
   the club may pick it (its own, or claimed; a merged one is its survivor),
   else 422 `SPEAKER_NOT_AVAILABLE` before anything is saved. An empty photo
   takes the profile's.
2. Nothing sent and the row is linked: it keeps its profile (older clients).
3. Not linked: the club's own active profile with exactly that `name_key`
   (empty fields of an unclaimed profile filled from the row), else a new
   profile from the row, and a merge candidate against profiles of the same
   name at other clubs.

`talk_title` is the row's when sent. A club's edit changes only its event
copy; `POST /clubs/speakers/{id}/apply-to-upcoming` copies the profile's name,
headline, bio, photo and first link onto the club's rows on events not over
yet (never a past one, never another club's).

Agenda sessions take `event_speaker_ids` (this event's rows only, else 422
`NOT_THIS_EVENTS_SPEAKER`); with speakers and no free text, the text is set to
their names, which older clients show. Removing a speaker from the event
removes it from its sessions (the link cascades).

## Names

`speakers.names.name_key`: case folded, accents and punctuation gone, leading
titles (Dr, Prof, Mr, Ms, Er, Shri…) dropped. "Dr. Meera Nair" and "meera
nair" are one name. The picker and the admin search use pg_trgm on
`name_key` (the words anywhere, or a near spelling).

## The backfill: run once after the deploy

`speakers.backfill` (`app/api/speakers/backfill.py`) links every existing row;
it changes nothing shown and can run again (a second run links nothing new,
adds no pair twice). The same person, oldest event first:

1. the same link (host without www, path, no query) under a name that is
   plainly theirs ("Meera" / "Meera Nair"); the same link under another name
   goes to the queue (`same_link_other_name`);
2. the same name at the same club;
3. the same name and the same photo key.

Anything else is a new profile. Then profiles with the same name at different
clubs go to the queue (`same_name_other_club`). Agenda free text is matched to
that event's speakers by the whole text, else each name in "A, B & C"; a name
that matches nobody stays text. Profiles without topics take their events'.

Run it, either way:

```sh
# From the admin portal or curl (an admin with MANAGE_CONTENT): queues the job.
curl -X POST https://api.myotherapp.in/api/v1/admin/speakers/backfill -H "Authorization: Bearer <admin token>"

# On the server: dry run first (counts, nothing kept), then for real.
$DC exec -T backend python -m scripts.speakers_backfill
$DC exec -T backend python -m scripts.speakers_backfill --apply
```

The job writes an audit line (`speaker.backfill_done`) with the counts.

## Merging

`POST /admin/speakers/merge {keep_id, merge_id}` moves the merged profile's
event rows, its contacts (or fills the kept contact's empty fields when the
club has one on both), its followers, its claim (two claimed profiles are
refused) and fills the kept profile's empty fields. Everything moved is
written on the merged profile's `merged_into` history line; `POST
/admin/speakers/{id}/unmerge` reads it back and moves those things home (what
moved on since stays). The merged profile's slug answers `{redirect_slug}`.

## Claiming

`POST /admin/speakers/{id}/invite {email}` emails
`https://myotherapp.in/speakers/claim/{token}` (14 days; a newer invite
replaces older ones). `GET /speakers/claim/{token}` says what it is for and
whether it still works (`reason`: used, replaced, expired, unavailable,
claimed); `POST` by a signed-in app user claims it (one profile per account).
The speaker then edits it at `/speakers/me` (`PATCH`, `PUT /me/photo`).

## Following and the push

`POST|DELETE /speakers/{slug}/follow`, `GET /speakers/following`. Whenever an
event is created, saved, or moved to published or live, the job
`speakers.tell_followers` (one waiting per event) looks at it: if it is
public and not over, each follower of one of its speakers' active profiles
who has no `speaker_event` line for it gets one, and one push (`{type:
speaker_event, event_id, slug}`) goes to exactly those people. Once per event
and person, however often it is saved. The followed clubs switch
(`notification_settings.clubs`) turns these off too.

## Routes

| Who | Route |
| --- | --- |
| Club | `GET /clubs/speakers?q=&limit=` picker · `GET /clubs/speakers/list?q=&page=&page_size=&format=xlsx` · `GET /clubs/speakers/{id}` · `PUT /clubs/speakers/{id}/contact` · `POST /clubs/speakers/{id}/apply-to-upcoming` |
| Admin (VIEW to read, MANAGE_CONTENT to change) | `GET /admin/speakers?q&topic&city&club_id&year&status&claimed&sort&page&page_size&format=xlsx` · `GET /admin/speakers/duplicates?status=` · `POST /admin/speakers/merge` · `POST /admin/speakers/duplicates/{id}/dismiss` · `POST /admin/speakers/backfill` · `GET|PATCH /admin/speakers/{id}` · `PUT /admin/speakers/{id}/photo` · `POST /admin/speakers/{id}/hide|unhide|remove-on-request|unmerge|invite` |
| Speaker | `GET|POST /speakers/claim/{token}` · `GET|PATCH /speakers/me` · `PUT /speakers/me/photo` |
| Public / app user | `GET /speakers/{slug}` · `POST|DELETE /speakers/{slug}/follow` · `GET /speakers/following` · `GET /speakers/sitemap` |

Event payloads (`/events/info/{id}`, `/discover/events/{ref}`) add
`speaker_profile_id`, `talk_title` and `profile_slug` to each speaker; agenda
items add `event_speaker_ids` and `speakers`. The AI event draft adds
`speaker_profile_id` (an exact name match only) and `matches` to each speaker;
the MCP tool `list_past_speakers` looks names up.
