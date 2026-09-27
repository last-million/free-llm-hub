"""Long-context deadlines and recap fidelity.

MEASURED 2026-09-27: at ~220K real tokens (~650 KB) ONE free hop needed
110-270 s, so a request doing nothing wrong ended in a clean 504 at the flat
240 s request deadline. And codex with a tiny 12K compaction limit (3
compactions in 7 turns) kept a stated preference but gave a file's last line
as ANOTHER file's value -- a summary had merged two files.

  * the request deadline grows with request size (streams stay under the
    clients' ~300 s header timeout);
  * huge requests walk hops MEASURED fast at long context first;
  * compaction carries an EXACT FACTS block, extracted mechanically, next to
    the model-written recap -- and a CLI's own compaction request is handed the
    same block to copy verbatim.
"""
import json
import time

import pytest

import app as A
import ctxwin


class _Resp:
    def __init__(self, status=200, payload=None, chunks=None):
        self.status_code = status
        self._payload = payload or {}
        self._chunks = chunks
        self.headers = {}
        self.text = ""

    def json(self):
        return self._payload

    def close(self):
        pass

    def iter_content(self, chunk_size=None):
        return iter(self._chunks or ())

    def iter_lines(self, decode_unicode=False):
        return iter(self._chunks or ())


def _answer(text="42"):
    return {"choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}]}


@pytest.fixture
def quiet(monkeypatch):
    for name in ("_record_chat_usage", "_record_outcome", "_save_perf_stats",
                 "_act_pick", "_note_ttft", "_record_stream_outcome",
                 "_note_provider_timeout", "_throttle_failed_hop"):
        monkeypatch.setattr(A, name, lambda *a, **k: None)
    monkeypatch.setattr(A, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(A, "_model_block_reason", lambda pid, m: None)
    monkeypatch.setattr(A, "_is_provider_dead", lambda pid: False)
    yield


@pytest.fixture
def clean_ledger():
    A._long_ctx_speed.clear()
    yield
    A._long_ctx_speed.clear()


def _defaults(monkeypatch, **over):
    monkeypatch.setattr(A.config, "get_setting",
                        lambda k, d=None: over.get(k, d))


# --------------------------------------------------------------------------- #
# 1. The deadline scales with the request
# --------------------------------------------------------------------------- #

def test_a_small_request_keeps_the_base_deadline(monkeypatch):
    _defaults(monkeypatch)
    assert A._scaled_request_deadline(0) == 240
    assert A._scaled_request_deadline(12000) == 240
    assert A._scaled_request_deadline(A.LONG_DEADLINE_FROM_TOKENS) == 240


def test_a_220k_request_gets_room_for_a_second_slow_hop(monkeypatch):
    """220K tokens: 160K over the threshold = 16 blocks x 15 s = +240 s. The
    measured single-hop worst case (270 s) now fits with room for a second."""
    _defaults(monkeypatch)
    secs = A._scaled_request_deadline(220000)
    assert secs == 240 + 16 * 15 == 480
    assert secs > 270


def test_the_scaled_deadline_is_capped_and_configurable(monkeypatch):
    _defaults(monkeypatch)
    assert A._scaled_request_deadline(2000000) == 600
    _defaults(monkeypatch, request_deadline_per_10k_seconds=30,
              request_deadline_max_seconds=900)
    assert A._scaled_request_deadline(220000) == 240 + 16 * 30
    assert A._scaled_request_deadline(2000000) == 900
    # A cap below the base never SHRINKS the deadline.
    _defaults(monkeypatch, request_deadline_max_seconds=100)
    assert A._scaled_request_deadline(220000) == 240


def test_a_stream_keeps_its_header_limit(monkeypatch):
    """The hub withholds a stream's 200 until content: clients give up on
    headers at ~300 s, so a stream never gets a deadline past that."""
    _defaults(monkeypatch)
    assert A._scaled_request_deadline(220000, stream=True) == A.LONG_DEADLINE_STREAM_MAX
    assert A.LONG_DEADLINE_STREAM_MAX < 300
    assert A._scaled_request_deadline(70000, stream=True) == 255
    assert A._scaled_request_deadline(12000, stream=True) == 240


def test_unbounded_stays_unbounded(monkeypatch):
    _defaults(monkeypatch, request_deadline_seconds=0)
    assert A._scaled_request_deadline(220000) is None


def test_the_clock_extends_from_the_same_start_and_never_shrinks(monkeypatch):
    _defaults(monkeypatch)
    with A.app.test_request_context():
        first = A._begin_request_deadline()
        started = A.g.hub_deadline_started
        assert first == pytest.approx(started + 240)
        time.sleep(0.02)
        longer = A._begin_request_deadline(220000)
        assert longer == pytest.approx(started + 480)      # from the SAME start
        assert A._request_deadline_limit() == pytest.approx(480)
        # A retry re-entry (small est or none) cannot shorten it or restart it.
        assert A._begin_request_deadline() == longer
        assert A._begin_request_deadline(1000) == longer


def test_the_chain_clock_carries_the_scaled_limit(monkeypatch):
    _defaults(monkeypatch)
    with A.app.test_request_context():
        c = A._ChainClock(est=220000, stream=False)
        assert c.limit == pytest.approx(480)
        assert 470 < c.left() <= 480
    with A.app.test_request_context():
        c = A._ChainClock()
        assert c.limit == 240


def _long_messages(tokens):
    # ~4 chars per token, varied words so no loop/junk heuristic trips.
    words = " ".join("w%d" % i for i in range(tokens))
    return [{"role": "user", "content": "refactor the parser module. " + words}]


def _slow_answer(delay):
    calls = []

    def dispatch(pid, payload, stream):
        calls.append(pid)
        time.sleep(delay)
        return _Resp(200, _answer("done: the parser module is refactored now."))
    return dispatch, calls


def test_a_long_request_is_not_cut_at_the_base_deadline(quiet, monkeypatch):
    """End to end on /v1/chat/completions: a hop slower than the BASE deadline
    answers a long request; the identical hop on a short request is the old
    clean 504 (control)."""
    monkeypatch.setattr(A, "_request_deadline_seconds", lambda: 0.4)
    monkeypatch.setattr(A, "_long_deadline_setting",
                        lambda k, d: {"request_deadline_per_10k_seconds": 0.5,
                                      "request_deadline_max_seconds": 5}.get(k, d))
    monkeypatch.setattr(A, "_is_trivial_turn", lambda *a, **k: False)
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("p1", "m1", "hard"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [("p1", "m1"), ("p2", "m2")])
    monkeypatch.setattr(A.crews, "looks_like_full_project", lambda *a, **k: False)
    dispatch, calls = _slow_answer(0.9)
    monkeypatch.setattr(A, "_dispatch_chat", dispatch)
    client = A.app.test_client()

    long_msgs = _long_messages(90000)
    assert A._est_tokens(long_msgs) > A.LONG_DEADLINE_FROM_TOKENS + 20000
    r = client.post("/v1/chat/completions", json={
        "model": "auto", "stream": False, "messages": long_msgs})
    assert r.status_code == 200, r.get_data()[:300]
    assert "refactored" in r.get_json()["choices"][0]["message"]["content"]

    calls.clear()
    r = client.post("/v1/chat/completions", json={
        "model": "auto", "stream": False,
        "messages": [{"role": "user", "content": "refactor the parser module"}]})
    assert r.status_code == 504
    assert r.headers.get("X-Free-LLM-Hub-Last-Error") == "deadline"


# --------------------------------------------------------------------------- #
# 2. Fast long-context models first
# --------------------------------------------------------------------------- #

def test_long_context_speed_is_recorded_only_for_long_requests(clean_ledger):
    A._record_long_ctx_speed("p", "m", 5000, 1000.0)
    assert ("p", "m") not in A._long_ctx_speed
    A._record_long_ctx_speed("p", "m", 200000, 150000.0)
    assert A._long_ctx_band("p", "m") == 2
    assert A._long_ctx_band("q", "unmeasured") == 1
    for _ in range(3):
        A._record_long_ctx_speed("f", "fast", 200000, 20000.0)
    assert A._long_ctx_band("f", "fast") == 0
    # The LATEST long request stalling outweighs an older fast history.
    A._record_long_ctx_speed("f", "fast", 200000, 61000.0, stalled=True)
    assert A._long_ctx_band("f", "fast") == 2


def test_note_ttft_feeds_the_long_ledger(clean_ledger, monkeypatch):
    monkeypatch.setattr(A, "_record_ttft", lambda *a, **k: None)
    resp = _Resp()
    resp._hub_started = time.perf_counter() - 30.0
    resp._hub_est_tokens = 150000
    A._note_ttft(resp, "p", "m")
    rows = A._long_ctx_speed[("p", "m")]
    assert len(rows) == 1 and 29000 < rows[0][1] < 40000 and rows[0][2] is False


def test_a_long_hop_that_stalls_is_measured_slow(clean_ledger):
    with A.app.test_request_context():
        c = A._ChainClock(est=200000)
        c._hop_started = time.monotonic() - 70.0      # waited 70 s for nothing
        c.note_peek("p", "slowpoke", "timeout")
        c._hop_started = time.monotonic() - 10.0      # squeezed hop: no evidence
        c.note_peek("p", "squeezed", "timeout")
    assert A._long_ctx_band("p", "slowpoke") == 2
    assert ("p", "squeezed") not in A._long_ctx_speed


def test_a_huge_request_walks_measured_fast_models_first(clean_ledger):
    chain = [("a", "slow-strong"), ("b", "unknown"), ("c", "fast-one"),
             ("d", "gemma-3-27b")]
    A._record_long_ctx_speed("a", "slow-strong", 200000, 200000.0)
    A._record_long_ctx_speed("c", "fast-one", 200000, 15000.0)
    A._record_long_ctx_speed("d", "gemma-3-27b", 200000, 5000.0)
    out = A._prefer_fast_long_context(chain, 200000)
    # fast first, unmeasured next, measured slow after; the last-resort
    # family stays the TAIL however fast it is.
    assert out == [("c", "fast-one"), ("b", "unknown"), ("a", "slow-strong"),
                   ("d", "gemma-3-27b")]
    # A pinned model keeps hop one.
    assert A._prefer_fast_long_context(chain, 200000, keep_head=1)[0] == ("a", "slow-strong")
    # Below the threshold nothing moves.
    assert A._prefer_fast_long_context(chain, 20000) == chain


def test_no_long_context_evidence_changes_nothing(clean_ledger):
    chain = [("a", "x"), ("b", "y")]
    assert A._prefer_fast_long_context(chain, 300000) is chain


# --------------------------------------------------------------------------- #
# 3. Exact facts
# --------------------------------------------------------------------------- #

def _call(cid, cmd):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"id": cid, "type": "function", "function": {
                "name": "shell",
                "arguments": json.dumps({"command": ["bash", "-lc", cmd]})}}]}


