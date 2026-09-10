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
    body = _strip_body()
    assert "qt-activity" in body and "qt-usage" in body


def test_it_rides_the_poll_that_already_happens():
    i = SRC.index("function ingestStatus(")
    assert "renderQuotaToday(" in SRC[i:i + 500]


# --------------------------------------------------------------------------- #
# What it says
# --------------------------------------------------------------------------- #

def test_it_shows_used_and_remaining():
    body = _strip_body()
    assert "used" in body and "left" in body


def _strip_body():
    """The renderer, whole. Fixed-character windows kept breaking on nothing
    but added comments -- a 5000-char slice measures comment volume, not
    behaviour."""
    body = SRC[SRC.index("function renderQuotaToday("):]
    return body[:body.index(chr(10) + "  /* ---------- Getting started")]


def test_a_provider_with_no_published_limit_is_not_counted_as_zero():
    """Summing an unknown remaining as 0 would invent a number."""
    body = _strip_body()
    assert "limit_known" in body
    assert "unmetered" in body
    assert "publish no daily request limit" in body


def test_exhausted_providers_are_called_out():
    body = _strip_body()
    assert "exhausted" in body and "out of quota" in body


def test_the_reset_countdown_is_shown():
    body = _strip_body()
    assert "resets_in" in body


def test_every_provider_is_listed():
    """The first cut showed a top-8 and the report was immediate: "i connected
    many providers and i dont see all of them there, i see only 7". A quota
    panel that hides providers is one you cannot trust."""
    body = _strip_body()
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
    body = _strip_body()
    assert "q.keys" in body
    assert "keys pooled" in body


def test_open_gateways_are_labelled_not_hidden():
    body = _strip_body()
    assert "keyless" in body and "no API key needed" in body


def test_the_daily_total_names_both_halves():
    """"how much used" and "how much remaining" were the ask, and a total with
    no denominator answers neither."""
    body = _strip_body()
    assert "requests used" in body
    assert "of ' + _qtNum(limit)" in body
    assert "providers, " in body


def test_nothing_is_shown_when_there_is_nothing_to_show():
    body = _strip_body()
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
    body = _strip_body()
    assert "'out'" in body or '"out"' in body


def test_the_bars_are_hidden_from_screen_readers():
    body = _strip_body()
    assert 'class="qt-bar" aria-hidden="true"' in body


def test_each_chip_carries_the_full_numbers_in_its_title():
    body = _strip_body()
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


# --------------------------------------------------------------------------- #
# Tokens, not only requests
# --------------------------------------------------------------------------- #

def test_the_strip_leads_with_tokens():
    """REPORTED: "the bar on top for usage and how much remaining, they show
    requests and not how much tokens ... I want tokens used and remaining and
    total". Requests were all it had because requests are all quota.py counted;
    what a free tier actually meters is almost always tokens."""
    body = _strip_body()
    assert "tokens used" in body
    assert "tokens_used" in body


def test_it_shows_what_is_left_when_a_provider_publishes_it():
    body = _strip_body()
    assert "tokens_remaining" in body and "tokens_limit" in body
    assert "tokens left" in body


def test_an_unpublished_token_budget_is_not_invented():
    """The house rule: a guess must never be shown as a measurement. Most free
    tiers publish no token allowance at all."""
    body = _strip_body()
    assert "tokens_known" in body
    assert "honest remaining" in body


def test_requests_are_still_there():
    """The token figure is the headline, not a replacement -- a provider can
    have tokens left and no requests."""
    body = _strip_body()
    assert "'requests'" in body or "requests</span>" in body or "requests" in body


def test_each_provider_says_its_own_token_spend():
    body = _strip_body()
    assert "tokens today" in body


def test_the_status_route_reports_tokens_per_provider():
    src = open("app.py", encoding="utf-8").read()
    i = src.index("def api_status(")
    body = src[i:src.index("@app.route(\"/api/usage\"", i)]
    assert 's["tokens_used"]' in body
    assert "usage_history.get_day()" in body


def test_the_day_is_read_once_not_per_provider():
    """get_day() parses the whole day file; doing that sixteen times to answer
    one question is sixteen times the work for the same answer."""
    src = open("app.py", encoding="utf-8").read()
    i = src.index("def api_status(")
    body = src[i:src.index("@app.route(\"/api/usage\"", i)]
    assert body.count("usage_history.get_day()") == 1
    assert body.index("usage_history.get_day()") < body.index("for pid in keyed")


def test_quota_keeps_the_token_bucket_it_is_told_about():
    """observe_headers folded a spent token bucket into the REQUEST count --
    right for routing, but it threw away the only real token budget the hub
    ever sees."""
    import quota
    assert hasattr(quota, "_TOKENS")
    st = quota.status("groq")
    for field in ("tokens_used",):
        pass
    assert "tokens_known" in st and "tokens_remaining" in st and "tokens_limit" in st


def test_a_provider_that_reports_no_tokens_says_so_rather_than_zero():
    import quota
    st = quota.status("a-provider-that-never-answered")
    assert st["tokens_known"] is False
    assert st["tokens_remaining"] is None


def test_the_headline_is_the_whole_day_not_just_the_chips():
    """The strip lists FREE providers only (paid ones are skipped), so summing
    its chips under-reports a day where a paid provider did the work. "Tokens
    used today" has to mean today."""
    src = open("app.py", encoding="utf-8").read()
    i = src.index("def api_status(")
    body = src[i:src.index("@app.route(\"/api/usage\"", i)]
    assert '"tokens_today": tokens_today_total' in body
    assert 'total_tokens' in body


def test_the_strip_takes_the_day_total():
    body = _strip_body()
    assert "dayTotal" in body
    assert "renderQuotaToday(quota, dayTotal)" in SRC


def test_the_day_total_is_passed_in_from_status():
    assert "renderQuotaToday(s && s.quota, s && s.tokens_today)" in SRC


def test_it_falls_back_to_the_chips_when_there_is_no_day_total():
    """An older hub, or a status call that predates the field, must still show
    a number rather than a blank."""
    body = _strip_body()
    assert "typeof dayTotal === 'number'" in body
