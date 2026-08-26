"""Core message-handling logic, deliberately kept separate from the
FastAPI/WhatsApp transport layer (main.py, whatsapp.py) so it can be
exercised directly — e.g. from a script or test — without a live webhook.
That's the "build the backend now, wire WhatsApp once unblocked" plan.
"""

import asyncio
from typing import Any

from feedme_bot import rank, swiggy_client
from feedme_bot.intent import (
    classify_message,
    extract_intent,
    resolve_confirmation,
    resolve_numbered_choice,
    resolve_selection,
)
from feedme_bot.state import (
    PendingAddressChoice,
    PendingConfirmation,
    PendingOptions,
    UserState,
    store,
)

# search_menu returns no ETA field at all (only search_restaurants would) — so
# intent.max_eta_mins can't actually be honored by this item-search-based flow.
# Not silently ignoring it: surfaced as a known gap here rather than faked.
PRICE_RELAXATION_FACTOR = 1.25


def _format_item(label: str, item: dict[str, Any]) -> str:
    name = item.get("name", "?")
    price = item.get("price", "?")
    return f"{label}: {name}\n• Price: ₹{price}"


def _filter_candidates(
    items: list[dict[str, Any]], max_price: float | None
) -> tuple[list[dict[str, Any]], bool]:
    """In-stock always; price only if given. Returns (filtered, was_relaxed)."""
    in_stock = [item for item in items if item.get("inStock", True)]
    if max_price is None:
        return in_stock, False

    strict = [item for item in in_stock if item.get("price", 0) <= max_price]
    if len(strict) >= 2:
        return strict, False

    relaxed = [
        item for item in in_stock if item.get("price", 0) <= max_price * PRICE_RELAXATION_FACTOR
    ]
    if len(relaxed) >= 2:
        return relaxed, True

    # Nothing close either — fall back to whatever's in stock so the user
    # sees *something* rather than a dead end, but we tell them we gave up
    # on the budget constraint entirely.
    return in_stock, True


def _resolve_address(jid: str, user: UserState, addresses: list[dict[str, Any]]) -> str | None:
    """Returns an address id if resolvable without asking, else None (caller
    must prompt via pending_address_choice). Only ever asks once per user —
    a "Home"-tagged address or a previously-made choice short-circuits it."""
    if len(addresses) == 1:
        return addresses[0].get("id")

    if user.default_address_id:
        return user.default_address_id

    home_matches = [a for a in addresses if (a.get("addressTag") or "").strip().lower() == "home"]
    if len(home_matches) == 1:
        address_id = home_matches[0].get("id")
        if address_id:
            store.set_default_address(jid, address_id)
        return address_id

    return None


ADDRESS_PAGE_SIZE = 3


def _sort_home_first(addresses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        addresses, key=lambda a: 0 if (a.get("addressTag") or "").strip().lower() == "home" else 1
    )


def _format_address_tag(index: int, address: dict[str, Any]) -> str:
    # Tag only, not the full address line — keeps the one-time picker short.
    tag = address.get("addressTag") or f"Address {index}"
    return f"{index}️⃣ {tag}"


def _address_prompt(candidates: list[dict[str, Any]], shown_count: int) -> str:
    shown = candidates[:shown_count]
    listing = "\n".join(_format_address_tag(i + 1, a) for i, a in enumerate(shown))
    footer = 'Reply with the number'
    if shown_count < len(candidates):
        footer += ', or say "show more" for the rest'
    footer += " — I'll remember it for next time, no need to pick again."
    return f"Which address should I deliver to?\n\n{listing}\n\n{footer}"


async def _start_new_order(jid: str, user: UserState, text: str) -> str:
    recent_names = [o.get("name", "") for o in user.order_history[-5:] if o.get("name")]
    intent = extract_intent(text, order_history=recent_names or None)

    addresses = await swiggy_client.get_addresses()
    if not addresses:
        return "No saved address on your Swiggy account — add one before I can search."

    address_id = _resolve_address(jid, user, addresses)
    if address_id is None:
        ordered = _sort_home_first(addresses)
        user.pending_address_choice = PendingAddressChoice(
            candidates=ordered, original_text=text, shown_count=min(ADDRESS_PAGE_SIZE, len(ordered))
        )
        return _address_prompt(ordered, user.pending_address_choice.shown_count)

    search = await swiggy_client.search_menu(
        address_id, intent.query, veg_only=(intent.dietary_preference == "veg")
    )
    raw_candidates = search.get("items", [])
    candidates, was_relaxed = _filter_candidates(raw_candidates, intent.max_price)

    if len(candidates) < 2:
        return f"Couldn't find enough matches for '{intent.query}' near you at all — try a different dish?"

    try:
        safe_pick, mood_pick = rank.rank_candidates(intent, candidates)
    except rank.NoValidCandidatesError:
        return "Had trouble picking between the options — try rephrasing your request?"

    user.pending_options = PendingOptions(
        safe_pick=safe_pick, mood_pick=mood_pick, address_id=address_id
    )
    header = "🍽️ Top 2 Picks Found:"
    if was_relaxed and intent.max_price is not None:
        header += f"\n(nothing solid under ₹{intent.max_price}, showing closest matches)"
    return (
        f"{header}\n\n"
        f"1️⃣ {_format_item('Safe Pick', safe_pick)}\n\n"
        f"2️⃣ {_format_item('Mood Pick', mood_pick)}\n\n"
        'Reply "1" or "2" to pick one — I\'ll confirm before ordering anything.'
    )


