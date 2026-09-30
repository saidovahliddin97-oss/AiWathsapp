"""HTTP layer: verification challenge, signature, parsing, background processing."""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import Settings
from app.memory import Store
from app.models import AgeGroup, Relative
from app.webhook import Assistant, DryRunSender
from app.whatsapp import parse_webhook
from tests.conftest import FakeGenerator


def payload(mid="wamid.X", text="Салом", phone="992900000001", mtype="text"):
    m = {"from": phone, "id": mid, "timestamp": "1700000000", "type": mtype}
    if mtype == "text":
        m["text"] = {"body": text}
    elif mtype == "audio":
        m["audio"] = {"id": "media1", "mime_type": "audio/ogg"}
    elif mtype == "image":
        m["image"] = {"id": "media2", "caption": "бин"}
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "1", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "contacts": [{"wa_id": phone, "profile": {"name": "Мама"}}],
            "messages": [m]}}]}],
    }


@pytest.fixture
def client():
    s = Settings(_env_file=None, anthropic_api_key="", dry_run=True, whatsapp_verify_token="vt",
                 whatsapp_app_secret="appsecret", database_path=":memory:")
    store = Store(":memory:")
    store.upsert_relative(Relative(id="mom", phone="992900000001", name="Модар", relation="mother",
                                   age_group=AgeGroup.ELDER, address="Модарҷон"))
    gen = FakeGenerator(["Хуб, Модарҷон!"])
    sender = DryRunSender()
    main.app.state.settings = s
    main.app.state.store = store
    main.app.state.whatsapp = None
    main.app.state.assistant = Assistant(s, store, gen, sender)
    main.app.state.demo_assistant = Assistant(s, store, gen, DryRunSender())
    with TestClient(main.app) as c:
        c.sender, c.gen, c.store = sender, gen, store
        yield c
    main.app.state.store = None


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(b"appsecret", body, hashlib.sha256).hexdigest()


def test_verify_challenge(client):
    ok = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "vt", "hub.challenge": "42"})
    assert ok.status_code == 200 and ok.text == "42"
    bad = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "no", "hub.challenge": "42"})
    assert bad.status_code == 403


def test_webhook_end_to_end_and_duplicate(client):
    body = json.dumps(payload()).encode()
    for _ in range(2):  # Meta re-delivers the same event
        r = client.post("/webhook", content=body, headers={"x-hub-signature-256": sign(body)})
        assert r.status_code == 200 and r.json() == {"received": 1}
    assert client.sender.sent == [("992900000001", "Хуб, Модарҷон!")]
    assert [m.sender for m in client.store.recent_messages("mom")] == ["relative", "assistant"]


def test_bad_signature_rejected(client):
    body = json.dumps(payload()).encode()
    r = client.post("/webhook", content=body, headers={"x-hub-signature-256": "sha256=deadbeef"})
    assert r.status_code == 401 and client.sender.sent == []


def test_status_updates_ignored(client):
    p = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {"statuses": [{"id": "x", "status": "read"}]}}]}]}
    body = json.dumps(p).encode()
    r = client.post("/webhook", content=body, headers={"x-hub-signature-256": sign(body)})
    assert r.json() == {"received": 0}


def test_parse_media_types():
    audio = parse_webhook(payload(mtype="audio"))[0]
    assert audio.kind == "audio" and audio.media_id == "media1"
    image = parse_webhook(payload(mtype="image"))[0]
    assert image.kind == "image" and image.caption == "бин"
    sticker = parse_webhook(payload(mtype="sticker"))[0]
    assert sticker.kind == "unsupported"
    assert parse_webhook({"object": "other"}) == []
    assert parse_webhook("garbage") == []


def test_health_and_demo(client):
    assert client.get("/health").json()["whatsapp"] == "dry-run"
    assert "Family Assistant" in client.get("/demo").text
    r = client.post("/demo/api/message", json={"relative_id": "mom", "text": "Салом"})
    assert r.json()["status"] == "dry_run"
    assert client.sender.sent == []  # demo never uses the real sender
