"""A pipeline answer is COMPLETE, or it says what is missing.

LIVE EVIDENCE, 2026-09-27:

A) model "swarm", subscription manager sub-claude/sonnet, a request that
   NUMBERED three deliverables (tally.py, a pytest file with >= 5 tests, a
   README section with a flags table). After 359 s the header read
   "plan=tally.py implementation; ... timed_out=1" and the answer was tally.py
   alone. The plan had MORE phases -- trailer_summary listed only the finished
   ones -- but the flat 180 s cap was spent on plan + phase 1 + its verdict
   (a subscription CLI call takes 30-150 s), the second wave never started,
   review was skipped, and one finished phase shipped as "the answer".
B) a trivial tool turn on auto 503'd after 117.5 s with an NVIDIA NIM body
   ({"detail": "Function '4df48b4f-...': Not found for account"}) relayed to
   the client; the 10-hop chain was 8 nvidia + 2 dahl.
C) "What is N plus 1? Answer with only the number." answered "5064": no
   salvage / stub retry fired in that window, but the non-stream brevity trim
   could cut "50648 ..." to "5064" under a tiny caller budget.

No network, no real CLI: every model call is a stub.
"""
import json
import threading
import time
from unittest import mock

import pytest
from flask import g

import app as A
import swarm


# --------------------------------------------------------------------------- #
# The request, the plans and the workers
# --------------------------------------------------------------------------- #

TALLY = ("Build a tiny CLI. Deliver: 1) the full tally.py using argparse with flags "
         "-l,-w,-c that print line, word and character counts of a file 2) a pytest "
         "file test_tally.py with at least 5 tests covering each flag 3) a README "
         "section with a description, a usage block, a markdown table of the 3 flags "
         "and one example. Every part must be consistent.")

CODE = ("```python\nimport argparse\n\ndef main():\n    p = argparse.ArgumentParser()\n"
        "    p.add_argument('-l')\n```")
TESTS = "```python\n" + "".join("def test_%d():\n    assert True\n\n" % i
                                for i in range(5)) + "```"
README = ("## tally\nCounts things.\n\n```\ntally -l file.txt\n```\n\n"
          "| Flag | Meaning |\n|---|---|\n| -l | lines |\n| -w | words |\n"
          "| -c | chars |\n\nExample: `tally -w notes.txt`")

PLAN_CODE_ONLY = {"goal": "tally", "phases": [
    {"title": "tally.py implementation",
     "task": "Implement tally.py with argparse flags -l -w -c printing line, word "
             "and character counts", "needs": []},
    {"title": "Packaging hint", "task": "One pyproject hint line", "needs": []}]}

PLAN_FULL = {"goal": "tally", "phases": [
    {"title": "tally.py implementation",
     "task": "Implement tally.py with argparse flags -l -w -c printing line, word "
             "and character counts", "needs": []},
    {"title": "Tests", "task": "Write test_tally.py: pytest, at least 5 tests",
     "needs": [1]},
    {"title": "README section",
     "task": "README section: description, usage block, markdown table of the "
             "flags, one example", "needs": [1]}]}


def _msgs(content):
    return [{"role": "user", "content": content}]


def _user(msgs):
    return msgs[-1]["content"]


def _kind(msgs):
    system, user = msgs[0]["content"], _user(msgs)
    if system == swarm._PLAN_SYSTEM:
        return "plan"
    if "REQUIRED PARTS the user enumerated" in system:
        return "parts-verify"
    if "YOUR PHASE (" in user or "\nYOUR TASK: " in user:
        return "worker"
    if "WHAT THE TEAM PRODUCED" in user:
        return "supervise"
    if "PHASE OUTPUTS" in user:
        return "synth"
    if user.startswith("BRIEF\n"):
        return "review"
    return "other"


def _worker_text(user):
    head = user.split("THE USER'S REQUEST", 1)[0]
    if "REQUIRED PART" in head and "README" in head:
        return README
    if "REQUIRED PART" in head and "pytest" in head:
        return TESTS
    if "Tests" in head or "pytest file" in head:
        return TESTS
    if "README" in head:
        return README
    if "Packaging" in head:
        return "pip install ."
    return CODE


