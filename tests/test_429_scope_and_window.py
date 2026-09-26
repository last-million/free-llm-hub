"""What a 429 says -- scope (model vs account), window, reset -- and what the
hub parks because of it. Plus the stream-usage label bug.

THE BUGS THESE PIN DOWN
-----------------------
1. Per-model limits were learned from success headers for groq/cerebras only.
   A Google per-model DAILY quota (quotaId GenerateRequestsPerDayPerProject
   PerModel-FreeTier) arrives with RetryInfo retryDelay "37s", so the model was
   retried every few minutes all day. It now parks until midnight PACIFIC
   (quota.RESET_RULES) -- and only that model, its siblings keep serving.
2. A per-MINUTE 429 without Retry-After benched the KEY until the provider's
   day window reset (mark_key_exhausted's None fallback). Never again.
3. Google's quota message says "...check your plan and billing details", which
   the billing-precondition regex read as "account needs a card" (402).
4. _responses_stream (Codex) filed usage under the client's label ("auto" ->
   provider "auto", model ""), and the /v1/chat/completions passthrough filed
   no usage at all.

Every body below is a recorded SHAPE (ids/numbers illustrative); no network.
"""
import json
import time

import pytest

import app
import quota


@pytest.fixture
def fresh_quota():
    saved = (dict(quota._STATE), dict(quota._MODEL_STATE),
             dict(quota._MODEL_THROTTLE), dict(quota._DYNAMIC),
             dict(quota._MODEL_DYNAMIC), dict(quota._TOKENS),
             dict(quota._KEY_COOLDOWN),
             quota._PERSIST_PATH, quota._persist_last, quota._key_counter)
    dicts = (quota._STATE, quota._MODEL_STATE, quota._MODEL_THROTTLE,
             quota._DYNAMIC, quota._MODEL_DYNAMIC, quota._TOKENS, quota._KEY_COOLDOWN)
    for d in dicts:
        d.clear()
    quota._PERSIST_PATH = None
    quota._persist_last = 0.0
    quota._key_counter = None
    try:
        yield
    finally:
        for d, old in zip(dicts, saved[:7]):
            d.clear()
            d.update(old)
        quota._PERSIST_PATH, quota._persist_last, quota._key_counter = saved[7:]


# --------------------------------------------------------------------------- #
# Recorded shapes
# --------------------------------------------------------------------------- #

def _google(quota_id, model="gemini-2.5-pro", delay="37s", value="50"):
    return {"error": {
        "code": 429,
        "message": ("You exceeded your current quota, please check your plan and "
                    "billing details. For more information on this error, head to: "
                    "https://ai.google.dev/gemini-api/docs/rate-limits.\n"
                    "* Quota exceeded for metric: generativelanguage.googleapis.com/"
                    "generate_content_free_tier_requests, limit: %s, model: %s\n"
                    "Please retry in 37.47s." % (value, model)),
        "status": "RESOURCE_EXHAUSTED",
        "details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
             "violations": [{
                 "quotaMetric": "generativelanguage.googleapis.com/"
                                "generate_content_free_tier_requests",
                 "quotaId": quota_id,
                 "quotaDimensions": {"location": "global", "model": model},
                 "quotaValue": value}]},
            {"@type": "type.googleapis.com/google.rpc.Help",
             "links": [{"description": "Learn more about Gemini API quotas",
                        "url": "https://ai.google.dev/gemini-api/docs/rate-limits"}]},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay},
        ]}}


GOOGLE_DAY = _google("GenerateRequestsPerDayPerProjectPerModel-FreeTier")
GOOGLE_MIN = _google("GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
                     model="gemini-2.5-flash", value="10")
OPENROUTER_DAY_RESET_MS = None   # filled per test from `now`


