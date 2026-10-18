# Stage 0 Lean Launch: Phase 0 audit

*Read-only audit, 2026-10-19. Nothing in any repository was changed by it.*

## Summary

1. **Storage.** There is one S3 bucket, reached through five boto3 clients, none of which sets an `endpoint_url`. The database stores **keys**, not URLs, in every file column, with three exceptions: published club-site HTML, legacy `*_url` entries in template configs, and a leading slash on organisation logos. No ACLs, presigned POSTs or bucket configuration are set from code. Two parts of today's S3 setup do not carry over to R2:
   - the backup uploader's SSE/KMS headers, which R2 marks as unimplemented;
   - the backup bucket's versioning and its no-delete IAM policy.
2. **Clients and Workers.** No client talks to S3 directly. The media Worker's SigV4 signing is hard-wired to `*.amazonaws.com`, region `us-east-1`. An R2 public bucket on `media.myotherapp.in` can replace it, but only if keys keep their current paths.
3. **Rendering speed.** Measured on an Apple M2, the layout renderer (reportlab + qrcode) draws a ticket, certificate or ID card in **7–12 ms** without artwork, **about 30 ms** with a 54 KB artwork and **about 260–270 ms** with a 1.3 MB one. The wkhtmltopdf path, used for HTML designs and receipts, could not be measured (it is not installed locally and Docker did not respond).
4. **When rendering happens.** Tickets are rendered at registration, because the ticket email attaches the PDF. Certificates and ID cards are already rendered on demand. Ticket and certificate PDFs are cached under `docs/{kind}/{registration}/…`. Render threads are capped per process (3), not globally.
5. **QR codes.** The QR is the plain ticket code `MOA-XXXXX-XXXXX`; certificates use `certificate:<ticket code>`. Every scanner sends the raw string to the server, which also accepts a registration UUID. Wallet passes, printed ID cards, emailed QRs and saved PDFs carry the plain code for good: no pass can be updated.
6. **Search.** Postgres trigram search over a trigger-maintained `search_text` column is already the default. Meilisearch is optional, behind `APP_SEARCH_URL`, with an outbox and a circuit breaker. There is no tsvector anywhere and no `SEARCH_BACKEND` setting.
7. **Live rooms** use SSE, not WebSockets, and are off by default. Fan-out goes through Redis pub/sub, or an in-process broker when Redis is absent. With more than one worker and no Redis, a change reaches other processes' streams only through the 5 s resync.
8. **Hypotheses.**
   - (a) through (h) are **confirmed**. For (g), every existing piece it names is there; the real gaps are listed in section 6.
   - (i) is **partly** confirmed: the media Worker becomes unnecessary only if its duties move into Cloudflare configuration and keys keep their paths.
   - The arm64 image builds with nothing compiled from source, but wkhtmltopdf is gone from Debian after bookworm.
9. **Most dangerous steps as the brief words them:**
   - Moving the compose file to `deploy/` renames the compose project, so Postgres would start on a **new, empty volume**.
   - PgBouncer in transaction mode breaks 14 session-level advisory-lock call sites and the `plan_cache_mode` startup parameter.
   - A Cloudflare proxy without real-IP handling collapses every rate limit onto Cloudflare's addresses.
   - Firewalling 80/443 to Cloudflare cuts off the other projects' vhosts.
10. **Decisions for the owner** (section 7): attach the ticket or link to it; the QR transition; the public/private bucket split; where backups live; one box or two (and which CPU architecture); staging on the production box.

**Snapshots read:**

| Repository | Commit |
|---|---|
| backend `master` | `c16c93f` |
| website `origin/main` | `cc50a00` |
| dashboards `origin/main` | `78ce714` |
| app and Workers `origin/main` | `190a2b4` |

**Path conventions:**
- Paths starting with `app/`, `scripts/`, `tests/` or `migrations/` are under `backend/`.
- A module path with no top folder (for example `clubs/models.py`) is under `backend/app/api/`.
- Anything else is from the repository root (`backend/Dockerfile`, `docker-compose.prod.yml`, `docs/OPERATIONS.md`).
- Other repositories are prefixed `site:`, `web:` and `app:`.
- "Unknown" means it could not be verified from code: production state is not readable from here.

---

## 1. Storage

### 1.1 Every place that touches files

**Clients.** Every client is `boto3.client("s3", aws_access_key_id, aws_secret_access_key)`, with no `endpoint_url`, no `region_name` (except backups), no `Config` and no addressing style. boto3/botocore are 1.36.26 (`requirements-base.txt:9-10`).

| Client | file:line | Used for |
|---|---|---|
| `_SIGNER` (created at import) | `app/core/storage/fields.py:35` | presigning every image or file column; deletes |
| `S3ImageField.s3_client` | `app/core/storage/fields.py:411` | image uploads: the original plus thumbnail, medium and large variants |
| `S3FileField.s3_client` | `app/core/storage/fields.py:539` | file uploads |
| `objects.client()` (lru_cache) | `app/core/storage/objects.py:32` | `put_bytes`, `presigned_url`, `fetch`/`get_bytes` |
| backups | `app/api/backups/offsite.py:90` | off-site dumps; the only client given a region (`APP_BACKUP_S3_REGION`, default `ap-south-1`) |

**Operations.**

| Operation | Where | Notes |
|---|---|---|
| `upload_fileobj` | `app/core/storage/objects.py:40` (ContentType); `app/core/storage/fields.py:427` (ContentType, original + variants); `app/core/storage/fields.py:590` | `app/core/storage/fields.py:590` sends **no ContentType** (event files) |
| `download_fileobj` | `app/core/storage/objects.py:174` | Byte LRU (`APP_RENDER_IMAGE_CACHE_MB` 64) and a 300 s "missing" cache. `_not_found` treats `404`, `NoSuchKey` and `NotFound` as missing (`app/core/storage/objects.py:141`). |
| `delete_object` | `app/core/storage/fields.py:306` (image + variants), `app/core/storage/fields.py:335` (file) | Called only from `clubs/service.py:152,1054,1205,1217,1314` and `users/service.py:1061,1092`. Posters, thread images, template art, designer uploads, `docs/` and close-out PDFs are never deleted (orphans). |
| `generate_presigned_url("get_object")` | `app/core/storage/fields.py:81` (`signed_url`); `app/core/storage/objects.py:48` | 3600 s for private files; `APP_PUBLIC_IMAGE_URL_TTL` (604800 s, clamped to 3600–604800, `app/config.py:287,296`) for public ones. Cached for half their life. **No presigned PUT or POST anywhere.** |
| backups | `backups/offsite.py:148` (`upload_file`, managed multipart), `:171` (`list_objects_v2`), `:211-212` (`head_object`, `download_file`) | Separate bucket `APP_BACKUP_S3_BUCKET` |

**URL building.**
- `app/core/storage/media.py:110-133`: `public_url` gives `{APP_MEDIA_CDN_URL}/{key}` when the key is in `PUBLIC_FOLDERS` (`app/core/storage/media.py:49-58`), and a week-signed link otherwise.
- `app/core/storage/fields.py:486-519`: column values are signed on every read.
- Other builders:
  - `objects.public_url`/`photo_url` (`app/core/storage/objects.py:53,61`)
  - `templates_images.py:93`
  - `event_designs/router.py:661,878`
  - `emails/service.py:562`
  - `agenda/service.py:53-54`
  - `merch/service.py:35,202,226`
  - `store/service.py:107,217`
  - the club-site image route `sites/router.py:119-134` (302 to the CDN, or to a signed link)
  - `sites/render.py:57-67`

**Key parsing.** `app/core/storage/bucket.py:22-58` (`key_from_url`) recognises **only `*.amazonaws.com`**, virtual-hosted or path style, plus the `APP_MEDIA_CDN_URL` host (`app/core/storage/media.py:136-159`). It is used by:
- `events/documents.py:326-353` (`fetch_bytes`, which falls back to an HTTPS GET);
- the doc-cache fingerprints (`events/tickets.py:144`, `events/certificates.py:123`);
- legacy template configs (`templates_images.py:58`).

**Hosts and names.**
- No bucket name is hard-coded anywhere.
- `amazonaws.com` appears only in that parser and in the SNS hook check (`app/api/hooks/sns.py:20-21`).
- There is no CloudFront.
- The tests use `APP_S3_BASE_PATH=https://test-bucket.s3.amazonaws.com` (`tests/conftest.py:25`).

**Worker and scripts.**
- `app/worker.py` makes no storage calls of its own.
- Its handlers that do:
  - `emails.send_message` (`emails/service.py:947`): inline images and PDFs;
  - `deliveries.send` (`deliveries/service.py:528`): ticket and certificate PDFs through the doc cache;
  - the close-out PDF (`closeout/service.py:138,168`).
- `scripts/backup.py:78` and `scripts/restore_drill.py:127-136` use the backup bucket.
- `scripts/republish_sites.py` re-renders site HTML and touches no storage.

**Clients outside the backend.** None uploads to storage, downloads from it, or works out a key from a URL. All uploads are multipart to the API: the dashboards through `web:packages/auth/src/gateway.ts:74-81`; the app uploads nothing; the site uploads nothing.

