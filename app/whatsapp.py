"""WhatsApp Cloud API integration (Meta Graph API).

Responsibilities: webhook verification, signature check, parsing inbound
events, sending text messages and downloading media. No Claude code here.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

import httpx

from app.config import Settings
from app.models import IncomingMessage

log = logging.getLogger(__name__)

GRAPH_URL = "https://graph.facebook.com"


class WhatsAppError(Exception):
    def __init__(self, message: str, status_code: int | None = None, retryable: bool = True) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


def verify_challenge(params: dict[str, str], verify_token: str) -> str | None:
    """Handle Meta's GET verification handshake. Returns the challenge or None."""
    if (
        verify_token
        and params.get("hub.mode") == "subscribe"
        and hmac.compare_digest(params.get("hub.verify_token", ""), verify_token)
    ):
        return params.get("hub.challenge", "")
    return None


def verify_signature(body: bytes, header: str | None, app_secret: str) -> bool:
    """Check X-Hub-Signature-256. If no app secret is configured the check is skipped."""
    if not app_secret:
        return True
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header.removeprefix("sha256="), expected)


def parse_webhook(payload: dict[str, Any]) -> list[IncomingMessage]:
    """Extract inbound user messages from a webhook payload.

    Status updates (sent/delivered/read) and unknown shapes are ignored.
    """
    out: list[IncomingMessage] = []
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        return out
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            names = {
                c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts") or []
            }
            for m in value.get("messages") or []:
                msg_id, sender = m.get("id"), m.get("from")
                if not msg_id or not sender:
                    continue
                mtype = m.get("type")
                common = dict(
                    message_id=msg_id,
                    phone=sender,
                    timestamp=int(m["timestamp"]) if str(m.get("timestamp", "")).isdigit() else None,
                    profile_name=names.get(sender),
                )
                if mtype == "text":
                    out.append(IncomingMessage(kind="text", text=(m.get("text") or {}).get("body", ""), **common))
                elif mtype in ("audio", "voice"):
                    media = m.get("audio") or m.get("voice") or {}
                    out.append(IncomingMessage(kind="audio", media_id=media.get("id"), **common))
                elif mtype == "image":
                    media = m.get("image") or {}
                    out.append(
                        IncomingMessage(
                            kind="image",
                            media_id=media.get("id"),
                            caption=media.get("caption"),
                            text=media.get("caption") or "",
                            **common,
                        )
                    )
                elif mtype == "button":
                    out.append(IncomingMessage(kind="text", text=(m.get("button") or {}).get("text", ""), **common))
                else:
                    out.append(IncomingMessage(kind="unsupported", **common))
    return out


class WhatsAppClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._http = http or httpx.AsyncClient(timeout=settings.whatsapp_timeout_seconds)

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.settings.whatsapp_token.get_secret_value()}"}

    @property
    def _base(self) -> str:
        return f"{GRAPH_URL}/{self.settings.whatsapp_api_version}"

    async def send_text(self, to: str, text: str) -> str | None:
        """Send a text message. Returns the WhatsApp message id."""
        url = f"{self._base}/{self.settings.whatsapp_phone_number_id}/messages"
        body = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": False, "body": text},
        }
        try:
            resp = await self._http.post(url, json=body, headers=self._headers)
        except httpx.HTTPError as e:
            raise WhatsAppError(f"WhatsApp API unreachable: {type(e).__name__}") from e
        if resp.status_code >= 400:
            code = None
            try:
                code = resp.json().get("error", {}).get("code")
            except ValueError:
                pass
            # 4xx other than rate limiting are not worth retrying blindly
            retryable = resp.status_code >= 500 or resp.status_code == 429 or code in (130429, 131048, 131056)
            raise WhatsAppError(f"WhatsApp API error {resp.status_code} (code {code})", resp.status_code, retryable)
        try:
            return (resp.json().get("messages") or [{}])[0].get("id")
        except ValueError:
            return None

    async def mark_as_read(self, message_id: str) -> None:
        url = f"{self._base}/{self.settings.whatsapp_phone_number_id}/messages"
        body = {"messaging_product": "whatsapp", "status": "read", "message_id": message_id}
        try:
            await self._http.post(url, json=body, headers=self._headers)
        except httpx.HTTPError:
            log.warning("mark_as_read failed", extra={"event_id": message_id})

    async def download_media(self, media_id: str) -> tuple[bytes, str]:
        """Download inbound media (audio / image). Returns (bytes, mime_type)."""
        try:
            meta = await self._http.get(f"{self._base}/{media_id}", headers=self._headers)
            meta.raise_for_status()
            info = meta.json()
            data = await self._http.get(info["url"], headers=self._headers)
            data.raise_for_status()
        except (httpx.HTTPError, KeyError, ValueError) as e:
            raise WhatsAppError(f"media download failed: {type(e).__name__}") from e
        return data.content, info.get("mime_type", "application/octet-stream").split(";")[0]

    async def aclose(self) -> None:
        await self._http.aclose()