def _openrouter_day(reset_ms):
    return {"error": {
        "message": "Rate limit exceeded: free-models-per-day. Add 10 credits to "
                   "unlock 1000 free model requests per day",
        "code": 429,
        "metadata": {"headers": {"X-RateLimit-Limit": "50",
                                 "X-RateLimit-Remaining": "0",
                                 "X-RateLimit-Reset": str(reset_ms)},
                     "provider_name": None}},
        "user_id": "user_x"}


OPENROUTER_UPSTREAM = {"error": {
    "message": "Provider returned error", "code": 429,
    "metadata": {"raw": "deepseek/deepseek-chat-v3-0324:free is temporarily "
                        "rate-limited upstream. Please retry shortly, or add your "
                        "own key to accumulate your rate limits: "
                        "https://openrouter.ai/settings/integrations",
                 "provider_name": "Chutes"}},
    "user_id": "user_x"}

GROQ_RPD = {"error": {
    "message": "Rate limit reached for model `llama-3.3-70b-versatile` in "
               "organization `org_01abc` service tier `on_demand` on requests per "
               "day (RPD): Limit 1000, Used 1000, Requested 1. Please try again in "
               "1m26.4s. Need more tokens? Upgrade to Dev Tier today at "
               "https://console.groq.com/settings/billing",
    "type": "requests", "code": "rate_limit_exceeded"}}


def _tomorrow_pacific(now):
    return quota._day_bounds_tz("America/Los_Angeles", now)[1]


# --------------------------------------------------------------------------- #
# quota.classify_429
# --------------------------------------------------------------------------- #

def test_google_per_model_day_quota_resets_at_midnight_pacific_not_in_37s():
    now = time.time()
    info = quota.classify_429("google", {}, json.dumps(GOOGLE_DAY), "gemini-2.5-pro",
                              per_model_default=True, now=now)
    assert info["source"] == "google"
    assert info["scope"] == "model" and info["window"] == "day"
    assert info["model"] == "gemini-2.5-pro"
    assert info["reset_at"] == pytest.approx(_tomorrow_pacific(now))
    assert info["seconds"] > 60, "the RetryInfo '37s' must not win on a spent day"


def test_google_openai_compat_list_wrapped_body_is_read_too():
    now = time.time()
    info = quota.classify_429("google", {}, json.dumps([GOOGLE_DAY]), now=now)
    assert (info["scope"], info["window"]) == ("model", "day")


def test_google_per_minute_quota_uses_retry_delay():
    now = time.time()
    info = quota.classify_429("google", {}, json.dumps(GOOGLE_MIN), now=now)
    assert (info["scope"], info["window"]) == ("model", "minute")
    assert info["seconds"] == pytest.approx(37, abs=1)


def test_openrouter_free_models_per_day_is_account_wide_with_its_own_reset():
    now = time.time()
    reset = now + 5 * 3600
    info = quota.classify_429("openrouter", {}, json.dumps(_openrouter_day(int(reset * 1000))),
                              "deepseek/deepseek-r1:free", now=now)
    assert info["source"] == "openrouter"
    assert (info["scope"], info["window"]) == ("account", "day")
    assert info["reset_at"] == pytest.approx(reset, abs=1)
    assert info["body_headers"]["x-ratelimit-remaining"] == "0"


def test_openrouter_upstream_rate_limit_is_one_model_for_a_minute():
    info = quota.classify_429("openrouter", {}, json.dumps(OPENROUTER_UPSTREAM))
    assert (info["scope"], info["window"]) == ("model", "minute")


def test_groq_rolling_day_bucket_trusts_its_try_again_in():
    now = time.time()
    info = quota.classify_429("groq", {"retry-after": "87"}, json.dumps(GROQ_RPD),
                              now=now)
    assert info["scope"] == "model" and info["model"] == "llama-3.3-70b-versatile"
    assert info["window"] == "day"
    assert info["seconds"] == pytest.approx(87, abs=1)


