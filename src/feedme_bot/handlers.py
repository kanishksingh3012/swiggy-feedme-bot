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
    PendingDishRetry,
    PendingMoreOptions,
    PendingOptions,
    PendingPaymentChoice,
    UserState,
    store,
)


def _swiggy_error_text(exc: swiggy_client.SwiggyToolError) -> str:
    """SwiggyToolError carries the raw isError:true payload — pull the
    human-readable text out of it rather than showing the user a raw dict."""
    payload = exc.args[0] if exc.args else {}
    content = payload.get("content", []) if isinstance(payload, dict) else []
    for block in content:
        if isinstance(block, dict) and block.get("text"):
            return str(block["text"])
    return "an unexpected error from Swiggy"


# search_menu returns no ETA field at all (only search_restaurants would) — so
# intent.max_eta_mins can't actually be honored by this item-search-based flow.
# Not silently ignoring it: surfaced as a known gap here rather than faked.
GREETING = "Hey! Let's find you something good."
WHATSAPP_ADDRESS_BUTTON_CAP = 3  # WhatsApp reply buttons hard-cap at 3, verified live
CHANGE_ADDRESS_TRIGGER_WORDS = ("change", "different", "another", "switch", "new")


def _item_id(item: dict[str, Any]) -> str:
    # search_menu / get_restaurant_menu disagree on the id field name — see plan notes.
    return str(item.get("menu_item_id") or item.get("id"))


def _item_line(item: dict[str, Any]) -> str:
    name = item.get("name", "?")
    price = item.get("price", "?")
    restaurant = item.get("restaurant_name", "")
    line = f"{name} — ₹{price}"
    if restaurant:
        line += f" — {restaurant}"
    return line


def _total_line(cart: dict[str, Any] | None) -> str:
    """Real post-coupon total from the cart itself, not the bare item
    price — verified live: get_food_cart returns pricing.to_pay as the
    final payable amount, and offers.coupon_discount tells us whether a
    coupon Swiggy auto-applied server-side actually knocked anything off."""
    if not cart:
        return ""
    to_pay = (cart.get("pricing") or {}).get("to_pay")
    if to_pay is None:
        return ""
    offers = cart.get("offers") or {}
    coupon = offers.get("coupon_applied")
    discount = offers.get("coupon_discount") or 0
    line = f"Total to pay: ₹{to_pay}"
    if coupon and discount:
        line += f" ({coupon} applied, -₹{discount})"
    return line


def _greeting_prefix(user: UserState) -> str:
    if user.has_greeted:
        return ""
    user.has_greeted = True
    return f"{GREETING}\n\n"


def _resolve_address(jid: str, user: UserState, addresses: list[dict[str, Any]]) -> str | None:
    """Returns an address id only when there's truly nothing to choose
    (exactly one saved address), else None — caller must ask. Explicit
    direction: always ask when there's more than one address, every time,
    no remembered-default skip. (default_address_id / usage tracking stay
    in state.py, just unused for auto-resolution now — kept in case this
    changes again, not deleted.)"""
    if len(addresses) == 1:
        return addresses[0].get("id")
    return None


def _filter_candidates(
    items: list[dict[str, Any]], max_price: float | None
) -> list[dict[str, Any]]:
    """In-stock and addon-free always; price is a hard cap, never relaxed —
    a budget the user stated is a real requirement, not a suggestion, so
    this never substitutes something over it just to fill the slot.
    (Previously allowed up to 25% over when too few strict matches
    existed; removed — showing a pricier item without saying so violated
    the one thing the user actually asked for. Scarcity within budget is
    now handled by trying more query alternatives in _start_new_order
    instead of loosening the price.)

    Excluding hasAddons/hasVariants items is a coarse, deliberate tradeoff —
    search_menu can't tell us whether an addon group is mandatory (only the
    cart response's min/max constraints can, and that's only known after
    trying to add it), so this also excludes some optional-addon items that
    would've worked fine. Traded for never suggesting a dish that then fails
    at checkout, per explicit direction — see plan notes on INVALID_ADDON."""
    processable = [
        item
        for item in items
        if item.get("inStock", True) and not item.get("hasAddons") and not item.get("hasVariants")
    ]
    if max_price is None:
        return processable
    return [item for item in processable if item.get("price", 0) <= max_price]


