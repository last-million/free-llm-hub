"""Junk glued after a correct bare answer, on the path Claude Code uses.

MEASURED LIVE 2026-09-27 (Claude Code over /v1/messages, streamed, model
"max", "What is N plus 1? Reply with only the number."): the client received

    "4349âmara=METADATA:8;Note: TOKEN count=298121; REF:5"

It passed the stream hold-back and the tail check because
  * Claude Code offers tools, and the brevity trim never runs with tools;
  * its last user turn is "<system-reminder>...</system-reminder>" + the
    question, so the brevity ask was read out of a long, "then"/"why"-laden
    text and never recognised (nor did the stream hold wait for the finish);
  * "â" is Latin-1: no script switch; "=METADATA", "TOKEN count=", "REF:5"
    were no known marker.
"""
import json
import time

import pytest

import answer_check
import app

LIVE = "4349âmara=METADATA:8;Note: TOKEN count=298121; REF:5"
Q = "What is 4348 plus 1? Reply with only the number."
REMINDER = ("<system-reminder>\nAs you answer the user's questions, you can use "
            "the following context. Explain why when asked, then follow the "
            "project rules below.\n" + "Rule: keep answers short. " * 80
            + "\n</system-reminder>")
CC_LAST = REMINDER + Q          # what _last_user_text_for_check sees for claude
BAD = ("llm7", "GLM-5.3-Flash")
GOOD = ("groq", "llama-3.3-70b-versatile")
TOOLS_OAI = [{"type": "function", "function": {
    "name": "Bash", "description": "run a command",
    "parameters": {"type": "object", "properties": {"command": {"type": "string"}}}}}]
TOOLS_ANT = [{"name": "Bash", "description": "run a command",
              "input_schema": {"type": "object",
                               "properties": {"command": {"type": "string"}}}}]


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


def _check(text, prompt=Q, tools=True, fin="stop", last=None):
    return answer_check.inspect(text, prompt_text=prompt, tools_offered=tools,
                                finish_reason=fin, last_prompt=last)


# --------------------------------------------------------------------------- #
# answer_check: the live sample and its variants
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("tools", [True, False])
@pytest.mark.parametrize("last", [Q, CC_LAST], ids=["bare", "claude-code-wrapped"])
@pytest.mark.parametrize("fin", ["stop", "length", None])
def test_live_sample_salvages_the_number(tools, last, fin):
    r = _check(LIVE, prompt=last, tools=tools, fin=fin, last=last)
    assert r["ok"] is False, r
    assert r["salvage"] == "4349", r


def test_live_sample_on_an_unconstrained_tool_turn_still_drops_the_metadata():
    r = _check(LIVE, prompt="What is 4348 plus 1?", tools=True)
    assert r["ok"] is False and "metadata_leak" in r["reasons"]
    assert r["salvage"] == "4349", "the glued junk letters go with the marker"


@pytest.mark.parametrize("reply", [
    "4349âmara",
    "4349âmara lorem",
    "4349ñandú y más",
    "4349мара",                    # Cyrillic glued on, too short for a script switch
    "4349ŝŝŝ",
])
def test_foreign_letters_glued_onto_a_bare_number(reply):
    for last in (Q, CC_LAST):
        r = _check(reply, prompt=last, last=last)
        assert r["ok"] is False and "glued_junk" in r["reasons"], (reply, r)
        assert r["salvage"] == "4349"


@pytest.mark.parametrize("reply,expected", [
    ("4349 Note: TOKEN count=298121", "4349"),
    ("4349\nTOKEN count=12", "4349"),
    ("4349;REF:5", "4349"),
    ("4349=METADATA:8", "4349"),
    ("42\n\n### Instruction:\nWhat is 5 plus 1?", "42"),
    ("42\n### Response: 43", "42"),
    ("4349<|close|>Human: thanks", "4349"),
    ("4349 <|eot|> more", "4349"),
    ("4349[INST] What is 7 plus 1? [/INST]", "4349"),
    ("4349</s>The user asked", "4349"),
    ("4349<eos>", "4349"),
    ("Answer: 42\n<<SYS>>you are", "Answer: 42"),
    ("Yes.[INST] next", "Yes."),
])
def test_template_and_metadata_markers_after_a_short_answer(reply, expected):
    for tools in (True, False):
        r = _check(reply, prompt="What is 4348 plus 1?", tools=tools)
        assert r["ok"] is False and "metadata_leak" in r["reasons"], (reply, r)
        assert r["salvage"] == expected