Host assumptions:

| Where | What | Matters for R2? |
|---|---|---|
| `site:next.config.mjs:24-31` | `remotePatterns` lists only `*.s3.amazonaws.com` | No: the site never uses `next/image` |
| `web:apps/clubs/next.config.ts:12-18` (admin the same) | `**.amazonaws.com`, `**.myotherapp.in` | No: every remote `<Image>` is `unoptimized` |
| CSP | none on the site or dashboards; club-sites Worker `img-src https: data:` (`app:infra/cloudflare/club-sites/worker.js:45-55`; backend `sites/render.py:439-440`) | No |
| `site:src/seo/media.js:78-112`, `site:scripts/check-seo.mjs:83,167` | reads SigV4 `X-Amz-Date`/`X-Amz-Expires`, greps for signatures | Same parameters on R2 |
| `app:lib/shared/utils/image_url.dart:11-26` | Flutter accepts any http(s) host | No |

The one direct browser read: the designer runs `fetch(templateUrl)` as a blob (`web:apps/clubs/src/features/designer/components/layer-designer.tsx:642`). That needs CORS on the **private** bucket for the clubs origin. Today's S3 CORS configuration is unknown.

### 1.2 Keys or URLs in the database (per column)

Keys are written as `{APP_S3_BASE_PATH}/{folder}/…`. Production's base path is unknown; `app:infra/cloudflare/media/wrangler.toml` (`S3_BASE_PATH=""`) and `sites/router.py:28` (`IMAGE_PREFIXES=("clubs/",)`) both imply it is empty. Image variants are separate objects beside the original (`app/core/storage/fields.py:267-271`). `has_variants` (`app/core/storage/fields.py:471`) allows legacy rows holding a full URL; how many exist is unknown. No migration rewrites a file column.

| model.column | file:line | Key or URL | Example |
|---|---|---|---|
| `Clubs.logo`, `Clubs.cover` | `clubs/models.py:31,46` | key | `clubs/logos/v-<32hex>.png`, plus `.thumbnail/.medium/.large` variants |
| `ClubTeamMembers.photo`, `ClubPartners.logo` | `clubs/models.py:263,280` | key | `clubs/team/v-<hex>.jpeg` |
| `Events.poster` | `events/models.py:52` | key, stored in the **club-logo folder** | `clubs/logos/v-<hex>.jpeg` |
| `Events.images` (ARRAY) | `events/models.py:64` | whatever the client sent; format unknown | — |
| `EventSpeakers.photo` | `events/models.py:316` | key | `events/speakers/v-<hex>.png` |
| `EventFiles.file` | `events/models.py:345` | key | `events/files/<uuid4>.pdf` (no writer found) |
| `ClubThreads.image` | `threads/models.py:19` | key | `clubs/threads/v-<hex>.jpeg` |
| `UserAvatars.image` | `users/models.py:68` | key | `avatars/v-<hex>.png` |
| `UserProfiles.profile_pic` | `users/models.py:198` | key (private) | `users/profile_pic/v-<hex>.jpeg` |
| `Organizations.logo` | `orgs/models.py:31` (`upload_to="/organizations/logos/"`, `:33`) | key with a **leading slash** | `/organizations/logos/v-<hex>.png` |
| Ticket and certificate templates: `logo`, `secondary_logo`, `background_image`, `signature_1/2` | `ticket_templates/models.py:50-58`, `certificate_templates/models.py:50-66` | key, no variants | `templates/tickets/logos/v-<hex>.png` |
| `…Templates.config` (JSON) | `ticket_templates/models.py:64`, `certificate_templates/models.py:72` | `{field}_key` keys; `layout.canvas.templateKey`; `layers[].imageKey`. **Legacy rows may still hold `{field}_url` amazonaws presigned URLs.** | `{"logo_key":"templates/…","layout":{"canvas":{"templateKey":"events/7/layout/template-<hex>.png"}}}` |
| `BadgeTemplates.config`, `AgendaTemplates.config` | `idcards/models.py:44`, `agenda/models.py:51` | layout JSON with keys | as above |
| `AgendaItems.speaker_photo_key` | `agenda/models.py:33` | key from the client, not checked | `events/7/layout/image-<hex>.jpg` |
| `MerchItems.photo_key`, `EventStoreItems.photo_key` | `merch/models.py:26`, `store/models.py:21` | key | `events/7/merch/v-<hex>.jpg` |
| `CloseoutReports.pdf_key` | `closeout/models.py:62` | key | `events/7/closeout/<hex>.pdf` |
| `EmailTemplates.design`, `Messages.design` | `emails/models.py:42,100` | `blocks[].image_key` | `events/7/layout/email-<hex>.png`, `clubs/3/email/email-<hex>.png` |
| **`ClubSites.html` (Text)** | `sites/models.py:36` | **full URLs baked in**, including `og:image`: `https://media.myotherapp.in/<key>` or `{PUBLIC_API_URL}/api/v1/sites/{slug}/i?k=<key>` | written by `sites/service.py:163`, `sites/render.py:57-67,363` |

API responses carry signed URLs made at read time (an `S3Image` dict per size, or an `S3File` string), never stored URLs.

The public prefixes are `clubs/{logos,covers,team,partners,threads}/`, `events/speakers/`, `avatars/` and `events/{id}/merch/` (`app/core/storage/media.py:49-58`, mirrored in `app:infra/cloudflare/media/media.js:19-50`). Everything else is private.

**`events/{id}/layout/` mixes private designer art, email images and public agenda speaker photos**, so splitting buckets by prefix cannot separate them without moving the speaker photos.

### 1.3 Generated files

| What | Key | Visibility | How clients get it |
|---|---|---|---|
| Ticket and certificate PDF cache | `docs/{ticket\|certificate}/{registration_id}/{design_version}-{fingerprint24}.pdf` (`app/api/events/doc_cache.py:67`) | private | Streamed by the API: `events/router.py:600,762`, the email link `/api/v1/files/{tickets\|certificates}/{ticket_id}.pdf?s=<hmac>` (`emails/router.py:285-305`), and the ZIPs (`deliveries/router.py:78`, `certificates/router.py:87`) |
| Designer, email and agenda art | `events/{id}/layout/`, `clubs/{id}/layout/`, `clubs/{id}/email/` | private (speaker photos handed out week-signed) | 1 h signed links; renders embed them as `data:` URIs (`templates_images.py:103`) |
| Close-out report | `events/{id}/closeout/` | private | streamed (`closeout/router.py:39`) |
| Backups | separate bucket, `backups/{daily\|weekly}/backup-YYYYMMDD-HHMMSS.dump` (`backups/offsite.py:78`) | private | restore drill or the aws CLI |
| Wallet passes, ID cards, agenda and kiosk PDFs, CSVs, receipts | **not stored**; made per request | — | streamed (`events/router.py:620`, `idcards/router.py:38,54`, …) |

### 1.4 S3 features the code uses, against R2

| Feature | Where | On R2 |
|---|---|---|
| `ServerSideEncryption` `AES256` / `aws:kms` + `SSEKMSKeyId` | `app/api/backups/offsite.py:98-108,153` | **❌ in R2's S3 compatibility table, for PutObject and CreateMultipartUpload.** R2 encrypts at rest regardless. Off-site backups would fail until these args are dropped for R2. |
| `x-amz-meta-sha256`, `tier` (written, then read via HEAD) | `backups/offsite.py:155,213` | supported |
| `ContentType` | `app/core/storage/objects.py:41`, `app/core/storage/fields.py:431`, `backups/offsite.py:154` | supported |
| ACL, tagging, storage class, website redirect, presigned POST | not used | (ACL, tagging and website redirect would be ❌) |
| Bucket CORS, lifecycle, versioning or website set from code | none | — |
| Lifecycle rules (daily 14 d, weekly 56 d) | set by hand (`docs/OPERATIONS.md:337-359`) | PutBucketLifecycleConfiguration ✅ |
| Versioning, and an IAM policy that cannot delete backups | `docs/OPERATIONS.md:337-375` | **PutBucketVersioning ❌.** R2 API tokens are per bucket, not per prefix, and read/write includes delete. The "a leaked key cannot destroy backups" property is lost (R2 bucket locks may replace it; unverified). |
| Region | unset (boto default / env) for media; `ap-south-1` for backups | R2 wants `auto`. Empty or `us-east-1` alias to it; **`ap-south-1` does not.** Every client also needs `endpoint_url=https://<account>.r2.cloudflarestorage.com`. |
| botocore 1.36 default CRC32 checksums | every upload | Probably fine. Smoke-test; the fallback is `request_checksum_calculation="when_required"`. |
| Presign expiry up to 7 days | `app/config.py:287-301` | same 7-day ceiling |
| Media Worker SigV4 | `app:infra/cloudflare/media/sigv4.js:52-89`: service `s3`, `S3_REGION="us-east-1"` (`app:infra/cloudflare/media/wrangler.toml:28`), host `{bucket}.s3.{region}.amazonaws.com` or path style for dotted names (`sigv4.js:79-84`); its IAM key can read only the public folders | Hard-wired to AWS hosts. An R2 public bucket on a custom domain makes the signing unnecessary (see 6(i)). |

