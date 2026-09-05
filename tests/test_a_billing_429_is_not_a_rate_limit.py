"""Some gateways answer a BILLING PRECONDITION with 429 instead of 402.

VERIFIED LIVE 2026-09-05 on experientiallabs, with a valid key and no card:

    HTTP 429: Requires a card on file to spend platform credits. Add one (no
    charge) at https://platform.experientiallabs.ai/credits?add-card=1

Read as a rate limit that is wrong in both directions. The key gets benched for
a Retry-After that will never help, and the provider is re-tried forever, because
"rate limited" is by definition a condition that clears on its own. This one does
not clear until the account changes.

So it is graded with the 402 "the account is broke" family: sidelined after two
distinct models rather than once (one model's billing refusal should not bench a
provider outright), and re-probed on the normal 30-minute cycle so adding the
card revives it with no restart and no edit.
"""
from unittest import mock

import app as A


class _Resp:
    def __init__(self, status, detail):
        self.status_code = status
        self._detail = detail
        self.headers = {}

    def json(self):
        return {"error": {"message": self._detail}}

    @property
    def text(self):
        return self._detail


REAL = ("Requires a card on file to spend platform credits. Add one (no charge) "
        "at https://platform.experientiallabs.ai/credits?add-card=1 -- everything "
        "else, including your own provider keys (BYOK) and trace uploads, works now.")


def test_the_real_message_is_recognised():
    assert A._is_billing_precondition(_Resp(429, REAL))


def test_other_ways_of_saying_it_are_recognised():
    for detail in ("Please add a payment method to continue.",
                   "A card on file is required.",
                   "Add a card to enable requests.",
                   "Missing billing details for this organization."):
        assert A._is_billing_precondition(_Resp(429, detail)), detail


def test_an_ordinary_rate_limit_is_not_mistaken_for_one():
    """The whole point is telling them apart -- a real 429 must keep its
    key-rotation and Retry-After handling."""
    for detail in ("Rate limit reached for requests",
                   "You exceeded your current quota, please check your plan",
                   "Too many requests. Please retry after 60s",
                   "429 Resource has been exhausted (e.g. check quota)."):
        assert not A._is_billing_precondition(_Resp(429, detail)), detail


def test_an_empty_or_broken_body_is_not_a_billing_error():
    """Fail closed: guessing 'billing' from an unreadable body would sideline a
    provider that is merely rate-limited."""
    class _Broken:
        status_code = 429
        headers = {}

        def json(self):
            raise ValueError("not json")

        @property
        def text(self):
            return ""

    assert not A._is_billing_precondition(_Broken())


def test_it_is_graded_as_an_account_fact_not_a_key_exhaustion(monkeypatch):
    """The two calls that matter: mark_key_exhausted must NOT fire (benching a
    key for a Retry-After that cannot help), and the 402 grader must."""
    src = open("app.py", encoding="utf-8").read()
    i = src.index("if resp.status_code == 429 and _is_billing_precondition(resp):")
    window = src[i:i + 900]
    assert "_mark_provider_authfail(pid, payload.get(\"model\"), 402)" in window
    head = window[:window.index("elif resp.status_code == 429")]
    assert "mark_key_exhausted" not in head


def test_a_plain_429_still_benches_only_the_key():
    """The ordinary path is untouched: one key out is not the provider out."""
    src = open("app.py", encoding="utf-8").read()
    i = src.index("elif resp.status_code == 429:")
    assert "quota.mark_key_exhausted" in src[i:i + 400]


def test_one_billing_refusal_does_not_bench_the_whole_provider():
    """_PROVIDER_NOCREDIT_THRESHOLD is 2 distinct models. A provider that serves
    some ids and bills on others must not be sidelined on the first refusal."""
    assert A._PROVIDER_NOCREDIT_THRESHOLD >= 2


def test_the_sideline_expires_so_adding_a_card_revives_it():
    """No restart, no config edit: the 30-minute re-probe picks it up."""
    assert 0 < A._PROVIDER_DEAD_TTL <= 60 * 60
