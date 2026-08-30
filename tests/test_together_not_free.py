"""Together AI stopped being free — keep it out of free routing.

Confirmed 2026-08-30 on a real account: Together holds a new account in
READ-ONLY mode until it takes a deposit ("You're currently in read-only mode.
Make an initial deposit to unlock full access."). A key generated in that state
is issued normally but 401s on EVERY authenticated endpoint with "Invalid API
key provided" — indistinguishable from a mistyped key, which is exactly what
made it expensive to diagnose.

The row stays in the registry so a funded account can still pin
'together/<model>' explicitly; it just must never be offered as free.
"""
import app
import providers as prov


def test_together_is_marked_paid():
    assert prov.PROVIDERS["together"].get("paid") is True


def test_together_offers_no_free_models():
    """A populated default_free_models is returned as 'free models' REGARDLESS
    of free_filter (the puter precedent), which would put a deposit-gated
    provider back into free auto-rotation and waste a hop on every pick."""
    assert prov.PROVIDERS["together"]["default_free_models"] == []
    assert app.provider_free_models("together", live=False) == []


def test_is_free_model_rejects_every_together_id():
    """paid:True short-circuits is_free_model, so not even a '-Free' slug — the
    thing that used to qualify — can be smuggled back into a free slot."""
    for mid in ("meta-llama/Llama-3.3-70B-Instruct-Turbo-Free",
                "meta-llama/Llama-Vision-Free",
                "deepseek-ai/DeepSeek-R1-Distill-Llama-70B-Free",
                "Qwen/Qwen3-235B-A22B"):
        assert not prov.is_free_model("together", mid), mid


def test_together_is_still_registered_for_explicit_pinning():
    """Not deleted: a funded account must still be able to use it on purpose,
    and the read-only-mode finding must not be lost with the row."""
    p = prov.get_provider("together")
    assert p is not None
    assert prov.is_known_provider("together")
    assert p["base_url"].startswith("https://")
    assert "read-only" in p["notes"].lower()
