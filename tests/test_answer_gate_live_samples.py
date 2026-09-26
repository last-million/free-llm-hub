"""Second batch of live answer-gate misses (llm7/GLM-5.3-Flash, non-stream
/v1/chat/completions, prompt "What is N plus 1? Answer with only the number.",
max_tokens 40). All four went out as clean 200s and were filed as successes:

    "2826HAMSTER-2826282628262826282628262826"   glued short-unit repetition
    "1573 1573 ------------------------------"   repeat + separator run
    "5499 картинка внутри Thinking content: 5"   Cyrillic switch + reasoning marker
    '8446"Sfc cover" is an additional paramet'   same-script runaway after the answer

WIRING (verified here end to end, through the real router, chain loop and
_dispatch_chat, with only _upstream_chat faked): the gate IS reached on every
non-stream branch -- pinned model, auto, best (quality_mode), a fallback hop and
a response-cache round trip. The misses were the detectors, not the wiring.
"""
import pytest

import answer_check
import app

N_PROMPT = "What is %d plus 1? Answer with only the number."

# (reply, prompt, expected salvage)
LIVE = [
    ("2826HAMSTER-2826282628262826282628262826", N_PROMPT % 2825, "2826"),
    ("1573 1573 ------------------------------", N_PROMPT % 1572, "1573"),
    ("5499 картинка внутри Thinking content: 5", N_PROMPT % 5498, "5499"),
    ('8446"Sfc cover" is an additional paramet', N_PROMPT % 8445, "8446"),
    # the first batch must keep working
    ("OK出具证明的，原试题解析 做题如有雷同，纯属巧合…", "Reply with just the word OK.", "OK"),
    ("6510</arg_value></tool_call>6510</arg_value></tool_call>The user asked",
     N_PROMPT % 6509, "6510"),
]
LIVE_IDS = ["glued-repeat", "separator-run", "cyrillic-thinking", "same-script",
            "cjk", "tool-markup"]

BAD = ("llm7", "GLM-5.3-Flash")
GOOD = ("groq", "llama-3.3-70b-versatile")


@pytest.fixture(autouse=True)
def clean_state():
    def wipe():
        with app._outcome_lock:
            app._outcomes.clear()
        with app._dead_lock:
            app._dead_models.clear()
    wipe()
    yield
    wipe()


def _outcome(pid, model):
    with app._outcome_lock:
        rec = app._outcomes.get((pid, model)) or {}
    return rec.get("ok", 0), rec.get("fail", 0)


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("fin", ["length", "stop"])
@pytest.mark.parametrize("reply,prompt,expected", LIVE, ids=LIVE_IDS)
def test_live_samples_are_salvaged_to_the_answer(reply, prompt, expected, fin):
    r = answer_check.inspect(reply, prompt_text=prompt, finish_reason=fin)
    assert r["ok"] is False, r
    assert r["salvage"] == expected, r


def test_glued_repetition_alone_at_the_cap():
    # unconstrained prompt: the glued-loop detector must catch it by itself
    r = answer_check.inspect("2826HAMSTER-2826282628262826282628262826",
                             prompt_text="What is 2825 plus 1?", finish_reason="length")
    assert r["ok"] is False and "repetition" in r["reasons"]
    assert r["salvage"] == "2826"


def test_glued_repetition_from_the_first_char_keeps_one_copy():
    r = answer_check.inspect("2826" * 9 + "28", prompt_text="What is 2825 plus 1?",
                             finish_reason="length")
    assert r["ok"] is False and r["salvage"] == "2826"


def test_glued_repetition_after_prose_cuts_at_the_run():
    r = answer_check.inspect("The sum is 42. 4242424242424242",
                             prompt_text="add 40 and 2", finish_reason="length")
    assert r["ok"] is False and r["salvage"] == "The sum is 42."


def test_separator_run_alone_at_the_cap():
    r = answer_check.inspect("1573 1573 ------------------------------",
                             prompt_text="What is 1572 plus 1?", finish_reason="length")
    assert r["ok"] is False and "separator_run" in r["reasons"]
    assert r["salvage"] == "1573"


