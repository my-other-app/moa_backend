# Connecting AI assistants to a club (MCP)

Status: built in the backend's `feat/mcp-server` (off `feat/club-merch`), combined with AI assist on `feat/mcp-on-ai`, behind
`APP_MCP_ENABLED` (off by default). The dashboard's confirm page and its
Security-page list are still to build. Section 7 is their contract.

A club connects Claude, ChatGPT, Cursor or any other MCP client to
`https://api.myotherapp.in/mcp`. After that, its team can ask the assistant
things like "how many people checked in to Bot Build Night?" or "draft next
month's hack night and offer the club tee". The assistant:

- reads what the person who connected it could read in the dashboard;
- writes **drafts only**;
- **asks** a person to publish an event or send an announcement, through a
  link that person opens in the dashboard.

Code: `backend/app/api/mcp/` (the package docstrings say what each file does).
Role rules: `backend/app/api/club_access/policy.py`.
Tests: `backend/tests/test_mcp.py`.

- **Protocol:** MCP specification revision **2026-07-28**, with the
  authorization spec of the same revision. The server also serves clients
  that still open with the `initialize` handshake (2024-11-05 to 2025-11-25).
- **SDK:** the official Python SDK, `mcp==2.3.0`.

---

## 1. What a club does

### Claude (claude.ai, Claude Desktop)

1. Go to **Settings → Connectors → Add custom connector**.
2. Fill in:
   - **Name:** `MyOtherApp`
   - **URL:** `https://api.myotherapp.in/mcp`
3. Choose **Add**, then **Connect**. A MyOtherApp page opens.
4. Sign in with the club dashboard's email and password: the club's own login, or your own team login.
5. If two-step sign-in is on, enter the code.
6. If the address opens more than one club, pick the club.
7. Untick anything you don't want to allow, then choose **Allow**.
8. Claude now lists the MyOtherApp tools. You can turn individual tools off in its connector settings.

On Claude Team and Enterprise, an organisation owner may need to add the
custom connector for the organisation first. Then each person connects it as
above.

### ChatGPT

1. Go to **Settings → Apps & Connectors → Advanced settings** and turn on **Developer mode**.
2. Create a connector:
   - **MCP server URL:** `https://api.myotherapp.in/mcp`
   - **Authentication:** OAuth
3. ChatGPT registers itself with the server and opens the same MyOtherApp sign-in page.
4. Sign in, pick the club, and allow, as for Claude.

ChatGPT's menu labels move around; the URL and "OAuth" are what matter.

### Cursor, VS Code, the MCP Inspector, and anything else

Add a remote MCP server with the URL `https://api.myotherapp.in/mcp`. In
Cursor's `mcp.json`:

```json
{ "mcpServers": { "myotherapp": { "url": "https://api.myotherapp.in/mcp" } } }
```

The client opens the browser for sign-in. Desktop clients come back on their
own loopback port (`http://127.0.0.1:<port>/...`) or their own scheme
(`cursor://...`). For those, the pages warn: "will be reached on this
device… carry on only if you started this from that app".

### Disconnecting

There are two places to disconnect:

- the assistant's own connector settings, which revoke the token;
- the dashboard's Security page, through `DELETE /api/v1/clubs/connected-apps/{id}`.

Either way the app loses access at once. Requests it made that are still
waiting for approval are rejected.

---

## 2. What it may do: scopes

The consent page lists each scope the app asked for that **the person's role
could use**. Each one is ticked by default and can be unticked. Scopes the
role can't use are shown greyed out.

Which role may grant which scope is `policy.allows_scope`. Each scope stands
for the dashboard request that does the same thing.

| Scope | Consent page says | Roles that can grant it |
|---|---|---|
| `events:read` | Read your events | owner, admin, editor, door |
| `events:write` | Draft events | owner, admin, editor |
| `attendees:read` | See attendees' contact details | owner, admin, editor |
| `finance:read` | Read your finances | owner, admin |
| `store:read` | Read your store | owner, admin, editor, door |
| `store:write` | Add to your store | owner, admin, editor |
| `comms:write` | Draft announcements | owner, admin, editor |
| `team:read` | See your team | owner, admin |

