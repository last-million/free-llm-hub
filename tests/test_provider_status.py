"""Provider cards tell the truth about keys, and quiet junk still costs a strike.

REPORTED 2026-09-28 (live Providers page):

1. An ENABLED provider that needs a key but has none said status_reason "ok",
   while routing (_enabled_keyed / _check_provider_ready) skipped it. It now
   says "no_key" with "No API key saved — add one or switch it off".
2. Keys that authenticate but cannot generate (opencode-zen 0/4, tokenrouter
   0/4: "Key authenticates and lists models ... none work") and partly dead
   pools (zenmux 1/5, g4f 1/4) were invisible between Test clicks, and
   rotation re-spent a hop on the same dead key every request. The Test
   verdict and live 401/403 strikes now mark a key DEAD (quota.mark_key_dead):
   the card says "dead keys: N of M" and usable_keys skips it (fail-open).
3. "2768.2768" / "11991199.1199" for "Answer with only the number" -- the
   answer repeated around a dot. A real decimal could look like that, so it is
   NOT trimmed; it is flagged (answer_check "echoed_decimal") and filed as a
   JUNK strike for that (provider, model), so repeat offenders are demoted and
   benched.

All fakes: no network, no live hub.
"""
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest

import answer_check
import app
import config
import quota

_DASH = {"X-Free-LLM-Hub": "dashboard"}
PID = "groq"                     # a keyed, free, non-paid provider


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    path = Path(tempfile.mkdtemp()) / "state" / "config.json"
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", str(path))
    monkeypatch.setattr(quota, "_PERSIST_PATH", None)
    monkeypatch.setattr(quota, "_STATE", {})
    monkeypatch.setattr(quota, "_MODEL_STATE", {})
    monkeypatch.setattr(quota, "_MODEL_THROTTLE", {})
    monkeypatch.setattr(quota, "_DYNAMIC", {})
    monkeypatch.setattr(quota, "_SOURCE_STATE", {})
    monkeypatch.setattr(quota, "_KEY_COOLDOWN", {})
    monkeypatch.setattr(quota, "_KEY_DEAD", {})
    monkeypatch.setattr(quota, "_KEY_AUTH_STRIKES", {})
    monkeypatch.setattr(app, "_dead_providers", {})
    monkeypatch.setattr(app, "_dead_provider_why", {})
    monkeypatch.setattr(app, "_dead_models", {})
    monkeypatch.setattr(app, "_provider_consec_fail", {})
    monkeypatch.setattr(app, "_provider_timeout_fail", {})
    monkeypatch.setattr(app, "_provider_authfail", {})
    monkeypatch.setattr(app, "_provider_keyfail", {})
    monkeypatch.setattr(app, "_outcomes", {})
    monkeypatch.setattr(app, "_save_perf_stats", lambda force=False: None)
    monkeypatch.setattr(app, "provider_free_models",
                        lambda pid, live=False: ["m-a", "m-b", "m-c"])
    yield


def _rows():
    headers = dict(_DASH, **{"X-Free-LLM-Hub-Token": config.ensure_control_token()})
    resp = app.app.test_client().get("/api/providers", headers=headers)
    assert resp.status_code == 200
    return {r["id"]: r for r in resp.get_json()}


def _fake_resp(status, text="{}", body=None):
    r = mock.Mock(status_code=status)
    r.json.return_value = body if body is not None else {
        "choices": [{"message": {"content": "hi"}}]}
    r.headers = {}
    r.text = text
    r.close = mock.Mock()
    return r


# --------------------------------------------------------------------------- #
# 1. enabled + needs a key + none saved -> no_key
# --------------------------------------------------------------------------- #

def test_enabled_keyed_provider_without_a_key_is_no_key():
    config.set_provider_config(PID, enabled=True)
    r = _rows()[PID]
    assert r["enabled"] is True and r["has_key"] is False
    assert r["status_reason"] == "no_key"
    assert r["detail"] == "No API key saved — add one or switch it off"
    assert r["until"] is None


def test_routing_still_skips_it():
    config.set_provider_config(PID, enabled=True)
    assert PID not in app._enabled_keyed()
    assert "no API key" in (app._check_provider_ready(PID) or "")


def test_disabled_provider_without_a_key_is_not_no_key():
    config.set_provider_config(PID, enabled=False)
    assert _rows()[PID]["status_reason"] != "no_key"


def test_a_saved_key_clears_no_key():
    config.set_provider_config(PID, enabled=True, api_key="sk-live-aaaaaaaaaaaa")
    r = _rows()[PID]
    assert r["status_reason"] == "ok"
    assert r["detail"] == ""


