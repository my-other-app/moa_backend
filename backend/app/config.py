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

    S3_BUCKET: str
    S3_ACCESS_KEY: str
    S3_SECRET_KEY: str
    S3_BASE_PATH: str

    SES_ACCESS_KEY: str
    SES_SECRET_KEY: str
    SES_DEFAULT_SENDER: str

    GOOGLE_WEB_CLIENT_ID: str
    GOOGLE_ANDROID_CLIENT_ID: str
    GOOGLE_IOS_CLIENT_ID: str

    # Apple Sign In
    APPLE_CLIENT_ID: str = "app.myotherapp.com"  # Your app's bundle ID
    APPLE_TEAM_ID: str = "PX6QV826X4"  # Apple Developer Team ID
    APPLE_KEY_ID: str = "S875RD7TDG"  # Key ID from Apple Developer Console
    APPLE_PRIVATE_KEY: str = ""  # Contents of .p8 file (set via environment variable)

    RAZORPAY_KEY_ID: str
    RAZORPAY_KEY_SECRET: str
    RAZORPAY_WEBHOOK_SECRET: str

    ACCESS_TOKEN_EXPIRE_MINUTES: int
    REFRESH_TOKEN_EXPIRE_DAYS: int

    DISCORD_ERROR_WEBHOOK: str | None = None

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
