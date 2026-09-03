"""Usage counted per KEY, not only per provider.

Gap B from the freellmapi comparison: their counters are per (platform, model,
key); ours were per provider, with a secondary per-model layer for display.

That mattered here more than it looks. This install has 64 keys across 35
providers, and the pool exists precisely so a provider survives one key running
out. Rotation made that work -- and made "which of my keys is dead?"
unanswerable, because every counter aggregated the pool away. The provider total
cannot tell a key doing all the work from one being rotated onto and rejected on
every attempt, which is exactly the state a pool is designed to make survivable
and therefore invisible.

The key itself is never stored in the counters. A truncated SHA-256 is enough to
say "this one", and unlike config.json the quota state file is not encrypted, so
a credential must not end up in it.
"""
from unittest import mock

import pytest

import quota


@pytest.fixture(autouse=True)
def clean():
    with quota._LOCK:
        quota._KEY_STATE.clear()
        quota._STATE.clear()
    yield
    with quota._LOCK:
        quota._KEY_STATE.clear()
        quota._STATE.clear()


# --------------------------------------------------------------------------- #
# The fingerprint
# --------------------------------------------------------------------------- #

def test_it_is_stable():
    assert quota.key_fingerprint("sk-abc") == quota.key_fingerprint("sk-abc")


def test_different_keys_differ():
    assert quota.key_fingerprint("sk-abc") != quota.key_fingerprint("sk-abd")


def test_it_does_not_contain_the_key():
    """The quota state file is not encrypted, unlike config.json."""
    fp = quota.key_fingerprint("sk-super-secret-value")
    assert "sk-super" not in fp and "secret" not in fp


def test_a_missing_key_has_no_fingerprint():
    """no_key and static_key providers have nothing to attribute."""
    for k in (None, "", 123):
        assert quota.key_fingerprint(k) == ""


# --------------------------------------------------------------------------- #
# Counting
# --------------------------------------------------------------------------- #

def test_requests_are_attributed_to_the_key_that_served():
    quota.record_key("groq", "key-a", "m")
    quota.record_key("groq", "key-a", "m")
    quota.record_key("groq", "key-b", "m")
    rows = quota.keys("groq")
    assert rows[quota.key_fingerprint("key-a")]["count"] == 2
    assert rows[quota.key_fingerprint("key-b")]["count"] == 1


def test_a_keyless_provider_records_nothing():
    quota.record_key("pollinations", None, "m")
    assert quota.keys("pollinations") == {}


def test_outcomes_separate_a_working_key_from_a_rejected_one():
    """The distinction a pooled counter erases, and the reason to have this."""
    quota.record_key("groq", "good", "m")
    quota.note_key_outcome("groq", "good", True)
    quota.record_key("groq", "dead", "m")
    quota.note_key_outcome("groq", "dead", False)
    rows = quota.keys("groq")
    assert rows[quota.key_fingerprint("good")]["ok"] == 1
    assert rows[quota.key_fingerprint("good")]["fail"] == 0
    assert rows[quota.key_fingerprint("dead")]["fail"] == 1


def test_an_outcome_for_an_uncounted_key_is_ignored():
    quota.note_key_outcome("groq", "never-used", False)
    assert quota.keys("groq") == {}


def test_the_last_use_is_recorded():
    quota.record_key("groq", "key-a", "m")
    assert quota.keys("groq")[quota.key_fingerprint("key-a")]["last"] > 0


def test_an_unknown_provider_has_no_rows():
    assert quota.keys("nope") == {}


def test_per_key_counts_add_up_to_the_provider_total():
    """They share the provider's window, so the two views cannot drift apart."""
    for k in ("a", "b", "c"):
        quota.record("groq", "m")
        quota.record_key("groq", k, "m")
    total = sum(r["count"] for r in quota.keys("groq").values())
    assert total == 3


# --------------------------------------------------------------------------- #
# Through the hub
# --------------------------------------------------------------------------- #

def test_the_provider_row_reports_usage_per_key():
    import app as A
    quota.record_key("groq", "sk-aaaaaaaaaaaa", "m")
    quota.note_key_outcome("groq", "sk-aaaaaaaaaaaa", True)
    rows = A._key_rows("groq", ["sk-aaaaaaaaaaaa", "sk-bbbbbbbbbbbb"])
    assert rows[0]["requests"] == 1 and rows[0]["ok"] == 1
    assert rows[1]["requests"] == 0
    assert rows[1]["last_used"] is None


def test_the_row_still_only_shows_a_masked_key():
    import app as A
    rows = A._key_rows("groq", ["sk-abcdefghijkl"])
    assert rows[0]["masked"] == "sk-a…ijkl"
    assert "sk-abcdefghijkl" not in str(rows[0])


def test_the_key_that_served_is_the_one_counted():
    """Rotation picks a key per attempt; counting the pool's first entry rather
    than the one actually used would attribute every request to one key."""
    src = open("app.py", encoding="utf-8").read()
    body = src.split("def _upstream_chat(", 1)[1]
    body = body[:body.index("def _upstream_post(")] if "def _upstream_post(" in body else body
    assert "quota.record_key(pid, key, payload.get(\"model\"))" in body


def test_the_non_chat_surfaces_count_too():
    """/v1/embeddings goes through _upstream_post, and its requests spend the
    same quota."""
    src = open("app.py", encoding="utf-8").read()
    body = src.split("def _upstream_post(", 1)[1][:2500]
    assert "quota.record_key(" in body


def test_only_key_level_rejections_count_as_a_key_failure():
    """A 500 is the provider being broken, not this key being dead; counting it
    against the key would condemn a perfectly good one."""
    src = open("app.py", encoding="utf-8").read()
    assert src.count("quota.note_key_outcome(pid, key, resp.status_code not in (401, 403, 429))") == 2


# --------------------------------------------------------------------------- #
# ...and it is visible
# --------------------------------------------------------------------------- #

def _template():
    with open("templates/index.html", encoding="utf-8") as f:
        return f.read()


def test_each_saved_key_shows_its_usage():
    html = _template()
    assert "function keyUsageTag(" in html
    assert "keyUsageTag(k)" in html


def test_an_unused_key_says_so_rather_than_showing_zero():
    html = _template()
    i = html.index("function keyUsageTag(")
    assert "unused" in html[i:i + 900]


def test_a_key_rejected_on_every_attempt_is_marked():
    """The state a pooled counter hides: it looks identical to an unused key."""
    html = _template()
    i = html.index("function keyUsageTag(")
    body = html[i:i + 900]
    assert "bad >= n" in body
    assert ".keyrow-use.bad{" in html
