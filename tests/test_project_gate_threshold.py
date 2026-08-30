"""The multi-part branch of the project gate was nearly dead code.

looks_like_full_project() has two paths: strong project vocabulary (no length
floor), and a weaker "build verb + artefact noun + several parts listed" path
guarded by BOTH a 60-char floor at the top of the function and a second
len > 100 check at the bottom.

MEASURED 2026-08-30: that second floor meant real multi-part builds never
reached the swarm. "build a portfolio website with a blog, a contact form and
dark mode" is unambiguous and 66 characters. `parts >= 2` is what actually
carries the signal; the extra length floor only suppressed it.

crews.looks_like_full_project and looksLikeFullProject() in templates/index.html
are mirrors -- if they disagree, the gate the user is OFFERED differs from the
one the server APPLIES. Both moved together; the last test here guards that.
"""
import io
import os
import re

import crews

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_a_short_multipart_build_now_reaches_the_swarm():
    assert crews.looks_like_full_project(
        "build a portfolio website with a blog, a contact form and dark mode")


def test_strong_project_vocabulary_still_needs_no_length():
    for t in ("build an e-commerce site",
              "Create a full-stack SaaS app from scratch",
              "make a complete website"):
        assert crews.looks_like_full_project(t), t


def test_single_artefact_asks_are_still_not_projects():
    """The swarm is a multi-minute pipeline. It must not fire on ordinary work."""
    for t in ("build me a landing page for a bakery",
              "design a logo concept",
              "write a python function to reverse a string",
              "make a motion design animation for a hero section"):
        assert not crews.looks_like_full_project(t), t


def test_questions_are_never_projects():
    for t in ("what is the capital of France", "hi", "is python faster than go", ""):
        assert not crews.looks_like_full_project(t), t


def test_short_asks_are_still_floored_out():
    """A 60-char floor still guards the weak path -- 'build an app and a blog'
    is too little to commit a multi-model pipeline to."""
    assert not crews.looks_like_full_project("build an app and a blog, ok?")


def test_the_python_and_javascript_gates_still_agree():
    """A drifted mirror means the user is offered the crew for one set of asks
    and the server runs it for another."""
    js = io.open(os.path.join(ROOT, "templates", "index.html"),
                 encoding="utf-8").read()
    m = re.search(r"function looksLikeFullProject\(t\)\{.*?\n    \}", js, re.S)
    assert m, "looksLikeFullProject() not found in the template"
    body = m.group(0)
    assert "parts >= 2" in body
    assert "s.length > 100" not in body, "JS still carries the removed floor"
    assert "s.length < 60" in body, "JS lost the 60-char floor"
