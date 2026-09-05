"""A provider nobody has measured must not be treated as a small one.

REPORTED 2026-09-05: every opencode turn coming back 503. The hub's log:

    CHAT-503 est=162710 errors=[sub-claude: HTTP 413; groq: RequestException;
    opencode-zen: HTTP 400; g4f: HTTP 404]

Four hops, out of a fleet of 348 models, and when those four failed for four
unrelated reasons the turn was over.

MEASURED at that exact size, before the fix:

    size-capable providers: 2 of 16   (nvidia and google)

_provider_capable answers "can this provider take an est-token request" using
_PROVIDER_TPM -- a hand-maintained table that most providers are not in. The
ones missing from it were measured against _DEFAULT_TPM, a GUESS of 100,000,
which this filter then treated as a hard fact. Eleven of the fourteen exclusions
were that guess: tokenrouter, dahl, g4f, opencode-zen, zenmux, llm7, kilocode
and friends, several of them serving models with million-token windows.

That is failing CLOSED on no evidence, and it is the opposite of the rule the
same file states two hundred lines earlier -- _context_ok returns True when a
model's limit is unknown, "never blocks on a guess", precisely so an absent
measurement cannot delete a working model.

A provider with a REAL entry is still filtered: groq's 8000 is measured and a
30K request genuinely 413s on it. An unmeasured one is admitted, and the truth
arrives the honest way: _upstream_chat compacts to the model's own window before
sending, a real 413 teaches _MODEL_MAX_INPUT the actual ceiling, and _context_ok
enforces it from then on.
"""
import pytest

import app as A


BIG = 162710          # the est from the reported 503


@pytest.fixture(autouse=True)
def known(monkeypatch):
    """A table with one small provider, one big one, and nothing else."""
    monkeypatch.setattr(A, "_PROVIDER_TPM", {"tiny": 8000, "huge": 900000})
    yield


def test_a_measured_small_provider_is_still_excluded():
    """groq's 8000 is a fact, not a guess, and a big request really does 413."""
    assert not A._provider_capable("tiny", BIG)


def test_a_measured_large_provider_is_admitted():
    assert A._provider_capable("huge", BIG)


def test_an_unmeasured_provider_is_admitted():
    """The change. It used to be measured against a 100,000 default and dropped,
    which is what left a 348-model fleet with four hops."""
    assert A._provider_capable("unmeasured", BIG)


def test_an_unmeasured_provider_is_admitted_at_any_size():
    """There is no evidence at ANY size, so there is no size at which the guess
    becomes true. Compaction and a real 413 settle it instead."""
    for est in (200000, 500000, 2000000):
        assert A._provider_capable("unmeasured", est), est


def test_a_small_request_is_unaffected_everywhere():
    for pid in ("tiny", "huge", "unmeasured"):
        assert A._provider_capable(pid, 500), pid


def test_no_estimate_means_no_filtering():
    assert A._provider_capable("tiny", 0)


def test_a_local_subscription_is_never_filtered(monkeypatch):
    """It is the user's own paid session, sized by the real context window --
    _SUB_MAX_PROMPT_CHARS is the only guard that applies to it."""
    monkeypatch.setattr(A, "_is_sub", lambda pid: pid == "sub-claude")
    assert A._provider_capable("sub-claude", BIG)


def test_the_measured_boundary_still_holds():
    """Admitting the unmeasured must not have loosened the measured: a provider
    just under the request size is still excluded, just over is not."""
    monkeypatch_val = int(BIG * 1.15) + 512
    A._PROVIDER_TPM["edge_lo"] = monkeypatch_val - 1
    A._PROVIDER_TPM["edge_hi"] = monkeypatch_val
    try:
        assert not A._provider_capable("edge_lo", BIG)
        assert A._provider_capable("edge_hi", BIG)
    finally:
        A._PROVIDER_TPM.pop("edge_lo", None)
        A._PROVIDER_TPM.pop("edge_hi", None)


def test_the_guess_is_no_longer_consulted_for_the_verdict():
    """_DEFAULT_TPM still exists -- _model_ctx_budget uses it to decide how far
    to COMPACT, which is a fine use of a guess. What it may no longer do is
    decide whether a provider is tried at all."""
    src = open("app.py", encoding="utf-8").read()
    i = src.index("def _provider_capable(")
    body = src[i:i + 2200]
    assert "if pid not in _PROVIDER_TPM:" in body
    assert body.index("if pid not in _PROVIDER_TPM:") < body.index("_provider_tpm(pid) >=")
