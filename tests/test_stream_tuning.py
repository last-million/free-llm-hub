"""Stream gate tuning: early release of clean answers, structured tails kept.

1. The hold-back gate held a clean answer's first 400 chars / 2.5 s before
   releasing any of it. It now releases as soon as the held text reads as a
   normal answer (answer_check.reads_as_answer, >= 80 chars of varied prose
   or code), so first-token latency is ~the time to the first 80 chars. The
   junk it exists for (short brevity answers, glued loops, leaked markers)
   never reads as an answer, so it is still held and judged.
2. The loop checks cut an answer ENDING in 5+ identical lines. Real answers
   do that: checklists, table rows, retry logs, indented program output,
   key: value records, a pytest banner. Structured lines now only count as a
   loop at the token cap (the runaway case), past 16 copies mid-stream, or
   past 50 at a natural stop.

Fakes only, no network.
"""
import json
import time

import pytest

import answer_check as AC
import app


# --------------------------------------------------------------------------- #
# fake upstream
# --------------------------------------------------------------------------- #

def _chunk(content=None, fin=None):
    delta = {} if content is None else {"content": content}
    return {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m",
            "choices": [{"index": 0, "delta": delta, "finish_reason": fin}]}


def _frame(obj):
    return b"data: " + json.dumps(obj).encode("utf-8") + b"\n\n"


def _frames(text, fin="stop", size=4):
    out = [_frame(_chunk(text[i:i + size])) for i in range(0, len(text), size)]
    return out + [_frame(_chunk(fin=fin)), b"data: [DONE]\n\n"]


def _parse(body):
    text, fins = [], []
    for fr in body.split(b"\n\n"):
        fr = fr.strip()
        if not fr.startswith(b"data:") or fr.endswith(b"[DONE]"):
            continue
        c = json.loads(fr[5:])["choices"][0]
        if c.get("delta", {}).get("content"):
            text.append(c["delta"]["content"])
        if c.get("finish_reason"):
            fins.append(c["finish_reason"])
    return "".join(text), fins


def _gate(items, prompt, **kw):
    return app._StreamAnswerGate(iter(items), mode="bytes", hop_pid=None,
                                 hop_model=None, prompt_text=prompt,
                                 last_prompt=prompt, **kw)


PROSE = ("To rotate a log file safely, first copy it aside, then truncate the "
         "original in place so the process keeps its open file handle. Tools "
         "like logrotate do exactly this with the copytruncate option, which "
         "avoids restarting the service while still bounding disk usage.")
CODE = ("Here is a small helper that retries a request:\n\n```python\n"
        "def fetch(url, tries=3):\n    for i in range(tries):\n"
        "        try:\n            return requests.get(url, timeout=5)\n"
        "        except requests.RequestException:\n"
        "            time.sleep(2 ** i)\n    raise RuntimeError(url)\n```\n")


# --------------------------------------------------------------------------- #
# 1. early release
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text", [PROSE, CODE])
def test_clean_prose_and_code_read_as_an_answer(text):
    assert AC.reads_as_answer(text[:120], last_prompt="how do I do this")


@pytest.mark.parametrize("text,prompt", [
    # the live junk samples, padded past the threshold
    ("3324TouchableOpacity_FP$.\n\nActually, the answer is 3324 because 3323 plus "
     "one is 3324 and that is the whole of it really.",
     "What is 3323 plus 1? Answer with only the number."),
    ("3192" * 40, "What is 3191 plus 1?"),
    ("Sure, here it is: " + "3192" * 30, "compute it"),
    # a line loop already under way
    ("I will check the config file now. " * 6, "fix the config"),
    # leaked reasoning / template markers
    ("The answer is 42, as computed above in detail.<think>let me reconsider "
     "the whole question from the start again", "what is the answer"),
    ("The file was updated and the tests pass now on every platform we have."
     "<|im_end|><|im_start|>user", "update the file"),
    # too short to judge
    ("Paris.", "capital of France?"),
])
def test_junk_and_short_text_never_reads_as_an_answer(text, prompt):
    assert not AC.reads_as_answer(text, last_prompt=prompt)


