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
    )

    SECRET_KEY: str
    WORKERS: int = 1
    PORT: int = 8000
    DEBUG: bool = False
    # uvicorn's line per request. nginx in front already logs every request,
    # so this is a second copy, written on every request; false drops it.
    ACCESS_LOG: bool = True
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
    # Mounted from the host, deliberately outside the directory the deploy
    # rsyncs with --delete -- a backup inside it would be erased by the next
    # deploy, which is the opposite of the point.
    BACKUP_DIR: str = "/backups"

    S3_BUCKET: str
    S3_ACCESS_KEY: str
    S3_SECRET_KEY: str
    S3_BASE_PATH: str

    SES_ACCESS_KEY: str
    SES_SECRET_KEY: str
    SES_DEFAULT_SENDER: str
    # Shared secret in the SES → SNS hook URL (?token=), on top of Amazon's signature.
    SES_WEBHOOK_TOKEN: str = ""
    # Which provider sends mail: "ses" (default) or "resend", which falls back
    # to SES when Resend refuses a mail (a 4xx) -- never when it may have taken it.
    MAIL_PROVIDER: str = "ses"
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
    WHATSAPP_VERIFY_TOKEN: str = ""
    WHATSAPP_APP_SECRET: str = ""
    WHATSAPP_ACCESS_TOKEN: str = ""
    WHATSAPP_PHONE_NUMBER_ID: str = ""

    GOOGLE_WEB_CLIENT_ID: str
    GOOGLE_ANDROID_CLIENT_ID: str
    GOOGLE_IOS_CLIENT_ID: str

    # Apple Sign In
    APPLE_CLIENT_ID: str = "app.myotherapp.com"  # Your app's bundle ID
    APPLE_TEAM_ID: str = "PX6QV826X4"  # Apple Developer Team ID
    APPLE_KEY_ID: str = "S875RD7TDG"  # Key ID from Apple Developer Console
    APPLE_PRIVATE_KEY: str = ""  # Contents of .p8 file (set via environment variable)

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

    # Where a link in an email points: the API as the public reaches it.
    PUBLIC_API_URL: str = "https://api.myotherapp.in"
    # The clubs dashboard, for links a volunteer opens on a phone.
    PUBLIC_WEB_URL: str = "https://clubs-next.myotherapp.in"
    # The public site: an event's page is /events/{slug}, a club's /clubs/{slug}. Tracking links land here.
    PUBLIC_SITE_URL: str = "https://myotherapp.in"
    # Where an address is turned into a place for the Security page; {ip} is filled in. Empty disables it.
    GEOIP_URL: str = "https://ipapi.co/{ip}/json/"
    # A local MaxMind GeoLite2-City (or City) database, read in place of
    # GEOIP_URL for the discover site's "roughly where" (app/api/discover/geo.py):
    # no request leaves the box and nothing is waited for. Unset, or a file
    # that cannot be read: GEOIP_URL as before. Not shipped; download it under
    # MaxMind's licence and point this at the .mmdb.
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
    RENDER_THREADS: int = 3
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

    # The public site's own server (app/core/site.py). It reads the API from
    # a few of Vercel's shared addresses; a request carrying
    # `X-Site-Token: <one of these>` is the site, counted in a bucket of its
    # own (SITE_RATE_LIMIT, over the public reads only) and allowed to say
    # whose page it is rendering with `X-Visitor-IP`. A comma-separated list,
    # so a token can be rotated without a gap; each at least 32 characters
    # (`openssl rand -hex 32`). Empty: no request is the site.
    SITE_TOKENS: str = ""
    SITE_RATE_LIMIT: str = "3000/minute"
    # Told when an event's or a club's public page changes, so it renders the
    # page again (app/core/site_revalidate.py): a POST with
    # `Authorization: Bearer <SITE_REVALIDATE_SECRET>`. Empty secret: never told.
    SITE_REVALIDATE_URL: str = "https://myotherapp.in/api/revalidate"
    SITE_REVALIDATE_SECRET: str = ""
    # How long a signed link to a public picture stays valid, in seconds: an
    # event's poster, a club's logo and cover, a speaker's, team member's or
    # partner's picture, a post's photo (app/core/storage/fields.py). A week,
    # the most a SigV4 link allows. Private files -- tickets, certificates,
    # template art and signatures, profile pictures, event files -- keep an hour.
    PUBLIC_IMAGE_URL_TTL: int = 7 * 24 * 3600

    @field_validator("PUBLIC_IMAGE_URL_TTL")
    @classmethod
    def _signable_ttl(cls, value: int) -> int:
        """Between an hour and SigV4's ceiling of a week: S3 refuses a link
        signed for longer, which would break every picture at once."""
        return max(3600, min(int(value), 7 * 24 * 3600))

    @property
    def site_tokens(self) -> list[str]:
        return [t.strip() for t in (self.SITE_TOKENS or "").split(",") if t.strip()]

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
    # workers or hosts point it at Redis (redis://host:6379/0) so they share
    # one budget. A redis:// URI needs the `redis` package installed.
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

    @field_validator("PLAN_ENFORCEMENT_FROM")
    @classmethod
    def _aware_cutoff(cls, value: datetime) -> datetime:
        """A cutoff written without an offset is read as India time, the
        clock every plan month is counted on."""
        return value if value.tzinfo else value.replace(tzinfo=_IST)

    CORS_ORIGINS: list[str] | str

    APP_VERSION: str = "1.0"

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
