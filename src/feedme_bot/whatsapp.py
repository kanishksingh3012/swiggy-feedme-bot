"""WhatsApp Cloud API — outbound messages only. Inbound handling lives in
the FastAPI webhook route in main.py.
"""

import httpx

from feedme_bot.config import settings

GRAPH_API_VERSION = "v21.0"


async def _send(payload: dict) -> None:
    url = (
        f"https://graph.facebook.com/{GRAPH_API_VERSION}/"
        f"{settings.meta_phone_number_id}/messages"
    )
    headers = {"Authorization": f"Bearer {settings.meta_whatsapp_token}"}
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(url, headers=headers, json=payload)
    response.raise_for_status()


async def send_text(to: str, body: str) -> None:
    await _send(
        {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}}
    )


async def send_image(to: str, image_url: str, caption: str = "") -> None:
    """For UPI QR codes — the response's upiIntentUrl needs to be rendered
    as an image first (not implemented here yet); this just sends a
    already-hosted image URL."""
    await _send(
        {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "image",
            "image": {"link": image_url, "caption": caption},
        }
    )


async def send_reply_buttons(
    to: str, body: str, buttons: list[tuple[str, str]], footer: str = ""
) -> None:
    """buttons: list of (id, title) pairs, max 3 — verified live against
    Meta's docs: title max 20 chars, body max 1024, footer max 60."""
    if len(buttons) > 3:
        raise ValueError(f"WhatsApp reply buttons cap at 3, got {len(buttons)}")
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body[:1024]},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": bid, "title": title[:20]}}
                    for bid, title in buttons
                ]
            },
        },
    }
    if footer:
        payload["interactive"]["footer"] = {"text": footer[:60]}
    await _send(payload)


async def send_list(
    to: str,
    body: str,
    button_text: str,
    rows: list[tuple[str, str, str]],
    section_title: str = "Options",
) -> None:
    """rows: list of (id, title, description), max 10 total — verified live:
    row title max 24 chars, description max 72, button text max 20."""
    if len(rows) > 10:
        raise ValueError(f"WhatsApp list rows cap at 10 total, got {len(rows)}")
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body[:4096]},
            "action": {
                "button": button_text[:20],
                "sections": [
                    {
                        "title": section_title[:24],
                        "rows": [
                            {
                                "id": rid,
                                "title": title[:24],
                                "description": description[:72],
                            }
                            for rid, title, description in rows
                        ],
                    }
                ],
            },
        },
    }
    await _send(payload)
