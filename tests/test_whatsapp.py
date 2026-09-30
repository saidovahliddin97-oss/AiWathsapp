import httpx
import pytest

from app.config import Settings
from app.whatsapp import WhatsAppClient, WhatsAppError, verify_signature


def settings():
    return Settings(_env_file=None, whatsapp_token="tok", whatsapp_phone_number_id="123")


async def test_send_text_ok():
    seen = {}

    def handler(req: httpx.Request):
        seen["url"], seen["auth"], seen["body"] = str(req.url), req.headers["authorization"], req.content
        return httpx.Response(200, json={"messages": [{"id": "wamid.1"}]})

    wa = WhatsAppClient(settings(), http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await wa.send_text("992900000001", "Салом") == "wamid.1"
    assert seen["url"].endswith("/v21.0/123/messages") and seen["auth"] == "Bearer tok"


@pytest.mark.parametrize("status, retryable", [(500, True), (429, True), (400, False)])
async def test_send_text_errors(status, retryable):
    transport = httpx.MockTransport(lambda r: httpx.Response(status, json={"error": {"code": 100}}))
    wa = WhatsAppClient(settings(), http=httpx.AsyncClient(transport=transport))
    with pytest.raises(WhatsAppError) as ei:
        await wa.send_text("1", "x")
    assert ei.value.retryable is retryable and "tok" not in str(ei.value)


async def test_network_error():
    def boom(r):
        raise httpx.ConnectError("down")

    wa = WhatsAppClient(settings(), http=httpx.AsyncClient(transport=httpx.MockTransport(boom)))
    with pytest.raises(WhatsAppError):
        await wa.send_text("1", "x")


async def test_download_media():
    def handler(req):
        if req.url.path.endswith("/media1"):
            return httpx.Response(200, json={"url": "https://cdn.example/f", "mime_type": "audio/ogg; codecs=opus"})
        return httpx.Response(200, content=b"OGG")

    wa = WhatsAppClient(settings(), http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await wa.download_media("media1") == (b"OGG", "audio/ogg")


def test_signature():
    import hashlib, hmac
    body = b'{"a":1}'
    sig = "sha256=" + hmac.new(b"s", body, hashlib.sha256).hexdigest()
    assert verify_signature(body, sig, "s")
    assert not verify_signature(body, "sha256=00", "s")
    assert not verify_signature(body, None, "s")
    assert verify_signature(body, None, "")  # not configured
