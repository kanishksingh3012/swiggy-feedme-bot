"""The last bug found live tonight: extract_intent could never say "I
don't know," so a signal-less message ("Home", "ok") got silently
converted into a guessed search. Also covers the PendingDishRetry bridge
that stops address from being re-asked right after."""

from unittest.mock import Mock

from conftest import last_text

from feedme_bot import handlers
from feedme_bot.intent import OrderIntent


async def test_zero_signal_asks_instead_of_guessing(monkeypatch, mocked_swiggy, mocked_whatsapp):
    monkeypatch.setattr(handlers, "classify_message", Mock(return_value="new_order"))
    monkeypatch.setattr(handlers, "extract_intent", Mock(return_value=OrderIntent(query=None)))
    mocked_swiggy["get_addresses"].return_value = [{"id": "a1"}]  # single address, no ask needed

    await handlers.handle_message("jid1", "Home")

    mocked_swiggy["search_menu"].assert_not_called()
    assert "what would you like to eat" in last_text(mocked_whatsapp).lower()


async def test_dish_retry_reuses_address_without_reasking(monkeypatch, mocked_swiggy, mocked_whatsapp):
    monkeypatch.setattr(handlers, "classify_message", Mock(return_value="new_order"))
    mocked_swiggy["get_addresses"].return_value = [
        {"id": "a1", "addressTag": "Home"},
        {"id": "a2", "addressTag": "Office"},
    ]

    # First: a mood guess with zero real search matches -> retry state set,
    # address must already be settled by the time the user replies again.
    monkeypatch.setattr(
        handlers, "extract_intent", Mock(return_value=OrderIntent(query="biryani", query_alternatives=[]))
    )
    mocked_swiggy["search_menu"].return_value = {"items": []}
    await handlers.handle_message("jid1", "craving something", interactive_id=None)
    # Picking Home resolves address AND runs the (still-empty) search.
    await handlers.handle_message("jid1", "Home", interactive_id="addr:a1")
    assert "couldn't find enough matches" in last_text(mocked_whatsapp).lower()

    # Second message continues the SAME attempt — must not ask for address again.
    monkeypatch.setattr(handlers, "extract_intent", Mock(return_value=OrderIntent(query="pizza")))
    mocked_swiggy["search_menu"].return_value = {
        "items": [
            {"menu_item_id": "1", "price": 100, "name": "A"},
            {"menu_item_id": "2", "price": 100, "name": "B"},
        ]
    }
    monkeypatch.setattr(
        handlers.rank,
        "rank_candidates",
        Mock(return_value=({"menu_item_id": "1", "price": 100, "name": "A"}, {"menu_item_id": "2", "price": 100, "name": "B"})),
    )
    await handlers.handle_message("jid1", "pizza then")

    mocked_swiggy["search_menu"].assert_awaited_with("a1", "pizza", veg_only=False, restaurant_id=None)
