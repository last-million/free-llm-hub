r"""What three refuters found in the fact-reference stamping before it shipped.

Each test here is a concrete repro one of them produced against the first
version of memory.py's reference stamping (see test_memory_knows_what_moved):
a NUL byte losing the fact, prose eating the reference slots, a file replaced
by a folder reading as unchanged, a corrupted stamp taking the whole memory
block down, paths with spaces shredded, marks cut mid-path, the moved-files
line clipped on exactly the interrupts that matter, no legend, every path
stat'd twice per turn, the stamp taken under the global lock, and a link
inside the project pointing outside it.
"""
import os

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


def test_a_nul_byte_in_a_fact_does_not_lose_the_fact(proj):
    """os.stat raises ValueError, not OSError, on an embedded NUL; that used to
    escape and the 'original request' fact was silently never stored."""
    assert M.remember_fact("s1", "The original request: fix src/x.js\x00 now", project_dir=str(proj))
    assert M.get("s1")["facts"] == ["The original request: fix src/x.js now"]
    assert M.note_interrupted("s1", request="edit src/a\x00b.py", doing=["Write(src/a.js\x00)"],
                              project_dir=str(proj))


def test_prose_that_looks_like_a_path_does_not_take_the_slots(proj):
    """Dates, fractions, 'e.g.', 'Node.js' consumed the three slots in text
    order and the real paths after them were never stamped."""
    for n in ("a.py", "b.py", "c.py"):
        (proj / "src" / n).write_text("x", encoding="utf-8")
    M.remember_fact("s1", "deadline 12/09/2026, e.g. Node.js and/or 24/7; code in src/a.py src/b.py src/c.py",
                    project_dir=str(proj))
    refs = [r["p"] for r in list(M.get("s1")["fact_meta"].values())[0]["refs"]]
    assert refs == ["src/a.py", "src/b.py", "src/c.py"]


def test_existing_paths_win_the_slots(proj):
    M.remember_fact("s1", "touches ghost.py phantom.py app.py src/tools/pen.js", project_dir=str(proj))
    refs = [r["p"] for r in list(M.get("s1")["fact_meta"].values())[0]["refs"]]
    assert refs[:2] == ["app.py", "src/tools/pen.js"]


def test_a_file_replaced_by_a_folder_of_its_name_is_a_change(proj):
    M.remember_fact("s1", "the config is in app.py", project_dir=str(proj))
    (proj / "app.py").unlink()
    (proj / "app.py").mkdir()
    assert "[changed since: app.py]" in _block("s1", proj)


def test_a_corrupted_stamp_costs_only_its_mark(proj):
    """A refs field that is a number used to raise out of context_block and
    the whole memory block -- facts, tasks, the stopping place -- was lost."""
    M.remember_fact("s1", "the API is in app.py", project_dir=str(proj))
    M.note_interrupted("s1", request="r", doing=["x"], project_dir=str(proj))
    mem = M.get("s1")
    mem["interrupted"]["refs"] = 5
    for k in mem["fact_meta"]:
        mem["fact_meta"][k]["refs"] = [{"p": None}, 7]
    M._save(mem)
    out = M.context_block("s1", 2000, project_dir=str(proj), query="api")
    assert "the API is in app.py" in out and "PREVIOUS TURN" in out
    assert "[now on disk: ]" not in out


def test_a_project_folder_with_spaces_in_its_path(tmp_path):
    """This repo lives under a folder with spaces in its path; the tool lines
    a stopped turn is filed with spell the whole path, spaces included."""
    p = tmp_path / "my proj"
    (p / "src").mkdir(parents=True)
    (p / "src" / "pen.js").write_text("x", encoding="utf-8")
    line = "Write: " + str(p / "src" / "pen.js") + " now"
    M.note_interrupted("s1", request="fix", doing=[line], project_dir=str(p))
    refs = M.get("s1")["interrupted"]["refs"]
    assert [r["p"] for r in refs] == ["src/pen.js"]
    (p / "src" / "pen.js").unlink()
    assert "gone since: src/pen.js" in M.resume_block("s1", project_dir=str(p))


def test_marks_are_never_cut_in_the_middle_of_a_path(proj):
    names = ["src/components/dashboard/Header.tsx", "src/components/dashboard/Sidebar.tsx",
             "src/components/dashboard/Footer.tsx"]
    for n in names:
        (proj / n).parent.mkdir(parents=True, exist_ok=True)
        (proj / n).write_text("x", encoding="utf-8")
    M.remember_fact("s1", "layout lives in " + ", ".join(names), project_dir=str(proj))
    for n in names:
        (proj / n).unlink()
    out = _block("s1", proj)
    suffix = out[out.rindex(" ["):]
    assert len(suffix) <= M.REF_SUFFIX_MAX_CHARS + 1
    assert "more]" in suffix, "what did not fit is counted, not silently dropped"
    for piece in suffix.strip(" []").split("; "):
        if piece.startswith("+"):
            continue
        assert piece.endswith(".tsx"), piece      # the file's own name is whole


def test_the_moved_line_survives_a_long_interrupt(proj):
    target = str(proj / "app.py")
    M.note_interrupted("s1", request="r" * 200, doing=["edit " + target] + ["x" * 150] * 7,
                       partial="p" * 700, project_dir=str(proj))
    (proj / "app.py").unlink()
    out = M.resume_block("s1", budget_chars=1000, project_dir=str(proj))
    assert "Since it stopped: gone since: app.py" in out
    assert "Continue from exactly there" in out


def test_marks_come_with_a_legend_and_a_clean_block_does_not(proj):
    M.remember_fact("s1", "the tool is app.py", project_dir=str(proj))
    assert "mark means" not in _block("s1", proj)
    (proj / "app.py").unlink()
    out = _block("s1", proj)
    assert "mark means that file moved after the fact was learned" in out


def test_nothing_is_stat_twice_in_one_turn(proj, monkeypatch):
    for i in range(10):
        M.remember_fact("s1", "fact %d about app.py and src/tools/pen.js" % i, project_dir=str(proj))
    (proj / "app.py").write_text("changed", encoding="utf-8")
    calls = []
    real = M._ref_status_now

    def counting(ref, project_dir, rel):
        calls.append(rel)
        return real(ref, project_dir, rel)
    monkeypatch.setattr(M, "_ref_status_now", counting)
    M.context_block("s1", 2000, project_dir=str(proj), query="fact")
    assert sorted(set(calls)) == ["app.py", "src/tools/pen.js"]
    assert len(calls) == 2, "recall runs twice per turn; each path is looked at once"


def test_the_stamp_is_taken_outside_the_lock():
    src = open("memory.py", encoding="utf-8").read()
    body = src[src.index("def remember_fact("):src.index("def remember_recent(")]
    assert body.index("stamp = _stamp(text, project_dir)") < body.index("with _LOCK:")


def test_a_link_pointing_outside_the_project_is_refused(tmp_path, proj):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "secret.txt").write_text("s", encoding="utf-8")
    link = proj / "link"
    try:
        os.symlink(str(outside), str(link), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("no symlink privilege here")
    M.remember_fact("s1", "see link/secret.txt", project_dir=str(proj))
    assert list(M.get("s1")["fact_meta"].values())[0]["refs"] == []


def test_a_drive_root_project_is_not_a_crash(proj):
    assert M._harvest_refs("Windows/notepad.exe", "C:\\", 3) is not None
