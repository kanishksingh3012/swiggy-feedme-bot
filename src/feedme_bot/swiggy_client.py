"""Swiggy MCP tool-calling client.

Verified live (see plan notes): JSON-RPC 2.0 over Streamable HTTP, not
REST. Requires `Accept: application/json, text/event-stream` or the
server 406s. Domain/business errors surface as `isError: true` inside a
200 response, not as an HTTP error or a JSON-RPC `error` object — check
isError on every call, not just the HTTP status.

Only wrapping tools actually verified against live per-tool docs
(get_addresses, search_menu, update_food_cart, get_food_cart,
flush_food_cart, get_payment_options, place_food_order,
check_payment_status, confirm_order) — see the plan file's "never
invent tool names/parameters" rule. Add more as they're verified, not
before.
"""

import asyncio
from typing import Any

import httpx

from feedme_bot.config import settings
from feedme_bot.swiggy_auth import load_token


class SwiggyToolError(Exception):
    """isError: true came back inside an otherwise-200 JSON-RPC response."""


async def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    # load_token()/login() are blocking (opens a browser, waits on a local
    # HTTP callback for up to 5 minutes) — running it in a thread instead of
    # awaiting it directly keeps the event loop free to handle other
    # requests in the meantime, rather than freezing the whole server.
    token = await asyncio.to_thread(load_token)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{settings.swiggy_mcp_base_url}/food", headers=headers, json=payload
        )

    if response.status_code == 401:
        # No refresh grant exists — force a fresh interactive login and retry once.
        from feedme_bot.swiggy_auth import login

        await asyncio.to_thread(login)
        return await _call_tool(name, arguments)

    response.raise_for_status()
    result = response.json().get("result", {})
    if result.get("isError"):
        raise SwiggyToolError(result)
    return result


def _unwrap(result: dict[str, Any]) -> dict[str, Any]:
    """Verified live on get_addresses: despite what the docs describe, the
    actual payload sits under `structuredContent`, not `data` — `data` isn't
    present at all. Checking both rather than trusting either blindly, since
    this API's docs have now been wrong about response shape more than
    once. Falls back to the raw result if neither key is present."""
    if "structuredContent" in result:
        return result["structuredContent"]
    if "data" in result:
        return result["data"]
    return result


async def get_addresses() -> list[dict[str, Any]]:
    result = await _call_tool("get_addresses", {})
    return _unwrap(result).get("addresses", [])


async def search_menu(
    address_id: str,
    query: str,
    veg_only: bool = False,
    offset: int | None = None,
    restaurant_id: str | None = None,
) -> dict[str, Any]:
    args: dict[str, Any] = {"addressId": address_id, "query": query}
    if veg_only:
        args["vegFilter"] = 1
    if offset is not None:
        args["offset"] = offset
    if restaurant_id:
        args["restaurantIdOfAddedItem"] = restaurant_id
    result = await _call_tool("search_menu", args)
    return _unwrap(result)


async def search_restaurants(
    address_id: str, query: str, offset: int | None = None
) -> dict[str, Any]:
    args: dict[str, Any] = {"addressId": address_id, "query": query}
    if offset is not None:
        args["offset"] = offset
    result = await _call_tool("search_restaurants", args)
    return _unwrap(result)


def _unwrap_cart(result: dict[str, Any]) -> dict[str, Any]:
    """Cart-mutation responses (update_food_cart/get_food_cart) have a
    second, Swiggy-specific "data" nesting on top of the outer MCP
    envelope _unwrap() already handles — verified live: a successful
    response looks like {"statusCode":0,...,"data":{"items":[...],
    "pricing":{...}}}, while a failure looks like
    {"successful":false,"data":null,"titleMessage":...} with no nested
    items at all. Drill into the inner "data" only when it actually holds
    the cart payload, so failure responses (checked via _cart_error in
    handlers.py) aren't masked by this."""
    unwrapped = _unwrap(result)
    inner = unwrapped.get("data")
    if isinstance(inner, dict) and "items" in inner:
        return inner
    return unwrapped


async def update_food_cart(
    address_id: str, restaurant_id: str, menu_item_id: str, quantity: int = 1
) -> dict[str, Any]:
    args = {
        "addressId": address_id,
        "restaurantId": restaurant_id,
        "cartItems": [{"menu_item_id": menu_item_id, "quantity": quantity}],
    }
    result = await _call_tool("update_food_cart", args)
    return _unwrap_cart(result)


async def get_food_cart(address_id: str) -> dict[str, Any]:
    result = await _call_tool("get_food_cart", {"addressId": address_id})
    return _unwrap_cart(result)


async def flush_food_cart() -> dict[str, Any]:
    result = await _call_tool("flush_food_cart", {})
    return _unwrap(result)


async def get_payment_options(address_id: str) -> dict[str, Any]:
    result = await _call_tool("get_payment_options", {"addressId": address_id})
    return _unwrap(result)


async def place_food_order(
    address_id: str,
    payment_method: str,  # "Cash" or "UPI" — never "COD"/"PayWithQR", per verified findings
    generate_upi_qr: bool = False,
    intent_app: str | None = None,  # e.g. "gpay://upi/" — byte-for-byte from get_payment_options
) -> dict[str, Any]:
    args: dict[str, Any] = {"addressId": address_id, "paymentMethod": payment_method}
    if payment_method == "UPI" and generate_upi_qr:
        args["generateUPIQR"] = True
    if payment_method == "UPI" and intent_app:
        args["intentApp"] = intent_app
    result = await _call_tool("place_food_order", args)
    return _unwrap(result)


async def check_payment_status(paas_id: str, **extra: Any) -> dict[str, Any]:
    # extra: orderId/addressId/cartId/lat/lng, whichever place_food_order handed back —
    # optional, but pass through whatever's available for auto-confirm on Swiggy's side.
    args = {"paasId": paas_id, **{k: v for k, v in extra.items() if v is not None}}
    result = await _call_tool("check_payment_status", args)
    return _unwrap(result)


async def confirm_order(
    order_id: str, address_id: str, lat: float, lng: float, cart_id: str | None = None
) -> dict[str, Any]:
    args: dict[str, Any] = {
        "orderId": order_id,
        "addressId": address_id,
        "lat": lat,
        "lng": lng,
    }
    if cart_id:
        args["cartId"] = cart_id
    result = await _call_tool("confirm_order", args)
    return _unwrap(result)
