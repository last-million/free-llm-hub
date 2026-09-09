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
    assert "publish no daily limit" in body


def test_exhausted_providers_are_called_out():
    i = SRC.index("function renderQuotaToday(")
    body = SRC[i:i + 5000]
    assert "exhausted" in body and "out of quota" in body


def test_the_reset_countdown_is_shown():
    i = SRC.index("function renderQuotaToday(")
    assert "resets_in" in SRC[i:i + 5000]


def test_every_provider_is_listed():
    """The first cut showed a top-8 and the report was immediate: "i connected
    many providers and i dont see all of them there, i see only 7". A quota
    panel that hides providers is one you cannot trust."""
    i = SRC.index("function renderQuotaToday(")
    body = SRC[i:i + 6000]
    assert ".slice(0," not in body, "still truncating the provider list"
    assert "sort(" in body, "what is nearly gone should still come first"


def test_the_list_scrolls_instead_of_truncating():
    rule = _rule(".qt-provs")
    assert "overflow-y:auto" in rule and "max-height" in rule


def test_pooled_keys_are_shown():
    """quota.status already multiplies the daily limit by the key count, so a
    provider with four keys reports four times the allowance -- and nothing
    said so, which made a big number look like a bug instead of the reason to
    add a second account."""
    i = SRC.index("function renderQuotaToday(")
    body = SRC[i:i + 6000]
    assert "q.keys" in body
    assert "keys pooled" in body


def test_open_gateways_are_labelled_not_hidden():
    i = SRC.index("function renderQuotaToday(")
    body = SRC[i:i + 6000]
    assert "keyless" in body and "no API key needed" in body


def test_the_daily_total_names_both_halves():
    """"how much used" and "how much remaining" were the ask, and a total with
    no denominator answers neither."""
    i = SRC.index("function renderQuotaToday(")
    body = SRC[i:i + 6000]
    assert "requests used" in body
    assert "of ' + _qtNum(limit)" in body
    assert "providers, " in body


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
    assert "title=" in body and "publishes no daily limit" in body


def test_the_key_count_reaches_the_dashboard():
    """The strip cannot show what /api/status does not send."""
    src = io.open("app.py", encoding="utf-8").read()
    # Scoped to the loop body rather than a byte count: a window measured in
    # characters fails the moment someone writes a longer comment inside it,
    # which says nothing about whether the field is still sent.
    i = src.index('s["models"] = quota.models(pid)')
    block = src[i:src.index("q[pid] = s", i)]
    assert 's["keys"] = quota.key_count(pid)' in block
    assert 's["keyless"] = bool(p.get("no_key"))' in block


def test_a_missing_key_counter_does_not_break_status():
    src = io.open("app.py", encoding="utf-8").read()
    i = src.index('s["keys"] = quota.key_count(pid)')
    assert "except Exception" in src[i:i + 300]


def test_open_gateways_are_detected_by_the_flag_that_exists():
    """`no_key` is what providers.py actually sets. An earlier pass guessed
    `needs_key`, which does not exist -- so it read False for every provider
    and quietly labelled the keyless ones as keyed."""
    import providers
    src = io.open("app.py", encoding="utf-8").read()
    assert 's["keyless"] = bool(p.get("no_key"))' in src
    keyless = [pid for pid, v in providers.PROVIDERS.items()
               if isinstance(v, dict) and v.get("no_key")]
    assert keyless, "providers.py no longer marks any open gateway"
    for pid in ("pollinations", "llm7", "kilocode"):
        assert pid in keyless, pid
