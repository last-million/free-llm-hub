r"""Today's free quota, shown on the pages where the question is actually asked.

REQUESTED 2026-09-09: "in page /usage i want to show how many remaining quota
today and how much used, and also in activity page".

It had a page of its own, and that page is fine. But the two moments you want
the number are not on it: "answers stopped" is noticed on Activity, and "what
have I spent" is the question Usage is open for. One click away is one click
too many for a number that changes the reading of everything else on the page.

Same numbers, same source. /api/status already carries a per-provider quota row
-- used, limit, remaining, resets_in -- and the dashboard already polls it, so
the strip rides that poll rather than adding a request of its own.

HONEST ABOUT WHAT IS NOT KNOWN. Most free tiers publish no limit at all:
`limit_known` is false and `remaining` is null for them. Summing those as zero
would invent a number, so they are counted separately and said out loud.
"""
import io
import re

SRC = io.open("templates/index.html", encoding="utf-8").read()


def _rule(sel):
    m = re.search(re.escape(sel) + r"\s*\{([^}]*)\}", SRC)
    assert m, "no rule for %s" % sel
    return m.group(1).replace(" ", "")


# --------------------------------------------------------------------------- #
# It is on both pages
# --------------------------------------------------------------------------- #

def test_the_strip_is_on_activity_and_usage():
    assert 'id="qt-activity"' in SRC and 'id="qt-usage"' in SRC


def test_it_sits_at_the_top_of_each_section():
    """Below the fold it may as well be on the other page."""
    for sec, host in (("sec-activity", "qt-activity"), ("sec-usage", "qt-usage")):
        i = SRC.index('id="%s"' % sec)
        assert host in SRC[i:i + 300], "%s is not at the top of %s" % (host, sec)


def test_both_are_filled_by_one_renderer():
    """Two copies of this arithmetic would eventually disagree, and the reader
    would have no way to tell which page was lying."""
    assert SRC.count("function renderQuotaToday(") == 1
    i = SRC.index("function renderQuotaToday(")
    body = SRC[i:i + 900]
    assert "qt-activity" in body and "qt-usage" in body


def test_it_rides_the_poll_that_already_happens():
    i = SRC.index("function ingestStatus(")
    assert "renderQuotaToday(" in SRC[i:i + 500]


# --------------------------------------------------------------------------- #
# What it says
# --------------------------------------------------------------------------- #

def test_it_shows_used_and_remaining():
    i = SRC.index("function renderQuotaToday(")
    body = SRC[i:i + 5000]
    assert "used" in body and "left" in body


def test_a_provider_with_no_published_limit_is_not_counted_as_zero():
    """Summing an unknown remaining as 0 would invent a number."""
    i = SRC.index("function renderQuotaToday(")
    body = SRC[i:i + 5000]
    assert "limit_known" in body
    assert "unmetered" in body
    assert "publish no limit" in body


def test_exhausted_providers_are_called_out():
    i = SRC.index("function renderQuotaToday(")
    body = SRC[i:i + 5000]
    assert "exhausted" in body and "out of quota" in body


def test_the_reset_countdown_is_shown():
    i = SRC.index("function renderQuotaToday(")
    assert "resets_in" in SRC[i:i + 5000]


def test_the_list_is_capped_and_ordered_by_what_matters():
    """Forty provider chips help nobody; what is nearly gone comes first."""
    i = SRC.index("function renderQuotaToday(")
    body = SRC[i:i + 5000]
    assert ".slice(0, 8)" in body
    assert "sort(" in body


def test_nothing_is_shown_when_there_is_nothing_to_show():
    i = SRC.index("function renderQuotaToday(")
    body = SRC[i:i + 5000]
    assert "h.hidden = true" in body


def test_a_broken_status_payload_cannot_break_the_page():
    i = SRC.index("function ingestStatus(")
    assert "try { renderQuotaToday" in SRC[i:i + 500]


# --------------------------------------------------------------------------- #
# Presentation
# --------------------------------------------------------------------------- #

def test_the_numbers_are_tabular():
    """Digits that change width make a number that updates look like it is
    twitching."""
    assert "font-variant-numeric:tabular-nums" in _rule(".qt-big")


def test_state_is_not_carried_by_colour_alone():
    """A red chip also says "out"."""
    i = SRC.index("function renderQuotaToday(")
    body = SRC[i:i + 5000]
    assert "'out'" in body or '"out"' in body


def test_the_bars_are_hidden_from_screen_readers():
    i = SRC.index("function renderQuotaToday(")
    body = SRC[i:i + 5000]
    assert 'class="qt-bar" aria-hidden="true"' in body


def test_each_chip_carries_the_full_numbers_in_its_title():
    i = SRC.index("function renderQuotaToday(")
    body = SRC[i:i + 5000]
    assert "title=" in body and "no published limit" in body
