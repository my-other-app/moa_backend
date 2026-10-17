"""Alerts for the platform's admins: the bell in the admin app, a mail to
ADMIN_ALERT_EMAILS and a post to ADMIN_ALERT_DISCORD_WEBHOOK."""

from app.api.admin_alerts.service import KINDS, notify

__all__ = ["KINDS", "notify"]
