r"""Memory that survives a restart, a compaction, and a resumed session.

REQUESTED: "utilise le memory manager ... memory management, and also context
window, and also persistence, and also the orchestrator ... nothing can escape,
and all agents and conversations will always follow the rules 100%".

The hub had three kinds of memory and none of them lasted:

  * the running recap _compact_to_budget writes when it drops old turns lives
    in `_summary_cache` -- 64 entries, in RAM -- so the 5-hourly `git pull`
    restart is also a 5-hourly amnesia;
  * standing instructions ship on turn 1 and never again (for codex and
    opencode the addition is literally "" from turn 2), so a forty-turn session
    is following rules it was told about once, before compaction ate them;
  * `_Session.turn_count` resets to 0 on resume, so nothing could even ask how
    long a conversation had been going.

Deliberately small: a JSON file per conversation holding what must outlive the
context window. Not a vector store. Everything is best-effort -- a conversation
whose memory cannot be written works exactly as it did before.
"""
import json
import os

import pytest

import memory


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(memory._ROOT_ENV, str(tmp_path))
    yield


# --------------------------------------------------------------------------- #
# It persists
# --------------------------------------------------------------------------- #

def test_a_summary_survives_being_written_and_read():
    memory.remember_summary("s1", "we built the schema and the API")
    assert "schema" in memory.get("s1")["summary"]


def test_it_survives_a_restart(tmp_path):
    """The whole point: the RAM cache it replaces did not."""
    memory.remember_summary("s1", "the important recap")
    path = os.path.join(str(tmp_path), "s1.json")
    assert os.path.isfile(path)
    fresh = json.load(open(path, encoding="utf-8"))
    assert fresh["summary"] == "the important recap"


def test_a_summary_replaces_rather_than_appends():
    """A recap stands in for the turns it describes; appending would grow the
    thing that exists to stay small."""
    memory.remember_summary("s1", "first")
    memory.remember_summary("s1", "second")
    assert memory.get("s1")["summary"] == "second"


def test_facts_accumulate_and_deduplicate():
    memory.remember_fact("s1", "the database is Postgres")
    memory.remember_fact("s1", "the database is Postgres")
    memory.remember_fact("s1", "the API is REST, not GraphQL")
    assert memory.get("s1")["facts"] == ["the database is Postgres",
                                         "the API is REST, not GraphQL"]


def test_conversations_do_not_share_memory():
    memory.remember_fact("s1", "mine")
    assert memory.get("s2")["facts"] == []


def test_an_unknown_conversation_reads_as_blank():
    mem = memory.get("never-seen")
    assert mem["summary"] == "" and mem["facts"] == [] and mem["turns"] == 0


# --------------------------------------------------------------------------- #
# It stays small
# --------------------------------------------------------------------------- #

def test_the_summary_is_bounded():
    memory.remember_summary("s1", "y" * (memory.MAX_SUMMARY_CHARS * 3))
    assert len(memory.get("s1")["summary"]) <= memory.MAX_SUMMARY_CHARS


def test_facts_are_bounded_oldest_out_first():
    for i in range(memory.MAX_FACTS + 12):
        memory.remember_fact("s1", "decision number %d" % i)
    facts = memory.get("s1")["facts"]
    assert len(facts) == memory.MAX_FACTS
    assert facts[-1] == "decision number %d" % (memory.MAX_FACTS + 11)
    assert "decision number 0" not in facts


def test_one_fact_is_bounded_too():
    memory.remember_fact("s1", "z" * (memory.MAX_FACT_CHARS * 4))
    assert len(memory.get("s1")["facts"][0]) <= memory.MAX_FACT_CHARS


def test_blank_input_is_not_remembered():
    memory.remember_summary("s1", "   ")
    memory.remember_fact("s1", "")
    assert memory.get("s1")["summary"] == "" and memory.get("s1")["facts"] == []


# --------------------------------------------------------------------------- #
# Turn counting, durably
# --------------------------------------------------------------------------- #