def _echo_synth(user):
    body = user.split("PHASE OUTPUTS\n", 1)[1]
    return body.split("\n\nREVIEWER PROBLEMS", 1)[0]


class Fleet:
    """A free-model dispatch keyed on the stage it is asked for."""

    def __init__(self, plans, worker=None, synth=None, slow=None, verify=None):
        self.plans = list(plans)
        self.worker = worker or _worker_text
        self.synth = synth or _echo_synth
        self.slow = slow or {}
        self.verify = verify
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, msgs, max_tokens, exclude_pids=()):
        kind = _kind(msgs)
        with self.lock:
            self.calls.append(kind)
        if kind == "plan":
            plan = self.plans.pop(0) if len(self.plans) > 1 else self.plans[0]
            return json.dumps(plan), "free/planner"
        if kind == "worker":
            user = _user(msgs)
            for title, secs in self.slow.items():
                if "): %s\n" % title in user:
                    time.sleep(secs)
            return self.worker(user), "free/worker"
        if kind == "parts-verify":
            return json.dumps(self.verify or {"missing": []}), "free/checker"
        if kind == "supervise":
            return '{"missing": []}', "free/sup"
        if kind == "review":
            return '{"verdict": "ship", "problems": []}', "other/rev"
        if kind == "synth":
            return self.synth(_user(msgs)), "free/synth"
        return "", None


# --------------------------------------------------------------------------- #
# A. Enumerated parts
# --------------------------------------------------------------------------- #

def test_the_three_numbered_deliverables_are_found():
    parts = swarm.required_parts(TALLY)
    assert len(parts) == 3
    assert parts[0].startswith("the full tally.py")
    assert "test_tally.py" in parts[1]
    assert "markdown table" in parts[2]
    assert "Every part" not in parts[2], "a sentence about ALL parts is not part 3"


@pytest.mark.parametrize("text,n", [
    ("Landing page copy. Deliver:\n- hero headline\n- about paragraph\n- 6 menu "
     "descriptions\n\nThanks", 3),
    ("Write a README section. Include: a one-paragraph description, a usage block, "
     "a markdown table of exactly 3 flags (-l, -w, -c) with descriptions, and one "
     "example command with its output.", 4),
    ("(1) a login form (2) a signup form", 2),
])
def test_other_enumerations_are_found(text, n):
    assert len(swarm.required_parts(text)) == n


@pytest.mark.parametrize("text", [
    "What is 1.5 plus 2? Answer with only the number.",
    "Build me a website for my bakery with a menu and a contact form.",
    "Explain step 2. of the install",
    "",
])
def test_prose_is_not_a_checklist(text):
    assert swarm.required_parts(text) == []


def test_markers_prove_absence_only_on_hard_evidence():
    parts = swarm.required_parts(TALLY)
    assert [swarm._part_present(p, CODE) for p in parts] == [True, False, False]
    assert swarm._part_present(parts[1], TESTS) is True
    assert swarm._part_present(parts[2], README) is True
    assert swarm._part_present("a hero headline", "anything") is None


def test_a_plan_missing_parts_gets_one_reask_then_added_phases():
    fleet = Fleet([PLAN_CODE_ONLY])          # the re-ask returns the same gap
    out = swarm.run(_msgs(TALLY), fleet)
    assert fleet.calls.count("plan") == 2, "exactly ONE coverage re-ask"
    added = [t for t in out["planned"] if t.startswith("Part ")]
    assert len(added) == 2 and added[0].startswith("Part 2") and added[1].startswith("Part 3")
    assert "def test_" in out["text"] and "| -l | lines |" in out["text"]
    assert "unfinished" not in out and "Not finished" not in out["text"]


def test_a_reask_that_covers_everything_is_adopted():
    fleet = Fleet([PLAN_CODE_ONLY, PLAN_FULL])
    out = swarm.run(_msgs(TALLY), fleet)
    assert out["planned"] == ["tally.py implementation", "Tests", "README section"]
    assert ("plan:coverage", "free/planner") in out["models"]


