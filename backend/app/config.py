from pydantic import PostgresDsn, RedisDsn, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        case_sensitive=True,
        env_prefix="APP_",
    )

    PORT: int = 8000
    DEBUG: bool = False
    WORKERS: int = 1
    SECRET_KEY: str

    DATABASE_URL: str
    DATABASE_URL_SYNC: str

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
