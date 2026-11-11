"""Automatic image generation uses the best model that is available.

Owner, 2026-11-11: "for image provider he should automatically as default use the
best model available image generation, not any randomly."

Two things made it look random:

1. The automatic order was a fixed order of PROVIDERS, not of quality.
2. MEASURED on his image history: 9 of 13 images came from the weakest model he
   has. Pollinations' free endpoint allows one image every 45 s and answers a
   faster request with HTTP 402; the hub read that 402 as "model dead for 6 h".

Hermetic: fake providers, fake HTTP replies in the shape the live probe showed,
a fake clock. No network, no real waiting.
"""
import os

import pytest
import requests

import app
import config
import imagerank
import providers as prov

CF = "cloudflare"
MS = "modelscope"
PO = "pollinations"
AH = "aihorde"

CF_SCHNELL = (CF, "@cf/black-forest-labs/flux-1-schnell")
CF_SDXL_L = (CF, "@cf/bytedance/stable-diffusion-xl-lightning")
CF_SDXL = (CF, "@cf/stabilityai/stable-diffusion-xl-base-1.0")
CF_DREAM = (CF, "@cf/lykon/dreamshaper-8-lcm")
MS_QWEN = (MS, "Qwen/Qwen-Image")
MS_ZIT = (MS, "Tongyi/Z-Image-Turbo")
MS_DEV = (MS, "black-forest-labs/FLUX.1-dev")
MS_SCHNELL = (MS, "black-forest-labs/FLUX.1-schnell")
PO_FLUX = (PO, "flux")
PO_TURBO = (PO, "turbo")
AH_SD = (AH, "stable_diffusion")

B64 = "ZmFrZS1pbWFnZQ=="


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    path = tmp_path / "state" / "config.json"
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", str(path))
    app._runtime_active[0] = 0
    app._runtime_shutdown_thread[0] = None
    app._runtime_server[0] = None
    return path


def _fleet(monkeypatch, pids, state=None):
    monkeypatch.setattr(app, "_available_providers", lambda: list(pids))
    monkeypatch.setattr(config, "get_images_state", lambda: dict(state or {"priority_mode": "auto"}))
    monkeypatch.setattr(app, "_is_model_dead", lambda pid, model: False)


# --------------------------------------------------------------------------- #
# 1. The board (imagerank.py): real numbers, matched by what the id names
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("model_id,expected", [
    ("@cf/black-forest-labs/flux-1-schnell", 742),
    ("black-forest-labs/FLUX.1-schnell", 742),
    ("togetherai:black-forest-labs/flux.1-schnell", 742),
    ("black-forest-labs/flux-schnell", 742),
    ("black-forest-labs/FLUX.1-dev", 820),
    ("Qwen/Qwen-Image", 906),
    ("Tongyi/Z-Image-Turbo", 936),
    ("gpt-image-1-mini", 936),
    ("flux-pro/kontext/max/text-to-image", 931),
    ("gemini-2.5-flash-image", 987),
    ("gpt-image-1", 1016),
    ("bytedance-seed/seedream-4.5", 1030),
    ("gemini-3-pro-image-preview", 1120),
    ("higgsfield/nano-banana-pro", 1120),
    ("gpt-image-1.5", 1125),
    ("gemini-3.1-flash-image", 1143),
    ("gpt-image-2", 1190),
])
def test_a_listed_model_has_the_boards_score_on_every_host_spelling(model_id, expected):
    assert imagerank.score(model_id) == expected
    assert imagerank.info(model_id)["name"]


@pytest.mark.parametrize("model_id", [
    "flux", "turbo", "ai-image", "stable_diffusion",
    "@cf/bytedance/stable-diffusion-xl-lightning",
    "@cf/stabilityai/stable-diffusion-xl-base-1.0",
    "@cf/lykon/dreamshaper-8-lcm",
    "bytedance/seedream/v4/text-to-image",
    "higgsfield/text2image/soul",
    # neighbours of listed models that are OTHER models
    "gpt-image-2.5-flare", "gpt-image-2.6-sunburst", "qwen-image-2.1", "qwen-image-edit",
    "qwen-image-3", "flux.1.1-pro", "flux.2-klein-4b", "flux-2-dev-free",
    "gemini-3.1-flash-lite-image", "seedream-4.0", "seedream-5.0-pro",
    "", None, 123,
])
def test_a_model_the_board_does_not_list_gets_no_score(model_id):
    assert imagerank.score(model_id) is None
    assert imagerank.info(model_id) is None


