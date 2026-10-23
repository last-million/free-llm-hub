"""Together AI stopped being free -- and, at the owner's request, left the registry.

Confirmed 2026-08-30 on a real account: Together holds a new account in
READ-ONLY mode until it takes a deposit ("You're currently in read-only mode.
Make an initial deposit to unlock full access."). A key generated in that state
is issued normally but 401s on EVERY authenticated endpoint with "Invalid API
key provided" -- indistinguishable from a mistyped key, which is exactly what
made it expensive to diagnose. Re-checked 2026-10-24 against Together's own
billing pages: no free trial, $5 minimum purchase.

2026-10-24, owner: "check if Together AI doesn't give free API anymore; if not,
remove it". It used to stay registered (paid, no free models) so a funded account
could pin it; it is now gone, and a saved key moves to `retired_providers`
(migrations v4, tests/test_safe_migrations.py) instead of being deleted.
"""
import app
import providers as prov


def test_together_is_removed_from_the_registry():
    assert "together" not in prov.PROVIDERS
    assert prov.get_provider("together") is None
    assert not prov.is_known_provider("together")
    assert "together" not in app._NEW_PROVIDER_IDS


def test_together_offers_no_free_models():
    """Whatever is left in a saved config, nothing may offer Together as free."""
    assert app.provider_free_models("together", live=False) == []


def test_is_free_model_rejects_every_together_id():
    """An unknown provider is never free, so not even a '-Free' slug -- the thing
    that used to qualify -- can be smuggled back into a free slot."""
    for mid in ("meta-llama/Llama-3.3-70B-Instruct-Turbo-Free",
                "meta-llama/Llama-Vision-Free",
                "deepseek-ai/DeepSeek-R1-Distill-Llama-70B-Free",
                "Qwen/Qwen3-235B-A22B"):
        assert not prov.is_free_model("together", mid), mid


def test_the_finding_is_kept_as_a_comment_not_lost():
    """So the next person who wonders why Together is missing finds the facts."""
    src = open("providers.py", encoding="utf-8").read()
    assert "REMOVED 2026-10-24" in src
    assert "read-only" in src.lower()
    assert "$5 minimum" in src