async def _ask_address(jid: str, user: UserState, addresses: list[dict[str, Any]], text: str) -> None:
    # Buttons, not a list — WhatsApp reply buttons open with a single tap,
    # no bottom-sheet step, but hard-cap at 3. Ranked by actual usage
    # frequency (falls back to Home-first before any usage history exists).
    # Known tradeoff: any address outside the top 3 isn't reachable through
    # this picker at all — accepted explicitly in favor of fewer taps.
    top = store.top_addresses(jid, addresses, WHATSAPP_ADDRESS_BUTTON_CAP)
    user.pending_address_choice = PendingAddressChoice(candidates=top, original_text=text)
    buttons = [
        (f"addr:{a.get('id')}", a.get("addressTag") or f"Address {i + 1}")
        for i, a in enumerate(top)
    ]
    body = _greeting_prefix(user) + "Which address should this go to?"
    await whatsapp.send_reply_buttons(jid, body, buttons)


def _normalize_restaurant_name(name: str) -> str:
    # Real names carry apostrophes/hyphens users don't bother typing
    # ("La Pino'z" vs "La Pinoz") — verified live this breaks a plain
    # substring match, so strip everything but letters/digits/spaces
    # before comparing rather than requiring an exact-punctuation match.
    return "".join(ch for ch in name.lower() if ch.isalnum() or ch.isspace()).strip()


async def _resolve_restaurant(address_id: str, stated_name: str) -> str | None:
    """Resolves a user-stated restaurant name to a real restaurant id, or
    None if nothing actually matches. Deliberately NOT "take the top
    result" — verified live that search_restaurants ranks sponsored "(Ad)"
    listings first regardless of name match (a search for "pizza" put two
    unrelated sponsored pizza places ahead of anything else), so trusting
    result order would silently scope the search to the wrong restaurant."""
    result = await swiggy_client.search_restaurants(address_id, stated_name)
    needle = _normalize_restaurant_name(stated_name)
    for r in result.get("restaurants", []):
        candidate = _normalize_restaurant_name((r.get("name") or "").replace("(Ad)", ""))
        if needle in candidate or candidate in needle:
            return r.get("id")
    return None


async def _start_new_order(
    jid: str, user: UserState, text: str, resolved_address_id: str | None = None
) -> None:
    recent_names = [o.get("name", "") for o in user.order_history[-5:] if o.get("name")]
    intent = extract_intent(text, order_history=recent_names or None)

    addresses = await swiggy_client.get_addresses()
    if not addresses:
        await whatsapp.send_text(
            jid, "No saved address on your Swiggy account — add one before I can search."
        )
        return

    # resolved_address_id is set when this call is a direct continuation
    # right after the user just tapped an address — skips re-resolving
    # (which would otherwise always ask again, since there's no remembered
    # default to fall back on anymore, causing an infinite ask loop).
    address_id = resolved_address_id or _resolve_address(jid, user, addresses)
    if address_id is None:
        await _ask_address(jid, user, addresses, text)
        return
    store.record_address_use(jid, address_id)

    if intent.query is None:
        # No real signal about what food is wanted — address is settled,
        # so ask directly rather than guessing a dish (or silently reaching
        # into order history) just to have something to search with.
        user.pending_dish_retry = PendingDishRetry(address_id=address_id)
        await whatsapp.send_text(jid, _greeting_prefix(user) + "What would you like to eat?")
        return

    restaurant_id: str | None = None
    if intent.restaurant:
        restaurant_id = await _resolve_restaurant(address_id, intent.restaurant)
        if restaurant_id is None:
            user.pending_dish_retry = PendingDishRetry(address_id=address_id)
            await whatsapp.send_text(
                jid,
                f"Couldn't find a restaurant called '{intent.restaurant}' near you — "
                "try without naming it, or check the spelling?",
            )
            return

    # A mood/craving translates to one guessed dish name, and that guess can
    # easily have nothing nearby even when the mood itself has plenty of
    # matches — try the LLM's ranked alternatives before giving up and
    # pushing the "name a dish yourself" burden back onto the user.
    candidates: list[dict[str, Any]] = []
    for query in (intent.query, *intent.query_alternatives):
        search = await swiggy_client.search_menu(
            address_id,
            query,
            veg_only=(intent.dietary_preference == "veg"),
            restaurant_id=restaurant_id,
        )
        raw_candidates = search.get("items", [])
        candidates = _filter_candidates(raw_candidates, intent.max_price)
        if len(candidates) >= 2:
            break

    if len(candidates) < 2:
        user.pending_dish_retry = PendingDishRetry(address_id=address_id)
        budget_note = f" under ₹{intent.max_price}" if intent.max_price is not None else ""
        where = f" at {intent.restaurant}" if intent.restaurant else " near you"
        await whatsapp.send_text(
            jid,
            f"Couldn't find enough matches{budget_note}{where} for that — "
            "try describing it differently, or bump the budget?",
        )
        return

    try:
        safe_pick, mood_pick = rank.rank_candidates(intent, candidates)
    except rank.NoValidCandidatesError:
        user.pending_dish_retry = PendingDishRetry(address_id=address_id)
        await whatsapp.send_text(jid, "Had trouble picking between the options — try rephrasing?")
        return

    shown_ids = {_item_id(safe_pick), _item_id(mood_pick)}
    remaining = [c for c in candidates if _item_id(c) not in shown_ids]
    user.pending_options = PendingOptions(
        safe_pick=safe_pick, mood_pick=mood_pick, address_id=address_id, remaining=remaining
    )
    body = _greeting_prefix(user)
    if intent.high_protein:
        body += "High-protein picks:\n\n"
    body += (
        f"Safe Pick (closest match, quickest): {_item_line(safe_pick)}\n\n"
        f"Mood Pick (leans into what you're craving): {_item_line(mood_pick)}"
    )
    buttons = [("pick:safe", "Safe Pick"), ("pick:mood", "Mood Pick")]
    if remaining:
        buttons.append(("pick:more", "Give me more"))
    await whatsapp.send_reply_buttons(
        jid, body, buttons, footer="I'll confirm before ordering anything"
    )


