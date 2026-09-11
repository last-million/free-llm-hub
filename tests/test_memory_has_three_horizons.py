r"""Short, medium and long memory - per session AND per project.

REQUESTED: "long, short, medium memory for each project and session, and for
long and short and medium context window".

They are three different kinds of thing, and collapsing them is why a long
conversation both forgets what matters and pays for what does not:

  SHORT   the last few exchanges. The model already has these in its window;
          what it loses is the ones compaction just dropped, so a one-line
          trace of each is kept to hand back.
  MEDIUM  the running summary of everything older - one paragraph standing in
          for a hundred turns.
  LONG    the decisions that outlive the conversation entirely, and - new here
          - that can belong to the PROJECT rather than the session, so
          tomorrow's conversation in the same folder starts knowing what
          yesterday's established.

The budget is spent in order of what is expensive to LOSE, not in order of
size: a decision re-litigated costs a whole exchange arriving back where the
conversation already was, so long memory is served first and trimmed last.
"""
import os

import pytest

import memory


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(memory._ROOT_ENV, str(tmp_path))
    yield


# --------------------------------------------------------------------------- #
# SHORT
# --------------------------------------------------------------------------- #

def test_a_recent_turn_is_kept():
    memory.remember_recent("s1", "add the login page", "user")
    assert memory.get("s1")["recent"][0]["text"] == "add the login page"


def test_short_memory_is_bounded():
    """It is a trace, not a transcript: the real turns are in the window."""
    for i in range(memory.MAX_RECENT + 20):
        memory.remember_recent("s1", "turn %d" % i)
    rec = memory.get("s1")["recent"]
    assert len(rec) == memory.MAX_RECENT
    assert rec[-1]["text"] == "turn %d" % (memory.MAX_RECENT + 19)


def test_one_trace_is_bounded_too():
    memory.remember_recent("s1", "x" * (memory.MAX_RECENT_CHARS * 4))
    assert len(memory.get("s1")["recent"][0]["text"]) <= memory.MAX_RECENT_CHARS


def test_a_blank_turn_is_not_traced():
    memory.remember_recent("s1", "   ")
    assert memory.get("s1")["recent"] == []


def test_the_role_is_kept():
    memory.remember_recent("s1", "done", "agent")
    assert memory.get("s1")["recent"][0]["role"] == "agent"


# --------------------------------------------------------------------------- #
# LONG, at the project level
# --------------------------------------------------------------------------- #

def test_a_project_fact_outlives_the_session(tmp_path):
    proj = str(tmp_path / "site")
    memory.remember_project_fact(proj, "This repo uses pnpm, not npm")
    assert "This repo uses pnpm, not npm" in memory.project_facts(proj)
    # a brand-new conversation in the same folder
    assert "pnpm" in memory.context_block("brand-new-session", project_dir=proj)


def test_the_same_folder_spelled_differently_is_one_project(tmp_path):
    """On Windows the same folder routinely arrives as both C:\\work\\site and
    c:/work/site within a single session."""
    a = str(tmp_path / "site")
    b = os.path.join(str(tmp_path), "site") + os.sep
    assert memory.project_key(a) == memory.project_key(b)


def test_two_projects_do_not_share(tmp_path):
    memory.remember_project_fact(str(tmp_path / "a"), "uses pnpm")
    assert memory.project_facts(str(tmp_path / "b")) == []


def test_a_project_path_never_becomes_a_filename(tmp_path):
    """The key is a hash: a path is neither safe nor short as a filename."""
    key = memory.project_key(str(tmp_path / "site"))
    assert key.startswith("proj-") and memory._SAFE_ID_RE.match(key)


def test_no_project_means_no_key():
    for bad in (None, "", "   ", 7):
        assert memory.project_key(bad) is None


def test_forgetting_a_project(tmp_path):
    proj = str(tmp_path / "site")
    memory.remember_project_fact(proj, "uses pnpm")
    assert memory.forget_project(proj) is True
    assert memory.project_facts(proj) == []


# --------------------------------------------------------------------------- #
# The three together
# --------------------------------------------------------------------------- #