def test_with_a_manager_the_parts_are_added_without_a_second_paid_call():
    paid = []

    def manager(msgs, max_tokens, purpose):
        paid.append(purpose)
        if purpose == "plan":
            return json.dumps(PLAN_CODE_ONLY), "sub/mgr", 10
        if purpose == "verify":
            return '{"ok": true}', "sub/mgr", 5
        if purpose == "supervise":
            return '{"missing": []}', "sub/mgr", 5
        if purpose == "review":
            return '{"verdict": "ship", "problems": []}', "sub/mgr", 5
        return "", None, 0
    fleet = Fleet([PLAN_CODE_ONLY])
    out = swarm.run(_msgs(TALLY), fleet, manager=manager)
    assert paid.count("plan") == 1 and "plan" not in fleet.calls
    assert len(out["planned"]) == 4
    assert "def test_" in out["text"] and "| -l |" in out["text"]


# --------------------------------------------------------------------------- #
# A. The budget follows the plan and the manager's measured latency
# --------------------------------------------------------------------------- #

def test_the_cap_grows_per_phase_beyond_two():
    out = swarm.run(_msgs(TALLY), Fleet([PLAN_FULL]), max_seconds=5,
                    seconds_per_phase=10)
    assert out["cap_seconds"] == 15.0          # 5 + 10 x (3 - 2)


def test_the_cap_grows_by_the_managers_measured_latency_and_stays_bounded():
    def slow_manager(msgs, max_tokens, purpose):
        time.sleep(0.2)
        if purpose == "plan":
            return json.dumps(PLAN_FULL), "sub/mgr", 10
        if purpose == "verify":
            return '{"ok": true}', "sub/mgr", 5
        if purpose == "review":
            return '{"verdict": "ship", "problems": []}', "sub/mgr", 5
        return '{"missing": []}', "sub/mgr", 5
    out = swarm.run(_msgs(TALLY), Fleet([PLAN_FULL]), max_seconds=5,
                    manager=slow_manager)
    # plan + one verdict per wave (2 waves) + supervise + review + the parts
    # verdict = 6 manager calls at ~0.2 s each
    assert 6.1 <= out["cap_seconds"] <= 6.7
    capped = swarm.run(_msgs(TALLY), Fleet([PLAN_FULL]), max_seconds=5,
                       seconds_per_phase=100, max_seconds_ceiling=7,
                       manager=slow_manager)
    assert capped["cap_seconds"] == 7.0


def test_without_scaling_kwargs_the_cap_is_exactly_the_base():
    out = swarm.run(_msgs(TALLY), Fleet([PLAN_FULL]), max_seconds=30)
    assert out["cap_seconds"] == 30.0


# --------------------------------------------------------------------------- #
# A. Past the cap: finish in parallel with fast models, or say what is missing
# --------------------------------------------------------------------------- #

def test_the_evidence_run_now_ships_all_three_parts():
    """Phase 1 overruns the cap (the manager-latency shape): wave 2 never
    starts. The grace window finishes tests + README on the FAST dispatch,
    in parallel, and the answer carries all three parts."""
    fleet = Fleet([PLAN_FULL], slow={"tally.py implementation": 0.5})
    fast_calls = []

    def fast(msgs, max_tokens, exclude_pids=()):
        fast_calls.append(_user(msgs))
        return _worker_text(_user(msgs)), "fast/model"
    t0 = time.monotonic()
    out = swarm.run(_msgs(TALLY), fleet, max_seconds=0.2, grace_seconds=5,
                    fast_dispatch=fast)
    assert time.monotonic() - t0 < 4
    assert out["timed_out"] is True
    assert len(fast_calls) == 2, "the two skipped phases, nothing else"
    assert ("phase-finish:Tests", "fast/model") in out["models"]
    assert CODE in out["text"] and "def test_4" in out["text"] and "| -c |" in out["text"]
    assert "unfinished" not in out


def test_what_the_grace_cannot_finish_is_named_never_silent():
    fleet = Fleet([PLAN_FULL], slow={"tally.py implementation": 0.4})
    out = swarm.run(_msgs(TALLY), fleet, max_seconds=0.1, grace_seconds=1,
                    fast_dispatch=lambda m, t, exclude_pids=(): ("", None))
    assert out["text"].startswith(CODE)
    assert "**Not finished:**" in out["text"]
    assert [u.split(" (")[0] for u in out["unfinished"]] == ["part 2", "part 3"]
    line = swarm.trailer_summary(out)
    assert line.startswith("unfinished=2: part 2")
    assert "plan=tally.py implementation | Tests | README section" in line
    assert "done=1/3" in line and "timed_out=1" in line


