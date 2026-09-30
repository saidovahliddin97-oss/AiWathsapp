"""FastAPI application: WhatsApp webhook, admin API and a local demo UI."""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel

from app import scheduler
from app.bridge import BridgeSender
from app.claude import build_generator
from app.config import Settings, get_settings
from app.memory import Store, load_relatives_config
from app.models import Fact, IncomingMessage, Relative
from app.webhook import Assistant, DryRunSender
from app.whatsapp import WhatsAppClient, parse_webhook, verify_challenge, verify_signature

log = logging.getLogger("app")
OUTBOX_RETRY_SECONDS = 60


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs full request URLs at INFO - keep them out of the logs
    logging.getLogger("httpx").setLevel(logging.WARNING)


def build_state(app: FastAPI, settings: Settings) -> None:
    store = Store(settings.database_path)
    rel_file = settings.relatives_file
    if not Path(rel_file).exists() and Path("config/relatives.example.json").exists():
        log.warning("%s not found - loading config/relatives.example.json", rel_file)
        rel_file = "config/relatives.example.json"
    load_relatives_config(store, rel_file)

    generator = build_generator(settings)
    wa = WhatsAppClient(settings) if settings.cloud_enabled else None
    if settings.dry_run:
        sender = DryRunSender()
    elif settings.whatsapp_mode == "bridge":
        sender = BridgeSender(settings)
    elif wa is not None:
        sender = wa
    else:
        sender = DryRunSender()
    if isinstance(sender, DryRunSender):
        log.warning("WhatsApp sending disabled (DRY_RUN or credentials missing) - replies are only stored")
    log.info("whatsapp_mode=%s llm=%s", settings.whatsapp_mode, type(generator).__name__)

    app.state.settings = settings
    app.state.store = store
    app.state.whatsapp = wa
    app.state.sender = sender
    app.state.assistant = Assistant(settings, store, generator, sender, media_source=wa)
    # The demo never sends real WhatsApp messages.
    app.state.demo_assistant = Assistant(settings, store, generator, DryRunSender(), media_source=None)


