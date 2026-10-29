"""One Multi helper per TARGET (2026-10-29; scopecard targets, multitarget.py,
swarm_windows' target hooks, app.py section "MULTI PER TARGET").

Owner: when several targets are tested at once (several authorized programs, or
several of the owner's own apps/sites), Multi runs ONE helper per target, each
knowing only its own scope, its own open findings and what was already done --
and a later run continues each target from where it stopped.

HERMETIC: synthetic targets on example.com / example.net / example.org hosts, a
temp project folder, a temp scopes.json, a temp findings ledger, a fresh task
board, fake planner / spawn / run_turn (no network, no CLI, never the real
~/.free-llm-hub). This is the ONE file where the conftest leaves the feature ON
(_multi_per_target_off_unless_tested)."""
import os
import re
import threading
import time

import pytest

import app as A
import config
import craft
import findings as F
import multitarget as MT
import scopecard as S
import swarm_windows as SW
import taskboard

SCOPE_MD = """# Scope for this engagement

Out of scope: accounts.example.com

## acme
### In scope
- *.acme.example.com
### Out of scope
- pay.acme.example.com
### Rules
- No automated scanning

## globex
### In scope
- shop.globex.example.net
### Out of scope
- admin.globex.example.net
"""

GOAL = "Bug bounty: test acme and globex for IDOR and XSS, stay in scope."


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _runs(tmp_path, monkeypatch):
    monkeypatch.setenv(SW._STORE_ENV, str(tmp_path / "runs"))
    monkeypatch.setattr(SW, "SPAWN_STAGGER", 0.0)
    SW._RUNS.clear()
    yield
    for run in list(SW._RUNS.values()):
        run.stop_flag.set()
    SW._RUNS.clear()


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A project folder with a two-target SCOPE.md, its own scope store,
    findings ledger and task board."""
    store = S.Store(str(tmp_path / "state" / "scopes.json"))
    monkeypatch.setattr(A, "_scope_store", lambda: store)
    monkeypatch.setattr(A, "_SCOPE_MEM", {})
    monkeypatch.setattr(A, "_SCOPE_SEEN", {})
    monkeypatch.setattr(A, "_SCOPE_MISSED", {})
    monkeypatch.setattr(A, "_scope_card_on", lambda: True)
    root = tmp_path / "state" / "findings"
    monkeypatch.setattr(A, "_fc_root", lambda: str(root))
    A._FC_LEDGERS.clear()
    board = taskboard.Board()
    monkeypatch.setattr(taskboard, "default", board)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "SCOPE.md").write_text(SCOPE_MD, "utf-8")
    return {"proj": proj, "board": board, "ledger": A._fc_ledger()}


def _finding(ledger, proj, host_path, cls, status, reason=""):
    claim = {"key": cls + "|" + host_path, "title": "%s on %s" % (F.CLASS_LABELS[cls], host_path),
             "class": cls, "target": host_path, "severity": ""}
    ev = {"status": status, "reasons": [reason] if reason else [], "pointer": ""}
    ledger.upsert(str(proj), [(claim, ev, "fam")], "conv-1")


_ids = [0]


def _spawn(cli, project):
    _ids[0] += 1
    return "sess-%d" % _ids[0]


def _wait(run_id, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        st = SW.status(run_id)
        if st and st["state"] in (SW.DONE, SW.FAILED, SW.STOPPED):
            return st
        time.sleep(0.02)
    return SW.status(run_id)


def _recorder(write_notes=None, fail=None):
    """run_turn that records each prompt by the target its block names, and can
    write a target's notes file or fail a target's phase."""
    seen = {}
    lock = threading.Lock()

    def run_turn(session_id, prompt):
        m = re.search(r"^YOUR TARGET: (\S+)", prompt, re.M)
        key = m.group(1) if m else ("final" if "THIS RUN WORKS ON" in prompt else "other")
        with lock:
            seen.setdefault(key, []).append(prompt)
        if fail and key in fail:
            yield {"event": "error", "status": 503, "detail": "upstream exploded"}
            return
        if write_notes and key in write_notes:
            folder, text = write_notes[key]
            os.makedirs(os.path.join(folder, "notes"), exist_ok=True)
            with open(os.path.join(folder, "notes", key + ".md"), "w", encoding="utf-8") as fh:
                fh.write(text)
        yield {"event": "message", "text": "done with %s" % key}
    return run_turn, seen


def _planner(plan_json, systems):
    def planner(system, user):
        systems.append(system)
        return plan_json
    return planner


GOOD_PLAN = """{"goal": "g", "design": {}, "phases": [
 {"title": "acme", "target": "acme", "task": "Map acme's apps, then test them for IDOR and XSS.",
  "done_when": "notes/acme.md lists done and remaining", "files": ["notes/acme.md"],
  "needs": [], "parallel": true},
 {"title": "globex", "target": "globex", "task": "Map globex's shop, then test it for IDOR and XSS.",
  "done_when": "notes/globex.md lists done and remaining", "files": ["notes/globex.md"],
  "needs": [1], "parallel": true},
 {"title": "Summary across targets", "task": "Merge the notes.", "needs": [1, 2]}]}"""

ONE_PHASE_PLAN = """{"goal": "g", "phases": [
 {"title": "Test everything", "task": "Test acme and globex.", "done_when": "done", "needs": []},
 {"title": "Write report", "task": "Write a report.", "done_when": "REPORT.md", "needs": [1]}]}"""


# --------------------------------------------------------------------------- #
# 1. scopecard: several named targets on one card
# --------------------------------------------------------------------------- #

def test_a_card_reads_two_named_targets_and_keeps_shared_entries_in_each():
    card = S.extract(SCOPE_MD, src="SCOPE.md")
    ts = S.targets(card)
    assert [t["name"] for t in ts] == ["acme", "globex"]
    acme, globex = ts
    assert [e["p"] for e in acme["in"]] == ["*.acme.example.com"]
    assert sorted(e["p"] for e in acme["out"]) == ["accounts.example.com", "pay.acme.example.com"]
    assert [r["t"] for r in acme["rules"]] == ["No automated scanning"]
    assert [e["p"] for e in globex["in"]] == ["shop.globex.example.net"]
    assert sorted(e["p"] for e in globex["out"]) == ["accounts.example.com",
                                                     "admin.globex.example.net"]
    assert globex["rules"] == []
    # The guard still reads the whole card.
    assert S.in_scope("pay.acme.example.com", card) is False
    assert S.in_scope("admin.globex.example.net", card) is False
    assert S.in_scope("api.acme.example.com", card) is True


def test_render_target_shows_only_its_own_scope_and_the_shared_part():
    card = S.extract(SCOPE_MD, src="SCOPE.md")
    a = S.render_target(card, "ACME", 800)
    assert a.startswith("SCOPE OF TARGET acme")
    assert "*.acme.example.com" in a and "pay.acme.example.com" in a
    assert "accounts.example.com" in a and "No automated scanning" in a
    assert "globex" not in a
    assert S.render_target(card, "nope", 800) == ""
    assert S.render_target(card, "acme", 800) == S.render_target(card, "acme", 800)
    assert len(S.render_target(card, "acme", 300)) <= 300


def test_label_lines_and_app_hosts_open_targets():
    card = S.extract("App: shop.example.com\nApp: blog.example.org\n")
    assert [t["name"] for t in S.targets(card)] == ["shop.example.com", "blog.example.org"]
    assert {e["p"]: e["tg"] for e in card["in"]} == {"shop.example.com": "shop.example.com",
                                                      "blog.example.org": "blog.example.org"}
    two = S.extract("Program: acme\nIn scope: *.acme.example.com\n\n"
                    "Program: globex\nIn scope: shop.globex.example.net\n"
                    "Out of scope: admin.globex.example.net\n")
    assert [t["name"] for t in S.targets(two)] == ["acme", "globex"]
    assert [e["p"] for e in S.targets(two)[0]["in"]] == ["*.acme.example.com"]


def test_a_single_program_page_reads_exactly_as_before():
    page = ("# Acme Program\n## Overview\nWe run a program.\n## In scope\n- a.example.com\n"
            "## Rewards\nUp to 5000\n## Out of scope\n- c.example.com\n")
    card = S.extract(page)
    assert "targets" not in card
    assert all("tg" not in e for lst in ("in", "out", "rules") for e in card[lst])
    one = S.extract("## acme\nIn scope: a.acme.example.com\n")      # one target only
    assert "targets" not in one and all("tg" not in e for e in one["in"])
    # a heading that holds the targets is not one of them
    nested = S.extract("# Programs\n## acme\nIn scope: a.acme.example.com\n"
                       "## globex\nIn scope: b.globex.example.net\n")
    assert [t["name"] for t in S.targets(nested)] == ["acme", "globex"]


def test_an_old_card_loads_unchanged_and_is_one_unnamed_target(tmp_path):
    old = {"in": [{"p": "app.example.com", "kind": "program", "src": "message"}],
           "out": [{"p": "pay.example.com", "kind": "program", "src": "SCOPE.md"}],
           "rules": [{"t": "No DoS", "kind": "program", "src": "SCOPE.md"}]}
    assert S.normalize(old) == old
    st = S.Store(str(tmp_path / "scopes.json"))
    st.put("k", old)
    assert S.Store(str(tmp_path / "scopes.json")).get("k")["card"] == old
    ts = S.targets(old)
    assert len(ts) == 1 and ts[0]["name"] == "" and ts[0]["in"] == old["in"]
    assert S.targets(S.empty()) == []


def test_targets_survive_store_merge_and_go_with_their_source(tmp_path):
    card = S.extract(SCOPE_MD, src="SCOPE.md")
    st = S.Store(str(tmp_path / "scopes.json"))
    st.put("k", card)
    back = S.Store(str(tmp_path / "scopes.json")).get("k")["card"]
    assert back == S.normalize(card)
    merged = S.merge(back, S.extract("out of scope: x.example.org", src="message"))
    assert [t["name"] for t in S.targets(merged)] == ["acme", "globex"]
    dropped = S.drop_source(merged, "SCOPE.md")
    assert "targets" not in dropped and [e["p"] for e in dropped["out"]] == ["x.example.org"]


# --------------------------------------------------------------------------- #
# 2. multitarget: which targets, the plan, the block
# --------------------------------------------------------------------------- #

def test_plan_targets_follows_the_goal():
    card = S.extract(SCOPE_MD)
    assert MT.plan_targets(card, GOAL) == ["acme", "globex"]
    assert MT.plan_targets(card, "Start recon on every program in scope") == ["acme", "globex"]
    assert MT.plan_targets(card, "recon on acme only") == []          # one target named
    assert MT.plan_targets(card, "recon on api.acme.example.com") == []
    unnamed = S.extract("In scope: shop.example.com, blog.example.org, api.example.org")
    assert MT.plan_targets(unnamed, "pentest shop.example.com and blog.example.org") == [
        "shop.example.com", "blog.example.org"]
    assert MT.plan_targets(unnamed, "pentest shop.example.com") == []
    assert MT.plan_targets(S.empty(), GOAL) == []


def test_notes_files_are_one_per_target_and_never_shared():
    nm = MT.notes_map(["Acme Corp", "acme-corp", "summary", "shop.example.com"])
    assert nm["Acme Corp"] == "notes/acme-corp.md"
    assert nm["acme-corp"] == "notes/acme-corp-2.md"
    assert nm["summary"] == "notes/summary-2.md"                   # never the merge file
    assert nm["shop.example.com"] == "notes/shop.example.com.md"
    assert len(set(nm.values())) == 4


def test_fit_plan_keeps_a_good_plan_and_shapes_it():
    phases = SW.clean_phases(SW._extract_json(GOOD_PLAN))
    out = MT.fit_plan(phases, ["acme", "globex"], GOAL, SW.REVIEW_TITLE)
    assert [p["title"] for p in out] == ["acme", "globex", SW.REVIEW_TITLE]
    assert out[0]["task"].startswith("Map acme's apps")               # the planner's task stays
    for p, n in zip(out[:2], ("acme", "globex")):
        assert p["target"] == n and p["parallel"] is True
        assert p["files"] == ["notes/%s.md" % n] and p["needs"] == []   # globex no longer waits
    assert out[2]["needs"] == [1, 2] and "notes/summary.md" in out[2]["task"]
    assert MT.fit_plan(out, ["acme", "globex"], GOAL, SW.REVIEW_TITLE) == out   # idempotent


def test_fit_plan_replaces_a_plan_that_is_not_one_phase_per_target():
    phases = SW.clean_phases(SW._extract_json(ONE_PHASE_PLAN))
    out = MT.fit_plan(phases, ["acme", "globex"], GOAL, SW.REVIEW_TITLE)
    assert [p["title"] for p in out] == ["acme", "globex", SW.REVIEW_TITLE]
    assert "ONLY on the target \"acme\"" in out[0]["task"] and GOAL in out[0]["task"]
    # a setup phase that needs no target is kept and the targets still need it
    setup = [{"title": "Install tools", "task": "install httpx", "needs": []},
             {"title": "acme", "task": "t", "needs": [1]},
             {"title": "globex", "task": "t", "needs": [1, 2]}]
    out = MT.fit_plan(setup, ["acme", "globex"], GOAL, SW.REVIEW_TITLE)
    assert [p["title"] for p in out] == ["Install tools", "acme", "globex", SW.REVIEW_TITLE]
    assert out[1]["needs"] == [1] and out[2]["needs"] == [1] and out[3]["needs"] == [1, 2, 3]


def test_the_planner_ask_names_every_target_and_its_notes_file():
    ask = MT.plan_ask(["acme", "globex"], SW.REVIEW_TITLE)
    assert "ONE phase per target" in ask and '"parallel": true' in ask
    assert "notes/acme.md" in ask and "notes/globex.md" in ask and SW.REVIEW_TITLE in ask
    base = SW.plan_system(GOAL, helpers=4)
    with_t = SW.plan_system(GOAL, helpers=4, targets=["acme", "globex"])
    assert "PER-TARGET PLAN" not in base and with_t.startswith(base.split("\n")[0])
    assert "PER-TARGET PLAN" in with_t
    assert SW.plan_system(GOAL, helpers=4, targets=[]) == base


def test_the_block_holds_only_this_targets_state_and_is_bounded():
    card = S.extract(SCOPE_MD)
    rows = [
        {"id": "f1", "title": "Cross-site scripting (XSS) on a.acme.example.com/search",
         "class": "xss", "target": "a.acme.example.com/search", "status": "unverified",
         "reasons": ["no observed request + response; missing: the payload executing"],
         "last_seen": 5},
        {"id": "f2", "title": "IDOR on shop.globex.example.net/api/orders/{id}", "class": "idor",
         "target": "shop.globex.example.net/api/orders/{id}", "status": "unverified",
         "reasons": ["missing: two accounts"], "last_seen": 6},
        {"id": "f3", "title": "Open redirect on acme.example.com/go", "class": "open_redirect",
         "target": "acme.example.com/go", "status": "proven", "reasons": [], "last_seen": 4}]
    tasks = [{"id": "t1", "title": "acme: check password reset", "status": "todo"},
             {"id": "t2", "title": "globex: map API", "status": "todo"},
             {"id": "t3", "title": "acme: old work", "status": "done"}]
    notes = "## Done\n- mapped endpoints\n## Remaining\n- test password reset\n## Leads\n- /api/v2/export\n"
    hosts = MT.target_hosts(card, "acme")
    assert hosts == ["*.acme.example.com"]
    text = MT.block("acme", S.render_target(card, "acme", MT.SCOPE_CHARS), rows, hosts, tasks,
                    notes, "notes/acme.md")
    assert text.startswith("YOUR TARGET: acme")
    assert "*.acme.example.com" in text and "XSS" in text and "missing: the payload" in text
    assert "PROVEN: Open redirect" in text                         # the apex counts for attribution
    assert "test password reset" in text and "/api/v2/export" in text and "mapped endpoints" in text
    assert "acme: check password reset" in text and "old work" not in text
    assert "globex" not in text and "shop.globex" not in text
    assert "notes/acme.md" in text and "unverified" in text
    assert len(text) <= MT.BLOCK_MAX_CHARS
    assert text == MT.block("acme", S.render_target(card, "acme", MT.SCOPE_CHARS), rows, hosts,
                            tasks, notes, "notes/acme.md")               # deterministic
    many = [dict(rows[0], id="x%d" % i, title="XSS %d on a.acme.example.com/p%d" % (i, i),
                 target="a.acme.example.com/p%d" % i) for i in range(80)]
    big = MT.block("acme", "S" * 5000, many, hosts, tasks * 40, "- [ ] item\n" * 3000,
                   "notes/acme.md")
    assert len(big) <= MT.BLOCK_MAX_CHARS and big.endswith("proves it.")


def test_overview_lists_every_target_with_its_notes_file():
    text = MT.overview(["acme", "globex"])
    assert "2 TARGETS" in text and "acme (notes/acme.md)" in text and "globex (notes/globex.md)" in text
    assert MT.overview(["acme"]) == ""


def test_craft_names_a_testing_goal():
    assert craft.is_app_testing(GOAL)
    assert craft.is_app_testing("pentest shop.example.com and blog.example.org")
    assert not craft.is_app_testing("build a todo app with a login page")


# --------------------------------------------------------------------------- #
# 3. A Multi run: one helper per target, each with only its own target
# --------------------------------------------------------------------------- #

def test_a_testing_run_plans_one_helper_per_target_each_with_only_its_target(world):
    proj = world["proj"]
    _finding(world["ledger"], proj, "a.acme.example.com/search", "xss", "unverified",
             "no observed request + response; missing: the payload executing in a browser")
    _finding(world["ledger"], proj, "shop.globex.example.net/api/orders/{id}", "idor",
             "unverified", "missing: two observed requests (one per account)")
    gid = world["board"].add_goal("Test the two programs", project_dir=str(proj))
    world["board"].add_task(gid, "acme: check password reset")
    world["board"].add_task(gid, "globex: map the API")
    systems = []
    run_turn, seen = _recorder()
    rid = SW.start(GOAL, str(proj), "opencode", _spawn, run_turn,
                   planner=_planner(GOOD_PLAN, systems))
    st = _wait(rid)
    assert st["state"] == SW.DONE, st
    assert "PER-TARGET PLAN" in systems[0] and "notes/globex.md" in systems[0]
    assert st["targets"] == ["acme", "globex"]
    assert [a["title"] for a in st["agents"]] == ["acme", "globex", SW.REVIEW_TITLE]
    assert [a.get("target") for a in st["agents"]] == ["acme", "globex", None]
    assert st["waves"][0] == [1, 2]                                   # side by side
    # ONE helper per target: "parallel": true never adds a co-pilot session on a target
    assert len(seen["acme"]) == 1 and len(seen["globex"]) == 1
    assert [a["pair"] for a in st["agents"][:2]] == [None, None]
    a, g = seen["acme"][0], seen["globex"][0]
    assert "*.acme.example.com" in a and "pay.acme.example.com" in a
    assert "accounts.example.com" in a and "No automated scanning" in a
    assert "XSS" in a and "acme: check password reset" in a and "notes/acme.md" in a
    for host in ("shop.globex.example.net", "admin.globex.example.net", "globex: map the API"):
        assert host not in a
    assert "shop.globex.example.net" in g and "IDOR" in g and "globex: map the API" in g
    for host in ("*.acme.example.com", "pay.acme.example.com", "a.acme.example.com/search"):
        assert host not in g
    final = seen["final"][0]
    assert "acme (notes/acme.md)" in final and "globex (notes/globex.md)" in final
    assert "notes/summary.md" in final


def test_a_planner_that_ignores_the_ask_gets_the_hubs_per_target_plan(world):
    systems = []
    run_turn, seen = _recorder()
    rid = SW.start(GOAL, str(world["proj"]), "opencode", _spawn, run_turn,
                   planner=_planner(ONE_PHASE_PLAN, systems))
    st = _wait(rid)
    assert [a["title"] for a in st["agents"]] == ["acme", "globex", SW.REVIEW_TITLE]
    assert "acme" in seen and "globex" in seen


def _norm(prompts, rid):
    return [p.replace(rid, "RUN") for p in prompts]


def test_off_or_not_a_testing_goal_changes_nothing(world, monkeypatch):
    plan = ('{"phases": [{"title": "Backend", "task": "build the API", "needs": []},'
            ' {"title": "Frontend", "task": "build the UI", "needs": []}]}')

    def run_once(goal):
        systems = []
        run_turn, seen = _recorder()
        rid = SW.start(goal, str(world["proj"]), "opencode", _spawn, run_turn,
                       planner=_planner(plan, systems))
        st = _wait(rid)
        return systems, sorted(_norm(sum(seen.values(), []), rid)), st

    on_build = run_once("build a todo app with a login page")
    monkeypatch.setattr(A, "_mt_on", lambda: False)
    off_test = run_once(GOAL)
    off_build = run_once("build a todo app with a login page")
    monkeypatch.setattr(SW, "_TARGET_HOOKS", {"names": None, "block": None})
    bare_build = run_once("build a todo app with a login page")
    for systems, prompts, st in (on_build, off_test, off_build, bare_build):
        assert "PER-TARGET" not in systems[0]
        assert not any("YOUR TARGET" in p or "THIS RUN WORKS ON" in p for p in prompts)
        assert "targets" not in st and all("target" not in a for a in st["agents"])
    assert on_build[0] == off_build[0] == bare_build[0]                # byte for byte
    assert on_build[1] == off_build[1] == bare_build[1]


def test_no_scope_card_means_no_split(world, monkeypatch):
    monkeypatch.setattr(A, "_scope_card_on", lambda: False)
    assert A._mt_targets_for(str(world["proj"]), GOAL) == []
    monkeypatch.setattr(A, "_scope_card_on", lambda: True)
    assert A._mt_targets_for(str(world["proj"]), GOAL) == ["acme", "globex"]
    assert A._mt_targets_for(str(world["proj"]), "write the README") == []
    assert A._mt_targets_for("", GOAL) == []


def test_api_scope_lists_the_targets(world, monkeypatch):
    # No control token: an earlier test on this worker may have created one.
    monkeypatch.setattr(A.config, "get_control_token", lambda: None)
    A._scope_refresh(str(world["proj"]), [])
    r = A.app.test_client().get("/api/scope", query_string={"project_dir": str(world["proj"])})
    assert r.status_code == 200 and r.get_json()["targets"] == ["acme", "globex"]


# --------------------------------------------------------------------------- #
# 4. Continue later: each target from where it stopped
# --------------------------------------------------------------------------- #

def test_a_later_run_continues_each_target_from_its_notes(world):
    proj = str(world["proj"])
    notes = ("## Done\n- mapped acme endpoints\n## Remaining\n- test password reset flow\n"
             "## Leads\n- /api/v2/export answered without a session\n")
    run_turn, _seen = _recorder(write_notes={"acme": (proj, notes)})
    first = _wait(SW.start(GOAL, proj, "opencode", _spawn, run_turn,
                           planner=_planner(GOOD_PLAN, [])))
    assert first["state"] == SW.DONE
    assert os.path.isfile(os.path.join(proj, "notes", "acme.md"))
    # a finding filed after the first run
    _finding(world["ledger"], proj, "x.acme.example.com/export", "info_disclosure",
             "needs_review", "a second model finds it plausible; missing: the data itself")
    run_turn2, seen2 = _recorder()
    second = _wait(SW.start(GOAL, proj, "opencode", _spawn, run_turn2,
                            planner=_planner(GOOD_PLAN, [])))
    assert second["state"] == SW.DONE
    a = seen2["acme"][0]
    assert "LAST STATE (from notes/acme.md" in a
    assert "test password reset flow" in a and "/api/v2/export answered" in a
    assert "mapped acme endpoints" in a and "NEEDS REVIEW" in a
    assert "LAST STATE" not in seen2["globex"][0]                      # globex wrote no notes


def test_continue_reruns_a_failed_target_with_its_own_block(world):
    proj = str(world["proj"])
    run_turn, seen = _recorder(fail={"globex"})
    rid = SW.start(GOAL, proj, "opencode", _spawn, run_turn, planner=_planner(GOOD_PLAN, []))
    st = _wait(rid)
    assert [a["state"] for a in st["agents"][:2]] == [SW.DONE, SW.FAILED]
    (world["proj"] / "notes").mkdir(exist_ok=True)
    (world["proj"] / "notes" / "globex.md").write_text("## Remaining\n- retry the cart\n", "utf-8")
    run_turn2, seen2 = _recorder()
    assert SW.resume(rid, _spawn, run_turn2) == rid
    st = _wait(rid)
    assert st["state"] == SW.DONE
    assert "acme" not in seen2                                         # done work is not redone
    assert "YOUR TARGET: globex" in seen2["globex"][0] and "retry the cart" in seen2["globex"][0]
    assert "final" in seen2                                            # the merge runs again


def test_targets_are_persisted_with_the_run():
    run = SW._Run(GOAL, ".", "opencode",
                  [{"title": "acme", "task": "t", "needs": [], "target": "acme"},
                   {"title": "globex", "task": "t", "needs": [], "target": "globex"}],
                  targets=["acme", "globex"])
    back = SW._Run.from_row(run.row())
    assert back.targets == ["acme", "globex"]
    assert [a.target for a in back.agents] == ["acme", "globex"]
    plain = SW._Run.from_row(SW._Run("g", ".", "opencode",
                                     [{"title": "a", "task": "t", "needs": []}]).row())
    assert plain.targets == [] and plain.agents[0].target is None
    assert "targets" not in plain.row() and "target" not in plain.row()["agents"][0]


def test_the_fitted_and_pair_prompts_carry_the_target_block(world):
    proj = str(world["proj"])
    A._scope_refresh(proj, [])
    run = SW._Run(GOAL, proj, "opencode",
                  [{"title": "acme", "task": "t", "needs": [], "target": "acme", "parallel": True},
                   {"title": "globex", "task": "t", "needs": [], "target": "globex"}],
                  targets=["acme", "globex"])
    agent = run.agents[0]
    fitted = SW._fit_agent_prompt(run, agent, 24000)
    assert "YOUR TARGET: acme" in fitted and "*.acme.example.com" in fitted
    assert fitted.index("YOUR TARGET") < fitted.index("THE PROJECT FOLDER IS")
    pair = SW._pair_prompt(run, agent)
    assert "YOUR TARGET: acme" in pair and "shop.globex.example.net" not in pair


def test_explicit_phases_are_tagged_not_replanned(world):
    run_turn, seen = _recorder()
    rid = SW.start(GOAL, str(world["proj"]), "opencode", _spawn, run_turn,
                   phases=[{"title": "acme", "task": "t", "needs": []},
                           {"title": "globex", "task": "t", "needs": []}])
    st = _wait(rid)
    assert [a["title"] for a in st["agents"]] == ["acme", "globex", SW.REVIEW_TITLE]
    assert [a.get("target") for a in st["agents"]] == ["acme", "globex", None]
    assert "YOUR TARGET: acme" in seen["acme"][0]