def _cart_error(cart: dict[str, Any]) -> str | None:
    """Cart-mutation responses use a different envelope than search/address
    calls (statusCode/successful/titleMessage, not items/data) — verified
    live after a real INVALID_ADDON failure. Surface Swiggy's own message
    rather than a generic fallback when successful is explicitly False."""
    if cart.get("successful") is False:
        return (
            cart.get("titleMessage")
            or cart.get("statusMessage")
            or "That item couldn't be added — the restaurant may require picking a size/variant we don't handle yet."
        )
    return None


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

    update_result = await swiggy_client.update_food_cart(address_id, restaurant_id, str(menu_item_id))
    error = _cart_error(update_result)
    if error:
        # Known gap: this is very likely a mandatory-addon item we don't
        # handle yet (see plan checklist) — clear the now-invalid cart so a
        # retry isn't blocked by leftover bad state, rather than silently
        # leaving it stuck.
        await swiggy_client.flush_food_cart()
        return None, error

    cart = await swiggy_client.get_food_cart(address_id)
    error = _cart_error(cart)
    if error:
        await swiggy_client.flush_food_cart()
        return None, error

    cart_items = cart.get("items", [])
    # Verified live: search_menu's menu_item_id is a string, the cart
    # response's is an int for the same item — compare as strings on both
    # sides rather than assume either type.
    matching = next(
        (c for c in cart_items if str(c.get("menu_item_id")) == str(menu_item_id)), None
    )
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
            await whatsapp.send_text(jid, "Order placed! Track it in the Swiggy app.")
            return

        if status.get("isTerminalFailure"):
            await whatsapp.send_text(jid, "Payment didn't go through — want to try again?")
            return

    await whatsapp.send_text(jid, "Still waiting on payment — check the Swiggy app if this takes a while.")


async def _confirm_pick(jid: str, user: UserState, item: dict[str, Any], address_id: str) -> None:
    """Shared by every path that ends in "user picked one specific item"
    (Safe/Mood pick, or one of the "give me more" alternatives) — adds it
    to the real cart now so the confirm prompt can show the actual
    post-coupon total, then gates on an explicit yes/no before payment."""
    cart, error = await _add_to_cart_and_revalidate(item, address_id)
    if error:
        await whatsapp.send_text(jid, error)
        return

    user.pending_confirmation = PendingConfirmation(item=item, address_id=address_id)
    await whatsapp.send_reply_buttons(
        jid, f"Confirm: {_item_line(item)}\n{_total_line(cart)}\n\nPlace this order?",
        [("confirm:yes", "Yes"), ("confirm:no", "No")],
    )


async def _offer_more_options(jid: str, user: UserState, pending_o: PendingOptions) -> None:
    """"Give me more" — real leftover candidates from the same search, not
    another LLM guess. Deterministically sorted (rating desc, price asc)
    rather than ranked, since this is just "show me what else you found,"
    not another safe/mood-style judgment call."""
    top3 = sorted(
        pending_o.remaining,
        key=lambda c: (-(c.get("rating") or 0), c.get("price", 0)),
    )[:3]
    user.pending_more_options = PendingMoreOptions(items=top3, address_id=pending_o.address_id)
    lines = [f"{i + 1}. {_item_line(item)}" for i, item in enumerate(top3)]
    buttons = [(f"more:{i}", f"Option {i + 1}") for i in range(len(top3))]
    await whatsapp.send_reply_buttons(jid, "\n".join(lines), buttons)