def test_a_per_minute_limit_is_never_benched_for_long_whatever_the_header():
    now = time.time()
    body = {"error": {"message": "Too many requests: limit 30 requests per minute"}}
    info = quota.classify_429("someprov", {"Retry-After": "7200"}, json.dumps(body), now=now)
    assert info["window"] == "minute"
    assert info["seconds"] <= quota._MINUTE_429_CAP


def test_retry_after_alone_is_honoured_as_is():
    now = time.time()
    info = quota.classify_429("someprov", {"Retry-After": "120"}, "slow down", now=now)
    assert info["window"] is None and info["scope"] is None
    assert info["seconds"] == pytest.approx(120, abs=1)


def test_http_date_retry_after_parses():
    from email.utils import formatdate
    now = time.time()
    info = quota.classify_429("someprov", {"Retry-After": formatdate(now + 600, usegmt=True)},
                              "", now=now)
    assert info["seconds"] == pytest.approx(600, abs=2)


@pytest.mark.parametrize("body", [None, "", "<html>429</html>", b"\xff\xfe", "[]", "{",
                                  json.dumps({"error": None}), json.dumps([1, 2])])
def test_unreadable_bodies_fail_open(body):
    info = quota.classify_429("x", None, body)
    assert info["scope"] is None and info["reset_at"] is None


def test_per_model_default_fills_only_an_unstated_scope():
    assert quota.classify_429("google", {}, "slow down",
                              per_model_default=True)["scope"] == "model"
    body = json.dumps({"error": {"message": "Your account has hit its daily limit"}})
    assert quota.classify_429("google", {}, body,
                              per_model_default=True)["scope"] == "account"


# --------------------------------------------------------------------------- #
# What gets parked
# --------------------------------------------------------------------------- #

class _Resp429:
    status_code = 429

    def __init__(self, body, headers=None):
        self.text = json.dumps(body) if not isinstance(body, str) else body
        self.headers = headers or {}

    def json(self):
        return json.loads(self.text)

    def close(self):
        pass


def test_google_day_quota_parks_only_that_model_until_pacific_midnight(fresh_quota):
    resp = _Resp429(GOOGLE_DAY)
    info = app._classify_429("google", resp, "gemini-2.5-pro")
    app._apply_429("google", "gemini-2.5-pro", info, resp)
    until = quota._MODEL_THROTTLE[("google", "gemini-2.5-pro")]["throttled_until"]
    assert until == pytest.approx(_tomorrow_pacific(time.time()), abs=5)
    assert not quota.is_model_throttled("google", "gemini-2.5-flash"), "siblings keep serving"
    assert (quota._STATE.get("google") or {}).get("throttled_until", 0) <= time.time(), \
        "the provider is NOT benched"
    assert not quota.is_exhausted("google")


def test_google_minute_quota_parks_the_model_for_minutes_not_a_day(fresh_quota):
    resp = _Resp429(GOOGLE_MIN)
    for _ in range(8):                       # a streak must not escalate past the cap
        app._apply_429("google", "gemini-2.5-flash",
                       app._classify_429("google", resp, "gemini-2.5-flash"), resp)
    until = quota._MODEL_THROTTLE[("google", "gemini-2.5-flash")]["throttled_until"]
    assert 0 < until - time.time() <= quota._MINUTE_429_CAP + 1


def test_openrouter_daily_pool_benches_the_provider(fresh_quota):
    reset = time.time() + 3 * 3600
    resp = _Resp429(_openrouter_day(int(reset * 1000)))
    app._apply_429("openrouter", "m:free", app._classify_429("openrouter", resp, "m:free"),
                   resp)
    st = quota._STATE["openrouter"]
    assert st["throttled_until"] == pytest.approx(reset, abs=5)


def test_model_scoped_429_only_briefly_skips_the_key():
    info = {"scope": "model", "window": "day", "seconds": 20 * 3600}
    assert app._key_cooldown_for_429(info, _Resp429("")) <= 60