def _out(cid, text):
    return {"role": "tool", "tool_call_id": cid,
            "content": "Exit code: 0\nWall time: 0.1 seconds\nOutput:\n" + text}


def _codex_session():
    """The misquote shape: two files, each with its own last line."""
    return [
        {"role": "user", "content": "<environment_context>cwd</environment_context>"
                                    "I prefer tabs over spaces. Always answer in English."},
        _call("c1", "cat notes/alpha.txt"),
        _out("c1", "alpha first\nalpha middle\nALPHA-LAST-7731\n"),
        _call("c2", "cat notes/beta.txt"),
        _out("c2", "beta first\nBETA-LAST-2209\n"),
        {"role": "assistant", "content": "Both files read."},
    ]


def test_each_file_keeps_its_own_last_line():
    facts = ctxwin.exact_facts(_codex_session())
    alpha = [f for f in facts if f.startswith("notes/alpha.txt")]
    beta = [f for f in facts if f.startswith("notes/beta.txt")]
    assert len(alpha) == 1 and len(beta) == 1
    assert 'last "ALPHA-LAST-7731"' in alpha[0] and "BETA" not in alpha[0]
    assert 'last "BETA-LAST-2209"' in beta[0] and "ALPHA" not in beta[0]
    assert "cat notes/alpha.txt" in alpha[0]            # how it was obtained
    # The user's stated preference, verbatim; the wrapper block is not a fact.
    assert 'user said: "I prefer tabs over spaces."' in facts
    assert not any("environment_context" in f or f.endswith('"cwd"') for f in facts)


