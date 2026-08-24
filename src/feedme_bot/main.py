"""FastAPI webhook — the one piece that stays blocked until the WhatsApp
Cloud API rate limit clears and a test number is claimed. Everything it
calls into (handlers.py, swiggy_client.py, intent.py) can be exercised
independently right now, e.g. via a script that calls handle_message()
directly with fake JIDs/text.
"""

import logging

from fastapi import FastAPI, Request, Response

from feedme_bot import whatsapp
from feedme_bot.config import settings
from feedme_bot.handlers import handle_message

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("feedme_bot")

app = FastAPI(title="FeedMe Bot")


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


@app.post("/webhook")
async def receive_webhook(request: Request) -> dict[str, str]:
    payload = await request.json()
    try:
        entry = payload["entry"][0]
        change = entry["changes"][0]["value"]
        messages = change.get("messages", [])
    except (KeyError, IndexError):
        return {"status": "ignored"}

    for message in messages:
        from_number = message.get("from", "")
        if settings.allowed_whatsapp_number and from_number != settings.allowed_whatsapp_number:
            logger.info("Ignoring message from non-allowed number %s", from_number)
            continue

        text = message.get("text", {}).get("body", "")
        if not text:
            continue  # voice notes / other types handled separately, not wired up yet

        reply = await handle_message(from_number, text)
        await whatsapp.send_text(from_number, reply)

    return {"status": "ok"}
