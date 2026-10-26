"""A relay that dropped the user's turn is kept away from big requests.

MEASURED 2026-10-26 (Build session d9af0686, codex, `coding-max`, ~43K-token turns): the user's
message reached the hub every time, yet g4f/pa:fec3c477:claude-haiku-5-5-anthropic answered three
turns "your message contains only a tool setup". Asked directly (fresh random content on every call,
a code at the very start of the prompt and one just before the question at the end) it often returned
only the first one -- "...the message ends mid-sentence", "the ACT section is cut off". It is NOT
stable and not a fixed cap:

    21:50  tools ON cut at 1.5K-3.5K tokens (500-800 ok); tools OFF cut at 3K (2 of 2)
    22:02  tools OFF ok at 1.5K / 3K / 8K (6/6); tools ON cut at 1.5K / 3K / 8K (6/6);
           tools ON 500 ok, 900-1,200 mixed
    22:08  tools ON ok at 24K (2/2)

g4f.space routes to community-donated servers, so it varies with time (and perhaps with tools). Most
blind answers are plausible prose that no per-reply check can catch, so the hub acts on the ones it
CAN see: a relay hop rejected for `empty_message_claim` gets its server a provisional input ceiling at
once (half the size that failed, 30 min) and a background probe looks for the real one (3 h). The
ceiling is the pair's learned window, which routing, the chain walk, the quality fallback and
compaction already honour. Nothing is probed ahead of a failure: an earlier version probed the first
big request to every relay and spent enough relay budget to get the hub throttled.

Hermetic: the relay is a fake `_upstream_chat` that keeps the first N characters; nothing touches the
network and no thread is left running (`_trunc_start` is captured).
"""
import json
import re
import time

import pytest

import app as A

PAIR = ("g4f", "pa:fec3c477:claude-haiku-5-5-anthropic")
SIBLING = ("g4f", "pa:fec3c477:gpt-5")            # same gateway, another model
OTHER = ("g4f", "srv_mkombumpae45db46dcb8:z-ai/glm-5.3")
KEY = "g4f|pa:fec3c477"


class FakeResp:
    def __init__(self, status=200, text=""):
        self.status_code = status
        self._text = text
        self.headers = {}
        self.text = text

    def json(self):
        return {"choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": self._text}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

    def close(self):
        pass


def relay_keeping(chars, calls=None, tools_only=False, drop_letters=False):
    """A fake `_upstream_chat` for a gateway that keeps the first `chars` characters of the
    prompt (`tools_only`: only of a request that carries tools), and a model that answers the
    format question when it can see it. `drop_letters`: the model answers "HEAD=95031" for the
    code "H95031" (seen live)."""
    def fake(pid, payload, stream, *a, **kw):
        if calls is not None:
            calls.append((pid, payload.get("model"), A._payload_est_tokens(payload),
                          bool(payload.get("tools"))))
        full = "\n".join(str(m.get("content") or "") for m in payload["messages"])
        kept = full[:chars] if (payload.get("tools") or not tools_only) else full
        head = re.search(r"CODE-AT-START: (H\d+)", kept)
        tail = re.search(r"CODE-AT-END: (T\d+)", kept)
        if head and tail and "TAIL=<the CODE-AT-END value>" in kept:
            h, t = head.group(1), tail.group(1)
            if drop_letters:
                h, t = h[1:], t[1:]
            return FakeResp(200, "HEAD=%s TAIL=%s" % (h, t))
        return FakeResp(200, "I received a list of words, but it cuts off mid-sentence and I "
                             "don't see a question.")
    return fake


@pytest.fixture
def probe_on(monkeypatch):
    """The feature on (its own file), its background thread captured instead of started."""
    jobs = []
    monkeypatch.setattr(A, "_trunc_on", lambda: True)
    monkeypatch.setattr(A, "_trunc_start", lambda fn: jobs.append(fn))
    return jobs