def test_open_no_key_gateway_is_never_no_key():
    import providers as prov
    gateways = [p["id"] for p in prov.list_providers() if p.get("no_key")]
    assert gateways, "registry has no no_key provider to check"
    for g in gateways:
        config.set_provider_config(g, enabled=True)
    rows = _rows()
    for g in gateways:
        assert rows[g]["status_reason"] != "no_key", g


# --------------------------------------------------------------------------- #
# 2. dead keys: ledger, rotation, Test verdict, card
# --------------------------------------------------------------------------- #

def test_usable_keys_skips_dead_keys_and_fails_open():
    quota.mark_key_dead(PID, "k2", why="Test: HTTP 403: no credits")
    assert quota.usable_keys(PID, ["k1", "k2", "k3"]) == ["k1", "k3"]
    quota.mark_key_dead(PID, "k1")
    quota.mark_key_dead(PID, "k3")
    # every key dead: tried anyway, the provider is the real authority
    assert quota.usable_keys(PID, ["k1", "k2", "k3"]) == ["k1", "k2", "k3"]


def test_dead_mark_expires():
    quota.mark_key_dead(PID, "k1", seconds=0.01)
    time.sleep(0.03)
    assert quota.key_dead(PID, "k1") is False
    assert quota.dead_keys(PID) == {}


def test_live_credential_401_twice_marks_dead_and_2xx_clears():
    app._note_key_live_status(PID, "k1", _fake_resp(401, '{"error":"invalid api key"}'))
    assert quota.key_dead(PID, "k1") is False        # one strike is not proof
    app._note_key_live_status(PID, "k1", _fake_resp(401, '{"error":"invalid api key"}'))
    assert quota.key_dead(PID, "k1") is True
    assert quota.dead_keys(PID)[quota.key_fingerprint("k1")]["source"] == "live"
    app._note_key_live_status(PID, "k1", _fake_resp(200))
    assert quota.key_dead(PID, "k1") is False


def test_model_scoped_401_never_marks_the_key():
    # opencode-zen answers 401 for a withdrawn MODEL -- the key is fine.
    body = '{"error":"Model north-mini-code-free is not supported"}'
    for _ in range(4):
        app._note_key_live_status(PID, "k1", _fake_resp(401, body))
    assert quota.key_dead(PID, "k1") is False


def test_429_and_5xx_on_live_traffic_do_not_mark_dead():
    for code in (429, 500, 503):
        for _ in range(3):
            app._note_key_live_status(PID, "k1", _fake_resp(code))
    assert quota.key_dead(PID, "k1") is False


def test_upstream_chat_rotation_skips_a_dead_key():
    """End to end through the real _upstream_chat: the dead key is never
    posted to while a live one exists."""
    posted = []

    def fake_post(url=None, json=None, headers=None, **kw):
        posted.append((headers or {}).get("Authorization"))
        return _fake_resp(200, body={"choices": [{"message": {"content": "ok"}}]})

    config.set_provider_config(PID, enabled=True, api_key="DEADKEY-000000")
    config.add_provider_key(PID, "GOODKEY-111111")
    quota.mark_key_dead(PID, "DEADKEY-000000", why="Test: HTTP 403")
    with mock.patch.object(app.requests, "post", side_effect=fake_post):
        for _ in range(3):
            app._upstream_chat(PID, {"model": "m-a", "max_tokens": 8,
                                     "messages": [{"role": "user", "content": "hi"}]},
                               stream=False)
    assert posted and all(a == "Bearer GOODKEY-111111" for a in posted), posted


def _run_test(keys, fake_chat):
    with mock.patch.object(config, "get_provider_config",
                           return_value={"api_key": keys[0], "api_keys": list(keys),
                                         "enabled": True}), \
            mock.patch.object(app, "_models_url_for", return_value=None), \
            mock.patch.object(app, "_upstream_chat", side_effect=fake_chat), \
            mock.patch.object(app, "_record_test_result", return_value=([], [])):
        headers = dict(_DASH, **{"X-Free-LLM-Hub-Token": config.ensure_control_token()})
        return app.app.test_client().post("/api/test/" + PID, headers=headers).get_json()