def test_a_phase_that_failed_without_a_cap_is_named_too():
    def worker(user):
        return "" if "): Packaging hint\n" in user else _worker_text(user)
    plan = {"goal": "g", "phases": [
        {"title": "Copy", "task": "write the copy", "needs": []},
        {"title": "Packaging hint", "task": "one hint", "needs": []}]}
    out = swarm.run(_msgs("Write copy for my shop"), Fleet([plan], worker=worker))
    assert out["unfinished"] == ["Packaging hint"]
    assert "Not finished" in out["text"]


# --------------------------------------------------------------------------- #
# A. The draft is checked for every part before review; synthesis cannot drop one
# --------------------------------------------------------------------------- #

def test_a_part_missing_from_the_draft_is_produced_by_a_repair_worker():
    def worker(user):
        # The README worker "forgets" the table: the markers prove it.
        text = _worker_text(user)
        if "): README section\n" in user:
            return "## tally\nCounts things.\n\n```\ntally -l f\n```"
        return text
    fleet = Fleet([PLAN_FULL], worker=worker)
    out = swarm.run(_msgs(TALLY), fleet)
    assert ("repair:part 3", "free/worker") in out["models"]
    assert "| -l | lines |" in out["text"]
    assert "unfinished" not in out


def test_parts_no_marker_can_judge_go_to_one_verdict():
    req = ("Landing copy for my bakery. Deliver:\n- hero headline\n- about "
           "paragraph\n- closing call to action\n")
    plan = {"goal": "copy", "phases": [
        {"title": "Hero headline", "task": "write the hero headline", "needs": []},
        {"title": "About paragraph", "task": "write the about paragraph", "needs": []},
        {"title": "Closing call to action", "task": "write the closing call to action",
         "needs": []}]}

    def worker(user):
        if "REQUIRED PART" in user.split("THE USER'S REQUEST", 1)[0]:
            return "Order your loaf today."
        return "Fresh bread." if "): Closing" not in user else "Bread."
    fleet = Fleet([plan], worker=worker, verify={"missing": [3]})
    out = swarm.run(_msgs(req), fleet)
    assert fleet.calls.count("parts-verify") == 1
    assert ("repair:part 3", "free/worker") in out["models"]
    assert "Order your loaf today." in out["text"]


def test_with_a_manager_the_parts_verdict_is_the_managers():
    seen = []

    def manager(msgs, max_tokens, purpose):
        if "REQUIRED PARTS the user enumerated" in msgs[0]["content"]:
            seen.append(purpose)
            return '{"missing": []}', "sub/mgr", 5
        if purpose == "plan":
            return json.dumps({"goal": "g", "phases": [
                {"title": "Hero headline", "task": "write the hero headline", "needs": []},
                {"title": "About paragraph", "task": "write the about paragraph",
                 "needs": []}]}), "sub/mgr", 5
        if purpose == "review":
            return '{"verdict": "ship", "problems": []}', "sub/mgr", 5
        if purpose == "verify":
            return '{"ok": true}', "sub/mgr", 5
        return '{"missing": []}', "sub/mgr", 5
    fleet = Fleet([{}], worker=lambda u: "Fresh bread.")
    swarm.run(_msgs("Copy. Deliver:\n- hero headline\n- about paragraph\n"), fleet,
              manager=manager)
    assert seen == ["verify"] and "parts-verify" not in fleet.calls


def test_a_part_synthesis_dropped_is_restored_from_its_phase():
    fleet = Fleet([PLAN_FULL], synth=lambda user: CODE)   # keeps only the code
    out = swarm.run(_msgs(TALLY), fleet)
    assert CODE in out["text"] and "def test_0" in out["text"] and "| -w |" in out["text"]
    assert "unfinished" not in out


def test_the_dashboard_trailer_ticks_only_what_finished():
    out = {"text": "x", "phases": [{"title": "A", "output": "x"}],
           "planned": ["A", "B"], "plan": {}}
    text = swarm.format_answer(out)
    assert "1. [x] A" in text and "2. [ ] B" in text


