"""bandit.py: learned model choice per kind of task (Thompson nudges)."""
import ast
import json
import os
import random
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bandit  # noqa: E402
from bandit import MAX_NUDGE, Bandit, task_kind  # noqa: E402

KIND = "coding|hard|tools|m"


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _mean_delta(b, kind, pid, model, draws=2000):
    return sum(b.nudge(kind, pid, model, 100.0) - 100.0
               for _ in range(draws)) / draws


# -- task_kind ---------------------------------------------------------------
def test_task_kind_strings():
    assert task_kind("coding", "hard", True, 30_000) == "coding|hard|tools|m"
    assert task_kind(None, "simple", False, 100) == "any|simple|notools|s"
    assert task_kind("all", "medium", False, 0) == "any|medium|notools|s"
    assert task_kind("Vision", "HARD", True, 60_000) == "vision|hard|tools|l"
    assert task_kind("coding", "hard", True, 11_999).endswith("|s")
    assert task_kind("coding", "hard", True, 12_000).endswith("|m")
    assert task_kind("coding", "hard", True, 59_999).endswith("|m")
    assert task_kind("coding", "weird", True, None) == "coding|any|tools|s"
    assert task_kind("a|b", None, 0, "nope") == "a/b|any|notools|s"


# -- nudges ------------------------------------------------------------------
def test_nudge_bounded_and_unbiased_without_data():
    b = Bandit(rng=random.Random(1))
    deltas = [b.nudge(KIND, "p", "m", 100.0) - 100.0 for _ in range(4000)]
    assert all(abs(d) <= MAX_NUDGE + 1e-9 for d in deltas)
    assert abs(sum(deltas) / len(deltas)) < 0.05
    assert min(deltas) < -0.8 and max(deltas) > 0.8  # really explores
    note = b.propensity_note(KIND, "p", "m")
    assert note == {"alpha": 1.0, "beta": 1.0, "n": 0.0, "source": "prior"}


def test_nudge_is_a_tie_breaker_inside_the_top_band():
    # Owner rule 2026-10-04: best AVAILABLE models first. One model's +nudge
    # plus another's -nudge never exceeds app.py's 2-point _AUTO_TOP_BAND,
    # so a model more than 2 points stronger is never overtaken.
    assert MAX_NUDGE == 1.0
    assert 2 * MAX_NUDGE <= 2.0
    b = Bandit(rng=random.Random(11))
    for _ in range(50):
        b.reward(KIND, "weak", "w", 1)
        b.reward(KIND, "strong", "s", 0)
    for _ in range(2000):
        assert (b.nudge(KIND, "weak", "w", 130.0)
                <= b.nudge(KIND, "strong", "s", 132.0))


def test_nudge_leaves_a_non_number_alone():
    b = Bandit(rng=random.Random(2))
    assert b.nudge(KIND, "p", "m", None) is None
    assert abs(b.nudge(KIND, "p", "m", 7) - 7) <= MAX_NUDGE


def test_rewarded_model_beats_a_failing_one_for_that_kind():
    b = Bandit(rng=random.Random(3))
    for _ in range(20):
        b.reward(KIND, "good", "m1", 1)
        b.reward(KIND, "bad", "m2", 0)
    assert _mean_delta(b, KIND, "good", "m1") > 0.5
    assert _mean_delta(b, KIND, "bad", "m2") < -0.5
    for d in (b.nudge(KIND, "good", "m1", 50.0) - 50.0 for _ in range(500)):
        assert abs(d) <= MAX_NUDGE + 1e-9
    # a model nobody measured is not dragged along
    assert abs(_mean_delta(b, KIND, "other", "m3", 4000)) < 0.07


def test_backoff_to_global_with_fewer_than_three_kind_observations():
    clock = Clock()
    b = Bandit(clock=clock, rng=random.Random(4))
    other = "any|simple|notools|s"
    for _ in range(10):
        b.reward(other, "p", "m", 1)
    b.reward(KIND, "p", "m", 0)
    b.reward(KIND, "p", "m", 0)
    note = b.propensity_note(KIND, "p", "m")
    assert note["source"] == "model"
    assert note["alpha"] == pytest.approx(11.0)
    assert note["beta"] == pytest.approx(3.0)
    assert note["n"] == pytest.approx(12.0)
    # global evidence is positive, so the draw is still pulled up
    assert _mean_delta(b, KIND, "p", "m") > 0.25
    b.reward(KIND, "p", "m", 0)
    note = b.propensity_note(KIND, "p", "m")
    assert note["source"] == "kind"
    assert (note["alpha"], note["beta"], note["n"]) == pytest.approx((1, 4, 3))
    assert _mean_delta(b, KIND, "p", "m") < -0.25
    # an hour later the three (slightly decayed) observations still count
    clock.t += 3600
    assert b.propensity_note(KIND, "p", "m")["source"] == "kind"
    # two half-lives later they are ~0.75 of an observation: back to global
    clock.t += 2 * bandit.HALF_LIFE
    assert b.propensity_note(KIND, "p", "m")["source"] == "model"


