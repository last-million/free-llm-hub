"""What the hub has learned about slow / resting / failing pairs survives a restart.

Owner 2026-10-24: "of course the hub should remember the slowness -- it should be done
with code." MEASURED the same day: after a restart a roles turn re-found that nvidia
GLM-5.3 is silent for 30 s by failing on it AGAIN, because first-content latency
(_tool_ttft), resting pairs (_pair_rest / _empty_200), stall marks (_recent_hop_fail),
text-only "never chains" pairs (_chain_seen), slow long requests (_long_ctx_speed) and
per-pair quirks (_SYSTEM_FIRST, _THINKING_LEARNED) lived in plain dicts and this hub
restarts on every auto-update and every reboot.

`learnstate.py` stores a JSON-native blob; app.py packs / restores it. Every restored
row is dropped when it is past its own TTL (on save AND on restore), a live entry is
never overwritten, a corrupt / old / wrong-schema file restores nothing, and the flag
`persist_learned_state` turns the whole thing off. Hermetic: a temp file, fresh dicts.
"""
import json
import time

import pytest

import app as A
import config
import learnstate

DICTS = ("_tool_ttft", "_long_ctx_speed", "_chain_seen", "_recent_hop_fail", "_empty_200",
         "_pair_rest", "_THINKING_LEARNED", "_SYSTEM_FIRST", "_ls_ttft_sig")


@pytest.fixture
def world(monkeypatch, tmp_path):
    path = tmp_path / "learned-state.json"
    monkeypatch.setattr(learnstate, "_path", lambda: str(path))
    monkeypatch.setattr(learnstate, "_last_save", [0.0])
    for name in DICTS:
        monkeypatch.setattr(A, name, {})
    monkeypatch.setattr(A.perfstats, "save", lambda *a, **k: False)   # not under test
    return path


def _clear():
    for name in DICTS:
        getattr(A, name).clear()


def _fill(now):
    A._tool_ttft[("nvidia", "z-ai/glm-5.3")] = [31000.0, 33000.0, 30500.0]
    A._recent_hop_fail[("nvidia", "z-ai/glm-5.3-flash")] = (now - 30, "timeout")
    A._pair_rest[("g4f", "srv_x:gpt-5")] = {"until": now + 1800, "level": 2, "cls": "deadline"}
    A._empty_200[("kilocode", "dots")] = [(now - 60, "empty"), (now - 30, "empty")]
    A._chain_seen[("g4f", "poe/GLM-5.3-Flash-T")] = {"text": 9, "last": now - 10}
    A._long_ctx_speed[("nvidia", "kimi-k3")] = [(now - 100, 90000.0, True)]
    A._THINKING_LEARNED[("inception", "mercury-3")] = now - 100
    A._SYSTEM_FIRST[("uncloseai", "Qwen3.8-27B")] = now - 100


def test_everything_comes_back_after_a_restart(world):
    now = time.time()
    _fill(now)
    assert A._save_learned_state(force=True) is True
    assert world.exists()
    _clear()
    assert A._load_learned_state() == 8
    assert A._tool_ttft[("nvidia", "z-ai/glm-5.3")] == [31000.0, 33000.0, 30500.0]
    assert A._recent_hop_fail[("nvidia", "z-ai/glm-5.3-flash")] == (now - 30, "timeout")
    assert A._pair_rest[("g4f", "srv_x:gpt-5")] == {"until": now + 1800, "level": 2,
                                                    "cls": "deadline"}
    assert A._empty_200[("kilocode", "dots")] == [(now - 60, "empty"), (now - 30, "empty")]
    assert A._chain_seen[("g4f", "poe/GLM-5.3-Flash-T")] == {"text": 9, "last": now - 10}
    assert A._long_ctx_speed[("nvidia", "kimi-k3")] == [(now - 100, 90000.0, True)]
    assert A._THINKING_LEARNED[("inception", "mercury-3")] == now - 100
    assert A._SYSTEM_FIRST[("uncloseai", "Qwen3.8-27B")] == now - 100


