"""_poll_payment_and_finalize — mocks asyncio.sleep so the test doesn't
actually wait, then drives check_payment_status through the real branches:
success needing confirm_order, success already confirmed, terminal
failure, and the loop exhausting without ever resolving."""

from unittest.mock import AsyncMock

from conftest import last_text

from feedme_bot import handlers

PLACE_RESULT = {
    "paasId": "p1",
    "pollingIntervalInMs": 1000,
    "maxTimeToPollForInMs": 5000,
    "orderId": "o1",
    "lat": 1.0,
    "lng": 2.0,
    "cartId": "c1",
}


async def test_success_needing_confirm_order(monkeypatch, mocked_swiggy, mocked_whatsapp):
    monkeypatch.setattr(handlers.asyncio, "sleep", AsyncMock())
    mocked_swiggy["check_payment_status"].side_effect = [
        {"terminal": False},
        {"terminal": True, "isTerminalSuccess": True, "confirmed": False, "orderId": "o1"},
    ]

    await handlers._poll_payment_and_finalize("jid1", PLACE_RESULT, "a1")

    mocked_swiggy["confirm_order"].assert_awaited_once_with("o1", "a1", 1.0, 2.0, cart_id="c1")
    assert "order placed" in last_text(mocked_whatsapp).lower()


async def test_success_already_confirmed_skips_confirm_order(monkeypatch, mocked_swiggy, mocked_whatsapp):
    monkeypatch.setattr(handlers.asyncio, "sleep", AsyncMock())
    mocked_swiggy["check_payment_status"].return_value = {
        "terminal": True, "isTerminalSuccess": True, "confirmed": True,
    }

    await handlers._poll_payment_and_finalize("jid1", PLACE_RESULT, "a1")

    mocked_swiggy["confirm_order"].assert_not_called()
    assert "order placed" in last_text(mocked_whatsapp).lower()


async def test_terminal_failure_says_payment_did_not_go_through(monkeypatch, mocked_swiggy, mocked_whatsapp):
    monkeypatch.setattr(handlers.asyncio, "sleep", AsyncMock())
    mocked_swiggy["check_payment_status"].return_value = {"terminal": True, "isTerminalFailure": True}

    await handlers._poll_payment_and_finalize("jid1", PLACE_RESULT, "a1")

    assert "didn't go through" in last_text(mocked_whatsapp).lower()


async def test_never_terminal_exhausts_and_says_still_waiting(monkeypatch, mocked_swiggy, mocked_whatsapp):
    monkeypatch.setattr(handlers.asyncio, "sleep", AsyncMock())
    mocked_swiggy["check_payment_status"].return_value = {"terminal": False}
    short = {**PLACE_RESULT, "maxTimeToPollForInMs": 2000, "pollingIntervalInMs": 1000}

    await handlers._poll_payment_and_finalize("jid1", short, "a1")

    assert mocked_swiggy["check_payment_status"].await_count == 2
    assert "still waiting" in last_text(mocked_whatsapp).lower()


async def test_missing_paas_id_never_starts_polling(mocked_swiggy, mocked_whatsapp):
    await handlers._poll_payment_and_finalize("jid1", {}, "a1")

    mocked_swiggy["check_payment_status"].assert_not_called()
    assert "couldn't be started" in last_text(mocked_whatsapp).lower()
