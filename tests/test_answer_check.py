"""Answer-quality gate: a correct answer with junk glued on is not a success.

MEASURED LIVE (llm7/GLM-5.3-Flash): the model answered, then generated until
max_tokens, and the hub served both of these as clean 200s AND filed them as
_record_outcome(True):

    "OK出具证明的，原试题解析 做题如有雷同，纯属巧合…"
    "6510</arg_value></tool_call>6510</arg_value></tool_call>The user asked"

Streams recorded NO outcome at all at their commit points.
"""
import json
import time

import pytest

import answer_check
import app

CJK_JUNK = "OK出具证明的，原试题解析 做题如有雷同，纯属巧合…"
MARKUP_JUNK = "6510</arg_value></tool_call>6510</arg_value></tool_call>The user asked"
# A junk verdict weighs more than a plain failure (see _JUNK_FAIL_WEIGHT).
JUNK_FAIL = app._JUNK_FAIL_WEIGHT


@pytest.fixture(autouse=True)
def clean_state():
    with app._outcome_lock:
        app._outcomes.clear()
    with app._dead_lock:
        app._dead_models.clear()
    yield
    with app._outcome_lock:
        app._outcomes.clear()
    with app._dead_lock:
        app._dead_models.clear()


def _outcome(pid, model):
    with app._outcome_lock:
        rec = app._outcomes.get((pid, model)) or {}
    return rec.get("ok", 0), rec.get("fail", 0)


# --------------------------------------------------------------------------- #
# Detection: the two live samples
# --------------------------------------------------------------------------- #

def test_live_cjk_runaway_is_salvaged_to_the_answer():
    r = answer_check.inspect(CJK_JUNK, prompt_text="Reply with just the word OK.",
                             finish_reason="length")
    assert r["ok"] is False
    assert "runaway_script" in r["reasons"]
    assert r["salvage"] == "OK"


def test_live_cjk_runaway_is_caught_even_when_the_provider_says_stop():
    # glued straight onto the answer ("OK出具") is signal enough on its own
    r = answer_check.inspect(CJK_JUNK, prompt_text="Reply with just the word OK.",
                             finish_reason="stop")
    assert r["ok"] is False and r["salvage"] == "OK"


def test_live_leaked_tool_markup_is_salvaged_to_the_number():
    r = answer_check.inspect(MARKUP_JUNK, prompt_text="What is 5 + 6505? Number only.",
                             finish_reason="length")
    assert r["ok"] is False
    assert "tool_markup" in r["reasons"]
    assert r["salvage"] == "6510"


def test_repetition_loop_to_the_cap_is_salvaged():
    text = "The answer is 42." + " The answer is 42." * 30 + " The ans"
    r = answer_check.inspect(text, prompt_text="What is 6*7?", finish_reason="length")
    assert r["ok"] is False and "repetition" in r["reasons"]
    assert r["salvage"] == "The answer is 42."


def test_repeated_line_loop_is_caught():
    text = "Result: 7\n" + "I think we are done here.\n" * 5
    r = answer_check.inspect(text, prompt_text="add 3 and 4")
    assert r["ok"] is False and "repetition" in r["reasons"]
    assert r["salvage"] == "Result: 7\nI think we are done here."


def test_pure_junk_has_no_salvage():
    r = answer_check.inspect("</tool_call></tool_call>", prompt_text="hi")
    assert r["ok"] is False and r["salvage"] is None


def test_truncation_alone_is_reported_but_never_fails():
    text = "Photosynthesis converts light energy into chemical energy. In the"
    r = answer_check.inspect(text, prompt_text="Explain photosynthesis", finish_reason="length")
    assert r["ok"] is True
    assert r["reasons"] == ["truncated"]
    assert r["salvage"] is None


# --------------------------------------------------------------------------- #
# Conservative: legit answers must NEVER be flagged
# --------------------------------------------------------------------------- #

CODE_ANSWER = """Here is the fix:

```python
def pad(rows):
    out = []
    out.append(None)
    out.append(None)
    out.append(None)
    out.append(None)
    return out
# <tool_call> in a comment is fine inside code
```

Call `pad(rows)` and wrap the output in `<arg_value>` if needed.
"""

TABLE_ANSWER = """| Model | Score | Notes |
|---|---|---|
| A | N/A | pending review |
| A | N/A | pending review |
| A | N/A | pending review |
| B | 91 | fine |
"""

ESSAY = " ".join(
    "Paragraph %d discusses how the industrial revolution changed labour, "
    "cities and family life across Europe in distinct ways." % i for i in range(120))

