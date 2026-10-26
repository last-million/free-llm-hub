"""A reply that says "your message contains only a tool setup" is not an answer.

MEASURED 2026-10-26 (Build session d9af0686, codex, `coding-max`, ~43K-token turns):
the user's message reached the hub intact (codex's own rollout has it as a user
message every time), but the g4f relay copy `pa:fec3c477:claude-haiku-5-5-anthropic`
answered three turns in a row with a complaint that there was no request:

    "It looks like your message contains only a tool specification and system
     configuration, with no actual question or task attached. ..."
    "It looks like your message contains only a tool-definition block ..."
    "It looks like your message contains only the tool setup and no specific request. ..."

Reproduced on purpose through the hub: the same pair answered "BANANA-42" to a
155-token request and "your message is a long block of system-style instructions ...
it doesn't include an actual task" to a 36K-token one -- the relay loses the END of
a big request, which is where the user's turn sits.

Clean prose, no tool call -> served as the final answer -> codex recorded
task_complete and the user had to resend. `idle_greeting` missed it because the offer
to help comes ~80 chars in (its start limit is 40, pinned by test_double_answer).
This is the separate rule: the reply's OPENING claims the user's message holds no
request but a tool/system setup, while the user's last turn is a real, non-empty one.
Same gate as idle_greeting (tool turns and relay hops). Flag-free: it rides inside
`inspect`'s checks array.
"""
import copy

import pytest

import answer_check as a

# The replies, verbatim from the codex rollout and the probe.
CLAIM_1 = ("It looks like your message contains only a tool specification and system "
           "configuration, with no actual question or task attached. There's nothing for "
           "me to act on yet.\n\nWhat would you like to do? For example, I can:\n\n"
           "- Answer a general question or explain a concept\n"
           "- Help with writing, editing, or planning something\n"
           "- Search online for current information, news, or prices")
CLAIM_2 = ("It looks like your message contains only a tool-definition block (the "
           "system-style instructions listing shell commands, MCP resources, browser "
           "tools, and so on) and no actual request. There is nothing for me to act on yet.")
CLAIM_3 = ("It looks like your message contains only the tool setup and no specific "
           "request. What would you like me to help with? I can answer questions, write "
           "or review code, work with files on your machine, search the web, or generate "
           "documents and images.")
CLAIM_4 = ("It looks like your message contains only a tool configuration block without "
           "a specific request. What would you like help with?")
CLAIM_5 = ("I notice your message is a long, repeated block of system-style instructions "
           "describing a coding agent with tools (shell_command, list_mcp_resources, "
           "browser_navigate), and it doesn't include an actual task or question for me "
           "to act on.\n\nCould you tell me what you'd like help with?")
CLAIMS = (CLAIM_1, CLAIM_2, CLAIM_3, CLAIM_4, CLAIM_5)

TASK = ("i'm talking abut motion desing  with 3D  real 3 D motion desing  cinematic  "
        "paralal aut reverse  in ll the page  with scnes")


def test_every_measured_reply_is_rejected():
    for text in CLAIMS:
        assert a.is_empty_message_claim(text, TASK), text


def test_the_build_pages_standing_instruction_does_not_switch_the_rule_off():
    # Every Build message is the user's text + this notice (agentic_chat.py ~2858). It
    # says "native planning/task tool" and "tool access": a bare "tool" exclusion would
    # have made the rule dead for the very sessions that hit the bug.
    build_prompt = (TASK + "\n\n---\n(Standing instruction for this session: For any "
                    "non-trivial task: think it through step by step first, then break it "
                    "into phases with a visible todo list -- your own native planning/task "
                    "tool if you have one, and a real file in this project either way, "
                    "PROGRESS.md, as a markdown checklist. Testing/verification is expected "
                    "this session: use your existing tool access (Playwright, if installed) "
                    "to actually run and verify the result. READ FIRST: C:\\Users\\x\\"
                    "project\\.calvoun-brief-d9af0686a0e4.md -- this task's required "
                    "standards. Open it at that exact path and follow it.)")
    for text in CLAIMS:
        assert a.is_empty_message_claim(text, build_prompt), text


def test_a_codex_wrapper_around_the_real_turn_is_stripped():
    wrapped = "<environment_context><cwd>C:\\p</cwd></environment_context>\n" + TASK
    assert a.is_empty_message_claim(CLAIM_3, wrapped)


