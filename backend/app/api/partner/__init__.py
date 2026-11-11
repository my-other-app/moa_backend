"""The Integrations API: other platforms working with MyOtherApp, both ways.

feature-register/integrations-api-contract.md is the shared truth;
docs/INTEGRATIONS_API.md the whole picture. In short:

- `/partner/v1/...` (router.py) is the public, versioned surface. Every
  change made through it goes through the same domain services the
  dashboard and the app use (live.py), so capacity, approvals, waiting
  lists, emails and webhooks behave the same whoever made the change.
  Test credentials work on a sandbox instead (sandbox.py): nothing of
  theirs reaches the real tables, the public, a guest's inbox or a plan's
  counts.
- Two ways in (auth.py): a club's own API keys (keys.py), and partner apps
  a platform admin registered, connected by a club over OAuth 2.0 with
  PKCE (apps.py, oauth.py).
- Nothing is on by default: the platform admin allocates API access to a
  club (allocation.py); the plan key `api_access` is declared for later.
- Money never moves through the API (live.py's registration rules).

Club dashboard routes: /api/v1/clubs/integrations/... (club_router.py);
admin routes: /api/v1/admin/integrations/... (admin_router.py); OAuth at
/partner/oauth/... (oauth_router.py).
"""
