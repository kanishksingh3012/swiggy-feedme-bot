"""FastAPI webhook — the one piece that stays blocked until the WhatsApp
Cloud API rate limit clears and a test number is claimed. Everything it
calls into (handlers.py, swiggy_client.py, intent.py) can be exercised
independently right now, e.g. via a script that calls handle_message()
directly with fake JIDs/text.
"""

import hashlib
import hmac
import json
import logging
from collections import OrderedDict
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from feedme_bot import swiggy_client
from feedme_bot.config import settings
from feedme_bot.handlers import handle_message
from feedme_bot.state import store

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("feedme_bot")

_MAX_SEEN_MESSAGE_IDS = 500


class _SeenMessages:
    """Bounded, in-memory set of already-processed WhatsApp message ids.
    Meta retries webhook delivery on a slow/failed response — including
    exactly the kind of tunnel hiccup that silently ate messages for 90
    minutes earlier tonight — and a retried delivery must NOT re-run
    handle_message, since re-running a payment-choice tap would place the
    same real order twice."""

    def __init__(self, max_size: int = _MAX_SEEN_MESSAGE_IDS) -> None:
        self._ids: OrderedDict[str, None] = OrderedDict()
        self._max_size = max_size

    def seen_before(self, message_id: str) -> bool:
        if message_id in self._ids:
            return True
        self._ids[message_id] = None
        if len(self._ids) > self._max_size:
            self._ids.popitem(last=False)
        return False


_seen_messages = _SeenMessages()


def _has_valid_signature(raw_body: bytes, signature_header: str) -> bool:
    """Meta signs every webhook POST body with HMAC-SHA256 keyed on the app
    secret, sent as `X-Hub-Signature-256: sha256=<hex>`. Without checking
    this, the public webhook URL accepts ANY payload shaped like a
    WhatsApp message from anyone who finds it — including a forged
    payment-choice tap, which is the literal purchase trigger. Verifies
    against the raw bytes, not the re-serialized JSON, since re-encoding
    can change byte-for-byte output and silently break every signature."""
    if not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(
        settings.meta_app_secret.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    provided = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, provided)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # All session state is in-memory (personal-tool phase 1, see
    # state.py), wiped on every restart — but the real Swiggy cart is not.
    # A crash or restart between "item added at selection time" and
    # "user tapped Yes/No" leaves a real cart item with nothing in memory
    # pointing at it. Flush on startup so a later, unrelated order can't
    # silently ship that stale item — place_food_order acts on whatever's
    # in the cart, not a specific item id.
    try:
        await swiggy_client.flush_food_cart()
    except Exception:
        logger.exception("Startup cart flush failed — continuing anyway")
    yield


app = FastAPI(title="FeedMe Bot", lifespan=lifespan)


@app.get("/webhook")
async def verify_webhook(request: Request) -> Response:
    # Meta's handshake uses dotted query param names (hub.mode etc.), which
    # can't bind to normal FastAPI function parameters — read them raw.
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge", "")
    if mode == "subscribe" and token == settings.meta_webhook_verify_token:
        return Response(content=challenge, media_type="text/plain")
    return Response(status_code=403)


@app.post("/webhook", response_model=None)
async def receive_webhook(request: Request) -> dict[str, str] | Response:
    raw_body = await request.body()
    if settings.meta_app_secret:
        signature_header = request.headers.get("x-hub-signature-256", "")
        if not _has_valid_signature(raw_body, signature_header):
            logger.warning("Rejected webhook POST with invalid/missing signature")
            return Response(status_code=403)

    payload = json.loads(raw_body)
    try:
        entry = payload["entry"][0]
        change = entry["changes"][0]["value"]
        messages = change.get("messages", [])
    except (KeyError, IndexError):
        return {"status": "ignored"}

    for message in messages:
        message_id = message.get("id")
        if message_id and _seen_messages.seen_before(message_id):
            logger.info("Ignoring duplicate webhook delivery for message %s", message_id)
            continue

        from_number = message.get("from", "")
        if settings.allowed_whatsapp_number and from_number != settings.allowed_whatsapp_number:
            logger.info("Ignoring message from non-allowed number %s", from_number)
            continue

        text = ""
        interactive_id: str | None = None
        msg_type = message.get("type")

        if msg_type == "text":
            text = message.get("text", {}).get("body", "")
        elif msg_type == "interactive":
            # Standard Cloud API shape for a tapped button/list row — logging
            # the raw block on first use since the docs didn't give us the
            # exact shape to verify against ahead of time (see plan notes).
            interactive = message.get("interactive", {})
            logger.info("Raw interactive payload: %s", interactive)
            reply_obj = interactive.get("button_reply") or interactive.get("list_reply") or {}
            interactive_id = reply_obj.get("id")
            text = reply_obj.get("title", "")
        else:
            continue  # voice notes / other types not wired up yet

        if not text and not interactive_id:
            continue

        try:
            async with store.get_lock(from_number):
                await handle_message(from_number, text, interactive_id=interactive_id)
        except Exception:
            # A failed reply (bad outbound send, unhandled error mid-flow)
            # shouldn't crash the webhook handler — log and move on. Meta
            # expects a fast 200 regardless, or it'll start retrying delivery.
            logger.exception("Failed handling message from %s", from_number)

    return {"status": "ok"}