def test_a_one_word_continue_is_still_a_real_message():
    # The report was about "continue": no word-count gate, unlike idle_greeting.
    for prompt in ("continue", "go", "ok", "yes"):
        assert a.is_empty_message_claim(CLAIM_3, prompt), prompt


def test_inspect_rejects_it_on_a_relay_tool_turn_and_a_direct_tool_turn():
    for relay in (True, False):
        v = a.inspect(CLAIM_3, prompt_text=TASK, last_prompt=TASK,
                      tools_offered=True, relay=relay)
        assert v["ok"] is False, relay
        assert "empty_message_claim" in v["reasons"], relay


def test_inspect_rejects_it_on_a_relay_chat_turn_without_tools():
    v = a.inspect(CLAIM_3, prompt_text=TASK, last_prompt=TASK, tools_offered=False, relay=True)
    assert v["ok"] is False
    assert "empty_message_claim" in v["reasons"]


def test_inspect_leaves_a_tool_free_direct_turn_alone():
    # Neither relay nor tools_offered -> the rule does not apply (same gate as idle_greeting).
    v = a.inspect(CLAIM_3, prompt_text=TASK, last_prompt=TASK, tools_offered=False, relay=False)
    assert "empty_message_claim" not in v["reasons"]


def test_a_really_empty_user_turn_may_be_told_so():
    # Nothing (or only a CLI wrapper block) was sent: the model is right.
    for prompt in ("", None, "   ", "<system-reminder>tools: shell</system-reminder>"):
        assert not a.is_empty_message_claim(CLAIM_3, prompt), repr(prompt)


def test_a_user_who_pastes_a_tool_definition_is_not_wrong_to_be_told_so():
    for prompt in ("here is my tool definition block, what is wrong with it?",
                   "review this tool configuration for me",
                   "what does this system prompt do? {\"tools\": []}",
                   "check my function schema please"):
        assert not a.is_empty_message_claim(CLAIM_2, prompt), prompt


def test_a_real_answer_is_not_touched():
    for text in ("Done. I rebuilt the hero with a scroll-driven camera and verified it live.",
                 "Your message mentions the tool configuration, so I updated "
                 "`codex-config.toml` and restarted the server.",
                 # similar words, but it answers the task instead of claiming there is none
                 "Your request is to rebuild the page. I changed three files; no question "
                 "remains, the task is done."):
        assert not a.is_empty_message_claim(text, TASK), text


def test_the_claim_must_open_the_reply():
    text = ("Here is the plan for the 3D page:\n\n" + ("step one two three " * 20) +
            "\n\nNote: it looks like your message contains only a tool setup and no "
            "specific request.")
    assert not a.is_empty_message_claim(text, TASK)


def test_an_overlong_reply_is_left_alone():
    assert not a.is_empty_message_claim(CLAIM_3 + (" more text" * 300), TASK)


def test_claim_without_the_no_request_half_is_not_enough():
    # Subject + tool wording alone is not a claim that nothing was asked.
    text = "Your message contains the tool setup block I need, so I'll start on the files now."
    assert not a.is_empty_message_claim(text, TASK)


@pytest.mark.parametrize("bad", [None, "", 7, {}, []])
def test_garbage_never_raises(bad):
    assert a.is_empty_message_claim(bad, TASK) in (True, False)
    assert a.is_empty_message_claim(CLAIM_3, bad) in (True, False)


def test_garbage_never_mutates_the_input():
    before = copy.deepcopy(CLAIM_3)
    a.is_empty_message_claim(CLAIM_3, TASK)
    assert CLAIM_3 == before


# --- the stream gate: tiny frames must not release the reply early ----------------

def test_reads_as_answer_never_releases_the_claim_early():
    # The claim's first 80+ chars pass every generic "reads like prose" test; the
    # no-request half arrives later. The opening alone must hold the stream.
    for text in CLAIMS:
        for cut in (90, 100, len(text)):
            assert not a.reads_as_answer(text[:cut], last_prompt=TASK, relay=True), (cut, text[:cut])
    genuine = ("The scan is done. I found three dead-end tool calls in the relay layer "
               "and fixed the routing so the chain moves on.")
    assert a.reads_as_answer(genuine, last_prompt=TASK, relay=True)


