"""Simple config, loaded from environment."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _split_ids(raw: str) -> set[int]:
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part:
            out.add(int(part))
    return out


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_ids: set[int]
    signing_key: str
    admin_api_token: str | None
    db_path: str
    api_host: str
    api_port: int
    event_retention_days: int
    rate_limit_per_min: int


def load_settings() -> Settings:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise RuntimeError("BOT_TOKEN is not set in environment")

    signing_key = os.getenv("SIGNING_KEY", "").strip()
    if not signing_key or signing_key.startswith("changeme"):
        raise RuntimeError("SIGNING_KEY is not set to a safe value")

    admin_ids = _split_ids(os.getenv("ADMIN_IDS", ""))
    if not admin_ids:
        raise RuntimeError("ADMIN_IDS is not set (need at least 1 chat ID)")

    return Settings(
        bot_token=bot_token,
        admin_ids=admin_ids,
        signing_key=signing_key,
        admin_api_token=(os.getenv("ADMIN_API_TOKEN") or "").strip() or None,
        db_path=os.getenv("DB_PATH", "/srv/data/licenses.db"),
        api_host=os.getenv("API_HOST", "0.0.0.0"),
        api_port=int(os.getenv("API_PORT", "8080")),
        event_retention_days=int(os.getenv("EVENT_RETENTION_DAYS", "90")),
        rate_limit_per_min=int(os.getenv("RATE_LIMIT_PER_MIN", "60")),
    )