def test_separator_run_after_a_repeated_answer_even_without_the_cap():
    r = answer_check.inspect("1573 1573 ------------------------------",
                             prompt_text="What is 1572 plus 1?", finish_reason="stop")
    assert r["ok"] is False and r["salvage"] == "1573"


def test_reasoning_marker_alone():
    r = answer_check.inspect("The capital is Paris. Thinking content: the user wants",
                             prompt_text="What is the capital of France?",
                             finish_reason="length")
    assert r["ok"] is False and "reasoning_leak" in r["reasons"]
    assert r["salvage"] == "The capital is Paris."


@pytest.mark.parametrize("reply,expected", [
    ("42<think>the user asked for", "42"),
    ("42\n</think>\nThe user asked for a number", "42"),
    ("42 Thought: I should double check", "42"),
    ("42<|im_end|>\n<|im_start|>user\nthanks", "42"),
])
def test_other_reasoning_and_template_leaks(reply, expected):
    r = answer_check.inspect(reply, prompt_text="What is 6*7?", finish_reason="stop")
    assert r["ok"] is False and r["salvage"] == expected, r


@pytest.mark.parametrize("reply,expected", [
    ("5499 картинка внутри Thinking content: 5", "5499"),
    ("5499 مرحبا بكم في الموقع الرسمي", "5499"),
    ("5499 שלום עולם ומלואו כאן", "5499"),
    ("5499 καλημέρα σας φίλοι μου", "5499"),
    ("5499 नमस्ते दुनिया आप कैसे हैं", "5499"),
    ("5499 出具证明的原试题解析", "5499"),
])
def test_script_switch_after_a_short_answer_any_script_even_on_stop(reply, expected):
    r = answer_check.inspect(reply, prompt_text="What is 5498 plus 1?", finish_reason="stop")
    assert r["ok"] is False and "runaway_script" in r["reasons"], r
    assert r["salvage"] == expected


@pytest.mark.parametrize("prompt,reply,expected", [
    ("What is 2+2? Answer with only the number.", "4. Let me know if you need more!", "4"),
    ("Capital of France? Reply with just the word.", "Paris\n\nParis is the capital.", "Paris"),
    ("Is water wet? Answer only yes or no.", "Yes, because it is a liquid.", "Yes"),
    ("How many legs does a spider have? Number only.", "8 legs in total.", "8"),
    ("Reply with exactly PONG", "PONG and some more words", "PONG"),
    ("Describe the sky in one word.", "Blue - vast and open.", "Blue"),
    ("Quel est 2+2 ? Réponds uniquement par le nombre.", "4 est la réponse", "4"),
])
def test_prompt_constrained_brevity_salvages_the_leading_answer(prompt, reply, expected):
    r = answer_check.inspect(reply, prompt_text=prompt, finish_reason="stop")
    assert r["ok"] is False and "extra_text" in r["reasons"], r
    assert r["salvage"] == expected


# --------------------------------------------------------------------------- #
# Conservative: none of these may be flagged
# --------------------------------------------------------------------------- #

LONG_WITH_RULES = (
    "## Part one\n\nThe first section explains the setup in plain words.\n\n"
    "--------\n\n## Part two\n\nThe second section covers the details.\n\n"
    "---------------\n\nThat is all there is to it, with a final remark.")

