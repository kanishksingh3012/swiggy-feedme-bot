import time
from dataclasses import dataclass, field
from typing import Any

# TTL for a set of presented options before they're considered stale and
# must be re-searched rather than checked out against (see plan notes on
# price/availability drift between search and confirm).
PENDING_OPTIONS_TTL_SECONDS = 15 * 60


@dataclass
class PendingOptions:
    safe_pick: dict[str, Any]
    mood_pick: dict[str, Any]
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
class UserState:
    pending_options: PendingOptions | None = None
    pending_confirmation: PendingConfirmation | None = None
    order_history: list[dict[str, Any]] = field(default_factory=list)


class StateStore:
    """In-memory, JID-keyed session state.

    Personal-tool phase 1 has exactly one real key in here, but keying by
    JID from day one avoids a rewrite when a second user shows up later.
    Not persisted — a restart clears in-flight sessions, which is fine
    given everything mid-flight has a TTL anyway.
    """

    def __init__(self) -> None:
        self._users: dict[str, UserState] = {}

    def get(self, jid: str) -> UserState:
        return self._users.setdefault(jid, UserState())

    def clear_pending(self, jid: str) -> None:
        user = self.get(jid)
        user.pending_options = None
        user.pending_confirmation = None


store = StateStore()
