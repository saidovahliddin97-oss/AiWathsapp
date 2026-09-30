"""Sender for the free linked-device bridge (bridge/index.js, Baileys).

The bridge keeps the WhatsApp Web session; this backend asks it to send
messages over a localhost HTTP API protected by BRIDGE_TOKEN.
"""
from __future__ import annotations

import httpx

from app.config import Settings
from app.whatsapp import WhatsAppError


class BridgeSender:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        # sending includes a human-like "typing..." pause, so allow a generous timeout
        self._http = http or httpx.AsyncClient(timeout=60.0)

    async def send_text(self, to: str, text: str, quote_id: str | None = None) -> str | None:
        body = {"to": to, "text": text}
        if quote_id:
            body["quote_id"] = quote_id
        try:
            resp = await self._http.post(
                f"{self.settings.bridge_url.rstrip('/')}/send",
                json=body,
                headers={"x-bridge-token": self.settings.bridge_token.get_secret_value()},
            )
        except httpx.HTTPError as e:
            raise WhatsAppError(f"bridge unreachable: {type(e).__name__}") from e
        if resp.status_code >= 400:
            raise WhatsAppError(
                f"bridge error {resp.status_code}", resp.status_code, retryable=resp.status_code in (429, 503, 500)
            )
        try:
            return resp.json().get("id")
        except ValueError:
            return None

    async def aclose(self) -> None:
        await self._http.aclose()
