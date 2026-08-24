"""Swiggy MCP tool-calling client.

Verified live (see plan notes): JSON-RPC 2.0 over Streamable HTTP, not
REST. Requires `Accept: application/json, text/event-stream` or the
server 406s. Domain/business errors surface as `isError: true` inside a
200 response, not as an HTTP error or a JSON-RPC `error` object — check
isError on every call, not just the HTTP status.

Only wrapping the tools actually verified against live per-tool docs
(get_addresses, search_menu, place_food_order) — see the plan file's
"never invent tool names/parameters" rule. Add more as they're verified,
not before.
"""

from typing import Any

import httpx

from feedme_bot.config import settings
from feedme_bot.swiggy_auth import load_token


class SwiggyToolError(Exception):
    """isError: true came back inside an otherwise-200 JSON-RPC response."""


async def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    token = load_token()
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

        login()
        return await _call_tool(name, arguments)

    response.raise_for_status()
    result = response.json().get("result", {})
    if result.get("isError"):
        raise SwiggyToolError(result)
    return result


async def get_addresses() -> list[dict[str, Any]]:
    result = await _call_tool("get_addresses", {})
    return result.get("data", {}).get("addresses", result.get("data", []))


async def search_menu(
    address_id: str, query: str, veg_only: bool = False, offset: int | None = None
) -> dict[str, Any]:
    args: dict[str, Any] = {"addressId": address_id, "query": query}
    if veg_only:
        args["vegFilter"] = 1
    if offset is not None:
        args["offset"] = offset
    result = await _call_tool("search_menu", args)
    return result.get("data", result)


async def update_food_cart(
    address_id: str, restaurant_id: str, menu_item_id: str, quantity: int = 1
) -> dict[str, Any]:
    args = {
        "addressId": address_id,
        "restaurantId": restaurant_id,
        "cartItems": [{"menu_item_id": menu_item_id, "quantity": quantity}],
    }
    result = await _call_tool("update_food_cart", args)
    return result.get("data", result)


async def get_food_cart(address_id: str) -> dict[str, Any]:
    result = await _call_tool("get_food_cart", {"addressId": address_id})
    return result.get("data", result)


async def place_food_order(
    address_id: str,
    payment_method: str,  # "Cash" or "UPI" — never "COD"/"PayWithQR", per verified findings
    generate_upi_qr: bool = False,
) -> dict[str, Any]:
    args: dict[str, Any] = {"addressId": address_id, "paymentMethod": payment_method}
    if payment_method == "UPI" and generate_upi_qr:
        args["generateUPIQR"] = True
    return await _call_tool("place_food_order", args)


async def check_payment_status(paas_id: str, **extra: Any) -> dict[str, Any]:
    # extra: orderId/addressId/cartId/lat/lng, whichever place_food_order handed back —
    # optional, but pass through whatever's available for auto-confirm on Swiggy's side.
    args = {"paasId": paas_id, **{k: v for k, v in extra.items() if v is not None}}
    result = await _call_tool("check_payment_status", args)
    return result.get("data", result)


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
    return result.get("data", result)