A token carries the scopes that were allowed. Calling a tool outside them is
refused at the HTTP level:

- **Status:** `403`
- **Header:** `WWW-Authenticate: Bearer error="insufficient_scope", scope="<the one needed>", resource_metadata="…"`

A client can use this to ask for more, which is step-up authorization. A
refresh can keep the scopes or narrow them, never widen them.

---

## 3. The tools

Every tool is scoped to the token's club. An id from another club answers
"not found".

On every call each tool checks, in this order:

1. The token's scope.
2. The club's plan: `mcp`.
3. The person's role **now**: `policy.allows_tool`. The tool is allowed exactly when the role could make the "stands for" request in the dashboard.
4. The arguments, against the tool's JSON schema. Every schema has `additionalProperties: false` and one `type` per property.

`tools/list` shows only the tools the token's scopes **and** the current role allow.

| Tool | Scope | Hints | Stands for (policy) | What it does |
|---|---|---|---|---|
| `list_events` | events:read | read-only | GET /clubs/events/list | Upcoming, past, draft or all; paged. |
| `get_event` | events:read | read-only | GET /events/{id}/summary | Dates, venue, fee, capacity, registration window, contact, description, form questions, speakers, store offers, links. |
| `event_stats` | events:read | read-only | GET /events/{id}/summary | Registrations, check-ins, paying guests, capacity. Money (tickets, store, refunds, owed) needs finance:read and a money role (`policy.allows_event_money`). |
| `list_registrations` | events:read | read-only | GET /events/registration/{id}/list | Name, college, status (paid/unpaid/free/refunded/revoked), checked in, form answers; paged. **Email and phone are masked** unless the token has attendees:read **and** `policy.allows_contacts(role)`; masked mode also masks contacts typed into answers and searches by name only. |
| `finance_summary` | finance:read | read-only | GET /clubs/finance | Totals, by month and by event, for up to 24 months. |
| `list_store_items` | store:read | read-only | GET /clubs/store | The club catalogue: price, stock and left per size, on sale, which events offer it. |
| `list_feedback` | events:read | read-only | GET /events/ratings/{id} | Average rating, reviews (newest first, paged), survey results. |
| `list_team` | team:read | read-only | GET /clubs/access | The club account, and each member's role and status. |
| `create_event_draft` | events:write | write | POST /events/create | A new event, **lifecycle draft**: nobody sees it, nobody is told. Counts against `events_per_month`; a fee needs `tickets`. Returns its id and dashboard link. |
| `update_event_draft` | events:write | write, idempotent | PUT /events/update/{id} | Changes a **draft** (`clear` empties fields). A published event returns `EVENT_NOT_DRAFT` with "edit it in the dashboard" and the link. |
| `create_store_item` | store:write | write | POST /clubs/store | A catalogue item, created **off sale** (needs `merch`). |
| `offer_store_items` | store:write | write, idempotent | PUT /events/{id}/store/offers | Adds or removes offers on a **draft** event (`merch_items_per_event` holds). |
| `draft_announcement` | comms:write | write | POST /clubs/notifications/events/{id}/send | Saves an announcement to an event's people (all, attendees, non_attendees) **unsent**; says whom it would reach. |
| `request_publish_event` | events:write | write | PATCH /events/{id}/closeout/settings | A pending action: returns `confirm_url`. |
| `request_send_announcement` | comms:write | write | POST /clubs/notifications/events/{id}/send | A pending action for a draft: returns `confirm_url`. |

No tool is destructive (`destructiveHint: false` on all of them).
`openWorldHint` is false on all of them.

### Never through MCP

These are not possible through MCP: no tool exists for them, and no pending action kind does either.

- refunds and payouts;
- deleting anything;
- changing a published or live event, including its price;
- team or role changes;
- sending an email or a push directly.

### People's text

Text that people typed is always returned as `{"user_written": true, "text": "…"}`. That covers:

