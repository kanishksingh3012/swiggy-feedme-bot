"""If Swiggy returns no usable payment method at all, the tool's own
guidance is: don't attempt place_food_order. Must say so and not leave a
dangling pending_payment_choice with nothing tappable."""

from conftest import last_text

from feedme_bot import handlers
from feedme_bot.state import UserState


async def test_no_payment_methods_available_says_so(mocked_swiggy, mocked_whatsapp):
    mocked_swiggy["get_payment_options"].return_value = {"cod": {"available": False}, "platforms": {}}
    user = UserState()

    await handlers._offer_payment_options("jid1", user, {"name": "Chicken Tikka"}, "a1")

    assert "no payment methods" in last_text(mocked_whatsapp).lower()
    assert user.pending_payment_choice is None