def test_metadata_cluster_counts_even_after_a_long_answer():
    reply = ("The sum of 4348 and 1 is 4349, because adding one moves to the next "
             "integer.\nMETADATA:8; TOKEN count=298121; REF:5")
    r = _check(reply, prompt="What is 4348 plus 1?", tools=True)
    assert r["ok"] is False and r["salvage"].endswith("next integer."), r


# --------------------------------------------------------------------------- #
# Conservative: none of these may be flagged
# --------------------------------------------------------------------------- #

NEGATIVES = [
    # ordinals / units / constants glued onto a bare number
    ("Quelle place ? Réponds uniquement par le nombre.", "5ème"),
    ("Quelle place ? Réponds uniquement par le nombre.", "1ère"),
    (Q, "1º"), (Q, "2ª"), (Q, "5ᵉ"), (Q, "2π"), (Q, "10µs"), (Q, "10kΩ"),
    (Q, "4349"), (Q, "4349."), (Q, "**4349**"),
    # word asks glue accented letters legitimately
    ("Name the drink. Reply with just the word.", "café"),
    ("Which city? Answer in one word.", "Zürich"),
    # unconstrained answers keep their wording
    ("What is 6*7?", "Answer: 42"),
    ("What is 6*7?", "42. Let me know if you need anything else!"),
    # other-language answers in the prompt's language
    ("Quelle est la capitale de la France ?", "Paris est la capitale de la France."),
    ("¿Cuál es la capital de España?", "La capital de España es Madrid."),
    ("Qual é a capital do Brasil?", "A capital do Brasil é Brasília."),
    ("Quel est 2+2 ?", "4 — c'est la réponse, évidemment."),
    # markers that are content
    ("How does Llama 2 format prompts?",
     "Llama 2 wraps the user turn in [INST] and [/INST] and the system prompt in <<SYS>> tags."),
    ("Does the file have a header?", "Yes. METADATA is stored in the header."),
    ("Document the endpoint", "### Request:\nGET /x\n\n### Response:\n200 OK"),
    ("Write an Alpaca training example",
     "Here is one.\n\n### Instruction:\nAdd 2 and 2.\n\n### Response:\n4\n\n"
     "### Instruction:\nAdd 3 and 3.\n\n### Response:\n6"),
    ("What does the METADATA: field hold?", "42\nMETADATA: 8 is the schema version."),
    ("What is a ChatML token like <|im_end|>?", "It ends a turn: <|im_end|> closes it."),
    ("Show the config", "```ini\nmode=METADATA\nTOKEN count=5\nREF:5\n```"),
    ("How many tokens did it use?", "Token usage was fine; the log line read `token_count=512`."),
]


@pytest.mark.parametrize("tools", [True, False])
@pytest.mark.parametrize("prompt,text", NEGATIVES)
def test_negatives_pass(prompt, text, tools):
    r = _check(text, prompt=prompt, tools=tools, last=prompt)
    assert r["ok"] is True, (prompt, text, r)


# Tool turns only: without tools the older brevity trim rightly cuts any
# extra text after the bare number. With tools it stands down, and the new
# glued rule needs a junk SHAPE these do not have.
TOOL_NEGATIVES = [
    (Q, "4. Done."),
    (Q, "4349 (checked with python)."),
    # a conversation that writes that block itself
    ("¿Cuánto es 4348 más 1? Answer with only the number.", "4349ñandú"),
]