### 1.5 Links already out there

| Where | What it holds | After a storage move |
|---|---|---|
| Published club sites (`ClubSites.html`) | `https://media.myotherapp.in/<key>` or `/api/v1/sites/{slug}/i?k=<key>` | Survive only if `media.myotherapp.in/<key>` and `/i` keep resolving the same keys. `scripts/republish_sites.py` and the nightly sweep re-render the HTML. |
| Emails | Images inline by CID (`emails/service.py:513-528`); PDFs attached as bytes; links point at the API (`app/core/mail/links.py:27-29`) | Unaffected |
| Ticket and certificate links in emails | `/api/v1/files/tickets/{id}.pdf?s=<hmac>`, with **no expiry** (HMAC of `APP_SECRET_KEY`, `app/core/mail/links.py:20-30`) | Unaffected by storage. They must keep answering 200 with a PDF (6(f)). Rotating `APP_SECRET_KEY` breaks every one already sent. |
| Wallet `.pkpass` | No URLs: the poster is embedded as `strip.png`; barcode = ticket_id; no `webServiceURL` (`events/wallet_service.py:108-179`) | Unaffected by storage. Passes can never be updated (QR, section 2). |
| Push notifications | `image_url` as sent (`clubs/notifications_router.py:41`, `notifications/router.py:276`); the inbox stores only `has_image` (`app/core/notifications/triggers.py:373`) | Low risk (the app downloads it on receipt) |
| API responses already in apps and browsers | Week-signed `https://{bucket}.s3.amazonaws.com/…` links | Break only if the S3 objects are deleted before they expire (7 days) |
| App image cache | `CachedNetworkImage`, keyed by URL without its query (`app:lib/shared/widgets/event_poster_image.dart:61-68,172`) | A host change means cache misses and re-downloads, nothing worse. No Hive, prefs or sqflite holds API data. |
| Site OG previews | `site:src/seo/urls.js:73-76` fingerprints the image without its query; `site:src/app/media/[...path]/route.jsx:49-73` 307-redirects to whatever the API returns | One harmless cache-bust |

**Coupling to fix in the move.** `key_from_url` knows only AWS hosts and the CDN host. With R2 presigned hosts it returns `None`. Wallet posters, email logos and icons, ID-card icons and ticket fingerprints would then fall back to HTTPS fetches, and because a fingerprint built from a re-signed URL changes every time, the doc cache would keep missing.

---

## 2. Rendering

### 2.1 Libraries

| Engine | Library (pin) | Draws | Where |
|---|---|---|---|
| Layout renderer (designs made in the layer editor) | reportlab 5.0.1 + fontTools 4.66.1; qrcode 7.4.2 (vector modules, level M) | Ticket, certificate and ID-card PDFs, agenda | `app/core/layout/renderer.py:1-60,349-372`, `app/core/layout/fonts.py`, `app/core/layout/engine.py` |
| PNG preview of a PDF | pypdfium2 5.14.0 (behind a process-wide lock, `pool.PDFIUM`) + Pillow 12.3.0 | Ticket and certificate images, previews | `app/core/layout/renderer.py:375-399`, `app/core/layout/pool.py:70-96` |
| HTML templates (designs not in layout mode, `events/documents.py:195-197`) | wkhtmltopdf / wkhtmltoimage (Debian bookworm 0.12.6) via pdfkit 1.0.0, in a subprocess with `RENDER_TIMEOUT` 30 s; jinja2 | Ticket and certificate PDFs for HTML designs, their PNGs, **payment receipts** | `app/core/utils/pdf.py:29-191`; callers `events/tickets.py:165`, `events/certificates.py:131`, `payments/background_tasks.py:23`, `events/router.py:574,753`, `event_designs/router.py:585` |
| QR as PNG | qrcode + Pillow (level L) | HTML-template QRs, email QR | `app/api/events/documents.py:96-113` |
| Wallet pass | Pillow (strip image) + `openssl smime` subprocess | `.pkpass` | `app/api/events/wallet_service.py:201-238,314,331-409` |
| Email HTML | own renderer, in a thread | Emails | `app/core/mail/render.py`, `emails/service.py:513-570` |

All renders run on a dedicated pool of `APP_RENDER_THREADS` (default **3**) threads **per process** (`app/core/layout/pool.py:36-54`, `app/config.py:255`), never on the event loop. With `APP_WORKERS=3` plus the worker, that is up to 12 concurrent renders on the box. Nothing caps renders globally.

### 2.2 What triggers a render

| Trigger | Renders | Sync or async | file:line |
|---|---|---|---|
| Free registration (or payment captured) | **Ticket PDF**, because the ticket email attaches it | Job `deliveries.send` (`concurrency=2` per runner) after the commit | `events/registration/service.py:452-459` → `deliveries/service.py:614-623` → `:400-404` (fallback copy) or `emails.compose_invite` → `emails/service.py:533-541` |
| Ticket email design | `attachments.ticketPdf: true` in the starter; **stored per template** (`email_templates.attachments`, `emails/models.py:43`), copied into each event | — | `app/core/mail/starters.py:67-73` |
| Ticket email link | "Download your ticket" `{{ticket_url}}` already exists in the same starter | rendered on GET (cache hit after the first) | `app/core/mail/starters.py:70`, `emails/service.py:439-440`, `emails/router.py:285-305` |
| Ticket download / image | PDF; PNG from the cached PDF | Synchronous in the request, on the render pool | `events/router.py:600-618`, `:538-575` |
| Bundle ZIP of every ticket | every ticket, one after another, in **one request** | synchronous | `deliveries/service.py:672-692`, `deliveries/router.py:78` |
| Certificate issue | **nothing**: it only sets `certificate_issued_at` | — | `certificates/service.py:117-119` |
| Certificate send / download / ZIP | Certificate PDF (cached) | job `deliveries.send` / request | `deliveries/service.py:262-267`, `events/router.py:762-782`, `certificates/router.py:87` |
| ID card (check-in, console, batch) | Card PDF, **not cached**; batch = one PDF of every card in one request | synchronous on the pool | `idcards/router.py:38-63`, `idcards/service.py:76-87,173` |
| Wallet pass | `.pkpass`, **not cached** | synchronous on the pool | `events/router.py:620-640`, `events/wallet_service.py:378-409` |
| Payment receipt | wkhtmltopdf PDF, emailed | job `payments.receipt` | `payments/service.py:432,440`, `payments/background_tasks.py:23-39` |
| Templated emails with a QR | QR PNG + email HTML | job `emails.send_message` (`concurrency=2`) | `emails/service.py:520-530,947` |

**Doc cache** (`app/api/events/doc_cache.py`):
- Key: `docs/{kind}/{registration_id}/{design_version}-{fingerprint}.pdf`. The fingerprint is a SHA-256 (24 hex) of `RENDER_VERSION` ("2026-10-18.1"), the printed values, the design minus volatile `_url`s, and the template file's hash (`:35-68`).
- A hit is a bucket read. A miss draws on the pool and writes back only when every image loaded (`:84-94`).
- **No single-flight:** two concurrent first downloads both render.
- Nothing is ever invalidated or deleted; superseded files stay in the bucket.
- Switched by `APP_RENDER_DOC_CACHE` (default on).

### 2.3 Measured render times

Apple M2, Python 3.11.11, the pinned reportlab/Pillow/pypdfium2/qrcode. Pure renders: no database, no bucket. 30 warm runs (15 with artwork). Script: `render_bench.py` in the session scratchpad, not committed.

| Render | Cold (first in process) | Warm p50 | Warm p95 | Size |
|---|---:|---:|---:|---:|
| Ticket PDF, starter `classic` | 39.0 ms | **6.8 ms** | 6.8 ms | 57 KB |
| Ticket PDF, other starters (`invitation`, `minimal`, `stub`), with logo and icon | 7.8–9.7 ms | 7.1–9.1 ms | 7.2–9.2 ms | 38–68 KB |
| Certificate PDF, starter `classic` | 20.0 ms | **9.0 ms** | 9.6 ms | 63 KB |
| Certificate PDF, with logo and icon | 13.4 ms | 12.4 ms | 12.6 ms | 64 KB |
| ID card PDF, badge starter `classic` | 9.3 ms | **7.4 ms** | 8.0 ms | 57 KB |
| ID card PDF, with logo and icon | 11.3 ms | 10.6 ms | 11.7 ms | 58 KB |
| Any of the three with a 2000×1100 artwork, 54 KB JPEG | 34–62 ms | **30–32 ms** | 32–33 ms | 125–131 KB |
| Any of the three with a 2000×1100 artwork, 1.36 MB JPEG (high-entropy) | 259–277 ms | **261–272 ms** | 268–300 ms | 1.76 MB |
| Ticket PNG 1000 px from the PDF (pdfium + Pillow) | 340 ms (pdfium load) | 16.4 ms | 17.3 ms | 25 KB |
| Certificate PNG 1600 px | 48.9 ms | 44.7 ms | 46.9 ms | 62 KB |
| QR PNG (`qr_png_base64`) | 2.3 ms | 1.5 ms | 1.5 ms | 0.6 KB |

