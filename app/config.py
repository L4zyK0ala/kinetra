from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


class Settings:
    def __init__(self) -> None:
        key = os.environ.get("FITDASH_ENCRYPTION_KEY")
        if not key:
            raise RuntimeError(
                "FITDASH_ENCRYPTION_KEY .env dosyasında tanımlı değil. Üretmek için: "
                "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )
        self.encryption_key = key
        self.database_url = os.environ.get(
            "FITDASH_DATABASE_URL", f"sqlite:///{BASE_DIR / 'fitdash.db'}"
        )
        self.host = os.environ.get("FITDASH_HOST", "0.0.0.0")
        self.port = int(os.environ.get("FITDASH_PORT", "8010"))
        self.sync_interval_minutes = int(os.environ.get("FITDASH_SYNC_INTERVAL_MINUTES", "45"))
        self.sync_lookback_days = int(os.environ.get("FITDASH_SYNC_LOOKBACK_DAYS", "14"))
        self.public_url = os.environ.get("FITDASH_PUBLIC_URL", "http://localhost:8010").rstrip("/")
        self.strava_client_id = os.environ.get("STRAVA_CLIENT_ID", "")
        self.strava_client_secret = os.environ.get("STRAVA_CLIENT_SECRET", "")
        self.local_timezone = os.environ.get("FITDASH_TIMEZONE", "Europe/Istanbul")
        # Kinetra Koç sohbeti (Claude API). Anahtar SDK tarafından ANTHROPIC_API_KEY'den okunur.
        self.coach_model = os.environ.get("FITDASH_COACH_MODEL", "claude-opus-5-5")
        self.coach_effort = os.environ.get("FITDASH_COACH_EFFORT", "medium")
        self.coach_monthly_budget_usd = float(os.environ.get("FITDASH_COACH_MONTHLY_BUDGET_USD", "10"))
        # Telegram botu (TELEGRAM_BOT_TOKEN ortam değişkeninden okunur) ve akşam kontrolü
        self.telegram_api_base = os.environ.get("FITDASH_TELEGRAM_API_BASE", "https://api.telegram.org")
        self.evening_check_hour = int(os.environ.get("FITDASH_EVENING_CHECK_HOUR", "21"))


settings = Settings()
