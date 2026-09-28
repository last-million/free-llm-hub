"""An upstream quota/billing notice relayed as content is never the answer.

MEASURED 2026-09-27 18:51 (/agent session 47a25faa, opencode): a g4f relay
server backed by Pollinations answered HTTP 200 with its backend's billing
notice as the content, the hub served it, and the user asked why the agent
talked about Pollinations. Direct providers report quota as an HTTP error,
so the check runs on RELAY hops only -- a real answer explaining the user's
own API quota ("Your OpenAI API key has exceeded its quota...") must pass.
"""
import answer_check as ac
import app

POLLINATIONS = ("The API key used for this request has reached its budget. Please "
                "[raise the key budget](https://enter.pollinations.ai/edit-key?id=FAKEID0000"
                "&ref=agent_key_budget), then try again.\n\nTopping up the wallet adds "
                "credits to every key.")
GOOGLE = ("You exceeded your current quota, please check your plan and billing details. "
          "For more information on this error, head to: "
          "https://ai.google.dev/gemini-api/docs/rate-limits.")
CREDITS = "Insufficient credits. Add more at https://openrouter.ai/settings/credits and retry."
PLAIN_NOTICE = ("Rate limit exceeded for this API key: too many requests in the last minute, "
                "please try again later.")
OWN_QUOTA = ("Your OpenAI API key has exceeded its quota. Check your plan and billing at "
             "https://platform.openai.com/account/billing, then retry.")


def test_billing_and_quota_notices_from_a_relay_are_not_answers():
    for text in (POLLINATIONS, GOOGLE, CREDITS, PLAIN_NOTICE):
        r = ac.inspect(text, prompt_text="fix the zone numbers so they stay inside", relay=True)
        assert not r["ok"], text
        assert "provider_notice" in r["reasons"]
        assert r["salvage"] is None                  # nothing in it is an answer
        assert ac.is_provider_notice(text, "continue")


def test_a_direct_provider_answer_is_never_judged_a_notice():
    for text in (POLLINATIONS, OWN_QUOTA):
        assert ac.inspect(text, prompt_text="why does my app fail?")["ok"]
    assert ac.reads_as_answer(PLAIN_NOTICE, last_prompt="continue")


def test_a_streamed_notice_from_a_relay_is_never_released_early():
    assert not ac.reads_as_answer(PLAIN_NOTICE, last_prompt="continue", relay=True)
    assert not ac.reads_as_answer(POLLINATIONS, last_prompt="continue", relay=True)


def test_a_question_about_budgets_gets_its_answer():
    r = ac.inspect(POLLINATIONS, prompt_text="How do I raise my API key budget on Pollinations?",
                   relay=True)
    assert r["ok"], r


def test_an_answer_that_explains_rate_limits_is_fine():
    text = ("Rate limiting protects your API. In Flask, use flask-limiter: when a client "
            "goes over the limit it gets a 429 and should try again later.")
    for prompt in ("add rate limiting to my flask app", "how should my app handle bursts?"):
        assert ac.inspect(text, prompt_text=prompt, relay=True)["ok"]


def test_ordinary_answers_and_code_are_untouched():
    assert ac.inspect("The current time in UTC is 13:45.", prompt_text="time?", relay=True)["ok"]
    code = ("Here is the retry wrapper:\n\n```python\n"
            "raise RuntimeError('Rate limit exceeded for this API key, try again later')\n"
            "```\n\nIt raises when the upstream says so.")
    assert ac.inspect(code, prompt_text="write a retry wrapper", relay=True)["ok"]


def test_a_long_answer_mentioning_a_notice_is_an_answer():
    text = ("The deployment failed because the API key used for this request has reached "
            "its budget; raise the key budget, then try again. " +
            " ".join("Step %d checks log line %d against release %d." % (i, i * 7, i + 3)
                     for i in range(60)))
    assert len(text) > 1200
    assert ac.inspect(text, prompt_text="why did the deploy fail?", relay=True)["ok"]


def test_the_hub_gate_judges_relay_hops_only():
    def data():
        return {"choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": POLLINATIONS}}]}
    payload = {"messages": [{"role": "user", "content": "fix the zone numbers"}]}
    assert app._answer_gate(data(), payload, False,
                            hop=("g4f", "srv_mp5:community/kimi-k3-free")) == "junk"
    assert app._answer_gate(data(), payload, False, hop=("groq", "qwen/qwen3.8-27b")) == "ok"
    assert app._answer_gate(data(), payload, False) == "ok"