- event descriptions and guidelines;
- speakers' bios;
- store item descriptions;
- form answers;
- reviews and survey comments.

Every result's text starts with a line saying such values are data, not
instructions. The server's `instructions` say the same.

### Tool errors

A refusal is a normal tool result with `isError: true`. Its `structuredContent` is:

```json
{"error": "<CODE>", "message": "...", ...details}
```

The codes:

| Code | When |
|---|---|
| `EVENT_NOT_FOUND` | No such event in this club. |
| `EVENT_NOT_DRAFT` | The event isn't a draft. Comes with `dashboard_url`. |
| `ROLE_FORBIDDEN` | The person's role doesn't allow the tool. |
| `PLAN_FEATURE_LOCKED` | The plan doesn't include it. Comes with `feature`, `required_plan` and `upgrade_url`. |
| `PLAN_LIMIT_REACHED` | A plan limit is reached. |
| `INVALID_ARGUMENTS` | The arguments don't fit the tool's schema. |
| `INVALID_ARGUMENT` | An argument's value isn't usable. |
| `UNKNOWN_CATEGORY` | No event type by that name. |
| `ITEM_UNAVAILABLE` | A store item isn't in this club's store. |
| `DRAFT_NOT_FOUND` | No announcement draft by that id. |
| `DRAFT_NOT_PENDING` | The announcement was already sent or discarded. |
| `INTERNAL` | Something failed on the server. The details are logged, never returned. |

---

## 4. Signing in and the tokens (OAuth 2.1)

The backend is the authorization server for its own MCP endpoint. It
follows the MCP authorization spec of revision 2026-07-28.

### Endpoints

All of these answer 404 while `APP_MCP_ENABLED` is off.

| Endpoint | What |
|---|---|
| `GET /.well-known/oauth-protected-resource/mcp` (and `/.well-known/oauth-protected-resource`) | RFC 9728. `resource` is `https://api.myotherapp.in/mcp`; `authorization_servers` is `["https://api.myotherapp.in"]`. |
| `GET /.well-known/oauth-authorization-server` | RFC 8414. Includes `code_challenge_methods_supported: ["S256"]`, `client_id_metadata_document_supported: true` and `authorization_response_iss_parameter_supported: true`. |
| `POST /oauth/register` | RFC 7591 dynamic client registration, limited to 20 an hour per address. A public client (`token_endpoint_auth_method: none`) gets no secret. Otherwise a secret is returned once and stored as its sha256. |
| `GET /oauth/authorize` | Checks the request and shows the sign-in page. |
| `POST /oauth/authorize` | The steps: sign in, then two-step code, then club picker, then consent. Limited to 20 a minute per address. |
| `POST /oauth/token` | `authorization_code` and `refresh_token`. |
| `POST /oauth/revoke` | RFC 7009. Revoking a refresh token disconnects the app; revoking an access token ends only that token. |

### Clients

Two kinds of client are accepted:

- **Self-registered:** RFC 7591. The MCP spec now calls this deprecated, but Claude and ChatGPT use it.
- **Client ID Metadata Documents:** the `client_id` is an https URL.
  - The document is fetched on first use, then cached for its `max-age`, clamped to between 5 minutes and 24 hours.
  - The fetch uses https on port 443 only. Every resolved address must be public. Redirects are not followed. Limits: 16 KB and 5 seconds.
  - The document's `client_id` must equal its URL.
  - Turn this off with `APP_MCP_CLIENT_METADATA_DOCUMENTS=false`.

### Redirect URIs

- Matching is exact.
- A loopback URI may come back on any port (RFC 8252).
- https is allowed.
- http is allowed only for localhost and 127.0.0.1.
- A native app's own scheme is allowed, such as `cursor://…`. Schemes that run code or open files are refused.
- A redirect URI that doesn't match is shown as an error page. The browser is never redirected to it.

### The authorization request

- **PKCE is required, S256 only.** A missing challenge, `plain`, or a missing method is refused with `invalid_request`.
- **`resource`** must be the endpoint. Anything else is `invalid_target`.
- **Every redirect back** carries `iss` (RFC 9207).

