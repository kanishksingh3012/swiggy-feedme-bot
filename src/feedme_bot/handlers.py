"""Core message-handling logic, deliberately kept separate from the
FastAPI/WhatsApp transport layer (main.py) so it can be exercised
directly — e.g. from a script or test — without a live webhook.

Sends replies itself (via whatsapp.py) rather than returning a string,
since replies are now interactive messages (buttons/lists), not just
plain text — main.py just hands off the incoming message and moves on.

Tap-first design: every pending state is driven primarily by a known
interactive_id (a button/list-row tap), with the old LLM-based text
resolvers kept as a fallback for when the user types instead of tapping.
"""

import asyncio
from typing import Any

from feedme_bot import rank, swiggy_client, whatsapp
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
GREETING = "Hey! Let's find you something good."
WHATSAPP_ADDRESS_ROW_CAP = 10  # verified live against Meta's docs


def _item_line(item: dict[str, Any]) -> str:
    name = item.get("name", "?")
    price = item.get("price", "?")
    restaurant = item.get("restaurant_name", "")
    line = f"{name} — ₹{price}"
    if restaurant:
        line += f" — {restaurant}"
    return line


def _greeting_prefix(user: UserState) -> str:
    if user.has_greeted:
        return ""
    user.has_greeted = True
    return f"{GREETING}\n\n"


def _resolve_address(jid: str, user: UserState, addresses: list[dict[str, Any]]) -> str | None:
    """Returns an address id if unambiguous (exactly one address, or a
    remembered choice from a previous order), else None — caller must ask.
    Deliberately does NOT auto-pick a uniquely "Home"-tagged address on its
    own; that silent behavior was confusing (looked like a bug) — always
    ask when there's real choice to make, just make Home easy to spot."""
    if len(addresses) == 1:
        return addresses[0].get("id")
    if user.default_address_id:
        return user.default_address_id
    return None


def _sort_home_first(addresses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        addresses, key=lambda a: 0 if (a.get("addressTag") or "").strip().lower() == "home" else 1
    )


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


async def _ask_address(jid: str, user: UserState, addresses: list[dict[str, Any]], text: str) -> None:
    ordered = _sort_home_first(addresses)[:WHATSAPP_ADDRESS_ROW_CAP]
    user.pending_address_choice = PendingAddressChoice(candidates=ordered, original_text=text)
    rows = [
        (
            f"addr:{a.get('id')}",
            a.get("addressTag") or f"Address {i + 1}",
            (a.get("addressLine") or "")[:72],
        )
        for i, a in enumerate(ordered)
    ]
    body = _greeting_prefix(user) + "Which address should this go to?"
    await whatsapp.send_list(jid, body, "Choose address", rows)


async def _start_new_order(jid: str, user: UserState, text: str) -> None:
    recent_names = [o.get("name", "") for o in user.order_history[-5:] if o.get("name")]
    intent = extract_intent(text, order_history=recent_names or None)

    addresses = await swiggy_client.get_addresses()
    if not addresses:
        await whatsapp.send_text(
            jid, "No saved address on your Swiggy account — add one before I can search."
        )
        return

    address_id = _resolve_address(jid, user, addresses)
    if address_id is None:
        await _ask_address(jid, user, addresses, text)
        return

    search = await swiggy_client.search_menu(
        address_id, intent.query, veg_only=(intent.dietary_preference == "veg")
    )
    raw_candidates = search.get("items", [])
    candidates, was_relaxed = _filter_candidates(raw_candidates, intent.max_price)

    if len(candidates) < 2:
        await whatsapp.send_text(
            jid, f"Couldn't find enough matches for '{intent.query}' near you — try a different dish?"
        )
        return

    try:
        safe_pick, mood_pick = rank.rank_candidates(intent, candidates)
    except rank.NoValidCandidatesError:
        await whatsapp.send_text(jid, "Had trouble picking between the options — try rephrasing?")
        return

    user.pending_options = PendingOptions(
        safe_pick=safe_pick, mood_pick=mood_pick, address_id=address_id
    )
    body = _greeting_prefix(user)
    if intent.high_protein:
        body += "High-protein picks:\n\n"
    if was_relaxed and intent.max_price is not None:
        body += f"(nothing solid under ₹{intent.max_price}, showing closest matches)\n\n"
    body += f"Safe Pick: {_item_line(safe_pick)}\n\nMood Pick: {_item_line(mood_pick)}"
    await whatsapp.send_reply_buttons(
        jid,
        body,
        [("pick:safe", "Safe Pick"), ("pick:mood", "Mood Pick")],
        footer="I'll confirm before ordering anything",
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
    jid: str, place_result: dict[str, Any], address_id: str
) -> None:
    paas_id = place_result.get("paasId")
    if not paas_id:
        await whatsapp.send_text(jid, "Payment couldn't be started — try again in a moment?")
        return

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
            if not status.get("confirmed"):
                order_id = status.get("orderId") or place_result.get("orderId")
                lat, lng = place_result.get("lat"), place_result.get("lng")
                if order_id and lat is not None and lng is not None:
                    await swiggy_client.confirm_order(
                        order_id, address_id, lat, lng, cart_id=place_result.get("cartId")
                    )
            await whatsapp.send_text(
                jid, "Order placed! Track it in the Swiggy app — no more messages from me on this one."
            )
            return

        if status.get("isTerminalFailure"):
            await whatsapp.send_text(jid, "Payment didn't go through — want to try again?")
            return

    await whatsapp.send_text(jid, "Still waiting on payment — check the Swiggy app if this takes a while.")


