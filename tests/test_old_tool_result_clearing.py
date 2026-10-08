"""Old tool results are cleared on big turns (ctxwin.clear_old_tool_results).

MEASURED 2026-10-08 (hub.log, OpenCode `coding-swarm`): ~85K-token tool turns on
free models timed out -- `CHAT-DEADLINE est=85203 ... _HopBudgetExceeded no
answer within 106s` -- and on a slow uplink the same body costs ~17 s of upload
per hop. Most of those tokens are OLD tool outputs the model no longer needs
verbatim. The hub replaces the content of an older, long tool result with one
line and sends the smaller conversation; nothing is removed and no id changes.

Everything below is hermetic: deterministic generated histories, a fake
`requests.post`, no network, no real config.
"""
import copy
import json
import logging
import re

import pytest

import app as A
import ctxwin

FLAG = "old_tool_result_clearing"

TOOLS = [{"type": "function", "function": {
    "name": n, "description": "Tool %s. " % n + "Use it carefully. " * 14,
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}
    for n in ("read", "edit", "bash", "grep", "glob", "write", "todo", "task")]


# --------------------------------------------------------------------------- #
# A deterministic generator of realistic agent history (no `random`)
# --------------------------------------------------------------------------- #

class _Lcg:
    def __init__(self, seed):
        self.x = (seed * 2654435761 + 12345) & 0x7FFFFFFF

    def next(self, n):
        self.x = (self.x * 1103515245 + 12345) & 0x7FFFFFFF
        return (self.x >> 8) % n


_WORDS = ("request response handler cache config token parse render session router "
          "payload buffer stream worker queue retry timeout schema client server index "
          "value result option filter mapper loader writer").split()


def _code_file(rng, chars, name):
    lines = ["# " + name, "import os, sys, json", ""]
    size = sum(len(x) + 1 for x in lines)
    while size < chars:
        w1, w2, w3 = (_WORDS[rng.next(len(_WORDS))] for _ in range(3))
        block = ["def %s_%s(%s, %s=None):" % (w1, w2, w3, w2),
                 "    %s = %s.get('%s', %d)" % (w3, w1, w2, rng.next(1000)),
                 "    if %s is None:" % w3,
                 "        raise ValueError('%s %s missing')" % (w1, w3),
                 "    return %s_%s(%s, %d)" % (w2, w3, w1, rng.next(100)),
                 ""]
        lines += block
        size += sum(len(x) + 1 for x in block)
    return "\n".join(lines)[:chars]


def _test_log(rng, chars):
    lines = ["============================= test session starts ============================="]
    i = 0
    while sum(len(x) + 1 for x in lines) < chars:
        w = _WORDS[rng.next(len(_WORDS))]
        lines.append("tests/test_%s.py::test_%s_%d PASSED%s[%3d%%]"
                     % (w, w, i, " " * 20, min(99, i % 100)))
        i += 1
    lines.append("============================== %d passed in 3.21s ==============================" % i)
    return "\n".join(lines)


def _grep_out(rng, chars):
    lines = []
    while sum(len(x) + 1 for x in lines) < chars:
        w = _WORDS[rng.next(len(_WORDS))]
        lines.append("src/%s_%d.py:%d:    def %s_handler(self, %s):"
                     % (w, rng.next(40), 1 + rng.next(400), w, _WORDS[rng.next(len(_WORDS))]))
    return "\n".join(lines)


FAIL_TAIL = ("\nExit code: 1\nTraceback (most recent call last):\n"
             "  File \"src/x.py\", line 3, in run\nValueError: boom\n")


def _tool_call(cid, name, args):
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": cid, "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)}}]}


def _history(target_tokens=85000, tools=TOOLS, fail_steps=()):
    """OpenAI-shaped agent history of ~target_tokens: file reads of 3-8K chars,
    grep output, test logs, tiny edit confirmations, a prose line now and then."""
    rng = _Lcg(2026)
    msgs = [{"role": "system", "content": "You are a coding agent. " + "Follow the rules. " * 300},
            {"role": "user", "content": "Refactor the request handlers in src/ and make "
                                        "the tests pass."}]
    chars = sum(len(m["content"]) for m in msgs) + len(json.dumps(tools))
    step = 0
    while chars // 4 + 400 < target_tokens:
        cid = "call_%03d" % step
        kind = step % 5
        if kind in (0, 4):
            name, args = "read", {"path": "src/mod_%d.py" % step}
            out = _code_file(rng, 3000 + rng.next(5000), "src/mod_%d.py" % step)
        elif kind == 1:
            name, args = "grep", {"pattern": "handler"}
            out = _grep_out(rng, 1000 + rng.next(3000))
        elif kind == 2:
            name, args = "edit", {"path": "src/mod_%d.py" % step}
            out = "Edited src/mod_%d.py (1 replacement)" % step
        else:
            name, args = "bash", {"command": "pytest -q"}
            out = _test_log(rng, 2000 + rng.next(4000))
        if step in fail_steps:
            out += FAIL_TAIL
        msgs.append(_tool_call(cid, name, args))
        msgs.append({"role": "tool", "tool_call_id": cid, "content": out})
        chars += len(out) + 120
        if step % 4 == 3:
            note = "Done with step %d; continuing with the next file." % step
            msgs.append({"role": "assistant", "content": note})
            chars += len(note)
        step += 1
    return msgs