# --------------------------------------------------------------------------- #
# A. The handler wires the budget, the outer bound, the header and the row
# --------------------------------------------------------------------------- #

def test_the_budget_kwargs_are_bounded(monkeypatch):
    monkeypatch.setattr(A.config, "get_setting", lambda name, default=None: default)
    kw = A._swarm_budget_kwargs(300)
    assert kw["seconds_per_phase"] == A._SWARM_SECONDS_PER_PHASE_DEFAULT
    assert kw["grace_seconds"] == A._SWARM_GRACE_SECONDS_DEFAULT
    assert 300 <= kw["max_seconds_ceiling"] <= (A._PIPELINE_OUTER_MAX
                                                 - A._PIPELINE_OUTER_GRACE
                                                 - kw["grace_seconds"])
    assert callable(kw["fast_dispatch"])
    assert A._swarm_budget_kwargs(None) == {}
    monkeypatch.setattr(A.config, "get_setting", lambda name, default=None: 10 ** 6)
    kw = A._swarm_budget_kwargs(300)
    assert kw["max_seconds_ceiling"] + kw["grace_seconds"] + A._PIPELINE_OUTER_GRACE \
        <= A._PIPELINE_OUTER_MAX


def test_the_handler_passes_the_budget_and_flags_what_is_unfinished(monkeypatch):
    seen = {}
    result = {"text": "tally.py\n\n---\n**Not finished:** part 3 (README)",
              "plan": {"goal": "G"}, "phases": [{"title": "Code", "output": "x"}],
              "planned": ["Code", "README"], "review": {}, "models": [],
              "timed_out": True, "unfinished": ["part 3 (README)"], "cap_seconds": 400}

    def fake_run(messages, dispatch, on_event=None, **kw):
        seen.update(kw)
        seen["outer_left"] = A._pipeline_time_left()
        return dict(result)
    monkeypatch.setattr(A.swarm, "run", fake_run)
    monkeypatch.setattr(A, "_swarm_fast_path", lambda *a, **k: False)
    monkeypatch.setattr(A, "_swarm_manager_kwargs", lambda: {})
    monkeypatch.setattr(A, "_swarm_max_seconds", lambda: 300.0)
    monkeypatch.setattr(A.config, "get_setting", lambda name, default=None: default)
    body = {"model": "swarm", "messages": _msgs(TALLY)}
    with A.app.test_request_context(json=body), \
            mock.patch.object(A, "_act_pipeline_watcher", lambda: None):
        g.act = {}
        resp = A.app.make_response(A._swarm_completion(body))
        act = g.act
    assert seen["max_seconds"] == 300.0
    assert {"seconds_per_phase", "max_seconds_ceiling", "grace_seconds",
            "fast_dispatch"} <= set(seen)
    assert seen["outer_left"] > seen["max_seconds_ceiling"] + seen["grace_seconds"] - 5
    assert act["pipeline_unfinished"] == ["part 3 (README)"]
    assert act["pipeline_plan"] == ["Code", "README"]
    hdr = resp.headers["X-Free-LLM-Hub-Pipeline"]
    assert hdr.startswith("unfinished=1: part 3") and "cap=400s" in hdr
    assert "Not finished" in resp.get_json()["choices"][0]["message"]["content"]


def test_crews_forward_the_budget(monkeypatch):
    import crews
    seen = {}
    monkeypatch.setattr(crews.swarm, "run",
                        lambda m, d, profile=None, on_event=None, **kw: seen.update(kw) or {})
    fast = lambda m, t, exclude_pids=(): ("", None)                 # noqa: E731
    crews.run(_msgs(TALLY), lambda *a, **k: ("", None), "crew-code", max_seconds=300,
              seconds_per_phase=60, max_seconds_ceiling=900, grace_seconds=90,
              fast_dispatch=fast)
    assert seen["max_seconds_ceiling"] == 900 and seen["fast_dispatch"] is fast
    assert seen["seconds_per_phase"] == 60 and seen["grace_seconds"] == 90


def test_the_dashboard_shows_the_unfinished_badge():
    html = open("templates/index.html", encoding="utf-8").read()
    assert "a.pipeline_unfinished" in html