async def handle_message(jid: str, text: str, interactive_id: str | None = None) -> None:
    user = store.get(jid)

    # A pending checkout confirmation takes priority over everything else —
    # this is the hard gate before place_food_order, never skip it. Address
    # picking comes next since it blocks the order from even being searched.
    if user.pending_confirmation is None and user.pending_address_choice is not None:
        pending = user.pending_address_choice
        if pending.is_expired():
            user.pending_address_choice = None
            await whatsapp.send_text(jid, "That timed out — send your order again?")
            return

        chosen_id: str | None = None
        if interactive_id and interactive_id.startswith("addr:"):
            chosen_id = interactive_id.split(":", 1)[1]
        else:
            choice_num = resolve_numbered_choice(text, len(pending.candidates))
            if choice_num is not None:
                chosen_id = pending.candidates[choice_num - 1].get("id")

        if chosen_id is None:
            await whatsapp.send_text(jid, "Not sure which address — tap one from the list above.")
            return

        original_text = pending.original_text
        user.pending_address_choice = None
        store.set_default_address(jid, chosen_id)
        await _start_new_order(jid, user, original_text)
        return

    if user.pending_confirmation is not None:
        pending_c = user.pending_confirmation
        if pending_c.is_expired():
            user.pending_confirmation = None
            await whatsapp.send_text(jid, "That offer's gone stale — want me to search again?")
            return

        if interactive_id == "confirm:yes":
            decision = "yes"
        elif interactive_id == "confirm:no":
            decision = "no"
        else:
            decision = resolve_confirmation(text)

        if decision == "yes":
            item = pending_c.item
            address_id = pending_c.address_id
            user.pending_confirmation = None

            cart, error = await _add_to_cart_and_revalidate(item, address_id)
            if error:
                await whatsapp.send_text(jid, error)
                return

            fresh_price = cart.get("pricing", {}).get("to_pay") if cart else None
            # Default to Cash/COD for now — payment method choice (UPI apps,
            # COD) is the next increment, not built yet, see plan checklist.
            result = await swiggy_client.place_food_order(address_id, payment_method="Cash")
            user.order_history.append(item)

            if result.get("status") == "PENDING_PAYMENT" or result.get("normalizedStatus") == "pending":
                await _poll_payment_and_finalize(jid, result, address_id)
                return

            price_note = f" (₹{fresh_price})" if fresh_price else ""
            await whatsapp.send_text(
                jid,
                f"Order placed! {item.get('name', 'Your order')}{price_note} is on its way. "
                "Track it in the Swiggy app — no more messages from me on this one.",
            )
        elif decision == "no":
            user.pending_confirmation = None
            await whatsapp.send_text(jid, "No worries, cancelled.")
        else:
            await whatsapp.send_text(jid, "Just tap Yes or No to confirm.")
        return

    if user.pending_options is not None and not user.pending_options.is_expired():
        pending_o = user.pending_options

        if interactive_id in ("pick:safe", "pick:mood"):
            choice = "safe" if interactive_id == "pick:safe" else "mood"
        else:
            kind = classify_message(text, has_pending_options=True)
            if kind == "new_order":
                user.pending_options = None
                await _start_new_order(jid, user, text)
                return
            if kind == "cancellation":
                user.pending_options = None
                await whatsapp.send_text(jid, "No worries, cancelled.")
                return
            if kind == "modification":
                # TODO: merge the modification into the existing search rather
                # than discarding it — deliberately not built yet.
                user.pending_options = None
                await whatsapp.send_text(jid, "Got it — send your updated request and I'll search again.")
                return
            if kind != "selection":
                await whatsapp.send_text(
                    jid, "Not sure that's about food — still got two options waiting if you want them."
                )
                return
            choice = resolve_selection(text)
            if choice == "unclear":
                await whatsapp.send_text(jid, "Not sure which one — tap Safe Pick or Mood Pick above.")
                return

        item = pending_o.safe_pick if choice == "safe" else pending_o.mood_pick
        address_id = pending_o.address_id
        user.pending_options = None
        user.pending_confirmation = PendingConfirmation(item=item, address_id=address_id)
        await whatsapp.send_reply_buttons(
            jid, f"Confirm: {_item_line(item)}\n\nPlace this order?", [("confirm:yes", "Yes"), ("confirm:no", "No")]
        )
        return

    if user.pending_options is not None and user.pending_options.is_expired():
        user.pending_options = None

    kind = classify_message(text, has_pending_options=False)
    if kind == "not_food":
        await whatsapp.send_text(jid, "I only handle food orders here — tell me what you're craving.")
        return
    await _start_new_order(jid, user, text)