What the numbers say:
- Artwork size dominates. Embedding the image drives the cost, and the PDF grows with it.
- **Not measured:**
  - the **wkhtmltopdf** path (HTML designs, receipts): not installed locally, and the local Docker daemon did not answer. `docs/SCALING.md:103-107` assumed "a few hundred milliseconds" and also never measured it.
  - production's t2.medium: burstable Xeon, CPU-throttled to about 60% pressure on 2026-10-18 (`docs/OPERATIONS.md:726-729`). Expect several times these numbers there.
  - the share of designs in layout mode against HTML: unknown (`ticket_templates.config->'layout'->'layers'`).
- Not included: the bucket read and write around a render (doc cache, artwork fetch). Those are network calls to us-east-1.

### 2.4 The job queue today (`app/core/jobs.py`)

- **Storage:** Postgres table `jobs`.
  - Claim: `FOR UPDATE SKIP LOCKED` over `ix_jobs_claim (name, priority, run_at, id) WHERE unfinished` (`:132,451-485`).
  - Lease 120 s, renewed by a 15 s heartbeat (`:97-104`). A dead runner's job is retaken when its lease ends.
- **Priorities:** `PRIORITIES = (2,1,0,-1,-2)`, walked highest first per name (`:114`). **No caller passes a priority** (`git grep "priority="` finds only FCM's `"high"`, `app/core/notifications/service.py:294`), so priorities exist but are unused.
- **Concurrency:**
  - `APP_JOBS_CONCURRENCY` jobs per runner: default 4; 2 on the worker, set in `docker-compose.prod.yml:188`.
  - A per-name cap per runner: `handler(concurrency=)` or `APP_JOBS_LIMITS="name=n,…"` (`:209-255`).
  - Capped today: `deliveries.send=2`, `emails.send_message=2`, `registrations.import=1`.
  - Caps are **per runner**. With `APP_JOBS_WORKER` unset, each of the 3 API processes runs its own runner, so `deliveries.send` can run 6 at once.
- **Retries:** `max_attempts` (default 6). Backoff is 30 s × 2^(n−1) with jitter, capped at 3600 s. A job that runs out of attempts is marked dead, kept 30 days, and raises a `job_dead` admin alert at most once per name per 10 minutes. Settling is fenced by the attempt number (`:92,513-545,611`).
- **Wake-up:**
  - an in-process `asyncio.Event` on commit (`:353-395`);
  - with `APP_JOBS_WORKER`, a Redis publish on `jobs:wake` (`:397-410`);
  - otherwise a poll every `APP_JOBS_POLL_SECONDS` (1.0 s).
  - **No LISTEN/NOTIFY.**
- **Registered handlers:** 19, with timeouts of 60–3600 s (`git grep "@jobs.handler"`).

### 2.5 What the QR encodes, and who reads it

| Document | QR payload | file:line |
|---|---|---|
| Ticket (layout and HTML), email QR, ID card, ticket image | `ticket_id` = `MOA-XXXXX-XXXXX` (31-character alphabet, CSPRNG); legacy `MOA-<hex time>-XXXXX` | `app/core/utils/keys.py:7-34`; `events/tickets.py:122,159`; `idcards/service.py:79`; `emails/service.py:567`; `events/router.py:570` |
| Certificate | `certificate:<ticket_id>` (the printed number is `CERT-…`) | `events/certificates.py:30-49` |
| Apple Wallet | `barcodes[0].message = ticket_id`, no `webServiceURL` | `events/wallet_service.py:121-125` |
| App fallback ticket card | `widget.ticketId` (qr_flutter); the id is parsed out of `ticket_url` | `app:lib/features/events/presentation/pages/ticket_view_page.dart:966-969`, `individual_event_page.dart:1956-1963` |
| Site ticket page, registration success | `ticketid` / `ticket.ticket_id`, level H, 132–150 px. The site's "download" is a client-side html2canvas + jsPDF capture. | `site:src/components/ticket.jsx:123`, `site:src/components/register/success.jsx:120`, `site:src/components/download.jsx:13-22` |
| Site certificate | `certificate:${ticketid}`, 80 px | `site:src/components/certificate.jsx:194` |

**Scanners.** None of them parses the code; each sends the raw string, at most trimmed.

| Scanner | Decoder | Sends to | Offline |
|---|---|---|---|
| App volunteer | mobile_scanner `rawValue`, untrimmed (`app:lib/features/volunteer/presentation/pages/volunteer_scanner_page.dart:60-80`) | `POST /events/volunteer/validate/{id}`, **then** `/checkin/{id}` with the same value | none |
| App manual entry | trimmed; keyboard in capitals (`manual_ticket_entry_page.dart:54,148`) | checkin | none |
| Clubs gate | BarcodeDetector, falling back to qr-scanner; `raw.trim()`; the same code ignored for 2.5 s (`web:apps/clubs/src/features/gate/lib/scanner.ts:12-71`, `scan-stage.tsx:62-66`) | `POST /api/v1/events/{id}/gate/scan {code, door, scanned_at, client_ref}` | Queued raw in IndexedDB and replayed to `/gate/scan-batch` (`gate/lib/queue.ts`, `queries.ts:36-47,79-119`); no local guest list |
| Clubs kiosk | same scanner, front camera (`kiosk-app.tsx:296-332`) | `POST /api/v1/kiosk/redeem {code}` | none |
| Clubs merch | the scanned code used as a search query (`merch-panel.tsx:75-105`) | `GET /gate/search?q=` (ilike, at most 100 characters) | none |

**Server side.** Every path goes through `checkins.service._code_filters` (`app/api/checkins/service.py:51-70`):
1. strip;
2. try `uuid.UUID(code)` as the registration id;
3. otherwise `ticket_id == code.upper()`, and the code as typed if it differs.

Admission numbers are refused for admission (`:89-99`). A hit on another event gives `WRONG_EVENT` (`:101-111`). Length limits: `ScanBody.code` 4–128 (`checkins/schemas.py:145-150`), `RedeemBody.code` 4–128 (`kiosk/schemas.py:63-65`), volunteer `ticket_id` unlimited. The public certificate check is `GET /api/v1/verify/certificates/{code}`, limited to 60 a minute per address (`certificates/router.py:28,119-120`).

### 2.6 If the QR became a signed token

- **Already issued and frozen:** Wallet passes (no update URL), printed ID cards, emailed QRs, saved PDFs and screenshots all carry the plain `ticket_id`. The server must keep accepting it for those events, indefinitely or until the event ends.
- **Clients that draw the QR themselves keep drawing `ticket_id`:** the app's fallback card and the site's ticket and success pages. The API has to expose a token field in ticket details, My tickets, the registration response and event details. The app also uses `ticket_id` for the PDF, image and pkpass URLs, so the token cannot replace it there.
- **Server parsing:** verify a token before `.upper()` and the UUID attempt. Tokens over 128 characters get a 422 at the gate and kiosk; over 100, merch search fails, and search would never match a token anyway.
- **Single-use tokens** break the app's validate-then-check-in flow, which sends the same code twice. **Expiring tokens** fail offline gate scans replayed later, unless they are judged at `scanned_at`.
- **Typed entry** is upper-cased on mobile, so a case-sensitive token cannot be typed. Keep the human code for typing.
- **QR density:** a long token at level H in 80–150 px (site) gets hard for the kiosk's front camera to read.
- **Doc cache:** the token is printed, so it must enter the fingerprint (`events/tickets.py:144,168`). If tokens rotate, every cached ticket is re-rendered.

---

## 3. Search

**On/off.** Meilisearch is on only when `APP_SEARCH_URL` is set (`app/core/search.py:59-60`, `app/config.py:230`) **and** the `search-build` row in `scheduled_runs` shows a finished build (`search/sync.py:100-107`). There is no `SEARCH_BACKEND`. Settings (`app/config.py:220-236`):

| Setting | Default |
|---|---|
| `APP_SEARCH_URL` / `APP_SEARCH_KEY` | `""` / `""` |
| `APP_SEARCH_TIMEOUT` | 0.8 s |
| `APP_SEARCH_BREAKER_FAILURES` / `_SECONDS` | 3 / 30 s |
| `APP_SEARCH_INDEX_PREFIX` | `moa_` |
| `APP_SEARCH_RECONCILE_HOURS` | 6 |

Whether production sets any of these is unknown; Wave 2 is being deployed with Meilisearch not running.

**Meilisearch:**

| Aspect | Today |
|---|---|
| Client | One `httpx.AsyncClient` per process: 0.8 s timeout, 0.5 s connect, 50 connections (`app/core/search.py:141-155`) |
| Indexes | `moa_events`, `moa_clubs`; primary key `id` (`app/core/search.py:36-38,255-263`) |
| Event document | `id, name, location, tags, club_name, club_location, category, interests, topics, club_id, category_id, topic_ids, interest_ids, city, is_online, has_fee, fee, has_point, starts_at/ends_at/created_at` (µs), `lifecycle, listed, popularity, _geo, h` (`search/docs.py:144-157`) |
| Club document | `id, name, tagline, location, interests, topics, topic_ids, interest_ids, city, listed, follower_count, name_sort, created_at, has_place, _geo, h` (`search/docs.py:174-184`) |
| Searchable order | Events: name > club_name > interests > topics > category > tags > location > club_location (`app/core/search.py:385`). Clubs: name > tagline > interests > topics > location (`app/core/search.py:396`). |
| Ranking | `sort, words, typo, proximity, attribute, exactness` (`app/core/search.py:372`). Typos: one from 5 letters, two from 9 (`app/core/search.py:373`). `maxTotalHits` 1200. 40 synonym groups, mostly Indian cities (`app/core/search.py:313-362`). 16 stop words. `matchingStrategy: all` (`search/engine.py:180-185`). |
| Outbox | `search_outbox(id, kind, ref_id, created_at)`. Written by **database triggers** on events, clubs, categories, interests and links (`search/outbox.py:84-133`, `search/text.py:157-183`). |
| Drainer | Leader loop under advisory lock `773_140`: 500 rows a pass, at most 20 s a tick (`search/sync.py:53-69,164-217,353`). Retries the whole batch with backoff up to 60 s; no dead letter. Reconciles every 6 h (`search/sync.py:284-344`). Rebuild: `{index}__build`, then swap (`search/sync.py:229-265`). |
| Breaker | 3 failures open it for 30 s, then half-open with one trial at a time (`app/core/search.py:71-130`). Every engine call returns `None` on failure, and the caller runs Postgres (`search/engine.py:233-235,291-293,345-347,383-385`). Pages are always re-read from Postgres (`search/engine.py:238-243`). |

**Postgres** (the default):

| Aspect | Today |
|---|---|
| Column | `search_text` on `events`, `clubs` and `users` (migration `w2_search`, `migrations/versions/w2_search.py:65-71,483-484`), kept by BEFORE and AFTER triggers. Normalised by `moa_search_norm` (lower + unaccent + whitespace), which is STABLE (`search/text.py:57-64,197-223`). |
| Contents | Events: name, location, tags, club name and location, category, interests, topics. Clubs: name, tagline, location, interests, topics. Users: name, username, email. **Not the event description or the club's about text.** |
| Indexes | GIN `gin_trgm_ops`: `ix_events_search_trgm`, `ix_events_shown_search_trgm`, `ix_clubs_search_trgm`, `ix_users_search_trgm`, all built CONCURRENTLY (`migrations/versions/w2_search.py:352-357,504-505`). Plus `ix_events_name_trgm` and `ix_clubs_name_trgm` (`migrations/versions/discover_perf.py:43,46`). Extensions `pg_trgm` and `unaccent` (`app/db/base.py:11,13`). |
| Matching | Every word must match, at most 6 words. 3+ letters: `LIKE '%w%'`. 2 letters: regex `\m w \M`, which no index serves (`discover/service.py:105,144-176`). If nothing matches, a typo fallback: `search_text %> w` at 0.5 (`discover/service.py:341-391`). |
| Ranking | `greatest(word_similarity(q,name), 0.8·club, 0.6·location, 0.5·category)` on the raw columns (`discover/service.py:393-402`). Clubs have no relevance sort. |
| Measured at 1 lakh clubs (`scripts/bench/RESULTS.md:1395-1406`) | `q=robotics` (relevance) 1.55 s, `q=ai` 2.2 s, typo 0.4 s; two words or a rare word 25–36 ms. A common word got slower with `w2_search` at this scale, because `search_text` now matches more rows: relevance went from 0.78 s to 1.55 s, and the app's `/events/list?search=robotics` from 38 ms to 10.9 s (RESULTS.md blames part of that on a shared disk). Production has a few thousand events. |

**Endpoints that use search:**

| Endpoint | Engine | Callers |
|---|---|---|
| `GET /discover/events?q` (`discover/router.py:73`) | Meili, else Postgres | `site:src/api/discover.js:99`, `site:src/server/discover.js:120` |
| `GET /discover/clubs?q` (`discover/router.py:147`) | Meili, else Postgres | `site:src/api/discover.js:133`, `site:src/server/discover.js:141` |
| `GET /discover/calendar?q` (`discover/router.py:113`) | Postgres only | `site:src/api/discover.js:131` |
| `GET /events/list?search` (`events/router.py:353`) | Meili when no personal filter | `app:lib/features/events/data/datasources/event_remote_datasource.dart:115-123` |
| `GET /clubs/list?search` (`clubs/router.py:263`) | Meili when no personal filter or sort | `app:…/club_remote_datasource.dart:97-107` |
| `GET /clubs/admin/list`, `/user/admin/list`, `/user/admin/export` | Postgres | `web:apps/admin/src/features/{clubs,users}/api.ts` |
| `GET /clubs/events/list?search` | `Events.name ILIKE` | `web:apps/clubs/src/features/events/api.ts:16` |

**Missing for a weighted tsvector:**
- No tsvector column.
- No `setweight`: `search_text` is one flat `concat_ws`, so field boundaries are lost (`search/text.py:83,100`).
- No text-search configuration (`simple` against `english`) and no stemming.
- No `ts_rank`/`ts_rank_cd`, no `ts_headline`, no `:*` prefix queries.
- No GIN on a tsvector.
- `unaccent` would need an IMMUTABLE wrapper for a generated column; otherwise the column stays trigger-filled as now.
- Synonyms and stop words live only in Meilisearch.

Tests exist: `tests/test_search_text.py`, `test_search_routing.py`, `test_search_sync.py`, and an in-memory fake Meilisearch in `tests/fake_meili.py`.

---

## 4. Live rooms

**Transport.** **SSE, confirmed:**
- `GET /api/v1/events/{id}/live/stream?view=room|banner|quiz|host|screen|questions|polls` (`app/api/live/stream.py`; contract in `docs/LIVE_SSE.md`).
- No WebSocket route exists, and the image cannot serve one: there is no `websockets` or `wsproto` in the requirements; only uvicorn 0.29.0 and httptools (`requirements.txt:39`, `requirements-base.txt:21`).
- Off unless `APP_LIVE_SSE=true` (`app/config.py:450`). Clients keep their polls and fall back to them on 404.

**Fan-out:**
- `bus.changed` publishes to `moa:ch:<event>` (`app/api/live/bus.py`).
- With Redis: one `PSUBSCRIBE moa:ch:*` connection per process (`app/core/redis.py:179-206`). Versions are minted by a Redis `INCR`, floored at the clock in µs (`live/bus.py:62-72`).
- Without Redis, or while Redis is down: the in-process `_Broker` (`app/core/redis.py:225-240`).
- Audience changes are folded every 0.3 s (`COALESCE_SECONDS`); the room view sends at most one snapshot per 2 s.
- Every feed rebuilds every `RESYNC_SECONDS = 5` and re-announces if anything differs (`live/stream.py:83-94`).

**With `APP_WORKERS` > 1:**

| | Redis on | Redis off |
|---|---|---|
| A host's change reaches streams in its own process | at once | at once |
| …in the other processes | at once (pub/sub) | only at the next 5 s resync of each feed |
| `id` (Last-Event-ID) across processes | identical (Redis INCR) | per process (the clock floor): a reconnect to another process may resend a snapshot |
| Stream caps | per process: `APP_LIVE_SSE_MAX_STREAMS` 5000, 4000 per event, 3000 per address (`docs/OPERATIONS.md:1012-1016`) | same |
| Per-person live limits (`app/api/live/limits.py`) | shared in Redis | per process |

`docs/OPERATIONS.md:1022-1024` already requires Redis before `APP_LIVE_SSE` with more than one process.

**nginx requirements** (`nginx.conf:37-51,87-101`, `docs/OPERATIONS.md:933-1003`):
- a `location ~ ^/api/v1/events/\d+/live/stream$` with `proxy_buffering off`, `proxy_cache off`, `gzip off`, `proxy_read_timeout 120s`, `send_timeout 60s`, HTTP/1.1 with an empty `Connection`, and an access log without the query string (keys travel in it);
- `worker_connections 16384` and `worker_rlimit_nofile 65536`;
- the backend container's `nofile` is 65536 (`docker-compose.prod.yml:144-147`, applied on recreate).

Whether the host nginx has any of this is unknown. Behind a Cloudflare proxy, the stream pings every 15 s, inside Cloudflare's 100 s idle timeout. That Cloudflare passes `text/event-stream` through unbuffered is unverified.

**Against the brief.** It asks for "WebSocket fan-out via Redis". What exists is SSE fan-out via Redis, with a published client contract implemented in the site, app and dashboards. Switching to WebSockets would change that contract in all three clients. Recommendation: keep SSE.

---

## 5. Docker and deploy

### 5.1 What exists

| Piece | Today | file:line |
|---|---|---|
| Base image | `python:3.11-bookworm` (full Debian image, about 2.95 GB built), no USER (runs as root), no HEALTHCHECK | `backend/Dockerfile:5` |
| apt | `wkhtmltopdf` and `openssl` (with recommends); `postgresql-client-17` from PGDG `bookworm-pgdg` (pg_dump must be at least the 17.4 server) | `backend/Dockerfile:10-31` |
| pip | Two layers, `requirements-base.txt` then `requirements.txt`, with BuildKit cache mounts (BuildKit required) | `backend/Dockerfile:45-51` |
| Code | `COPY ./` but **bind-mounted over it in production** (`./backend:/backend`), so a code deploy is rsync + restart | `docker-compose.prod.yml:83-84` |
| Services | `postgres:17.4` (1280m, 2 CPU, **host port 6432 → 5432**), `backend` (2304m, 2 CPU, host ports **8000 and 8001**, nofile 65536), `worker`, `redis:7-alpine` (256m, no persistence), `getmeili/meilisearch:v1.54.3` (768m). Fixed `container_name: doth-live-*`, subnet `192.168.5.0/24`, fixed IPs. | `docker-compose.prod.yml:29-313` |
| Postgres tuning | none: no `command: -c …`, no config file. Image defaults (`shared_buffers` 128 MB, `max_connections` 100). No `shm_size`. | `docker-compose.prod.yml:36-75` |
| Deploy | `workflow_dispatch`: rsync with `--delete` (excluding `.env`) to `PROD_PROJECT_PATH` (`/var/www/myotherapp-production`). Builds **on the server** with `sudo docker-compose -f docker-compose.prod.yml` (compose v2.33.1 binary). Migrates in the serving container, recreates, prunes, then a smoke check (`/api/v1/plans`, or `vars.PROD_SMOKE_URL`). No registry, no `--platform`, no QEMU. | `.github/workflows/live-deploy.yml:101-444` |
| DEV deploy | A separate server (`DEV_REMOTE_IP`) on `docker-compose.yml`. Still runs `exec` without `-T` (the 2026-10-05 stdin bug). | `.github/workflows/dev-deploy.yml:63-141` |
| Host nginx | Not deployed by any workflow. `nginx.conf` in the repo is a reference copy: `api.myotherapp.in` → `localhost:8001`, `api.staging.myotherapp.in` → `:8000`, a leftover `pulsetap.in` subdomain rule, and the SSE locations. The compose comment says `api.myotherapp.in` → 8000 and `api.myotherapp.com` → 8001, so which one is live is unknown. TLS from certbot in `/etc/letsencrypt` on the host. | `nginx.conf:1-116`, `docker-compose.prod.yml:149-159` |
| Stale copies | `docker-compose.prod.yml`, `nginx_prod.conf`, `nginx_staging.conf`, `nginx_update.conf` in the app repo, last changed 2026-01-26 | `app:` root |

### 5.2 ARM64

Images were checked with `docker manifest inspect`, apt packages with Debian madison and the PGDG `binary-arm64/Packages` index, and wheels with `pip download --only-binary=:all: --platform manylinux…_aarch64 --python-version 3.11`, with x86_64 as a control. The full dependency resolve came to 100 packages.

| Dependency | arm64 | Note |
|---|---|---|
| `python:3.11-bookworm` (3.11.17), `postgres:17.4`, `redis:7-alpine`, `getmeili/meilisearch:v1.54.3` | prebuilt | Meilisearch publishes only amd64 and arm64 |
| `wkhtmltopdf` 0.12.6-2+b1 (Debian bookworm main, not the upstream .deb) | prebuilt | **Upstream is archived** (wkhtmltopdf since 2022, packaging since August 2023). **Debian dropped it after bookworm** (no trixie or sid package), which pins the image to bookworm on any architecture. `app/core/utils/pdf.py:67` notes an unpatched local-file-read issue. |
| `postgresql-client-17` (17.11-1.pgdg12+2) | prebuilt | PGDG publishes amd64, arm64 and ppc64el for `bookworm-pgdg`; the sources line has no `[arch=]` |
| openssl, curl, ca-certificates, gnupg | prebuilt / arch-independent | |
| 18 compiled pins (asyncpg, bcrypt, cryptography, greenlet, httptools, maxminddb, numpy, orjson, pandas, pillow, psycopg2-binary, pydantic_core, pypdfium2, SQLAlchemy, uvloop, fonttools, MarkupSafe, charset-normalizer) | manylinux aarch64 wheels | Pillow is manylinux_2_28, fine on glibc 2.36 |
| 6 compiled **unpinned** transitive packages (cffi, grpcio, protobuf, google-crc32c, msgpack, wrapt) | wheels today | They float: a future release without an aarch64 wheel would build from source (grpcio is very slow to build). Needs a lock or constraints file resolved for both platforms. |
| 55 pure-Python pins, including reportlab 5.0.1 | `py3-none-any` | |

**Nothing compiles from source on arm64 today.** Nothing in the Dockerfile, compose or workflows assumes amd64. The only `--platform linux/amd64` lines are examples in `docs/OPERATIONS.md:662,673`. A multi-arch image needs either a registry (GHCR or ECR, `docker/build-push-action` with `platforms: linux/amd64,linux/arm64`) or the containerd image store: a manifest list cannot be `--load`ed. That route is blocked while GitHub Actions billing is broken. Building natively on a Graviton box with today's server-side flow needs no change at all. The pip cache mount should be keyed per architecture (`id=pip-$TARGETARCH`) if both architectures are built on one builder.

### 5.3 Where `docker-compose.prod.yml` is referenced, and moving it to `deploy/compose.prod.yml`

References:
- `.github/workflows/live-deploy.yml:156,164,308,342`
- `tests/test_one_box.py:18` and `tests/test_deploy_workflow.py:66,233`: tests parse the file
- docstrings: `scripts/{backup,restore_drill,index_usage,search_reindex,ledger_backfill,ledger_compare,ledger_reconcile}.py`, `app/worker.py:2`, `app/config.py:61,387`
- `docs/OPERATIONS.md` (10 places, including the **host crontab lines** at `:389-390`), `docs/SCALING.md` (3), `docs/PUSH_TOPICS.md:72`, `sample.env:15`
- the compose file's own comments (`:244,248`)

What a move requires beyond editing those:

1. **The project name.** Compose names the project after the compose file's directory, so `/var/www/myotherapp-production/deploy` becomes `deploy`. The volumes `postgres_data`, `redis_data` and `meili_data` are then looked up as `deploy_postgres_data` and so on: **Postgres would start on a new, empty volume.** The network `doth_network` becomes `deploy_doth_network` with the same `192.168.5.0/24` subnet, so "Pool overlaps" while the old network exists. The fixed `container_name`s collide with the running containers.
   - **Fix:** a top-level `name: <current project>` in the file, or `-p` / `COMPOSE_PROJECT_NAME`.
   - The current project name is unknown (presumably `myotherapp-production`). Read it from `docker inspect doth-live-postgres --format '{{ index .Config.Labels "com.docker.compose.project" }}'`.
2. **Relative paths resolve from the file's directory:** `build: backend/`, `./backend:/backend`, `env_file: .env`, and the `.env` used for `${POSTGRES_*}` interpolation. Use `../backend`, `../.env`, or `--project-directory .` on every invocation, crontab included.
3. **The host crontab lines** (`docs/OPERATIONS.md:389-390`), if installed (unknown), name the old path and must change together.

---

## 6. Risks and unknowns

**(a) An nginx container in compose collides with the host nginx and the other projects' vhosts on 80/443. Confirmed.**

What is on 80/443 today:
- The host's nginx 1.24 holds 80/443 for `api.myotherapp.in`, `api.myotherapp.com`, `api.staging…`, the `finnet` vhosts (`docker-compose.prod.yml:149-157`) and, presumably, letmego and bcards.
- Certificates are certbot's, under `/etc/letsencrypt` on the host (`nginx.conf:24-27,74-77`).
- `bcards-nginx` is a container whose published ports are unknown.

If the brief is applied as written:
- **Breaks:** a compose nginx publishing 80/443 fails to bind ("address already in use"), so the new stack has no edge.
- **Breaks:** freeing the ports by stopping the host nginx takes the other two projects and the other API names offline, and stops certificate renewal.

Safe alternatives:
- Keep the host nginx as the edge and add the moa locations there.
- Or run the compose proxy on `127.0.0.1:<port>` behind the host nginx.
- Or, on a new box, move every project's vhosts into one edge deliberately.

**(b) PgBouncer in transaction mode breaks asyncpg prepared statements and session-level locks. Confirmed.**

| Usage | file:line | In transaction mode |
|---|---|---|
| asyncpg statement cache (default on, nothing sets `statement_cache_size`) | `app/db/core.py:37-44` | Breaks ("prepared statement … does not exist / already exists") unless `statement_cache_size=0` + `prepared_statement_cache_size=0`, or PgBouncer ≥ 1.21 with `max_prepared_statements` |
| `server_settings={"plan_cache_mode": "force_custom_plan"}`, a startup parameter | `app/db/core.py:43` | **PgBouncer refuses connections** with an unsupported startup parameter unless it is in `ignore_startup_parameters` or `track_extra_parameters`. Move it to `ALTER ROLE … SET plan_cache_mode` (`docs/SCALING.md:297`). |
| **Session-level** `pg_try_advisory_lock` / `pg_advisory_unlock` through `app/db/locks.py:26-54` (`singleton`, on an AUTOCOMMIT connection): 11 sites in `app/` | `app/core/jobs.py:899` (774501, job maintenance); `discover/counters.py:233,252` (773104); `discover/popularity.py:82` (7731402); `search/sync.py:353` + `scripts/search_reindex.py:56` (773140); `auth/retention.py:89` (773130); `emails/service.py:1188` (773101, timed-email sweep); `closeout/service.py:322` ((773103, event)), `:467` (773102); `live/service.py:943` (7301010); `store/sweep.py:23` (773121) | **Breaks.** Lock and unlock can land on different server connections: the lock leaks onto a pooled connection, and since advisory locks are re-entrant per session, any client later given that connection "gets" it too. Two processes run the same leader loop, or none does. |
| Raw session-level locks: 3 sites | `club_domains/sweep.py:161-166` (773120); `subscriptions/sweep.py:277-282` (773110); `sites/sweep.py:126-131` (773121) | Breaks, as above |
| Side finding | `store/sweep.py:15` and `sites/sweep.py:42` **both use key 773121** | They skip each other's turn today, with or without PgBouncer |
| Transaction-level `pg_(try_)advisory_xact_lock` | `payments/service.py:103`, `payments/sweep.py:238`, `ledger/service.py:78,80`, `ledger/jobs.py:360`, `ledger/backfill.py:59`, `discover/stats.py:151`, `admin_alerts/service.py:135`, `emails/service.py:655`, `subscriptions/gates.py:843`, `subscriptions/router.py:1117`, `auth/password_reset.py:172`, `auth/verification.py:146`, `users/service.py:289`, `events/registration/service.py:215`, `payment_gateways/service.py:361,471,505,553` | Safe |
| `SET LOCAL` / `RESET` inside a transaction | `discover/service.py:381,385,390,1704,1706`, `discover/cells.py:205`, `search/text.py:350` | Safe |
| LISTEN/NOTIFY | **none** in app or scripts. The job runner wakes by in-process event, Redis `jobs:wake` or a 1 s poll (`app/core/jobs.py:353-410`). | n/a |
| Migrations (session `SET`s, `app/db/migration_session.py:172`), `pg_dump`, the restore drill | use `APP_DATABASE_URL_SYNC` directly (`docs/OPERATIONS.md:105-107`) | Must stay direct |

If applied as written, PgBouncer **breaks** production: connection failures at once from the startup parameter, then duplicate or stuck sweeps.

Safe alternative:
- PgBouncer ≥ 1.21 with `max_prepared_statements`, `plan_cache_mode` moved to the role, and the leader loops taking their lock on a direct connection: a small second engine on a direct DSN, or a session-mode pool.
- Or convert them to the `scheduled_runs` + `pg_try_advisory_xact_lock` pattern already used by `payments/sweep.py` and `ledger/jobs.py`.
- At the current size, PgBouncer is avoidable: three workers fit 97 connections with `APP_DB_MAX_OVERFLOW=10` (`docs/OPERATIONS.md:824-829`).

**(c) A Cloudflare proxy plus 80/443 limited to Cloudflare makes every client a Cloudflare IP. Confirmed.**

How the address is read today:
- nginx sets `X-Real-IP $remote_addr` and `X-Forwarded-For $proxy_add_x_forwarded_for` (`nginx.conf:42-43,112-113`).
- `real_client` trusts headers only from a private peer (the Docker gateway) and takes the right-most public hop (`app/core/net.py:35-52`).
- Behind Cloudflare, that hop is the Cloudflare edge.
- `ClientAddressMiddleware` writes it into the scope (`app/core/net.py:68-86`), so every consumer gets the edge address.
- No code reads `CF-Connecting-IP`, `CF-IPCountry` or `True-Client-IP` (grep is empty).

| Consumer | file:line | Effect |
|---|---|---|
| Rate limiter, keyed on `get_remote_address` = the scope client, and per-route keys such as the certificate check's (`certificates/router.py:115-120`) | `app/core/rate_limit.py:171,196` | Every visitor in a region shares a few edge IPs, so mass 429s |
| Live-room caps per address (3000 streams per process) and per-person limits with a ceiling per address | `app/api/live/limits.py`, `docs/OPERATIONS.md:1012-1016` | A whole venue on one edge IP hits the address ceiling |
| Geo by IP for "near you" | `app/api/discover/geo.py:798-803` (`site.visitor_ip` or `client_address`) | The site's server path is unaffected (`X-Visitor-IP` with a site token, `app/core/site.py:43,84-90`); the app and the browser get the edge PoP's city |
| Sign-in records, sessions, audit, Security page geolocation (`APP_GEOIP_URL`) | via `net.client_address` | Edge IPs recorded |

If applied as written:
- **Breaks:** the rate limits and live caps.
- **Breaks:** the other projects' domains and any non-proxied names (`api.myotherapp.com`, `finnet`, letmego, bcards) if 80/443 is firewalled to Cloudflare at the host or security group.
- **Degrades:** geo and audit.

Safe alternatives:
- The host nginx `realip` module: `set_real_ip_from <Cloudflare ranges>; real_ip_header CF-Connecting-IP;`, kept current. No app change is needed.
- Or the app trusts `CF-Connecting-IP` only when the right-most hop is in Cloudflare's ranges.
- Restrict 80/443 per vhost rather than per port, or only once every name on the box is proxied.
- If you keep certbot HTTP-01 for any name that is not proxied, it needs port 80 open.

**(d) A staging stack and load tests on the production box degrade production; today's box cannot hold them. Confirmed.**
- On today's box, there is no room: 3.8 GB, about 200 MB free, 2 burstable vCPUs (`docs/OPERATIONS.md:499-503,782-797`).
- A second copy of `docker-compose.prod.yml` cannot run beside the first anyway: fixed `container_name`s, subnet `192.168.5.0/24`, fixed IPs, and host ports 8000, 8001 and 6432 all collide.
- Load tests against production share its CPU, disk and Postgres whatever the box.
- **If applied as written:** it breaks on today's box (OOM), and on a 16 GB box it degrades production during every test.
- **Safe alternative:** staging as a separate compose project, with its own names, subnet, ports and caps, on a separate small instance, or on the big box with hard `cpus`/`mem_limit` and never during events. Run load tests from another machine against staging, or against production only in agreed windows.

**(e) `shared_buffers` 4 GB needs the 16 GB box and a Postgres restart. Confirmed.**
- The Postgres container is capped at `mem_limit: 1280m` (`docker-compose.prod.yml:49`), so 4 GB of buffers would OOM-kill it. The whole box today has 3.8 GB.
- `shared_buffers` takes effect only on a server restart.
- Nothing sets Postgres parameters today, and there is no `shm_size`: parallel queries use `/dev/shm`, 64 MB by default in Docker.
- **If applied as written:** it breaks on today's box.
- **Safe alternative:** on the 16 GB box, raise `mem_limit` (about 6–7 GB) together with `shared_buffers` 4 GB, `effective_cache_size`, `work_mem` and `shm_size: 1g` via `command: -c …`. Restart in a quiet window: API requests fail for the seconds Postgres takes to come back.

**(f) A 202-polling download path, or a link instead of an attachment, is a client and contract change. Confirmed, with one part already in place.**

Every client today expects **200 with the bytes**:

| Client | file:line |
|---|---|
| App: Dio fetches PDF, PNG and pkpass, saved to file | `app:…/ticket_view_page.dart:119-153,300-321` |
| Dashboards: fetch a blob or `window.open` | `web:apps/clubs/src/features/idcards/api.ts:44-48`, `print-frame.tsx:46-96` |
| Site: certificate `window.open` | `site:src/components/certificate.jsx:41-46` |
| Email links | `/api/v1/files/tickets/{id}.pdf?s=` (`emails/router.py:285-305`) |

A 202 would show up as a broken or empty file in every one of them.

The link already exists in the ticket email (`{{ticket_url}}`, `app/core/mail/starters.py:70`). Removing the attachment, however, is a **data change**: `attachments.ticketPdf` is stored on every club's and event's copy of the email (`emails/models.py:43`), not only on the starter.

Safe alternative:
- Keep the GET endpoints synchronous, rendering on a miss through the doc cache, with a single-flight per key added.
- Add a separate 202 endpoint only for bulk and new clients.
- Switch the email to link-only through a migration or a server-side override, with the owner's consent (section 7).

**(g) Much of Phases 3–5 already exists from Wave 2. Confirmed: every item the hypothesis names exists. The real gaps are in the last column.**

| Brief item | Exists | Where | Real gap |
|---|---|---|---|
| `/healthz`, `/readyz` | yes | `app/api/health.py:94-110`, `doth.py:196-199` | The deploy smoke check uses `/api/v1/plans` unless `vars.PROD_SMOKE_URL` is set (`live-deploy.yml:420`) |
| Postgres search as default | yes (trigram) | section 3 | weighted tsvector, `simple`/`english` config, ranking, highlights |
| Meilisearch as an option | yes, behind `APP_SEARCH_URL` | `app/core/search.py`, `app/api/search/` | only the switch name (`SEARCH_BACKEND`) |
| Compose limits | yes, for 2 vCPU / 8 GB | `docker-compose.prod.yml`, `docs/OPERATIONS.md:734-870`, `tests/test_one_box.py` | resizing to 4 vCPU / 16 GB; Postgres parameters |
| Backups with an off-site copy and a restore drill | yes (pg_dump custom, sha256, daily/weekly tiers, alerts) | `scripts/backup.py`, `scripts/restore_drill.py`, `app/api/backups/offsite.py` | Target is S3, not R2, and the SSE args are incompatible with R2. No WAL archiving or point-in-time recovery. Whether the cron is installed and the bucket configured in production is unknown. |
| Runbook | yes, 12 sections | `docs/OPERATIONS.md` | **No fresh-server bring-up** (Docker, nginx, certbot, `.env`, restore, DNS); nothing on Cloudflare |
| buildx | BuildKit required and checked | `backend/Dockerfile:1-4`, `live-deploy.yml:223-228` | multi-arch, a registry |
| Job queue: priorities, per-name concurrency, retries, dead letters | yes | `app/core/jobs.py` | priorities unused; the render cap is per process, not global |
| Redis fan-out for live | yes (SSE) | `app/api/live/bus.py`, `app/core/redis.py` | none, unless WebSockets are insisted on |
| On-demand rendering | partly: doc cache; certificates and ID cards on demand | section 2.2 | tickets rendered at registration (attachment); no single-flight; ID cards and passes not cached |
| Monitoring | Sentry (off until `APP_SENTRY_DSN`), JSON logs, request ids, admin alerts (`job_dead`, `backup_failed`), Discord webhook | `app/core/observability.py`, `docs/OPERATIONS.md:551-583` | no metrics, uptime check or host/Postgres monitoring; `pg_stat_statements` not enabled |
| Load tests | Python bench (`load.py` browse mix, `hot.py` registration rush, `live.py`/`live_sse.py`, `sweeps.py`) | `scripts/bench/`, `scripts/bench/RESULTS.md` | k6/Locust, a certificate burst, and runs against a real server (all laptop runs so far) |
| PgBouncer, R2, Cloudflare in front of the API, signed QR, staging on the box | no | — | all |

**(h) The app's settings use the `APP_` prefix, so the brief's names need mapping. Confirmed.**
- `SettingsConfigDict(case_sensitive=True, env_prefix="APP_")` (`app/config.py:14-17`). A bare `SEARCH_BACKEND`, `STORAGE_*` or `PRERENDER_POPULAR` would sit in the environment and be ignored. `docker-compose.prod.yml:117-120` already warns about exactly this.
- Use `APP_SEARCH_BACKEND`, `APP_STORAGE_*` and `APP_PRERENDER_POPULAR`, or `validation_alias`.
- `APP_S3_BUCKET`, `APP_S3_ACCESS_KEY`, `APP_S3_SECRET_KEY` and `APP_S3_BASE_PATH` are **required with no default** (`app/config.py:109-112`). Renaming them without aliases stops the app booting.
- None of the brief's names exist today.
- Compose-level names (`POSTGRES_*`, `MEILI_MASTER_KEY`) are not app settings and stay unprefixed.

**(i) An R2 public bucket on a custom domain may make the media Worker unnecessary. Partly confirmed.**

The Worker does no transforms and no resizing; variants are made at upload (`app:infra/cloudflare/media/README`). Its only job that R2 cannot do itself is reading a **private** S3 bucket with a read-only key. With a public R2 bucket on `media.myotherapp.in`, it becomes unnecessary, provided its other duties move elsewhere:

| What the Worker does today | Where (in `app:infra/cloudflare/media/`) | Where it would have to go |
|---|---|---|
| Allow-list of 8 public folders | `media.js:19-50` | Only public objects in the public bucket. That means moving agenda speaker photos out of `events/{id}/layout/` (1.2). |
| No SVG, image extensions only | `media.js` | Enforced by the backend at upload |
| `..` and `%2f` rejection | `media.js:60-74` | — |
| Cache key ignores the query; `v-<hex>` keys immutable for a year, others 1 h browser / 1 day edge, misses 60 s | `media.js:39-44,82-84`; `worker.js:60-63` | A Cache Rule, and `Cache-Control` set at upload |
| ETag/304 | `media.js:99-113` | R2 does this itself |
| `ACAO *`, CORP, `nosniff` | `worker.js:41-45` | A Transform Rule plus R2 CORS |
| 300 misses a minute per IP | `wrangler.toml:36-39` | A WAF rate-limit rule (plan-dependent) |
| `robots.txt` | `worker.js:128-137` | An object in the bucket |

Route conflicts:
- The Worker route `media.myotherapp.in/*` (`app:infra/cloudflare/media/wrangler.toml:23`) must be removed.
- The README's `AAAA 100::` placeholder record may conflict with attaching an R2 custom domain (unverified).
- The club-sites wildcard `*.myotherapp.in/*` reserves `media`, `cdn`, `static` and `assets` (`app:infra/cloudflare/club-sites/routing.js:21-28`). It passes them through, but each request still costs a Worker invocation. A new host name must be added to that list.

Most important: **R2 keys must equal today's URL paths** (minus `S3_BASE_PATH`), or `media.myotherapp.in` links already in club-site HTML, the app's image cache and link previews break. Whether `APP_MEDIA_CDN_URL` and the Worker are live in production is unknown.

**Other risks found on the way:**
- **Postgres is published on host port 6432** (`docker-compose.prod.yml:61`). That is PgBouncer's conventional port, and it is open to the internet if the security group allows it (unknown).
- **The backend is published on 8000 and 8001 on all interfaces**: nginx can be bypassed if the security group allows those ports (unknown). Direct callers' headers are ignored (`app/core/net.py:39-41`).
- Many bucket objects are never deleted (1.1). A copy to R2 moves the orphans too, unless it is driven from the database's keys.
- `APP_SECRET_KEY` signs the permanent emailed ticket and certificate links (`app/core/mail/links.py:20-30`). Rotating it breaks every one already sent.

---

## 7. Questions for the owner

These are decisions that change data, money or a public contract.

1. **Ticket email: attachment or link?**
   - Today every ticket email attaches the PDF, which is what forces a render at registration, and it also carries a "Download your ticket" link.
   - Going link-only changes what guests receive. It means rewriting `attachments.ticketPdf` on every club's and event's stored email template (or overriding it on the server).
   - Should paid-event tickets keep the attachment?
2. **The QR transition.**
   - Should a signed token replace the plain ticket code on new tickets only?
   - Must the gate keep accepting plain codes forever? Wallet passes cannot be updated, and printed ID cards, saved PDFs and old emails exist.
   - Should tokens be single-use (this breaks the app's validate-then-check-in flow) or expire (this breaks offline gate replays)?
3. **Public versus private objects.**
   - Agenda speaker photos live in a private-looking prefix (`events/{id}/layout/`). Should they move?
   - Profile pictures stay private: confirm.
   - Should orphaned objects (old posters, superseded `docs/` PDFs) be migrated or dropped?
4. **Keep `media.myotherapp.in`**, with identical key paths, as the R2 public bucket's domain, so links already in club sites, the app's cache and link previews keep working? This also means republishing every club site.
5. **Where backups live.** On R2, the uploader must drop SSE, and S3 versioning plus the no-delete IAM key are lost (R2 bucket locks might replace them; unverified). Alternatively, keep backups on S3 in another region and provider account, which also keeps storage and its backup with different providers.
6. **The new box: one or two, and which architecture?**
   - Should letmego and bcards move to the 4 vCPU / 16 GB box too? That decides the nginx-in-compose question.
   - Which region: `docs/SCALING.md` recommends ap-south-1.
   - Graviton (arm64, cheaper; the image builds natively) or x86?
   - It also sets the monthly cost.
7. **Staging on the production box?** Is the risk accepted, with caps and no load tests during events, or does staging get its own small instance?
8. **Proxy `api.myotherapp.in` through Cloudflare?**
   - Rate limits, geo and audit then depend on the realip configuration being right.
   - Firewalling 80/443 to Cloudflare affects the other domains on the box.
   - Which Cloudflare plan: WAF rate-limit rules, needed to replace the media Worker's limiter, are plan-dependent.
9. **HTML-template designs.** wkhtmltopdf is archived and gone after Debian bookworm. Should the remaining HTML ticket and certificate designs, and the payment receipt, be redrawn with the layout renderer, given that **existing designs would look different**?
10. **Live-room transport.** Keep SSE, which already ships in all three clients, rather than the brief's WebSockets, which would change a published client contract?