def _mini(n=14, size=2500, fail=(), first="Fix the parser."):
    """n steps of one tool call + one `size`-char result each (small history,
    used with the size gate off)."""
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": first}]
    for i in range(n):
        body = _code_file(_Lcg(i + 1), size, "p%d.py" % i)
        if i in fail:
            body += FAIL_TAIL
        msgs.append(_tool_call("call_%02d" % i, "read", {"path": "p%d.py" % i}))
        msgs.append({"role": "tool", "tool_call_id": "call_%02d" % i, "content": body})
    return msgs


def _anthropic_native(n=14, size=3000, fail=()):
    msgs = [{"role": "user", "content": [{"type": "text", "text": "Fix the parser please."}]}]
    for i in range(n):
        body = _code_file(_Lcg(i + 1), size, "p%d.py" % i)
        msgs.append({"role": "assistant", "content": [
            {"type": "text", "text": "reading p%d.py" % i},
            {"type": "tool_use", "id": "toolu_%02d" % i, "name": "Read",
             "input": {"path": "p%d.py" % i}}]})
        block = {"type": "tool_result", "tool_use_id": "toolu_%02d" % i, "content": body}
        if i in fail:
            block["is_error"] = True
        msgs.append({"role": "user", "content": [
            block, {"type": "text", "text": "<system-reminder>keep going</system-reminder>"}]})
    return msgs


def _responses_native(n=14, size=3000):
    items = [{"type": "message", "role": "user",
              "content": [{"type": "input_text", "text": "Refactor the handler."}]}]
    for i in range(n):
        body = _code_file(_Lcg(i + 1), size, "f%d.py" % i)
        items.append({"type": "function_call", "call_id": "c%02d" % i, "name": "shell",
                      "arguments": json.dumps({"cmd": "cat f%d.py" % i})})
        items.append({"type": "function_call_output", "call_id": "c%02d" % i, "output": body})
    return items


def _clear(msgs, **kw):
    kw.setdefault("min_tokens", 0)
    return ctxwin.clear_old_tool_results(msgs, **kw)


def _is_stub(text):
    return isinstance(text, str) and text.startswith(ctxwin.CLEARED_RESULT_PREFIX)


def _skeleton(msgs):
    """What a strict provider validates: roles and tool-call ids, in order."""
    return [(m.get("role"), m.get("tool_call_id"),
             tuple(tc["id"] for tc in m.get("tool_calls") or [])) for m in msgs]


def _tool_texts(msgs):
    return [m["content"] for m in msgs if m.get("role") == "tool"]


# --------------------------------------------------------------------------- #
# The stub
# --------------------------------------------------------------------------- #

def test_the_stub_is_the_documented_one_line():
    body = "line one\n\n   line   two\t" + "x" * 3000
    stub = ctxwin.cleared_stub(body)
    m = re.match(r"^\[tool output cleared by the hub to save context \(was ~(\d+) chars\): "
                 r"(.*)\. Re-run the command if you need it\.\]$", stub)
    assert m, stub
    assert int(m.group(1)) == len(body)
    assert "\n" not in stub
    assert len(m.group(2)) <= 160
    assert m.group(2).startswith("line one line two xxx")


def test_a_stub_is_a_function_of_the_result_alone():
    body = _code_file(_Lcg(5), 4000, "a.py")
    assert ctxwin.cleared_stub(body) == ctxwin.cleared_stub(body)
    assert ctxwin.cleared_stub(body) != ctxwin.cleared_stub(body + "more")


# --------------------------------------------------------------------------- #
# Pairing: all three wire shapes, native and through the real translators
# --------------------------------------------------------------------------- #

def test_openai_shape_keeps_every_call_paired_with_its_result():
    msgs = _mini(14)
    out, st = _clear(msgs, keep_recent=4)
    assert st["cleared"] == 10
    assert len(out) == len(msgs)
    assert _skeleton(out) == _skeleton(msgs)
    assert len(A._sanitize_tool_messages(out)) == len(out)      # nothing orphaned
    for a, b in zip(msgs, out):
        if a is not b:                                          # only results change
            assert a["role"] == "tool" and set(a) == set(b)
            assert b["tool_call_id"] == a["tool_call_id"]