def test_turns_count_across_a_restart():
    """_Session.turn_count resets to 0 on resume; this is what "how long has
    this been going" has to read instead."""
    assert memory.note_turn("s1") == 1
    assert memory.note_turn("s1") == 2
    assert memory.get("s1")["turns"] == 2


def test_rules_are_restated_on_a_schedule():
    for _ in range(memory.RESTATE_EVERY - 1):
        memory.note_turn("s1")
    assert not memory.should_restate_rules("s1")
    memory.note_turn("s1")
    assert memory.should_restate_rules("s1")


def test_restating_resets_the_clock():
    for _ in range(memory.RESTATE_EVERY):
        memory.note_turn("s1")
    assert memory.should_restate_rules("s1")
    memory.mark_rules_restated("s1")
    assert not memory.should_restate_rules("s1")


def test_a_fresh_conversation_is_not_due():
    assert not memory.should_restate_rules("brand-new")


# --------------------------------------------------------------------------- #
# What gets injected
# --------------------------------------------------------------------------- #

def test_the_block_carries_decisions_and_the_story():
    memory.remember_fact("s1", "Postgres, not SQLite")
    memory.remember_summary("s1", "the schema is done")
    block = memory.context_block("s1")
    assert "Postgres, not SQLite" in block and "the schema is done" in block


def test_an_empty_memory_injects_nothing():
    """A conversation with nothing to remember must not pay tokens for a
    heading that says so."""
    assert memory.context_block("s1") == ""


def test_decisions_survive_the_budget_and_the_story_is_what_gets_cut():
    """A half-remembered decision is worse than a half-remembered narrative."""
    memory.remember_fact("s1", "MUST use Postgres")
    memory.remember_summary("s1", "x" * 3000)
    block = memory.context_block("s1", budget_chars=400)
    assert "MUST use Postgres" in block
    assert len(block) <= 460


def test_the_block_never_exceeds_its_budget_with_facts_alone():
    for i in range(memory.MAX_FACTS):
        memory.remember_fact("s1", "decision %d" % i)
    assert len(memory.context_block("s1", budget_chars=200)) <= 200


# --------------------------------------------------------------------------- #
# It cannot break a turn, or escape its directory
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("sid", ["../escape", "a/b", "", None, "x" * 200, "a b"])
def test_a_bad_session_id_writes_nothing(sid, tmp_path):
    """Ids arrive from a URL segment and from CLI output. A session id of
    '../config' must not be able to name a file outside the memory directory."""
    memory.remember_summary(sid, "should not be written")
    memory.remember_fact(sid, "nor this")
    assert memory.get(sid)["summary"] == ""
    leaked = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert not leaked, leaked


def test_an_unwritable_directory_is_not_an_error(monkeypatch, tmp_path):
    """A file where the directory should be: makedirs cannot win, and the turn
    must still go through."""
    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")
    monkeypatch.setenv(memory._ROOT_ENV, str(blocker / "inside"))
    assert memory.remember_summary("s1", "x") is False
    assert memory.get("s1")["summary"] == ""


def test_a_corrupt_memory_file_reads_as_blank(tmp_path):
    (tmp_path / "s1.json").write_text("{ not json", encoding="utf-8")
    assert memory.get("s1")["facts"] == []


def test_a_file_holding_the_wrong_shape_reads_as_blank(tmp_path):
    (tmp_path / "s1.json").write_text('["a list"]', encoding="utf-8")
    assert memory.get("s1")["summary"] == ""


def test_forgetting_removes_it():
    memory.remember_fact("s1", "x")
    assert memory.forget("s1") is True
    assert memory.get("s1")["facts"] == []


def test_stats_do_not_raise_on_a_missing_directory(monkeypatch, tmp_path):
    monkeypatch.setenv(memory._ROOT_ENV, str(tmp_path / "nope"))
    assert memory.stats() == {"conversations": 0, "bytes": 0}


def test_this_module_is_a_leaf():
    """No app.py, no agentic_chat -- same reason model_categories is one."""
    src = open("memory.py", encoding="utf-8").read()
    assert "import app" not in src
    assert "import agentic_chat" not in src
