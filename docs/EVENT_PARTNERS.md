# Event sponsors and partners

Each event can list its own **sponsors** and **partners**: logos on its
public page, on the website and in the app. They are separate from the
club's year-round partners (`/clubs/partners`, on the club page and the club
site), though a club can copy any of those onto an event.

Code: `backend/app/api/event_partners/`. Migration: `add_event_partners`.
Tests: `backend/tests/test_event_partners.py`. Everything is additive: old
apps and older site builds keep working, and a client talking to a server
without this hides the section.

All paths are under `/api/v1`.

---

## 1. The row

| Field | Rule |
|---|---|
| `id` | |
| `kind` | `"sponsor"` or `"partner"` (a check constraint holds it). |
| `name` | Required, trimmed, 1–100 characters. |
| `label` | Optional, trimmed, at most 60 characters: "Title sponsor", "Gold sponsor", "Venue partner", "Media partner". An empty string clears it. |
| `link` | Optional. An absolute `https://` URL with a host (one with a dot), at most 500 characters, no spaces. `http://`, other schemes and bare domains are refused. An empty string clears it. |
| `position` | Order within its kind, 0 first. Set by the server. |
| `logo` | Optional picture: `{thumbnail, medium, large, original}` URLs. |
| `club_partner_id` | The club partner it was copied from, or `null`. |

**At most 50** live rows an event, sponsors and partners together. Rows are
soft-deleted (`is_deleted`, `deleted_at`), like the club's partners; a
deleted row never shows or counts again.

### The logo

- A square frame, 1:1; we ask for 1080 × 1080.
- PNG, JPEG, WebP or GIF, at most 10 MB.
- Stored under `events/partners/` with a versioned name, as every public
  picture is (`app/core/storage/fields.py`).
- Sizes: `thumbnail` 150, `medium` 400 and `large` 1080, each the **whole**
  picture fitted inside a square box of that side, keeping its shape and
  never enlarged. Nothing is cropped, so a wide wordmark stays whole.
- `events/partners/` is a public folder (`PUBLIC_FOLDERS` in
  `app/core/storage/media.py`): with `APP_MEDIA_CDN_URL` on, its links are on
  the media CDN. The Worker's own list (`infra/cloudflare/media/media.js` in
  the app repo) and the Worker's IAM read policy must list it too.
- Replacing a logo deletes the old files once the new one is saved. A row's
  files are its own: a copied logo is a copy (see §3), never a shared key.

---

## 2. The club's endpoints

Auth: the club's session (`ClubAuth`), for the event's own club or a
platform admin (`deliveries.event_for_club`). Team roles take the default
rules for an event's setup (`app/api/club_access/policy.py`), as the agenda
and organisers do: owners, admins and editors read and change; door staff
read, and any change is `403 ROLE_FORBIDDEN`.

| Method and path | Body | Answer |
|---|---|---|
| `GET /events/{event_id}/partners` | | `[row]`: sponsors first, then partners, each by `position`. |
| `POST /events/{event_id}/partners` | `{kind, name, label?, link?}` | `201` row, last of its kind. |
| `PATCH /events/{event_id}/partners/{id}` | any of `{kind, name, label, link}` | row. Only what is sent changes. A new `kind` puts it last of that kind. |
| `PUT /events/{event_id}/partners/{id}/logo` | multipart `logo` | row |
| `DELETE /events/{event_id}/partners/{id}` | | `{"deleted": true}` |
| `PUT /events/{event_id}/partners/order` | `{kind, ids}` | the whole list, as GET. `ids` must be every live row of `kind`, each once. |
| `POST /events/{event_id}/partners/from-club` | `{kind, club_partner_ids}` (1–50) | `201` `{added: [row], skipped: [club_partner_id]}` |

Adding a row, copying from the club and changing a row's kind lock the
event's row (`SELECT … FOR UPDATE`) until commit, so two requests at once can
neither pass the cap together nor take the same position.

### Errors

| Status | `error_code` | When |
|---|---|---|
| 401 | | Not signed in. |
| 403 | | Another club's event. |
| 403 | `ROLE_FORBIDDEN` | Door staff changing anything. |
| 404 | | No such event, or deleted. |
| 404 | `EVENT_PARTNER_NOT_FOUND` | No live row with that id on this event. |
| 422 | | A field is wrong: `errors` names it. |
| 422 | `EVENT_PARTNERS_LIMIT` | Past 50 rows. |
| 422 | `EVENT_PARTNERS_ORDER` | The order's `ids` are not exactly the kind's live rows. |
| 422 | `CLUB_PARTNER_NOT_FOUND` | Not one of this club's live partners. |
| 422 | | The logo is not an image, or cannot be read. |
| 413 | | The logo is over 10 MB. |

---

## 3. From the club's partners

`POST /events/{event_id}/partners/from-club` makes, for each club partner in
the order given, a row of `kind` at the end of that kind:

- `name`: the club partner's name;
- `label`: its `role`, cut to 60 characters (club partners have no label of
  their own; "Industry partner" is the usual role);
- `link`: `null`. Club partners have no link field, so there is nothing to copy;
- `logo`: a **copy** of its logo. The original is read from the bucket
  (`objects.fetch`) and uploaded again through this table's column, so it
  gets its own key and its own square sizes. If it cannot be read, the row is
  still added, without a logo, and a warning is logged;
- `club_partner_id`: its id.

A club partner already on the event (a live row with that
`club_partner_id`, of either kind) is skipped and listed in `skipped`. The
whole request is refused if it would pass 50.

The copy stands alone. Renaming the club partner, replacing its logo or
removing it changes nothing on the event.

---

## 4. Where they show

### `GET /events/info/{event_id}`

The club's own event payload (the dashboard's editor), the admin portal's
event detail and the app's older detail read gain:

```json
"sponsors": [row, …],
"partners": [row, …]
```

Live rows only, each list by `position`. Always present on this server.

### `GET /discover/events/{ref}`

The public event page (website and app) gains two top-level lists beside
`speakers`, `agenda` and `team`:

```json
"sponsors": [{"id", "kind", "name", "label", "link", "logo"}],
"partners": [{"id", "kind", "name", "label", "link", "logo"}]
```

`logo` is one URL: the `medium` size, else `large`, else the original; `null`
when there is none.

Clients show a **Sponsors** section and then a **Partners** section, each
only when it has entries. A tile is square, with the logo whole (contain,
padded), then the name, the label under it, and the tile links out when there
is a link.

### Other effects

- A change to the rows tells the public site to render the event's page
  again (`app/core/site_revalidate.py`, as for speakers and organisers).
- Duplicating an event does not copy them: they usually differ per edition.