def test_the_routers_own_questions_are_answered_the_same_after_a_restart(world):
    now = time.time()
    _fill(now)
    A._save_learned_state(force=True)
    _clear()
    # Before the load the hub knows nothing (the old behaviour: it fails on it again) ...
    assert A._recent_hop_failure("nvidia", "z-ai/glm-5.3-flash") is None
    assert not A._pair_resting("g4f", "srv_x:gpt-5")
    A._load_learned_state()
    # ... after it, the readers the chain builder uses see what the last run learned.
    assert A._recent_hop_failure("nvidia", "z-ai/glm-5.3-flash") == "timeout"
    assert A._pair_resting("g4f", "srv_x:gpt-5")
    assert A._empty_streak("kilocode", "dots") == 2
    assert A._never_chains("g4f", "poe/GLM-5.3-Flash-T")
    assert A._tool_turn_sick("g4f", "srv_x:gpt-5")


def test_rows_past_their_own_ttl_are_not_even_written(world):
    now = time.time()
    A._recent_hop_fail[("a", "m")] = (now - A._RECENT_FAIL_TTL - 5, "timeout")
    A._pair_rest[("b", "m")] = {"until": now - A._STREAK_REST_CAP - 5, "level": 3, "cls": "empty"}
    A._empty_200[("c", "m")] = [(now - 5000, "empty")]
    A._chain_seen[("d", "m")] = {"text": 20, "last": now - A._NEVER_CHAIN_TTL - 5}
    A._long_ctx_speed[("e", "m")] = [(now - A.LONG_CTX_SPEED_TTL - 5, 1.0, False)]
    A._THINKING_LEARNED[("f", "m")] = now - A._THINKING_LEARNED_TTL - 5
    A._SYSTEM_FIRST[("g", "m")] = now - A._SYSTEM_FIRST_TTL - 5
    pack = A._ls_pack(now)
    for name in ("recent_fail", "pair_rest", "empty_200", "chain_seen", "long_ctx",
                 "thinking", "system_first"):
        assert pack[name] == [], name


def test_a_rest_that_just_ran_out_is_kept_so_its_level_keeps_doubling(world):
    # _note_pair_failure doubles the next rest while the last one ended < _STREAK_REST_CAP ago.
    now = time.time()
    A._pair_rest[("b", "m")] = {"until": now - 100, "level": 3, "cls": "empty"}
    A._save_learned_state(force=True)
    _clear()
    A._load_learned_state()
    assert A._pair_rest[("b", "m")]["level"] == 3


def test_stale_rows_in_an_old_file_are_skipped_on_restore(world):
    now = time.time()
    old = now - 10 * 86400
    world.write_text(json.dumps({"schema": 1, "saved": now, "dicts": {
        "tool_ttft": [["a", "m", [1.0, 2.0], old]],
        "recent_fail": [["a", "m", [now - 5000, "timeout"]]],
        "pair_rest": [["a", "m", {"until": now - 99999, "level": 1, "cls": "empty"}]],
        "empty_200": [["a", "m", [[now - 5000, "empty"]]]],
        "chain_seen": [["a", "m", {"text": 20, "last": now - 99999}]],
        "long_ctx": [["a", "m", [[now - 99999, 10.0, False]]]],
        "thinking": [["a", "m", old]],
        "system_first": [["a", "m", old]],
    }}))
    assert A._load_learned_state() == 0
    assert not any(getattr(A, n) for n in DICTS)


def test_the_ttft_age_is_when_the_list_last_changed_not_when_it_was_saved(world):
    t0 = 1_000_000_000.0
    A._tool_ttft[("nvidia", "z-ai/glm-5.3")] = [30000.0, 31000.0]
    first = A._ls_pack(t0)["tool_ttft"][0]
    again = A._ls_pack(t0 + 3600)["tool_ttft"][0]
    assert first[3] == again[3] == t0                     # unchanged list keeps its age
    A._tool_ttft[("nvidia", "z-ai/glm-5.3")].append(29000.0)
    changed = A._ls_pack(t0 + 7200)["tool_ttft"][0]
    assert changed[3] == t0 + 7200                        # a new sample refreshes it
    assert A._ls_pack(t0 + 7200 + 8 * 86400)["tool_ttft"] == []   # a week untouched: gone


# Built inside the test, never in the parametrize values: a time.time() in a test id makes
# every xdist worker collect DIFFERENT test names and pytest-xdist aborts the whole run.
BAD_FILES = {
    "not_json": lambda now: "this is not json {",
    "foreign_schema": lambda now: json.dumps(
        {"schema": 99, "saved": now, "dicts": {"tool_ttft": []}}),
    "a_month_old": lambda now: json.dumps({"schema": 1, "saved": now - 30 * 86400, "dicts": {}}),
    "dicts_not_a_map": lambda now: json.dumps({"schema": 1, "saved": now, "dicts": "nope"}),
    "a_list": lambda now: json.dumps([1, 2, 3]),
}