def test_test_verdict_marks_dead_keys_and_the_card_counts_them():
    keys = ["KEY-GOOD-000001", "KEY-DEAD-000002", "KEY-DEAD-000003",
            "KEY-DEAD-000004", "KEY-DEAD-000005"]

    def fake_chat(pid, payload, stream, only_key=app._NO_KEY_PIN):
        if only_key == keys[0]:
            return _fake_resp(200)
        return _fake_resp(402, body={"error": {"message": "insufficient credits"}})

    body = _run_test(keys, fake_chat)
    assert body["ok"] is True and "1 of 5 keys work" in body["detail"]
    assert not quota.key_dead(PID, keys[0])
    assert all(quota.key_dead(PID, k) for k in keys[1:])
    assert quota.usable_keys(PID, keys) == [keys[0]]

    # The card: same pool saved for real, then /api/providers.
    config.set_provider_config(PID, enabled=True, api_key=keys[0])
    for k in keys[1:]:
        config.add_provider_key(PID, k)
    r = _rows()[PID]
    assert r["status_reason"] == "ok"
    assert "dead keys: 4 of 5" in r["detail"], r["detail"]
    dead_flags = [row["dead"] for row in r["keys"]]
    assert dead_flags == [False, True, True, True, True]
    assert "insufficient credits" in r["keys"][1]["dead_why"]
    raw = str(r)
    for k in keys:
        assert k not in raw                       # never the secret itself


def test_all_keys_dead_says_so_and_stays_routable():
    keys = ["ZEN-KEY-AAAAAA01", "ZEN-KEY-AAAAAA02", "ZEN-KEY-AAAAAA03", "ZEN-KEY-AAAAAA04"]

    def fake_chat(pid, payload, stream, only_key=app._NO_KEY_PIN):
        return _fake_resp(403, body={"error": {"message": "generation not allowed"}})

    body = _run_test(keys, fake_chat)
    assert body["ok"] is False
    config.set_provider_config(PID, enabled=True, api_key=keys[0])
    for k in keys[1:]:
        config.add_provider_key(PID, k)
    r = _rows()[PID]
    assert "dead keys: 4 of 4" in r["detail"], r["detail"]
    assert quota.usable_keys(PID, keys) == keys        # fail-open, honestly said


def test_passing_test_clears_a_dead_key():
    keys = ["CLR-KEY-000001", "CLR-KEY-000002"]
    for k in keys:
        quota.mark_key_dead(PID, k, why="old")
    _run_test(keys, lambda pid, payload, stream, only_key=app._NO_KEY_PIN: _fake_resp(200))
    assert not any(quota.key_dead(PID, k) for k in keys)


def test_test_429_and_network_errors_do_not_mark_dead():
    keys = ["RL-KEY-0000001", "NET-KEY-000002"]

    def fake_chat(pid, payload, stream, only_key=app._NO_KEY_PIN):
        if only_key == keys[0]:
            return _fake_resp(429, body={"error": {"message": "rate limited"}})
        raise app.requests.ConnectionError("unreachable")

    with mock.patch.object(app.time, "sleep", lambda s: None):
        _run_test(keys, fake_chat)
    assert not any(quota.key_dead(PID, k) for k in keys)


def test_dead_keys_survive_a_restart_by_fingerprint_only():
    path = Path(tempfile.mkdtemp()) / "quota.json"
    quota.mark_key_dead(PID, "PERSIST-KEY-0001", why="Test: HTTP 402")
    with mock.patch.object(quota, "_PERSIST_PATH", str(path)):
        quota.save_state()
    raw = path.read_text(encoding="utf-8")
    assert "PERSIST-KEY-0001" not in raw
    quota._KEY_DEAD.clear()
    quota._load_state(str(path))
    assert quota.key_dead(PID, "PERSIST-KEY-0001") is True


# --------------------------------------------------------------------------- #
# 3. echoed decimal: never cut, always a junk strike
# --------------------------------------------------------------------------- #

ASK = "What is 2767 plus 1? Answer with only the number."


@pytest.mark.parametrize("reply", ["2768.2768", "11991199.1199", " 2768.2768\n", "2768.2768."])
def test_echoed_decimal_is_flagged(reply):
    assert answer_check.echoed_decimal(reply, ASK) is True


@pytest.mark.parametrize("reply,prompt", [
    ("2768", ASK),                                  # the clean answer
    ("2768.2769", ASK),                             # a different right side
    ("12.12", ASK),                                 # 2-digit side: plausible
    ("3.14159", ASK),                               # a real decimal
    ("2768.2768", "What is 2767 plus 1?"),          # no bare-number ask
    ("2768.2768", "Repeat 2768.2768. Answer with only the number."),  # echo
])
def test_echoed_decimal_guards(reply, prompt):
    assert answer_check.echoed_decimal(reply, prompt) is False


