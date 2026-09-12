r"""A remembered fact is marked when the files it names have moved under it.

Reviewed from outside (2026-09-12): "this project's memory is useful
continuity memory, not semantic truth ... it does not know whether a code
claim is still true at the current revision." Right. Facts were bare strings;
"the API lives in api/routes.py" was handed to every later turn after the file
was renamed, as if still true.

Each fact is now stamped with the project paths it names (existence, size,
mtime, a content hash for small files) and the project's git HEAD when it was
learned, and at recall the same paths are stat'd again. A fact whose
references moved is marked -- "[gone since: ...]", "[changed since: ...]",
"[now on disk: ...]" -- never dropped, never re-ordered, and never called
"still true": an unchanged file is a freshness signal about the references,
not a proof of the claim. No LLM call, no subprocess, no tree walk.
"""
import os
import shutil
import subprocess
import time

import pytest

import memory as M


@pytest.fixture(autouse=True)
def _mem(tmp_path, monkeypatch):
    monkeypatch.setenv(M._ROOT_ENV, str(tmp_path / "mem"))
    yield


@pytest.fixture
def proj(tmp_path):
    p = tmp_path / "proj"
    (p / "src" / "tools").mkdir(parents=True)
    (p / "src" / "tools" / "pen.js").write_text("draw();", encoding="utf-8")
    (p / "app.py").write_text("print(1)", encoding="utf-8")
    return p


def _block(scope, proj, query=""):
    return M.recall([scope], query, 900, project_dir=str(proj))


# --------------------------------------------------------------------------- #
# What moved
# --------------------------------------------------------------------------- #

def test_a_file_that_is_gone_is_marked_and_the_fact_is_untouched(proj):
    M.remember_fact("s1", "The pen tool lives in src/tools/pen.js", project_dir=str(proj))
    assert "[" not in _block("s1", proj)
    (proj / "src" / "tools" / "pen.js").unlink()
    out = _block("s1", proj)
    assert out.endswith("[gone since: src/tools/pen.js]")
    assert M.get("s1")["facts"] == ["The pen tool lives in src/tools/pen.js"], "the raw fact is unchanged"


def test_a_file_that_changed_is_marked_and_a_touch_is_not(proj):
    M.remember_fact("s1", "the endpoint is in app.py:6749", project_dir=str(proj))
    t = time.time() + 5
    os.utime(proj / "app.py", (t, t))                  # same content, new mtime
    assert "[" not in _block("s1", proj), "a touch is not a change: the hash says so"
    (proj / "app.py").write_text("print(2)", encoding="utf-8")
    assert "[changed since: app.py]" in _block("s1", proj)


def test_a_file_the_request_asked_for_is_marked_once_it_exists(proj):
    """Turn-1 facts name files that do not exist yet; the flip is the news."""
    M.remember_fact("s1", "The original request: create hello.txt and README.md", project_dir=str(proj))
    assert "[" not in _block("s1", proj)
    (proj / "hello.txt").write_text("hi", encoding="utf-8")
    out = _block("s1", proj)
    assert out.endswith("[now on disk: hello.txt]"), "README.md, still absent, is not marked"


def test_a_pinned_rule_keeps_its_place_and_its_mark_is_a_suffix(proj):
    M.remember_fact("s1", "MUST use Postgres; the schema is in db/schema.sql", project_dir=str(proj))
    M.remember_fact("s1", "the colour is blue", project_dir=str(proj))
    (proj / "db").mkdir()
    (proj / "db" / "schema.sql").write_text("x", encoding="utf-8")
    out = _block("s1", proj, query="something unrelated entirely")
    lines = [ln for ln in out.splitlines() if ln.startswith("- ")]
    assert lines and lines[0].startswith("- MUST use Postgres"), "still pinned, still first"
    assert lines[0].endswith("[now on disk: db/schema.sql]")


