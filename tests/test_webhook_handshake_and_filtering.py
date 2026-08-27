"""The remaining two untested transport-layer paths: the GET handshake
Meta uses to verify a webhook URL, and the allowed_whatsapp_number filter
that's supposed to ignore everyone but you."""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from feedme_bot import main


def _payload(message_id: str, from_number: str = "919999999999") -> bytes:
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
                                        "from": from_number,
                                        "type": "text",
                                        "text": {"body": "hi"},
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


def test_correct_handshake_returns_the_challenge(monkeypatch):
    monkeypatch.setattr(main.settings, "meta_webhook_verify_token", "secret-token")
    with TestClient(main.app) as client:
        r = client.get(
            "/webhook",
            params={"hub.mode": "subscribe", "hub.verify_token": "secret-token", "hub.challenge": "12345"},
        )
    assert r.status_code == 200
    assert r.text == "12345"


def test_wrong_verify_token_is_rejected(monkeypatch):
    monkeypatch.setattr(main.settings, "meta_webhook_verify_token", "secret-token")
    with TestClient(main.app) as client:
        r = client.get(
            "/webhook",
            params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "12345"},
        )
    assert r.status_code == 403


def test_message_from_non_allowed_number_is_ignored(monkeypatch):
    monkeypatch.setattr(main.settings, "allowed_whatsapp_number", "919999999999")
    body = _payload("wamid.FILTER_TEST", from_number="911111111111")
    with patch.object(main, "handle_message", new=AsyncMock()) as mock_handle:
        with TestClient(main.app) as client:
            r = client.post("/webhook", content=body, headers={"x-hub-signature-256": _sign(body)})
    assert r.status_code == 200
    mock_handle.assert_not_called()


def test_message_from_the_allowed_number_still_goes_through(monkeypatch):
    monkeypatch.setattr(main.settings, "allowed_whatsapp_number", "919999999999")
    body = _payload("wamid.ALLOWED_TEST")
    with patch.object(main, "handle_message", new=AsyncMock()) as mock_handle:
        with TestClient(main.app) as client:
            client.post("/webhook", content=body, headers={"x-hub-signature-256": _sign(body)})
    mock_handle.assert_called_once()
