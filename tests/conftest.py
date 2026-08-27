"""Shared fixtures. Nothing in this suite may touch a real network call
or the developer's actual persisted files — see each fixture's docstring
for exactly what's isolated and why.
"""

from unittest.mock import AsyncMock

import pytest

from feedme_bot import handlers, state


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    """Every test gets a fresh StateStore, and its on-disk
    default-address/usage files point at a throwaway tmp_path — a test
    must never read or write the developer's real
    ~/.config/feedme-bot/*.json, and must never leak session state
    (pending_options, order_history, ...) into the next test."""
    monkeypatch.setattr(state, "DEFAULT_ADDRESS_PATH", tmp_path / "default_addresses.json")
    monkeypatch.setattr(state, "ADDRESS_USAGE_PATH", tmp_path / "address_usage.json")
    fresh = state.StateStore()
    monkeypatch.setattr(state, "store", fresh)
    monkeypatch.setattr(handlers, "store", fresh)
    return fresh


@pytest.fixture(autouse=True)
def mocked_swiggy(monkeypatch):
    """Every Swiggy MCP call becomes an AsyncMock the test configures
    explicitly — this suite must never hit mcp.swiggy.com, never mutate a
    real cart, never place a real order."""
    mocks = {}
    for name in (
        "get_addresses",
        "search_menu",
        "search_restaurants",
        "update_food_cart",
        "get_food_cart",
        "flush_food_cart",
        "get_payment_options",
        "place_food_order",
        "check_payment_status",
        "confirm_order",
    ):
        mock = AsyncMock()
        monkeypatch.setattr(handlers.swiggy_client, name, mock)
        mocks[name] = mock
    return mocks


@pytest.fixture(autouse=True)
def mocked_whatsapp(monkeypatch):
    """Captures every outbound message instead of hitting Meta's Graph
    API — tests assert on what WOULD have been sent."""
    send_text = AsyncMock()
    send_reply_buttons = AsyncMock()
    monkeypatch.setattr(handlers.whatsapp, "send_text", send_text)
    monkeypatch.setattr(handlers.whatsapp, "send_reply_buttons", send_reply_buttons)
    return {"send_text": send_text, "send_reply_buttons": send_reply_buttons}


def last_text(mocked_whatsapp) -> str:
    """The body of the most recent send_text call, for a quick assertion."""
    args, _ = mocked_whatsapp["send_text"].call_args
    return args[1]


def last_buttons_body(mocked_whatsapp) -> str:
    args, _ = mocked_whatsapp["send_reply_buttons"].call_args
    return args[1]
