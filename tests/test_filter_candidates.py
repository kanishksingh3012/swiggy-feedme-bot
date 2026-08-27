"""_filter_candidates is the hard budget cap — the exact thing that was
silently relaxed by 25% until tonight's fix. These guard against that
regression coming back."""

from feedme_bot import handlers


def _item(price, in_stock=True, has_addons=False, has_variants=False):
    return {
        "price": price,
        "inStock": in_stock,
        "hasAddons": has_addons,
        "hasVariants": has_variants,
        "name": f"item-{price}",
    }


def test_budget_is_a_hard_cap_never_relaxed():
    items = [_item(250), _item(365), _item(500)]
    result = handlers._filter_candidates(items, max_price=300)
    assert result == [items[0]]


def test_zero_matches_under_budget_returns_empty_not_a_fallback():
    # Previously this fell back to "whatever's in stock" over budget —
    # must now come back genuinely empty.
    items = [_item(1000), _item(2000)]
    result = handlers._filter_candidates(items, max_price=300)
    assert result == []


def test_no_budget_stated_returns_everything_processable():
    items = [_item(100), _item(9999)]
    result = handlers._filter_candidates(items, max_price=None)
    assert len(result) == 2


def test_excludes_out_of_stock_and_mandatory_addon_items():
    items = [
        _item(100, in_stock=False),
        _item(100, has_addons=True),
        _item(100, has_variants=True),
        _item(100),
    ]
    result = handlers._filter_candidates(items, max_price=None)
    assert result == [items[3]]


def test_boundary_price_is_inclusive():
    items = [_item(300)]
    result = handlers._filter_candidates(items, max_price=300)
    assert result == items