def test_echoed_decimal_is_not_flagged_on_tool_turns():
    assert answer_check.echoed_decimal("2768.2768", ASK, tools_offered=True) is False


def test_inspect_keeps_it_ok_and_uncut():
    for reply in ("2768.2768", "11991199.1199"):
        v = answer_check.inspect(reply, prompt_text=ASK, last_prompt=ASK)
        assert v["ok"] is True
        assert v["salvage"] is None
        assert "echoed_decimal" in v["reasons"]
    # a stream still in progress is never judged for it
    v = answer_check.inspect("2768.2768", prompt_text=ASK, last_prompt=ASK, partial=True)
    assert "echoed_decimal" not in v["reasons"]


def _chat_json(text):
    return {"choices": [{"message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}]}


def _payload():
    return {"model": "m-x", "messages": [{"role": "user", "content": ASK}]}


def test_answer_gate_serves_it_unchanged_and_files_a_junk_strike():
    data = _chat_json("2768.2768")
    assert app._answer_gate(data, _payload(), False, hop=("fakeprov", "m-x")) == "ok"
    assert data["choices"][0]["message"]["content"] == "2768.2768"
    rec = app._outcomes[("fakeprov", "m-x")]
    assert rec["fail"] == app._JUNK_FAIL_WEIGHT
    assert len(app._junk_events[("fakeprov", "m-x")]) == 1


def test_clean_answer_files_no_strike():
    data = _chat_json("2768")
    assert app._answer_gate(data, _payload(), False, hop=("fakeprov", "m-x")) == "ok"
    assert ("fakeprov", "m-x") not in app._outcomes
    assert ("fakeprov", "m-x") not in app._junk_events


def test_gate_without_hop_still_serves_it():
    data = _chat_json("2768.2768")
    assert app._answer_gate(data, _payload(), False) == "ok"
    assert data["choices"][0]["message"]["content"] == "2768.2768"


def test_repeat_offender_is_benched_and_demoted():
    for _ in range(app._JUNK_BENCH_STRIKES):
        app._answer_gate(_chat_json("11991199.1199"), _payload(), False,
                         hop=("fakeprov", "m-x"))
    assert app._is_pair_benched("fakeprov", "m-x") is True
    assert app._answer_quality_penalty("fakeprov", "m-x") >= app._JUNK_BENCH_PENALTY
    # ...and only that pair: the same model elsewhere is untouched
    assert app._is_pair_benched("otherprov", "m-x") is False


def test_streamed_echo_files_the_strike_too():
    app._record_stream_outcome("streamprov", "m-s", "2768.2768",
                               prompt_text=ASK, last_prompt=ASK)
    rec = app._outcomes[("streamprov", "m-s")]
    # one delivery credit + one junk-weighted failure
    assert rec["ok"] == 1 and rec["fail"] == app._JUNK_FAIL_WEIGHT
    assert len(app._junk_events[("streamprov", "m-s")]) == 1


def test_every_chain_gate_passes_its_hop():
    """Every chat loop that gates an answer names the hop it came from, so the
    strike lands on the right pair (the hedge-leg judge excepted: it only
    ranks legs, the winner is gated again by its loop)."""
    import inspect as _inspect
    src = _inspect.getsource(app)
    calls = [line.strip() for line in src.splitlines()
             if "_answer_gate(data, payload" in line and "def " not in line]
    assert calls
    unnamed = [c for c in calls if "hop=" not in c and "bool(body.get" not in c
               and not c.startswith("return _answer_gate(data, payload, False)")]
    assert not unnamed, unnamed


# --------------------------------------------------------------------------- #
# Static template: the card shows no_key clearly, escaped
# --------------------------------------------------------------------------- #

def _template():
    return (Path(app.__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")


def test_card_labels_no_key_without_the_out_of_quota_cross():
    html = _template()
    assert "var NOTE_REASONS = { no_key: 'No API key' };" in html
    assert "OUT_REASONS[p.status_reason] || NOTE_REASONS[p.status_reason]" in html
    out = html.split("var OUT_REASONS = {", 1)[1].split("};", 1)[0]
    assert "no_key" not in out            # not an outage: no red cross


def test_card_detail_is_escaped():
    html = _template()
    fn = html.split("function outReasonLine(p){", 1)[1].split("function usedByLine", 1)[0]
    assert "esc(label)" in fn
    assert "esc(p.detail)" in fn
    assert "+ p.detail" not in fn