def test_anthropic_native_blocks_keep_their_ids_and_only_lose_old_text():
    msgs = _anthropic_native(14)
    before = copy.deepcopy(msgs)
    out, st = _clear(msgs, keep_recent=8)
    assert msgs == before, "the input was mutated"
    assert st["cleared"] == 6                     # units 0..6 older; unit 0 is the user turn
    assert len(out) == len(msgs)

    def ids(ms):
        return [(b.get("type"), b.get("id") or b.get("tool_use_id"))
                for m in ms if isinstance(m["content"], list)
                for b in m["content"] if b.get("type") in ("tool_use", "tool_result")]
    assert ids(out) == ids(msgs)
    cleared = [b for m in out if isinstance(m["content"], list) for b in m["content"]
               if b.get("type") == "tool_result" and _is_stub(b["content"])]
    assert len(cleared) == 6
    # the reminder text block next to a result is not a result: untouched
    assert all(m["content"][1] == {"type": "text",
                                   "text": "<system-reminder>keep going</system-reminder>"}
               for m in out if m["role"] == "user" and len(m["content"]) == 2)


def test_responses_native_items_keep_their_call_ids():
    items = _responses_native(14)
    before = copy.deepcopy(items)
    out, st = _clear(items, keep_recent=8)
    assert items == before
    assert st["cleared"] == 6
    assert [(i.get("type"), i.get("call_id")) for i in out] == \
           [(i.get("type"), i.get("call_id")) for i in items]
    outs = [i["output"] for i in out if i["type"] == "function_call_output"]
    assert sum(_is_stub(o) for o in outs) == 6
    assert not any(_is_stub(o) for o in outs[-8:])               # newest units verbatim


@pytest.mark.parametrize("shape", ["anthropic", "responses"])
def test_the_real_translators_hand_over_pairs_that_survive_clearing(shape):
    if shape == "anthropic":
        chat = A._anthropic_to_openai_messages(
            {"system": "S", "messages": _anthropic_native(14)})
    else:
        chat = A._responses_to_chat({"instructions": "S", "input": _responses_native(14)})
    out, st = _clear(chat, keep_recent=4)
    assert st["cleared"] > 0
    assert _skeleton(out) == _skeleton(chat)
    assert len(A._sanitize_tool_messages(out)) == len(out)
    assert any(_is_stub(t) for t in _tool_texts(out))


def test_parallel_calls_are_one_unit_with_all_their_results():
    """/v1/responses turns Codex's parallel calls into one assistant message per
    call; the whole run plus its results is ONE unit, cleared or kept together."""
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "go"}]
    big = lambda i: _code_file(_Lcg(i), 3000, "f%d" % i)          # noqa: E731
    for step in range(5):
        a, b = "a%d" % step, "b%d" % step
        msgs += [_tool_call(a, "read", {}), _tool_call(b, "read", {}),
                 {"role": "tool", "tool_call_id": a, "content": big(step * 2)},
                 {"role": "tool", "tool_call_id": b, "content": big(step * 2 + 1)}]
    out, st = _clear(msgs, keep_recent=2)
    texts = _tool_texts(out)
    assert [_is_stub(t) for t in texts] == [True] * 6 + [False] * 4
    assert st["cleared"] == 6


def test_message_units_match_the_old_implementation_on_openai_histories():
    """ctxwin.unit_spans replaced app._message_units' body; on OpenAI-shaped
    histories it must group exactly as the old code did."""
    def old(rest):
        is_call = lambda m: (isinstance(m, dict) and m.get("role") == "assistant"   # noqa: E731
                             and isinstance(m.get("tool_calls"), list))
        units, i, n = [], 0, len(rest)
        while i < n:
            m = rest[i]
            if is_call(m):
                unit, j = [m], i + 1
                while j < n and is_call(rest[j]):
                    unit.append(rest[j])
                    j += 1
                ids = {tc.get("id") for a in unit for tc in a["tool_calls"]
                       if isinstance(tc, dict) and tc.get("id")}
                while (j < n and isinstance(rest[j], dict) and rest[j].get("role") == "tool"
                       and (not ids or rest[j].get("tool_call_id") in ids)):
                    unit.append(rest[j])
                    j += 1
                units.append(unit)
                i = j
            else:
                units.append([m])
                i += 1
        return units

    shapes = [
        _history(20000)[1:],
        _mini(6)[1:],
        [_tool_call("x", "t", {}), {"role": "tool", "tool_call_id": "other", "content": "z"},
         {"role": "tool", "tool_call_id": "x", "content": "y"}, {"role": "user", "content": "u"}],
        [{"role": "tool", "tool_call_id": "orphan", "content": "o"},
         {"role": "assistant", "content": None, "tool_calls": []},
         {"role": "tool", "content": "no id"}],
        [{"role": "assistant", "content": None, "tool_calls": [{"function": {}}]},
         {"role": "tool", "tool_call_id": "q", "content": "r"}],
    ]
    for rest in shapes:
        new = A._message_units(rest)
        want = old(rest)
        assert [[id(m) for m in u] for u in new] == [[id(m) for m in u] for u in want]


