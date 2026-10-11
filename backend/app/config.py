from pydantic import PostgresDsn, RedisDsn, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        case_sensitive=True,
        env_prefix="APP_",
    )

    SECRET_KEY: str
    WORKERS: int = 1
    PORT: int = 8000
    DEBUG: bool = False
    CORS_ORIGINS: list[str] | str
    APP_VERSION: str = "1.0"

    DATABASE_URL: str
    DATABASE_URL_SYNC: str
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

    ACCESS_TOKEN_EXPIRE_MINUTES: int
    REFRESH_TOKEN_EXPIRE_DAYS: int

    DISCORD_ERROR_WEBHOOK: str | None = None

    # Where a link in an email points: the API as the public reaches it.
    PUBLIC_API_URL: str = "https://api.myotherapp.in"
    # The clubs dashboard, for links a volunteer opens on a phone.
    PUBLIC_WEB_URL: str = "https://clubs-next.myotherapp.in"
    # The public event site a tracking link lands on; {slug} for a club's page.
    PUBLIC_SITE_URL: str = "https://events.myotherapp.in"
    PUBLIC_CLUB_URL: str = "https://events.myotherapp.in/clubs/{slug}"

    CORS_ORIGINS: list[str] | str

    APP_VERSION: str = "1.0"

    @property
    def cors_origins(self) -> list[str]:
        if isinstance(self.CORS_ORIGINS, str):
            return [
                origin.strip()
                for origin in self.CORS_ORIGINS.split(",")
                if origin.strip()
            ]
        return self.CORS_ORIGINS


settings = AppConfig()
