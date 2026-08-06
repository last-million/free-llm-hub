"""Tests for the quick-chat PROJECT GATE (templates/index.html).

User request 2026-08-06: a full-project opening on Auto must ASK "crew swarm
or plain answer?" before sending — a crew run costs 5-20 silent minutes, so
the choice belongs to the user, never to a routing regex. Small asks (a
landing page, a question) must NOT be gated.

Two layers:
- structural markers in the template (gate wiring, chooser buttons, resets)
- the REAL looksLikeFullProject() extracted from the page and executed in
  node against triggering and non-triggering prompts (skipped without node)

NOTE: no pytest tmp_path here — this machine's basetemp is permission-denied;
tempfile.mkdtemp(prefix="hub-pytest-") works.
"""

import io
import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

import app

REPO_ROOT = os.path.dirname(os.path.abspath(app.__file__))
TEMPLATE = os.path.join(REPO_ROOT, "templates", "index.html")


def _html():
    with io.open(TEMPLATE, encoding="utf-8") as fh:
        return fh.read()


# --------------------------------------------------------------------------- #
# Structural wiring
# --------------------------------------------------------------------------- #

def test_gate_function_and_state_exist():
    html = _html()
    assert "function looksLikeFullProject(t){" in html
    assert "function askProjectGate(){" in html
    assert "var projectGateAnswered = false;" in html


def test_gate_only_fires_on_opening_turn_with_auto():
    """A deliberate picker choice (a crew id, or a specific provider/model) IS
    already the user's answer — the gate must never nag then."""
    html = _html()
    i = html.find("model === 'auto' && looksLikeFullProject(content)")
    assert i != -1, "gate condition not found"
    c = html[max(0, i - 300):i]
    assert "if (" in c
    assert "!history.length" in c          # opening turn only
    assert "!projectGateAnswered" in c     # once per conversation


def test_chooser_crew_button_selects_the_crew_model_visibly():
    html = _html()
    assert "modelSel.value = 'crew'; sendChat();" in html


def test_gate_state_resets_with_the_conversation():
    """New chat / clear must re-arm the gate, exactly like openingEnhanced."""
    html = _html()
    assert html.count("projectGateAnswered = false;") >= 3  # declaration + 2 resets


def test_pipeline_regex_shared_between_gate_and_wait_notice():
    """The 'crew answer takes minutes' notice and the gate must agree on what a
    pipeline model is — two drifting regexes were the risk."""
    html = _html()
    assert "var PIPELINE_MODEL_RE = /^(crew|swarm|team|plan)" in html
    assert "if (PIPELINE_MODEL_RE.test(model)){" in html


# --------------------------------------------------------------------------- #
# The heuristic itself, executed for real
# --------------------------------------------------------------------------- #

def _run_heuristic(cases):
    """Extract looksLikeFullProject from the template and run it in node.
    Returns {case: bool}."""
    html = _html()
    m = re.search(r"(function looksLikeFullProject\(t\)\{.*?\n    \})", html, re.S)
    assert m, "could not extract looksLikeFullProject from the template"
    script = (m.group(1) + "\n"
              "var cases = %s;\n"
              "cases.forEach(function(c){ console.log(JSON.stringify([c, looksLikeFullProject(c)])); });\n"
              % json.dumps(cases))
    tmp = tempfile.mkdtemp(prefix="hub-pytest-")
    try:
        js = os.path.join(tmp, "gate.js")
        with io.open(js, "w", encoding="utf-8") as fh:
            fh.write(script)
        out = subprocess.run(["node", js], capture_output=True, text=True, timeout=30)
        assert out.returncode == 0, out.stderr
        return {c: bool(v) for c, v in (json.loads(line) for line in out.stdout.splitlines())}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_heuristic_fires_on_full_projects_only():
    project = "Build me a complete multi-page website for my restaurant with " \
              "a menu page, a gallery, online reservations and a contact form"
    fullstack = "Create a full-stack e-commerce app from scratch"
    saas = ("Develop a SaaS dashboard application with user authentication, " \
            "billing, an admin panel, and reporting charts")
    res = _run_heuristic([project, fullstack, saas])
    assert res[project] is True
    assert res[fullstack] is True
    assert res[saas] is True


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_heuristic_stays_quiet_on_basic_asks():
    landing = "Create a landing page for a coffee shop"      # single artefact = basic
    question = "what is the difference between TCP and UDP and ICMP and ARP?"
    bug = "fix my python bug please"
    res = _run_heuristic([landing, question, bug])
    assert res[landing] is False
    assert res[question] is False
    assert res[bug] is False