# --------------------------------------------------------------------------- #
# What is never touched
# --------------------------------------------------------------------------- #

def test_the_newest_units_the_system_prompt_and_the_instruction_are_untouched():
    msgs = _mini(14)
    msgs.insert(8, {"role": "user", "content": "Actually, use the v2 parser instead."})
    out, st = _clear(msgs, keep_recent=5)
    assert st["cleared"] > 0
    rest = msgs[1:]
    spans = ctxwin.unit_spans(rest)
    for a, b in spans[-5:]:
        for k in range(a, b):
            assert out[1 + k] is msgs[1 + k], "a message of the newest 5 units changed"
    assert out[0] is msgs[0]                                     # system prompt
    assert out[8] is msgs[8]                                     # the instruction
    assert out[1] is msgs[1]


def test_a_message_carrying_the_latest_instruction_is_never_touched():
    msgs = _anthropic_native(14)
    control, _ = _clear(msgs, keep_recent=4)
    assert control[4] is not msgs[4]                             # would be cleared ...
    msgs[4]["content"].append({"type": "text", "text": "Actually, switch to the v2 parser."})
    out, _ = _clear(msgs, keep_recent=4)
    assert out[4] is msgs[4]                                     # ... but carries the instruction


def test_the_last_three_failing_steps_keep_their_output():
    # 15 units (user + 14 steps), keep_recent=4: steps 0..9 are older. Failing
    # steps 1,2,3,4 and 13; the LAST THREE failing are 13 (in the window), 4, 3.
    msgs = _mini(14, fail=(1, 2, 3, 4, 13))
    out, st = _clear(msgs, keep_recent=4)
    texts = _tool_texts(out)
    stubbed = [i for i, t in enumerate(texts) if _is_stub(t)]
    assert stubbed == [0, 1, 2, 5, 6, 7, 8, 9]
    assert st["kept_failing"] == 2                                # steps 3 and 4
    assert texts[3].endswith(FAIL_TAIL) and texts[4].endswith(FAIL_TAIL)


def test_an_anthropic_is_error_result_counts_as_failing():
    # keep_recent=4 -> steps 0..9 are older; failing 2,3,4,5 -> the last three
    # (5, 4, 3) keep their output, step 2 (the fourth-last) is cleared.
    msgs = _anthropic_native(14, fail=(2, 3, 4, 5))
    out, st = _clear(msgs, keep_recent=4)
    kept = [i for i in range(10) if not _is_stub(out[2 + 2 * i]["content"][0]["content"])]
    assert kept == [3, 4, 5]
    assert st["kept_failing"] == 3


@pytest.mark.parametrize("text,failing", [
    ("ok\nExit code: 1\n", True),
    ("Process exited with code 127", True),
    ("bash: foo: command not found", True),
    ("Traceback (most recent call last):\n  File x", True),
    ("FAILED tests/test_a.py::test_b - assert 1 == 2", True),
    ("== 3 failed, 10 passed in 2s ==", True),
    ("npm ERR! code E404", True),
    ("src/a.c:3:5: error: expected ';'", True),
    ("Exit code: 0\nall good", False),
    ("== 12 passed in 2.1s ==", False),
    ("== 0 failed, 12 passed ==", False),
    ("def f():\n    return 1\n" * 200, False),
])
def test_what_reads_as_a_failing_step(text, failing):
    assert ctxwin.looks_failed(text) is failing


def test_a_word_in_the_middle_of_a_long_file_is_not_a_failure():
    body = "x = 1\n" * 400 + "raise SyntaxError('in the middle')\n" + "y = 2\n" * 400
    assert not ctxwin.looks_failed(body)


def test_messages_carrying_images_are_untouched():
    big = "x" * 4000
    msgs = _mini(10)
    # (a) an OpenAI tool result that carries an image part
    msgs[3] = {"role": "tool", "tool_call_id": "call_00", "content": [
        {"type": "text", "text": big},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}
    # (b) an Anthropic-style user turn: a text result AND an image block
    msgs.insert(6, {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t", "content": big},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA"}}]})
    out, st = _clear(msgs, keep_recent=4)
    assert out[3] is msgs[3]
    assert out[6] is msgs[6]
    assert st["cleared"] > 0                                      # others still cleared