def _slow(frames, gap):
    for fr in frames:
        time.sleep(gap)
        yield fr


def test_clean_answer_is_released_after_its_first_80_chars_not_400():
    # 4 chars per 20 ms: 80 chars at ~0.4 s; the old hold waited for 400
    # chars (2.0 s here) or 2.5 s.
    t0 = time.monotonic()
    first_at = None
    g = _gate(_slow(_frames(PROSE * 2), 0.02), "how do I rotate logs")
    body = []
    for fr in g:
        body.append(fr)
        if first_at is None and b'"content"' in fr:
            first_at = time.monotonic() - t0
    assert first_at is not None and first_at < 1.2, first_at
    text, fins = _parse(b"".join(body))
    assert text == PROSE * 2 and fins == ["stop"] and not g.cut
    assert app._HOLD_EARLY_CHARS == AC.EARLY_MIN_CHARS == 80


def test_pinned_hold_window_keeps_the_old_behaviour():
    g = _gate(_frames(PROSE), "how", hold_chars=10_000, hold_seconds=30)
    assert g._early_min is None
    assert _parse(b"".join(g))[0] == PROSE


def test_brevity_prompt_is_still_held_and_trimmed():
    junk = ("3324TouchableOpacity_FP$.\n\nActually, the answer is 3324 because "
            "3323 plus one is 3324 and that is all there is to say about it.")
    g = _gate(_frames(junk, fin="length"),
              "What is 3323 plus 1? Answer with only the number.")
    text, fins = _parse(b"".join(g))
    assert text == "3324" and fins == ["stop"] and g.cut


def test_glued_loop_without_a_brevity_ask_is_still_cut():
    g = _gate(_frames("Sure! " + "3192" * 150, fin="length"), "compute it")
    text, _fins = _parse(b"".join(g))
    assert g.cut and len(text) < 40


@pytest.mark.parametrize("size", range(1, 9))
@pytest.mark.parametrize("marker", ["<think>", "<|im_end|>", "<tool_call>"])
def test_a_leak_split_across_deltas_never_reaches_the_client(size, marker):
    # After an early release the tail check judges each delta; a marker
    # split over two deltas ("<thi" + "nk>") must not leak its first half.
    ans = PROSE + " The answer is 42." + marker + "let me reconsider the question"
    g = _gate(_frames(ans, size=size), "what is the answer")
    text, _fins = _parse(b"".join(g))
    assert g.cut and marker[:2] not in text and text.startswith(PROSE[:80]), text[-30:]


@pytest.mark.parametrize("size", [1, 3, 4])
def test_ordinary_angle_brackets_pass_unchanged(size):
    ans = (PROSE + " In the template write <div class=\"row\"> and compare a < b "
           "or x <= y; an arrow like <- or a generic List<int> is fine too.")
    g = _gate(_frames(ans, size=size), "explain")
    text, fins = _parse(b"".join(g))
    assert text == ans and fins == ["stop"] and not g.cut


# --------------------------------------------------------------------------- #
# 2. structured tails are content
# --------------------------------------------------------------------------- #
_INTRO = ("Here is where things stand after the migration. Everything below was "
          "checked against the staging database this morning.\n\n")