NEGATIVES = [
    # unconstrained answers keep their wording
    ("What is 6*7?", "Answer: 42", "stop"),
    ("What is 6*7?", "42. Let me know if you need anything else!", "stop"),
    ("What is 6*7?", "The answer is 42 because six sevens are forty-two.", "stop"),
    # markdown rules / setext / tables
    ("write two sections", LONG_WITH_RULES, "stop"),
    ("write two sections", LONG_WITH_RULES + "\n\n--------\n\nAnd then the next", "length"),
    ("make a heading", "Title\n==========\n\nBody text here.", "stop"),
    ("compare in a table", "| a | b |\n|------------|------------|\n| 1 | 2 |", "length"),
    ("draw a separator", "Sure:\n\n=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=", "stop"),
    # repetition that is content
    ("What is 1/11 as a decimal?", "1/11 = 0.0909090909090909", "stop"),
    ("What is 1/11 as a decimal?", "1/11 = 0.0909090909090909", "length"),
    ("tell me a joke", "Why did the chicken cross the road? hahahahahahahaha", "stop"),
    ("show a CAG repeat", "A CAG repeat looks like CAGCAGCAGCAGCAGCAG", "stop"),
    ("what is 0xFF in binary?", "0b11111111 or 0b1010101010101010 style", "stop"),
    # legit other-language answers
    ("Сколько будет два плюс два?", "Два плюс два равно четырём.", "stop"),
    ("Сколько будет два плюс два? Ответь только числом.", "4 — это четыре, всё просто.", "stop"),
    ("How do I say thank you in Russian?", "Спасибо (spasibo) is the usual word.", "stop"),
    ("What is the Moscow metro called locally?", "It's called Метрополитен in Moscow", "stop"),
    ("What does 你好 mean? 请用英文回答", "It means hello. 你好 is a greeting.", "stop"),
    ("What is the area of a circle?", "A = πr², where π ≈ 3.14 and r is the radius", "stop"),
    ("say hello in some languages", "Hello Привет Hola Bonjour Ciao", "stop"),
    # reasoning blocks the provider leads with / prompts that ask about them
    ("What is 6*7?", "<think>six sevens</think>\n\n42", "stop"),
    ("Explain the <think> tag", "Models wrap reasoning in <think> tags, then answer.", "stop"),
    ("show a ReAct trace", "Here is one:\nThought: I need the weather\nAction: search", "stop"),
    # constrained prompts whose reply cannot be trimmed safely
    ("What is 2+2? Answer with only the number.", "2 + 2 = 4", "stop"),
    ("What is 2+2? Answer with only the number.", "4", "stop"),
    ("What is 2+2? Answer with only the number.", "4.", "stop"),
    ("What is 2+2? Answer with only the number.", "**4**", "stop"),
    ("What is 255 in hex? Answer with only the number.", "0xFF is 255", "stop"),
    ("Write 1573 in French style. Answer with only the number.", "1 573", "stop"),
    ("Avogadro's number? Answer with only the number.", "6.02e23 per mole", "stop"),
    ("Answer with only the number, then explain why: 2+2?", "4 because 2 and 2 make 4", "stop"),
    ("Answer with only the number: 2+2?", "The answer is 4.", "stop"),
    ("Which city? Reply with just the name.", "New York City", "stop"),
]


@pytest.mark.parametrize("prompt,text,fin", NEGATIVES)
def test_negatives_pass(prompt, text, fin):
    r = answer_check.inspect(text, prompt_text=prompt, finish_reason=fin)
    assert r["ok"] is True, (prompt, text, r)


def test_constraint_is_read_from_the_last_user_turn_only():
    payload = {"messages": [
        {"role": "user", "content": "What is 2+2? Answer with only the number."},
        {"role": "assistant", "content": "4"},
        {"role": "user", "content": "Now describe it in a full sentence."}]}
    data = {"choices": [{"index": 0, "finish_reason": "stop", "message": {
        "role": "assistant", "content": "4 is the sum because two pairs make four."}}]}
    assert app._answer_gate(data, payload, False) == "ok"
    assert data["choices"][0]["message"]["content"].startswith("4 is the sum")


def test_constraint_is_skipped_when_tools_are_offered():
    r = answer_check.inspect("4. Done.", prompt_text="What is 2+2? Answer with only the number.",
                             tools_offered=True, finish_reason="stop")
    assert "extra_text" not in r["reasons"]


# --------------------------------------------------------------------------- #
# Wiring: /v1/chat/completions, non-stream, only the HTTP upstream faked
# --------------------------------------------------------------------------- #