WHATSAPP_PAYMENT_BUTTON_CAP = 3


async def _offer_payment_options(
    jid: str, user: UserState, item: dict[str, Any], address_id: str
) -> None:
    """Real payment methods only, from get_payment_options, never invented
    — verified live that no "card" option exists on this account/API at
    all, only UPI apps (each deep-links to that specific app), QR
    (desktop-oriented), and COD. Picking one of the resulting buttons IS
    the final purchase trigger, handled in handle_message's
    pending_payment_choice branch."""
    payment_data = await swiggy_client.get_payment_options(address_id)
    options: dict[str, dict[str, Any]] = {}
    buttons: list[tuple[str, str]] = []

    cod = payment_data.get("cod", {})
    if cod.get("available"):
        bid = "pay:cod"
        options[bid] = {"payment_method": "Cash"}
        buttons.append((bid, (cod.get("displayName") or "Cash on Delivery")[:20]))

    mobile_methods = payment_data.get("platforms", {}).get("mobile", {}).get("methods", [])
    for method in mobile_methods:
        if len(buttons) >= WHATSAPP_PAYMENT_BUTTON_CAP:
            break
        app_id = method.get("id")
        if not app_id or not method.get("enabled", True):
            continue
        bid = f"pay:upi:{app_id}"
        options[bid] = {"payment_method": "UPI", "intent_app": app_id}
        buttons.append((bid, (method.get("displayName") or "UPI")[:20]))

    if not buttons:
        # Matches the tool's own guidance: don't attempt place_food_order
        # with no real methods available.
        await whatsapp.send_text(
            jid, "No payment methods are available for this order right now — try again later?"
        )
        return

    user.pending_payment_choice = PendingPaymentChoice(
        item=item, address_id=address_id, options=options
    )
    price = payment_data.get("paymentAmount")
    body = f"How do you want to pay{f' (₹{price})' if price else ''}?"
    await whatsapp.send_reply_buttons(jid, body, buttons[:WHATSAPP_PAYMENT_BUTTON_CAP])


