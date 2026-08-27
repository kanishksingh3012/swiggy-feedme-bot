"""The payment-choice branch is the literal purchase trigger — its error
and timeout paths need to be right, since by this point real money and a
real cart item are both already in motion."""

import time

import pytest
from conftest import last_text

from feedme_bot import handlers, swiggy_client
from feedme_bot.state import PendingPaymentChoice

ITEM = {"menu_item_id": "1", "name": "Chicken Tikka", "price": 185, "restaurant_id": "r1"}


def _pending(jid: str, created_at: float | None = None) -> PendingPaymentChoice:
    kwargs = {"item": ITEM, "address_id": "a1", "options": {"pay:cod": {"payment_method": "Cash"}}}
    if created_at is not None:
        kwargs["created_at"] = created_at
    pending = PendingPaymentChoice(**kwargs)
    handlers.store.get(jid).pending_payment_choice = pending
    return pending


async def test_swiggy_tool_error_says_nothing_was_charged(mocked_swiggy, mocked_whatsapp):
    _pending("jid1")
    mocked_swiggy["place_food_order"].side_effect = swiggy_client.SwiggyToolError(
        {"content": [{"text": "restaurant closed"}]}
    )

    await handlers.handle_message("jid1", "cash", interactive_id="pay:cod")

    text = last_text(mocked_whatsapp).lower()
    assert "restaurant closed" in text
    assert "nothing was charged" in text
    assert handlers.store.get("jid1").pending_payment_choice is None


async def test_unexpected_exception_still_re_raises_after_messaging(mocked_swiggy, mocked_whatsapp):
    _pending("jid1")
    mocked_swiggy["place_food_order"].side_effect = RuntimeError("network blew up")

    with pytest.raises(RuntimeError):
        await handlers.handle_message("jid1", "cash", interactive_id="pay:cod")

    assert "nothing was charged" in last_text(mocked_whatsapp).lower()


async def test_expired_payment_choice_flushes_cart_before_timing_out(mocked_swiggy, mocked_whatsapp):
    _pending("jid1", created_at=time.time() - 999_999)

    await handlers.handle_message("jid1", "cash", interactive_id="pay:cod")

    mocked_swiggy["flush_food_cart"].assert_awaited_once()
    mocked_swiggy["place_food_order"].assert_not_called()
    assert "timed out" in last_text(mocked_whatsapp).lower()
    assert handlers.store.get("jid1").pending_payment_choice is None