def test_reads_as_answer_releases_the_same_words_when_the_user_turn_is_empty():
    # Nothing was sent, so the model is right and the stream is not held on the claim.
    assert a.reads_as_answer(CLAIM_3, last_prompt="", relay=True) is True


# --- the pre-commit stream peek, token by token ----------------------------------------
# MEASURED 2026-10-26 with the first version of the rule: fed in ONE frame the claim was
# "junk"; fed 1-10 characters at a time (how a model really streams) the peek reached its
# first verdict point ~11 characters in, saw nothing wrong, and committed -- codex then
# ended its turn on the full claim. The peek now keeps reading while the text could still
# become the claim, and fails the hop the moment both halves are in.
import json  # noqa: E402

import app as A  # noqa: E402

_TOOLS = [{"type": "function", "function": {"name": "bash", "parameters": {"type": "object"}}}]


def _sse(text, n):
    out = [("data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": text[i:i + n]}}]})
            ).encode() for i in range(0, len(text), n)]
    out.append(b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}')
    out.append(b"data: [DONE]")
    return out


def _peek(text, n, pid, tools, prompt):
    payload = {"messages": [{"role": "user", "content": prompt}]}
    if tools:
        payload["tools"] = _TOOLS
    check = A._peek_check(payload, tools, pid=pid)
    status, _buf = A._peek_until_content(iter(_sse(text, n)), 5, check=check)
    return status


@pytest.mark.parametrize("n", [1, 3, 10, 400])
@pytest.mark.parametrize("pid,tools", [("g4f", True), ("nvidia", True), ("g4f", False)])
@pytest.mark.parametrize("prompt", [TASK, "continue"])
def test_the_peek_never_commits_the_claim_whatever_the_frame_size(n, pid, tools, prompt):
    for text in CLAIMS:
        assert _peek(text, n, pid, tools, prompt) in ("junk", "nonanswer"), (n, pid, tools, text[:60])


@pytest.mark.parametrize("n", [1, 3, 10, 400])
def test_a_real_reply_that_shares_the_opener_still_commits(n):
    for text in ("It looks like the bug is in parser.py: parse() drops the last token. "
                 "I changed the loop bound to len(tokens) and the test passes now.",
                 "Your message asks for a cinematic 3D page. I rebuilt the hero with a "
                 "scroll-driven camera and verified it live in the browser, no errors.",
                 "I see the problem: the tool setup in codex-config.toml pointed at the old "
                 "port. I fixed it and restarted the server; everything answers now."):
        for pid, tools in (("g4f", True), ("nvidia", True), ("g4f", False)):
            assert _peek(text, n, pid, tools, TASK) == "content", (n, pid, tools, text[:40])


def test_a_claim_to_an_empty_user_turn_is_not_failed_by_the_peek():
    for n in (1, 10, 400):
        assert _peek(CLAIM_3, n, "g4f", True, "") == "content"


def test_the_gate_stays_off_for_a_direct_tool_free_turn_like_inspect():
    # Neither tools nor a relay hop: inspect() does not apply the rule there, so neither
    # does the peek (and it must not hold such a stream waiting for it).
    for n in (1, 10, 400):
        assert _peek(CLAIM_3, n, "nvidia", False, TASK) == "content"


def test_a_tool_call_after_a_claim_like_opener_still_commits_at_once():
    frames = _sse("It looks like your message", 1)[:-2] + [
        b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"c1",'
        b'"function":{"name":"bash","arguments":"{}"}}]}}]}',
        b"data: [DONE]"]
    check = A._peek_check({"messages": [{"role": "user", "content": TASK}], "tools": _TOOLS},
                          True, pid="g4f")
    status, _ = A._peek_until_content(iter(frames), 5, check=check)
    assert status == "content"


# --- history: a bogus turn must not poison the hops after it -----------------------------
# The CLI keeps every reply in the thread, and a model copies what it finds there. Each hop
# sees those turns replaced by a neutral note (the CLI's own history is untouched).

def _thread():
    return [
        {"role": "system", "content": "You are a coding agent."},
        {"role": "user", "content": TASK},
        {"role": "assistant", "content": CLAIM_1},
        {"role": "user", "content": "continue"},
        {"role": "assistant", "content": CLAIM_3},
        {"role": "user", "content": "go on, rebuild it with real 3D parallax"},
    ]


