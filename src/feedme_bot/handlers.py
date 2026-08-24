"""Core message-handling logic, deliberately kept separate from the
FastAPI/WhatsApp transport layer (main.py, whatsapp.py) so it can be
exercised directly — e.g. from a script or test — without a live webhook.
That's the "build the backend now, wire WhatsApp once unblocked" plan.
"""

from typing import Any

from feedme_bot import rank, swiggy_client
from feedme_bot.intent import (
    classify_message,
    extract_intent,
    resolve_confirmation,
    resolve_selection,
)
from feedme_bot.state import PendingConfirmation, PendingOptions, UserState, store


def _format_item(label: str, item: dict[str, Any]) -> str:
    name = item.get("name", "?")
    price = item.get("price", "?")
    return f"{label}: {name}\n• Price: ₹{price}"


async def _start_new_order(user: UserState, text: str) -> str:
    intent = extract_intent(text)

    addresses = await swiggy_client.get_addresses()
    if not addresses:
        return "No saved address on your Swiggy account — add one before I can search."
    address_id = addresses[0]["addressId"] if "addressId" in addresses[0] else addresses[0]["id"]

    search = await swiggy_client.search_menu(
        address_id, intent.query, veg_only=(intent.dietary_preference == "veg")
    )
    candidates = search.get("items", [])
    if len(candidates) < 2:
        # TODO: relaxation policy for zero/near-zero results (widen price/ETA,
        # explain what was relaxed) — deliberately not built yet, see plan checklist.
        return f"Couldn't find enough matches for '{intent.query}'. Try loosening the constraints?"

    try:
        safe_pick, mood_pick = rank.rank_candidates(intent, candidates)
    except rank.NoValidCandidatesError:
        return "Had trouble picking between the options — try rephrasing your request?"

    user.pending_options = PendingOptions(
        safe_pick=safe_pick, mood_pick=mood_pick, address_id=address_id
    )
    return (
        "🍽️ Top 2 Picks Found:\n\n"
        f"1️⃣ {_format_item('Safe Pick', safe_pick)}\n\n"
        f"2️⃣ {_format_item('Mood Pick', mood_pick)}\n\n"
        'Reply "1" or "2" to pick one — I\'ll confirm before ordering anything.'
    )


async def handle_message(jid: str, text: str) -> str:
    user = store.get(jid)

    # A pending checkout confirmation takes priority over everything else —
    # this is the hard gate before place_food_order, never skip it.
    if user.pending_confirmation is not None:
        if user.pending_confirmation.is_expired():
            user.pending_confirmation = None
            return "That offer's gone stale — want me to search again?"

        decision = resolve_confirmation(text)
        if decision == "yes":
            item = user.pending_confirmation.item
            address_id = user.pending_confirmation.address_id
            user.pending_confirmation = None
            # TODO: re-validate price/availability against a fresh cart fetch
            # immediately before this call — deliberately not built yet,
            # see plan checklist (staleness guard).
            result = await swiggy_client.place_food_order(address_id, payment_method="Cash")
            user.order_history.append(item)
            if result.get("normalizedStatus") == "pending":
                return "Order placed via UPI — pay via the link/QR to confirm."
            return f"Confirmed! {item.get('name', 'Your order')} is on its way."
        elif decision == "no":
            user.pending_confirmation = None
            return "No worries, cancelled."
        else:
            return 'Just need a yes or no to confirm — reply "yes" to place the order.'

    if user.pending_options is not None and not user.pending_options.is_expired():
        kind = classify_message(text, has_pending_options=True)
        if kind == "new_order":
            user.pending_options = None
            return await _start_new_order(user, text)
        if kind == "selection":
            choice = resolve_selection(text)
            if choice == "unclear":
                return 'Not sure which one — reply "1" for Safe Pick or "2" for Mood Pick.'
            item = (
                user.pending_options.safe_pick
                if choice == "safe"
                else user.pending_options.mood_pick
            )
            address_id = user.pending_options.address_id
            user.pending_options = None
            user.pending_confirmation = PendingConfirmation(item=item, address_id=address_id)
            return f"{_format_item('Confirm', item)}\n\nPlace this order? (yes/no)"
        if kind == "cancellation":
            user.pending_options = None
            return "No worries, cancelled."
        if kind == "modification":
            # TODO: merge the modification into the existing search rather than
            # discarding it — deliberately not built yet, see plan checklist.
            user.pending_options = None
            return "Got it — send your updated request and I'll search again."
        return "Not sure that's about food — still got two options waiting if you want them."

    if user.pending_options is not None and user.pending_options.is_expired():
        user.pending_options = None

    kind = classify_message(text, has_pending_options=False)
    if kind == "not_food":
        return "I only handle food orders here — tell me what you're craving."
    return await _start_new_order(user, text)
