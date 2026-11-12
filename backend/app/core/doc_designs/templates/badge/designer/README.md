# ID card templates: integration guide

This package contains 15 ID card / badge designs for an event-hosting and club-management platform. The cards are printed on demand at self check-in kiosks. This guide is written for the developer or AI agent integrating them into the software.

## What is in this package

| Path | What it is |
|---|---|
| `templates/*.html` | 15 standalone HTML templates, one per design. Plain HTML with inline CSS, no JavaScript, no build step. |
| `templates.json` | Machine-readable manifest: card size, and for each template its id, file, use (event or club), whether it has a photo, default accent colour and the exact list of fields it needs. |
| `sample-data.json` | One example value for every field, for testing. |
| `sample-photo.svg`, `sample-qr.svg` | Stand-in images for testing only. The sample QR does not scan. |
| `previews/*.png` | Reference renders of each template filled with the sample data. `previews/all-designs.png` shows all 15. Your output should match these. |

## Card specification

- Size: 4 x 6 in portrait (384 x 576 CSS px at 96 px/in). Each template's root element is exactly 384 x 576 px and `@page` is set to `4in 6in` with zero margin.
- The designs run to the edge (full bleed). If the printer cannot print edge to edge, scale the whole card down uniformly; do not crop.
- The pill shape at the top centre of every card marks the lanyard slot position. Remove that element if the card stock is pre-punched or the slot is not wanted.
- If the kiosk uses a different stock size (for example CR80, 2.125 x 3.375 in), the templates must be redesigned for it, not just scaled, because small text would fall below a readable size. Ask the owner before changing size.

## How to fill a template

Templates use double-brace tokens such as `{{full_name}}`. They are compatible with Mustache / Handlebars, or a plain string replace.

1. Pick a template by `id` from `templates.json`.
2. Build a data object containing every field listed for that template.
3. HTML-escape every text value before inserting it (names can contain `&`, `<`, apostrophes).
4. Replace each `{{field}}` token. Every token must be replaced; a leftover token must fail the print job rather than print.
5. Render the HTML in Chromium (kiosk webview, or headless Chromium / Puppeteer / Playwright on the server) and print or export a PDF at 4 x 6 in with margins set to none and background graphics enabled.

Minimal fill example (JavaScript):

```js
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const urlFields = new Set(['photo_url', 'qr_code_url']);
function fill(html, data) {
  return html.replace(/\{\{(\w+)\}\}/g, (_, key) => {
    if (data[key] == null || data[key] === '') throw new Error('Missing field: ' + key);
    return urlFields.has(key) ? encodeURI(data[key]) : esc(data[key]);
  });
}
```

## Templates

| id | Name | Use | Photo | Default accent | Fields |
|---|---|---|---|---|---|
| `01-ticket-stub` | Ticket Stub | event | yes | #FF8A3D | `access_level`, `badge_id`, `event_date`, `event_name`, `event_venue`, `first_name`, `job_title`, `last_name`, `organization`, `photo_url`, `qr_code_url`, `role` |
| `02-arch` | Arch | club | yes | #F2B441 | `full_name`, `member_id`, `membership_tier`, `org_founded_year`, `org_name`, `photo_url`, `qr_code_url`, `valid_thru` |
| `03-role-rail` | Role Rail | event | yes | #D63A22 | `access_level`, `event_name`, `event_year`, `first_name`, `job_title`, `last_name`, `organization`, `photo_url`, `qr_code_url`, `role`, `session_hall`, `session_time` |
| `04-big-name` | Big Name | event | no | #FFD83A | `access_level`, `badge_id`, `event_date`, `event_name`, `first_name`, `last_name`, `organization`, `qr_code_url`, `role` |
| `05-monogram` | Monogram | club | no | #0B5E5E | `city`, `full_name`, `initials`, `joined_date`, `member_id`, `org_name`, `qr_code_url`, `role` |
| `06-thermal-mono` | Thermal Mono | event | no | — | `access_level`, `badge_id`, `checkin_time`, `event_date`, `event_name`, `first_name`, `kiosk_id`, `last_name`, `organization`, `qr_code_url`, `role`, `zone` |
| `07-polaroid` | Polaroid | club | yes | #5B3DF5 | `city`, `full_name`, `member_id`, `member_since_year`, `org_name`, `photo_url`, `qr_code_url`, `role` |
| `08-full-bleed` | Full Bleed | event | yes | #C6F432 | `badge_id`, `booth_no`, `event_name`, `event_year`, `full_name`, `job_title`, `organization`, `photo_url`, `qr_code_url`, `role` |
| `09-boarding-pass` | Boarding Pass | event | no | #FF8A4C | `badge_id`, `day_label`, `doors_open_time`, `event_name`, `full_name`, `hall`, `organization`, `qr_code_url`, `role`, `zone` |
| `10-colour-blocks` | Colour Blocks | club | no | — | `chapter`, `full_name`, `member_id`, `org_name`, `qr_code_url`, `role`, `valid_thru` |
| `11-night-grid` | Night Grid | event | no | #6CF0C2 | `badge_id`, `event_date`, `event_name`, `event_tagline`, `first_name`, `last_name`, `qr_code_url`, `role`, `table_no`, `team_name`, `track` |
| `12-stamp` | Stamp | club | no | #B42B22 | `city`, `full_name`, `joined_date`, `member_id`, `membership_tier`, `org_name`, `qr_code_url`, `valid_year` |
| `13-sash` | Sash | event | no | #1F3BD8 | `access_level`, `badge_id`, `event_date`, `event_name`, `first_name`, `last_name`, `organization`, `qr_code_url`, `role` |
| `14-fine-line` | Fine Line | club | no | #B4532A | `first_name`, `last_name`, `member_id`, `member_since_year`, `membership_tier`, `org_name`, `qr_code_url`, `valid_thru` |
| `15-race-bib` | Race Bib | club | no | #FF6A3D | `bib_number`, `category`, `event_date`, `event_name`, `full_name`, `org_name`, `qr_code_url`, `start_time`, `wave` |