def test_the_fast_dispatch_takes_the_quickest_capable_model(monkeypatch):
    tried = []
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("slow", "a", "hard"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [
        ("slow", "a"), ("mid", "b"), ("quick", "c")])
    ranks = {"slow": 90.0, "mid": 20.0, "quick": 2.0}
    monkeypatch.setattr(A, "_latency_rank", lambda pid, m: (ranks[pid], 0))

    class _R:
        status_code = 200

        def json(self):
            return {"choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": "done"}}]}

        def close(self):
            pass

    def dispatch(pid, payload, deadline=None):
        tried.append((pid, deadline))
        return _R(), None
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", dispatch)
    monkeypatch.setattr(A, "_record_chat_usage", lambda *a, **k: None)
    monkeypatch.setattr(A, "_act_pick", lambda *a, **k: None)
    text, who = A._swarm_fast_dispatch(_msgs("write the README"), 100)
    assert (text, who) == ("done", "quick/c")
    assert tried == [("quick", A._SWARM_FAST_HOP_DEADLINE)]
    # The ordinary stage keeps the strongest-first order and its long hop.
    tried.clear()
    A._swarm_dispatch(_msgs("write the README"), 100)
    assert tried == [("slow", A._SWARM_HOP_DEADLINE)]


# --------------------------------------------------------------------------- #
# B. An exhausted chain answers in the hub's own words, on all three protocols
# --------------------------------------------------------------------------- #

NIM_404 = {"detail": "Function '4df48b4f-1a2b-4c3d-9e8f-001122334455': Not found "
                     "for account 'acct-7788'"}


