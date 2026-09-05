"""A provider that never had a free tier does not say you spent one.

ASKED 2026-09-05: "why i see this in morph and all api keys tests was ok" --
the card read

    key OK ... 4 keys saved ... Out of free quota - resets in 25d 21h

Both halves were true and together they were nonsense. morph's registry entry is
{"limit": 0, "window": "month"}: a RESEARCHED zero meaning "no free tier at
all", and its own card note says so ("No free models -- all 8 models bill per
token"). Used was 0. Nothing was spent, and no reset will change anything.

"Out of free quota, resets in 25d" tells the reader to wait a month for
something that will never arrive, on a provider whose keys are working
perfectly. The distinction the UI was missing: exhausted-because-spent versus
never-free.
"""
import quota


def _template():
    with open("templates/index.html", encoding="utf-8") as f:
        return f.read()


def test_morph_really_is_a_researched_zero():
    """Not an accident or an unknown -- the registry states it."""
    assert quota.FREE_LIMITS["morph"]["limit"] == 0


def test_a_zero_limit_survives_the_key_scaling():
    """No free tier times four keys is still no free tier."""
    quota.set_key_counter(lambda pid: 4)
    try:
        assert quota._limit_for("morph")["limit"] == 0
    finally:
        quota.set_key_counter(None)


def test_it_is_still_reported_as_exhausted_to_the_router():
    """Keeping it out of FREE routing is the whole point -- only the wording
    changes, never the behaviour."""
    assert quota.status("morph")["exhausted"] is True


def test_the_card_distinguishes_never_free_from_spent():
    html = _template()
    assert "var neverFree" in html
    assert "q.limit === 0" in html


def test_it_says_no_free_tier_instead_of_out_of_quota():
    html = _template()
    i = html.index("var neverFree")
    assert "No free tier" in html[i:i + 700]


def test_it_shows_no_countdown_for_something_that_never_resets():
    """The countdown was the actively misleading part."""
    html = _template()
    i = html.index("if (neverFree){")
    block = html[i:html.index("} else if (isExhausted){", i)]
    assert "resets in" not in block


def test_it_says_the_keys_are_fine():
    """The reader's actual question was whether their keys were broken."""
    html = _template()
    i = html.index("if (neverFree){")
    assert "keys work" in html[i:i + 400]


def test_a_genuinely_spent_allowance_still_says_so():
    """Providers that DO have a free tier and used it must be unaffected."""
    html = _template()
    assert "Out of ' + (allowanceSpent ? 'allowance' : 'free quota')" in html