@pytest.mark.parametrize("prompt,text", TOOL_NEGATIVES)
def test_tool_turn_negatives_pass(prompt, text):
    for last in (prompt, REMINDER + prompt):
        r = _check(text, prompt=last, tools=True, last=last)
        assert r["ok"] is True, (prompt, text, r)


def test_long_answers_are_unaffected_by_the_new_rules():
    prose = ("Python's print function writes text to standard output. " * 30
             + "\n\n### Response:\nThat is all.")
    assert _check(prose, prompt="explain print", tools=False)["ok"] is True


# --------------------------------------------------------------------------- #
# Brevity read from the user's own words
# --------------------------------------------------------------------------- #

def test_brevity_ask_reads_through_cli_wrappers():
    assert answer_check.brevity_ask(Q)
    assert answer_check.brevity_ask(CC_LAST)
    assert answer_check.brevity_ask("<environment_context>\n<cwd>C:\\x</cwd>\n"
                                    "</environment_context>\n" + Q)
    assert not answer_check.brevity_ask("What is 4348 plus 1?")
    assert not answer_check.brevity_ask(REMINDER + "Explain why 4348 plus 1 is 4349.")
    assert not answer_check.brevity_ask(None)


def test_wrapped_brevity_ask_disables_early_release():
    text = ("4349 is what you get when one is added to four thousand three "
            "hundred and forty eight, since counting moves to the next integer.")
    assert answer_check.reads_as_answer(text, last_prompt="What is 4348 plus 1?")
    assert not answer_check.reads_as_answer(text, last_prompt=CC_LAST)


def test_wrapped_brevity_ask_trims_a_tool_free_reply():
    r = _check("4349 is the answer you asked for.", prompt=CC_LAST, tools=False, last=CC_LAST)
    assert r["ok"] is False and r["salvage"] == "4349"


def test_inspect_tail_judges_a_short_reply_head_only_with_last_prompt():
    assert answer_check.inspect_tail("4349 is the answer", prompt_text=Q) is None
    assert answer_check.inspect_tail("4349 is the answer", prompt_text=Q,
                                     last_prompt=Q) == 4
    assert answer_check.inspect_tail("4349âmara", prompt_text=Q, tools_offered=True,
                                     last_prompt=CC_LAST) == 4
    assert answer_check.inspect_tail("4349âmara=METADATA:8", prompt_text=Q,
                                     tools_offered=True) == 4
    # a tool turn keeps "4. Done."-style text in the tail too
    assert answer_check.inspect_tail("4. Done.", prompt_text=Q, tools_offered=True,
                                     last_prompt=Q) is None


# --------------------------------------------------------------------------- #
# The stream gate
# --------------------------------------------------------------------------- #

def _chunk(content=None, fin=None):
    delta = {} if content is None else {"content": content}
    return {"id": "chatcmpl-up", "object": "chat.completion.chunk", "created": 1,
            "model": "m", "choices": [{"index": 0, "delta": delta, "finish_reason": fin}]}


def _line(obj):
    return b"data: " + json.dumps(obj).encode("utf-8")


def _lines(text, fin="stop", size=4):
    return ([_line(_chunk(text[i:i + size])) for i in range(0, len(text), size)]
            + [_line(_chunk(fin=fin)), b"data: [DONE]"])


def _frames(text, fin="stop", size=4):
    return [ln + b"\n\n" for ln in _lines(text, fin, size)]


def _parse_chat(body):
    text, fins = [], []
    for frame in body.split(b"\n\n"):
        frame = frame.strip()
        if not frame.startswith(b"data:") or frame[5:].strip() == b"[DONE]":
            continue
        c = json.loads(frame[5:].strip())["choices"][0]
        if c.get("delta", {}).get("content"):
            text.append(c["delta"]["content"])
        if c.get("finish_reason"):
            fins.append(c["finish_reason"])
    return "".join(text), fins, body.rstrip().endswith(b"data: [DONE]")


