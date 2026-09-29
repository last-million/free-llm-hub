"""OpenRouter: every free model, Space Bunny in the top band, a conclusive Test.

MEASURED 2026-09-29 on OpenRouter's live catalog:
  * stealth/space-bunny-alpha (anonymous, free in stealth, 1M window, tools,
    reasoning, image input) is priced 0 with NO ':free' suffix, so the
    'suffix_free' rule hid it -- the hub only knew it through a g4f relay,
    scored 14 as an unknown family. OWNER DIRECTIVE: "make it from the best
    models". The other zero-priced non-':free' rows were google/lyria-3-*
    (0 prompt/completion but AUDIO output, billed per song) and the
    openrouter/free router alias.
  * the provider Test probed the catalog in listed order and landed on a
    model the blocklist switches off, with a 16-token budget a thinking
    model spends before any text: "empty reply (inconclusive)" on every key.
"""
from unittest import mock

import pytest

import app
import config
import providers as prov

CATALOG = {"data": [
    {"id": "stealth/space-bunny-alpha", "pricing": {"prompt": "0", "completion": "0"},
     "architecture": {"input_modalities": ["text", "image", "video"], "output_modalities": ["text"]},
     "context_length": 1000000},
    {"id": "google/lyria-3-pro-preview", "pricing": {"prompt": "0", "completion": "0"},
     "architecture": {"output_modalities": ["text", "audio"]}},
    {"id": "openrouter/free", "pricing": {"prompt": "0", "completion": "0"},
     "architecture": {"output_modalities": ["text"]}},
    {"id": "anthropic/claude-sonnet-5", "pricing": {"prompt": "0.000003", "completion": "0.000015"},
     "architecture": {"output_modalities": ["text"]}},
    {"id": "qwen/qwen3.8-27b:free", "pricing": {"prompt": "0", "completion": "0"},
     "architecture": {"output_modalities": ["text"]}},
    {"id": "mystery/no-prices", "architecture": {"output_modalities": ["text"]}},
    {"id": "mystery/no-modalities", "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "mystery/odd-price", "pricing": {"prompt": "0", "completion": "0", "request": "0.02"},
     "architecture": {"output_modalities": ["text"]}},
]}


@pytest.fixture(autouse=True)
def _reset_extra():
    before = dict(prov._EXTRA_FREE)
    prov._EXTRA_FREE.clear()
    yield
    prov._EXTRA_FREE.clear()
    prov._EXTRA_FREE.update(before)


def test_only_free_text_models_without_the_suffix_are_picked():
    assert app._zero_priced_text_ids(CATALOG) == ["stealth/space-bunny-alpha"]
    assert app._zero_priced_text_ids({}) == [] and app._zero_priced_text_ids(None) == []


def test_openrouter_counts_them_as_free_once_the_catalog_is_read():
    assert prov.PROVIDERS["openrouter"].get("free_zero_text") is True
    assert not prov.is_free_model("openrouter", "stealth/space-bunny-alpha")
    prov.note_extra_free("openrouter", app._zero_priced_text_ids(CATALOG))
    assert prov.is_free_model("openrouter", "stealth/space-bunny-alpha")
    assert prov.is_free_model("openrouter", "qwen/qwen3.8-27b:free")      # the suffix rule stays
    for paid in ("google/lyria-3-pro-preview", "anthropic/claude-sonnet-5", "openrouter/free"):
        assert not prov.is_free_model("openrouter", paid), paid
    # a provider without the flag is not affected by OpenRouter's list
    assert not prov.is_free_model("kilocode", "stealth/space-bunny-alpha")


def test_live_discovery_lists_it(monkeypatch):
    r = mock.Mock(status_code=200)
    r.json.return_value = CATALOG
    monkeypatch.setattr(app.requests, "get", lambda *a, **k: r)
    monkeypatch.setattr(config, "get_provider_config",
                        lambda pid: {"api_keys": ["k"], "api_key": "k", "enabled": True})
    app._MODEL_CACHE.pop("openrouter", None) if hasattr(app, "_MODEL_CACHE") else None
    models = app.provider_free_models("openrouter", live=True)
    assert "stealth/space-bunny-alpha" in models
    assert "qwen/qwen3.8-27b:free" in models
    assert not any(m.startswith(("google/lyria", "anthropic/", "openrouter/free")) for m in models)


# ---------------------------------------------------------------- the ranking

def test_space_bunny_is_in_the_top_band():
    s = lambda m, p="openrouter": app._benchmark_score(p, m)      # noqa: E731
    assert s("stealth/space-bunny-alpha") == pytest.approx(app._PREF_FLOORS[12])
    assert s("stealth/space-bunny-alpha") > s("pixel-canary") > s("moonshotai/kimi-k3")
    assert app._PREF_FLOORS[12] < app._PREF_FLOORS[5]              # still under Claude's 138
    assert s("srv_x:stealth/space-bunny-alpha", "g4f") == pytest.approx(
        app._PREF_FLOORS[12] - app._RELAY_DISCOUNT["g4f"])
    for other in ("bunny-lora", "spaceship-bunnyhop", "space-bunnyx"):
        assert s(other) < 100, other


# ---------------------------------------------------------------- the Test

def _hdrs():
    return {"X-Free-LLM-Hub-Token": config.ensure_control_token(),
            "X-Free-LLM-Hub": "dashboard"}


