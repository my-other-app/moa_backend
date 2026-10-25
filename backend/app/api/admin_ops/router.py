"""Every route of the admin portal's operator tools, for app/api/router.py."""
from fastapi import APIRouter

from app.api.admin_ops import admins, disputes, jobs_admin, payouts, refunds, settings_router, suppressions, whatsapp_templates
from app.api.admin_ops import club_reject as _club_reject  # noqa: F401 - registers the rejection mail's job

router = APIRouter()
for sub in (
    settings_router.ops_router,
    settings_router.router,
    admins.router,
    admins.public_router,
    payouts.router,
    disputes.router,
    refunds.router,
    jobs_admin.router,
    suppressions.router,
    whatsapp_templates.router,
):
    router.include_router(sub)