def test_scrub_replaces_only_the_bogus_assistant_turns():
    msgs = _thread()
    before = copy.deepcopy(msgs)
    out = A._scrub_empty_claims(msgs)
    assert msgs == before                                  # never mutated
    assert out[2]["content"] == A._EMPTY_CLAIM_TURN_NOTE
    assert out[4]["content"] == A._EMPTY_CLAIM_TURN_NOTE
    for i in (0, 1, 3, 5):                                 # system + user turns untouched
        assert out[i] == msgs[i]
    assert [m["role"] for m in out] == [m["role"] for m in msgs]      # roles keep alternating


def test_scrub_is_deterministic_and_idempotent():
    once = A._scrub_empty_claims(_thread())
    assert once == A._scrub_empty_claims(_thread())
    assert A._scrub_empty_claims(once) is once             # nothing left to replace


def test_scrub_leaves_real_replies_and_tool_calls_alone():
    msgs = [{"role": "user", "content": TASK},
            {"role": "assistant", "content": "Done. I rebuilt the hero scene."},
            {"role": "user", "content": "now the footer"},
            {"role": "assistant", "content": CLAIM_3,
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "bash", "arguments": "{}"}}]}]
    assert A._scrub_empty_claims(msgs) is msgs


def test_scrub_keeps_a_claim_that_follows_an_empty_user_turn():
    msgs = [{"role": "user", "content": "<system-reminder>x</system-reminder>"},
            {"role": "assistant", "content": CLAIM_3}]
    assert A._scrub_empty_claims(msgs) is msgs


def test_scrub_flag_off_is_byte_identical(monkeypatch):
    real = A.config.get_flag
    monkeypatch.setattr(A.config, "get_flag",
                        lambda name, default=None: False if name == "scrub_empty_claim_turns"
                        else real(name, default))
    msgs = _thread()
    assert A._scrub_empty_claims(msgs) is msgs


@pytest.mark.parametrize("bad", [None, "x", 3, {}, [None, 7, "s"]])
def test_scrub_never_raises_on_garbage(bad):
    A._scrub_empty_claims(bad)


def test_the_hop_sends_the_scrubbed_thread(monkeypatch):
    from tests.test_old_tool_result_clearing import _hop
    body, _ratio = _hop(monkeypatch, _thread(), tools=_TOOLS)
    sent = json.dumps(body["messages"])
    assert "contains only" not in sent
    assert A._EMPTY_CLAIM_TURN_NOTE in sent
    assert "rebuild it with real 3D parallax" in sent      # the user's turns went up intact


# --- the pair that lost the request goes behind others for BIG requests -----------------
# It answers small turns fine (155 tokens: "BANANA-42"), so it is not benched outright: the
# long-context ledger (requests >= LONG_CTX_SPEED_TOKENS) moves it behind the others.

@pytest.fixture
def ledger():
    with A._outcome_lock:
        saved = dict(A._long_ctx_speed)
        A._long_ctx_speed.clear()
    yield A._long_ctx_speed
    with A._outcome_lock:
        A._long_ctx_speed.clear()
        A._long_ctx_speed.update(saved)


BIG = A.LONG_CTX_SPEED_TOKENS + 20000
PAIR = ("g4f", "pa:fec3c477:claude-haiku-5-5-anthropic")


def test_a_lost_request_on_a_big_turn_puts_the_pair_in_the_slow_band(ledger):
    assert A._long_ctx_band(*PAIR) == 1                    # unmeasured
    A._note_lost_request(*PAIR, ["empty_message_claim"], BIG)
    assert A._long_ctx_band(*PAIR) == 2


def test_the_big_turn_chain_puts_that_pair_behind_the_others(ledger, monkeypatch):
    monkeypatch.setattr(A, "_rr_fit", lambda est: est)
    chain = [PAIR + ("x",), ("nvidia", "z-ai/glm-5.3", "x"), ("inception", "mercury-3", "x")]
    A._note_lost_request(*PAIR, ["empty_message_claim"], BIG)
    out = A._prefer_fast_long_context(chain, BIG)
    assert out[-1][:2] == PAIR
    # ...and a small turn keeps the strength order, the pair stays first choice there.
    assert A._prefer_fast_long_context(chain, 5000) == chain


