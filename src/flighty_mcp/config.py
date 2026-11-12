"""Configuration without embedded app or account credentials."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .errors import FlightyError

API_BASE = "https://api.flightyapp.com"
WEBSOCKET_URL = "wss://websockets.flightyapp.com/ws"
MIN_SYNC_INTERVAL = 60.0

APP_CONTRACT = "4.12.0 (4875)"
SCHEMA_VERSION = "4.12.0"
USER_AGENT = f"Flighty {APP_CONTRACT} com.flightyapp.flighty"


def secret_env(name: str) -> str:
    """Read a secret from an environment variable or Docker-style *_FILE."""
    direct = os.environ.get(name, "").strip()
    filename = os.environ.get(name + "_FILE", "").strip()
    if direct and filename:
        raise FlightyError(f"Set only {name} or {name}_FILE, not both.")
    if filename:
        try:
            direct = Path(filename).read_text().strip()
        except (OSError, UnicodeError):
            raise FlightyError(f"Cannot read {name}_FILE.") from None
    if any(c in direct for c in "\r\n\0"):
        raise FlightyError(f"{name} must contain one credential, without line breaks.")
    return direct


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, str(default)).strip().lower()
    if value not in {"true", "false", "1", "0"}:
        raise FlightyError(f"{name} must be true or false.")
    return value in {"true", "1"}


@dataclass(frozen=True)
class Settings:
    build_token: str = field(default="", repr=False)
    device_id: str = ""
    locale: str = "en_US"
    user_agent: str = USER_AGENT
    allow_writes: bool = True
    allow_account_deletion: bool = False
    timeout: float = 60.0
    cache_ttl: float = 120.0
    max_pages: int = 100
    max_response_bytes: int = 32 * 1024 * 1024
    max_cache_accounts: int = 8

    @classmethod
    def from_env(cls) -> Settings:
        try:
            settings = cls(
                build_token=secret_env("FLIGHTY_BUILD_TOKEN"),
                device_id=os.environ.get("FLIGHTY_DEVICE_ID", "").strip(),
                locale=os.environ.get("FLIGHTY_LOCALE", "en_US").strip(),
                user_agent=os.environ.get("FLIGHTY_USER_AGENT", USER_AGENT).strip(),
                allow_writes=env_bool("FLIGHTY_ALLOW_WRITES", default=True),
                allow_account_deletion=env_bool("FLIGHTY_ALLOW_ACCOUNT_DELETION"),
                timeout=float(os.environ.get("FLIGHTY_TIMEOUT", "60")),
                cache_ttl=float(os.environ.get("FLIGHTY_CACHE_TTL", "120")),
                max_pages=int(os.environ.get("FLIGHTY_MAX_PAGES", "100")),
            )
        except ValueError:
            raise FlightyError("Invalid numeric FLIGHTY configuration.") from None
        if settings.timeout <= 0 or settings.cache_ttl < 0 or not 1 <= settings.max_pages <= 1000:
            raise FlightyError("Timeout/max-pages must be positive and cache TTL nonnegative.")
        if not settings.locale or not settings.user_agent:
            raise FlightyError("Locale and User-Agent must not be empty.")
        for value in (settings.device_id, settings.locale, settings.user_agent):
            if any(c in value for c in "\r\n\0"):
                raise FlightyError("Invalid Flighty request header configuration.")
        return settings
