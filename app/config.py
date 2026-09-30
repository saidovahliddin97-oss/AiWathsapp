"""Application configuration loaded from environment variables / .env.

Secrets are only ever read from the environment - never hard-coded.
"""
from __future__ import annotations

from functools import lru_cache

import re

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM provider: auto | claude | gemini | offline
    # auto = Claude if ANTHROPIC_API_KEY is set, else Gemini if GEMINI_API_KEY is set, else offline demo
    llm_provider: str = "auto"
    gemini_api_key: SecretStr = SecretStr("")
    gemini_model: str = "gemini-flash-latest"

    # Claude
    anthropic_api_key: SecretStr = SecretStr("")
    llm_model: str = "claude-opus-5-5"
    llm_effort: str = "low"
    llm_max_tokens: int = 4096
    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 2
    llm_use_fallbacks: bool = True

    # How we talk to WhatsApp: "bridge" (free, linked device via bridge/) or "cloud" (official Cloud API)
    whatsapp_mode: str = "bridge"
    bridge_url: str = "http://127.0.0.1:3001"
    bridge_token: SecretStr = SecretStr("")

    # Owner (you): notifications and WhatsApp commands
    owner_phone: str = ""
    owner_names: str = ""  # comma separated names that mean "the bot is being addressed" in groups
    notify_owner: bool = True
    # true = the bot runs on YOUR OWN WhatsApp (linked device): notifications go to your
    # "message yourself" chat and your own manual replies pause the bot in that chat
    personal_number: bool = False
    # forward messages the bot does not answer (greeting-only / autopilot off) to you
    forward_unanswered: bool = True
    manual_pause_minutes: int = 120  # after you reply yourself, the bot keeps quiet in that chat

    # Scheduled greetings (GREETING_ONLY)
    greetings_enabled: bool = True
    greeting_interval_days: float = 7.0
    greeting_hours: str = "10-20"  # local time window for sending
    greeting_gap_minutes_min: float = 1.0
    greeting_gap_minutes_max: float = 5.0

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

    @field_validator("personal_number", mode="before")
    @classmethod
    def _personal_number_phone(cls, v):
        # People often put their phone number here; that means "yes, my own number".
        if isinstance(v, str) and len(re.sub(r"\D", "", v)) >= 8:
            return True
        return v

    @model_validator(mode="before")
    @classmethod
    def _owner_phone_from_personal(cls, data):
        if isinstance(data, dict):
            for key in ("personal_number", "PERSONAL_NUMBER"):
                v = data.get(key)
                if isinstance(v, str) and len(re.sub(r"\D", "", v)) >= 8 and not (
                    data.get("owner_phone") or data.get("OWNER_PHONE")
                ):
                    data["owner_phone"] = v
        return data

    @property
    def claude_enabled(self) -> bool:
        return bool(self.anthropic_api_key.get_secret_value())

    @property
    def cloud_enabled(self) -> bool:
        return self.whatsapp_mode == "cloud" and bool(
            self.whatsapp_token.get_secret_value() and self.whatsapp_phone_number_id
        )

    @property
    def gemini_enabled(self) -> bool:
        return bool(self.gemini_api_key.get_secret_value())

    @property
    def owner_name_list(self) -> list[str]:
        return [n.strip() for n in self.owner_names.split(",") if n.strip()]

    @property
    def greeting_window(self) -> tuple[int, int]:
        try:
            start, end = (int(x) for x in self.greeting_hours.split("-"))
            return start, end
        except ValueError:
            return 10, 20


@lru_cache
def get_settings() -> Settings:
    return Settings()


if __name__ == "__main__":  # python -m app.config  -> check .env in plain words
    import sys

    from pydantic import ValidationError

    try:
        s = Settings()
    except ValidationError as e:
        for err in e.errors():
            name = str(err["loc"][0]).upper()
            print(f"✖ В файле .env неверное значение {name}={err.get('input')!r}: {err['msg']}")
        print("  Для true/false пишите только true или false; номер телефона — в OWNER_PHONE.")
        sys.exit(1)
    llm = "Claude" if s.claude_enabled else "Gemini" if s.gemini_enabled else "нет ключа (демо-режим)"
    print(f"✓ Настройки в порядке. Модель: {llm}. Личный номер: {'да' if s.personal_number else 'нет'}.")
