# Social auto-posting and the share kit

Code: `backend/app/api/social`. Contract with the club dashboard, website and app:
`feature-register/social-posting-contract.md`. Product write-up: "Feature 1" in the feature list.

Clubs connect their Facebook Page, Instagram business or creator account, LinkedIn profile, LinkedIn
Page and X account once (owners and admins, no passwords shared), plan one post per platform per
event, and MyOtherApp publishes each at the moment they chose. Every club, on every plan, also gets
the **share kit**: each platform's text and poster, ready to post by hand.

## What works without any setup

The share kit needs nothing from the owner:

- drafted text per platform with fill-in words, hashtag ideas, and a tracking link per platform
  (`Social · Instagram`, utm_medium `social`);
- the poster made in every platform's size (crop around a focus point, or fit on the club's colour),
  stored on the media CDN;
- the team's push when an event is published ("Hack Night is live — share it") and the app's share
  kit screen;
- the attendees' share row on every event page, each platform with its own tracking link
  (utm_medium `share`);
- `share_image_url` (the 1200 x 630 poster) on the public event payloads, for link previews.

## What switches on with each platform's settings

Until a platform's settings are saved, `GET /clubs/social/platforms` says
`available: false, reason: "not_configured"` for it and nothing can be connected. Save them in the
server's `.env` (never in a repository or a chat), then restart.

| Setting | What it is |
| --- | --- |
| `APP_SOCIAL_KEYS_SECRET` | The Fernet key every connected account's tokens are encrypted with. Required for any platform. Its own key: never reuse `PAYMENT_KEYS_SECRET`, `AI_KEYS_SECRET` or `WHATSAPP_KEYS_SECRET`. A comma-separated list rotates (first encrypts, all decrypt). |
| `APP_META_APP_ID`, `APP_META_APP_SECRET` | The Meta app (already used by WhatsApp Embedded Signup): Facebook Pages and Instagram. |
| `APP_META_LOGIN_CONFIG_ID` | Optional: a Facebook Login for Business configuration's id. Empty: the classic dialog asks for the permissions by name. |
| `APP_LINKEDIN_CLIENT_ID`, `APP_LINKEDIN_CLIENT_SECRET` | The LinkedIn app: members' own profiles. |
| `APP_LINKEDIN_PAGES_ENABLED` | `true` only after LinkedIn approves the Community Management API (Standard tier). Until then LinkedIn Pages read `review_pending`. |
| `APP_X_CLIENT_ID`, `APP_X_CLIENT_SECRET` | The X app's OAuth 2.0 client id and secret. |
| `APP_X_MONTHLY_POST_CAP` | Posts to X across all clubs in one IST month (default 100, about $20 with links). At the cap a post fails with "share manually". |
| `APP_SOCIAL_GRAPH_VERSION`, `APP_LINKEDIN_API_VERSION` | `v26.0` and `202610` by default; move them on yearly (below). |

Generate `APP_SOCIAL_KEYS_SECRET` on the server:

```
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Keep a copy somewhere safe: without it no saved token can be read and every club must reconnect.

### The addresses to register

Each platform's app must list these exactly:

| Platform | Redirect (callback) URL |
| --- | --- |
| Facebook Page | `https://api.myotherapp.in/api/v1/social/oauth/facebook/callback` |
| Instagram | `https://api.myotherapp.in/api/v1/social/oauth/instagram/callback` |
| LinkedIn (member) | `https://api.myotherapp.in/api/v1/social/oauth/linkedin/callback` |
| LinkedIn Page | `https://api.myotherapp.in/api/v1/social/oauth/linkedin_page/callback` |
| X | `https://api.myotherapp.in/api/v1/social/oauth/x/callback` |

Meta also needs:

- Deauthorize callback URL: `https://api.myotherapp.in/api/v1/social/meta/deauthorize`
- Data deletion request URL: `https://api.myotherapp.in/api/v1/social/meta/data-deletion`
  (answers Meta with `{url, confirmation_code}`; the status page is
  `https://myotherapp.in/data-deletion?code=...`, built by the website).

### Meta app: Facebook Pages and Instagram

1. developers.facebook.com → the MyOtherApp app (the one WhatsApp uses) → **Add product: Facebook
   Login for Business** (or Facebook Login).
2. Facebook Login → Settings: turn on Client OAuth Login and Web OAuth Login; add the two Meta
   redirect URLs above to **Valid OAuth Redirect URIs**; leave "Enforce HTTPS" on.
3. If using Login for Business: Configurations → create one ("MyOtherApp social posting", user access
   token, permissions below) and save its id as `APP_META_LOGIN_CONFIG_ID`.
4. **Add product: Instagram** → "API setup with Facebook login" (the Instagram account must be a
   Business or Creator account linked to a Facebook Page).
5. Permissions to request at **Advanced Access** in App Review: `pages_show_list`,
   `pages_manage_posts`, `pages_read_engagement`, `instagram_basic`, `instagram_content_publish`.
   Each needs a reason and a screen recording of the real flow (Connect → pick the Page → schedule a
   post → the post on the Page/Instagram).
6. App settings → Basic: Privacy Policy URL, Terms URL, **User data deletion: Data deletion
   callback URL** (above), app icon, category; Business Verification of MyOtherApp (Business
   settings → Security Centre) if not already done for WhatsApp.
7. App settings → Advanced → Deauthorize callback URL (above).
8. Test with a test Facebook Page and a test Instagram business account (Roles → Testers) before
   review; never real clubs' accounts.

### LinkedIn app