def _fill(proj):
    memory.remember_project_fact(proj, "PROJECT: uses pnpm")
    memory.remember_fact("s1", "SESSION: the API is REST")
    memory.remember_summary("s1", "MEDIUM: we built the schema")
    memory.remember_recent("s1", "SHORT: add the login page", "user")


def test_all_three_appear(tmp_path):
    proj = str(tmp_path / "site")
    _fill(proj)
    block = memory.context_block("s1", budget_chars=2000, project_dir=proj)
    for marker in ("PROJECT:", "SESSION:", "MEDIUM:", "SHORT:"):
        assert marker in block, marker


def test_the_narrower_scope_leads(tmp_path):
    """Narrowest first: task, agent, run, project, global. The more specific
    claim is the one that should be read first and trimmed last -- a session
    that settled something overrides the project note it contradicts."""
    proj = str(tmp_path / "site")
    _fill(proj)
    block = memory.context_block("s1", budget_chars=2000, project_dir=proj)
    assert block.index("SESSION:") < block.index("PROJECT:") < block.index("MEDIUM:")


def test_a_fact_the_session_repeats_is_not_listed_twice(tmp_path):
    proj = str(tmp_path / "site")
    memory.remember_project_fact(proj, "uses pnpm")
    memory.remember_fact("s1", "Uses PNPM")
    block = memory.context_block("s1", project_dir=proj)
    assert block.lower().count("pnpm") == 1


def test_short_is_cut_first_when_the_budget_is_tight(tmp_path):
    """Cheapest and most perishable. A decision is not."""
    proj = str(tmp_path / "site")
    _fill(proj)
    block = memory.context_block("s1", budget_chars=120, project_dir=proj)
    assert "SESSION:" in block, "a decision must survive a tight budget"
    assert "SHORT:" not in block


def test_each_horizon_can_be_switched_off(tmp_path):
    proj = str(tmp_path / "site")
    _fill(proj)
    only_long = memory.context_block("s1", project_dir=proj,
                                     medium=False, short=False)
    assert "PROJECT:" in only_long
    assert "MEDIUM:" not in only_long and "SHORT:" not in only_long


def test_an_unspent_share_is_handed_down(tmp_path):
    """A conversation with no decisions yet gives the whole budget to the
    summary rather than padding the block."""
    memory.remember_summary("s1", "M" * 900)
    block = memory.context_block("s1", budget_chars=1000)
    assert len(block) > int(1000 * memory.SHARE_MEDIUM)


def test_the_block_never_exceeds_its_budget(tmp_path):
    proj = str(tmp_path / "site")
    for i in range(memory.MAX_FACTS):
        memory.remember_project_fact(proj, "project decision %d" % i)
        memory.remember_fact("s1", "session decision %d" % i)
    memory.remember_summary("s1", "x" * memory.MAX_SUMMARY_CHARS)
    for i in range(memory.MAX_RECENT):
        memory.remember_recent("s1", "turn %d" % i)
    for budget in (200, 800, 2000):
        assert len(memory.context_block("s1", budget_chars=budget,
                                        project_dir=proj)) <= budget, budget


def test_an_empty_memory_still_injects_nothing(tmp_path):
    assert memory.context_block("s1", project_dir=str(tmp_path / "site")) == ""


def test_a_missing_project_is_not_an_error():
    memory.remember_fact("s1", "the API is REST")
    assert "REST" in memory.context_block("s1", project_dir=None)


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #

def test_the_agent_gets_its_project_memory():
    src = open("agentic_chat.py", encoding="utf-8").read()
    body = src[src.index("def _memory_block("):]
    body = body[:body.index("\ndef write_task_brief(")]
    assert "project_dir=getattr(sess" in body


def test_every_turn_leaves_a_short_trace():
    src = open("agentic_chat.py", encoding="utf-8").read()
    body = src[src.index("def send_message_stream(session_id, text):"):]
    body = body[:body.index("\n    def err(")]
    assert "memory.remember_recent(session_id, text" in body