def test_nothing_outside_the_project_is_ever_looked_at(proj, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    M.remember_fact("s1", "see ../outside.txt and %s and C:/Windows/system32/cmd.exe"
                    % str(outside).replace("\\", "/"), project_dir=str(proj))
    meta = list(M.get("s1")["fact_meta"].values())[0]
    assert meta["refs"] == []


def test_urls_and_versions_are_not_paths(proj):
    M.remember_fact("s1", "use https://example.com/a/b.js at v2.0 on python 3.12", project_dir=str(proj))
    assert list(M.get("s1")["fact_meta"].values())[0]["refs"] == []


def test_at_most_three_references_per_fact(proj):
    M.remember_fact("s1", "touches a.py b.py c.py d.py e.py", project_dir=str(proj))
    assert len(list(M.get("s1")["fact_meta"].values())[0]["refs"]) == M.REF_MAX_PER_FACT


def test_the_suffix_is_bounded(proj):
    names = ["a-very-long-file-name-number-%d-for-the-suffix.js" % i for i in range(3)]
    for n in names:
        (proj / n).write_text("x", encoding="utf-8")
    M.remember_fact("s1", "uses " + " ".join(names), project_dir=str(proj))
    for n in names:
        (proj / n).unlink()
    out = _block("s1", proj)
    suffix = out[out.rindex(" ["):]
    assert len(suffix) <= M.REF_SUFFIX_MAX_CHARS + 1 and suffix.endswith("]")


# --------------------------------------------------------------------------- #
# Budgets, old files, bounds
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("budget", [120, 300, 900, 2000])
def test_the_budget_still_holds_with_marks(proj, budget):
    for i in range(8):
        (proj / ("f%d.py" % i)).write_text("x", encoding="utf-8")
        M.remember_fact("s1", "must keep module f%d.py exactly as it is" % i, project_dir=str(proj))
    for i in range(8):
        (proj / ("f%d.py" % i)).unlink()
    out = M.context_block("s1", budget, project_dir=str(proj), query="anything")
    assert len(out) <= budget


def test_an_old_memory_file_without_stamps_still_recalls(tmp_path, proj):
    M.remember_fact("s1", "old fact about app.py")        # no project_dir: no stamp
    assert M.get("s1")["fact_meta"] == {}
    assert _block("s1", proj).endswith("old fact about app.py")


def test_stamps_do_not_outlive_their_facts(proj):
    for i in range(M.MAX_FACTS + 10):
        M.remember_fact("s1", "fact %d about app.py" % i, project_dir=str(proj))
    mem = M.get("s1")
    assert len(mem["facts"]) == M.MAX_FACTS
    assert set(mem["fact_meta"]) == {M._fact_key(f) for f in mem["facts"]}


def test_a_restated_fact_is_re_stamped(proj):
    M.remember_fact("s1", "the tool is src/tools/pen.js", project_dir=str(proj))
    (proj / "src" / "tools" / "pen.js").write_text("draw2();", encoding="utf-8")
    assert "[changed since" in _block("s1", proj)
    M.remember_fact("s1", "the tool is src/tools/pen.js", project_dir=str(proj))   # said again
    assert "[" not in _block("s1", proj), "re-asserted at the current state"


def test_unchanged_references_are_cheap(proj):
    for i in range(40):
        M.remember_fact("s1", "fact %d names app.py src/tools/pen.js" % i, project_dir=str(proj))
    t0 = time.time()
    for _ in range(5):
        M.context_block("s1", 2000, project_dir=str(proj))
    assert time.time() - t0 < 2.0


# --------------------------------------------------------------------------- #
# The revision, without git on PATH
# --------------------------------------------------------------------------- #

def test_a_plain_folder_has_no_revision(proj):
    assert M._project_rev(str(proj)) is None
    M.remember_summary("s1", "so far so good", project_dir=str(proj))
    assert M.get("s1")["summary_rev"] is None


@pytest.mark.skipif(not shutil.which("git"), reason="git needed to make the repo")
def test_the_summary_heading_says_when_the_tree_moved(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")

    def git(*a):
        subprocess.run(["git", "-C", str(repo)] + list(a), check=True, env=env,
                       capture_output=True)
    git("init", "-q")
    (repo / "a.txt").write_text("1", encoding="utf-8")
    git("add", "."); git("commit", "-q", "-m", "one")
    rev_a = M._project_rev(str(repo))
    assert rev_a and len(rev_a) == 7
    M.note_turn("s1")
    M.remember_summary("s1", "the recap", project_dir=str(repo))
    M.note_compaction("s1")
    assert "What has happened so far:" in M.context_block("s1", 2000, project_dir=str(repo), query="x")
    (repo / "a.txt").write_text("2", encoding="utf-8")
    git("commit", "-q", "-am", "two")
    rev_b = M._project_rev(str(repo))
    assert rev_b != rev_a
    # The recap was written this turn: an agent that commits every turn moves
    # HEAD on the very turn the recap lands, so "moved" says nothing yet.
    assert "as of rev" not in M.context_block("s1", 2000, project_dir=str(repo), query="x")
    M.note_turn("s1")
    M.note_turn("s1")
    out = M.context_block("s1", 2000, project_dir=str(repo), query="x")
    assert "What has happened so far (as of rev %s; now %s):" % (rev_a, rev_b) in out


def test_the_revision_is_read_without_a_subprocess():
    src = open("memory.py", encoding="utf-8").read()
    assert "import subprocess" not in src and "subprocess." not in src


# --------------------------------------------------------------------------- #
# Where a stopped turn was, and the files it touched
# --------------------------------------------------------------------------- #

def test_a_stopped_turn_is_told_which_of_its_files_moved(proj):
    target = str(proj / "src" / "tools" / "pen.js")
    M.note_interrupted("s1", request="fix the pen", doing=["read " + target, "edit " + target],
                       partial="", why="stopped", project_dir=str(proj))
    assert "Since it stopped" not in M.resume_block("s1", project_dir=str(proj))
    (proj / "src" / "tools" / "pen.js").unlink()
    out = M.resume_block("s1", project_dir=str(proj))
    assert "Since it stopped: gone since: src/tools/pen.js. Re-check these before continuing." in out


def test_a_task_file_that_vanished_is_said_so(proj):
    (proj / "PROGRESS.md").write_text("- [x] a\n- [ ] b\n", encoding="utf-8")
    M.update_tasks_from_project("s1", str(proj))
    assert "from PROGRESS.md):" in M.resume_block("s1", project_dir=str(proj))
    (proj / "PROGRESS.md").unlink()
    assert "from PROGRESS.md (file now missing)):" in M.resume_block("s1", project_dir=str(proj))


# --------------------------------------------------------------------------- #
# The call sites hand the folder over
# --------------------------------------------------------------------------- #

def test_every_writer_passes_the_project_folder():
    chat = open("agentic_chat.py", encoding="utf-8").read()
    app = open("app.py", encoding="utf-8").read()
    assert 'project_dir=(get_session(session_id) or {}).get("project_dir")' in chat
    assert 'project_dir=(sess_info or {}).get("project_dir"))' in chat
    assert "memory.remember_summary(sid, out," in app
    assert "why=run.state, project_dir=run.project_dir)" in app
    body = app[app.index("def _multi_turn_events("):app.index("\ndef _multi_record(")]
    assert "project_dir=project_dir)" in body


def test_it_is_a_freshness_signal_not_a_truth_test():
    """The words the module uses: a mark says what happened to a reference,
    never that a claim is true."""
    src = open("memory.py", encoding="utf-8").read()
    assert "still true" not in M._annotate("x", {"refs": []}, ".")
    for word in ("gone since", "changed since", "now on disk"):
        assert word in src