# --- the probe ----------------------------------------------------------------------------

def test_the_probe_payload_has_a_code_at_each_end_a_tool_and_the_size_asked():
    payload, head, tail = A._trunc_payload("m", 6000, 7)
    sys_msg, user_msg = payload["messages"]
    assert sys_msg["role"] == "system" and sys_msg["content"].startswith("CODE-AT-START: " + head)
    assert user_msg["role"] == "user" and tail in user_msg["content"]
    assert user_msg["content"].rstrip().endswith("TAIL=<the CODE-AT-END value>")
    assert payload["_no_craft"] is True and payload["stream"] is False
    assert [t["function"]["name"] for t in payload["tools"]] == ["shell_command"]
    est = A._payload_est_tokens(payload)
    assert 5000 <= est <= 7200, est
    lines = sys_msg["content"].splitlines()[1:]
    assert len(set(lines)) == len(lines)                      # nothing repeats
    plain, _h, _t = A._trunc_payload("m", 6000, 7, tools=False)
    assert "tools" not in plain


def test_it_finds_the_ceiling_of_a_gateway_that_keeps_8000_characters(monkeypatch, probe_on):
    calls = []
    monkeypatch.setattr(A, "_upstream_chat", relay_keeping(8000, calls))
    assert A._trunc_probe(*PAIR, 43000) == "ceiling"
    c = A._trunc_ceiling(*PAIR)
    assert c and 1000 <= c <= 2400, c                         # 8000 chars ~ 2000 hub tokens
    assert A._trunc_entry(KEY)["src"] == "probe"
    assert 3 <= len(calls) <= 6, calls                        # a handful of calls, none above the top rung
    assert max(x[2] for x in calls) <= A._TRUNC_TOP * 1.2
    assert all(x[3] for x in calls)                           # every rung carried the tool


def test_a_gateway_that_returns_a_whole_request_is_clean_and_nothing_is_filed(monkeypatch, probe_on):
    """One whole answer proves little about a flaky gateway, so it must not file a 'safe' verdict."""
    calls = []
    monkeypatch.setattr(A, "_upstream_chat", relay_keeping(10 ** 9, calls))
    assert A._trunc_probe(*PAIR, 43000) == "clean"
    assert len(calls) == 1 and calls[0][2] <= A._TRUNC_TOP * 1.2
    assert A._trunc_entry(KEY) is None and A._trunc_ceiling(*PAIR) is None


def test_a_request_smaller_than_the_top_rung_probes_at_its_own_size(monkeypatch, probe_on):
    calls = []
    monkeypatch.setattr(A, "_upstream_chat", relay_keeping(10 ** 9, calls))
    A._trunc_probe(*PAIR, 9000)
    assert 7000 <= calls[0][2] <= 10500


def test_the_probe_can_be_tool_free(monkeypatch, probe_on):
    calls = []
    monkeypatch.setattr(A, "_upstream_chat", relay_keeping(8000, calls, tools_only=True))
    assert A._trunc_probe(*PAIR, 43000, tools=False) == "clean"
    assert not any(x[3] for x in calls)
    assert A._trunc_probe(*PAIR, 43000, tools=True) == "ceiling"


def test_a_model_that_drops_the_letters_of_the_codes_still_passes(monkeypatch, probe_on):
    """Seen live: a model that saw everything answered 'HEAD=95031 TAIL=74272' for H95031 /
    T74272. That is not a cut."""
    monkeypatch.setattr(A, "_upstream_chat", relay_keeping(10 ** 9, drop_letters=True))
    assert A._trunc_probe(*PAIR, 43000) == "clean"


def test_a_model_that_ignores_the_format_records_nothing(monkeypatch, probe_on):
    monkeypatch.setattr(A, "_upstream_chat", lambda *a, **k: FakeResp(200, "hello there"))
    assert A._trunc_probe(*PAIR, 43000) == "unusable"
    assert A._trunc_entry(KEY) is None


