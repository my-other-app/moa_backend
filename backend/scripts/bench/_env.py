"""Environment for the benchmark scripts: the settings the app needs to
import, pointed at a scratch database (``moa_bench`` unless BENCH_DB says
otherwise). Import this before anything under ``app``.

Nothing here reaches the network: the address lookup is switched off, and
image URLs are signed locally with throwaway keys.
"""
import os

DB = os.environ.get("BENCH_DB", "moa_bench")

os.environ["APP_DATABASE_URL"] = f"postgresql+asyncpg://localhost:5432/{DB}"
os.environ["APP_DATABASE_URL_SYNC"] = f"postgresql://localhost:5432/{DB}"
os.environ["APP_GEOIP_URL"] = ""
for key, value in {
    "APP_SECRET_KEY": "bench-secret-key-not-for-production",
    "APP_CORS_ORIGINS": '["http://testserver"]',
    "APP_S3_BUCKET": "bench-bucket",
    "APP_S3_ACCESS_KEY": "bench-access-key",
    "APP_S3_SECRET_KEY": "bench-secret-key",
    "APP_S3_BASE_PATH": "",
    "APP_SES_ACCESS_KEY": "x",
    "APP_SES_SECRET_KEY": "x",
    "APP_SES_DEFAULT_SENDER": "bench@example.com",
    "APP_GOOGLE_WEB_CLIENT_ID": "x",
    "APP_GOOGLE_ANDROID_CLIENT_ID": "x",
    "APP_GOOGLE_IOS_CLIENT_ID": "x",
    "APP_RAZORPAY_KEY_ID": "x",
    "APP_RAZORPAY_KEY_SECRET": "x",
    "APP_RAZORPAY_WEBHOOK_SECRET": "x",
    "APP_ACCESS_TOKEN_EXPIRE_MINUTES": "600",
    "APP_REFRESH_TOKEN_EXPIRE_DAYS": "7",
}.items():
    os.environ.setdefault(key, value)
