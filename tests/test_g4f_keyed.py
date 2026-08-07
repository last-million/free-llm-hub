"""g4f.space ended anonymous access on 2026-08-06.

VERIFIED LIVE that day, not inferred:

    POST https://g4f.space/api/gemini/v1/chat/completions   (no Authorization)
    -> HTTP 402
    {"error":{"message":"No cake credits. Bake proof-of-work cakes at
      g4f.dev/chat to earn anonymous usage, or sign up at
      g4f.dev/members.html.","type":"insufficient_credits", ...}}

    GET https://g4f.space/api/gemini/models  -> HTTP 200   (catalog still public)

So ONLY the chat path broke. The hub cannot mine browser proof-of-work
"cakes", which leaves the free account key from g4f.dev/members.html as the
only usable path -- these are ordinary keyed providers now.

Why this matters beyond a flag flip: while they carried no_key=True the hub
treated them as permanently connected, so _available_providers kept handing
them real chain slots that could only ever 402. Requiring a key excludes them
cleanly until one is pasted.

ONE CARD, same day. g4f-groq / g4f-gemini / g4f-nvidia were three paths on one
service that always shared ONE g4f.dev account, so three cards meant pasting
the same key three times (and quota.py carried three separate 5/min budgets
for a single 5/min allowance, overstating it ~3x). Also verified live that
day: https://g4f.space/v1 is a real unified endpoint -- GET /v1/models returns
200 with 550 models spanning every upstream at once, a superset of what the
three separate cards covered -- and it is the base URL g4f's own member
dashboard prints next to the key. So the merge is correct, not cosmetic.
"""
import providers as prov
import quota

import app

G4F = ("g4f",)
RETIRED = ("g4f-groq", "g4f-gemini", "g4f-nvidia")


def test_g4f_providers_now_require_a_key():
    for pid in G4F:
        p = prov.get_provider(pid)
        assert p is not None, pid
        assert not p.get("no_key"), (
            "%s must not be no_key: a keyless call returns 402 'No cake credits', "
            "so routing would burn a chain hop on a guaranteed failure" % pid)
        assert app._needs_key(pid) is True, pid


def test_g4f_signup_points_at_the_page_that_actually_issues_a_key():
    """g4f.space (the old value) is the relay itself and has no signup -- the
    error body names g4f.dev/members.html explicitly."""
    for pid in G4F:
        p = prov.get_provider(pid)
        assert "g4f.dev/members.html" in p.get("signup_url", ""), pid
        assert "no key needed" not in (p.get("key_hint") or "").lower(), pid


def test_g4f_notes_record_the_change_and_its_date():
    for pid in G4F:
        notes = prov.get_provider(pid).get("notes") or ""
        assert "2026-08-06" in notes, pid
        assert "402" in notes, pid


def test_g4f_is_badged_new_so_the_change_is_visible():
    """It was silently 'connected' before; without a badge the card looks
    unchanged until a real request fails."""
    for pid in G4F:
        assert pid in app._NEW_PROVIDER_IDS, pid


def test_the_three_old_cards_are_fully_retired():
    """One service, one account, one key -> one card. Left half-merged, the
    stale ids would keep their own rows and quota budgets."""
    for pid in RETIRED:
        assert prov.get_provider(pid) is None, pid + " still registered"
        assert pid not in quota.FREE_LIMITS, pid + " still has its own quota row"
        assert pid not in app._NEW_PROVIDER_IDS, pid


def test_pinned_fallback_ids_are_the_ones_a_real_sweep_confirmed():
    """The pins went through two corrections, both driven by live probes:

    1. /v1/models serves TWO catalogs (2026-08-06). Anonymous -> 549 volatile
       "srv_<server>:<model>" ids including "auto"; with a bearer -> 273 clean
       ids where "auto" DOES NOT EXIST. The hub only discovers live once a key
       exists, so an "auto" pin was an id the real catalog rejects.
    2. A sweep of the top 24 by score with a real key (2026-08-07) then showed
       "default" -- the keyed router alias, pinned after fix 1 -- 429s on every
       attempt, and that only 7 of those 24 were FREE AND WORKING: the Gemini
       3.x family. The 134-138 scorers (claude-opus-4-8, claude-haiku-4.5,
       gpt-5.5, kimi-k3) return 402 PAYMENT_REQUIRED against g4f's "pollen"
       balance, and the whole gpt-5.6-* family 500/502s.

    So the pins must be sweep-verified ids, not catalog-plausible ones."""
    models = prov.get_provider("g4f")["default_free_models"]
    assert "auto" not in models, "'auto' is anonymous-catalog only"
    assert "default" not in models, "'default' 429'd on every sweep attempt"
    assert not any(m.startswith("srv_") for m in models), \
        "srv_-prefixed ids are volatile community servers, unfit as a pin"
    assert models, "a keyed provider still needs a discovery-failed fallback"
    # Every pin came back FREE AND WORKING in the sweep.
    assert set(models) <= {"gemini-3.5-flash", "gemini-3.6-flash", "gemini-3.1-pro-low"}
    # None of the confirmed-paid ids may be pinned as free.
    for paid in ("gpt-5.5", "kimi-k3", "claude-opus-4-8", "claude-haiku-4.5"):
        assert paid not in models, paid + " answers 402 PAYMENT_REQUIRED"


def test_the_merged_card_keeps_one_per_minute_budget_not_three():
    """Three rows of 5/min for a single shared 5/min allowance overstated the
    real budget ~3x, so the hub would keep calling a spent relay."""
    row = quota.FREE_LIMITS.get("g4f")
    assert row is not None, "g4f missing from FREE_LIMITS"
    assert row["limit"] == 5 and row["window"] == "minute"


def test_g4f_shows_up_in_guided_setup_once_unkeyed(monkeypatch):
    """The practical payoff: an unkeyed g4f now appears in the guided list with
    a real signup link, instead of masquerading as ready-to-use."""
    monkeypatch.setattr(app.config, "get_provider_config", lambda pid: {})
    token = app.config.get_control_token()
    resp = app.app.test_client().get(
        "/api/onboarding",
        headers={"X-Free-LLM-Hub-Token": token} if token else {})
    steps = resp.get_json()["steps"]
    ids = {s["id"] for s in steps}
    for pid in G4F:
        assert pid in ids, pid
    step = next(s for s in steps if s["id"] == "g4f")
    assert "g4f.dev/members.html" in step["signup_url"]
    assert step["new"] is True
    # ...and exactly once, not three times.
    assert len([s for s in steps if s["id"].startswith("g4f")]) == 1
