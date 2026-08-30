"""Opt-in uncensored mode: OFF by default, and ON means ONLY.

The safety filter (providers._BLOCK_RE) exists so an abliterated/NSFW fine-tune
is never served by accident. This adds an explicit operator switch that INVERTS
it rather than switching it off, so the pool becomes those models alone. A
request then either lands on one or fails visibly, instead of an uncensored
model quietly joining the pool and answering ordinary traffic.

What these lock down: the default is untouched, the mode only ever moves by an
explicit API call, and it survives the 5-hourly auto-update restart.
"""
from unittest import mock

import app
import config
import providers as prov


def _client():
    app.app.config["TESTING"] = True
    return app.app.test_client()


def _hdrs():
    return {"X-Free-LLM-Hub-Token": config.ensure_control_token(),
            "X-Free-LLM-Hub": "dashboard"}


NORMAL = ["meta-llama/Llama-3.3-70B-Instruct", "openai/gpt-oss-120b",
          "deepseek-ai/DeepSeek-V3.1", "qwen/qwen3.8-27b"]
UNCENSORED = ["cognitivecomputations/dolphin-mistral-24b-venice-edition",
              "Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-Q6_K_P.gguf",
              "some-abliterated-model", "an-nsfw-tune"]


def test_default_is_off_and_blocks_uncensored():
    """The shipped default must not change: this is opt-in only."""
    for m in NORMAL:
        assert prov.is_model_allowed(m, uncensored_only_mode=False), m
    for m in UNCENSORED:
        assert not prov.is_model_allowed(m, uncensored_only_mode=False), m


def test_on_means_only_not_also():
    """ON inverts the filter: normal models drop OUT of the pool."""
    for m in UNCENSORED:
        assert prov.is_model_allowed(m, uncensored_only_mode=True), m
    for m in NORMAL:
        assert not prov.is_model_allowed(m, uncensored_only_mode=True), m


def test_module_flag_defaults_off():
    assert prov.uncensored_only() is False


def test_empty_model_id_is_rejected_in_both_modes():
    for mode in (True, False):
        assert not prov.is_model_allowed(None, uncensored_only_mode=mode)
        assert not prov.is_model_allowed("", uncensored_only_mode=mode)


def test_toggle_endpoint_flips_the_live_flag_and_persists():
    try:
        with mock.patch.object(app, "_enabled_keyed", return_value=[]):
            r = _client().post("/api/uncensored-mode", json={"enabled": True},
                               headers=_hdrs())
            assert r.status_code == 200
            assert r.get_json()["enabled"] is True
            assert prov.uncensored_only() is True
            assert config.get_flag(app._UNCENSORED_FLAG, False) is True

            r = _client().post("/api/uncensored-mode", json={"enabled": False},
                               headers=_hdrs())
            assert r.get_json()["enabled"] is False
            assert prov.uncensored_only() is False
    finally:
        prov.set_uncensored_only(False)
        config.set_flag(app._UNCENSORED_FLAG, False)


def test_toggle_rejects_a_non_boolean():
    for bad in ({}, {"enabled": "yes"}, {"enabled": 1}, {"on": True}):
        r = _client().post("/api/uncensored-mode", json=bad, headers=_hdrs())
        assert r.status_code == 400, bad
    assert prov.uncensored_only() is False


def test_toggling_clears_the_discovery_cache():
    """The cache holds ALREADY-FILTERED lists, so a stale entry would make the
    switch look like it did nothing for up to MODEL_CACHE_TTL seconds."""
    try:
        with app._model_cache_lock:
            app._model_cache["groq"] = (9e9, ["stale-entry"])
        with mock.patch.object(app, "_enabled_keyed", return_value=[]):
            _client().post("/api/uncensored-mode", json={"enabled": True},
                           headers=_hdrs())
        with app._model_cache_lock:
            assert "groq" not in app._model_cache
    finally:
        prov.set_uncensored_only(False)
        config.set_flag(app._UNCENSORED_FLAG, False)


def test_a_cli_subscription_hop_is_never_blocked_by_the_mode():
    """REGRESSION. Leaving the mode on made the agent fail with
    "Model 'claude' is blocked by the safety filter" -- a 403 accusing the
    user's own logged-in Claude Code subscription of being unsafe.

    A sub-* hop is not an entry in the free catalog this toggle is about, and
    there is no uncensored counterpart to route it to, so the inverted filter
    restricts nothing there and only breaks the agent."""
    try:
        prov.set_uncensored_only(True)
        assert app._model_block_reason("sub-claude", "claude") is None
        assert app._model_block_reason("sub-codex", "gpt-5.2") is None
    finally:
        prov.set_uncensored_only(False)


def test_the_two_gates_no_longer_share_one_misleading_message():
    """Uncensored-only mode must not be reported as a safety block: the user
    needs to be told which toggle to flip, not that their model is unsafe."""
    try:
        prov.set_uncensored_only(True)
        why = app._model_block_reason("groq", "openai/gpt-oss-120b")
        assert why and "Uncensored-only mode is ON" in why, why
        assert "Settings" in why, why
        # ...and an actually-uncensored model passes while the mode is on.
        assert app._model_block_reason("g4f", "some-uncensored-thing") is None
    finally:
        prov.set_uncensored_only(False)
    # With the mode OFF the safety wording is the one that applies.
    why = app._model_block_reason("g4f", "some-uncensored-thing")
    assert why and "safety filter" in why, why
    assert app._model_block_reason("groq", "openai/gpt-oss-120b") is None


def test_an_exhausted_chain_names_the_mode_as_the_reason():
    """REGRESSION. With the mode on, the pool is a handful of models on ONE
    provider, so a single rate-limit empties it and the user got

        503 All providers failed: none available

    -- true, but it reads like the whole hub is down rather than like a setting
    doing exactly what it was asked to."""
    assert app._no_candidates_hint() == ""          # off -> normal wording
    try:
        prov.set_uncensored_only(True)
        hint = app._no_candidates_hint()
        assert "uncensored-only mode is ON" in hint, hint
        assert "Settings" in hint, hint
    finally:
        prov.set_uncensored_only(False)
    assert app._no_candidates_hint() == ""


def test_the_setting_is_restored_on_boot():
    """providers.py holds it as a live module flag with no config import, so it
    starts empty every boot -- including the 5-hourly auto-update restart."""
    try:
        config.set_flag(app._UNCENSORED_FLAG, True)
        prov.set_uncensored_only(False)          # simulate a fresh process
        prov.set_uncensored_only(config.get_flag(app._UNCENSORED_FLAG, False))
        assert prov.uncensored_only() is True
    finally:
        prov.set_uncensored_only(False)
        config.set_flag(app._UNCENSORED_FLAG, False)
