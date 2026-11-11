# Support desk and sales leads

The admin portal's "Team work": a **support desk**, where every question from
an app user, a guest or a club becomes a case with an owner, and **leads**,
where the sales team tracks clubs, sponsors, venue partners and vendors from
first contact to signed, with every follow-up on a date.

The cross-repo contract is `feature-register/support-leads-contract.md`; the
spec is "Feature 2" in `features-we-are-planning-to-add.md`. Code:
`backend/app/api/support/` and `backend/app/api/leads/`. Migration
`support_desk`.

## Teams and rights

Two teams, `support` and `sales` (`admin_teams`, reference rows), on top of
the admin roles: the role says what an admin may change on the platform, the
team which queue they work. Someone who is not an admin today gets an admin
account with the `viewer` role and a team: membership alone gives the desk's
rights below and nothing else (a viewer in the support team still cannot
refund, suspend or change a club; those routes keep their own capabilities).

| Who | Support cases | Leads |
| --- | --- | --- |
| Admin owner | everything: teams, topics and rules, reassigning | everything, reassigning included |
| Support team | read every case and its messages; answer, change and close the ones assigned to them; take unassigned ones; notes on any; log a case by hand | read; turn a case into a lead |
| Sales team | read every case and its messages | their own leads: edit, log, move stages, files, contacts; take an unowned one; add leads |
| Any other admin | the lists and counts, never a message's body (`hidden: true`) | read |

Helpers: `app.core.auth.permissions.teams_of / in_team / is_desk_owner`;
`app.api.support.teams.Rights` is computed once a request. Refusals:
`403 TEAM_REQUIRED`, `OWNER_REQUIRED`, `NOT_ASSIGNED_TO_YOU`, `NOT_YOUR_LEAD`.
Excel exports of the case and lead lists are for the teams and the owners.
The desk's settings (team members, topics and their rules, saved replies,
help answers, lost reasons, working hours and retention) are owner-only and
ask for the owner's capability (`MANAGE_ADMINS`) at the door: 403
`INSUFFICIENT_ADMIN_ROLE`. The rest is gated by team in each handler, and
`tests/test_permission_hardening.py` lists those routes (`TEAM_DESK_WRITES`) with why.

**Away.** `PATCH /admin/teams/me {away, away_until?}`: out of round robin;
their unfinished cases go back to Unassigned. The sweep brings them back once
`away_until` passes. Removing someone from a team does the same, and a sales
member who leaves has their open leads' owner emptied for an owner to reassign.

## Support cases

**Doors.** The app (`channel app`), the website (`web`), the club dashboard
(`dashboard`) -- all `POST /api/v1/support/requests` -- mail to support@
(`email`), and a case logged by hand (`manual`, `POST /admin/support/cases`).
A signed-in app account is linked (`requester_kind user`); a club's session
links the club and, for a team member's session, the member
(`requester_kind club`); a guest gives an email and a name (`guest`). Every
club member, door staff included, may use `/api/v1/support`
(`club_access.policy`).

**Number.** `support_case_number_seq`, from 1001, shown `#1042`; every mail
about the case carries `[#1042]` in its subject.

**Assignment.** By the topic's rule: a named support member while available,
else round robin across the support team (the member given work longest ago),
skipping anyone away; nobody available leaves it Unassigned.

**Statuses.** `new -> open -> waiting (we answered) -> open (they answered) ...
-> resolved -> closed`. A reply from the person within 7 days of Resolved
reopens the case; to a closed case, or later, it opens a new case with
`parent_case_id` pointing at the old. The same person writing about the same
subject within an hour joins the case they have. On Resolved the person gets
"Did we solve it?" (yes/no links to `https://myotherapp.in/help/survey/{token}?solved=yes|no`;
the website posts `POST /api/v1/support/survey/{token}`); "no" reopens it.

**Timers.** In working hours (default Monday-Saturday 09:30-18:30 IST; the
owners change them under Settings): `first_reply_due` (default 4 working
hours) and `resolution_due` (default 18: two working days). A case is
`overdue` when it has no first reply past the first, or is unfinished past
the second.