@pytest.mark.parametrize("resp", [FakeResp(500, "boom"), FakeResp(200, ""), None])
def test_an_inconclusive_answer_records_nothing(monkeypatch, probe_on, resp):
    def fake(*a, **k):
        if resp is None:
            raise RuntimeError("down")
        return resp
    monkeypatch.setattr(A, "_upstream_chat", fake)
    assert A._trunc_probe(*PAIR, 43000) == "error"
    assert A._trunc_entry(KEY) is None


def test_a_direct_provider_is_never_probed(monkeypatch, probe_on):
    monkeypatch.setattr(A, "_upstream_chat", relay_keeping(100))
    assert A._trunc_probe("nvidia", "z-ai/glm-5.3", 43000) == "error"
    assert not A._TRUNC


# --- what the ceiling does ------------------------------------------------------------------

def test_the_ceiling_is_the_learned_window_of_every_model_behind_the_gateway():
    before = {p: A._model_ctx_info(*p) for p in (PAIR, SIBLING, OTHER, ("nvidia", "z-ai/glm-5.3"))}
    A._trunc_set(KEY, 1500, "probe")
    for p in (PAIR, SIBLING):
        assert A._model_ctx_info(*p) == (1500, "learned"), p
    for p in (OTHER, ("nvidia", "z-ai/glm-5.3")):             # another server / a direct provider
        assert A._model_ctx_info(*p) == before[p], p


def test_a_request_bigger_than_the_ceiling_does_not_fit_that_gateway():
    A._trunc_set(KEY, 1500, "probe")
    assert not A._window_fits(*PAIR, 43000)
    assert not A._window_fits(*SIBLING, 6000)
    assert A._window_fits(*PAIR, 400)                          # a tiny turn still may go there
    assert A._window_fits(*OTHER, 43000)


def test_the_ceiling_never_raises_a_smaller_known_window(monkeypatch):
    monkeypatch.setattr(A, "_model_ctx_info_uncapped", lambda p, m: (1000, "learned"))
    A._trunc_set(KEY, 1500, "probe")
    assert A._model_ctx_info(*PAIR) == (1000, "learned")


def test_entries_expire():
    A._trunc_set(KEY, 1500, "probe", now=1_000_000.0)
    assert A._trunc_entry(KEY, now=1_000_000.0 + A._TRUNC_TTL - 5)
    assert A._trunc_entry(KEY, now=1_000_000.0 + A._TRUNC_TTL + 5) is None
    A._trunc_set(KEY, 1500, "claim", now=1_000_000.0)
    assert A._trunc_entry(KEY, now=1_000_000.0 + A._TRUNC_PROVISIONAL_TTL + 5) is None
    assert A._TRUNC_PROVISIONAL_TTL < A._TRUNC_TTL <= 6 * 3600   # short on purpose: it is a flaky gateway


def test_an_empty_ledger_changes_nothing():
    before = {p: A._model_ctx_info(*p) for p in (PAIR, SIBLING, OTHER)}
    A._trunc_set(KEY, 1500, "probe")
    A._TRUNC.clear()
    assert {p: A._model_ctx_info(*p) for p in (PAIR, SIBLING, OTHER)} == before


def test_flag_off_means_no_ceiling_no_probe_no_entry(monkeypatch):
    A._trunc_set(KEY, 1500, "probe")
    monkeypatch.setattr(A, "_trunc_on", lambda: False)
    assert A._trunc_ceiling(*PAIR) is None
    assert A._model_ctx_info(*PAIR) != (1500, "learned")
    jobs = []
    monkeypatch.setattr(A, "_trunc_start", lambda fn: jobs.append(fn))
    A._TRUNC.clear()
    A._trunc_note_claim(*PAIR, 43000)
    A._note_lost_request(*PAIR, ["empty_message_claim"], 43000)
    assert not A._TRUNC and not jobs


