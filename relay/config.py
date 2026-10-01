from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

from .costs import Pricing


@dataclass(frozen=True)
class Settings:
    api_key: str = field(repr=False)
    base_url: str = "https://api.poixe.com/v1"
    model: str = "aws-claude/claude-opus-5-5"
    database_url: str = "sqlite:////data/relay.db"
    timeout_seconds: float = 180
    default_max_tokens: int = 8192
    pricing: Pricing = field(default_factory=Pricing)
    static_dir: Path = Path(__file__).resolve().parents[1] / "frontend" / "dist"

    def __post_init__(self):
        parts = urlsplit(self.base_url)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ValueError("Base URL must be an HTTP(S) URL without embedded credentials")
        if not self.api_key.strip():
            raise ValueError("AM2OAIR_RELAY_API_KEY is required")
        if not self.model.strip() or len(self.model) > 200:
            raise ValueError("A valid AM2OAIR_RELAY_MODEL is required")
        if self.timeout_seconds <= 0 or self.default_max_tokens <= 0:
            raise ValueError("Timeout and default max tokens must be positive")

    @classmethod
    def from_env(cls) -> Settings:
        env = {**dotenv_values(".env"), **os.environ}
        return cls(
            api_key=env.get("AM2OAIR_RELAY_API_KEY") or "",
            base_url=env.get("AM2OAIR_RELAY_BASE_URL") or "https://api.poixe.com/v1",
            model=env.get("AM2OAIR_RELAY_MODEL") or "aws-claude/claude-opus-5-5",
            database_url=env.get("AM2OAIR_RELAY_DATABASE_URL") or "sqlite:////data/relay.db",
            timeout_seconds=float(env.get("AM2OAIR_RELAY_TIMEOUT_SECONDS") or 180),
            default_max_tokens=int(env.get("AM2OAIR_RELAY_DEFAULT_MAX_TOKENS") or 8192),
            pricing=Pricing.from_env(env),
        )

    def public_config(self) -> dict:
        # Only this fixed allowlist is visible to the management API.
        return {
            "base_url": self.base_url.replace(self.api_key, "[REDACTED]"),
            "model": self.model.replace(self.api_key, "[REDACTED]"),
        }
