from app.api.orgs.models import *
from app.api.users.models import *
from app.api.clubs.models import *
from app.api.events.models import *
from app.api.interests.models import *
from app.api.notifications.models import *
from app.api.payments.models import *
from app.api.payment_gateways import models as _payment_gateway_models  # noqa: F401
from app.api.subscriptions.models import *
from app.api.events.volunteer.models import *
from app.api.models import *
from app.api.ticket_templates.models import *
from app.api.certificate_templates.models import *
from app.api.badges.models import *
from app.api.audit.models import *
from app.api.admin_alerts import models as _admin_alert_models  # noqa: F401
# The club dashboard's bell (app/api/club_alerts).
from app.api.club_alerts import models as _club_alert_models  # noqa: F401
# Monthly message counters, statements and Enterprise clubs' own limits (app/api/message_quota).
from app.api.message_quota import models as _message_quota_models  # noqa: F401
from app.api.deliveries.models import *
from app.api.certificates import models as _certificate_models  # noqa: F401
from app.api.emails.models import *
from app.api.checkins.models import *
from app.api.idcards.models import *
from app.api.kiosk.models import *
from app.api.agenda.models import *
from app.api.merch.models import *
from app.api.closeout.models import *
from app.api.auth.sessions import UserSessions  # noqa: F401
from app.api.auth.verification import EmailVerificationCodes  # noqa: F401
from app.api.auth.password_reset import PasswordResetCodes  # noqa: F401
from app.api.links import models as _links_models  # noqa: F401
from app.api.finance import models as _finance_models  # noqa: F401
from app.api.security import models as _security_models  # noqa: F401
from app.api.threads import models as _threads_models  # noqa: F401
from app.api.store import models as _store_models  # noqa: F401
# Ticket types, discount codes and waiting lists (app/api/ticketing).
from app.api.ticketing import models as _ticketing_models  # noqa: F401
from app.api.live import models as _live_models  # noqa: F401
from app.api.competitions import models as _competition_models  # noqa: F401
from app.api.sites import models as _site_models  # noqa: F401
from app.api.club_access import models as _club_access_models  # noqa: F401
from app.api.club_domains import models as _club_domain_models  # noqa: F401
# A club's own WhatsApp number: the connection, quality, templates, inbox (app/api/club_whatsapp).
from app.api.club_whatsapp import models as _club_whatsapp_models  # noqa: F401
from app.api.club_whatsapp import inbox_models as _club_whatsapp_inbox_models  # noqa: F401
# Its spend, consents and broadcasts (app/api/club_whatsapp/reach_models.py).
from app.api.club_whatsapp import reach_models as _club_whatsapp_reach_models  # noqa: F401
# A club's mailbox: its address, threads, mail and files (app/api/club_mailbox).
from app.api.club_mailbox import models as _club_mailbox_models  # noqa: F401
# Results, cancellations, club refunds, guest notices, club password reset codes (app/api/club_ops).
from app.api.club_ops import models as _club_ops_models  # noqa: F401
# Connected AI assistants: clients, grants, tokens, pending actions, announcement drafts (app/api/mcp).
from app.api.mcp import models as _mcp_models  # noqa: F401
from app.api.organisers import models as _organiser_models  # noqa: F401
from app.api.discover.geocode import GeocodeCache  # noqa: F401
from app.api.discover.stats import DiscoverStats  # noqa: F401
# The triggers that keep clubs.place_lat/place_lng, installed with the events table.
from app.api.discover import places as _club_places  # noqa: F401
from app.api.reports import models as _report_models  # noqa: F401
from app.api.ai import models as _ai_models  # noqa: F401
# The job queue and its runners' heartbeats (app/core/jobs.py).
from app.core import jobs as _jobs  # noqa: F401
from app.api.ledger import models as _ledger_models  # noqa: F401
# The admin portal's operator tools: settings, invites, payouts, fees, refunds, disputes (app/api/admin_ops).
from app.api.admin_ops import models as _admin_ops_models  # noqa: F401
# search_text, the search outbox and the grid cells: their triggers go in after every table.
from app.api.search import outbox as _search_outbox, text as _search_text  # noqa: F401
from app.api.discover import cells as _geo_cells  # noqa: F401
