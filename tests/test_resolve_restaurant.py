"""_resolve_restaurant caught two real bugs live tonight: sponsored "(Ad)"
listings ranking first regardless of name match, and punctuation
("La Pino'z" vs "La Pinoz") breaking a plain substring match. Both are
pinned here."""

from feedme_bot import handlers


async def test_matches_despite_apostrophe_the_user_did_not_type(mocked_swiggy):
    mocked_swiggy["search_restaurants"].return_value = {
        "restaurants": [{"id": "646505", "name": "La Pino'z Pizza (Ad)"}]
    }
    result = await handlers._resolve_restaurant("addr1", "La Pinoz")
    assert result == "646505"


async def test_does_not_trust_ad_ranked_top_result_blindly(mocked_swiggy):
    mocked_swiggy["search_restaurants"].return_value = {
        "restaurants": [
            {"id": "999", "name": "Some Sponsored Place (Ad)"},
            {"id": "123", "name": "Paradise Biryani"},
        ]
    }
    result = await handlers._resolve_restaurant("addr1", "Paradise")
    assert result == "123"


async def test_no_real_match_returns_none_not_a_guess(mocked_swiggy):
    mocked_swiggy["search_restaurants"].return_value = {
        "restaurants": [{"id": "1", "name": "Totally Unrelated Place"}]
    }
    result = await handlers._resolve_restaurant("addr1", "Paradise")
    assert result is None


async def test_empty_results_returns_none(mocked_swiggy):
    mocked_swiggy["search_restaurants"].return_value = {"restaurants": []}
    result = await handlers._resolve_restaurant("addr1", "Anything")
    assert result is None