def test_a_clis_own_compaction_request_is_untouched():
    msgs = _history(70000)
    msgs.append({"role": "user", "content": "Create a detailed summary of the conversation "
                                            "so far, covering files and decisions."})
    out, st = ctxwin.clear_old_tool_results(msgs, est_tokens=70000)
    assert out is msgs and st["skipped"] == "compaction" and st["cleared"] == 0


def test_text_part_content_keeps_its_shape():
    msgs = _mini(10)
    body = msgs[3]["content"]
    msgs[3] = {"role": "tool", "tool_call_id": "call_00",
               "content": [{"type": "text", "text": body}]}
    out, _ = _clear(msgs, keep_recent=4)
    c = out[3]["content"]
    assert isinstance(c, list) and len(c) == 1 and c[0]["type"] == "text"
    assert _is_stub(c[0]["text"])


def test_only_results_longer_than_min_chars_are_cleared():
    msgs = _mini(10, size=2500)
    for idx, n in ((3, 1500), (5, 1501)):
        msgs[idx] = dict(msgs[idx], content="y" * n)
    out, _ = _clear(msgs, keep_recent=2)
    assert out[3] is msgs[3]                                      # exactly min_chars: kept
    assert _is_stub(out[5]["content"])                            # one char over: cleared


def test_a_stub_is_never_longer_than_what_it_replaces():
    msgs = _mini(8, size=300)
    out, st = _clear(msgs, keep_recent=2, min_chars=100)
    assert all(len(b["content"]) <= len(a["content"])
               for a, b in zip(msgs, out) if a.get("role") == "tool")
    assert st["chars_after"] < st["chars_before"] or st["cleared"] == 0


# --------------------------------------------------------------------------- #
# The size gate, the flag, idempotence, stats
# --------------------------------------------------------------------------- #

def test_a_request_under_the_threshold_is_byte_identical():
    small = _mini(14)
    out, st = ctxwin.clear_old_tool_results(small)
    assert out is small and st["skipped"] == "small" and st["cleared"] == 0
    got, info = A._clear_old_results_for_hop(small, TOOLS)
    assert got is small and info is None


def test_the_threshold_is_60000_estimated_tokens():
    assert ctxwin.OLD_RESULT_CLEAR_FROM_TOKENS == 60000
    below = _history(59000)
    above = _history(62000)
    assert A._est_tokens(below, TOOLS) < 60000 <= A._est_tokens(above, TOOLS)
    got, info = A._clear_old_results_for_hop(below, TOOLS)
    assert got is below and info is None
    got, info = A._clear_old_results_for_hop(above, TOOLS)
    assert got is not above and info and info["cleared"] > 0


def test_the_kill_switch_makes_the_hop_byte_identical(monkeypatch):
    big = _history(70000)
    real = A.config.get_flag
    monkeypatch.setattr(A.config, "get_flag",
                        lambda k, d=False: False if k == FLAG else real(k, d))
    got, info = A._clear_old_results_for_hop(big, TOOLS)
    assert got is big and info is None


def test_the_flag_defaults_on():
    seen = []
    real = A.config.get_flag

    def spy(k, d=False):
        if k == FLAG:
            seen.append(d)
        return real(k, d)
    A.config.get_flag = spy
    try:
        A._clear_old_results_for_hop(_history(70000), TOOLS)
    finally:
        A.config.get_flag = real
    assert seen == [True]


def test_clearing_twice_changes_nothing_more():
    big = _history(70000)
    once, info = A._clear_old_results_for_hop(big, TOOLS)
    assert info and info["cleared"] > 0
    twice, info2 = A._clear_old_results_for_hop(once, TOOLS)
    assert twice is once and info2 is None
    out, st = _clear(once)
    assert out is once and st["cleared"] == 0
    mini = _mini(14)
    first, _ = _clear(mini, keep_recent=4)
    second, st2 = _clear(first, keep_recent=4)
    assert second is first and st2["cleared"] == 0 and st2["skipped"] == "nothing"


def test_stats_describe_exactly_what_was_cleared():
    msgs = _mini(14, size=2500)
    out, st = _clear(msgs, keep_recent=4)
    older = [m for m in msgs if m.get("role") == "tool"][:10]
    assert st["cleared"] == 10
    assert st["chars_before"] == sum(len(m["content"]) for m in older)
    assert st["chars_after"] == sum(len(ctxwin.cleared_stub(m["content"])) for m in older)
    assert st["units"] == 15 and st["kept_failing"] == 0 and st["skipped"] is None
    assert sum(len(m["content"]) for m in out if m["role"] == "tool") == \
        sum(len(m["content"]) for m in msgs if m["role"] == "tool") \
        - st["chars_before"] + st["chars_after"]