# --- nothing is probed ahead of a failure ---------------------------------------------------------

def big_payload(model=PAIR[1], tokens=20000):
    return {"model": model, "tools": [{"type": "function", "function": {
        "name": "bash", "parameters": {"type": "object"}}}],
        "messages": [{"role": "user", "content": "word " * (tokens * 4 // 5)}]}


def test_a_big_request_to_an_unmeasured_relay_starts_no_probe(monkeypatch, probe_on):
    """The first version probed this case and burned enough relay budget to get the hub throttled."""
    sent = []
    monkeypatch.setattr(A, "_upstream_chat",
                        lambda pid, payload, stream: sent.append(pid) or FakeResp())
    A._dispatch_chat("g4f", big_payload(), False)
    assert sent == ["g4f"] and not probe_on and not A._TRUNC
    assert not hasattr(A, "_trunc_maybe_probe")


# --- a rejected "your message is only a tool setup" ---------------------------------------------

def test_a_rejected_claim_files_a_provisional_ceiling_and_measures(monkeypatch, probe_on):
    A._trunc_note_claim(*PAIR, 43000)
    assert A._trunc_ceiling(*PAIR) == 21500                    # half the size that failed
    assert A._trunc_entry(KEY)["src"] == "claim"
    assert len(probe_on) == 1
    monkeypatch.setattr(A, "_upstream_chat", relay_keeping(8000))
    probe_on[0]()                                              # the real measurement replaces it
    e = A._trunc_entry(KEY)
    assert e["src"] == "probe" and 1000 <= e["ceiling"] <= 2400


def test_a_clean_probe_leaves_the_provisional_ceiling_to_run_out(monkeypatch, probe_on):
    A._trunc_note_claim(*PAIR, 43000)
    monkeypatch.setattr(A, "_upstream_chat", relay_keeping(10 ** 9))
    probe_on[0]()
    e = A._trunc_entry(KEY)
    assert e["src"] == "claim" and e["ceiling"] == 21500
    assert A._trunc_entry(KEY, now=time.time() + A._TRUNC_PROVISIONAL_TTL + 5) is None


def test_a_small_request_that_lost_its_turn_counts_too(probe_on):
    A._trunc_note_claim(*PAIR, 5000)
    assert A._trunc_ceiling(*PAIR) == 2500


def test_a_lower_measured_ceiling_is_not_raised_by_a_later_claim(probe_on):
    A._trunc_set(KEY, 1500, "probe")
    A._trunc_note_claim(*PAIR, 43000)
    assert A._trunc_ceiling(*PAIR) == 1500


def test_note_lost_request_feeds_it_at_any_size(probe_on):
    A._note_lost_request(*PAIR, ["empty_message_claim"], 5000)
    assert A._trunc_ceiling(*PAIR) == 2500
    A._TRUNC.clear()
    A._note_lost_request(*PAIR, ["idle_greeting"], 43000)       # a different reason
    assert not A._TRUNC


def test_note_lost_request_does_nothing_when_the_client_left(monkeypatch, probe_on):
    monkeypatch.setattr(A, "_client_gone", lambda: True)
    A._note_lost_request(*PAIR, ["empty_message_claim"], 43000)
    assert not A._TRUNC and not probe_on


def test_a_direct_provider_claim_files_no_ceiling(probe_on):
    A._trunc_note_claim("nvidia", "z-ai/glm-5.3", 43000)
    assert not A._TRUNC and not probe_on


def test_the_claim_path_remembers_whether_the_failed_request_had_tools(monkeypatch, probe_on):
    seen = []
    monkeypatch.setattr(A, "_trunc_probe", lambda pid, model, est, tools=True: seen.append(tools))
    A._note_lost_request(*PAIR, ["empty_message_claim"], 43000, True)
    probe_on.pop()()
    A._trunc_tried.clear()
    A._note_lost_request(*PAIR, ["empty_message_claim"], 43000, False)
    probe_on.pop()()
    assert seen == [True, False]


def test_probes_are_one_at_a_time_and_not_repeated_in_a_row(probe_on):
    assert A._trunc_spawn(*PAIR, 43000) is True
    assert A._trunc_spawn(*PAIR, 43000) is False               # running
    probe_on[0]()                                              # it ends (no relay faked: "error")
    assert A._trunc_spawn(*PAIR, 43000) is False               # tried a minute ago
    A._trunc_tried.clear()
    assert A._trunc_spawn(*PAIR, 43000) is True


def test_a_failing_probe_start_never_breaks_the_caller(monkeypatch, probe_on):
    def boom(fn):
        raise RuntimeError("no threads")
    monkeypatch.setattr(A, "_trunc_start", boom)
    A._note_lost_request(*PAIR, ["empty_message_claim"], 43000)    # must not raise
    assert A._trunc_ceiling(*PAIR) == 21500


# --- surviving a restart ------------------------------------------------------------------------

def test_the_ceiling_survives_a_restart():
    A._trunc_set(KEY, 1500, "probe")
    A._trunc_set("g4f|srv_other", 900, "claim")
    blob = json.loads(json.dumps(A._ls_pack()))                 # through real JSON
    A._TRUNC.clear()
    assert A._ls_restore(blob) >= 2
    assert A._trunc_ceiling(*PAIR) == 1500
    assert A._trunc_entry("g4f|srv_other")["src"] == "claim"


def test_an_expired_entry_is_not_saved_or_restored():
    A._trunc_set(KEY, 1500, "probe", now=1.0)                   # ancient
    assert not [r for r in A._ls_pack().get("trunc", []) if r[1] == KEY]
    A._TRUNC.clear()
    blob = {"trunc": [["trunc", KEY, {"ceiling": 1500, "at": 1.0, "src": "probe"}]]}
    assert A._ls_restore(blob) == 0 and not A._TRUNC


def test_a_live_entry_is_never_overwritten_by_a_restore():
    A._trunc_set(KEY, 900, "probe")
    blob = {"trunc": [["trunc", KEY, {"ceiling": 5000, "at": time.time(), "src": "probe"}]]}
    assert A._ls_restore(blob) == 0
    assert A._trunc_ceiling(*PAIR) == 900


@pytest.mark.parametrize("row", [["trunc"], ["trunc", KEY], ["trunc", KEY, "x"], ["trunc", "", {}],
                                 ["trunc", KEY, {"ceiling": 0, "at": 9e12, "src": "probe"}],
                                 [1, 2, 3], "junk"])
def test_a_malformed_row_never_costs_the_others(row):
    good = ["trunc", "g4f|srv_ok", {"ceiling": 800, "at": time.time(), "src": "probe"}]
    assert A._ls_restore({"trunc": [row, good]}) == 1
    assert A._trunc_entry("g4f|srv_ok")["ceiling"] == 800


# --- end to end: the symptom the owner saw ------------------------------------------------------

def test_end_to_end_the_claim_that_ended_codex_turns_now_protects_the_next_one(monkeypatch, probe_on):
    """43K-token turns to a gateway that drops the end. The first one is rejected
    (empty_message_claim, the hub's earlier fix); from then on the pair is out of the way of big
    requests, while a small turn can still use it."""
    assert A._window_fits(*PAIR, 43000)                        # nothing known yet
    A._note_lost_request(*PAIR, ["empty_message_claim"], 43000)
    assert not A._window_fits(*PAIR, 43000)                    # protected at once (provisional)
    monkeypatch.setattr(A, "_upstream_chat", relay_keeping(8000))
    probe_on[0]()
    assert not A._window_fits(*PAIR, 43000) and not A._window_fits(*SIBLING, 43000)
    assert A._window_fits(*PAIR, 600)
    assert A._window_fits(*OTHER, 43000)