@pytest.mark.parametrize("reasons,est,why", [
    (["empty_message_claim"], 5000, "a small request: only the ordinary junk strike"),
    (["empty_message_claim"], 0, "unknown size"),
    (["idle_greeting"], BIG, "a different reason"),
    ([], BIG, "no reason"),
    (None, BIG, "no reasons at all")])
def test_no_demotion_without_a_big_lost_request(ledger, reasons, est, why):
    A._note_lost_request(*PAIR, reasons, est)
    assert A._long_ctx_band(*PAIR) == 1, why


def test_no_demotion_when_the_client_left_or_the_flag_is_off(ledger, monkeypatch):
    monkeypatch.setattr(A, "_client_gone", lambda: True)
    A._note_lost_request(*PAIR, ["empty_message_claim"], BIG)
    assert A._long_ctx_band(*PAIR) == 1
    monkeypatch.setattr(A, "_client_gone", lambda: False)
    real = A.config.get_flag
    monkeypatch.setattr(A.config, "get_flag",
                        lambda name, default=None: False if name == "lost_request_demote"
                        else real(name, default))
    A._note_lost_request(*PAIR, ["empty_message_claim"], BIG)
    assert A._long_ctx_band(*PAIR) == 1


def test_the_peek_files_it_on_a_big_request(ledger):
    payload = {"tools": _TOOLS, "messages": [
        {"role": "system", "content": "context " * (BIG * 4 // 8)},
        {"role": "user", "content": TASK}]}
    assert A._payload_est_tokens(payload) >= A.LONG_CTX_SPEED_TOKENS
    check = A._peek_check(payload, True, pid=PAIR[0], model=PAIR[1])
    status, _ = A._peek_until_content(iter(_sse(CLAIM_3, 3)), 5, check=check)
    assert status == "junk"
    assert A._long_ctx_band(*PAIR) == 2


def test_the_peek_does_not_file_it_on_a_small_request(ledger):
    check = A._peek_check({"tools": _TOOLS, "messages": [{"role": "user", "content": TASK}]},
                          True, pid=PAIR[0], model=PAIR[1])
    status, _ = A._peek_until_content(iter(_sse(CLAIM_3, 3)), 5, check=check)
    assert status == "junk"
    assert A._long_ctx_band(*PAIR) == 1


# --- through the real roles path ----------------------------------------------------

from tests.test_tool_turn_roles import roles, _dispatcher, _ro_body  # noqa: E402,F401


def _claim_reply(payload):
    return {"choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": CLAIM_3}}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60}}


def _text_reply(payload):
    return {"choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "Done. Fixed it."}}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 5, "total_tokens": 45}}


def test_the_roles_path_hands_the_claim_over_to_the_next_actor(roles, monkeypatch):
    import app as A
    calls = []
    body = _ro_body()
    body["messages"] = [{"role": "user", "content": TASK}]
    monkeypatch.setattr(A, "_is_relay_pid", lambda pid: pid == "p1")
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (0.0, _claim_reply, 200), "p2": (0.0, _text_reply, 200),
        "p3": (0.0, _text_reply, 200)}, calls))
    data, _hdrs = A._swarm_tool_result(body)
    m = data["choices"][0]["message"]
    assert m["content"] != CLAIM_3
    assert "Fixed" in m["content"]
    assert any(pid == "p1" for pid, *_ in calls)     # tried and rejected, not skipped


def test_scrub_skips_long_replies_without_running_the_check(monkeypatch):
    """The scrub runs on every hop over the whole thread: a long answer is never a claim (the
    claim is <= 1200 chars), so it must not reach the regexes at all."""
    calls = []
    real = A.answer_check.is_empty_message_claim
    monkeypatch.setattr(A.answer_check, "is_empty_message_claim",
                        lambda *a, **k: calls.append(1) or real(*a, **k))
    msgs = [{"role": "user", "content": TASK},
            {"role": "assistant", "content": "word " * 5000},          # 25K chars
            {"role": "user", "content": "next"},
            {"role": "assistant", "content": CLAIM_3}]
    out = A._scrub_empty_claims(msgs)
    assert len(calls) == 1                                              # only the short one
    assert out[3]["content"] == A._EMPTY_CLAIM_TURN_NOTE and out[1] is msgs[1]