STRUCTURED_TAILS = {
    "checklist": _INTRO + "Remaining work:\n\n"
    + "- [ ] Add a unit test for this endpoint\n" * 6,
    "bullets": _INTRO + "Owners per module:\n\n" + "- Pending review by the team\n" * 5,
    "pipe_table_no_leading_pipe": _INTRO + "shard | status | owner\n--- | --- | ---\n"
    + "TBD | not started | TBD\n" * 6,
    "retry_log": _INTRO + "Log output:\n\n"
    + "[WARN] Connection refused, retrying in 5s\n" * 7,
    "timed_log": _INTRO + "Access log:\n\n" + "12:00:01 GET /health 200 OK 3ms\n" * 6,
    "unittest": _INTRO + "Test run:\n\n"
    + "test_retry_backoff (tests.test_net.RetryTest) ... ok\n" * 5,
    "indented_output": _INTRO + "The program prints:\n\n"
    + "    Hello from the worker thread\n" * 6,
    "key_value": _INTRO + "Per-region summary:\n\n" + "Status: pending approval\n" * 5,
    "fenced": _INTRO + "Output:\n\n```\n" + "Hello from the worker thread\n" * 9 + "```\n",
}


@pytest.mark.parametrize("name", sorted(STRUCTURED_TAILS))
def test_structured_tail_is_not_cut(name):
    ans = STRUCTURED_TAILS[name]
    assert AC.inspect(ans, prompt_text="status?", finish_reason="stop")["ok"], name
    assert AC.inspect(ans, prompt_text="status?")["ok"], name
    assert AC.inspect_tail(ans, prompt_text="status?") is None, name
    g = _gate(_frames(ans), "status?")
    text, fins = _parse(b"".join(g))
    assert text == ans and fins == ["stop"] and not g.cut, name


def test_structured_block_mid_answer_is_not_cut():
    ans = (_INTRO + "- Pending review by the team\n" * 10
           + "\nThe rest of the plan is unchanged and ships on Friday as agreed.")
    assert AC.inspect(ans, prompt_text="status?")["ok"]


def test_pytest_banner_at_the_cap_is_not_a_separator_run():
    ans = (_INTRO + "tests/test_api.py ....\n\n"
           + "=" * 30 + " 12 passed in 0.43s " + "=" * 30)
    assert AC.inspect(ans, prompt_text="run tests", finish_reason="length")["ok"]
    # ...while a genuine separator run after a repeated token is still junk
    bad = "1573 1573 " + "-" * 30
    assert not AC.inspect(bad, prompt_text="compute", finish_reason="stop")["ok"]


def test_structured_lines_at_the_token_cap_are_still_a_runaway():
    ans = _INTRO + "- Checking the config file again now.\n" * 60
    v = AC.inspect(ans, prompt_text="fix it", finish_reason="length")
    assert not v["ok"] and v["salvage"].count("Checking the config") == 1


def test_structured_runaway_mid_stream_is_cut_after_16_copies():
    line = "- Checking the config file again now.\n"
    assert AC.inspect_tail(_INTRO + line * 12, prompt_text="fix it") is None
    cut = AC.inspect_tail(_INTRO + line * 30, prompt_text="fix it")
    assert cut is not None and cut <= len(_INTRO) + len(line)


def test_fifty_copies_at_a_natural_stop_are_still_junk():
    ans = _INTRO + "- Checking the config file again now.\n" * 60
    assert not AC.inspect(ans, prompt_text="fix it", finish_reason="stop")["ok"]


def test_plain_prose_loop_at_a_natural_stop_is_still_cut():
    ans = _INTRO + "I will now check the configuration file.\n" * 5
    assert not AC.inspect(ans, prompt_text="fix it", finish_reason="stop")["ok"]


@pytest.mark.parametrize("line,want", [
    ("- [ ] Add a test", True), ("12. Ship it", True), ("a) first", True),
    ("TBD | TBD | TBD", True), ("[INFO] started", True), ("PASSED", True),
    ("2026-09-27 10:00 boot", True), ("    indented()", True),
    ("Status: pending", True), ("Build Status: green", True),
    ("tests/test_x.py::test_y", True), ("test_foo ... ok", True),
    ("I will check the config again.", False), ("OK, I will check it.", False),
    ("The answer is: 42", False), ("Hello, World!", False),
])
def test_structured_line_classifier(line, want):
    assert AC._line_is_structured(line) is want