### The person's steps

The sign-in page is server-rendered, with no script, and styled as the dashboard. It answers with `frame-ancestors 'none'` and `no-store`.

1. **Sign in.**
   - It takes the dashboard's email and password: a team member's own, or the club's own account.
   - It has the same lockout as the dashboard: 5 wrong tries per address per account in 15 minutes, 20 per account in an hour.
   - Every try is recorded in `sign_in_attempts` with method `mcp`, so the Security page's history shows these sign-ins.
2. **Two-step.** When the club account has it on, an authenticator code or a recovery code.
3. **Pick a club.** Only when the email and password open more than one club's dashboard.
4. **Consent.** It shows:
   - the app's name and host, and where it will be sent back;
   - the club, who is signed in, and their role;
   - the scopes.

   Before the code is issued, the plan (`mcp`) and the person's access are checked again.

Each step's proof travels in a signed ticket: a 10-minute HMAC under the app
secret. The ticket is tied to the browser by an HttpOnly, SameSite=Lax
cookie that was set when the sign-in page was shown.

### Codes and tokens

**Authorization code:**

- good once, for 5 minutes;
- only for the client it was issued to;
- only with the right verifier.

A code presented a second time ends the grant its first use made. A failed
attempt (wrong verifier, expired) burns the code.

**Tokens:**

- Opaque: `moa_at_…` and `moa_rt_…`.
- Stored only as sha256 digests.
- Each is bound to a **grant**: client, club, the club account, the member if any, and the scopes. The grant is the club's "connected app".
- The access token's audience is `https://api.myotherapp.in/mcp`.

**Lifetimes:**

- Access tokens: `APP_MCP_ACCESS_TOKEN_MINUTES`, 60 by default.
- Refresh tokens: `APP_MCP_REFRESH_TOKEN_DAYS`, 30 by default, counted from each rotation.

**Refresh tokens rotate on every use.** A spent refresh token presented
again ends the whole grant: two parties held it, so one stole it. There is
no grace window.

### What every request re-checks

Before any tool runs, each `/mcp` request reads these as they are **now**:

- the grant;
- the club: live and approved;
- the club account: live and not suspended;
- the member, if any: still an active member of that club.

For an owner's grant it also checks the account's `token_version`, which a
password change or "sign out everywhere" bumps.

If any check fails, the grant is ended and the request answers 401
`invalid_token`. Refresh runs the same checks.

---

## 5. The endpoint

Every request goes to `POST https://api.myotherapp.in/mcp`. The endpoint uses
Streamable HTTP, **sessionless, JSON answers**: every worker can serve every
request, and no SSE goes through nginx.

Before the SDK sees a request, the endpoint does the following, in order:

1. **Kill switch:** 404 while `APP_MCP_ENABLED` is off.
2. **Origin check:** a request carrying an `Origin` not in `APP_MCP_ALLOWED_ORIGINS` gets 403. Requests with no `Origin` (server-side connectors, desktop apps) are fine.
3. **Bearer token:** with none, or a dead one, the answer is 401 with:
   ```
   WWW-Authenticate: Bearer [error="invalid_token", error_description="…",] scope="<all scopes>", resource_metadata="https://api.myotherapp.in/.well-known/oauth-protected-resource/mcp"
   ```
4. **Current access:** the checks in section 4.
5. **Rate limit:** per connected app, `APP_MCP_RATE_LIMIT`, `60/minute;2000/day` by default. Over the limit: 429, `Retry-After: <seconds>`, and a JSON-RPC error with code `-32000`.
6. **Scope check:** for `tools/call`, the token must carry the tool's scope. Otherwise 403 `insufficient_scope` (section 2).

The server identifies itself as `myotherapp-clubs` ("MyOtherApp for clubs"). It offers tools only.

CORS:

- The discovery documents and `/oauth/register|token|revoke` answer any origin, without credentials, so browser-based clients can connect.
- `/mcp` answers only `APP_MCP_ALLOWED_ORIGINS`.

---

## 6. The confirm flow

