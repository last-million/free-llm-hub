"""An upstream quota/billing notice is never served as the answer.

MEASURED 2026-09-27 18:51 (/agent session 47a25faa, opencode): a g4f relay
server backed by Pollinations answered HTTP 200 with its backend's billing
notice as the content, the hub served it, and the user asked why the agent
talked about Pollinations.
"""
import answer_check as ac

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


def test_billing_and_quota_notices_are_not_answers():
    for text in (POLLINATIONS, GOOGLE, CREDITS, PLAIN_NOTICE):
        r = ac.inspect(text, prompt_text="fix the zone numbers so they stay inside")
        assert not r["ok"], text
        assert "provider_notice" in r["reasons"]
        assert r["salvage"] is None                  # nothing in it is an answer
        assert ac.is_provider_notice(text, "continue")


def test_a_streamed_notice_is_never_released_early():
    assert not ac.reads_as_answer(PLAIN_NOTICE, last_prompt="continue")
    assert not ac.reads_as_answer(POLLINATIONS, last_prompt="continue")


def test_a_question_about_budgets_gets_its_answer():
    r = ac.inspect(POLLINATIONS, prompt_text="How do I raise my API key budget on Pollinations?")
    assert r["ok"], r


def test_an_answer_that_explains_rate_limits_is_fine():
    text = ("Rate limiting protects your API. In Flask, use flask-limiter: when a client "
            "goes over the limit it gets a 429 and should try again later.")
    assert ac.inspect(text, prompt_text="add rate limiting to my flask app")["ok"]
    assert ac.inspect(text, prompt_text="how should my app handle bursts?")["ok"]


def test_ordinary_answers_and_code_are_untouched():
    assert ac.inspect("The current time in UTC is 13:45.", prompt_text="time?")["ok"]
    code = ("Here is the retry wrapper:\n\n```python\n"
            "raise RuntimeError('Rate limit exceeded for this API key, try again later')\n"
            "```\n\nIt raises when the upstream says so.")
    assert ac.inspect(code, prompt_text="write a retry wrapper")["ok"]


def test_a_long_answer_mentioning_a_notice_is_an_answer():
    text = ("The deployment failed because the API key used for this request has reached "
            "its budget; raise the key budget, then try again. " +
            " ".join("Step %d checks log line %d against release %d." % (i, i * 7, i + 3)
                     for i in range(60)))
    assert len(text) > 1200
    assert ac.inspect(text, prompt_text="why did the deploy fail?")["ok"]
