"""End-to-end handle_message flow, entirely mocked — the happy path plus
the two things tonight's fixes specifically targeted: the confirm message
showing the real post-coupon total, and cart being added at selection
time (not confirm-yes)."""

from unittest.mock import Mock

from conftest import last_buttons_body, last_text

from feedme_bot import handlers
from feedme_bot.intent import OrderIntent

SAFE_ITEM = {
    "menu_item_id": "111",
    "id": "111",
    "name": "Chicken Tikka",
    "price": 185,
    "restaurant_id": "r1",
    "restaurant_name": "Jollygunj",
    "rating": 4.2,
}
MOOD_ITEM = {
    "menu_item_id": "222",
    "id": "222",
    "name": "OG Chilli Chicken",
    "price": 250,
    "restaurant_id": "r1",
    "restaurant_name": "Jollygunj",
    "rating": 4.5,
}


async def test_multiple_addresses_asks_before_searching(monkeypatch, mocked_swiggy, mocked_whatsapp):
    monkeypatch.setattr(handlers, "classify_message", Mock(return_value="new_order"))
    monkeypatch.setattr(handlers, "extract_intent", Mock(return_value=OrderIntent(query="chicken")))
    mocked_swiggy["get_addresses"].return_value = [
        {"id": "a1", "addressTag": "Home"},
        {"id": "a2", "addressTag": "Office"},
    ]

    await handlers.handle_message("jid1", "craving chicken")

    mocked_swiggy["search_menu"].assert_not_called()
    assert "which address" in last_buttons_body(mocked_whatsapp).lower()


async def test_full_happy_path_pick_confirm_pay(monkeypatch, mocked_swiggy, mocked_whatsapp):
    monkeypatch.setattr(handlers, "classify_message", Mock(return_value="new_order"))
    monkeypatch.setattr(handlers, "extract_intent", Mock(return_value=OrderIntent(query="chicken")))
    monkeypatch.setattr(handlers.rank, "rank_candidates", Mock(return_value=(SAFE_ITEM, MOOD_ITEM)))

    mocked_swiggy["get_addresses"].return_value = [{"id": "a1", "addressTag": "Home"}]
    mocked_swiggy["search_menu"].return_value = {"items": [SAFE_ITEM, MOOD_ITEM]}

    # Single saved address -> resolves without asking, goes straight to search.
    await handlers.handle_message("jid1", "craving chicken")
    body = last_buttons_body(mocked_whatsapp)
    assert "Chicken Tikka" in body and "OG Chilli Chicken" in body

    # Pick Safe -> item added to cart NOW (not on confirm-yes), total shown.
    mocked_swiggy["update_food_cart"].return_value = {"successful": True}
    mocked_swiggy["get_food_cart"].return_value = {
        "items": [{"menu_item_id": 111, "in_stock": True}],
        "pricing": {"to_pay": 218},
        "offers": {"coupon_applied": "TRYNEW", "coupon_discount": 0},
    }
    await handlers.handle_message("jid1", "safe pick", interactive_id="pick:safe")
    mocked_swiggy["update_food_cart"].assert_awaited_once()
    confirm_body = last_buttons_body(mocked_whatsapp)
    assert "Total to pay: ₹218" in confirm_body

    # Confirm yes -> straight to payment, no second cart-add.
    mocked_swiggy["get_payment_options"].return_value = {
        "cod": {"available": True, "displayName": "Cash on Delivery"},
        "paymentAmount": 218,
    }
    await handlers.handle_message("jid1", "yes", interactive_id="confirm:yes")
    assert mocked_swiggy["update_food_cart"].await_count == 1  # still just once
    assert "how do you want to pay" in last_buttons_body(mocked_whatsapp).lower()

    # Tap Cash on Delivery -> real order placement, real success message.
    mocked_swiggy["place_food_order"].return_value = {"status": "CONFIRMED"}
    await handlers.handle_message("jid1", "cash", interactive_id="pay:cod")
    mocked_swiggy["place_food_order"].assert_awaited_once_with(
        "a1", payment_method="Cash", generate_upi_qr=False, intent_app=None
    )
    assert "order placed" in last_text(mocked_whatsapp).lower()


async def test_confirm_no_flushes_the_reserved_cart_item(monkeypatch, mocked_swiggy, mocked_whatsapp):
    monkeypatch.setattr(handlers, "classify_message", Mock(return_value="new_order"))
    monkeypatch.setattr(handlers, "extract_intent", Mock(return_value=OrderIntent(query="chicken")))
    monkeypatch.setattr(handlers.rank, "rank_candidates", Mock(return_value=(SAFE_ITEM, MOOD_ITEM)))
    mocked_swiggy["get_addresses"].return_value = [{"id": "a1"}]
    mocked_swiggy["search_menu"].return_value = {"items": [SAFE_ITEM, MOOD_ITEM]}
    mocked_swiggy["update_food_cart"].return_value = {"successful": True}
    mocked_swiggy["get_food_cart"].return_value = {
        "items": [{"menu_item_id": 111, "in_stock": True}],
        "pricing": {"to_pay": 218},
        "offers": {},
    }

    await handlers.handle_message("jid1", "craving chicken")
    await handlers.handle_message("jid1", "safe pick", interactive_id="pick:safe")
    await handlers.handle_message("jid1", "no", interactive_id="confirm:no")

    mocked_swiggy["flush_food_cart"].assert_awaited_once()
    assert "cancelled" in last_text(mocked_whatsapp).lower()