```
assistant ── request_publish_event(event_id) ─────────▶ /mcp
          ◀─ { status: "pending_approval", confirm_url, expires_at }
person opens confirm_url (https://club-dashboard.myotherapp.in/confirm/{token})
dashboard ── GET  /api/v1/clubs/pending-actions/{token} ──▶ what will happen
dashboard ── POST /api/v1/clubs/pending-actions/{token}/confirm (or /reject)
```

**The token:**

- `moa_pa_` followed by 43 url-safe characters;
- shown once, in the tool result;
- stored only as its sha256;
- expires after **24 hours**.

**Superseding:** a newer request for the same thing (same kind and target)
supersedes an older one that is still pending.

**Who may confirm:**

- Anyone signed in to that club's dashboard whose role may make the request
  the action stands for (`policy.allows_action`): owner, admin and editor for
  both kinds.
- Door staff can open the link but can't decide.
- Another club's session gets 404.

**What confirming does:** exactly what the dashboard does, as the person confirming.

- **publish_event**
  - Sets the draft's status to published, through the closeout settings.
  - Republishes the club site.
  - Tells the club's followers, and people whose interests match, once, as creating a published event does.
  - If the event is no longer a draft, the action **fails** (409 `EVENT_NOT_DRAFT`) and nothing changes.
- **send_announcement**
  - The plan's `announcements_per_month` is checked. If it's over, the answer is 403 `PLAN_LIMIT_REACHED` and the action **stays pending**: upgrade, then approve.
  - Then the same push and inbox send as the dashboard's announcement page, to the draft's audience.
  - The draft becomes `sent`.

Every request, confirmation, rejection and disconnection is written to the
audit log (section 8).

---

## 7. The dashboard's contract