async def handle_message(jid: str, text: str, interactive_id: str | None = None) -> None:
    user = store.get(jid)

    # Checked before any pending state, on purpose — "change address" should
    # work no matter where the user is in the flow, not get misclassified as
    # a dish "modification" by whatever's currently pending.
    lowered_text = text.strip().lower()
    wants_address_change = (
        interactive_id is None
        and "address" in lowered_text
        and any(w in lowered_text for w in CHANGE_ADDRESS_TRIGGER_WORDS)
    )
    if wants_address_change:
        user.pending_options = None
        user.pending_confirmation = None
        user.pending_address_choice = None
        user.pending_dish_retry = None
        user.pending_more_options = None
        user.default_address_id = None
        addresses = await swiggy_client.get_addresses()
        if not addresses:
            await whatsapp.send_text(jid, "No saved addresses on your Swiggy account to switch to.")
            return
        await _ask_address(jid, user, addresses, "")
        return

    # Picking a payment method IS the final purchase trigger — this is the
    # actual hard gate before place_food_order now, checked first.
    if user.pending_payment_choice is not None:
        pending_p = user.pending_payment_choice
        if pending_p.is_expired():
            user.pending_payment_choice = None
            # By this stage the item's been sitting in the real cart since
            # the selection step (see _confirm_pick) — clear it rather
            # than leave a stale reservation nothing points at anymore.
            await swiggy_client.flush_food_cart()
            await whatsapp.send_text(jid, "That timed out — want to confirm the order again?")
            return

        spec = pending_p.options.get(interactive_id or "")
        if spec is None:
            await whatsapp.send_text(jid, "Tap one of the payment options above.")
            return

        item = pending_p.item
        address_id = pending_p.address_id
        user.pending_payment_choice = None

        try:
            result = await swiggy_client.place_food_order(
                address_id,
                payment_method=spec["payment_method"],
                generate_upi_qr=spec.get("generate_upi_qr", False),
                intent_app=spec.get("intent_app"),
            )
        except swiggy_client.SwiggyToolError as exc:
            reason = _swiggy_error_text(exc)
            await whatsapp.send_text(jid, f"Couldn't place the order: {reason}. Nothing was charged.")
            return
        except Exception:
            await whatsapp.send_text(
                jid, "Something went wrong placing the order — nothing was charged. Try again?"
            )
            raise

        if result.get("status") == "PENDING_PAYMENT" or result.get("normalizedStatus") == "pending":
            user.order_history.append(item)
            await _poll_payment_and_finalize(jid, result, address_id)
            return

        user.order_history.append(item)
        await whatsapp.send_text(
            jid, f"Order placed! {item.get('name', 'Your order')} is on its way. Track it in the Swiggy app."
        )
        return

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
        if not original_text:
            # Triggered by a standalone "change address" request, not a
            # pending order — just confirm the switch, don't search on a
            # blank query.
            await whatsapp.send_text(jid, "Got it — using that address from now on.")
            return
        await _start_new_order(jid, user, original_text, resolved_address_id=chosen_id)
        return

    if user.pending_confirmation is not None:
        pending_c = user.pending_confirmation
        if pending_c.is_expired():
            user.pending_confirmation = None
            # The item's already sitting in the real cart from the selection
            # step — clear it out rather than leaving a stale reservation.
            await swiggy_client.flush_food_cart()
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
            # Already added to cart when the pick was made — go straight to payment.
            await _offer_payment_options(jid, user, item, address_id)
        elif decision == "no":
            user.pending_confirmation = None
            await swiggy_client.flush_food_cart()
            await whatsapp.send_text(jid, "No worries, cancelled.")
        else:
            await whatsapp.send_text(jid, "Just tap Yes or No to confirm.")
        return

    if user.pending_options is not None and not user.pending_options.is_expired():
        pending_o = user.pending_options

        if interactive_id in ("pick:safe", "pick:mood"):
            choice = "safe" if interactive_id == "pick:safe" else "mood"
        elif interactive_id == "pick:more":
            user.pending_options = None
            await _offer_more_options(jid, user, pending_o)
            return
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
            if kind == "more_options":
                if not pending_o.remaining:
                    await whatsapp.send_text(
                        jid, "That's everything I found nearby for this — tap Safe Pick or Mood Pick above?"
                    )
                    return
                user.pending_options = None
                await _offer_more_options(jid, user, pending_o)
                return
            if kind != "selection":
                await whatsapp.send_text(
                    jid, "Not sure that's about food — still got options waiting if you want them."
                )
                return
            choice = resolve_selection(text)
            if choice == "unclear":
                await whatsapp.send_text(jid, "Not sure which one — tap Safe Pick or Mood Pick above.")
                return

        item = pending_o.safe_pick if choice == "safe" else pending_o.mood_pick
        address_id = pending_o.address_id
        user.pending_options = None
        await _confirm_pick(jid, user, item, address_id)
        return

    if user.pending_options is not None and user.pending_options.is_expired():
        user.pending_options = None

    if user.pending_more_options is not None:
        pending_m = user.pending_more_options
        if pending_m.is_expired():
            user.pending_more_options = None
            await whatsapp.send_text(jid, "That offer's gone stale — want me to search again?")
            return

        index: int | None = None
        if interactive_id and interactive_id.startswith("more:"):
            index = int(interactive_id.split(":", 1)[1])
        else:
            kind = classify_message(text, has_pending_options=True)
            if kind == "new_order":
                user.pending_more_options = None
                await _start_new_order(jid, user, text)
                return
            if kind == "cancellation":
                user.pending_more_options = None
                await whatsapp.send_text(jid, "No worries, cancelled.")
                return
            choice_num = resolve_numbered_choice(text, len(pending_m.items))
            if choice_num is not None:
                index = choice_num - 1

        if index is None or not (0 <= index < len(pending_m.items)):
            await whatsapp.send_text(jid, "Not sure which one — tap one of the options above.")
            return

        item = pending_m.items[index]
        address_id = pending_m.address_id
        user.pending_more_options = None
        await _confirm_pick(jid, user, item, address_id)
        return

    # Only set right after a failed search whose address was already
    # resolved — this message is a continuation of that same attempt
    # ("try a different dish"), not a fresh order, so don't re-ask address.
    resolved_address_id = None
    if user.pending_dish_retry is not None:
        if not user.pending_dish_retry.is_expired():
            resolved_address_id = user.pending_dish_retry.address_id
        user.pending_dish_retry = None

    kind = classify_message(text, has_pending_options=False)
    if kind == "not_food":
        # A flat "I only handle X" reads as a rejection, especially to a
        # bare "Hi" — this should feel like an invitation to order, not a
        # scope statement. Uses the same one-time greeting as a real order
        # would, so a first "Hi" gets the full warm welcome and a later
        # off-topic aside just gets a light nudge, not a repeated intro.
        await whatsapp.send_text(
            jid, _greeting_prefix(user) + "What are you in the mood for? I can find and order it right here."
        )
        return
    await _start_new_order(jid, user, text, resolved_address_id=resolved_address_id)