def test_nonsense_input_is_returned_as_is():
    for junk in (None, [], "text", [None, 3, "x"], [{"role": "tool"}]):
        out, st = _clear(junk)
        assert out is junk and st["cleared"] == 0


def test_a_short_history_is_left_alone():
    msgs = _mini(5)
    out, st = _clear(msgs, keep_recent=8)
    assert out is msgs and st["skipped"] == "short"


# --------------------------------------------------------------------------- #
# Determinism: the cache-stable prefix
# --------------------------------------------------------------------------- #

def test_the_same_history_gives_the_same_bytes():
    big = _history(70000)
    a, _ = A._clear_old_results_for_hop(big, TOOLS)
    b, _ = A._clear_old_results_for_hop(copy.deepcopy(big), TOOLS)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_a_cleared_result_is_cleared_to_the_same_bytes_on_every_later_turn():
    """Grow the conversation turn by turn: whatever turn T cleared stays cleared
    to byte-identical text at every later turn, and everything OLDER than turn
    T's keep window is identical -- so a provider's prompt cache stays valid up
    to the one unit that crosses the window each turn."""
    full = _history(90000)
    rest = full[2:]
    ends = [b for _a, b in ctxwin.unit_spans(rest)]
    turns = [2 + e for e in ends[40::7]]                          # growing prefixes at unit ends
    assert len(turns) >= 4
    outs = []
    for k in turns:
        out, st = ctxwin.clear_old_tool_results(full[:k], min_tokens=0)
        outs.append((k, out, st))
    assert outs[-1][2]["cleared"] > outs[0][2]["cleared"] > 0
    for i, (k1, o1, _s1) in enumerate(outs):
        spans = ctxwin.unit_spans(full[2:k1])
        boundary = 2 + spans[-ctxwin.OLD_RESULT_KEEP_RECENT][0]   # start of the newest window
        for k2, o2, _s2 in outs[i + 1:]:
            assert json.dumps(o1[:boundary]) == json.dumps(o2[:boundary]), \
                "the cleared prefix changed between turns %d and %d" % (k1, k2)
            for j in range(k1):
                if o1[j] is not full[j]:                          # cleared at turn 1 ...
                    assert o2[j] == o1[j]                         # ... identical at turn 2


# --------------------------------------------------------------------------- #
# The measurement (85K-token turn)
# --------------------------------------------------------------------------- #

def test_an_85k_token_turn_gets_much_smaller():
    big = _history(85000)
    got, info = A._clear_old_results_for_hop(big, TOOLS)
    assert info is not None
    assert 80000 <= info["before"] <= 92000
    tok_cut = 1 - info["after"] / float(info["before"])
    b_before = len(json.dumps(got.__class__(big)))
    b_after = len(json.dumps(got))
    byte_cut = 1 - b_after / float(b_before)
    print("85K turn: %d -> %d est tokens (-%.0f%%), %d -> %d bytes (-%.0f%%), %d results cleared"
          % (info["before"], info["after"], 100 * tok_cut, b_before, b_after,
             100 * byte_cut, info["cleared"]))
    assert tok_cut >= 0.35, "only %.0f%% fewer tokens" % (100 * tok_cut)
    assert byte_cut >= 0.35
    assert _skeleton(got) == _skeleton(big)


# --------------------------------------------------------------------------- #
# Wired into the hop: what goes up the wire, and what the CLI is told
# --------------------------------------------------------------------------- #

class _Resp:
    headers = {}

    def __init__(self, status=200, text=""):
        self.status_code = status
        self.text = text

    def json(self):
        return {"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": "OK"}}]}

    def close(self):
        pass

    def iter_lines(self, decode_unicode=False):
        return iter(())


def _hop(monkeypatch, messages, budget=None, tools=TOOLS, stream=False, replies=None):
    """Run one _upstream_chat hop against a fake provider -> (posted body, ratio).
    `replies`: the responses to hand out in order (default: one 200); the LAST
    posted body is returned (a refit re-posts)."""
    posted, queue = [], list(replies or [_Resp()])

    def reply():
        return queue.pop(0) if len(queue) > 1 else queue[0]
    monkeypatch.setattr(A.requests, "post",
                        lambda *a, **kw: posted.append(kw.get("json")) or reply())
    monkeypatch.setattr(A, "_post_with_header_deadline",
                        lambda deadline, post, **kw: posted.append(kw.get("json")) or reply())
    # hermetic: no background recap call (it would route a real model request)
    monkeypatch.setattr(A, "_summarize_dropped", lambda dropped: None)
    if budget:
        monkeypatch.setattr(A, "_model_ctx_info", lambda pid, model: (budget, "catalog"))
    payload = {"model": "open", "_no_craft": True, "messages": messages, "tools": tools}
    with A.app.test_request_context("/v1/chat/completions"):
        A._ctx_begin({"model": "auto"}, messages, A._est_tokens(messages, tools))
        A._upstream_chat("uncloseai", payload, stream)
        ratio = A._ctx_hop_ratio("uncloseai", "open")
    assert posted, "the hop never posted"
    return posted[-1], ratio