def test_a_read_tool_is_attributed_to_its_path_argument():
    """Claude Code's Read: {"file_path": ...}, output numbered "     1→..."."""
    facts = ctxwin.exact_facts([
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "r1", "type": "function", "function": {
                "name": "Read", "arguments": json.dumps({"file_path": "/w/src/b.py"})}}]},
        {"role": "tool", "tool_call_id": "r1",
         "content": "     1→import os\n     2→VALUE = 17\n"}])
    assert facts == ['/w/src/b.py (read): 2 lines; first "import os"; last "VALUE = 17"']


def test_ambiguous_output_is_never_attributed():
    facts = ctxwin.exact_facts([
        _call("c1", "cat a.txt b.txt"), _out("c1", "one\ntwo\n")])
    assert facts == []


def test_multi_file_sections_are_split_per_file():
    facts = ctxwin.exact_facts([
        _call("c1", "tail -n 1 a.txt b.txt"),
        _out("c1", "==> a.txt <==\nA-END\n\n==> b.txt <==\nB-END\n")])
    assert any(f.startswith("a.txt") and '"A-END"' in f for f in facts)
    assert any(f.startswith("b.txt") and '"B-END"' in f for f in facts)


def test_writes_are_recorded_and_a_rewrite_replaces_stale_reads():
    msgs = [
        _call("c1", "cat cfg.ini"), _out("c1", "port=1\n"),
        _call("c2", "cat > cfg.ini <<'EOF'\nport=8787\nmode=fast\nEOF"),
        _out("c2", ""),
        _call("c3", "echo 'tail line' >> cfg.ini"), _out("c3", ""),
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "w1", "type": "function", "function": {
                "name": "write_file",
                "arguments": json.dumps({"path": "src/app.py",
                                         "content": "import os\nprint('v2')\n"})}}]},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "p1", "type": "function", "function": {
                "name": "apply_patch",
                "arguments": json.dumps({"input": "*** Begin Patch\n*** Add File: docs/x.md\n"
                                                  "+# Title\n+final words\n*** End Patch"})}}]},
    ]
    facts = ctxwin.exact_facts(msgs)
    cfg = [f for f in facts if f.startswith("cfg.ini")]
    assert not any('"port=1"' in f for f in cfg), "a rewrite must drop the stale read"
    assert any("(written)" in f and 'last "mode=fast"' in f for f in cfg)
    assert any('(appended): "tail line"' in f for f in cfg)
    assert any(f.startswith("src/app.py (written)") and "print('v2')" in f for f in facts)
    assert any(f.startswith("docs/x.md (written)") and '"final words"' in f for f in facts)