async def _outbox_loop(app: FastAPI) -> None:
    while True:
        await asyncio.sleep(OUTBOX_RETRY_SECONDS)
        try:
            await app.state.assistant.retry_outbox()
        except Exception:
            log.exception("outbox retry loop error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    if not getattr(app.state, "store", None):
        build_state(app, get_settings())
    tasks = [asyncio.create_task(_outbox_loop(app)), asyncio.create_task(scheduler.loop(app.state.assistant))]
    yield
    for task in tasks:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    if app.state.whatsapp:
        await app.state.whatsapp.aclose()


app = FastAPI(title="WhatsApp Family Assistant", lifespan=lifespan)


def require_admin(
    request: Request,
    x_admin_token: str | None = Header(default=None),
    token: str | None = Query(default=None),
) -> None:
    settings: Settings = request.app.state.settings
    expected = settings.admin_token.get_secret_value()
    if expected:
        if not hmac.compare_digest(x_admin_token or token or "", expected):
            raise HTTPException(status_code=401, detail="admin token required")
    elif not settings.dry_run and not isinstance(request.app.state.sender, DryRunSender):
        # Production without ADMIN_TOKEN: keep admin/demo closed.
        raise HTTPException(status_code=403, detail="set ADMIN_TOKEN to use admin endpoints")


# ----------------------------------------------------------------- webhook
@app.get("/health")
async def health(request: Request) -> dict:
    s: Settings = request.app.state.settings
    assistant: Assistant = request.app.state.assistant
    live = not isinstance(request.app.state.sender, DryRunSender)
    return {
        "status": "ok",
        "llm": assistant.generator_label,
        "whatsapp": (s.whatsapp_mode if live else "dry-run"),
        "autopilot": assistant.autopilot_on(),
        "relatives": len(request.app.state.store.list_relatives()),
    }


@app.get("/webhook")
async def webhook_verify(request: Request):
    s: Settings = request.app.state.settings
    challenge = verify_challenge(dict(request.query_params), s.whatsapp_verify_token.get_secret_value())
    if challenge is None:
        raise HTTPException(status_code=403, detail="verification failed")
    return PlainTextResponse(challenge)


@app.post("/webhook")
async def webhook_receive(request: Request, background: BackgroundTasks) -> dict:
    s: Settings = request.app.state.settings
    body = await request.body()
    if not verify_signature(body, request.headers.get("x-hub-signature-256"), s.whatsapp_app_secret.get_secret_value()):
        raise HTTPException(status_code=401, detail="bad signature")
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid json")
    messages = parse_webhook(payload)
    assistant: Assistant = request.app.state.assistant
    for msg in messages:
        # Acknowledge fast (Meta retries slow webhooks); process after the response.
        background.add_task(_safe_process, assistant, msg)
    return {"received": len(messages)}


class BridgeMessage(BaseModel):
    message_id: str
    phone: str = ""
    chat_id: str | None = None
    is_group: bool = False
    group_name: str | None = None
    addressed_to_bot: bool = False
    from_me: bool = False
    self_chat: bool = False
    kind: str = "text"
    text: str = ""
    caption: str | None = None
    profile_name: str | None = None
    media_b64: str | None = None
    media_mime: str | None = None


@app.post("/bridge/incoming")
async def bridge_incoming(
    request: Request, body: BridgeMessage, background: BackgroundTasks, x_bridge_token: str | None = Header(default=None)
) -> dict:
    """Messages from the linked-device bridge (bridge/index.js)."""
    s: Settings = request.app.state.settings
    expected = s.bridge_token.get_secret_value()
    if not expected or not hmac.compare_digest(x_bridge_token or "", expected):
        raise HTTPException(status_code=401, detail="bad bridge token")
    kind = body.kind if body.kind in ("text", "audio", "image") else "unsupported"
    msg = IncomingMessage(**body.model_dump(exclude={"kind"}), kind=kind)
    background.add_task(_safe_process, request.app.state.assistant, msg)
    return {"ok": True}


async def _safe_process(assistant: Assistant, msg: IncomingMessage) -> None:
    try:
        await assistant.process(msg)
    except Exception:
        log.exception("processing failed event_id=%s", msg.message_id)


# ------------------------------------------------------------------- admin
class FactIn(BaseModel):
    fact: str
    relative_id: str | None = None
    key: str | None = None
    source: str = "user"
    confidence: float = 1.0


class CandidateDecision(BaseModel):
    approve: bool
    fact_text: str | None = None


@app.get("/admin/relatives", dependencies=[Depends(require_admin)])
async def admin_relatives(request: Request) -> list[Relative]:
    return request.app.state.store.list_relatives()


@app.post("/admin/relatives", dependencies=[Depends(require_admin)])
async def admin_upsert_relative(request: Request, rel: Relative) -> dict:
    request.app.state.store.upsert_relative(rel)
    return {"ok": True}


@app.get("/admin/facts", dependencies=[Depends(require_admin)])
async def admin_facts(request: Request, relative_id: str | None = None) -> list[Fact]:
    return request.app.state.store.get_facts(relative_id, min_confidence=0.0)


@app.post("/admin/facts", dependencies=[Depends(require_admin)])
async def admin_add_fact(request: Request, body: FactIn) -> dict:
    fact = Fact(fact=body.fact, relative_id=body.relative_id, source=body.source, confidence=body.confidence)
    return {"active": request.app.state.store.add_fact(fact, key=body.key)}


@app.get("/admin/fact-candidates", dependencies=[Depends(require_admin)])
async def admin_candidates(request: Request) -> list[dict]:
    return request.app.state.store.list_fact_candidates()


@app.post("/admin/fact-candidates/{candidate_id}", dependencies=[Depends(require_admin)])
async def admin_resolve_candidate(request: Request, candidate_id: int, body: CandidateDecision) -> dict:
    ok = request.app.state.store.resolve_fact_candidate(candidate_id, body.approve, body.fact_text)
    if not ok:
        raise HTTPException(status_code=404, detail="candidate not found")
    return {"ok": True}


@app.post("/admin/greet/{relative_id}", dependencies=[Depends(require_admin)])
async def admin_greet(request: Request, relative_id: str):
    return await request.app.state.assistant.greet(relative_id)


@app.post("/admin/outbox/retry", dependencies=[Depends(require_admin)])
async def admin_outbox_retry(request: Request) -> dict:
    return await request.app.state.assistant.retry_outbox()


# -------------------------------------------------------------------- demo
class DemoMessage(BaseModel):
    relative_id: str
    text: str


@app.get("/demo", response_class=HTMLResponse)
async def demo_page() -> str:
    return (Path(__file__).parent / "static" / "demo.html").read_text(encoding="utf-8")


@app.get("/demo/api/state", dependencies=[Depends(require_admin)])
async def demo_state(request: Request) -> dict:
    store: Store = request.app.state.store
    return {
        "health": await health(request),
        "relatives": [r.model_dump() for r in store.list_relatives()],
        "candidates": store.list_fact_candidates(),
    }


@app.get("/demo/api/history/{relative_id}", dependencies=[Depends(require_admin)])
async def demo_history(request: Request, relative_id: str) -> dict:
    store: Store = request.app.state.store
    return {
        "messages": [m.model_dump() for m in store.recent_messages(relative_id, limit=100, max_chars=100_000)],
        "facts": [f.model_dump() for f in store.get_facts(relative_id, min_confidence=0.0)],
    }


@app.post("/demo/api/message", dependencies=[Depends(require_admin)])
async def demo_message(request: Request, body: DemoMessage):
    rel = request.app.state.store.get_relative(body.relative_id)
    if rel is None:
        raise HTTPException(status_code=404, detail="unknown relative")
    msg = IncomingMessage(message_id=f"demo-{uuid.uuid4()}", phone=rel.phone, text=body.text)
    return await request.app.state.demo_assistant.process(msg)


@app.post("/demo/api/greet/{relative_id}", dependencies=[Depends(require_admin)])
async def demo_greet(request: Request, relative_id: str):
    return await request.app.state.demo_assistant.greet(relative_id)