class _Resp:
    def __init__(self, status=200, payload=None, chunks=None):
        self.status_code = status
        self._payload = payload or {}
        self._chunks = chunks
        self.headers = {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload

    def close(self):
        pass

    def iter_content(self, chunk_size=None):
        return iter(self._chunks or ())

    def iter_lines(self, decode_unicode=False):
        return iter(self._chunks or ())


@pytest.fixture
def quiet(monkeypatch):
    for name in ("_record_chat_usage", "_record_outcome", "_save_perf_stats",
                 "_act_pick", "_note_ttft", "_record_stream_outcome",
                 "_note_provider_timeout", "_throttle_failed_hop", "_mark_model_dead"):
        monkeypatch.setattr(A, name, lambda *a, **k: None)
    monkeypatch.setattr(A, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(A, "_model_block_reason", lambda pid, m: None)
    monkeypatch.setattr(A, "_is_provider_dead", lambda pid: False)
    monkeypatch.setattr(A, "_capacity_eta", lambda cap=60: 1)
    monkeypatch.setattr(A.config, "get_flag",
                        lambda k, d=None: True if k == "hedge_simple_turns" else d)
    yield


def _route(monkeypatch, *chain, difficulty="simple"):
    monkeypatch.setattr(A, "_route_by_difficulty",
                        lambda *a, **k: (chain[0][0], chain[0][1], difficulty))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: list(chain))


def _hard_fail(monkeypatch):
    def dispatch(pid, payload, stream):
        if pid == "dahl":
            return _Resp(429, {"error": {"message": "rate limited, key sk-live-123"}})
        return _Resp(404, NIM_404)
    monkeypatch.setattr(A, "_dispatch_chat", dispatch)


def _assert_hubs_own_words(text):
    assert "Function" not in text and "4df48b4f" not in text and "acct-7788" not in text
    assert "sk-live-123" not in text
    assert "All providers failed" in text
    assert "nvidia: HTTP 404 x2" in text and "dahl: HTTP 429" in text
    assert "last hard error: HTTP 404 from nvidia" in text


TOOLS = [{"type": "function", "function": {"name": "add", "parameters": {}}}]


@pytest.mark.parametrize("stream", [False, True])
def test_chat_completions_never_relays_an_upstream_body(quiet, monkeypatch, stream):
    _route(monkeypatch, ("nvidia", "a"), ("nvidia", "b"), ("dahl", "c"))
    _hard_fail(monkeypatch)
    r = A.app.test_client().post("/v1/chat/completions", json={
        "model": "auto", "stream": stream, "tools": TOOLS,
        "messages": _msgs("Use the add tool to add 17 and 25")})
    assert r.status_code == 503
    _assert_hubs_own_words(r.get_json()["error"]["message"])
    assert r.headers["X-Free-LLM-Hub-Last-Error"]


def test_responses_never_relays_an_upstream_body(quiet, monkeypatch):
    _route(monkeypatch, ("nvidia", "a"), ("nvidia", "b"), ("dahl", "c"))
    _hard_fail(monkeypatch)
    r = A.app.test_client().post("/v1/responses", json={
        "model": "auto", "stream": False, "input": "What is 2 plus 2?"})
    assert r.status_code == 503
    _assert_hubs_own_words(r.get_json()["error"]["message"])


def test_messages_never_relays_an_upstream_body(quiet, monkeypatch):
    _route(monkeypatch, ("nvidia", "a"), ("nvidia", "b"), ("dahl", "c"))
    _hard_fail(monkeypatch)
    r = A.app.test_client().post("/v1/messages", json={
        "model": "claude-sonnet-4", "max_tokens": 64, "stream": False,
        "messages": _msgs("What is 2 plus 2?")})
    assert r.status_code == 503
    body = r.get_json()
    assert body["type"] == "error"
    _assert_hubs_own_words(body["error"]["message"])


def test_the_log_keeps_the_raw_body_for_diagnosis():
    assert "4df48b4f" in A._last_hard_log_body({"json": NIM_404})
    assert A._last_hard_log_body(None) == "none"


def test_hop_classes_are_spelled_for_a_client():
    got = A._client_hop_errors(["nvidia: _HopBudgetExceeded", "nvidia: HTTP 404",
                                "nvidia: HTTP 404", "dahl: HTTP 429"])
    assert got == ["nvidia: no answer within the hop budget", "nvidia: HTTP 404 x2",
                   "dahl: HTTP 429"]


# --------------------------------------------------------------------------- #
# B. A trivial tool turn does not walk eight siblings behind one stalled gateway
# --------------------------------------------------------------------------- #

def test_the_spread_demotes_never_drops():
    entries = [(100, "nv", "a"), (99, "nv", "b"), (98, "nv", "c"), (97, "dahl", "d"),
               (96, "nv", "e"), (95, "g4f", "f")]
    out = A._spread_by_provider(entries, 2)
    assert [e[2] for e in out] == ["a", "b", "d", "f", "c", "e"]


def test_a_trivial_tool_chain_is_spread_across_providers(monkeypatch):
    world = {"nv": ["nv-%d" % i for i in range(8)], "dahl": ["d-1"], "g4f": ["g-1"],
             "llm7": ["l-1"]}
    scores = {m: 100.0 - i for i, m in enumerate(
        world["nv"] + world["dahl"] + world["g4f"] + world["llm7"])}
    monkeypatch.setattr(A, "_available_providers", lambda *a, **k: list(world))
    monkeypatch.setattr(A, "_prefetch_auto_models", lambda pids: dict(world))
    monkeypatch.setattr(A, "_auto_models", lambda pid: list(world[pid]))
    monkeypatch.setattr(A, "_provider_capable", lambda pid, est: True)
    monkeypatch.setattr(A, "_supports_tools", lambda pid, m: True)
    monkeypatch.setattr(A, "_is_tool_proven", lambda m: m.startswith("nv"))
    monkeypatch.setattr(A, "_is_fast", lambda pid, m: True)
    monkeypatch.setattr(A, "_measured_latency_ms", lambda pid, m: None)
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, m: scores[m])
    monkeypatch.setattr(A, "_active_mode", lambda: A.MODE_ALL)
    monkeypatch.setattr(A, "_is_model_dead", lambda pid, m: False)
    monkeypatch.setattr(A.quota, "is_model_throttled", lambda pid, m: False)
    monkeypatch.setattr(A.quota, "model_status", lambda pid, m: {"exhausted": False})
    monkeypatch.setattr(A, "_context_ok", lambda pid, m, est: True)
    monkeypatch.setattr(A, "_sub_available_providers", lambda: [])
    monkeypatch.setattr(A.config, "get_flag", lambda k, d=None: d)
    for name in ("_reliability_penalty", "_latency_penalty", "_answer_quality_penalty"):
        monkeypatch.setattr(A, name, lambda pid, m: 0.0)
    monkeypatch.setattr(A, "_chain_reliability_band", lambda pid, m: 0)
    with A.app.test_request_context():
        g.hub_simple_turn = True
        chain = A._build_chain("nv", "nv-0", 400, require_tools=True)
        g.hub_simple_turn = False
        plain = A._build_chain("nv", "nv-0", 400, require_tools=True)
    first5 = [p for p, _m in chain[:5]]
    assert {"dahl", "g4f", "llm7"} <= set(first5), chain
    assert [p for p, _m in plain[:5]].count("nv") >= 4, "a real agent turn keeps strength order"