LEGIT = [
    ("fix my function", CODE_ANSWER, None),
    ("compare the models in a table", TABLE_ANSWER, None),
    ("write a long essay on the industrial revolution", ESSAY, "length"),
    ("Explique la photosynthèse en deux phrases.",
     "La photosynthèse transforme l'énergie lumineuse en énergie chimique. "
     "Elle produit du glucose et libère de l'oxygène.", "stop"),
    ("اشرح التمثيل الضوئي باختصار",
     "التمثيل الضوئي هو عملية تحول فيها النباتات ضوء الشمس إلى طاقة كيميائية.", "stop"),
    ("用一句话解释光合作用",
     "光合作用是植物利用光能把二氧化碳和水转化为葡萄糖并释放氧气的过程。", "stop"),
    ("What does 你好 mean? 请用英文回答",
     "It means hello. 你好 literally combines 你 (you) and 好 (good), "
     "so 你好吗 means how are you.", "stop"),
    ("Translate 'good morning everyone' to Japanese",
     "Good morning everyone: 皆さん、おはようございます", "stop"),
    ("How do I say thank you in Russian?",
     "You say: спасибо большое за вашу помощь", "stop"),
    ("What is the Greek letter used for the circle constant?",
     "It is π (pi); α and β are other common Greek letters in maths.", "stop"),
    ("list some East Asian greetings",
     "Here are a few:\n- こんにちは\n- 안녕하세요\n- 你好", "stop"),
    ("Repeat 'hip hip hooray! ' three times",
     "hip hip hooray! hip hip hooray! hip hip hooray! ", "stop"),
    ("reply OK", "OK", "stop"),
    ("draw a separator", "Sure:\n\n=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=", "stop"),
    ("sing", "La la la la la la la la la la la la la la la la", "stop"),
    ("Explain the <tool_call> format Qwen uses",
     "Qwen wraps each call as <tool_call>{...}</tool_call> in the raw text.", "stop"),
    ("how do lists work",
     "- first item here\n- second item here\n- third item here", "stop"),
]


STAR_HTML = (
    "<!doctype html>\n<html>\n<body>\n<div class=\"rating\">\n"
    + "<span class=\"star\">&#9733;</span>\n" * 4
    + "</div>\n<section class=\"pricing\">Plans from 9 EUR</section>\n"
    "<footer>Contact us</footer>\n</body>\n</html>\n")

LEGIT += [
    ("build a landing page with a 4-star rating", STAR_HTML, "stop"),
    ("summarise the change",
     "Summary of changes:\n- `app.py` updated imports\n- `cli.py` updated imports\n"
     "- `api.py` updated imports\nAll done, tests pass.", "stop"),
    ("run the tests",
     "- `test_add` passed OK\n- `test_sub` passed OK\n- `test_mul` passed OK", "stop"),
    ("show three examples",
     "```py\nprint(1)\n```\nThis prints the result.\n```py\nprint(2)\n```\n"
     "This prints the result.\n```py\nprint(3)\n```\nThis prints the result.\n"
     "Done.", "stop"),
    ("write a short song",
     "Verse one goes here\nCome along with me tonight\nCome along with me tonight\n"
     "Come along with me tonight", "stop"),
]


def test_code_separated_prose_is_not_cut_after_the_first_block():
    text = LEGIT[-2][1]
    r = answer_check.inspect(text, prompt_text="show three examples", finish_reason="stop")
    assert r["ok"] is True and r["salvage"] is None


def test_mid_text_runaway_line_loop_is_still_caught():
    text = "Answer: 7\n" + "I will now repeat myself again.\n" * 9 + "Bye then, all done."
    r = answer_check.inspect(text, prompt_text="add 3 and 4", finish_reason="stop")
    assert r["ok"] is False and r["salvage"] == "Answer: 7\nI will now repeat myself again."


@pytest.mark.parametrize("prompt,text,fin", LEGIT)
def test_legit_answers_pass(prompt, text, fin):
    r = answer_check.inspect(text, prompt_text=prompt, finish_reason=fin)
    assert r["ok"] is True, (prompt, r)


def test_tool_markup_is_left_to_the_tool_path_when_tools_are_offered():
    r = answer_check.inspect(MARKUP_JUNK, prompt_text="run ls", tools_offered=True)
    assert "tool_markup" not in r["reasons"]


def test_script_check_needs_a_prompt_to_compare_against():
    assert answer_check.inspect(CJK_JUNK, prompt_text=None, finish_reason="length")["ok"] is True


@pytest.mark.parametrize("text", [None, "", "   ", 42])
def test_degenerate_inputs_never_raise(text):
    assert answer_check.inspect(text)["ok"] is True


def _best_ms(fn, reps=10, rounds=5):
    """Fastest per-call time over several rounds: a round that a busy CPU
    (the rest of the suite, the live hub) preempted does not count."""
    best = float("inf")
    for _ in range(rounds):
        t0 = time.perf_counter()
        for _ in range(reps):
            fn()
        best = min(best, (time.perf_counter() - t0) / reps * 1000)
    return best


