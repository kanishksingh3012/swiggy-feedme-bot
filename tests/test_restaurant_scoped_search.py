"""'biryani from Paradise' should scope the dish search to that one
restaurant, and say so honestly if the named restaurant can't be found —
never silently search everywhere instead."""

from unittest.mock import Mock

from conftest import last_text

from feedme_bot import handlers
from feedme_bot.intent import OrderIntent


async def test_restaurant_found_scopes_the_menu_search(monkeypatch, mocked_swiggy, mocked_whatsapp):
    monkeypatch.setattr(handlers, "classify_message", Mock(return_value="new_order"))
    monkeypatch.setattr(
        handlers, "extract_intent", Mock(return_value=OrderIntent(query="biryani", restaurant="Paradise"))
    )
    monkeypatch.setattr(
        handlers.rank,
        "rank_candidates",
        Mock(
            return_value=(
                {"menu_item_id": "1", "price": 100, "name": "A"},
                {"menu_item_id": "2", "price": 100, "name": "B"},
            )
        ),
    )
    mocked_swiggy["get_addresses"].return_value = [{"id": "a1"}]
    mocked_swiggy["search_restaurants"].return_value = {
        "restaurants": [{"id": "rid42", "name": "Paradise Biryani"}]
    }
    mocked_swiggy["search_menu"].return_value = {
        "items": [
            {"menu_item_id": "1", "price": 100, "name": "A"},
            {"menu_item_id": "2", "price": 100, "name": "B"},
        ]
    }

    await handlers.handle_message("jid1", "biryani from Paradise")

    mocked_swiggy["search_menu"].assert_awaited_with(
        "a1", "biryani", veg_only=False, restaurant_id="rid42"
    )


async def test_restaurant_not_found_says_so_and_does_not_search(monkeypatch, mocked_swiggy, mocked_whatsapp):
    monkeypatch.setattr(handlers, "classify_message", Mock(return_value="new_order"))
    monkeypatch.setattr(
        handlers, "extract_intent", Mock(return_value=OrderIntent(query="biryani", restaurant="NoSuchPlace"))
    )
    mocked_swiggy["get_addresses"].return_value = [{"id": "a1"}]
    mocked_swiggy["search_restaurants"].return_value = {"restaurants": []}

    await handlers.handle_message("jid1", "biryani from NoSuchPlace")

    mocked_swiggy["search_menu"].assert_not_called()
    assert "couldn't find a restaurant called" in last_text(mocked_whatsapp).lower()
