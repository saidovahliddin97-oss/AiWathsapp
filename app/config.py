"""Application configuration loaded from environment variables / .env.

Secrets are only ever read from the environment - never hard-coded.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Claude
    anthropic_api_key: SecretStr = SecretStr("")
    llm_model: str = "claude-opus-5-5"
    llm_effort: str = "low"
    llm_max_tokens: int = 4096
    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 2
    llm_use_fallbacks: bool = True

    # WhatsApp Cloud API
    whatsapp_token: SecretStr = SecretStr("")
    whatsapp_phone_number_id: str = ""
    whatsapp_verify_token: SecretStr = SecretStr("")
    whatsapp_app_secret: SecretStr = SecretStr("")
    whatsapp_api_version: str = "v21.0"
    whatsapp_timeout_seconds: float = 15.0

    # Behaviour
    database_path: str = "database/assistant.db"
    relatives_file: str = "config/relatives.json"
    history_limit: int = Field(default=24, ge=2, le=100)
    ignore_unknown_senders: bool = True
    dry_run: bool = False
    allow_offline_generator: bool = True
    log_message_text: bool = False
    admin_token: SecretStr = SecretStr("")
    outbox_max_attempts: int = 5

    @property
    def claude_enabled(self) -> bool:
        return bool(self.anthropic_api_key.get_secret_value())

    @property
    def whatsapp_enabled(self) -> bool:
        return bool(self.whatsapp_token.get_secret_value() and self.whatsapp_phone_number_id)


@lru_cache
def get_settings() -> Settings:
    return Settings()