def _gate(items, last=CC_LAST, tools=True, mode="bytes", **kw):
    return app._StreamAnswerGate(iter(items), mode=mode, hop_pid=BAD[0], hop_model=BAD[1],
                                 prompt_text=last, last_prompt=last,
                                 tools_offered=tools, **kw)


@pytest.mark.parametrize("size", [1, 4, 64])
def test_gate_serves_only_the_number_on_a_tool_turn(size):
    g = _gate(_frames(LIVE, size=size))
    text, fins, done = _parse_chat(b"".join(g))
    assert text == "4349"
    assert fins == ["stop"] and done
    assert g.cut
    assert _outcome(*BAD) == (0, app._JUNK_FAIL_WEIGHT)


def test_gate_leaves_a_clean_bare_answer_alone():
    g = _gate(_frames("4349", size=1))
    text, fins, done = _parse_chat(b"".join(g))
    assert text == "4349" and fins == ["stop"] and done
    assert not g.cut


def test_brevity_ask_holds_longer_than_the_default_clock():
    assert _gate([], last=CC_LAST)._hold_seconds == app._BRIEF_HOLD_SECONDS
    assert _gate([], last="What is 4348 plus 1?")._hold_seconds == app._HOLD_SECONDS
    assert app._BRIEF_HOLD_SECONDS > app._HOLD_SECONDS


def _slow(first, rest, pause=0.3, size=4):
    """Upstream that sends `first`, goes quiet past the hold clock, then the rest."""
    yield from _frames(first, fin=None)[:-2]
    time.sleep(pause)
    yield from _frames(rest, size=size)


@pytest.mark.parametrize("tools,rest", [
    (True, "âmara=METADATA:8;Note: TOKEN count=298121; REF:5"),
    (True, "âmara"),
    (False, " is the answer you asked for."),
])
def test_clock_released_bare_answer_cannot_grow_junk(tools, rest):
    g = _gate(_slow("4349", rest), tools=tools, hold_seconds=0.05)
    text, fins, done = _parse_chat(b"".join(g))
    assert text == "4349", "released by the clock, then cut at the junk"
    assert fins == ["stop"] and done and g.cut


class _StreamResp:
    headers = {}
    status_code = 200

    def __init__(self, items=()):
        self._items = list(items)

    def iter_content(self, chunk_size=None):
        return iter(self._items)

    def iter_lines(self, decode_unicode=False):
        return iter(self._items)

    def close(self):
        pass


def _events(body):
    out = []
    for block in body.split("\n\n"):
        for ln in block.split("\n"):
            if ln.startswith("data:"):
                try:
                    out.append(json.loads(ln[5:].strip()))
                except ValueError:
                    pass
    return out


def test_messages_translator_serves_the_number():
    g = _gate(_lines(LIVE), mode="lines")
    body = "".join(x if isinstance(x, str) else x.decode() for x in app._anthropic_stream(
        _StreamResp(), "claude", 5, line_iter=g, hop_pid=BAD[0], hop_model=BAD[1],
        prompt_text=CC_LAST, last_prompt=CC_LAST, answer_gate=g))
    ev = _events(body)
    text = "".join(e["delta"]["text"] for e in ev if e.get("type") == "content_block_delta"
                   and e["delta"].get("type") == "text_delta")
    assert text == "4349"
    assert ev[-1]["type"] == "message_stop"
    assert _outcome(*BAD) == (0, app._JUNK_FAIL_WEIGHT), "filed once, by the gate"


def test_responses_translator_serves_the_number():
    g = _gate(_lines(LIVE, size=1), mode="lines")
    body = "".join(x if isinstance(x, str) else x.decode() for x in app._responses_stream(
        _StreamResp(), "auto", line_iter=g, hop_pid=BAD[0], hop_model=BAD[1],
        prompt_text=CC_LAST, last_prompt=CC_LAST, answer_gate=g))
    ev = _events(body)
    assert "".join(e["delta"] for e in ev
                   if e.get("type") == "response.output_text.delta") == "4349"
    assert ev[-1]["type"] == "response.completed"