1. linkedin.com/developers → Create app, associated with **MyOtherApp's LinkedIn Page**, then have a
   Page admin verify the app (Settings → Verify).
2. Products: **Sign In with LinkedIn using OpenID Connect** and **Share on LinkedIn** (self-serve).
3. Auth → OAuth 2.0 settings → Authorized redirect URLs: the two LinkedIn URLs above.
4. Copy the Client ID and Primary Client Secret into the settings. Members' tokens last 60 days; the
   daily job warns clubs seven days ahead.
5. Later, for club Pages: request the **Community Management API** (Development tier, then Standard
   tier with a screen recording). A rejection at either tier cannot be re-applied with the same
   app, so apply only with a finished demo. When Standard is granted, set
   `APP_LINKEDIN_PAGES_ENABLED=true`.

### X developer account

1. developer.x.com → sign up with **pay-per-use** billing and set a **monthly spend cap first**.
2. Create a Project and an App; User authentication settings: OAuth 2.0, type **Web App (confidential
   client)**, permissions **Read and write**, Callback URI the X URL above, Website
   `https://myotherapp.in`.
3. Copy the OAuth 2.0 Client ID and Client Secret into the settings. Keep `APP_X_MONTHLY_POST_CAP`
   in step with the spend cap (a post with a link is about $0.20).

### Deploying

The migration `social_posting` adds the tables and the two plan flags: deploy with
`migrate=true`. No new Python package (plain httpx and Pillow, already installed).

## How it works

### Plans and roles

- Share kit: every plan. Automatic posting to Facebook, Instagram and LinkedIn: plan flag
  `social_autopost` (starter, growth, pro, campus, enterprise from the migration). X: `social_x`
  (enterprise). Admins edit both in Admin → Subscriptions. Checked when an account connects, when a
  post is scheduled, and again when it is sent (a lapsed plan fails it with "share manually").
- Owners and admins connect and disconnect accounts, change the approval setting and approve.
  Editors draft, schedule (with approval off) and see results. Door staff see none of it (they do
  get the share kit). With approval on, an editor's post waits as `needs_approval`.
- A co-host club posts the shared event from its own accounts and sees only its own posts; when it
  leaves or is removed its waiting posts are cancelled.

### Triggers

`on_publish` (now, for a published event), `at` (the club's own time, never moved),
`before_start`, `before_reg_close`, `after_end` (minutes before or after). A relative time in quiet
hours (23:00–07:00 IST) moves to 07:00, or to 22:55 the evening before when 07:00 would be after
what it announces.

What the event does: published → `on_publish` posts go and the team gets the share kit; a new date,
time, venue or fee → relative times move, and a waiting post that wrote the old detail by hand is
paused (`event_changed`) until the club schedules it again (fill-in words never need that: they are
filled at posting); cancelled, unpublished or deleted → everything waiting is cancelled, and a
cancelled event whose posts went out gets a "Cancellation" draft.

### Exactly once

Each target is claimed by one conditional UPDATE before anything is sent; the platform's post id is
saved the moment it answers. A send whose answer was lost (a timeout after sending, a worker that died
mid-send) is looked up on the platform before it may be sent again. LinkedIn does not let apps read a
member's posts, so such a post fails with "check LinkedIn, then Retry" instead of guessing.

### Failures

| What happens | Becomes |
| --- | --- |
| Token expired or revoked | Account `needs_reconnect`, its posts `held`; bell + mail to the owner + push to admins on the app; reconnecting sends them |
| Instagram's 100 posts a day, X's rate limits | `waiting_limit`, tried again by itself (up to a day) |
| Platform down | Retried after 1, 2, 5, 10, 20, 40 minutes then hourly, for six hours; then `failed` with Retry |
| Picture refused | `failed`, action `fix_image` |
| X's month cap reached, plan lapsed, policy refusal | `failed`, action `share_manually` |

The daily job (after 06:00 IST) refreshes the tokens that can be refreshed, warns seven days before
access ends, marks ended ones, logs each platform's day (posted, failed, waiting) and raises the
admin alert `social_failures` when one platform's failures jump. There is no ops Telegram bot yet;
the summary is in the log and on the `scheduled_runs` row `social_daily`.

### Security

- Tokens are Fernet-encrypted with `SOCIAL_KEYS_SECRET` before they reach the database; never in a
  response, a log line (httpx's request log is scrubbed too), the activity log or an alert.
- Sign-in uses a signed, one-time state (15 minutes); the callback's `code` and `state` are filtered
  from access logs; `return_to` must be the club dashboard.
- Disconnecting revokes on the platform where possible (Meta: only when no other live connection
  rests on the same person's grant) and deletes the tokens. Removing a team member or deleting a club
  disconnects the accounts they connected.
- Every connect, disconnect, schedule, approval, cancel, retry and post is in the activity log
  (`social.*` actions) with who did it.

## Platform API versions (upgrade yearly)

| Adapter | Version | Retires |
| --- | --- | --- |
| Facebook Page, Instagram (`adapters/meta.py`) | Graph API `v26.0` (`APP_SOCIAL_GRAPH_VERSION`) | About two years after release (29 July 2026) |
| LinkedIn member and Page (`adapters/linkedin.py`) | REST `202610` (`APP_LINKEDIN_API_VERSION`) | About a year after release |
| X (`adapters/x.py`) | API v2 (`/2/tweets`, `/2/media/upload`) | Unversioned; watch X's changelog |

## Not in this version

Reels, Stories and carousels; milestone triggers ("20 seats left"); Instagram without a linked
Facebook Page (Meta's "Instagram API with Instagram Login" would need its own Instagram app id and
secret); a club's own X developer key.
