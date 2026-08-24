"""WhatsApp Cloud API — outbound messages only. Inbound handling lives in
the FastAPI webhook route in main.py.
"""

import httpx

from feedme_bot.config import settings

GRAPH_API_VERSION = "v21.0"


async def send_text(to: str, body: str) -> None:
    url = (
        f"https://graph.facebook.com/{GRAPH_API_VERSION}/"
        f"{settings.meta_phone_number_id}/messages"
    )
    headers = {"Authorization": f"Bearer {settings.meta_whatsapp_token}"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(url, headers=headers, json=payload)
    response.raise_for_status()


async def send_image(to: str, image_url: str, caption: str = "") -> None:
    """For UPI QR codes — the response's upiIntentUrl needs to be rendered
    as an image first (not implemented here yet); this just sends a
    already-hosted image URL."""
    url = (
        f"https://graph.facebook.com/{GRAPH_API_VERSION}/"
        f"{settings.meta_phone_number_id}/messages"
    )
    headers = {"Authorization": f"Bearer {settings.meta_whatsapp_token}"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "image",
        "image": {"link": image_url, "caption": caption},
    }
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(url, headers=headers, json=payload)
    response.raise_for_status()
