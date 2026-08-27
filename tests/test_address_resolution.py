"""_resolve_address is a hard user requirement, stated explicitly and
repeatedly: never silently reuse a remembered default, always ask when
there's a real choice to make."""

from feedme_bot import handlers
from feedme_bot.state import UserState


def test_single_saved_address_resolves_without_asking():
    user = UserState()
    result = handlers._resolve_address("jid1", user, [{"id": "a1"}])
    assert result == "a1"


def test_multiple_addresses_always_returns_none_to_force_asking():
    user = UserState()
    addrs = [{"id": "a1"}, {"id": "a2"}, {"id": "a3"}]
    result = handlers._resolve_address("jid1", user, addrs)
    assert result is None


def test_still_asks_even_with_a_remembered_default_address_id():
    # The explicit rule: default_address_id/usage tracking stay for
    # ranking which 3 buttons to show, but must NEVER auto-resolve.
    user = UserState(default_address_id="a1")
    addrs = [{"id": "a1"}, {"id": "a2"}]
    result = handlers._resolve_address("jid1", user, addrs)
    assert result is None