def _chat_resp(content):
    r = mock.Mock(status_code=200)
    r.json.return_value = {"choices": [{"message": {"content": content}}]}
    r.headers, r.text, r.close = {}, "{}", mock.Mock()
    return r


def _run_test(monkeypatch, answers, blocked=()):
    calls = []
    catalog = mock.Mock(status_code=200)
    catalog.json.return_value = {"data": [
        {"id": "inclusionai/ling-3.0-flash-sante:free"},
        {"id": "liquid/lfm-2.5-2.6b:free"},
        {"id": "qwen/qwen3.8-27b:free"}]}

    def fake_chat(pid, payload, stream, only_key=app._NO_KEY_PIN):
        calls.append((payload["model"], payload["max_tokens"]))
        return _chat_resp(answers.get(payload["model"], ""))
    monkeypatch.setitem(prov.PROVIDERS["openrouter"], "default_free_models", [])
    monkeypatch.setattr(config, "get_provider_config",
                        lambda pid: {"api_key": "K", "api_keys": ["K"], "enabled": True})
    monkeypatch.setattr(app, "_models_url_for", lambda *a, **k: "https://example.test/models")
    monkeypatch.setattr(app.requests, "get", lambda *a, **k: catalog)
    monkeypatch.setattr(app, "_upstream_chat", fake_chat)
    monkeypatch.setattr(app, "_record_test_result", lambda *a, **k: ([], []))
    monkeypatch.setattr(app, "_model_block_reason",
                        lambda p, m: "switched off" if m in blocked else None)
    body = app.app.test_client().post("/api/test/openrouter", headers=_hdrs()).get_json()
    return body, calls


def test_the_probe_asks_an_allowed_strong_model_first_with_room(monkeypatch):
    body, calls = _run_test(monkeypatch, {"qwen/qwen3.8-27b:free": "7"},
                            blocked=("inclusionai/ling-3.0-flash-sante:free",))
    assert calls[0][0] == "qwen/qwen3.8-27b:free"                    # not the blocked one
    assert calls[0][1] == app._TEST_PROBE_MAX_TOKENS
    assert body["ok"] is True and "qwen/qwen3.8-27b:free" in body["detail"]


def test_an_empty_reply_moves_on_to_a_model_that_answers(monkeypatch):
    body, calls = _run_test(monkeypatch, {"liquid/lfm-2.5-2.6b:free": "7"})
    assert [c[0] for c in calls][:2] == ["qwen/qwen3.8-27b:free", "liquid/lfm-2.5-2.6b:free"]
    assert "liquid/lfm-2.5-2.6b:free" in body["detail"]
    assert "inconclusive" not in body["detail"]


def test_all_empty_still_proves_the_key(monkeypatch):
    body, calls = _run_test(monkeypatch, {})
    assert body["ok"] is True and "inconclusive" in body["detail"]
    assert len(calls) == 3                                           # tried each once


def test_a_model_that_hangs_is_abandoned_and_the_next_one_tried(monkeypatch):
    # MEASURED 2026-09-29: the health check spent 10+ minutes on nvidia alone.
    import time as _t
    monkeypatch.setattr(app, "_TEST_PROBE_CALL_SECONDS", 0.3)
    monkeypatch.setattr(app, "_TEST_PROBE_KEY_SECONDS", 5)
    real_chat = {"qwen/qwen3.8-27b:free": None, "liquid/lfm-2.5-2.6b:free": "7"}

    def slow_or_fast(pid, payload, stream, only_key=app._NO_KEY_PIN):
        if real_chat.get(payload["model"]) is None:
            _t.sleep(3)                                              # hangs
        return _chat_resp(real_chat.get(payload["model"]) or "")
    started = _t.monotonic()
    body, _calls = _run_test(monkeypatch, {"liquid/lfm-2.5-2.6b:free": "7"})
    monkeypatch.setattr(app, "_upstream_chat", slow_or_fast)
    body = app.app.test_client().post("/api/test/openrouter", headers=_hdrs()).get_json()
    assert body["ok"] is True and "liquid/lfm-2.5-2.6b:free" in body["detail"]
    assert _t.monotonic() - started < 2.5


def test_a_slow_provider_is_not_reported_as_dead_keys():
    # MEASURED 2026-09-29: "Timeout: no answer within 9s" put nvidia under
    # DEAD KEYS in the health report.
    for d in ("Key authenticates ... FAILS on every candidate tried: Timeout: no answer within 9s",
              "ReadTimeout: HTTPSConnectionPool read timed out", "ConnectionError: refused"):
        assert app._health_failure_kind(d) == "server", d
    assert app._health_failure_kind("HTTP 401: invalid api key") == "key"
    assert app._health_failure_kind("HTTP 403: no permission") == "key"


def test_quick_models_are_probed_before_slow_ones(monkeypatch):
    monkeypatch.setattr(app, "_is_slow_model", lambda p, m: m == "qwen/qwen3.8-27b:free")
    body, calls = _run_test(monkeypatch, {"liquid/lfm-2.5-2.6b:free": "7",
                                          "qwen/qwen3.8-27b:free": "7"})
    assert calls[0][0] == "liquid/lfm-2.5-2.6b:free"                 # quick one first
