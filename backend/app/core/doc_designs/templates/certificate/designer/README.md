# Certificate templates — integration guide

Ten QR-verifiable certificate designs, delivered as standalone HTML templates. This guide is written for the developer or AI agent integrating them into the certificate software.

## What is in this folder

| Path | What it is |
|---|---|
| `templates/*.html` | The ten templates. Each is one self-contained HTML file with `{{token}}` placeholders. |
| `fields.json` | Machine-readable manifest: every token, what it means, which template uses which tokens, default accent colours, and sample data. |
| `previews/*.png` | Reference renders of each template filled with the sample data. Use them to check your output visually. |

## The templates

| File | Style | Best for |
|---|---|---|
| `01-heritage.html` | Classic, double frame, script name, seal | Formal course completion |
| `02-midnight-deco.html` | Dark navy and gold art deco | Excellence awards, digital sharing |
| `03-bold-geometric.html` | Modern split layout, cobalt panel | Achievement, modern institutes |
| `04-verified-pass.html` | Ticket style with a large QR stub | Credentials where verification is the focus |
| `05-botanical.html` | Soft green with leaf shapes | Wellness, arts, general courses |
| `06-blueprint.html` | Graph paper with engineering title block | Technical and engineering courses |
| `07-swiss-minimal.html` | Large headline, minimal grid | Professional and corporate training |
| `08-royal-ribbon.html` | Ribbon and medal, two signatures | Merit and rank awards |
| `09-terminal.html` | Dark, code style | Programming and IT courses |
| `10-confetti.html` | Bright and playful | Workshops, events, participation |

## Page size

- Every template is a fixed 1123 × 794 CSS px page, which is A4 landscape (297 × 210 mm) at 96 dpi.
- Each file already sets `@page { size: A4 landscape; margin: 0 }` and forces background colours to print.
- Do not change the root element's width or height. The layouts are absolutely sized and are not responsive.

## How to render a certificate

1. Load the template file as a string.
2. Replace every `{{token}}` with its value. **HTML-escape every text value first** (`&`, `<`, `>`, `"`). The tokens are plain `{{name}}` with no spaces, so they work with Mustache, Handlebars, Jinja2, Liquid, Twig, Blade or a simple regex replace: `/\{\{(\w+)\}\}/g`.
3. Render the result to PDF or PNG with a headless Chromium (Puppeteer or Playwright):
   - PDF: `page.pdf({ width: '297mm', height: '210mm', printBackground: true, preferCSSPageSize: true })`
   - PNG: viewport 1123 × 794, `deviceScaleFactor: 2` or higher for sharp output.
4. Wait for fonts before capturing: `await page.evaluate(() => document.fonts.ready)`.

Do not use a non-browser HTML-to-PDF library (wkhtmltopdf, dompdf, WeasyPrint and similar). The templates rely on flexbox, grid, inline SVG and `text-wrap`, which those engines render incorrectly.

## Tokens

All tokens are listed with descriptions in `fields.json`. Summary:

**Used by every template**

| Token | Meaning |
|---|---|
| `recipient_name` | Full name of the person receiving the certificate |
| `institute_name` | Issuing organisation |
| `certificate_id` | Unique certificate number, printed on the page and encoded in the QR |
| `qr_code_src` | Image source for the QR code (see below) |
| `signatory_name`, `signatory_designation` | Person signing |
| `accent_color` | Hex colour, e.g. `#6B1F2E`. Defaults per template are in `fields.json` |

**Used by some templates** (check `fields.json` → `templates.<name>.fields`)

`course_title`, `grade`, `duration_hours`, `issue_date`, `completion_date`, `start_date`, `end_date`, `expiry_date`, `issue_year`, `issue_city`, `verify_url`, `logo_src`, `rank_or_grade`, `second_signatory_name`, `second_signatory_designation`, `event_title`, `event_date`.

Rules:

- Dates are inserted as given. Format them before substitution (for example `12 November 2026`).
- `duration_hours` is a number only; the templates add the word "hours".
- If a value is not available, do not leave the raw token in the page. Either supply a fallback or remove the surrounding element for that issue.
- Some templates show the institute name in capitals. That is done with CSS, so always pass normal casing.

## The QR code

- In each template the QR is an `<img data-slot="qr" src="{{qr_code_src}}">` with a fixed width and height.
- Generate the QR yourself and pass it as a data URI (`data:image/svg+xml;base64,...` or `data:image/png;base64,...`) or as a URL the renderer can reach. SVG is preferred because it stays sharp in print.
- Encode the full verification URL for that certificate, for example `https://yourdomain.com/verify/CERT-2026-000123`. Use the same value for `verify_url` where a template prints it.
- Generate the QR with **no quiet zone (border 0)**. Every template already places the image on a white padded background.
- Use error correction level M or higher. Keep the code dark on white; do not recolour it with the accent colour.
- The smallest QR in the set is 84 px (about 22 mm when printed). Keep the encoded URL short so the modules stay large enough to scan; under roughly 80 characters is a safe target.

## Logo and signature

- `logo_src` (templates 01, 03, 05, 06): an `<img data-slot="logo">` with fixed size and `object-fit: contain`. Pass a data URI or reachable URL. A square or circular mark works best; template 06 has a wider 96 × 44 slot. Use a transparent PNG or SVG.
- Signature: each signature line is an empty `<div data-slot="signature">`. To show a scanned signature, insert an image inside that div, for example `<img src="..." style="height: 40px; display: block">`. If left empty the line stays blank for signing by hand.

## Accent colour

- `accent_color` appears many times in each file; replace all occurrences with one hex value.
- Defaults and three alternative colours per template are in `fields.json`.
- Templates 02 and 09 have dark backgrounds, so their accent must be a light colour. Templates 01, 05, 06, 07 and 08 use the accent for text on a light background, so it must be a dark colour. In 04 and 10 the accent is a background fill behind dark text, so keep it light.

## Fonts

- Each template loads its fonts from Google Fonts with a `<link>` in the `<head>`. The render server therefore needs internet access at render time.
- For offline or faster rendering, self-host the same families (for example from the `@fontsource/*` npm packages) and replace the `<link>` with local `@font-face` rules. The family names used are in each file's `<link>` URL.
- If fonts fail to load the layout falls back to system fonts and will not match the previews.

## Long values

The layouts were checked with a 15-character name and a 26-character course title. Names and titles wrap onto a second line when long, and the layouts have room for that. For very long values (names over about 30 characters, titles over about 60) reduce the font size of that element for that certificate rather than letting it run to three lines.

## Suggested verification flow

This is a recommendation, not something the templates enforce.

1. When a certificate is issued, store a record: `certificate_id`, recipient, course, dates, grade, status (`valid`, `revoked`, `expired`).
2. Make `certificate_id` hard to guess (a random component, not just a running number), or sign the URL, so IDs cannot be enumerated.
3. The QR opens a public page at the verification URL. That page reads the record and shows the same details as the printed certificate plus its status, so anyone can compare the two.
4. If a certificate is revoked or reissued, update the record; the printed QR keeps pointing at the live status.

## Acceptance check

For each template, render it with the `sample` data in `fields.json` and compare with the matching file in `previews/`. Then confirm:

- no `{{` remains in the output HTML;
- the PDF is exactly one A4 landscape page with backgrounds printed;
- the QR scans from the PDF on screen and from a paper print, and opens the right verification page.