def test_the_board_names_its_source_and_every_score_is_a_plain_number():
    assert imagerank.URL.startswith("https://artificialanalysis.ai/")
    assert imagerank.DATE == "2026-11-11" and imagerank.SOURCE
    scores = [s for _p, _n, s in imagerank._ROWS]
    assert all(isinstance(s, int) and 600 < s < 1400 for s in scores)


def test_rank_is_stable_and_only_reorders():
    pairs = [PO_FLUX, CF_SCHNELL, AH_SD, MS_ZIT, MS_SCHNELL, CF_DREAM, MS_QWEN]
    out = imagerank.rank(pairs)
    assert sorted(out) == sorted(pairs)                      # nothing added or dropped
    assert out[:2] == [MS_ZIT, MS_QWEN]                      # best score first
    assert out[2:4] == [CF_SCHNELL, MS_SCHNELL]              # equal score: the given order
    assert out[4:] == [PO_FLUX, AH_SD, CF_DREAM]             # unlisted: the given order, last
    assert pairs == [PO_FLUX, CF_SCHNELL, AH_SD, MS_ZIT, MS_SCHNELL, CF_DREAM, MS_QWEN]


def test_rank_never_raises():
    assert imagerank.rank([]) == []
    odd = [("a",), None, 5]
    assert imagerank.rank(odd) == odd
    assert imagerank.rank(None) is None


# --------------------------------------------------------------------------- #
# 2. The automatic order
# --------------------------------------------------------------------------- #

def test_auto_opens_on_the_best_model_available(monkeypatch):
    _fleet(monkeypatch, [PO, CF, MS, AH])
    assert app._image_candidates() == [
        MS_ZIT, MS_QWEN, MS_DEV,           # 936, 906, 820
        CF_SCHNELL, MS_SCHNELL,            # 742 twice: the old provider order breaks the tie
        PO_FLUX, AH_SD, CF_SDXL_L, PO_TURBO, CF_SDXL, CF_DREAM,   # not on the board
    ]
    assert app._resolve_image_model("auto") == MS_ZIT
    assert app._resolve_image_model("") == MS_ZIT


def test_before_this_change_auto_opened_on_cloudflares_schnell(monkeypatch):
    """The negative control: the same fleet with the ranking off is the fixed
    provider order, which put a 742 model ahead of a 936 one."""
    _fleet(monkeypatch, [PO, CF, MS, AH])
    monkeypatch.setattr(app, "_image_rank_on", lambda: False)
    off = app._image_candidates()
    assert off[:4] == [CF_SCHNELL, MS_QWEN, PO_FLUX, AH_SD]
    monkeypatch.setattr(app, "_image_rank_on", lambda: True)
    on = app._image_candidates()
    assert sorted(on) == sorted(off) and on != off          # the same models, reordered
    assert on == imagerank.rank(off)


def test_the_owners_fleet_keeps_its_order(monkeypatch):
    """Only Pollinations and AI Horde: neither is on the board, so nothing moves
    (what changes for that fleet is the queue, section 4)."""
    _fleet(monkeypatch, [PO, AH])
    assert app._image_candidates() == [PO_FLUX, AH_SD, PO_TURBO]


def test_a_saved_free_priority_still_goes_first(monkeypatch):
    _fleet(monkeypatch, [PO, CF, MS],
           {"priority_mode": "manual", "manual_priority": ["pollinations/flux"]})
    got = app._image_candidates()
    assert got[0] == PO_FLUX
    assert got[1] == MS_ZIT
    assert got.count(PO_FLUX) == 1


def test_a_paid_model_is_never_automatic_however_high_it_scores(monkeypatch):
    _fleet(monkeypatch, [CF, "openai", "google", "openrouter", "puter", "higgsfield"],
           {"priority_mode": "manual", "manual_priority": ["puter/gpt-image-1", "openai/gpt-image-2"]})
    got = app._image_candidates()
    assert {pid for pid, _ in got} == {CF}
    assert got[0] == CF_SCHNELL


