# Digital ticket designs — integration guide

This package contains ten landscape digital ticket designs as standalone HTML templates. Your job is to integrate them into the app so that any issued ticket can be rendered in any of the ten styles from live ticket data.

## What is in this package

```
ticket-designs/
  README.md            this guide
  sample-data.json     example payload with every field
  templates/
    01-dusk-horizon.html
    02-midnight-arcade.html
    03-daylight-boarding-pass.html
    04-live-session.html
    05-grand-premiere.html
    06-garden-party.html
    07-blueprint.html
    08-pop-block.html
    09-northern-lights.html
    10-match-day.html
```

Each template is one self-contained file: plain HTML with inline styles and inline SVG. There is no JavaScript, no build step and no image assets. The only external dependency is one Google Fonts stylesheet per template (the `<link>` in its `<head>`).

## Canvas and sizing

- Every template is a fixed **1280 × 600 px** stage (`.ticket-stage`), with the ticket itself about 1140 × 460 px centred inside it. The stage background is part of the design (it shows through the notches and carries the glow or shadow).
- The designs are **landscape only**. Do not reflow them for portrait.
- To fit a smaller or larger container, scale the whole stage uniformly. Do not change the internal pixel values.

```css
.ticket-frame { width: 100%; aspect-ratio: 1280 / 600; overflow: hidden; }
.ticket-frame > .ticket-stage { transform-origin: top left; transform: scale(var(--s)); }
```

```js
// --s = container width / 1280, recomputed on resize
const ro = new ResizeObserver(([e]) =>
  e.target.style.setProperty('--s', e.contentRect.width / 1280));
ro.observe(frameEl);
```

- On a phone held in portrait, show the ticket scaled to the screen width and offer a full-screen view that rotates it 90° so it fills the screen.

## Data tokens

Text slots are marked with `{{token}}` placeholders. Replace each with the HTML-escaped value from the ticket record.

| Token | Meaning | Example | Suggested max length |
|---|---|---|---|
| `{{app_name}}` | Product or organiser name | `EVENTLY` | 18 |
| `{{event_name}}` | Event title (the hero text) | `Sunset Sessions` | 16 |
| `{{tagline}}` | Sub-line, headline act or fixture | `An evening of live music` | 36 |
| `{{date}}` | Event date, already formatted | `14 Feb 2027` | 12 |
| `{{doors_time}}` | Doors or gates open time | `18:30` | 8 |
| `{{start_time}}` | Start or kick-off time (templates 03 and 10 only) | `19:30` | 8 |
| `{{venue}}` | Venue and city | `Tagore Theatre, Trivandrum` | 28 |
| `{{gate}}` | Gate or entrance | `B2` | 4 |
| `{{row}}` | Row | `F` | 4 |
| `{{seat}}` | Seat number | `12` | 4 |
| `{{ticket_type}}` | Tier | `VIP` | 12 |
| `{{ticket_id}}` | Human-readable ticket reference | `TK-48213` | 12 |
| `{{attendee_name}}` | Ticket holder | `Asha Nair` | 22 |

Notes:

- Templates 01, 02 and 04–09 do not use `{{start_time}}`; template 03 does not use `{{tagline}}`.
- Several templates force the event name and ticket type to upper case with CSS. Pass values in normal case.
- Some static labels are themed per design on purpose (for example "PASSENGER", "PLAYER ONE", "SUPPORTER" all label the attendee). Keep them unless the product owner asks otherwise.
- If an event has no gate, row or seat (general admission), hide that field's whole label-and-value block rather than printing an empty value or a dash.

## QR code

Each template has one element marked `data-slot="qr"`: a white square (168–190 px) containing three corner markers and the text `[QR CODE]`. These children are placeholders only.

- Remove the children of `[data-slot="qr"]` and insert the real QR code (inline SVG or `<img>`) at `width: 100%; height: 100%`, keeping the element's own white background, border and radius. Add about 12 px of padding so the code keeps a quiet zone.
- Render the QR in **pure black on pure white**, whatever the ticket's theme. Do not tint it.
- Encode whatever the app's entry validation already uses (typically a signed ticket token), not the visible `ticket_id` alone.
- Update the element's `aria-label` to something like `QR code for ticket {{ticket_id}}`.
- Template 03 also has a decorative barcode strip (the `repeating-linear-gradient` div above the ticket ID). It is not scannable. Leave it as decoration or replace it with a real barcode if the app issues one.

## Long text

The layouts are fixed size, so guard against overflow:

- `{{event_name}}`: keep on one line where possible. If it is wider than its column, reduce the `<h1>` font size step by step (down to about 60% of the designed size), then allow a second line. Never let it run under the QR stub.
- All other values: single line, `white-space: nowrap; overflow: hidden; text-overflow: ellipsis`.
- Validate the suggested maximum lengths above at the point where events are created, so most tickets never need shrinking.

## Fonts

Each template loads its own families from Google Fonts. If the app must work offline or inside a restricted webview, self-host the same families and weights and replace the `<link>`. Keep the pairing per design; the type is a large part of each ticket's character.

| Template | Fonts |
|---|---|
| 01 Dusk Horizon | Bricolage Grotesque, JetBrains Mono |
| 02 Midnight Arcade | Orbitron, Share Tech Mono |
| 03 Daylight Boarding Pass | Barlow Condensed, Barlow, IBM Plex Mono |
| 04 Live Session | Anton, DM Sans, DM Mono |
| 05 Grand Premiere | Cinzel, Cormorant Garamond, Josefin Sans |
| 06 Garden Party | Fraunces, DM Sans |
| 07 Blueprint | Space Grotesk, Space Mono |
| 08 Pop Block | Archivo Black, Archivo |
| 09 Northern Lights | Sora |
| 10 Match Day | Saira Condensed, Saira |

Wait for fonts to load (`document.fonts.ready`) before measuring text for the long-name shrink logic or before exporting an image.

## Suggested integration approach

1. Convert each template's `<body>` markup into a component in the app's own framework (one component per design, or one component with a `design` prop). Keep the inline styles as they are; they are the design specification.
2. Give every component the same props, matching the token table above, plus `qr` (the QR payload or a ready-made SVG).
3. Store the chosen design per event (for example `ticket_design: "04-live-session"`) so organisers pick a style once and every ticket for that event uses it.
4. Wrap the component in the scaling frame from "Canvas and sizing".
5. For "download" or "share", export the 1280 × 600 stage as a PNG at 2× (2560 × 1200), after fonts have loaded.

## Things to keep as designed

- Colours, spacing, radii, shadows and the decorative SVG artwork in each template.
- The white QR panel and its contrast.
- The decorative SVGs are marked `aria-hidden="true"`; keep that so screen readers skip them.
- Template 09 uses `backdrop-filter` on the QR panel. Where it is unsupported the panel falls back to a solid dark tint, which is acceptable.

## Acceptance checklist

- All ten designs render from the same data payload with no leftover `{{…}}` or `[…]` text.
- The QR code scans from a phone screen at normal brightness in every design.
- A 16-character event name fits on one line in every design; a 30-character name shrinks or wraps without overlapping other elements.
- General-admission tickets (no gate, row or seat) show no empty fields.
- The ticket scales cleanly from 360 px to 1280 px container width with no horizontal scroll.
- PNG export matches the on-screen ticket, with the correct fonts.
