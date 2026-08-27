import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# TTL for a set of presented options before they're considered stale and
# must be re-searched rather than checked out against (see plan notes on
# price/availability drift between search and confirm).
PENDING_OPTIONS_TTL_SECONDS = 15 * 60

# Address choice (and a bare retry-with-a-different-dish) carry no price or
# availability drift risk at all — it's just "where should this go" / "who
# was I talking to a minute ago", not a live cart quote going stale. A real
# human reading a WhatsApp message and tapping a button 20+ minutes later is
# completely normal, so these get a much longer leash than priced options.
PENDING_ADDRESS_TTL_SECONDS = 60 * 60

DEFAULT_ADDRESS_PATH = Path("~/.config/feedme-bot/default_addresses.json").expanduser()
ADDRESS_USAGE_PATH = Path("~/.config/feedme-bot/address_usage.json").expanduser()


@dataclass
class PendingOptions:
    safe_pick: dict[str, Any]
    mood_pick: dict[str, Any]
    address_id: str
    # Everything else that passed the price/stock filter, minus safe_pick and
    # mood_pick — kept around so "give me more" can serve real leftover
    # candidates instantly instead of re-searching or, worse, guessing.
    remaining: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def is_expired(self) -> bool:
        return time.time() - self.created_at > PENDING_OPTIONS_TTL_SECONDS


@dataclass
class PendingMoreOptions:
    """Up to 3 additional candidates shown after "give me more" — same
    price/availability drift risk as the original pair, so same TTL."""

    items: list[dict[str, Any]]
    address_id: str
    created_at: float = field(default_factory=time.time)

    def is_expired(self) -> bool:
        return time.time() - self.created_at > PENDING_OPTIONS_TTL_SECONDS


@dataclass
class PendingConfirmation:
    """The user picked an option; waiting on an explicit yes before checkout."""

    item: dict[str, Any]
    address_id: str
    created_at: float = field(default_factory=time.time)

    def is_expired(self) -> bool:
        return time.time() - self.created_at > PENDING_OPTIONS_TTL_SECONDS


@dataclass
class PendingAddressChoice:
    """Multiple saved addresses, no established default yet — asked once,
    waiting on a reply so we can remember it going forward."""

    candidates: list[dict[str, Any]]
    original_text: str
    created_at: float = field(default_factory=time.time)

    def is_expired(self) -> bool:
        return time.time() - self.created_at > PENDING_ADDRESS_TTL_SECONDS


@dataclass
class PendingDishRetry:
    """A search came up empty/ambiguous after address was already resolved —
    the very next message continues *this* order attempt rather than
    starting a fresh one. Without this, "try a different dish" falls through
    to _start_new_order's normal path, which always re-asks for an address
    (per the explicit "never silently reuse a remembered default" rule) —
    correct for an actual new order, wrong for "that guess didn't work,
    here's another dish" in the same breath."""

    address_id: str
    created_at: float = field(default_factory=time.time)

    def is_expired(self) -> bool:
        return time.time() - self.created_at > PENDING_ADDRESS_TTL_SECONDS


@dataclass
class PendingPaymentChoice:
    """Cart is validated and ready — waiting on which real payment method
    (from get_payment_options, never invented) to actually place the order
    with. Picking one of these IS the final purchase trigger."""

    item: dict[str, Any]
    address_id: str
    options: dict[str, dict[str, Any]]  # button_id -> {payment_method, intent_app, generate_upi_qr}
    created_at: float = field(default_factory=time.time)

    def is_expired(self) -> bool:
        return time.time() - self.created_at > PENDING_OPTIONS_TTL_SECONDS


@dataclass
class UserState:
    pending_options: PendingOptions | None = None
    pending_confirmation: PendingConfirmation | None = None
    pending_address_choice: PendingAddressChoice | None = None
    pending_payment_choice: PendingPaymentChoice | None = None
    pending_dish_retry: PendingDishRetry | None = None
    pending_more_options: PendingMoreOptions | None = None
    order_history: list[dict[str, Any]] = field(default_factory=list)
    default_address_id: str | None = None
    has_greeted: bool = False


def _load_default_addresses() -> dict[str, str]:
    if not DEFAULT_ADDRESS_PATH.exists():
        return {}
    try:
        return json.loads(DEFAULT_ADDRESS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_default_address(jid: str, address_id: str) -> None:
    data = _load_default_addresses()
    data[jid] = address_id
    DEFAULT_ADDRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_ADDRESS_PATH.write_text(json.dumps(data, indent=2))


def _load_address_usage() -> dict[str, dict[str, int]]:
    if not ADDRESS_USAGE_PATH.exists():
        return {}
    try:
        return json.loads(ADDRESS_USAGE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_address_usage(data: dict[str, dict[str, int]]) -> None:
    ADDRESS_USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    ADDRESS_USAGE_PATH.write_text(json.dumps(data, indent=2))


class StateStore:
    """In-memory, JID-keyed session state.

    Personal-tool phase 1 has exactly one real key in here, but keying by
    JID from day one avoids a rewrite when a second user shows up later.
    Mostly not persisted — a restart clears in-flight sessions, which is
    fine given everything mid-flight has a TTL anyway. The one exception
    is default_address_id, persisted to disk separately since re-asking
    "which address?" after every dev restart defeats the point of it.
    """

    def __init__(self) -> None:
        self._users: dict[str, UserState] = {}
        self._default_addresses = _load_default_addresses()
        self._address_usage = _load_address_usage()
        self._locks: dict[str, asyncio.Lock] = {}

    def get_lock(self, jid: str) -> asyncio.Lock:
        """One lock per JID so two webhook deliveries for the same user
        (a Meta retry, a double-tap) can't race on the same UserState —
        e.g. both reading pending_confirmation before either clears it,
        which could double-add to cart or double-place a real order."""
        return self._locks.setdefault(jid, asyncio.Lock())

    def get(self, jid: str) -> UserState:
        user = self._users.setdefault(jid, UserState())
        if user.default_address_id is None and jid in self._default_addresses:
            user.default_address_id = self._default_addresses[jid]
        return user

    def set_default_address(self, jid: str, address_id: str) -> None:
        self.get(jid).default_address_id = address_id
        self._default_addresses[jid] = address_id
        _save_default_address(jid, address_id)

    def record_address_use(self, jid: str, address_id: str) -> None:
        per_user = self._address_usage.setdefault(jid, {})
        per_user[address_id] = per_user.get(address_id, 0) + 1
        _save_address_usage(self._address_usage)

    def top_addresses(
        self, jid: str, addresses: list[dict[str, Any]], limit: int
    ) -> list[dict[str, Any]]:
        """Ranks by actual usage frequency (most-used first), falling back
        to Home-tagged-first, then original order, when there's no usage
        history yet to rank by (e.g. before any order has completed)."""
        usage = self._address_usage.get(jid, {})

        def sort_key(a: dict[str, Any]) -> tuple[int, int]:
            is_home = 0 if (a.get("addressTag") or "").strip().lower() == "home" else 1
            return (-usage.get(a.get("id", ""), 0), is_home)

        return sorted(addresses, key=sort_key)[:limit]

    def clear_pending(self, jid: str) -> None:
        user = self.get(jid)
        user.pending_options = None
        user.pending_confirmation = None
        user.pending_address_choice = None
        user.pending_payment_choice = None
        user.pending_dish_retry = None
        user.pending_more_options = None


store = StateStore()