def test_the_flag_off_or_unreadable_is_the_old_order(monkeypatch):
    _fleet(monkeypatch, [CF, MS])
    monkeypatch.setattr(config, "get_flag",
                        lambda name, default=None: False if name == "image_rank_by_quality" else default)
    assert app._image_rank_on() is False
    assert app._image_candidates()[0] == CF_SCHNELL

    def boom(*a, **k):
        raise RuntimeError("config unreadable")
    monkeypatch.setattr(config, "get_flag", boom)
    assert app._image_rank_on() is False
    assert app._image_candidates()[0] == CF_SCHNELL


def test_a_broken_board_leaves_the_order_alone(monkeypatch):
    _fleet(monkeypatch, [CF, MS])
    monkeypatch.setattr(app, "_image_rank_on", lambda: True)

    def boom(pairs):
        raise RuntimeError("x")
    monkeypatch.setattr(imagerank, "rank", boom)
    assert app._image_candidates()[0] == CF_SCHNELL


# --------------------------------------------------------------------------- #
# 3. The walk: a provider that failed a hop goes behind the others
# --------------------------------------------------------------------------- #

def _walk(monkeypatch, chain, results, rank_on=True):
    """POST one automatic image request over `chain`; returns (response, calls)."""
    calls = []
    monkeypatch.setattr(app, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(app, "_image_candidates", lambda: list(chain))
    monkeypatch.setattr(app, "_image_text_qa_flagged", lambda b64: False)
    monkeypatch.setattr(app, "_image_rank_on", lambda: rank_on)

    def make(pid):
        def gen(pcfg, model, prompt, size=1024, steps=4):
            calls.append((pid, model))
            r = results.get((pid, model), (500, None, "boom"))
            if isinstance(r, Exception):
                raise r
            return r
        return gen
    for pid in {p for p, _ in chain}:
        monkeypatch.setitem(app._IMAGE_GENERATORS, pid, make(pid))
    resp = app.app.test_client().post("/v1/images/generations", json={"prompt": "a fox"})
    return resp, calls


def test_a_failed_provider_does_not_get_the_next_hop(isolated_config, monkeypatch):
    chain = [MS_ZIT, MS_QWEN, MS_DEV, CF_SCHNELL, PO_FLUX]
    resp, calls = _walk(monkeypatch, chain, {CF_SCHNELL: (200, B64, None)})
    assert resp.status_code == 200
    assert calls == [MS_ZIT, CF_SCHNELL]
    assert resp.get_json()["model"] == "cloudflare/@cf/black-forest-labs/flux-1-schnell"


def test_an_exception_moves_the_provider_back_too(isolated_config, monkeypatch):
    chain = [MS_ZIT, MS_QWEN, CF_SCHNELL]
    resp, calls = _walk(monkeypatch, chain, {MS_ZIT: requests.ConnectionError("reset"),
                                             CF_SCHNELL: (200, B64, None)})
    assert resp.status_code == 200
    assert calls == [MS_ZIT, CF_SCHNELL]


def test_each_failing_provider_yields_then_the_first_one_gets_its_second_model(
        isolated_config, monkeypatch):
    chain = [MS_ZIT, MS_QWEN, CF_SCHNELL, PO_FLUX]
    resp, calls = _walk(monkeypatch, chain, {MS_QWEN: (200, B64, None)})
    assert calls == [MS_ZIT, CF_SCHNELL, PO_FLUX, MS_QWEN]   # nothing is dropped
    assert resp.status_code == 200
    assert resp.get_json()["model"] == "modelscope/Qwen/Qwen-Image"


def test_with_one_provider_left_its_other_models_are_still_tried(isolated_config, monkeypatch):
    chain = [CF_SCHNELL, CF_SDXL_L, CF_SDXL]
    resp, calls = _walk(monkeypatch, chain, {CF_SDXL: (200, B64, None)})
    assert calls == chain
    assert resp.status_code == 200


def test_a_text_flagged_image_is_not_a_provider_failure(isolated_config, monkeypatch):
    """The provider answered; only the image was rejected. The next BEST model is
    tried, the same provider included (Qwen Image is the stronger one at text)."""
    chain = [MS_ZIT, MS_QWEN, CF_SCHNELL]
    seen = []

    def flagged(b64):
        seen.append(b64)
        return len(seen) == 1                      # the first image only
    resp, calls = _walk(monkeypatch, chain, {MS_ZIT: (200, "Zmlyc3Q=", None),
                                             MS_QWEN: (200, B64, None),
                                             CF_SCHNELL: (200, "dGhpcmQ=", None)})
    # _walk installed "never flagged"; run it again with the real scenario
    seen.clear()
    monkeypatch.setattr(app, "_image_text_qa_flagged", flagged)
    calls.clear()
    resp = app.app.test_client().post("/v1/images/generations", json={"prompt": "a sign"})
    assert calls == [MS_ZIT, MS_QWEN]
    assert resp.get_json()["model"] == "modelscope/Qwen/Qwen-Image"


def test_the_hop_budget_is_unchanged(isolated_config, monkeypatch):
    chain = [MS_ZIT, CF_SCHNELL, PO_FLUX, AH_SD, MS_QWEN, CF_SDXL_L]
    resp, calls = _walk(monkeypatch, chain, {})
    assert len(calls) == app.MAX_IMAGE_HOPS == 4
    assert resp.status_code == 502
    assert "All image providers failed" in resp.get_json()["error"]["message"]


def test_the_flag_off_walks_the_first_hops_in_order_as_before(isolated_config, monkeypatch):
    chain = [MS_ZIT, MS_QWEN, CF_SCHNELL]
    resp, calls = _walk(monkeypatch, chain, {MS_QWEN: (200, B64, None)}, rank_on=False)
    assert calls == [MS_ZIT, MS_QWEN]
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# 4. A free image limit is a queue, not a dead model
# --------------------------------------------------------------------------- #

class _Resp:
    def __init__(self, status, content=b"", text="", headers=None):
        self.status_code = status
        self.content = content
        self.text = text
        self.headers = headers or {}


def _limit(retry_after="41"):
    """The reply the live probe got for a second image at once."""
    hdr = {"Retry-After": retry_after} if retry_after is not None else {}
    return _Resp(402, b"{}", '{"status":402,"success":false,"error":{"code":"FREE_LIMIT",'
                             '"message":"Free limit reached: 1 image every 45 seconds per IP."}}', hdr)


def _ok(body=b"image-bytes"):
    return _Resp(200, body)


class _Clock:
    def __init__(self):
        self.t = 1_000_000.0
        self.naps = []

    def now(self):
        return self.t

    def nap(self, seconds):
        self.naps.append(seconds)
        self.t += seconds


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(app, "_image_pace_now", c.now)
    monkeypatch.setattr(app, "_image_pace_nap", c.nap)
    monkeypatch.setattr(app, "_image_pace_on", lambda: True)
    monkeypatch.setattr(app, "_client_gone", lambda: False)
    app._image_pace_next.clear()
    return c


def _http(monkeypatch, replies, clock=None):
    """requests.get answers `replies` in order; returns the list of call times."""
    times = []
    queue = list(replies)

    def get(url, **kw):
        times.append(clock.t if clock else len(times))
        assert "image.pollinations.ai/prompt/" in url
        return queue.pop(0)
    monkeypatch.setattr(app.requests, "get", get)
    return times


@pytest.mark.parametrize("resp,expected", [
    (_limit("41"), 41.0),
    (_limit(None), 45.0),                                   # the body code alone
    (_Resp(429, b"", "slow down", {"Retry-After": "5"}), 5.0),
    (_Resp(402, b"", '{"error":{"code":"FREE_LIMIT"}}', {"Retry-After": "soon"}), 45.0),
    (_Resp(402, b"", "x", {"Retry-After": "0"}), 1.0),      # clamped
    (_Resp(429, b"", "x", {"Retry-After": "99999"}), 600.0),
    (_Resp(402, b"", "Payment required: this account has no credit"), None),   # a real refusal
    (_Resp(429, b"", "too many requests"), None),           # no time named: the caller's 429 path
    (_Resp(200, b"img"), None),
    (_Resp(500, b"", "FREE_LIMIT", {"Retry-After": "9"}), None),
])
def test_what_counts_as_a_rate_limit_reply(resp, expected):
    assert app._image_rate_limited(resp) == expected


def test_a_reply_object_without_headers_is_not_a_rate_limit():
    class Bare:
        status_code = 200
        content = b"x"
        text = ""
    assert app._image_rate_limited(Bare()) is None
    assert app._image_rate_limited(None) is None


def test_slots_are_45_seconds_apart_and_a_far_turn_is_refused(clock):
    assert app._image_pace_reserve(PO) == 0.0
    assert app._image_pace_reserve(PO) == 45.0
    assert app._image_pace_reserve(PO) is None              # 90 s away: more than the 50 s limit
    assert app._image_pace_next[PO] == clock.t + 90         # the refused request took no slot
    clock.t += 45
    assert app._image_pace_reserve(PO) == 45.0
    # a provider with no documented spacing never waits and is never recorded
    assert app._image_pace_reserve(CF) == 0.0 and app._image_pace_reserve(CF) == 0.0
    assert CF not in app._image_pace_next


def test_the_second_image_waits_its_turn_instead_of_asking_early(clock, monkeypatch):
    times = _http(monkeypatch, [_ok(b"one"), _ok(b"two")], clock)
    s1, b1, _ = app._pollinations_generate_image({"api_keys": []}, "flux", "a fox")
    s2, b2, _ = app._pollinations_generate_image({"api_keys": []}, "flux", "a cat")
    assert (s1, s2) == (200, 200) and b1 != b2
    assert times[1] - times[0] == pytest.approx(45.0)
    assert sum(clock.naps) == pytest.approx(45.0)
    assert max(clock.naps) <= 0.5                           # short slices, so a gone client is noticed


def test_too_soon_from_the_server_is_waited_out_and_asked_once_more(clock, monkeypatch):
    """After a restart the hub does not know when the last image was made: the
    server says "retry in 41 s", the hub waits and asks again."""
    times = _http(monkeypatch, [_limit("41"), _ok()], clock)
    status, b64, detail = app._pollinations_generate_image({"api_keys": []}, "flux", "a fox")
    assert status == 200 and b64 and detail is None
    assert len(times) == 2
    assert 41.0 <= times[1] - times[0] <= 46.0


def test_the_limit_never_comes_back_as_402(clock, monkeypatch):
    times = _http(monkeypatch, [_limit("41"), _limit("44")], clock)
    status, b64, detail = app._pollinations_generate_image({"api_keys": []}, "flux", "a fox")
    assert status == 429 and b64 is None                    # a throttle for the caller, not "dead"
    assert status not in app._DEAD_STATUSES
    assert "45 s" in detail
    assert len(times) == 2                                  # one retry, never a loop


def test_with_the_queue_off_the_limit_is_still_not_a_dead_model(clock, monkeypatch):
    monkeypatch.setattr(app, "_image_pace_on", lambda: False)
    times = _http(monkeypatch, [_limit("41")], clock)
    status, _b64, _detail = app._pollinations_generate_image({"api_keys": []}, "flux", "a fox")
    assert status == 429
    assert len(times) == 1 and clock.naps == []             # asked at once, no waiting


def test_a_real_payment_refusal_is_still_reported_as_402(clock, monkeypatch):
    times = _http(monkeypatch, [_Resp(402, b"", "Payment required: no credit on this key")], clock)
    status, _b64, detail = app._pollinations_generate_image({"api_keys": []}, "flux", "a fox")
    assert status == 402 and "no credit" in detail
    assert len(times) == 1


def test_a_turn_too_far_away_leaves_the_provider_to_the_next_one(clock, monkeypatch):
    times = _http(monkeypatch, [_ok()], clock)
    app._image_pace_next[PO] = clock.t + 200
    status, b64, detail = app._pollinations_generate_image({"api_keys": []}, "flux", "a fox")
    assert status == 503 and b64 is None and "busy" in detail
    assert status not in app._DEAD_STATUSES and status != 429
    assert times == [] and clock.naps == []                 # nothing sent, nobody held


def test_a_client_that_left_is_not_waited_for(clock, monkeypatch):
    times = _http(monkeypatch, [_ok()], clock)
    app._image_pace_next[PO] = clock.t + 30
    monkeypatch.setattr(app, "_client_gone", lambda: True)
    status, b64, _detail = app._pollinations_generate_image({"api_keys": []}, "flux", "a fox")
    assert status == 499 and b64 is None
    assert times == [] and clock.naps == []


def _spies(monkeypatch):
    dead, throttled = [], []
    monkeypatch.setattr(app, "_mark_model_dead", lambda pid, model, status: dead.append((pid, model, status)))
    monkeypatch.setattr(app.quota, "mark_throttled",
                        lambda pid, seconds=None: throttled.append((pid, seconds)))
    return dead, throttled


def _request_over(monkeypatch, chain, ah_result=(200, B64, None)):
    monkeypatch.setattr(app, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(app, "_image_candidates", lambda: list(chain))
    monkeypatch.setattr(app, "_image_text_qa_flagged", lambda b64: False)
    monkeypatch.setattr(app, "_image_rank_on", lambda: True)
    monkeypatch.setitem(app._IMAGE_GENERATORS, PO, app._pollinations_generate_image)
    monkeypatch.setitem(app._IMAGE_GENERATORS, AH,
                        lambda pcfg, model, prompt, size=1024, steps=4: ah_result)
    return app.app.test_client().post("/v1/images/generations", json={"prompt": "a fox"})


def test_the_request_that_met_the_limit_gets_its_image_from_the_same_model(
        isolated_config, clock, monkeypatch):
    """THE REPORTED CASE end to end: the server says "too soon"; the request waits,
    is served by Pollinations, and nothing is marked dead or throttled."""
    dead, throttled = _spies(monkeypatch)
    _http(monkeypatch, [_limit("41"), _ok()], clock)
    resp = _request_over(monkeypatch, [PO_FLUX, AH_SD])
    assert resp.status_code == 200
    assert resp.get_json()["model"] == "pollinations/flux"
    assert dead == [] and throttled == []


def test_a_limit_that_holds_is_a_short_throttle_never_a_dead_model(
        isolated_config, clock, monkeypatch):
    dead, throttled = _spies(monkeypatch)
    _http(monkeypatch, [_limit("41"), _limit("44")], clock)
    resp = _request_over(monkeypatch, [PO_FLUX, AH_SD])
    assert resp.status_code == 200
    assert resp.get_json()["model"] == "aihorde/stable_diffusion"       # this one image only
    assert dead == []                                                    # it was 6 hours before
    assert throttled == [(PO, 60)]


def test_before_this_change_the_same_reply_shut_the_model_out(isolated_config, clock, monkeypatch):
    """The negative control: with the rate-limit reading taken away, the hub does
    what it did on the owner's machine -- one 402 and the model is marked dead."""
    dead, throttled = _spies(monkeypatch)
    monkeypatch.setattr(app, "_image_rate_limited", lambda resp: None)
    _http(monkeypatch, [_limit("41")], clock)
    resp = _request_over(monkeypatch, [PO_FLUX, AH_SD])
    assert resp.get_json()["model"] == "aihorde/stable_diffusion"
    assert dead == [(PO, "flux", 402)]


def test_a_busy_queue_costs_nothing_but_this_one_hop(isolated_config, clock, monkeypatch):
    dead, throttled = _spies(monkeypatch)
    times = _http(monkeypatch, [], clock)
    app._image_pace_next[PO] = clock.t + 200
    resp = _request_over(monkeypatch, [PO_FLUX, PO_TURBO, AH_SD])
    assert resp.status_code == 200
    assert resp.get_json()["model"] == "aihorde/stable_diffusion"
    assert times == []                                       # turbo shares the limit: not asked either
    assert dead == [] and throttled == []


def test_other_test_files_never_wait(monkeypatch):
    """conftest switches the queue off outside this file; this pins the switch
    itself: off means no slot is taken and nothing sleeps."""
    monkeypatch.setattr(app, "_image_pace_on", lambda: False)
    naps = []
    monkeypatch.setattr(app, "_image_pace_nap", naps.append)
    monkeypatch.setattr(app.requests, "get", lambda url, **kw: _ok())
    app._image_pace_next.clear()
    for _ in range(3):
        assert app._pollinations_generate_image({"api_keys": []}, "flux", "x")[0] == 200
    assert naps == [] and app._image_pace_next == {}


def test_the_pace_flag_unreadable_counts_as_off(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("config unreadable")
    monkeypatch.setattr(config, "get_flag", boom)
    assert app._image_pace_on() is False


# --------------------------------------------------------------------------- #
# 5. The page says what Auto uses
# --------------------------------------------------------------------------- #

def test_the_images_payload_carries_the_score_and_its_source(isolated_config):
    body = app.app.test_client().get("/api/images").get_json()
    by_id = {m["id"]: m for m in body["models"]}
    assert by_id["cloudflare/@cf/black-forest-labs/flux-1-schnell"]["quality"] == 742
    assert by_id["modelscope/Tongyi/Z-Image-Turbo"]["quality"] == 936
    assert by_id["pollinations/flux"]["quality"] is None
    assert by_id["aihorde/stable_diffusion"]["quality"] is None
    assert body["quality_source"]["url"] == imagerank.URL
    assert body["quality_source"]["date"] == imagerank.DATE
    assert body["priority_ignored"] is None                  # nothing saved


def test_a_saved_paid_priority_is_reported_not_silently_ignored(isolated_config, monkeypatch):
    monkeypatch.setattr(app, "_image_candidates", lambda: [PO_FLUX, AH_SD])
    client = app.app.test_client()
    saved = client.post("/api/images", json={
        "revision": 0, "priority_mode": "manual", "manual_priority": ["puter/ai-image"],
    }, headers={"X-Free-LLM-Hub": "dashboard"})
    assert saved.status_code == 200
    body = client.get("/api/images").get_json()
    assert body["priority_ignored"] == {"id": "puter/ai-image", "why": "paid",
                                        "auto_pick": "pollinations/flux"}
    assert body["effective_priority"][0] == "pollinations/flux"


def test_priority_ignored_cases():
    models = [{"id": "puter/ai-image", "free": False}, {"id": "pollinations/flux", "free": True},
              {"id": "cloudflare/x", "free": True}]
    order = ["pollinations/flux"]
    f = app._image_priority_ignored
    assert f({"priority_mode": "auto", "manual_priority": ["puter/ai-image"]}, models, order) is None
    assert f({"priority_mode": "manual", "manual_priority": ["pollinations/flux"]}, models, order) is None
    assert f({"priority_mode": "manual", "manual_priority": ["cloudflare/x"]}, models, order) == {
        "id": "cloudflare/x", "why": "unavailable", "auto_pick": "pollinations/flux"}
    assert f({"priority_mode": "manual", "manual_priority": ["puter/ai-image"]}, models, []) == {
        "id": "puter/ai-image", "why": "paid", "auto_pick": None}
    assert f(None, None, None) is None                       # never raises


def _template():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "templates", "index.html"), encoding="utf-8") as f:
        return f.read()


def test_the_page_shows_what_auto_uses_and_why_a_priority_is_ignored():
    html = _template()
    assert 'id="image-priority-note"' in html
    assert "prNote.textContent = msg" in html                # backend text never via innerHTML
    assert "is a paid model, so automatic image requests do not use it" in html
    assert "'Best available now: ' + autoPick" in html
    assert "function(models, autoSub)" in html
    assert "esc(autoSub || 'Best free model, orchestrated')" in html
    assert "quality ' +" in html and "esc(String(r.quality))" in html


def test_the_registry_states_the_free_limit_and_keeps_both_models():
    rows = {r["id"]: r for r in prov.get_provider(PO)["image_models"]}
    assert set(rows) == {"flux", "turbo"}                    # the probe: two different models
    for r in rows.values():
        assert "45 s" in r["notes"] and r.get("free", True) is True


def test_a_saved_pollen_key_never_queues(clock, monkeypatch):
    """The 45 s spacing is the anonymous limit. With a key nothing waits, and a
    limit reply is still a limit (429), not a dead model."""
    times = _http(monkeypatch, [_ok(b"a"), _ok(b"b"), _limit("9")], clock)
    cfg = {"api_keys": ["test-key-not-real"]}
    assert app._pollinations_generate_image(cfg, "flux", "x")[0] == 200
    assert app._pollinations_generate_image(cfg, "flux", "y")[0] == 200
    status, _b64, _d = app._pollinations_generate_image(cfg, "flux", "z")
    assert status == 429
    assert clock.naps == [] and times[1] == times[0] and len(times) == 3
    assert app._image_pace_next == {}
