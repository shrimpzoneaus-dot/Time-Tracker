"""Runtime settings, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    database_url: str
    bot_token: str
    webhook_secret: str
    session_secret: str
    public_url: str
    timezone: str
    language: str
    session_days: int = 30
    magic_link_minutes: int = 15
    cookie_secure: bool = True

    @property
    def configured_admin_ids(self) -> set[int]:
        raw = os.getenv("ADMIN_CHAT_ID", "")
        return {int(part) for part in raw.replace(";", ",").split(",") if part.strip().isdigit()}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    database_url = os.getenv("DATABASE_URL", "")
    # Neon hands out postgresql:// URLs; SQLAlchemy needs the driver named.
    if database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)

    return Settings(
        database_url=database_url,
        bot_token=os.getenv("BOT_TOKEN", ""),
        webhook_secret=os.getenv("TELEGRAM_WEBHOOK_SECRET", ""),
        session_secret=os.getenv("SESSION_SECRET", ""),
        public_url=os.getenv("PUBLIC_URL", "").rstrip("/"),
        timezone=os.getenv("APP_TIMEZONE", "Australia/Sydney"),
        language=os.getenv("APP_LANGUAGE", "en"),
        # Fly terminates TLS and forces https, so the cookie is Secure in
        # production. Only a local/test run over plain http turns it off.
        cookie_secure=os.getenv("COOKIE_SECURE", "1") != "0",
    )