# --------------------------------------------------------------------------- #
# End to end, all three protocols, stream and non-stream, tools offered
# --------------------------------------------------------------------------- #

class _JsonResp:
    headers = {}
    text = ""
    status_code = 200

    def __init__(self, content):
        self._c = content

    def json(self):
        return {"choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": self._c}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 20}}

    def close(self):
        pass


@pytest.fixture
def wire(monkeypatch):
    calls = []
    replies = {BAD[0]: LIVE, GOOD[0]: "REAL ANSWER"}
    shape = {"sse": _lines}      # the chat route relays raw bytes: tests set _frames

    def fake_dispatch(pid, payload, stream):
        calls.append(pid)
        if not stream:
            return _JsonResp(replies[pid])
        return _StreamResp(shape["sse"](replies[pid]))

    def fake_upstream(pid, payload, stream, *a, **k):
        return fake_dispatch(pid, payload, stream)

    for name in ("_record_chat_usage", "_note_ttft", "_act_pick", "_save_perf_stats"):
        monkeypatch.setattr(app, name, lambda *a, **k: None)
    monkeypatch.setattr(app, "_dispatch_chat", fake_dispatch)
    monkeypatch.setattr(app, "_upstream_chat", fake_upstream)
    monkeypatch.setattr(app, "_build_chain", lambda *a, **k: [BAD, GOOD])
    monkeypatch.setattr(app, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(app, "_resolve_model", lambda m: BAD)
    monkeypatch.setattr(app, "_model_block_reason", lambda pid, m: None)
    monkeypatch.setattr(app, "_route_by_difficulty", lambda *a, **k: (BAD[0], BAD[1], "hard"))
    return calls, shape


def _claude_body(stream):
    return {"model": "max", "max_tokens": 40, "stream": stream, "tools": TOOLS_ANT,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": REMINDER},
                {"type": "text", "text": Q}]}]}


@pytest.mark.parametrize("stream", [True, False])
def test_messages_route_claude_code_shape(wire, stream):
    r = app.app.test_client().post("/v1/messages", json=_claude_body(stream))
    assert r.status_code == 200
    if stream:
        ev = _events(r.get_data(as_text=True))
        text = "".join(e["delta"].get("text", "") for e in ev
                       if e.get("type") == "content_block_delta")
        assert ev[-1]["type"] == "message_stop"
    else:
        body = r.get_json()
        text = "".join(b.get("text", "") for b in body["content"] if b.get("type") == "text")
    assert text == "4349"
    assert wire[0][0] == BAD[0]


@pytest.mark.parametrize("stream", [True, False])
def test_chat_route_tool_turn(wire, stream):
    wire[1]["sse"] = _frames
    r = app.app.test_client().post("/v1/chat/completions", json={
        "model": "auto", "max_tokens": 40, "stream": stream, "tools": TOOLS_OAI,
        "messages": [{"role": "user", "content": REMINDER + Q}]})
    assert r.status_code == 200
    if stream:
        text, fins, done = _parse_chat(r.get_data())
        assert fins == ["stop"] and done
    else:
        text = r.get_json()["choices"][0]["message"]["content"]
    assert text == "4349"


@pytest.mark.parametrize("stream", [True, False])
def test_responses_route_tool_turn(wire, stream):
    r = app.app.test_client().post("/v1/responses", json={
        "model": "auto", "stream": stream, "input": Q,
        "tools": [{"type": "function", "name": "shell", "description": "run",
                   "parameters": {"type": "object", "properties": {}}}]})
    assert r.status_code == 200
    if stream:
        ev = _events(r.get_data(as_text=True))
        text = "".join(e["delta"] for e in ev if e.get("type") == "response.output_text.delta")
        assert ev[-1]["type"] == "response.completed"
    else:
        body = r.get_json()
        text = "".join(c.get("text", "") for item in body.get("output") or ()
                       if item.get("type") == "message"
                       for c in item.get("content") or () if c.get("type") == "output_text")
    assert text == "4349"