"Use" is the context the sample wording was written for. Any template can serve either an event or a club if the fields are mapped sensibly.

## Field dictionary

Identity
- `first_name`, `last_name`, `full_name`: the person's name. Some templates use first and last on separate lines, others the full name on one line.
- `initials`: two letters, upper case (template 05 only).
- `job_title`, `organization`: the person's title and company, college or club.
- `photo_url`: URL or data URI of the person's photo. Photo templates crop it with `object-fit: cover`, so supply a roughly square, face-centred image of at least 600 x 600 px.

Access and role
- `role`: short role label, upper case, ideally 10 characters or fewer (ATTENDEE, SPEAKER, VOLUNTEER, ORGANISER, EXHIBITOR, GUEST, MEMBER).
- `access_level`: one short line such as "All-access" or "Stage + Lounge".
- `zone`, `hall`, `day_label`, `booth_no`, `table_no`, `track`, `team_name`, `session_hall`, `session_time`, `doors_open_time`: short event logistics values used by individual templates.
- `badge_id`: unique badge number for this person at this event.
- `qr_code_url`: URL or data URI of the QR code image for this badge (see QR codes below).
- `checkin_time`, `kiosk_id`: filled by the kiosk at print time (template 06).

Event
- `event_name`, `event_date`, `event_year`, `event_venue`, `event_tagline`.

Club or community
- `org_name`, `org_founded_year`, `city`, `chapter`.
- `membership_tier`: upper case, for example GOLD MEMBER, LIFE MEMBER, PATRON.
- `member_id`, `member_since_year`, `joined_date`, `valid_thru`, `valid_year`.

Race bib (template 15)
- `bib_number` (up to 4 digits), `category`, `wave`, `start_time`. The dashed "[SPONSOR LOGOS]" box is a fixed placeholder; replace that element with the sponsor logo image or remove it.

## Rules that keep the cards looking right

**Long text.** The templates are sized for typical values. Before printing, shrink any name or role that overflows its line instead of letting it wrap or clip. Run this after the fonts load, on the name and role elements:

```js
function fitText(el, minPx = 18) {
  el.style.whiteSpace = 'nowrap';
  let size = parseFloat(getComputedStyle(el).fontSize);
  while (el.scrollWidth > el.parentElement.clientWidth && size > minPx) {
    size -= 1;
    el.style.fontSize = size + 'px';
  }
}
```

Where a template puts first and last name on two lines (`{{first_name}}<br>{{last_name}}`), measure the wider of the two lines. Truncate with an ellipsis only if the text still overflows at the minimum size.

**Accent colour.** Templates with an accent define it once as the CSS variable `--accent` in the `<style>` block. Override that one value to colour-code roles or brand a club, for example by adding `<style>:root{--accent:#0B6E6E}</style>`. `accent_suggestions` in `templates.json` lists tested alternatives for each template. Check that text on the accent still has enough contrast: some templates put white text on it (03, 05, 07, 13) and others put dark text on it (01, 04, 08, 09, 15).

**QR codes.** Generate a real QR code per badge with a QR library and pass it as `qr_code_url` (an SVG or a PNG of at least 400 x 400 px). Encode the badge's check-in URL or id. Keep the code black on white and do not shrink the image below the size set in the template (76 to 108 px, about 0.8 to 1.1 in). Use error correction level M or higher. Test that printed samples scan.

**Fonts.** Each template loads its fonts from Google Fonts (Space Grotesk, Inter, Space Mono, Instrument Serif, Caveat). Kiosks are often offline or slow, so self-host these font files with `@font-face` and remove the Google Fonts `<link>`. Wait for `document.fonts.ready` before printing, otherwise the card prints in a fallback font.

**Printer type.** Templates 01 to 05 and 07 to 15 need a colour printer. Template 06 (Thermal Mono) is pure black on white and is the one to use on single-colour direct-thermal kiosk printers. Template 02, 08 and 11 use large dark areas and need a printer that handles heavy ink coverage.

**Missing data.** If an optional value is absent (for example no job title), remove the element that holds it, including any separator such as " · ", rather than printing an empty line.

## Suggested integration

1. Store the 15 templates and `templates.json` in the application.
2. Let the organiser choose a template per event or club, and optionally a different accent colour per role.
3. Map the application's own data model to the field names above in one place (a single mapping function per template family).
4. At check-in: build the data object, fill the template, apply the long-text fit, wait for fonts and images to load, then print.
5. Offer the organiser a preview using their real event data before the event starts.

## Acceptance checklist

- Each template filled with `sample-data.json` matches its image in `previews/`.
- No `{{token}}` or bracketed placeholder is visible on any printed card.
- A 30-character name and a 12-character role fit without clipping on every template in use.
- The printed QR code scans from a phone at arm's length.
- Fonts render correctly with the kiosk offline.
- Printed size measures 4 x 6 in with nothing cropped.