**Replies.** `POST /admin/support/cases/{id}/messages {kind: reply_out|note,
body (markdown-lite), attachments?, saved_reply_id?, preview_only?}`.
`preview_only` answers the subject, text and HTML the person would get. A
reply is a job (`support.reply_mail`): from `SUPPORT_FROM_ADDRESS`, subject
`Re: [#n] <subject>`, a Message-ID of ours, `In-Reply-To` the person's last
mail and `References` the chain, its files attached; an app account also gets
the push `support_reply` (data `{type, case_id, number}`). Markdown-lite:
`**bold**`, `*italic*`, `` `code` ``, `[words](https://...)`, bare links,
`- ` lists, paragraphs; everything else is escaped. Saved replies fill
`{name}`, `{number}`, `{subject}`.

**Not delivered.** A provider refusal, a suppressed address, or a bounce or
complaint later (`hooks/service.record` -> `support.mail.provider_verdict`)
marks the reply, flags the case `not_delivered`, puts it back in Open, and
adds a line suggesting another way to reach them.

**Privacy.** Card-like numbers (13-19 digits passing Luhn) are masked before
anything is stored, in the form's message and subject and in inbound mail.
Files are private (`support/...` in the media bucket), read only through the
API with headers that run nothing (the club mailbox's `media_headers`).
Notes never reach the person. The audit log records every change -- never a
word anyone wrote.

**Panel.** `GET /admin/support/cases/{id}` carries `links` (user, club, event,
registration, payment, refund, dispute) and `panel`: the account (sign-in
method, verified, suspended), the latest registrations and tickets, payments,
refunds and disputes, the clubs they run (their club account, and clubs where
they are an active team member), and the requester's club -- each with the
admin portal's path (`/users/{id}`, `/clubs/{id}`, `/events/{id}`,
`/payments?q=...`). A guest is matched to an app account by address
(`matched_by: email`).

**Tabs.** `mine`, `unassigned`, `open`, `waiting`, `resolved`, `all`, and
`spam` (mail SES called spam: closed, tagged `spam`, in no other tab; taking
the tag off puts it in the queue).

## Mail in: support@ and sales@

Through the club mailbox's inbound path (SES receipt rule -> the inbound
bucket -> SNS -> `POST /api/v1/hooks/mailbox/ses`): the hook reads each mail
once and hands it to the desk first (`support/inbound.py`), then to the
clubs. **Dormant** until `SUPPORT_INBOUND_ADDRESSES` / `SALES_INBOUND_ADDRESSES`
are set: until then nothing is read for them.

- support@: a virus is dropped; spam is kept closed (Spam tab). Otherwise it
  joins its case by thread headers (a Message-ID we sent or received), or by
  `[#n]` when the sender is the case's own person (a number alone is not a
  key), or the same sender's case on the same subject within the hour; else a
  new case, the sender matched to an app account or a club and flagged
  `unknown_sender` when neither. Mail from our own addresses is dropped.
- sales@ (often a BCC): an `email` line on each lead with a contact among the
  people on the mail (ours left out: the desk's addresses and admin accounts;
  `direction` out when an admin sent it), files on the lead; or a new lead
  (source `inbound_email`) with a follow-up for the sales round robin.
- Each mail is kept once, however often SNS delivers the notice; nothing a
  mail says reaches a log.

## Leads

A lead: name, type (`club|sponsor|venue|vendor|other`), stage
(`new|contacted|meeting|proposal|negotiation|won|lost`), source, owner, city,
website, value (INR a year), expected close, tags, contacts, timeline, files,
follow-ups, and the platform records it links to (the club and its
subscription; event and club partners of the same name for sponsors and
vendors; events at the venue for venues).

**Duplicates.** The same name (and city when both have one), or the same
organisation mail domain (from the website or a contact; free mail services
never count). `POST /admin/leads` answers `409 DUPLICATE_LEAD {matches}`
unless `force: true`; the import marks the row `duplicate`; "Talk to us" and
a club's own sign-up attach to the lead we have.

**Stages.** Lost needs a `lost_reason` (a key from `lead_lost_reasons`) and may
carry `retry_on`, which makes a "Try again" follow-up; won and lost mark what
was left to do as done. Moving to an open stage with no open follow-up
answers `needs_follow_up: true`; completing a follow-up on an open lead with
none left answers `needs_next: true`.