def _reference_work():
    return sum(i * i for i in range(5000))


def test_fast_on_20kb():
    """Budget ~2 ms per 20 KB answer. Unreliable before: ONE 20-call sample
    against a flat 8 ms bound, so a burst of load from the suite itself
    failed it with nothing slow in the code. Now best-of-N, and the bound
    scales with a pure-Python reference timed the same way (unloaded ~0.27 ms,
    slowest sample ~10x that): a CPU running slow slows both, a real
    regression in inspect() moves only one."""
    samples = [ESSAY[:20000], ("x = foo(bar)\n" * 1600)[:20000], "a" * 20000,
               "Sure: " + "中文" * 10000, CODE_ANSWER * 40]
    for s in samples:
        answer_check.inspect(s, prompt_text="write", finish_reason="length")  # warm
        per_call_ms = _best_ms(lambda: answer_check.inspect(
            s, prompt_text="write", finish_reason="length"))
        ref_ms = _best_ms(_reference_work)
        limit = max(8.0, 30.0 * ref_ms)
        assert per_call_ms < limit, (s[:20], per_call_ms, ref_ms)


# --------------------------------------------------------------------------- #
# _answer_gate: the hub-side policy on an OpenAI chat JSON
# --------------------------------------------------------------------------- #

def _chat(content, fin="length"):
    return {"choices": [{"index": 0, "finish_reason": fin,
                         "message": {"role": "assistant", "content": content}}]}


PAYLOAD = {"messages": [{"role": "system", "content": "be brief"},
                        {"role": "user", "content": "Reply with just the word OK."}]}


def test_gate_salvages_in_place():
    data = _chat(CJK_JUNK)
    assert app._answer_gate(data, PAYLOAD, False) == "salvaged"
    assert data["choices"][0]["message"]["content"] == "OK"
    assert data["choices"][0]["finish_reason"] == "stop"


def test_gate_junk_and_ok():
    assert app._answer_gate(_chat("</tool_call></tool_call>"), PAYLOAD, False) == "junk"
    assert app._answer_gate(_chat("OK", "stop"), PAYLOAD, False) == "ok"
    real_tool = {"choices": [{"message": {"content": MARKUP_JUNK,
                                          "tool_calls": [{"function": {"name": "x"}}]}}]}
    assert app._answer_gate(real_tool, PAYLOAD, True) == "ok"
    assert app._answer_gate("not a dict", PAYLOAD, False) == "ok"


def test_prompt_text_uses_user_turns_only():
    p = {"messages": [{"role": "system", "content": "系统"},
                      {"role": "user", "content": [{"type": "text", "text": "hello"}]},
                      {"role": "assistant", "content": "hi"},
                      {"role": "user", "content": "again"}]}
    assert app._prompt_text_for_check(p) == "hello\nagain"


# --------------------------------------------------------------------------- #
# Wiring: non-stream endpoints
# --------------------------------------------------------------------------- #