@pytest.mark.parametrize("shape", ["chat", "anthropic", "responses"])
def test_every_protocol_sends_the_cleared_conversation(monkeypatch, shape):
    if shape == "chat":
        msgs = _history(80000)
    elif shape == "anthropic":
        msgs = A._anthropic_to_openai_messages(
            {"system": "S", "messages": _anthropic_native(40, size=8000)})
    else:
        msgs = A._responses_to_chat(
            {"instructions": "S", "input": _responses_native(40, size=8000)})
    assert A._est_tokens(msgs, TOOLS) >= 60000
    body, _ratio = _hop(monkeypatch, msgs, budget=400000)
    sent = body["messages"]
    assert sum(_is_stub(t) for t in _tool_texts(sent)) > 5
    assert _skeleton(sent) == _skeleton(msgs)
    assert A._est_tokens(sent, TOOLS) < 0.7 * A._est_tokens(msgs, TOOLS)
    # the newest units went out verbatim
    assert sent[-1] == msgs[-1] and sent[-2] == msgs[-2]


def test_the_flag_off_sends_the_original_messages(monkeypatch):
    real = A.config.get_flag
    monkeypatch.setattr(A.config, "get_flag",
                        lambda k, d=False: False if k == FLAG else real(k, d))
    msgs = _history(80000)
    body, ratio = _hop(monkeypatch, msgs, budget=400000)
    assert body["messages"] == msgs
    assert ratio == 1.0


def test_reported_usage_stays_sized_on_the_request_the_cli_sent(monkeypatch):
    """A hop whose window held the cleared conversation did NOT compact; its
    upstream prompt count is the small one. The CLI must still be told the size
    of the request it sent, or its own compaction never fires."""
    msgs = _history(85000)
    est_orig = A._est_tokens(msgs, TOOLS)
    body, ratio = _hop(monkeypatch, msgs, budget=400000)
    est_sent = A._est_tokens(body["messages"], TOOLS)
    assert est_sent < 0.7 * est_orig and ratio > 1.4
    with A.app.test_request_context("/v1/chat/completions"):
        A._ctx_note_hop("uncloseai", "open", est_orig, est_sent)
        reported = A._reported_prompt_tokens(est_sent, est_orig, "uncloseai", "open")
        assert abs(reported - est_orig) <= 0.03 * est_orig
        # no upstream count at all: the estimate of the ORIGINAL request stands in
        assert A._reported_prompt_tokens(None, est_orig, "uncloseai", "open") == est_orig


def test_reported_usage_composes_with_a_window_fit(monkeypatch):
    """Cleared AND compacted: the ratio is original-vs-sent, not cleared-vs-sent."""
    msgs = _history(85000)
    est_orig = A._est_tokens(msgs, TOOLS)
    body, ratio = _hop(monkeypatch, msgs, budget=24000)
    est_sent = A._est_tokens(body["messages"], TOOLS)
    assert est_sent < 0.3 * est_orig
    assert abs(ratio - est_orig / float(est_sent)) <= 0.08 * ratio


def test_a_streaming_hop_sends_the_same_cleared_body(monkeypatch):
    msgs = _history(85000)
    est_orig = A._est_tokens(msgs, TOOLS)
    body, ratio = _hop(monkeypatch, msgs, budget=400000, stream=True)
    assert sum(_is_stub(t) for t in _tool_texts(body["messages"])) > 5
    assert _skeleton(body["messages"]) == _skeleton(msgs)
    assert ratio > 1.4
    est_sent = A._est_tokens(body["messages"], TOOLS)
    assert abs(ratio - est_orig / float(est_sent)) <= 0.03 * ratio


def test_a_learned_window_refit_keeps_the_usage_sized_on_the_original(monkeypatch):
    """The first pass only cleared; the provider answers 400 and teaches its real
    window; the refit compacts the cleared payload. The ratio must still be the
    ORIGINAL request over what finally went up."""
    monkeypatch.setattr(A, "_MODEL_MAX_INPUT", {})
    monkeypatch.setattr(A, "_MODEL_MAX_OUTPUT", {})
    monkeypatch.setattr(A, "_MODEL_LEARNED_AT", {})
    cf_400 = ('{"errors":[{"message":"AiError: {\\"error\\":{\\"message\\":\\"This '
              "model's maximum context length is 32768 tokens. However, you requested "
              '64 output tokens\\"}}"}]}')
    msgs = _history(85000)
    est_orig = A._est_tokens(msgs, TOOLS)
    body, ratio = _hop(monkeypatch, msgs, replies=[_Resp(400, cf_400), _Resp()])
    est_sent = A._est_tokens(body["messages"], TOOLS)
    assert est_sent < 0.45 * est_orig, "the refit never compacted"
    assert abs(ratio - est_orig / float(est_sent)) <= 0.08 * ratio


