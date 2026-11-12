"""Outgoing webhooks: MyOtherApp telling a club's own systems, or a partner
app acting for the club, what changed (docs/WEBHOOKS.md,
feature-register/integrations-api-contract.md).

- `emit` (emit.py) is called by the domain changes themselves -- an event
  made, a registration taken, a check-in, a refund -- wherever they happen;
  `signals` wraps it for each kind of change. It writes the change in the
  caller's transaction, so a rollback sends nothing, and it is sent only
  after that commits.
- delivery.py builds each change once, finds the endpoints that want it, and
  sends it to each (signed, retried, an endpoint failing for three days
  turned off and its owner told).
- router.py: the club dashboard's and the admin portal's routes; partner.py:
  the same endpoints on the partner API (/partner/v1/webhook-endpoints),
  mounted by the partner router.
"""
from app.api.webhooks_out.emit import emit  # noqa: F401