class _R:
    status_code = 200
    headers = {}
    text = ""

    def __init__(self, content, fin="length"):
        self._c = content
        self._fin = fin

    def json(self):
        return {"choices": [{"index": 0, "finish_reason": self._fin,
                             "message": {"role": "assistant", "content": self._c}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 900}}

    def close(self):
        pass


BAD = ("llm7", "GLM-5.3-Flash")
GOOD = ("groq", "llama-3.3-70b-versatile")


def _wire(monkeypatch, bad_content):
    calls = []

    def fake_dispatch(pid, payload, stream):
        calls.append(pid)
        return _R(bad_content) if pid == BAD[0] else _R("REAL ANSWER", "stop")

    monkeypatch.setattr(app, "_dispatch_chat", fake_dispatch)
    monkeypatch.setattr(app, "_build_chain", lambda *a, **k: [BAD, GOOD])
    monkeypatch.setattr(app, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(app, "_resolve_model", lambda m: BAD)
    return calls


def _post_chat(prompt):
    return app.app.test_client().post("/v1/chat/completions", json={
        "model": "auto", "max_tokens": 64, "stream": False,
        "messages": [{"role": "user", "content": prompt}]})


def test_chat_serves_the_salvage_and_records_a_failure(monkeypatch):
    calls = _wire(monkeypatch, CJK_JUNK)
    r = _post_chat("Reply with just the word OK.")
    assert r.status_code == 200
    body = r.get_json()
    assert body["choices"][0]["message"]["content"] == "OK"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert calls == ["llm7"], "a salvageable answer must not cost another hop"
    assert _outcome(*BAD) == (0, JUNK_FAIL), "a degenerating hop must not be promoted"
    assert app._is_model_dead(*BAD) is False, "a heuristic never bans a model"


def test_chat_pure_junk_falls_through_to_the_next_hop(monkeypatch):
    calls = _wire(monkeypatch, "</tool_call></arg_value></tool_call>")
    r = _post_chat("What is 5 + 6505?")
    assert r.status_code == 200
    assert r.get_json()["choices"][0]["message"]["content"] == "REAL ANSWER"
    assert calls == ["llm7", "groq"]
    assert _outcome(*BAD) == (0, JUNK_FAIL)
    assert _outcome(*GOOD) == (1, 0)


def test_chat_clean_answer_is_untouched(monkeypatch):
    _wire(monkeypatch, "Plain answer.")
    r = _post_chat("say something")
    assert r.get_json()["choices"][0]["message"]["content"] == "Plain answer."
    assert _outcome(*BAD) == (1, 0)


def test_responses_serves_the_salvage(monkeypatch):
    _wire(monkeypatch, MARKUP_JUNK)
    r = app.app.test_client().post("/v1/responses", json={
        "model": "auto", "stream": False, "input": "What is 5 + 6505? Number only."})
    assert r.status_code == 200
    blob = json.dumps(r.get_json())
    assert "6510" in blob and "tool_call" not in blob
    assert _outcome(*BAD) == (0, JUNK_FAIL)


def test_messages_pure_junk_falls_through(monkeypatch):
    calls = _wire(monkeypatch, "</tool_call></tool_call>")
    r = app.app.test_client().post("/v1/messages", json={
        "model": "auto", "max_tokens": 64, "stream": False,
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert "REAL ANSWER" in json.dumps(r.get_json())
    assert calls[:2] == ["llm7", "groq"]
    assert _outcome(*BAD)[1] == JUNK_FAIL


# --------------------------------------------------------------------------- #
# Wiring: streams now record an outcome when they END
# --------------------------------------------------------------------------- #

class _StreamResp:
    headers = {}

    def close(self):
        pass


def _sse(*contents, fin="stop"):
    lines = [b"data: " + json.dumps({"choices": [{"delta": {"content": c}}]}).encode()
             for c in contents]
    lines.append(b"data: " + json.dumps(
        {"choices": [{"delta": {}, "finish_reason": fin}]}).encode())
    lines.append(b"data: [DONE]")
    return lines


def test_chat_passthrough_stream_records_failure_for_junk():
    frames = [ln + b"\n\n" for ln in _sse("OK", "出具证明的，原试题解析 做题如有雷同，纯属巧合", fin="length")]
    out = b"".join(app._proxy_sse(_StreamResp(), iter(frames), hop_pid=BAD[0],
                                  hop_model=BAD[1], prompt_text="Reply with just OK"))
    assert b"\\u51fa\\u5177" in out, "streamed bytes are never un-sent"
    assert _outcome(*BAD) == (0, JUNK_FAIL)


def test_chat_passthrough_stream_records_success_for_clean():
    frames = [ln + b"\n\n" for ln in _sse("Hello ", "there.")]
    list(app._proxy_sse(_StreamResp(), iter(frames), hop_pid=GOOD[0],
                        hop_model=GOOD[1], prompt_text="greet me"))
    assert _outcome(*GOOD) == (1, 0)


def test_responses_stream_records_outcome():
    list(app._responses_stream(_StreamResp(), "auto", line_iter=iter(_sse(MARKUP_JUNK, fin="length")),
                               hop_pid=BAD[0], hop_model=BAD[1], prompt_text="5 + 6505?"))
    assert _outcome(*BAD) == (0, JUNK_FAIL)
    list(app._responses_stream(_StreamResp(), "auto", line_iter=iter(_sse("Fine.")),
                               hop_pid=GOOD[0], hop_model=GOOD[1], prompt_text="hi"))
    assert _outcome(*GOOD) == (1, 0)


def test_anthropic_stream_records_outcome():
    list(app._anthropic_stream(_StreamResp(), "claude", 5,
                               line_iter=iter(_sse("OK", "出具证明的，原试题解析 做题如有雷同", fin="length")),
                               hop_pid=BAD[0], hop_model=BAD[1], prompt_text="say OK"))
    assert _outcome(*BAD) == (0, JUNK_FAIL)
    list(app._anthropic_stream(_StreamResp(), "claude", 5, line_iter=iter(_sse("Fine.")),
                               hop_pid=GOOD[0], hop_model=GOOD[1], prompt_text="hi"))
    assert _outcome(*GOOD) == (1, 0)


def test_stream_without_hop_ids_records_nothing():
    list(app._responses_stream(_StreamResp(), "auto", line_iter=iter(_sse("x"))))
    with app._outcome_lock:
        assert not app._outcomes