async def _add_to_cart_and_revalidate(
    item: dict[str, Any], address_id: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Actually adds the item to the server-side cart (place_food_order acts
    on whatever's in the cart, not on an item id — this step isn't optional)
    and returns the fresh cart, which doubles as the staleness re-check
    right before checkout. Returns (cart, error_message)."""
    menu_item_id = item.get("menu_item_id") or item.get("id")
    restaurant_id = item.get("restaurant_id")
    if not menu_item_id or not restaurant_id:
        return None, "Lost track of which item this was — mind resending your request?"

    await swiggy_client.update_food_cart(address_id, restaurant_id, str(menu_item_id))
    cart = await swiggy_client.get_food_cart(address_id)

    cart_items = cart.get("items", [])
    matching = next((c for c in cart_items if c.get("menu_item_id") == menu_item_id), None)
    if matching is None:
        return None, "Couldn't confirm that item made it into the cart — want to try again?"
    if not matching.get("in_stock", True):
        return None, f"{item.get('name', 'That item')} just went out of stock — try something else?"

    return cart, None


async def _poll_payment_and_finalize(
    place_result: dict[str, Any], address_id: str
) -> str:
    paas_id = place_result.get("paasId")
    if not paas_id:
        return "Payment couldn't be started — try again in a moment?"

    interval_s = max(place_result.get("pollingIntervalInMs", 3000), 1000) / 1000
    max_time_s = place_result.get("maxTimeToPollForInMs", 120_000) / 1000
    elapsed = 0.0

    while elapsed < max_time_s:
        await asyncio.sleep(interval_s)
        elapsed += interval_s
        status = await swiggy_client.check_payment_status(
            paas_id,
            orderId=place_result.get("orderId"),
            addressId=address_id,
            cartId=place_result.get("cartId"),
            lat=place_result.get("lat"),
            lng=place_result.get("lng"),
        )
        if not status.get("terminal"):
            continue

        if status.get("isTerminalSuccess"):
            if status.get("confirmed"):
                return "Payment confirmed — your order's in!"
            order_id = status.get("orderId") or place_result.get("orderId")
            lat, lng = place_result.get("lat"), place_result.get("lng")
            if order_id and lat is not None and lng is not None:
                await swiggy_client.confirm_order(
                    order_id, address_id, lat, lng, cart_id=place_result.get("cartId")
                )
            return "Payment confirmed — your order's in!"

        if status.get("isTerminalFailure"):
            return "Payment didn't go through — want to try again?"

    return "Still waiting on payment confirmation — check the Swiggy app if this takes a while."


async def handle_message(jid: str, text: str) -> str:
    user = store.get(jid)

    # A pending checkout confirmation takes priority over everything else —
    # this is the hard gate before place_food_order, never skip it. Address
    # picking comes next since it blocks the order from even being searched.
    if user.pending_confirmation is None and user.pending_address_choice is not None:
        pending = user.pending_address_choice
        if pending.is_expired():
            user.pending_address_choice = None
            return "That timed out — send your order again?"

        lowered = text.strip().lower()
        wants_more = any(kw in lowered for kw in ("more", "other", "else", "none of"))
        if wants_more and pending.shown_count < len(pending.candidates):
            pending.shown_count = len(pending.candidates)
            return _address_prompt(pending.candidates, pending.shown_count)

        choice_num = resolve_numbered_choice(text, pending.shown_count)
        if choice_num is None:
            return f'Not sure which one — reply with a number, 1-{pending.shown_count}.'

        chosen_id = pending.candidates[choice_num - 1].get("id")
        original_text = pending.original_text
        user.pending_address_choice = None
        if chosen_id:
            store.set_default_address(jid, chosen_id)
        return await _start_new_order(jid, user, original_text)

    if user.pending_confirmation is not None:
        if user.pending_confirmation.is_expired():
            user.pending_confirmation = None
            return "That offer's gone stale — want me to search again?"

        decision = resolve_confirmation(text)
        if decision == "yes":
            item = user.pending_confirmation.item
            address_id = user.pending_confirmation.address_id
            user.pending_confirmation = None

            cart, error = await _add_to_cart_and_revalidate(item, address_id)
            if error:
                return error

            fresh_price = cart.get("pricing", {}).get("to_pay") if cart else None
            # Default to Cash/COD, matching the "minimal phone-interaction"
            # default from plan notes — UPI is a fallback, not built out here.
            result = await swiggy_client.place_food_order(address_id, payment_method="Cash")
            user.order_history.append(item)

            if result.get("status") == "PENDING_PAYMENT" or result.get("normalizedStatus") == "pending":
                return await _poll_payment_and_finalize(result, address_id)

            price_note = f" (₹{fresh_price})" if fresh_price else ""
            return f"Confirmed! {item.get('name', 'Your order')}{price_note} is on its way."
        elif decision == "no":
            user.pending_confirmation = None
            return "No worries, cancelled."
        else:
            return 'Just need a yes or no to confirm — reply "yes" to place the order.'

    if user.pending_options is not None and not user.pending_options.is_expired():
        kind = classify_message(text, has_pending_options=True)
        if kind == "new_order":
            user.pending_options = None
            return await _start_new_order(jid, user, text)
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
    return await _start_new_order(jid, user, text)