def test_minute_429_without_retry_after_never_benches_the_key_for_the_day():
    info = {"scope": "account", "window": "minute", "seconds": None}
    assert app._key_cooldown_for_429(info, _Resp429("")) == 60


def test_google_quota_429_is_not_a_billing_precondition():
    assert not app._is_billing_precondition(_Resp429(GOOGLE_DAY))
    assert app._is_billing_precondition(_Resp429(
        {"error": {"message": "Requires a card on file to spend platform credits."}}))


def test_upstream_chat_wires_a_google_day_429_to_the_model(fresh_quota, monkeypatch):
    monkeypatch.setattr(app.config, "get_provider_config",
                        lambda pid: {"api_keys": ["k1"], "enabled": True})
    monkeypatch.setattr(app, "_next_key_start", lambda pid, n: 0)
    monkeypatch.setattr(app.requests, "post", lambda url, **kw: _Resp429(GOOGLE_DAY))
    resp = app._upstream_chat("google", {"model": "gemini-2.5-pro", "messages": []}, False)
    assert resp.status_code == 429
    assert quota.is_model_throttled("google", "gemini-2.5-pro")
    ms = quota.model_status("google", "gemini-2.5-pro")
    assert ms["resets_at"] == pytest.approx(_tomorrow_pacific(time.time()), abs=5)
    assert not quota.is_model_throttled("google", "gemini-2.5-flash")


# --------------------------------------------------------------------------- #
# Usage lands on the REAL hop
# --------------------------------------------------------------------------- #

class _StreamResp:
    headers = {}

    def close(self):
        pass


def _frames(text="Hello there.", usage=None):
    out = [b"data: " + json.dumps({"choices": [{"delta": {"content": text}}]}).encode(),
           b"data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}).encode()]
    if usage:
        out.append(b"data: " + json.dumps({"choices": [], "usage": usage}).encode())
    out.append(b"data: [DONE]")
    return out


@pytest.fixture
def usage_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(app.usage_history, "record",
                        lambda pid, model, pt, ct, estimated=False:
                        calls.append((pid, model, pt, ct, estimated)))
    return calls


def test_responses_stream_records_usage_under_the_hop_not_auto(usage_calls):
    list(app._responses_stream(_StreamResp(), "auto", line_iter=iter(_frames(
        usage={"prompt_tokens": 11, "completion_tokens": 7})),
        hop_pid="groq", hop_model="llama-3.3-70b-versatile", prompt_text="hi"))
    assert usage_calls == [("groq", "llama-3.3-70b-versatile", 11, 7, False)]


def test_responses_stream_without_hop_ids_files_no_phantom_row(usage_calls):
    list(app._responses_stream(_StreamResp(), "auto", line_iter=iter(_frames())))
    assert usage_calls == []


def test_chat_passthrough_stream_records_usage_under_the_hop(usage_calls):
    frames = [f + b"\n\n" for f in _frames(usage={"prompt_tokens": 5, "completion_tokens": 3})]
    list(app._proxy_sse(_StreamResp(), iter(frames), hop_pid="groq",
                        hop_model="llama-3.3-70b-versatile", prompt_text="hi",
                        prompt_est=9))
    assert usage_calls == [("groq", "llama-3.3-70b-versatile", 5, 3, False)]


def test_chat_passthrough_stream_estimates_without_a_usage_frame(usage_calls):
    frames = [f + b"\n\n" for f in _frames(text="x" * 40)]
    list(app._proxy_sse(_StreamResp(), iter(frames), hop_pid="groq", hop_model="m",
                        prompt_est=9))
    assert usage_calls == [("groq", "m", 9, 10, True)]


def test_anthropic_stream_still_records_under_the_hop(usage_calls):
    list(app._anthropic_stream(_StreamResp(), "claude-sonnet", 5,
                               line_iter=iter(_frames()), hop_pid="groq", hop_model="m"))
    assert usage_calls and usage_calls[0][:2] == ("groq", "m")