**Doors.** By hand; the import (`POST /admin/leads/import`, xlsx or csv, a
`dry_run` first; columns in `GET /admin/leads/import/template`); "Talk to us"
(`POST /api/v1/leads/talk-to-us`, public, 5 an hour per address); a club that
applies by itself (`POST /clubs/create` -> a Club lead, source `signup`); a
support case (`POST /admin/support/cases/{id}/to-lead`); mail to sales@.

**Merge.** `POST /admin/leads/{id}/merge {into_id}`: contacts (one per
address), timelines, files, follow-ups and linked cases move; the merged
lead points at the other and leaves the lists.

## Follow-ups, My day, the badge, the reports

`POST /admin/follow-ups {lead_id|case_id, kind, title, due_at, owner_id?}`,
`PATCH /admin/follow-ups/{id}`. My day (`GET /admin/teamwork/my-day`): today's
(India's day) and overdue follow-ups, and the admin's unfinished cases. The
badge (`GET /admin/teamwork/badge`): `{due, overdue, cases}` (cases still
needing an answer). Reports (`GET /admin/teamwork/reports?from&to`, `+format=xlsx`):
median first reply and resolution hours, how many on target, by person and
topic, the survey; the pipeline by type and stage, won and lost by reason,
value by stage, follow-ups done on time.

## The sweep and the jobs

`support/sweep.py`, every ten minutes under advisory lock `SUPPORT_SWEEP`:

| Step | What | Once because |
| --- | --- | --- |
| digest | from 08:00 IST, a `support.digest_mail` job per team member (sent only when they have something) | `scheduled_runs` row `support.digest` holds the day; dedupe key per person and day |
| overdue | follow-ups 3 days overdue -> one mail to the admin owners | `follow_ups.overdue_flagged_at`, set with the job |
| waiting | day 5: reminder mail; day 7: closed, `no_reply` | `reminded_at`; the status |
| resolved | resolved 7 days: closed | the status |
| retention | closed for `retention_days` (default 365): messages and files deleted, counts kept (`purged_at`) | `purged_at` |
| uploads | help-form uploads never sent, after a day: deleted | the row goes |
| back | `away_until` passed: back | `away` |

Jobs: `support.reply_mail`, `support.reply_push`, `support.receipt_mail`,
`support.survey_mail`, `support.reminder_mail`, `support.digest_mail`,
`support.overdue_mail`. Each checks its row first and sends with an
idempotency key, so a run again sends nothing twice.

## Settings and owner steps

Settings (`APP_` prefix in the environment), all optional:

- `SUPPORT_INBOUND_ADDRESSES` -- e.g. `support@myotherapp.in`. Empty: dormant.
- `SALES_INBOUND_ADDRESSES` -- e.g. `sales@myotherapp.in`. Empty: dormant.
- `SUPPORT_FROM_ADDRESS` (default `support@myotherapp.in`), `SALES_FROM_ADDRESS`
  (default `sales@myotherapp.in`): must be senders the mail provider has verified.

The desk's own settings (working hours, retention, targets) live in the
database (`admin_teams.settings` on `support`), changed by an owner at
`PUT /admin/support/settings`.

To receive mail: an SES receipt rule for support@ and sales@ writing to the
club mailbox's inbound bucket and notifying its SNS topic (the same as the
club mailbox's; OPERATIONS section 23), and the Cloudflare MX/routing for
those two addresses -- an owner's change, agreed first. Until then the desk
works from the app, the website, the dashboard and by hand, and replies still
go out by mail.

## Data

`admin_teams`, `admin_team_members`, `support_topics`, `support_saved_replies`,
`support_help_answers`, `support_cases`, `support_messages`,
`support_attachments`, `leads`, `lead_contacts`, `lead_activities`,
`lead_files`, `follow_ups`, `lead_lost_reasons`. Reference rows from the
migration: the two teams, the seven topics (account, tickets, payments,
dashboard, report, bug, other; payments and report are high priority) and
five lost reasons. The demo reset keeps those, the saved replies and help
answers, and the admins' team memberships; it wipes cases, messages, files,
leads and follow-ups.