class _R:
    headers = {}
    text = ""

    def __init__(self, content, fin="length", status=200):
        self._c = content
        self._fin = fin
        self.status_code = status

    def json(self):
        return {"choices": [{"index": 0, "finish_reason": self._fin,
                             "message": {"role": "assistant", "content": self._c}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 40}}

    def close(self):
        pass


def _wire(monkeypatch, bad_content, chain=(BAD,), bad_status=None):
    calls = []

    def fake_upstream(pid, payload, stream, *a, **k):
        calls.append((pid, payload.get("model"), stream))
        if pid == "failhop":
            return _R("", status=429)
        if pid == BAD[0]:
            return _R(bad_content)
        return _R("REAL ANSWER", "stop")

    monkeypatch.setattr(app, "_upstream_chat", fake_upstream)
    monkeypatch.setattr(app, "_build_chain", lambda *a, **k: list(chain))
    monkeypatch.setattr(app, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(app, "_resolve_model", lambda m: BAD)
    monkeypatch.setattr(app, "_model_block_reason", lambda pid, m: None)
    return calls


def _post(model, prompt, **extra):
    return app.app.test_client().post("/v1/chat/completions", json=dict({
        "model": model, "max_tokens": 40, "stream": False,
        "messages": [{"role": "user", "content": prompt}]}, **extra))


@pytest.mark.parametrize("model", ["llm7/GLM-5.3-Flash", "auto", "best"])
@pytest.mark.parametrize("reply,prompt,expected", LIVE, ids=LIVE_IDS)
def test_chat_endpoint_serves_the_salvage(monkeypatch, model, reply, prompt, expected):
    calls = _wire(monkeypatch, reply)
    r = _post(model, prompt)
    assert r.status_code == 200
    body = r.get_json()
    assert body["choices"][0]["message"]["content"] == expected
    assert body["choices"][0]["finish_reason"] == "stop"
    assert [c[0] for c in calls] == ["llm7"], "a salvageable answer costs no extra hop"
    assert _outcome(*BAD) == (0, 1), "a salvaged answer is a FAILED delivery"
    assert app._reliability(*BAD) < 0.5
    assert app._reliability_penalty(*BAD) > 0


def test_chat_endpoint_rejected_answer_lowers_reliability(monkeypatch):
    calls = _wire(monkeypatch, "</tool_call></arg_value></tool_call>", chain=(BAD, GOOD))
    r = _post("auto", N_PROMPT % 2825)
    assert r.get_json()["choices"][0]["message"]["content"] == "REAL ANSWER"
    assert [c[0] for c in calls] == ["llm7", "groq"]
    assert _outcome(*BAD) == (0, 1) and app._reliability(*BAD) < 0.5
    assert _outcome(*GOOD) == (1, 0)


def test_chat_endpoint_gates_a_fallback_hop(monkeypatch):
    calls = _wire(monkeypatch, LIVE[0][0], chain=(("failhop", "x"), BAD))
    r = _post("auto", LIVE[0][1])
    assert r.get_json()["choices"][0]["message"]["content"] == "2826"
    assert [c[0] for c in calls] == ["failhop", "llm7"]


def test_chat_endpoint_gate_runs_before_the_response_cache(monkeypatch):
    _wire(monkeypatch, LIVE[1][0])
    real_flag = app.config.get_flag
    monkeypatch.setattr(app.config, "get_flag",
                        lambda name, default=False: True if name == "response_cache"
                        else real_flag(name, default))
    store = {}
    monkeypatch.setattr(app.respcache, "get", lambda body, ttl=None: store.get("hit"))
    monkeypatch.setattr(app.respcache, "put", lambda body, data: store.__setitem__("hit", data))
    first = _post("auto", LIVE[1][1])
    assert first.get_json()["choices"][0]["message"]["content"] == "1573"
    second = _post("auto", LIVE[1][1])
    assert second.headers.get("X-Free-LLM-Hub-Cache") == "hit"
    assert second.get_json()["choices"][0]["message"]["content"] == "1573"
