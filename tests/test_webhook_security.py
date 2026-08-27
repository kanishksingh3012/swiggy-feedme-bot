"""Formalizes the three live proofs run tonight into permanent tests:
duplicate-webhook protection, signature verification, and the per-JID
lock. These sit directly on the "a forged or replayed request places a
real order" risk, so they're the highest-value tests in this suite."""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from feedme_bot import handlers, main


def _payload(message_id: str, body: str = "hi") -> bytes:
    return json.dumps(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "id": message_id,
                                        "from": "919999999999",
                                        "type": "text",
                                        "text": {"body": body},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    ).encode()


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(main.settings.meta_app_secret.encode(), body, hashlib.sha256).hexdigest()


def test_duplicate_message_id_is_only_processed_once(monkeypatch):
    monkeypatch.setattr(main.settings, "allowed_whatsapp_number", "")
    body = _payload("wamid.DUPLICATE_TEST")
    with patch.object(main, "handle_message", new=AsyncMock()) as mock_handle:
        with TestClient(main.app) as client:
            headers = {"x-hub-signature-256": _sign(body)}
            r1 = client.post("/webhook", content=body, headers=headers)
            r2 = client.post("/webhook", content=body, headers=headers)  # simulated Meta retry
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert mock_handle.call_count == 1


def test_unsigned_and_forged_requests_are_rejected(monkeypatch):
    monkeypatch.setattr(main.settings, "allowed_whatsapp_number", "")
    body = _payload("wamid.SIG_TEST")
    bad_sig = "sha256=" + "0" * 64
    with patch.object(main, "handle_message", new=AsyncMock()) as mock_handle:
        with TestClient(main.app) as client:
            r_none = client.post("/webhook", content=body)
            r_bad = client.post("/webhook", content=body, headers={"x-hub-signature-256": bad_sig})
            r_good = client.post("/webhook", content=body, headers={"x-hub-signature-256": _sign(body)})
    assert r_none.status_code == 403
    assert r_bad.status_code == 403
    assert r_good.status_code == 200
    assert mock_handle.call_count == 1


async def test_per_user_lock_serializes_concurrent_deliveries():
    import asyncio

    active = 0
    overlapped = False

    async def fake_work():
        nonlocal active, overlapped
        async with handlers.store.get_lock("jid1"):
            active += 1
            overlapped = overlapped or active > 1
            await asyncio.sleep(0.05)
            active -= 1

    await asyncio.gather(fake_work(), fake_work())
    assert not overlapped