def test_a_stalled_provider_goes_behind_the_others_on_a_trivial_turn(quiet, monkeypatch):
    monkeypatch.setattr(A, "_TRIVIAL_HOP_BUDGET", 0.3)
    monkeypatch.setattr(A, "_TRIVIAL_SLOW_HOP_BUDGET", 0.3)
    monkeypatch.setattr(A, "_ADAPTIVE_HOP_FLOOR", 0.1)
    _route(monkeypatch, ("nvidia", "a"), ("nvidia", "b"), ("nvidia", "c"), ("llm7", "x"))
    calls = []
    tool_answer = {"choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None, "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "add", "arguments": "{\"a\":17,\"b\":25}"}}]}}]}

    def dispatch(pid, payload, stream):
        calls.append("%s/%s" % (pid, payload["model"]))
        if pid == "nvidia":
            time.sleep(1.0)
        return _Resp(200, tool_answer)
    monkeypatch.setattr(A, "_dispatch_chat", dispatch)
    r = A.app.test_client().post("/v1/chat/completions", json={
        "model": "auto", "stream": False, "tools": TOOLS,
        "messages": _msgs("Use the add tool to add 17 and 25")})
    assert r.status_code == 200
    assert calls == ["nvidia/a", "llm7/x"], calls


def test_a_real_turn_keeps_the_chain_order():
    clock = A._ChainClock.__new__(A._ChainClock)
    clock.trivial, clock._stalled = False, {"nv"}
    assert list(clock.walk([("nv", "a"), ("nv", "b"), ("x", "c")])) == [
        ("nv", "a"), ("nv", "b"), ("x", "c")]
    clock.trivial = True
    assert list(clock.walk([("nv", "a"), ("nv", "b"), ("x", "c")])) == [
        ("x", "c"), ("nv", "a"), ("nv", "b")]


def test_every_chain_loop_walks_through_the_clock():
    src = open("app.py", encoding="utf-8").read()
    assert src.count("for hop_pid, hop_model in _clock.walk(_chain):") == 3


# --------------------------------------------------------------------------- #
# C. The brevity trim never serves a prefix of a number
# --------------------------------------------------------------------------- #

def _chat(text, completion_tokens):
    return {"choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": {"completion_tokens": completion_tokens}}


def test_a_tiny_budget_never_turns_50648_into_5064():
    data = _chat("50648\n\nExplanation: 50647 plus one is 50648, which is the answer.", 30)
    A._fit_visible_to_caller(data, {"max_tokens": 1025, A._CALLER_MAX_KEY: 1})
    assert data["choices"][0]["message"]["content"].startswith("50648")


def test_a_cut_backs_off_to_the_last_whole_token():
    text = "The answer is 50648 and here is a very long explanation " * 4
    data = _chat(text, 60)
    A._fit_visible_to_caller(data, {"max_tokens": 1030, A._CALLER_MAX_KEY: 5})
    out = data["choices"][0]["message"]["content"]
    assert len(out) <= 20 and text.startswith(out) and not out.endswith("5064")
    assert data["choices"][0]["finish_reason"] == "length"


def test_the_stream_cap_never_emits_a_partial_number():
    frames = [b'data: {"choices":[{"delta":{"content":"50648"}}]}\n\n',
              b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
              b"data: [DONE]\n\n"]
    gate = A._StreamAnswerGate(iter(frames), mode="bytes", visible_cap=4,
                               hold_chars=10 ** 6)
    out = b"".join(gate).decode()
    text, _tools, _fin = A._sse_answer_digest([out.encode()])
    assert "5064" not in text.replace("50648", "")
    assert text in ("", "50648")
