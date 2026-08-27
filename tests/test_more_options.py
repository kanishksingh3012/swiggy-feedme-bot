""""Give me more" used to dead-end with "not sure which one." Must now
surface real leftover candidates and let one of them be picked."""

from unittest.mock import Mock

from conftest import last_buttons_body, last_text

from feedme_bot import handlers
from feedme_bot.state import PendingOptions

SAFE = {"menu_item_id": "1", "id": "1", "name": "Safe", "price": 100, "restaurant_id": "r1"}
MOOD = {"menu_item_id": "2", "id": "2", "name": "Mood", "price": 100, "restaurant_id": "r1"}
EXTRA_A = {"menu_item_id": "3", "id": "3", "name": "Extra A", "price": 120, "restaurant_id": "r1", "rating": 4.8}
EXTRA_B = {"menu_item_id": "4", "id": "4", "name": "Extra B", "price": 90, "restaurant_id": "r1", "rating": 4.1}


async def test_more_button_shows_real_leftover_candidates(mocked_whatsapp):
    user = handlers.store.get("jid1")
    user.pending_options = PendingOptions(
        safe_pick=SAFE, mood_pick=MOOD, address_id="a1", remaining=[EXTRA_A, EXTRA_B]
    )

    await handlers.handle_message("jid1", "give me more", interactive_id="pick:more")

    body = last_buttons_body(mocked_whatsapp)
    assert "Extra A" in body and "Extra B" in body
    assert user.pending_more_options is not None
    assert user.pending_options is None


async def test_typed_give_me_more_works_same_as_the_button(monkeypatch, mocked_whatsapp):
    monkeypatch.setattr(handlers, "classify_message", Mock(return_value="more_options"))
    user = handlers.store.get("jid1")
    user.pending_options = PendingOptions(
        safe_pick=SAFE, mood_pick=MOOD, address_id="a1", remaining=[EXTRA_A]
    )

    await handlers.handle_message("jid1", "what else you got")

    assert "Extra A" in last_buttons_body(mocked_whatsapp)


async def test_no_remaining_candidates_says_so_instead_of_offering_empty_more(monkeypatch, mocked_whatsapp):
    monkeypatch.setattr(handlers, "classify_message", Mock(return_value="more_options"))
    user = handlers.store.get("jid1")
    user.pending_options = PendingOptions(safe_pick=SAFE, mood_pick=MOOD, address_id="a1", remaining=[])

    await handlers.handle_message("jid1", "anything else?")

    assert "everything i found" in last_text(mocked_whatsapp).lower()
    assert user.pending_options is not None  # original Safe/Mood pair still stands


async def test_picking_an_extra_option_goes_to_confirm(mocked_swiggy, mocked_whatsapp):
    user = handlers.store.get("jid1")
    user.pending_options = PendingOptions(
        safe_pick=SAFE, mood_pick=MOOD, address_id="a1", remaining=[EXTRA_A, EXTRA_B]
    )
    await handlers.handle_message("jid1", "more", interactive_id="pick:more")

    mocked_swiggy["update_food_cart"].return_value = {"successful": True}
    mocked_swiggy["get_food_cart"].return_value = {
        "items": [{"menu_item_id": 3, "in_stock": True}],
        "pricing": {"to_pay": 120},
        "offers": {},
    }
    await handlers.handle_message("jid1", "option 1", interactive_id="more:0")

    assert "Extra A" in last_buttons_body(mocked_whatsapp)
    assert "Total to pay: ₹120" in last_buttons_body(mocked_whatsapp)