def test_reward_takes_only_quality_values():
    b = Bandit()
    for bad in (0.7, -1, 2, "1", None, True, False, float("nan"), [1]):
        b.reward(KIND, "p", "m", bad)
    assert b.propensity_note(KIND, "p", "m")["n"] == 0.0
    b.reward(KIND, "p", "m", 0.5)
    note = b.propensity_note(KIND, "p", "m")
    assert note["source"] == "model"
    assert (note["alpha"], note["beta"]) == pytest.approx((1.5, 1.5))


# -- decay -------------------------------------------------------------------
def test_decay_halves_evidence_after_one_half_life():
    clock = Clock()
    b = Bandit(clock=clock, rng=random.Random(5))
    for _ in range(10):
        b.reward(KIND, "p", "m", 1)
    note = b.propensity_note(KIND, "p", "m")
    assert (note["alpha"], note["beta"], note["n"]) == pytest.approx((11, 1, 10))
    clock.t += bandit.HALF_LIFE
    note = b.propensity_note(KIND, "p", "m")
    assert note["source"] == "kind"
    assert (note["alpha"], note["beta"], note["n"]) == pytest.approx((6, 1, 5))
    b.reward(KIND, "p", "m", 0)        # new evidence lands on the decayed one
    note = b.propensity_note(KIND, "p", "m")
    assert (note["alpha"], note["beta"]) == pytest.approx((6, 2))


def test_an_old_failure_fades_so_the_model_recovers():
    clock = Clock()
    b = Bandit(clock=clock, rng=random.Random(6))
    for _ in range(20):
        b.reward(KIND, "p", "m", 0)
    assert _mean_delta(b, KIND, "p", "m") < -0.5
    clock.t += 10 * bandit.HALF_LIFE
    assert b.propensity_note(KIND, "p", "m")["source"] == "prior"
    assert abs(_mean_delta(b, KIND, "p", "m", 4000)) < 0.07


# -- delayed tool-call credit ----------------------------------------------
def _tool(cid, text="ok"):
    return {"role": "tool", "tool_call_id": cid, "content": text}


def test_tool_call_credit_once_per_id_and_unknown_ids_ignored():
    b = Bandit()
    b.remember_tool_calls(["c1", "c2"], "p", "m", KIND)
    msgs = [{"role": "user", "content": "go"},
            {"role": "assistant", "tool_calls": [{"id": "c1"}]},
            _tool("c1"), _tool("zz"), _tool("c1")]
    graded = []

    def grade(msg):
        graded.append(msg["tool_call_id"])
        return 1

    assert b.credit_from_messages(msgs, grade) == 1
    assert graded == ["c1"]
    note = b.propensity_note(KIND, "p", "m")
    assert (note["alpha"], note["beta"]) == pytest.approx((2, 1))
    # the same history comes back next turn: nothing is credited twice
    assert b.credit_from_messages(msgs, grade) == 0
    # re-remembering a credited id does not re-open it
    b.remember_tool_calls("c1", "p", "m", KIND)
    assert b.credit_from_messages(msgs, grade) == 0
    # "cannot tell" skips without spending the id; a later grade counts
    assert b.credit_from_messages([_tool("c2")], lambda m: None) == 0
    assert b.credit_from_messages([_tool("c2")], lambda m: 0.7) == 0

    def boom(msg):
        raise RuntimeError("grader broke")

    assert b.credit_from_messages([_tool("c2")], boom) == 0
    assert b.credit_from_messages([_tool("c2")], lambda m: 0) == 1
    note = b.propensity_note(KIND, "p", "m")
    assert (note["alpha"], note["beta"]) == pytest.approx((2, 2))
    # junk input never raises
    assert b.credit_from_messages(None, grade) == 0
    assert b.credit_from_messages([None, "x", {"role": "tool"}], grade) == 0
    assert b.credit_from_messages(msgs, None) == 0


def test_tool_call_memory_respects_ttl():
    clock = Clock()
    b = Bandit(clock=clock)
    b.remember_tool_calls(["old"], "p", "m", KIND)
    clock.t += bandit.TOOL_CALL_TTL + 1
    b.remember_tool_calls(["new"], "p", "m", KIND)
    assert b.credit_from_messages([_tool("old"), _tool("new")],
                                  lambda m: 1) == 1
    assert b.propensity_note(KIND, "p", "m")["alpha"] == pytest.approx(2)


def test_tool_call_memory_is_an_lru():
    b = Bandit()
    for i in range(bandit.TOOL_CALL_MAX + 1):
        b.remember_tool_calls([f"c{i}"], "p", "m", KIND)
    assert b.credit_from_messages([_tool("c0")], lambda m: 1) == 0
    last = f"c{bandit.TOOL_CALL_MAX}"
    assert b.credit_from_messages([_tool("c1"), _tool(last)],
                                  lambda m: 1) == 2