Both pages use the dashboard's normal auth: `Authorization: Bearer <club
session>` or the `access_token` cookie, through the gateway as usual. A club
member's session works too; their role is checked as on every other route.

`policy.can(role)` has two new keys for the UI:

- `connected_apps`: true for owner and admin. These roles see and disconnect every app.
- `approve_actions`: true for owner, admin and editor.

### 7.1 The confirm page: `/confirm/{token}`

**Route.** A dashboard page at `/confirm/[token]`. If the visitor isn't
signed in, send them to `/login?next=/confirm/{token}` and come back after
sign-in.

**Read it:** `GET /api/v1/clubs/pending-actions/{token}` returns 200 with:

```jsonc
{
  "id": "e009b1f5-…",                       // the action's id
  "kind": "publish_event",                  // or "send_announcement"
  "status": "pending",                      // pending | confirmed | rejected | expired | superseded | failed
  "title": "Publish “Draft Demo Day”",      // the page's heading
  "effects": [                              // what approving will do, one line each: show as a list
    "The event becomes public: on the club's page, the website and the MyOtherApp app.",
    "Registration opens as the event is set up.",
    "The club's followers (128) are told about the new event."
  ],
  "target": {                               // {} when the event or draft no longer exists
    // publish_event:
    "type": "event", "id": 2, "name": "Draft Demo Day", "status": "draft", "deleted": false,
    "starts_at": "2026-11-19T01:21:17+05:30", "venue": "Lab 1", "online": false,
    "paid": false, "fee": null, "capacity": 80,
    "dashboard_url": "https://club-dashboard.myotherapp.in/events/2",
    "public_url": "https://myotherapp.in/events/draft-demo-day-39b0a7"
    // send_announcement instead:
    // "type": "announcement", "id": "<draft uuid>", "status": "draft", "title": "...", "body": "...",
    // "audience": "all" | "attendees" | "non_attendees", "event": {"id": 1, "name": "..."},
    // "people": 120, "reachable": 87
  },
  "requested": {
    "app": "Claude",                        // the connected app's name
    "by": {"name": "Eddie Editor", "email": "editor@…", "role": "editor"},  // or {"name": null, "email": null, "role": "owner"}
    "at": "2026-10-20T01:30:36+05:30"
  },
  "expires_at": "2026-10-21T01:30:36+05:30",
  "expired": false,
  "can_decide": true,                       // show Approve / Turn down only when true
  "cannot_decide_reason": null,             // shown instead when pending but not decidable (role, or target gone)
  "decided": null,                          // {"at": "...", "by": {...}} once confirmed / rejected / failed
  "result": null                            // after confirm: {"event_id", "status": "published", "public_url"}
                                            //   or {"draft_id", "event_id", "people", "pushed"};
                                            // failed: {"error", "message"}; disconnected app: {"reason": "app_disconnected"}
}
```

**What the page shows:**

- The heading is `title`.
- A card for `target`:
  - for an event: its date, venue, fee, capacity, and a link to it;
  - for an announcement: a preview of its title and body, its audience, and "reaches N people, M with the app".
- `effects` as a list.
- "Asked by `requested.app` for `requested.by`, at `requested.at`".
- "Expires `expires_at`".
- When `can_decide` is true, two buttons: **Approve** and **Turn down**.
- In every other state, the outcome instead:
  - `confirmed`, `rejected` or `failed`: show `decided` and `result`;
  - `expired`: "this request expired, ask the assistant again";
  - `superseded`: "a newer request replaced this one".

Render the announcement's `title` and `body` as plain text. An assistant
wrote them.

**Approve:** `POST /api/v1/clubs/pending-actions/{token}/confirm`, with no
body. It returns 200 and the same shape, with `status: "confirmed"` and
`result`.

**Turn down:** `POST /api/v1/clubs/pending-actions/{token}/reject`, with no
body. It returns 200 and the same shape, with `status: "rejected"`. A
rejected announcement stays a draft.

**Errors.** All of them use the API's usual error body, `{message, error_code, errors}`.

| Status | `error_code` | When / what to show |
|---|---|---|
| 401 | | Not signed in: go to login. |
| 403 | `NOT_A_CLUB` | An admin account: there is no club. |
| 403 | `ROLE_FORBIDDEN` | The role can't approve (a door member's POST). |
| 403 | `PLAN_LIMIT_REACHED` | Sending: over the announcements this month. `errors.upgrade_url`. The action stays pending. |
| 404 | `ACTION_NOT_FOUND` | Unknown token, or it is another club's. |
| 404 / 409 | `EVENT_NOT_FOUND`, `EVENT_NOT_DRAFT`, `DRAFT_NOT_FOUND`, `DRAFT_NOT_PENDING` | The target changed meanwhile. The action is now `failed`; reload. |
| 409 | `ACTION_DECIDED` | Already confirmed, rejected, superseded or failed: reload and show it. |
| 410 | `ACTION_EXPIRED` | Older than 24 hours. |
| 429 | | 60 reads or 30 decisions a minute per account. |
| 503 | `MCP_DISABLED` | The kill switch is off: nothing can be approved. |

### 7.2 Connected apps on the Security page

**List:** `GET /api/v1/clubs/connected-apps`

- Owner and admin see every app connected to the club.
- Any other member sees the apps they connected themselves.

It returns 200 with:

```jsonc
{
  "can_manage_all": true,
  "apps": [
    {
      "id": "1d0c…",                                  // the grant id: use it to disconnect
      "app": {"name": "Claude", "client_id": "mcp_…", "uri": "https://claude.ai", "logo_uri": null, "registration": "dcr"},
      "connected_by": {"type": "member", "member_id": 7, "name": "Eddie Editor", "email": "editor@…", "role": "editor"},
                     // or {"type": "owner"}
      "scopes": [{"key": "events:read", "label": "Read your events"}, …],
      "connected_at": "2026-10-20T01:24:03+00:00",
      "last_used_at": "2026-10-20T02:10:44+00:00",     // updated at most once a minute
      "pending_actions": 1                             // requests still waiting for approval
    }
  ]
}
```

Show, for each app:

- the name and host;
- who connected it;
- the scopes, as a short list of labels;
- connected and last used, as relative times;
- the pending count;
- a **Disconnect** button.

**Disconnect:** `DELETE /api/v1/clubs/connected-apps/{id}`. It returns 200
`{"disconnected": true, "id": "…"}`. It returns 404 `APP_NOT_FOUND` when:

- the id is unknown or already disconnected;
- the app is another club's;
- the caller is a member and the app is not theirs.

Disconnecting:

- ends every token the app holds, at once;
- rejects the app's pending actions.

**Sign-in history.** Sign-ins made to connect an app already appear in the
Security page's history, from `GET /api/v1/auth/security/overview`, with method `mcp`. The page
could label them "Connected an AI assistant".

---

## 8. Safety, in one place

| Concern | How |
|---|---|
| Acting beyond the person | Every tool stands for a dashboard request and is allowed only when the role, read on each call, may make it (`club_access/policy.py`). |
| Removed members, suspended clubs, password changes | The grant ends on the next request or refresh (`access.py`). |
| Another club's data | Every query is filtered by the token's club. Another club's id answers "not found". |
| Publishing or sending without a person | No tool does either. `request_*` makes a pending action that a person confirms in the dashboard. |
| Attendee contacts leaving the platform | Masked unless attendees:read **and** owner, admin or editor. Contacts typed into form answers are masked too. |
| Prompt injection through club data | People's text is returned only as `{"user_written": true, "text": …}`, and the instructions say it is data. |
| Token theft | Tokens are stored as digests, short-lived and audience-bound. Refresh tokens rotate, and reuse ends the grant. Each token can be revoked. |
| Floods | Per-app limit `60/minute;2000/day` (429 with Retry-After). Registration: 20 an hour per address. Sign-in: 20 a minute per address, plus the per-account lockout. |
| Audit | Every write leaves a row in `admin_audit_logs`, with `actor_id` set to the club account and `details` holding `via`, `client`, `client_id`, `grant_id`, `member_id`, `member_email` and `tool`. The actions are `mcp.app.connected`, `mcp.event.draft_created`, `mcp.event.draft_updated`, `mcp.store.item_created`, `mcp.store.offers_changed`, `mcp.announcement.drafted` and `mcp.action.requested`. Decisions are `mcp.action.confirmed` and `mcp.action.rejected` (`via: dashboard`, `requested_via: mcp`), and disconnecting is `mcp.app.disconnected`. |
| Plan | `plan_features.mcp`, on for Growth, Pro, Campus and Enterprise. It is a club-wide gate: checked at consent and on every tool call (`PLAN_FEATURE_LOCKED`, with the cheapest plan that has it). A club that drops below Growth keeps its connections, but every tool says why it can't. |
| Kill switch | `APP_MCP_ENABLED=false`: `/mcp`, `/oauth/*` and the discovery documents answer 404, and confirming answers 503. Listing and disconnecting apps still work. |

---

## 9. Ops

### Turning it on

1. **Migrate.** Run the `mcp_server` migration. The chain is club_merch → ai_assist → mcp_server. It:
   - adds six tables (`mcp_clients`, `mcp_grants`, `mcp_auth_codes`, `mcp_tokens`, `mcp_pending_actions`, `announcement_drafts`);
   - adds `plan_features.mcp`, and switches it on for growth, pro, campus and enterprise.

   Every table is new. `plan_features` is a handful of rows.

   The migration was proved on a scratch database: empty → head, down one, up again. The migrated tables match the models.
2. **Deploy the image.** New pins:
   - `mcp==2.3.0`, `mcp-types`;
   - the SDK's HTTP client, `httpx2` and `httpcore2`, plus `truststore`;
   - `sse-starlette`, `jsonschema`, `opentelemetry-api`.

   Bumps the SDK needs: pydantic 2.10.6 → 2.12.5 (AI assist's SDKs need it too), uvicorn 0.29.0 → 0.54.0.
3. **Set `APP_MCP_ENABLED=true`.** The rest have working defaults:

| Setting | Default | |
|---|---|---|
| `APP_MCP_ENABLED` | `false` | The kill switch. |
| `APP_PUBLIC_API_URL` | `https://api.myotherapp.in` | The issuer. `<this>/mcp` is the resource every token is bound to. It must be the public https origin. |
| `APP_MCP_DASHBOARD_URL` | `https://club-dashboard.myotherapp.in` | Used for `/confirm/{token}`, `/events/{id}` and `/merchandise` links. |
| `APP_MCP_ACCESS_TOKEN_MINUTES` | `60` | |
| `APP_MCP_REFRESH_TOKEN_DAYS` | `30` | From each rotation. |
| `APP_MCP_RATE_LIMIT` | `60/minute;2000/day` | Per connected app. |
| `APP_MCP_ALLOWED_ORIGINS` | `https://claude.ai,https://chatgpt.com,https://chat.openai.com` | For browser pages calling `/mcp` directly. |
| `APP_MCP_CLIENT_METADATA_DOCUMENTS` | `true` | Fetch https client_id documents. |

**nginx.** `location /` already proxies `/mcp`, `/oauth/*` and
`/.well-known/*` to the app. The answers are plain JSON, so nothing needs
unbuffering.

**Rate limits.** The rate limits use the API's limiter storage: Redis when
`APP_REDIS_URL` is set, otherwise per process. With several workers and no
Redis, each worker counts its own.

**The SDK.** The server is the SDK's low-level `Server` with a fresh
sessionless `StreamableHTTPSessionManager` per request (`server.py`). This
keeps the request's context (plan-gate cache, request id) visible to the
handlers, and needs no lifespan hook.

### Checking it by hand

```bash
curl -s https://api.myotherapp.in/.well-known/oauth-protected-resource/mcp
curl -si -X POST https://api.myotherapp.in/mcp -H 'content-type: application/json' \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'     # 401 + WWW-Authenticate
npx @modelcontextprotocol/inspector@2.9.0                    # web UI: Streamable HTTP, URL …/mcp, Connect
npx @modelcontextprotocol/inspector@2.9.0 --cli --server-url https://api.myotherapp.in/mcp \
     --transport http --header "Authorization: Bearer moa_at_…" --method tools/list --strict
```

The Inspector keeps the OAuth tokens from its web UI in the OS keychain,
keyed by server URL. Its CLI then uses those for the same URL, whatever
`--header` says. To try another person's token, use a different spelling of
the URL, such as `127.0.0.1` for `localhost`.

### Revoking everything

To end every connection now:

```sql
UPDATE mcp_grants SET revoked_at = now(), revoked_reason = 'ops' WHERE revoked_at IS NULL;
UPDATE mcp_tokens SET revoked_at = now() WHERE revoked_at IS NULL;
```

To end one club's connections, add `club_id = …` to the first statement,
and `grant_id IN (SELECT id FROM mcp_grants WHERE club_id = …)` to the
second.

### Housekeeping

Expired codes and tokens, and revoked grants, stay in their tables. No sweep
removes them yet. They are small rows: one per connect or refresh. A later
change can add them to `app/api/auth/retention.py`'s hourly sweep:

```sql
DELETE FROM mcp_auth_codes WHERE expires_at < now() - interval '1 day';
DELETE FROM mcp_tokens WHERE expires_at < now() - interval '1 day';
```

Clients that registered themselves but never connected also stay, in
`mcp_clients` with no grant. Registration is rate-limited per address.

### Where this differs from the letter of the spec, and why

- **Redirect URIs.**
  - **Deviation:** a native app's own scheme (`cursor://…`) is allowed, beside https and loopback http.
  - **Spec:** the MCP spec says redirect URIs MUST be localhost or https.
  - **Why:** Cursor registers `cursor://anysphere.cursor-mcp/oauth/callback`, and RFC 8252 allows private-use schemes.
  - **Safeguards:** schemes that run code or open files are refused, and the consent page warns.
- **Dynamic registration.**
  - **Deviation:** still supported.
  - **Spec:** deprecated.
  - **Why:** Claude and ChatGPT connectors use it. Client ID Metadata Documents, the spec's preferred mechanism, are supported as well.
- **No grace window for a lost refresh response.** A client that retries a refresh after losing the answer is disconnected and must connect again. That is the price of detecting refresh-token reuse.
