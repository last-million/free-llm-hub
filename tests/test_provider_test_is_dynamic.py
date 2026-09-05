"""Testing a key asks the LIVE catalog, and a model's 401 is not the key's fault.

REPORTED 2026-09-05: "can you check if provider OpenCode Zen work or no?" The
dashboard said

    None of the 2 keys work. Key authenticates and lists models (8 free models
    listed), but generation FAILS on every candidate tried -- HTTP 401: Model
    north-mini-code-free is not supported

Probed directly, five of that provider's eight live free models answered 200.
The keys were fine. The test was wrong, and the user was about to throw away a
working provider on its say-so.

Two causes, and the fix for both is the same instruction the user gave next:
"my system should be dynamic to get all available models in providers that are
working and free and available".

1. ORDER. Candidates were the registry's hardcoded `default_free_models` FIRST
   and the live catalog second. opencode-zen's three pins have gone stale:
   deepseek-v4-flash-free now answers 400 "Model is unavailable" and
   north-mini-code-free was withdrawn. The live list is what the hub actually
   routes to, so it is what the test should ask about; the pins are the fallback
   they were meant to be, for a provider with no /models to discover.

2. THE 401. The loop treated any 401 as a dead credential and stopped -- correct
   in general, and wrong here, because opencode-zen answers 401 for a model it
   no longer serves. providers.py has carried a note about that exact wording
   since 2026-07-27; the key test had never been told. So the probe stopped at
   candidate two and never reached mimo-v2.5-free, which works.
"""
import app as A


def _fn_src(name):
    src = open("app.py", encoding="utf-8").read()
    i = src.index("def " + name + "(")
    return src[i:i + 1200]


# --------------------------------------------------------------------------- #
# Telling a dead key from a withdrawn model
# --------------------------------------------------------------------------- #

def _scoped(detail):
    """The helper lives inside api_test_provider's closure, so exercise the
    rule it encodes against the strings providers actually send."""
    text = (detail or "").lower()
    if "model" not in text:
        return False
    return any(w in text for w in ("not supported", "not found", "unavailable",
                                   "does not exist", "no longer", "unknown model",
                                   "invalid model", "decommissioned"))


def test_the_exact_message_that_caused_this():
    assert _scoped("HTTP 401: Model north-mini-code-free is not supported")


def test_other_withdrawn_model_wordings():
    for msg in ("Model foo is unavailable", "model not found",
                "Unknown model: bar", "This model no longer exists",
                "invalid model id"):
        assert _scoped(msg), msg


def test_a_real_credential_failure_is_not_model_scoped():
    """These must still stop the loop -- no sibling model can rescue them."""
    for msg in ("Invalid API key", "Unauthorized", "authentication failed",
                "Your account is suspended", "invalid_api_key"):
        assert not _scoped(msg), msg


def test_the_word_model_alone_is_not_enough():
    """"the model refused to answer" is not a withdrawn-model signal."""
    assert not _scoped("the model returned an empty response")


def test_the_helper_is_wired_into_the_401_branch():
    src = open("app.py", encoding="utf-8").read()
    assert "and not _looks_like_model_scoped(reason)" in src


# --------------------------------------------------------------------------- #
# The live catalog decides
# --------------------------------------------------------------------------- #

def test_live_models_are_probed_before_the_hardcoded_pins():
    """The pins go stale; the live list is what routing actually uses."""
    src = open("app.py", encoding="utf-8").read()
    assert 'for m in sample_models + (p.get("default_free_models") or []):' in src


def test_the_pins_are_still_there_as_a_fallback():
    """A provider with no /models endpoint has nothing else to probe with."""
    src = open("app.py", encoding="utf-8").read()
    assert 'p.get("default_free_models")' in src


def test_opencode_zen_still_declares_pins_for_that_fallback():
    import providers as prov
    assert prov.get_provider("opencode-zen").get("default_free_models")