# -- persistence -------------------------------------------------------------
def test_save_load_round_trip(tmp_path):
    path = str(tmp_path / "sub" / "bandit.json")
    clock = Clock()
    b = Bandit(path, clock=clock)
    for _ in range(5):
        b.reward(KIND, "p", "m", 1)
    b.reward(KIND, "p", "m", 0.5)
    b.reward("any|simple|notools|s", "q", "n", 0)
    assert b.save(force=True) is True
    before = (b.propensity_note(KIND, "p", "m"),
              b.propensity_note("any|simple|notools|s", "q", "n"),
              b.stats())
    b2 = Bandit(path, clock=clock)
    after = (b2.propensity_note(KIND, "p", "m"),
             b2.propensity_note("any|simple|notools|s", "q", "n"),
             b2.stats())
    assert after == before
    assert not [f for f in os.listdir(os.path.dirname(path))
                if f.endswith(".tmp")]


@pytest.mark.parametrize("content", [
    "{not json", "[1, 2]", "",
    json.dumps({"kinds": [["k", "p", "m", -1, 0, 0], ["k", "p"], "x"],
                "models": [["p", "m", float("inf"), 0, 0]]}),
])
def test_corrupt_file_loads_empty(tmp_path, content):
    path = tmp_path / "bandit.json"
    path.write_text(content, encoding="utf-8")
    b = Bandit(str(path))
    assert b.stats() == []
    assert b.propensity_note("k", "p", "m")["source"] == "prior"


def test_missing_file_and_unwritable_path_never_raise(tmp_path):
    b = Bandit(str(tmp_path / "absent.json"))
    assert b.stats() == []
    blocker = tmp_path / "file"
    blocker.write_text("x")
    b = Bandit(str(blocker / "nested" / "bandit.json"))
    b.reward(KIND, "p", "m", 1)               # autosave fails quietly
    assert b.save(force=True) is False
    assert b.propensity_note(KIND, "p", "m")["n"] == pytest.approx(1)


def test_autosave_is_throttled(tmp_path):
    path = str(tmp_path / "bandit.json")
    clock = Clock()
    b = Bandit(path, clock=clock)

    def on_disk():
        n = Bandit(path, clock=clock).propensity_note(KIND, "p", "m")["n"]
        return round(n, 3)                 # seconds of decay are noise here

    b.reward(KIND, "p", "m", 1)
    assert on_disk() == 1
    clock.t += 10
    b.reward(KIND, "p", "m", 1)
    assert on_disk() == 1              # throttled
    assert b.save() is False
    clock.t += bandit.SAVE_MIN_INTERVAL
    b.reward(KIND, "p", "m", 1)
    assert on_disk() == 3
    clock.t += 1
    b.reward(KIND, "p", "m", 1)
    assert b.save(force=True) is True
    assert on_disk() == 4


def test_in_memory_bandit_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    b = Bandit()
    b.reward(KIND, "p", "m", 1)
    assert b.save(force=True) is False
    assert os.listdir(tmp_path) == []


def test_configure_points_default_at_a_file(tmp_path):
    path = str(tmp_path / "bandit.json")
    seed = Bandit(path)
    for _ in range(4):
        seed.reward(KIND, "p", "m", 1)
    seed.save(force=True)
    try:
        bandit.configure(path)
        assert bandit.default.path == path
        assert bandit.default.propensity_note(KIND, "p", "m")["source"] == "kind"
    finally:
        bandit.configure(None)
    assert bandit.default.path is None
    assert bandit.default.stats() == []


# -- stats -------------------------------------------------------------------
def test_stats_ranks_by_posterior_mean_and_limits():
    b = Bandit()
    for _ in range(10):
        b.reward(KIND, "good", "a", 1)
        b.reward(KIND, "bad", "b", 0)
    b.reward(KIND, "one", "c", 1)
    rows = b.stats()
    assert [r["pid"] for r in rows] == ["good", "one", "bad"]
    assert set(rows[0]) == {"kind", "pid", "model", "mean", "n"}
    assert rows[0]["n"] == pytest.approx(10)
    assert len(b.stats(limit=1)) == 1


# -- concurrency & purity ----------------------------------------------------
def test_thread_safety_smoke(tmp_path):
    b = Bandit(str(tmp_path / "bandit.json"), clock=Clock(),
               rng=random.Random(7))
    errors = []
    per_thread = 300
    threads_n = 8

    def work(tid):
        try:
            for i in range(per_thread):
                b.reward(KIND, "p", "m", (0, 0.5, 1)[i % 3])
                b.nudge(KIND, "p", "m", 100.0)
                cid = f"t{tid}-{i}"
                b.remember_tool_calls([cid], "q", "n", KIND)
                b.credit_from_messages([_tool(cid)], lambda m: 1)
                if i % 50 == 0:
                    b.stats()
                    b.save(force=True)
        except Exception as exc:          # pragma: no cover - the failure
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(t,))
               for t in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    total = per_thread * threads_n
    assert b.propensity_note(KIND, "p", "m")["n"] == pytest.approx(total)
    assert b.propensity_note(KIND, "q", "n")["alpha"] == pytest.approx(total + 1)


def test_module_is_pure_stdlib():
    src = open(bandit.__file__, encoding="utf-8").read()
    names = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
    assert names <= {"collections", "json", "math", "os", "random",
                     "threading", "time"}