def test_facts_are_bounded_and_the_user_share_survives_a_read_burst():
    msgs = [{"role": "user", "content": "My codename is ORCHID-9."}]
    for i in range(200):
        msgs += [_call("c%d" % i, "cat f%d.txt" % i), _out("c%d" % i, "value %d\n" % i)]
    facts = ctxwin.exact_facts(msgs, max_chars=1500)
    assert sum(len(f) + 3 for f in facts) <= 1500
    assert 'user said: "My codename is ORCHID-9."' in facts
    assert any(f.startswith("f199.txt") for f in facts), "the newest facts win"
    assert not any(f.startswith("f0.txt") for f in facts)
    assert len([f for f in facts if f.startswith("f")]) <= ctxwin.EXACT_FACTS_MAX_FILE


def test_a_standing_rule_outlives_a_run_of_per_step_values():
    msgs = [{"role": "user", "content": "I prefer tabs over spaces."}]
    msgs += [{"role": "user", "content": "Set the retry count to %d." % i} for i in range(60)]
    facts = ctxwin.exact_facts(msgs)
    assert facts[0] == 'user said: "I prefer tabs over spaces."'
    assert 'user said: "Set the retry count to 59."' in facts
    assert 'user said: "Set the retry count to 3."' not in facts
    assert len(facts) <= ctxwin.EXACT_FACTS_MAX_USER


@pytest.mark.parametrize("said", [
    "My favourite colour is teal.",
    "My favorite editor is helix.",
    "My dog's name is Biscuit.",
    "My email is dana at example dot org.",
])
def test_a_plain_personal_fact_is_kept(said):
    """No digit, no "remember": "My favourite colour is teal." was dropped."""
    facts = ctxwin.exact_facts([{"role": "user", "content": said + " Now read big1.txt."}])
    assert 'user said: "%s"' % said in facts


def test_a_favourite_outlives_a_run_of_per_step_values():
    msgs = [{"role": "user", "content": "My favourite colour is teal."}]
    msgs += [{"role": "user", "content": "Set the retry count to %d." % i} for i in range(60)]
    assert 'user said: "My favourite colour is teal."' in ctxwin.exact_facts(msgs)


@pytest.mark.parametrize("said", ["My code is broken", "Fix my tests please"])
def test_ordinary_sentences_about_my_things_are_not_facts(said):
    assert ctxwin.exact_facts([{"role": "user", "content": said}]) == []