def test_the_posted_body_is_smaller_so_the_upload_is_shorter(monkeypatch):
    msgs = _history(85000)
    body, _ = _hop(monkeypatch, msgs, budget=400000)
    assert A._body_bytes(body) < 0.65 * A._body_bytes({"messages": msgs, "tools": TOOLS})


def test_the_input_list_the_handlers_keep_is_never_mutated(monkeypatch):
    msgs = _history(80000)
    snapshot = json.dumps(msgs, sort_keys=True)
    _hop(monkeypatch, msgs, budget=400000)
    assert json.dumps(msgs, sort_keys=True) == snapshot


def test_one_log_line_per_request(caplog):
    big = _history(80000)
    with caplog.at_level(logging.INFO, logger="free-llm-hub"):
        with A.app.test_request_context("/v1/chat/completions"):
            A._clear_old_results_for_hop(big, TOOLS)
            A._clear_old_results_for_hop(big, TOOLS)      # a second hop of the same request
    lines = [r.getMessage() for r in caplog.records if "[ctx] cleared" in r.getMessage()]
    assert len(lines) == 1, lines
    assert re.match(r"^\[ctx\] cleared \d+ old tool results \(\d+ -> \d+ est tokens\)$", lines[0])


# --------------------------------------------------------------------------- #
# Compaction and exact facts still see what was said
# --------------------------------------------------------------------------- #

SECRET_TAIL = "FINAL_LINE_MARKER = 'zebra-4471'"


def _facts_history():
    """A file read whose LAST line is a value a later turn may ask for."""
    first = "\n".join("v%d = %d" % (i, i) for i in range(600)) + "\n" + SECRET_TAIL
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "inspect config.py"},
            _tool_call("c0", "bash", {"command": "cat config.py"}),
            {"role": "tool", "tool_call_id": "c0", "content": first}]
    for i in range(1, 12):
        msgs += [_tool_call("c%d" % i, "bash", {"command": "cat other%d.py" % i}),
                 {"role": "tool", "tool_call_id": "c%d" % i,
                  "content": _code_file(_Lcg(i), 3000, "other%d.py" % i)}]
    msgs.append({"role": "user", "content": "now report the last line of config.py"})
    return msgs


def test_exact_facts_skip_a_cleared_stub():
    msgs = _facts_history()
    cleared, _ = _clear(msgs, keep_recent=4)
    assert _is_stub(cleared[3]["content"])
    assert "config.py" in "\n".join(ctxwin.exact_facts(msgs))            # control: the real read
    facts = "\n".join(ctxwin.exact_facts(cleared))
    assert "config.py" not in facts, "a fact was mined from a stub"
    assert ctxwin.CLEARED_RESULT_PREFIX not in facts


def test_compaction_reads_the_originals_for_facts_and_recap(monkeypatch):
    msgs = _facts_history()
    cleared, _ = _clear(msgs, keep_recent=4)
    originals = {id(c): o for o, c in zip(msgs, cleared) if c is not o}
    assert originals
    seen, mined = [], []
    summ = lambda dropped: seen.append(list(dropped)) or None            # noqa: E731
    real_block = A._exact_facts_block

    def spy(messages, target_tokens=None):
        mined.append(list(messages))
        return real_block(messages, target_tokens)
    monkeypatch.setattr(A, "_exact_facts_block", spy)
    budget = int(A._est_tokens(cleared, None) * 0.7)
    out, did = A._compact_to_budget(cleared, None, budget, summarizer=summ,
                                    originals=originals)
    assert did
    tools_mined = [m["content"] for ms in mined for m in ms if m.get("role") == "tool"]
    assert any(SECRET_TAIL in t for t in tools_mined), \
        "the exact-facts miner never saw the file the CLI read"
    assert not any(_is_stub(t) for t in tools_mined)
    # the recap sees the messages as they were said, not the stubs
    assert seen and not any(_is_stub(m.get("content")) for m in seen[0] if m.get("role") == "tool")
    # without `originals` the compaction behaves exactly as it always did
    mined.clear()
    plain, did2 = A._compact_to_budget(cleared, None, budget)
    assert did2
    assert any(_is_stub(m["content"]) for ms in mined for m in ms if m.get("role") == "tool")
