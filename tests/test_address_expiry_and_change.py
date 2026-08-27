"""pending_address_choice expiry, and the 'change my address' override
that's supposed to work no matter what else is mid-flow."""

import time

from conftest import last_buttons_body, last_text

from feedme_bot import handlers
from feedme_bot.state import PendingAddressChoice, PendingOptions


async def test_expired_address_choice_says_timed_out(mocked_whatsapp):
    handlers.store.get("jid1").pending_address_choice = PendingAddressChoice(
        candidates=[{"id": "a1"}], original_text="pizza", created_at=time.time() - 999_999
    )

    await handlers.handle_message("jid1", "Home", interactive_id="addr:a1")

    assert "timed out" in last_text(mocked_whatsapp).lower()
    assert handlers.store.get("jid1").pending_address_choice is None


async def test_change_address_overrides_whatever_is_pending(mocked_swiggy, mocked_whatsapp):
    user = handlers.store.get("jid1")
    user.pending_options = PendingOptions(
        safe_pick={"name": "A"}, mood_pick={"name": "B"}, address_id="a1"
    )
    mocked_swiggy["get_addresses"].return_value = [
        {"id": "a1", "addressTag": "Home"},
        {"id": "a2", "addressTag": "Office"},
    ]

    await handlers.handle_message("jid1", "change my address")

    assert user.pending_options is None
    assert "which address" in last_buttons_body(mocked_whatsapp).lower()


async def test_change_address_with_no_saved_addresses_says_so(mocked_swiggy, mocked_whatsapp):
    mocked_swiggy["get_addresses"].return_value = []

    await handlers.handle_message("jid1", "switch to a different address")

    assert "no saved addresses" in last_text(mocked_whatsapp).lower()