def test_a_cli_summary_is_not_mined_but_its_exact_block_is_carried():
    """After codex's own compaction the history holds its SUMMARY (a paraphrase)
    -- which may contain the hub's block from the previous compaction."""
    prior = ctxwin.format_exact_facts(ctxwin.exact_facts(_codex_session()))
    summary = ("Another language model started to solve this problem and produced "
               "a summary of its thinking process. The alpha file ends with "
               "BETA-LAST-2209.\n\n" + prior)
    facts = ctxwin.exact_facts([{"role": "user", "content": summary}])
    assert any(f.startswith("notes/alpha.txt") and "ALPHA-LAST-7731" in f for f in facts)
    assert not any("alpha file ends with" in f for f in facts), "paraphrase is not a fact"
    assert 'user said: "I prefer tabs over spaces."' in facts


def test_compaction_puts_the_exact_block_next_to_the_recap():
    msgs = [{"role": "system", "content": "sys"}] + _codex_session()
    for i in range(30):
        msgs += [{"role": "user", "content": "step %d " % i + "x" * 800},
                 {"role": "assistant", "content": "ok %d " % i + "y" * 800}]
    msgs.append({"role": "user", "content": "What is the last line of notes/alpha.txt?"})
    out, did = A._compact_to_budget(msgs, None, 4000,
                                    summarizer=lambda d: "GOAL: files. alpha ends BETA")
    assert did is True
    i_note = next(i for i, m in enumerate(out)
                  if m.get("role") == "system" and "earlier turns" in m["content"])
    note, facts = out[i_note]["content"], out[i_note + 1]
    assert "[Recap of the dropped turns]" in note
    assert ctxwin.EXACT_FACTS_MARKER not in note, "beside the summary, never inside it"
    assert facts["role"] == "system"
    assert facts["content"].startswith("[" + ctxwin.EXACT_FACTS_MARKER)
    assert 'notes/alpha.txt (via `cat notes/alpha.txt`): 3 lines; ' \
           'first "alpha first"; last "ALPHA-LAST-7731"' in facts["content"]
    assert "omitted by the hub" not in facts["content"]
    # Still fits: the room for the block was reserved, not taken from nothing.
    assert A._est_tokens(out) <= int(4000 * 0.85) + 50


def test_the_switch_turns_the_block_off(monkeypatch):
    monkeypatch.setattr(A.config, "get_flag",
                        lambda k, d=None: False if k == "compact_exact_facts" else d)
    assert A._exact_facts_block(_codex_session()) == ""


_CODEX_COMPACT = ("You are performing a CONTEXT CHECKPOINT COMPACTION. Create a "
                  "handoff summary for another LLM that will resume the task.")


def test_a_cli_compaction_request_is_handed_the_facts_to_copy():
    msgs = _codex_session() + [{"role": "user", "content": _CODEX_COMPACT}]
    out = A._with_cli_compaction_facts(msgs)
    assert out is not msgs and msgs[-1]["content"] == _CODEX_COMPACT   # no mutation
    last = out[-1]["content"]
    assert last.startswith(_CODEX_COMPACT)
    assert "UNCHANGED" in last and ctxwin.EXACT_FACTS_MARKER in last
    assert "ALPHA-LAST-7731" in last and "BETA-LAST-2209" in last
    # Idempotent per hop, and ordinary turns are never touched.
    assert A._with_cli_compaction_facts(out) is out
    plain = _codex_session() + [{"role": "user", "content": "go on"}]
    assert A._with_cli_compaction_facts(plain) is plain


def test_list_content_gets_a_text_part():
    msgs = _codex_session() + [{"role": "user", "content": [
        {"type": "text", "text": _CODEX_COMPACT}]}]
    out = A._with_cli_compaction_facts(msgs)
    parts = out[-1]["content"]
    assert parts[0]["text"] == _CODEX_COMPACT and "ALPHA-LAST-7731" in parts[-1]["text"]


def test_the_rolling_recap_files_its_exact_facts(monkeypatch):
    store = ctxwin.RecapStore(lambda: None)
    monkeypatch.setattr(A, "_recap_store", store)
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("p", "m", "medium"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [("p", "m")])
    monkeypatch.setattr(A, "_dispatch_chat",
                        lambda pid, payload, stream: _Resp(200, _answer("RECAP: two files")))
    exact = ctxwin.exact_facts(_codex_session())
    A._summarize_worker("k-exact", "x" * 900, conv="conv-exact", head="h", last="l",
                        n=6, exact=exact)
    assert store.get("conv-exact")["exact"] == exact
    text = A._conversation_recap("conv-exact")
    assert text.startswith("RECAP: two files")
    assert "ALPHA-LAST-7731" in text and ctxwin.EXACT_FACTS_MARKER in text
