from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from pydantic import PostgresDsn, RedisDsn, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The public site's address before it moved to the bare domain; old links still come from it.
FORMER_SITE_ORIGIN = "https://events.myotherapp.in"

_IST = timezone(timedelta(hours=5, minutes=30))


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        case_sensitive=True,
        env_prefix="APP_",
        # A setting refused at startup is named, never printed: a secret
        # that fails a validator must not end up in the container's log.
        hide_input_in_errors=True,
    )

    SECRET_KEY: str
    WORKERS: int = 1
    PORT: int = 8000
    DEBUG: bool = False
    # Which deployment this is: production, staging, development, test...
    # Any but development, dev, local, test and ci (and, unset, any process
    # with DEBUG off) has its settings checked at startup for a missing,
    # placeholder, weak or half-set secret (app/core/config_checks.py;
    # docs/OPERATIONS.md, section 19). Sentry's environment, unless
    # SENTRY_ENVIRONMENT says otherwise.
    ENV: str = ""
    # What a failed check does. "warn" (the default): every finding is
    # logged at ERROR -- and so reaches Sentry -- and the process starts
    # anyway. "enforce": the process refuses to start. Set enforce once
    # `python -m app.core.config_checks` prints no FATAL line.
    CONFIG_CHECKS: str = "warn"
    # uvicorn's line per request. nginx in front already logs every request,
    # so this is a second copy, written on every request; false drops it.
    ACCESS_LOG: bool = True
    # A weak ETag on each 200 JSON answer to a GET or HEAD under /api/v1/,
    # and a 304 with no body when the caller's If-None-Match already names
    # it (app/core/middlewares/etag_middleware.py). The app keeps what it
    # read and asks again with the tag, so a list that has not changed costs
    # a few hundred bytes. False: no tags and no 304s, every answer as before.
    ETAGS: bool = True
    # One JSON object per log line (app/core/observability.py): time, level,
    # logger, message and the request id, for a log shipper. Off: uvicorn's
    # own format, as before. On (or SENTRY_DSN set), each request carries an
    # id in X-Request-ID, in and out, and a 500's track_id is that id.
    LOG_JSON: bool = False
    # Error tracking with Sentry (app/core/observability.py). Empty DSN: off,
    # and sentry_sdk is never imported. Personal data is scrubbed before an
    # event leaves. SENTRY_RELEASE defaults to the git sha the deploy writes
    # to backend/REVISION; SENTRY_ENVIRONMENT to ENV, else production.
    # SENTRY_SAMPLE_RATE: the share of errors sent;
    # SENTRY_TRACES_SAMPLE_RATE: the share of requests traced (0: none).
    SENTRY_DSN: str = ""
    SENTRY_ENVIRONMENT: str = ""
    SENTRY_RELEASE: str = ""
    SENTRY_SAMPLE_RATE: float = 1.0
    SENTRY_TRACES_SAMPLE_RATE: float = 0.0
    # Log every SQL statement with its parameters (app/db/core.py). Off by
    # default: the parameters are personal data.
    SQL_LOG: bool = False
    CORS_ORIGINS: list[str] | str
    APP_VERSION: str = "1.0"

    DATABASE_URL: str
    DATABASE_URL_SYNC: str
    # Connections each worker process may hold: DB_POOL_SIZE kept open, up to
    # DB_MAX_OVERFLOW more under load. Every worker has its own pool, so
    # WORKERS x (DB_POOL_SIZE + DB_MAX_OVERFLOW) must fit in Postgres's
    # max_connections; startup logs a warning when it does not
    # (app.db.core.check_connection_budget). Behind PgBouncer: 5 and 5.
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    # Which process this is: "api" (main.py, uvicorn's workers) or "worker"
    # (`python -m app.worker` sets it before the settings are read). It picks
    # the pool (app.db.core.pool_for) and tags Sentry's events.
    PROCESS: str = "api"
    # The job worker's own pool (the `worker` service in
    # docker-compose.prod.yml). It runs at most JOBS_CONCURRENCY jobs (2
    # there), and a mail send holds a connection only for each short
    # transaction, never while rendering or waiting on the provider, so it
    # needs far fewer than an API worker; a burst waits for a connection
    # rather than failing. Every process reads both pools, so the budget
    # check counts the worker on top of WORKERS x the API's pool wherever it
    # runs: in the worker, and in the API once APP_JOBS_WORKER or
    # APP_SWEEPS_IN_WORKER says there is one. docs/OPERATIONS.md, section 10.
    WORKER_DB_POOL_SIZE: int = 4
    WORKER_DB_MAX_OVERFLOW: int = 8
    # `alembic upgrade` runs beside the live API (app/db/migration_session.py
    # says why each exists). Postgres durations; "0" turns one off. A DDL
    # statement waits at most MIGRATION_LOCK_TIMEOUT for its lock, then the
    # migration's transaction rolls back and is tried again, up to
    # MIGRATION_LOCK_ATTEMPTS times, the pause doubling from
    # MIGRATION_RETRY_WAIT seconds. A statement may run
    # MIGRATION_STATEMENT_TIMEOUT, except an index built CONCURRENTLY (and the
    # other statements that block no one), which gets
    # MIGRATION_ONLINE_STATEMENT_TIMEOUT: none.
    MIGRATION_LOCK_TIMEOUT: str = "5s"
    MIGRATION_STATEMENT_TIMEOUT: str = "30s"
    MIGRATION_ONLINE_STATEMENT_TIMEOUT: str = "0"
    MIGRATION_IDLE_TIMEOUT: str = "60s"
    MIGRATION_LOCK_ATTEMPTS: int = 4
    MIGRATION_RETRY_WAIT: float = 5.0
    # Mounted from the host, deliberately outside the directory the deploy
    # rsyncs with --delete -- a backup inside it would be erased by the next
    # deploy, which is the opposite of the point.
    BACKUP_DIR: str = "/backups"
    # The off-site copy of every dump (app/api/backups/offsite.py): uploaded
    # to BACKUP_S3_BUCKET under BACKUP_S3_PREFIX, daily/ or weekly/, with
    # server-side encryption (BACKUP_S3_SSE: AES256, or aws:kms with
    # BACKUP_S3_KMS_KEY_ID) and the dump's sha256 in its metadata. Empty
    # bucket: no off-site copy, as before. BACKUP_S3_ACCESS_KEY and
    # BACKUP_S3_SECRET_KEY: a user that may put, list and read backups/ and
    # nothing else (no delete: the lifecycle rule expires them); unset, the
    # media bucket's S3_ACCESS_KEY and S3_SECRET_KEY.
    BACKUP_S3_BUCKET: str = ""
    BACKUP_S3_PREFIX: str = "backups/"
    BACKUP_S3_REGION: str = "ap-south-1"
    BACKUP_S3_SSE: str = "AES256"
    BACKUP_S3_KMS_KEY_ID: str = ""
    BACKUP_S3_ACCESS_KEY: str = ""
    BACKUP_S3_SECRET_KEY: str = ""
    # How many dumps scripts/backup.py leaves in BACKUP_DIR (the newest),
    # deleting older ones only after a good off-site upload. 0 keeps every one.
    BACKUP_KEEP_LOCAL: int = 0

    # The media bucket: posters, logos, photos, tickets, certificates, the
    # designer's uploads, mailbox files (app/core/storage/clients.py).
    S3_BUCKET: str
    S3_ACCESS_KEY: str
    S3_SECRET_KEY: str
    # A folder inside the bucket that every key is written under: a key
    # prefix, never an address (a link is made by the client from the
    # endpoint, the bucket and the key). Empty, the default: keys start at
    # the bucket's root, `clubs/logos/v-<hex>.png`, as production's always
    # have. Leave it empty on a store of our own (S3_ENDPOINT_URL set): its
    # links are then `<S3_PUBLIC_URL>/<S3_BUCKET>/clubs/logos/v-<hex>.png`,
    # and the club sites' image route, which serves keys under `clubs/` and `events/posters/`,
    # works. Set to `https://media.myotherapp.in/<bucket>`, every key would
    # begin with that text and its link would hold the address twice.
    S3_BASE_PATH: str = ""
    # A store of our own that speaks S3 (Garage on the production server),
    # for the media bucket only; all unset, AWS S3 as before. Off-site
    # backups (BACKUP_S3_*), the club mailbox's inbound bucket (MAILBOX_*)
    # and SES stay on AWS whatever these say.
    #   S3_ENDPOINT_URL  where the API reaches the store: uploads, reads,
    #                    deletes, listings (e.g. http://media:3900 inside
    #                    Docker). Set, every media client is path-style SigV4.
    #   S3_PUBLIC_URL    where browsers reach it: signed links are made for
    #                    this host (e.g. https://media.myotherapp.in, with
    #                    the proxy passing the Host header through). Unset:
    #                    S3_ENDPOINT_URL.
    #   S3_REGION        the region the store signs with (Garage's
    #                    s3_region, e.g. garage). Unset: boto3's default.
    # With an endpoint set, MEDIA_CDN_URL must not be the store's public
    # host (a CDN address is `<cdn>/<key>`, with no bucket in it), and
    # BACKUP_S3_ACCESS_KEY/_SECRET_KEY must be set (unset, backups would use
    # this store's keys on AWS). app/core/config_checks.py says so.
    S3_ENDPOINT_URL: str = ""
    S3_PUBLIC_URL: str = ""
    S3_REGION: str = ""

    @field_validator("S3_ENDPOINT_URL", "S3_PUBLIC_URL", "S3_REGION")
    @classmethod
    def _trimmed(cls, value: str) -> str:
        """Without spaces, and an address without its trailing slash. Its
        shape is checked by app/core/config_checks.py, which says so
        without stopping the process unless APP_CONFIG_CHECKS=enforce."""
        return (value or "").strip().rstrip("/")

    # SES's API keys. Empty is allowed so a server that sends through SES's
    # SMTP interface (MAIL_PROVIDER=ses_smtp) needs no API key; with
    # MAIL_PROVIDER=ses, app/core/config_checks.py says when they are missing.
    SES_ACCESS_KEY: str = ""
    SES_SECRET_KEY: str = ""
    # The From address of platform mail through SES (API or SMTP): an
    # identity verified in SES.
    SES_DEFAULT_SENDER: str
    # Shared secret in the SES → SNS hook URL (?token=), on top of Amazon's signature.
    SES_WEBHOOK_TOKEN: str = ""
    # Which provider sends mail: "ses" (default, SES's API), "ses_smtp" (SES
    # through its SMTP interface, with SMTP credentials and no API key), or
    # "resend", which falls back to SES when Resend refuses a mail (a 4xx) --
    # never when it may have taken it.
    MAIL_PROVIDER: str = "ses"
    # SES's SMTP interface (MAIL_PROVIDER=ses_smtp; app/core/email/ses_smtp.py):
    # the endpoint of the region the sender is verified in, STARTTLS on 587
    # (or 2587), and the SMTP credentials SES made for an IAM user -- not an
    # API key pair. The From address stays SES_DEFAULT_SENDER.
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    # Seconds the connection, and then each reply, may take before the mail is
    # tried again later.
    SMTP_TIMEOUT: float = 20.0
    RESEND_API_KEY: str = ""
    # The signing secret Resend shows for the webhook (whsec_…); without it the Resend hook refuses everything.
    RESEND_WEBHOOK_SECRET: str = ""
    # The sender for platform mail through Resend: an address on its verified domain.
    MAIL_FROM_ADDRESS: str = "info@myotherapp.in"
    MAIL_FROM_NAME: str = "MyOtherApp"
    # Where the SES identity lives. One client per process talks to it
    # (app/core/email/transport.py), so its TLS connections are reused.
    SES_REGION: str = "ap-south-1"
    # Mails a second this process hands to the provider (a token bucket,
    # app/core/email/pace.py). SES's sending quota starts at 14/s; with
    # WORKERS > 1 each worker paces itself, so set it to quota / WORKERS.
    MAIL_SEND_RATE: float = 14.0
    # Mails one send (a message, a batch of tickets) has in flight at once:
    # being rendered or waiting on the provider, all under the rate above.
    MAIL_CONCURRENCY: int = 8
    # Tries a mail gets while the provider is slow, throttling or down before
    # it is given up on (status "dead", and the admins are told). The wait
    # between tries doubles from a minute.
    MAIL_MAX_ATTEMPTS: int = 6
    # WhatsApp Cloud API: the hook's verify token and app secret, and the sending credentials. Empty = no provider.
    # With the token and the phone number id set, tickets, certificates and
    # the day-before reminder also go by WhatsApp to the guests who asked for
    # it (app/core/whatsapp.py); without them nothing is sent on the channel.
    WHATSAPP_VERIFY_TOKEN: str = ""
    WHATSAPP_APP_SECRET: str = ""
    WHATSAPP_ACCESS_TOKEN: str = ""
    WHATSAPP_PHONE_NUMBER_ID: str = ""
    # The platform number's WhatsApp Business Account id, for the admin's
    # template list (app/api/admin_ops/whatsapp_templates.py). Empty: read
    # from the access token's granted accounts (Meta's debug_token).
    WHATSAPP_BUSINESS_ACCOUNT_ID: str = ""
    # The Graph API version the calls name. Meta retires each about two years
    # after its release; move it on when Meta's changelog says so.
    WHATSAPP_GRAPH_VERSION: str = "v23.0"
    # The language code the templates were approved in.
    WHATSAPP_TEMPLATE_LANGUAGE: str = "en"
    # Seconds a call to Meta may take before it is given up on.
    WHATSAPP_TIMEOUT_SECONDS: float = 15.0
    # A club's own WhatsApp Business number (app/api/club_whatsapp, an
    # Enterprise feature): the Fernet key its access token, app secret and
    # webhook verify token are encrypted with. Its own secret, not
    # PAYMENT_KEYS_SECRET or AI_KEYS_SECRET, so one leaked key opens one kind
    # of club credential; a comma-separated list rotates (app/core/keyring.py).
    # Unset: no club can connect a number, and every WhatsApp send goes from
    # the platform's number, as before.
    WHATSAPP_KEYS_SECRET: str = ""
    # How often the sweep asks Meta for each club number's quality rating and
    # messaging limit (the club can also ask on demand).
    CLUB_WHATSAPP_QUALITY_HOURS: int = 6
    # The largest attachment the inbox fetches from Meta for the dashboard.
    CLUB_WHATSAPP_MEDIA_MAX_MB: int = 25
    # Messages a second a club's broadcast is sent at (Meta allows 80 a
    # second per number; this stays well under it).
    CLUB_WHATSAPP_BROADCAST_MPS: float = 10.0

    # ─── A club's mailbox: <handle>@mail.myotherapp.in (app/api/club_mailbox) ─
    # Pro and Enterprise (plan flag `mailbox`). Off until the owner has made
    # the SES domain, receipt rule, bucket, topic and DNS records
    # (docs/OPERATIONS.md section 23). Off: the dashboard hides Mail, nothing
    # can be set up or sent, and mail that arrives is dropped (never bounced).
    CLUB_MAILBOX: bool = False
    # The domain every club's address is on, verified in SES for sending and
    # receiving (its MX points at SES's inbound endpoint in the receiving region).
    MAILBOX_DOMAIN: str = "mail.myotherapp.in"
    # The private S3 bucket SES's receipt rule writes each mail to (raw MIME),
    # and the SNS topic the rule tells. A notification naming another bucket or
    # topic is refused.
    MAILBOX_INBOUND_BUCKET: str = ""
    MAILBOX_SNS_TOPIC_ARN: str = ""
    # Where SES receives the mail: Asia Pacific (Mumbai) receives (AWS's Email
    # Receiving endpoints table), so the same region as the platform's sending.
    MAILBOX_RECEIVING_REGION: str = "ap-south-1"
    # A user that may get (and delete) objects in the inbound bucket. Empty:
    # the SES keys (APP_SES_ACCESS_KEY / APP_SES_SECRET_KEY).
    MAILBOX_AWS_ACCESS_KEY: str = ""
    MAILBOX_AWS_SECRET_KEY: str = ""
    # A mail larger than this (MB) is dropped unread (SES receives up to 40 MB).
    MAILBOX_INBOUND_MAX_MB: int = 40
    # What one club may send, by the hour and by the day (abuse limits on top
    # of its email credits), and the most people one mail may go to.
    MAILBOX_SEND_PER_HOUR: int = 60
    MAILBOX_SEND_PER_DAY: int = 300
    MAILBOX_MAX_RECIPIENTS: int = 50
    # Meta's Embedded Signup ("Connect with Meta", app/api/club_whatsapp/
    # embedded_signup.py), once MyOtherApp is a Tech Provider: off until the
    # owner has Meta's approval (docs/OPERATIONS.md section 21). The platform
    # app's id and secret (App settings -> Basic), the login configuration's
    # id (Facebook Login for Business -> Configurations), and the token Meta
    # verifies the app's default webhook URL with. Pasting keys works either way.
    WHATSAPP_EMBEDDED_SIGNUP: bool = False
    META_APP_ID: str = ""
    META_APP_SECRET: str = ""
    META_ES_CONFIG_ID: str = ""
    META_WEBHOOK_VERIFY_TOKEN: str = ""

    GOOGLE_WEB_CLIENT_ID: str
    GOOGLE_ANDROID_CLIENT_ID: str
    GOOGLE_IOS_CLIENT_ID: str

    # Sign in with Apple: the audience an identity token must name, which is
    # the iOS app's bundle id (Team ID PX6QV826X4; the Android package is the same).
    APPLE_CLIENT_ID: str = "app.myotherapp.in"
    # Revoking a deleted account's Sign in with Apple token, which App Review
    # requires (guideline 5.1.1(v); app/api/auth/apple_tokens.py). The team id
    # (Apple developer -> Membership), and a key with Sign in with Apple
    # enabled (Certificates, Identifiers & Profiles -> Keys): its Key ID and
    # the .p8 file's contents (one line in .env, with \n for each line break).
    # APPLE_TOKENS_SECRET is the Fernet key the refresh tokens are kept under
    # (python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())").
    # Unset: sign-in works as before, but nothing is kept, so a deleted
    # account's token is not revoked.
    APPLE_TEAM_ID: str = ""
    APPLE_KEY_ID: str = ""
    APPLE_PRIVATE_KEY: str = ""
    APPLE_TOKENS_SECRET: str = ""
    # Seconds a call to Apple's token endpoints may take before it is given up on.
    APPLE_TIMEOUT_SECONDS: float = 10.0

    # Firebase Push Notifications
    # Option 1: Path to service account JSON file
    FIREBASE_SERVICE_ACCOUNT_PATH: str = ""
    # Option 2: Base64 encoded service account JSON (for environment variable)
    FIREBASE_SERVICE_ACCOUNT_BASE64: str = ""

    RAZORPAY_KEY_ID: str
    RAZORPAY_KEY_SECRET: str
    RAZORPAY_WEBHOOK_SECRET: str
    # How long one call to Razorpay may take, in seconds, before it is given
    # up on (app/api/payments/rzp.py). The SDK is synchronous: every call
    # runs in a worker thread, so the event loop keeps serving meanwhile.
    RAZORPAY_TIMEOUT: float = 20.0
    # A club's own Razorpay account (app/api/payment_gateways): the Fernet key
    # its saved key secret and webhook secret are encrypted with. A
    # comma-separated list rotates: the first encrypts, every one decrypts.
    # Unset: clubs cannot connect an account, and every payment goes to the
    # platform's, as before.
    PAYMENT_KEYS_SECRET: str = ""
    # Whether a club may connect rzp_test_ keys. Off in production: a test
    # key takes no real money, and an attendee would get a ticket for nothing.
    ALLOW_TEST_GATEWAY_KEYS: bool = False

    ACCESS_TOKEN_EXPIRE_MINUTES: int
    REFRESH_TOKEN_EXPIRE_DAYS: int

    DISCORD_ERROR_WEBHOOK: str | None = None
    # Who hears about billing and new club applications (app/api/admin_alerts):
    # a comma-separated list of addresses, each alert one mail to all of them.
    ADMIN_ALERT_EMAILS: str = "support@myotherapp.in"
    # The same alerts posted to a Discord channel. Unset: no post.
    ADMIN_ALERT_DISCORD_WEBHOOK: str | None = None
    # Addresses at these domains are never mailed and their accounts' devices
    # never pushed: the demo's invented people (scripts/demo) live at
    # demo.myotherapp.in, and the reminders, surveys and sweeps that run long
    # after a seed must not write to them. A JSON list or a comma-separated
    # string; see `mail_suppress_domains`.
    MAIL_SUPPRESS_DOMAINS: list[str] | str = ["demo.myotherapp.in"]

    # Where a link in an email points: the API as the public reaches it.
    PUBLIC_API_URL: str = "https://api.myotherapp.in"
    # What the links in emails are signed with (app/core/mail/links.py): a
    # guest's ticket and certificate links, the feedback survey's. They never
    # expire, so they must outlive a change of key: a comma-separated list,
    # the first signs and every one verifies (docs/OPERATIONS.md, section 5:
    # add the new key first, drop the old after 90 days). Unset: SECRET_KEY
    # signs them, as before this setting. Each at least 32 characters
    # (`openssl rand -hex 32`); a shorter one stops the app at startup.
    LINK_SIGNING_KEYS: str = ""
    # The clubs dashboard, for links a volunteer opens on a phone.
    PUBLIC_WEB_URL: str = "https://club-dashboard.myotherapp.in"
    # The admin portal, for an invited admin's set-password link.
    PUBLIC_ADMIN_URL: str = "https://main-portal.myotherapp.in"
    # The public site: an event's page is /events/{slug}, a club's /clubs/{slug}. Tracking links land here.
    PUBLIC_SITE_URL: str = "https://myotherapp.in"
    # Where an address is turned into a place, for the Security page and,
    # without GEOIP_MMDB_PATH, the discover site's "roughly where"; {ip} is
    # filled in. Empty disables it. ipapi.co's free tier is about a thousand
    # a day: after a refusal it rests (a minute, doubling to ten), and the
    # addresses it placed before keep their place meanwhile (discover/geo.py).
    GEOIP_URL: str = "https://ipapi.co/{ip}/json/"
    # A local MaxMind GeoLite2-City (or City) database, read in place of
    # GEOIP_URL for the discover site's "roughly where" (app/api/discover/geo.py):
    # no request leaves the box, nothing is waited for, and nothing is ever
    # refused. Unset, or a file that cannot be read: GEOIP_URL as before. Not
    # shipped; download it under MaxMind's licence and point this at the
    # .mmdb. In production: /geoip/GeoLite2-City.mmdb (docs/OPERATIONS.md,
    # section 16). Read once per process: a new file needs a restart.
    GEOIP_MMDB_PATH: str = ""
    # A comment is hidden from everyone but its writer once the open reports
    # on it weigh this much (app/api/reports): a report counts 1, or
    # REPORT_NEW_ACCOUNT_WEIGHT from an account younger than
    # REPORT_NEW_ACCOUNT_DAYS, so a handful of fresh accounts cannot hide
    # what they like.
    REPORTS_TO_HIDE: float = 3.0
    REPORT_NEW_ACCOUNT_DAYS: int = 7
    REPORT_NEW_ACCOUNT_WEIGHT: float = 0.5
    # Placing a venue typed without a map link (app/api/discover/geocode.py):
    # OpenStreetMap's Nominatim, at most one request a second, every answer and
    # every miss kept in geocode_cache. The address goes in the User-Agent, as
    # Nominatim's usage policy asks. GEOCODER_ENABLED=false stops it entirely.
    GEOCODER_ENABLED: bool = True
    GEOCODER_URL: str = "https://nominatim.openstreetmap.org/search"
    GEOCODER_EMAIL: str = "support@myotherapp.in"
    # Meilisearch (app/core/search.py, app/api/search): unset, every search
    # is Postgres's (search_text and its trigram indexes), as before. Set
    # (e.g. http://meilisearch:7700 and the instance's master or an admin
    # key), the search outbox is carried to it, the indexes are built once
    # by one process, and searches with words ask it -- falling back to
    # Postgres on any failure, and for SEARCH_BREAKER_SECONDS after
    # SEARCH_BREAKER_FAILURES failures in a row. SEARCH_TIMEOUT bounds each
    # call (seconds). SEARCH_INDEX_PREFIX names the indexes ({prefix}events,
    # {prefix}clubs), so two environments can share one instance. The indexes
    # are compared with the database every SEARCH_RECONCILE_HOURS.
    SEARCH_URL: str = ""
    SEARCH_KEY: str = ""
    SEARCH_TIMEOUT: float = 0.8
    SEARCH_BREAKER_FAILURES: int = 3
    SEARCH_BREAKER_SECONDS: float = 30.0
    SEARCH_INDEX_PREFIX: str = "moa_"
    SEARCH_RECONCILE_HOURS: float = 6.0
    PUBLIC_CLUB_URL: str = "https://myotherapp.in/clubs/{slug}"
    # A club's published website at its own address (served by the club-sites Cloudflare Worker).
    # PUBLIC_CLUB_URL stays the public site's page tracking links land on; it frames the same site.
    PUBLIC_CLUB_SITE_URL: str = "https://{slug}.myotherapp.in"
    # A club's own domain for its website (app/api/club_domains): a Cloudflare
    # for SaaS custom hostname on the myotherapp.in zone. The token needs
    # Zone > SSL and Certificates > Edit on that zone and nothing else. Either
    # unset: the feature is "unavailable" and nothing calls Cloudflare.
    CLOUDFLARE_API_TOKEN: str = ""
    CLOUDFLARE_ZONE_ID: str = ""
    # What a club's CNAME points at: the zone's fallback origin for custom hostnames.
    SITES_CNAME_TARGET: str = "sites.myotherapp.in"

    # Documents (app/core/layout/pool.py): tickets, certificates, ID cards,
    # previews and wallet passes are drawn on RENDER_THREADS threads of their
    # own, never on the event loop and never on the default pool that the
    # Razorpay, SES and S3 calls wait in. A wkhtmltopdf, wkhtmltoimage or
    # openssl run longer than RENDER_TIMEOUT seconds is killed.
    # Per process, so the box runs up to APP_WORKERS x this at once (and the
    # worker's own, when it runs). 2, not 3: reportlab draws under the GIL,
    # so on an idle 8-core laptop a third thread added a tenth to a burst's
    # renders a second and doubled how late the process's event loop
    # answered meanwhile (p99 20 -> 40 ms; scripts/bench/render_threads.py,
    # docs/OPERATIONS.md section 10) -- and the box has two vCPUs for three
    # processes, so there is no spare core for it. Each HTML design or
    # receipt is a wkhtmltopdf process besides: 6 at once, not 9.
    RENDER_THREADS: int = 2
    RENDER_TIMEOUT: float = 30.0
    # Bytes of bucket images (logos, artwork, posters) each process keeps for
    # the renderer; the least recently used go first. One object larger than
    # RENDER_IMAGE_CACHE_ITEM_MB is never kept.
    RENDER_IMAGE_CACHE_MB: int = 64
    RENDER_IMAGE_CACHE_ITEM_MB: int = 8
    # A rendered ticket or certificate PDF is kept in the bucket under
    # docs/{kind}/{registration}/{design version}-{hash}.pdf, so a download,
    # a resend or a bundle reuses it instead of drawing it again.
    RENDER_DOC_CACHE: bool = True
    # One process draws a document nobody has kept yet; the others asking
    # for it at the same moment wait for it (app/api/events/doc_cache.py), at
    # most this many seconds, then draw it themselves rather than fail.
    DOC_RENDER_WAIT_SECONDS: float = 20.0
    # Artwork, logos, signatures and pictures uploaded for a printed design
    # are stored with their longest side at most this many pixels (about 200
    # dpi across an A4 certificate), as a JPEG or, with transparency, a PNG
    # (app/core/storage/doc_art.py).
    DOC_ART_MAX_PX: int = 2400
    # Kept documents older than this many days are deleted, once a day
    # (app/api/events/doc_cache_sweep.py); a later download draws it again.
    # 0 (the default): never. Production turns it on on purpose (e.g. 90),
    # and not beside a bucket lifecycle rule on docs/ with another day count
    # (docs/OPERATIONS.md, section 13).
    DOC_CACHE_DAYS: int = 0
    # At most this many kept documents deleted in one day's run.
    DOC_CACHE_MAX_DELETE_PER_RUN: int = 50000
    # An admin alert when more than this percentage of what the sweep finds
    # under docs/ is not a kept document (and so is left alone).
    DOC_CACHE_SKIP_ALERT_PERCENT: float = 5.0

    # The public site's own server (app/core/site.py). It reads the API from
    # a few of Vercel's shared addresses; a request carrying
    # `X-Site-Token: <one of these>` is the site, counted in a bucket of its
    # own (SITE_RATE_LIMIT, over the public reads only) and allowed to say
    # whose page it is rendering with `X-Visitor-IP`. A comma-separated list,
    # so a token can be rotated without a gap; each at least 32 characters
    # (`openssl rand -hex 32`). Empty: no request is the site.
    SITE_TOKENS: str = ""
    SITE_RATE_LIMIT: str = "3000/minute"
    # The public certificate verifier (app/api/certificates/router.py), each
    # of its three asks counted on its own. Per person: the visitor the
    # site's server names in X-Visitor-IP behind a valid token, else the
    # caller's own address; every answer counts, a miss as much as a match.
    # And in all, for each site token, so a token that leaked buys no more.
    CERTIFICATE_VERIFY_LIMIT: str = "20/minute;100/hour"
    CERTIFICATE_VERIFY_SITE_LIMIT: str = "600/minute"
    # Told when an event's or a club's public page changes, so it renders the
    # page again (app/core/site_revalidate.py): a POST with
    # `Authorization: Bearer <SITE_REVALIDATE_SECRET>`. Empty secret: never told.
    SITE_REVALIDATE_URL: str = "https://myotherapp.in/api/revalidate"
    SITE_REVALIDATE_SECRET: str = ""
    # How long a signed link to a public picture stays valid, in seconds: an
    # event's poster, a club's logo and cover, a speaker's, team member's or
    # partner's picture, a post's photo, a stock avatar, a store item's photo
    # (app/core/storage/fields.py). A week, the most a SigV4 link allows.
    # Private files -- tickets, certificates, template art and signatures,
    # profile pictures, event files -- keep an hour.
    PUBLIC_IMAGE_URL_TTL: int = 7 * 24 * 3600
    # The media CDN in front of the bucket (the media Cloudflare Worker in the
    # app repo, infra/cloudflare/media), e.g. https://media.myotherapp.in. Set,
    # every public picture above is handed out as <MEDIA_CDN_URL>/<key>: one
    # permanent address, cached at the edge near the viewer, no signature
    # (app/core/storage/media.py). Unset: the week-signed links, as before.
    # Private files never go through it, set or not.
    MEDIA_CDN_URL: str = ""

    @field_validator("PUBLIC_IMAGE_URL_TTL")
    @classmethod
    def _signable_ttl(cls, value: int) -> int:
        """Between an hour and SigV4's ceiling of a week: S3 refuses a link
        signed for longer, which would break every picture at once."""
        return max(3600, min(int(value), 7 * 24 * 3600))

    @field_validator("MEDIA_CDN_URL")
    @classmethod
    def _cdn_origin(cls, value: str) -> str:
        """An origin, no trailing slash; `media.myotherapp.in` is read as
        https. A path, a query or a fragment is refused at startup rather than
        quietly breaking every picture: each address is this, "/", the key."""
        value = (value or "").strip()
        if not value:
            return ""
        if "://" not in value:
            value = f"https://{value}"
        parts = urlsplit(value)
        if parts.scheme not in ("https", "http") or not parts.hostname or parts.path.rstrip("/") or parts.query or parts.fragment:
            raise ValueError("APP_MEDIA_CDN_URL must be an origin like https://media.myotherapp.in (no path)")
        return f"{parts.scheme}://{parts.netloc.lower()}"

    @property
    def mcp_allowed_origins(self) -> list[str]:
        return [o.strip().rstrip("/") for o in (self.MCP_ALLOWED_ORIGINS or "").split(",") if o.strip()]

    @property
    def mail_suppress_domains(self) -> frozenset[str]:
        """MAIL_SUPPRESS_DOMAINS, lower case, without a leading @ or spaces."""
        raw = self.MAIL_SUPPRESS_DOMAINS
        listed = raw.split(",") if isinstance(raw, str) else raw
        return frozenset(d.strip().lstrip("@").lower() for d in listed if d and d.strip().lstrip("@"))

    @property
    def site_tokens(self) -> list[str]:
        return [t.strip() for t in (self.SITE_TOKENS or "").split(",") if t.strip()]

    @field_validator("LINK_SIGNING_KEYS")
    @classmethod
    def _link_keys_long_enough(cls, value: str) -> str:
        """A short key is a guessable one, and with it every emailed ticket:
        refused at startup rather than signing with it."""
        keys = [k.strip() for k in (value or "").split(",") if k.strip()]
        if any(len(k) < 32 for k in keys):
            raise ValueError("every key in APP_LINK_SIGNING_KEYS must be at least 32 characters (openssl rand -hex 32)")
        return ",".join(keys)

    @property
    def link_signing_keys(self) -> list[str]:
        """The emailed links' keys, the one that signs first; SECRET_KEY alone when none is set."""
        keys = [k for k in (self.LINK_SIGNING_KEYS or "").split(",") if k]
        return keys or [self.SECRET_KEY]

    # Club plans (app/api/subscriptions/gates.py). PLAN_ENFORCEMENT=false is
    # the kill switch: every plan gate lets everything through, as before the
    # plans existed. An event created before PLAN_ENFORCEMENT_FROM and held
    # within PLAN_GRANDFATHER_DAYS of it is "grandfathered": no event-scoped
    # gate applies to it (paid tickets, registration caps, live room, ID
    # cards...), so nothing under way breaks at the switch-on. The window
    # stops an old event being redated and reused for ever.
    PLAN_ENFORCEMENT: bool = True
    PLAN_ENFORCEMENT_FROM: datetime = datetime(2026, 10, 18, 0, 0, tzinfo=_IST)
    PLAN_GRANDFATHER_DAYS: int = 90
    # RETIRED (migration message_credits): extra messages past a plan's
    # monthly limits were billed as a Razorpay add-on each month. Clubs buy
    # prepaid credits instead (app/api/message_quota/credits.py) and nothing
    # is charged after the fact, whatever this says: it is read by nothing,
    # and kept only so an .env that still sets it starts.
    MESSAGE_OVERAGE_BILLING: bool = False

    @field_validator("RAZORPAY_WEBHOOK_SECRET")
    @classmethod
    def _webhook_secret_set(cls, value: str) -> str:
        """No webhook secret, no start: with an empty one, any body signed
        with an empty key would pass as Razorpay's."""
        if not (value or "").strip():
            raise ValueError("APP_RAZORPAY_WEBHOOK_SECRET is empty; set it to the webhook secret in Razorpay's dashboard")
        return value

    # ─── Accounts, sign-in and push (fix/accounts-integrity) ───────────────
    # Where the rate limiter keeps its counts (slowapi/limits storage URI).
    # memory:// is per process, which is exact while WORKERS=1; with more
    # workers or hosts they need one shared store. Left at memory:// with
    # REDIS_URL set, the limits use REDIS_URL (app/core/rate_limit.py); a
    # URI here other than memory:// wins over that.
    RATE_LIMIT_STORAGE: str = "memory://"
    # An admin's session as someone else ends this many minutes after it
    # starts, with no refresh (30 to 60).
    IMPERSONATION_SESSION_MINUTES: int = 30
    # A new event's push to people whose interests match it, beyond the
    # club's followers: at most this many people per event, and nobody more
    # than this many times a day. Followers are never capped.
    INTEREST_PUSH_MAX_PER_EVENT: int = 5000
    INTEREST_PUSH_MAX_PER_USER_PER_DAY: int = 3
    # The admin Overview is kept this many seconds (0 = worked out every time).
    ADMIN_OVERVIEW_CACHE_SECONDS: int = 60
    # Housekeeping: how long sign-in attempts are kept, and how often the
    # sweep removes those and the sessions that can never be used again.
    SIGN_IN_ATTEMPTS_KEEP_DAYS: int = 180
    ACCOUNT_SWEEP_SECONDS: int = 3600

    # ─── Platform: jobs, the worker, Redis (feat/w2-platform) ──────────────
    # The job queue (app/core/jobs.py) is always on: work after a request is
    # a row in `jobs`, written with the change it is about. Unset, each API
    # process runs the jobs it wrote as soon as its transaction commits (and
    # picks up retries and what a stopped process left). True: the API only
    # writes them, and the worker (`python -m app.worker`) runs them. Turn it
    # on only once the worker is running.
    JOBS_WORKER: bool = False
    # Jobs one process runs at once, all names together; a handler can ask
    # for fewer of its own at a time. JOBS_LIMITS overrides that per name:
    # "deliveries.send=2,notifications.followers=1".
    JOBS_CONCURRENCY: int = 4
    JOBS_LIMITS: str = ""
    # How long a runner with nothing to do waits before looking again (a
    # commit in the same process, or a finished job, wakes it sooner).
    JOBS_POLL_SECONDS: float = 1.0
    # Finished jobs are kept this many days (0: deleted as they finish), and
    # dead ones -- tried max_attempts times -- this many.
    JOBS_KEEP_DAYS: int = 3
    JOBS_DEAD_KEEP_DAYS: int = 30
    # On SIGTERM the worker stops taking jobs and gives the ones in hand this
    # long to finish; the rest go back to the queue. Under the stop_grace_period
    # docker-compose.prod.yml gives the worker.
    JOBS_DRAIN_SECONDS: float = 25.0
    # The leader-only loops (the email, closeout, billing, payments... sweeps
    # started in doth.py's lifespan) run in the worker instead of the API.
    # Each still takes its advisory lock, so one process runs each either way.
    # Turn it on only with the worker running: with it on, the API runs none.
    SWEEPS_IN_WORKER: bool = False
    # Redis (app/core/redis.py), e.g. redis://redis:6379/0. Unset: no Redis
    # anywhere, and everything works as before, per process. Set: the rate
    # limits are shared (RATE_LIMIT_STORAGE above), the discover cache has a
    # shared second level, and publish/subscribe crosses processes. Every
    # call has a short timeout and falls back to the per-process way.
    REDIS_URL: str = ""
    # How long one Redis call may take before it is given up on, in seconds.
    REDIS_TIMEOUT: float = 0.25

    # ─── The money ledger (app/api/ledger) ─────────────────────────────────
    # Every money transition is posted to the append-only ledger_entries and
    # summed into ledger_monthly as it happens, whatever this says. This picks
    # what the club finance pages, statements and the admin revenue page READ:
    # false (default) works their figures out from registrations, orders,
    # payment logs and subscription charges as before; true reads the
    # ledger's monthly sums. Turn on only after the backfill
    # (scripts/ledger_backfill.py) and a clean compare (scripts/ledger_compare.py).
    LEDGER_READS: bool = False
    # When the daily ledger job runs (failed postings retried, drift put
    # right, and -- once the history is backfilled -- Razorpay's payments and
    # refunds of the day before checked), as HH:MM in India. Empty: never.
    LEDGER_DAILY_AT: str = "02:30"

    # ─── Push by FCM topics (feat/w2-push) ──────────────────────────────────
    # A broadcast -- a club's new post or event to its followers, a new event
    # to people whose interests are under its topics, an announcement to
    # everyone -- is one send to an FCM topic (club_{id}, topic_{slug}, all)
    # instead of a send per device (app.core.notifications.delivery). Off:
    # every push goes device by device, exactly as before. Turn it on only
    # once most devices report topic support (GET
    # /notifications/topics/adoption); until they all do, the ones that have
    # not are still sent to one by one (PUSH_TOPICS_LEGACY), and no device is
    # sent a broadcast both ways.
    PUSH_TOPICS: bool = False
    # With PUSH_TOPICS on, the devices that have not said they subscribe to
    # topics (an app from before them) still get each broadcast, device by
    # device. Turn it off once those are gone: a broadcast is then one send.
    PUSH_TOPICS_LEGACY: bool = True
    # A broadcast's inbox line is one shared row, read through each person's
    # follows and interests, instead of a row per recipient
    # (app.core.notifications.inbox). Off: a row per recipient, as before.
    # Turned off after being on, the shared lines written meanwhile leave the
    # inbox until it is on again (they are kept, not deleted).
    INBOX_SHARED: bool = False
    # A topic push cannot be capped per person (INTEREST_PUSH_MAX_PER_USER_PER_DAY):
    # FCM sends it to whoever is subscribed. So with PUSH_TOPICS or
    # INBOX_SHARED on, each topic has at most this many new-event pushes a day.
    INTEREST_TOPIC_PUSH_MAX_PER_DAY: int = 3

    # ─── The live room pushed (feat/w2-live) ───────────────────────────────
    # Server-sent events for the live room (app/api/live/stream.py): one
    # connection per phone or console, the room's state pushed when it
    # changes, instead of a request per phone every two to five seconds.
    # Unset, the stream answers 404 and every client keeps polling; the polls
    # work either way. With several API processes it needs REDIS_URL, so a
    # change made in one reaches the streams held by the others.
    LIVE_SSE: bool = False
    # Open streams one API process holds at most (each is a socket and a few
    # tens of KB); past it a new one is told 503 and polls instead.
    LIVE_SSE_MAX_STREAMS: int = 5000
    # Telling the room (app/api/live/notify.py): when the host starts the
    # quiz or launches a poll and asks for it, a push to every device of
    # everyone checked in, and a line in their inbox. Off: the quiz and the
    # poll still start, and the console is told `skipped_reason: disabled`.
    LIVE_NOTIFY: bool = True
    # At most one such push per event in this many seconds...
    LIVE_NOTIFY_GAP_SECONDS: int = 180
    # ...and this many per event per day, midnight to midnight in India.
    LIVE_NOTIFY_PER_DAY: int = 10

    # ─── AI assist (app/core/ai, app/api/ai; docs/OPERATIONS.md section 14) ─
    # The switch. Off (the default): every AI endpoint answers 403
    # AI_DISABLED and no provider is ever called, a club's own key included.
    AI_ENABLED: bool = False
    # The platform's one Gemini API key (Google AI Studio). One key, never a
    # list: spreading calls over several keys or accounts to get past
    # Gemini's rate limits is against its terms. Production uses a key on a
    # PAID (billing-enabled) project: on the free tier Google may use what is
    # sent to improve its products, and people may read it.
    GEMINI_API_KEY: str = ""
    # Model ids, comma-separated, in the order they are tried: on a 429, a
    # 5xx or a timeout the next is asked after a short pause. The vision list
    # is used when a poster or a PDF is sent. From
    # https://ai.google.dev/gemini-api/docs/models (checked 2026-10-20).
    AI_MODELS: str = "gemini-3.8-flash,gemini-3.5-flash-lite"
    AI_VISION_MODELS: str = "gemini-3.8-flash,gemini-3.5-flash-lite"
    # Gemini's thinking level for these calls (low, medium, high; empty: the
    # model's own default). Low keeps a draft inside the timeout.
    AI_GEMINI_THINKING: str = "low"
    # A second provider for the platform, tried after every Gemini model has
    # failed: "anthropic", with its own key and models (empty models: its
    # default list). Unset: none. Never a second key for the same provider.
    AI_FALLBACK_PROVIDER: str = ""
    AI_FALLBACK_API_KEY: str = ""
    AI_FALLBACK_MODELS: str = ""
    # Seconds one call may take (a file makes it a vision call), and the
    # whole request with every fallback at most (under nginx's 120).
    AI_TIMEOUT_SECONDS: float = 30.0
    AI_VISION_TIMEOUT_SECONDS: float = 60.0
    AI_DEADLINE_SECONDS: float = 100.0
    # The pause before the next model is asked.
    AI_FALLBACK_BACKOFF_SECONDS: float = 0.5
    # The Fernet key clubs' own AI keys are encrypted with (as
    # PAYMENT_KEYS_SECRET is for their Razorpay secrets; a comma-separated
    # list rotates). Unset: no club can connect its own key.
    AI_KEYS_SECRET: str = ""

    # ─── Clubs connecting AI assistants over MCP (app/api/mcp, docs/MCP.md) ──
    # The kill switch. Off: /mcp, the OAuth endpoints and their discovery
    # documents all answer 404, no token is accepted, and no pending action
    # can be confirmed. The club dashboard's connected-apps list still reads
    # and revokes.
    MCP_ENABLED: bool = False
    # Where the club dashboard lives, for the links the tools hand back: an
    # event (/events/{id}), the store (/merchandise), and the page a person
    # opens to approve what an assistant asked for (/confirm/{token}).
    MCP_DASHBOARD_URL: str = "https://club-dashboard.myotherapp.in"
    # An access token's life, and a refresh token's (each refresh starts a new one).
    MCP_ACCESS_TOKEN_MINUTES: int = 60
    MCP_REFRESH_TOKEN_DAYS: int = 30
    # Calls one connected app may make (limits' syntax, ";" between limits).
    MCP_RATE_LIMIT: str = "60/minute;2000/day"
    # Browser pages allowed to call /mcp directly (the Origin header). A
    # request with no Origin -- a server-side connector, a desktop app -- is
    # never refused for it. Comma-separated origins.
    MCP_ALLOWED_ORIGINS: str = "https://claude.ai,https://chatgpt.com,https://chat.openai.com"
    # Clients that identify themselves by an https URL to their metadata
    # (OAuth Client ID Metadata Documents): the document is fetched when one
    # first asks to connect. False: only registered (RFC 7591) clients.
    MCP_CLIENT_METADATA_DOCUMENTS: bool = True

    @field_validator("PLAN_ENFORCEMENT_FROM")
    @classmethod
    def _aware_cutoff(cls, value: datetime) -> datetime:
        """A cutoff written without an offset is read as India time, the
        clock every plan month is counted on."""
        return value if value.tzinfo else value.replace(tzinfo=_IST)

    @property
    def cors_origins(self) -> list[str]:
        """CORS_ORIGINS, plus the public site (bare and www.) and its former
        address: the site calls the API from the browser, and the server's
        env may not list a domain the site has just moved to."""
        listed = self.CORS_ORIGINS.split(",") if isinstance(self.CORS_ORIGINS, str) else self.CORS_ORIGINS
        out: list[str] = []
        for origin in [*listed, *_site_origins(self.PUBLIC_SITE_URL), FORMER_SITE_ORIGIN]:
            origin = origin.strip()
            if origin and origin not in out:
                out.append(origin)
        return out


def _site_origins(url: str) -> list[str]:
    parts = urlsplit(url.strip())
    if not parts.scheme or not parts.netloc:
        return []
    host = parts.netloc.lower()
    other = host[len("www."):] if host.startswith("www.") else f"www.{host}"
    return [f"{parts.scheme}://{host}", f"{parts.scheme}://{other}"]


settings = AppConfig()
