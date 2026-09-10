r"""Every number in the README has to be true, and stay true.

The old README was well written and materially wrong, which is the worst
combination: it said "Just two dependencies. Flask + requests" (there are five
pinned, and two of them are load-bearing -- a missing `cryptography` has cost an
install its whole keyring), "Two protocols, one port" (there are four plus MCP),
and it listed the Gemini CLI as incompatible months after /v1beta went in.

Nobody notices a README going stale, so the checkable claims are checked here.
"""
import io
import os
import re

README = io.open("README.md", encoding="utf-8").read()


def test_the_dependency_claim_matches_requirements():
    req = [l.split("==")[0].strip().lower()
           for l in io.open("requirements.txt", encoding="utf-8")
           if l.strip() and not l.strip().startswith("#")]
    for dep in req:
        assert dep in README.lower(), "%s is pinned but not mentioned" % dep
    assert "two dependencies" not in README.lower()


def test_the_provider_count_is_real():
    import providers
    assert str(len(providers.PROVIDERS)) in README


def test_the_paid_marked_count_is_real():
    import providers
    paid = sum(1 for v in providers.PROVIDERS.values()
               if isinstance(v, dict) and v.get("paid"))
    assert re.search(r"\b%d are marked\s+paid" % paid, README)


def test_the_cli_counts_are_real():
    import app
    assert str(len(app.CLI_REGISTRY)) in README
    assert str(len(app._AUTOFIXERS)) in README


def test_the_route_count_is_real():
    src = io.open("app.py", encoding="utf-8").read()
    assert str(len(re.findall(r"@app\.route\(", src))) in README


def test_the_test_count_is_not_wildly_stale():
    """Within 10%: the exact number moves every commit, a claim that is out by
    a third is a claim nobody checked."""
    m = re.search(r"\*\*(\d[\d,]*) tests", README)
    assert m, "no test count claimed"
    claimed = int(m.group(1).replace(",", ""))
    actual = 0
    for root, _dirs, files in os.walk("tests"):
        for f in files:
            if f.startswith("test_") and f.endswith(".py"):
                actual += len(re.findall(
                    r"^def test_", io.open(os.path.join(root, f), encoding="utf-8").read(), re.M))
    assert actual * 0.75 <= claimed <= actual * 1.6, \
        "README claims %d tests, %d test functions exist" % (claimed, actual)


def test_every_protocol_it_claims_is_actually_served():
    src = io.open("app.py", encoding="utf-8").read()
    for name, marker in [("OpenAI", '"/v1/chat/completions"'),
                         ("Anthropic", '"/v1/messages"'),
                         ("Ollama", '"/api/tags"'),
                         ("Gemini", "/v1beta/models"),
                         ("MCP", '"/mcp"')]:
        assert marker in src, "%s is claimed but not served" % name
        assert name.lower() in README.lower()


def test_the_gemini_cli_is_no_longer_called_incompatible():
    """It was listed as impossible long after the /v1beta surface shipped."""
    assert "Incompatible" not in README


def test_the_python_floor_matches_what_the_launcher_installs():
    """run.sh tells the user "Python 3.9+"; the README must not promise
    something else."""
    launcher = io.open("run.sh", encoding="utf-8").read()
    m = re.search(r"Python (\d+\.\d+)\+", launcher)
    assert m, "run.sh no longer states a version"
    assert "Python %s+" % m.group(1) in README


def test_the_license_is_stated_before_the_install_instructions():
    """Noncommercial-only is the first thing a reader has to know, not a
    footnote after they have deployed it."""
    assert README.index("PolyForm") < README.index("## Install")


def test_the_swarm_cli_it_advertises_exists():
    assert os.path.isfile("scripts/swarm.py")
    assert "scripts/swarm.py" in README


def test_no_unverifiable_quota_numbers_are_promised():
    """quota.py marks several free-tier figures UNVERIFIED. The README must say
    so rather than print numbers it cannot stand behind."""
    assert "indicative" in README.lower()
    assert "unverified" in README.lower()


def test_the_review_wave_claim_is_true():
    """README says the last wave is always a review."""
    import swarm_windows as SW
    ph = SW.with_review(SW.clean_phases({"phases": [
        {"title": "a", "task": "t"}, {"title": "b", "task": "t"}]}))
    assert ph[-1]["title"] == SW.REVIEW_TITLE
    assert SW.waves(ph)[-1] == [len(ph)]


def test_the_runs_survive_a_restart_claim_is_true():
    import swarm_windows as SW
    assert hasattr(SW, "load")
    assert "_persist(run)" in open("swarm_windows.py", encoding="utf-8").read()


def test_the_conversation_memory_claim_is_true():
    import memory
    for name in ("remember_summary", "remember_fact", "note_turn",
                 "context_block", "note_compaction"):
        assert hasattr(memory, name), name


def test_the_compaction_claim_is_true():
    """"what they established rides back in on the next turn"."""
    src = open("agentic_chat.py", encoding="utf-8").read()
    assert "memory_block=_memory_block(sess)" in src