@pytest.mark.parametrize("kind", sorted(BAD_FILES))
def test_a_corrupt_old_or_foreign_file_restores_nothing(world, kind):
    world.write_text(BAD_FILES[kind](time.time()))
    assert A._load_learned_state() == 0
    assert not any(getattr(A, n) for n in DICTS)


def test_a_missing_file_is_the_old_boot(world):
    assert not world.exists()
    assert A._load_learned_state() == 0


def test_one_bad_row_never_costs_the_good_ones(world):
    now = time.time()
    world.write_text(json.dumps({"schema": 1, "saved": now, "dicts": {
        "recent_fail": [None, 7, ["only-two", "items"], [1, "m", [now, "t"]],
                        ["a", "m", "not-a-pair"], ["good", "m", [now - 10, "timeout"]]],
        "pair_rest": [["a", "m", {"until": "soon"}],
                      ["good", "m", {"until": now + 600, "level": 1, "cls": "deadline"}]],
        "empty_200": [["a", "m", [["x", "empty"]]],
                      ["good", "m", [[now - 5, "empty"]]]],
        "tool_ttft": [["a", "m", ["x"], now], ["good", "m", [5.0], now]],
    }}))
    assert A._load_learned_state() == 4
    assert A._recent_hop_failure("good", "m") == "timeout"
    assert A._pair_resting("good", "m")
    assert A._empty_streak("good", "m") == 1
    assert A._tool_ttft[("good", "m")] == [5.0]


def test_a_live_entry_is_never_overwritten_by_the_file(world):
    now = time.time()
    A._recent_hop_fail[("p", "m")] = (now - 1, "timeout")
    A._pair_rest[("p", "m")] = {"until": now + 60, "level": 1, "cls": "deadline"}
    A._save_learned_state(force=True)
    A._recent_hop_fail[("p", "m")] = (now, "reasoning_stall")      # newer, live
    A._pair_rest[("p", "m")] = {"until": now + 9999, "level": 5, "cls": "empty"}
    A._load_learned_state()
    assert A._recent_hop_fail[("p", "m")] == (now, "reasoning_stall")
    assert A._pair_rest[("p", "m")]["level"] == 5


def test_the_switch_off_saves_and_restores_nothing(world, monkeypatch):
    now = time.time()
    _fill(now)
    A._save_learned_state(force=True)
    assert world.exists()
    _clear()
    orig = config.get_flag
    monkeypatch.setattr(config, "get_flag",
                        lambda k, d=None: False if k == "persist_learned_state" else orig(k, d))
    assert A._load_learned_state() == 0
    assert not any(getattr(A, n) for n in DICTS)
    world.unlink()
    _fill(now)
    assert A._save_learned_state(force=True) is False
    assert not world.exists()


def test_the_hot_path_builds_the_snapshot_at_most_once_per_interval(world, monkeypatch):
    packed = []
    real = A._ls_pack
    monkeypatch.setattr(A, "_ls_pack", lambda *a, **k: (packed.append(1), real(*a, **k))[1])
    for _ in range(60):
        A._save_perf_stats()            # what _record_outcome calls on every result
    assert len(packed) == 1
    A._save_perf_stats(force=True)      # the shutdown / re-exec flush ignores the throttle
    assert len(packed) == 2


def test_a_stall_mark_is_saved_soon_after_it_is_written(world):
    A._note_recent_hop_failure("nvidia", "z-ai/glm-5.3", "timeout")
    data = json.loads(world.read_text())
    rows = data["dicts"]["recent_fail"]
    assert [r[:2] for r in rows] == [["nvidia", "z-ai/glm-5.3"]]
    assert rows[0][2][1] == "timeout"


def test_the_saved_file_is_plain_json_with_no_secrets_by_construction(world):
    _fill(time.time())
    A._save_learned_state(force=True)
    data = json.loads(world.read_text())
    assert data["schema"] == 1 and isinstance(data["saved"], float)
    assert set(data["dicts"]) == {"tool_ttft", "long_ctx", "chain_seen", "recent_fail",
                                  "empty_200", "pair_rest", "thinking", "system_first"}
