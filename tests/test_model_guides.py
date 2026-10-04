"""Model guides: evidence-backed instructions for weak and specific models.

Pure module (model_guides.py, stdlib only). Guides exist only for families with
evidence; each is short and cites it; guide_for / scaffold / sampling_for never
raise. NOTE: no pytest tmp_path here -- this machine's basetemp is
permission-denied.
"""
import ast
import os
import re
import shutil
import tempfile

import pytest

import model_guides as mg

# The families with hub.log / code-measured evidence (see AGENTS.md "Model
# guides"). Adding a file means adding its evidence AND this name.
EVIDENCED = {"deepseek", "glm", "kimi", "minimax", "gemini"}


def _fam(mid):
    """Stand-in for verify.family: vendor-ish names the hub might return."""
    low = mid.lower()
    for key, name in (("deepseek", "deepseek"), ("glm", "z-ai"), ("kimi", "moonshot"),
                      ("minimax", "minimax"), ("gemini", "gemini"), ("gemma", "gemma"),
                      ("llama", "llama"), ("qwen", "qwen")):
        if key in low:
            return name
    return "unknown"


def _files():
    return sorted(n for n in os.listdir(mg.GUIDES_DIR) if n.endswith(".md"))


# --------------------------------------------------------------- the files

def test_guides_exist_only_for_evidenced_families():
    assert set(mg.available_families()) == EVIDENCED
    assert {"weak.md", "strong.md"} <= set(_files())
    # qwen, llama, gemma, mistral, claude... have no behaviour evidence: no file
    for fam in ("qwen", "llama", "gemma", "mistral", "claude", "gpt", "nemotron"):
        assert not os.path.exists(os.path.join(mg.GUIDES_DIR, fam + ".md"))


@pytest.mark.parametrize("name", [n[:-3] for n in sorted(os.listdir(mg.GUIDES_DIR))
                                  if n.endswith(".md")])
def test_every_guide_is_short_and_cites_evidence(name):
    with open(os.path.join(mg.GUIDES_DIR, name + ".md"), encoding="utf-8") as fh:
        raw = fh.read()
    assert raw.lstrip().startswith("<!-- evidence:"), "evidence comment must come first"
    assert len(re.findall(r"<!--\s*evidence:", raw)) >= 1
    body = mg.guide_body(name)
    assert body.strip()
    assert len(body) <= mg.FILE_BODY_MAX_CHARS
    assert "<!--" not in body and "evidence:" not in body
    # direct instructions, one per bullet
    assert all(line.startswith("- ") for line in body.splitlines())


def test_unknown_sections_never_leak_and_kind_selects_sections():
    tools = mg.guide_body("weak", "tools")
    answer = mg.guide_body("weak", "answer")
    assert "native format" in tools and "Put the answer first" not in tools
    assert "Put the answer first" in answer and "native format" not in answer
    full = mg.guide_body("weak")
    assert "native format" in full and "Put the answer first" in full
    assert mg.guide_body("weak", "something-new") == full      # unknown kind = all


# --------------------------------------------------------------- is_weak

@pytest.mark.parametrize("score,weak", [
    (10, True), (44, True), (119.99, True), (120, False), (134.1, False), (138, False),
    (None, True), ("abc", True), (float("nan"), True), (True, True), ("130", False),
])
def test_is_weak(score, weak):
    assert mg.is_weak(score) is weak


def test_weak_score_sits_under_the_strong_band():
    assert mg.WEAK_SCORE < 130 < 133.6          # category evidence, glm-5.3-flash
    assert mg.WEAK_SCORE > 100                  # Tier S base without bonuses


# --------------------------------------------------------------- guide_for

def test_weak_known_family_gets_family_then_weak_guide():
    g = mg.guide_for("dahl/deepseek-ai/DeepSeek-V4-Flash-0731", _fam, 60, "tools")
    assert g.startswith(mg.HEADER)
    assert "DSML" in g                                   # deepseek line
    assert "Read a file before editing it" in g         # weak line
    assert g.index("DSML") < g.index("Read a file before editing it")
    assert len(g) <= mg.GUIDE_MAX_CHARS


def test_strong_known_family_gets_family_plus_minimal_strong():
    g = mg.guide_for("nvidia/z-ai/glm-5.3", _fam, 138, "tools")
    assert "<arg_value>" in g                            # glm line (via alias z-ai)
    assert "Back any \"done\" with a check you actually ran." in g
    assert "Read a file before editing it" not in g      # no weak scaffolding


def test_strong_unknown_family_gets_at_most_strong_md():
    g = mg.guide_for("openrouter/stealth/space-bunny-alpha", _fam, 137.7)
    assert g == mg.HEADER + "\n" + mg.guide_body("strong")


def test_unknown_family_gets_weak_md_only_when_weak():
    weak = mg.guide_for("groq/llama-3.3-70b-versatile", _fam, 48.6)
    assert weak == mg.HEADER + "\n" + mg.guide_body("weak")
    strong = mg.guide_for("groq/llama-3.3-70b-versatile", _fam, 125)
    assert "Read a file before editing it" not in strong


def test_gemma_is_never_read_as_gemini():
    g = mg.guide_for("google/gemma-4-31b-it", None, 26)
    assert "think briefly" not in g
    assert g == mg.HEADER + "\n" + mg.guide_body("weak")


def test_builtin_detection_when_family_callable_is_missing_or_unhelpful():
    for fam in (None, lambda m: "other", lambda m: None, "unknown"):
        g = mg.guide_for("g4f/AnyProvider:kimi-k3", fam, 138.1, "answer")
        assert "Never narrate the request" in g
    assert "Never narrate the request" in mg.guide_for("x/kimi-k3", "moonshotai", 138)


def test_answer_kind_drops_tool_lines():
    g = mg.guide_for("llm7/GLM-5.3-Flash", _fam, 60, "answer")
    assert "native tool-call channel" not in g and "native format" not in g
    assert "Reply only in the user's language." in g
    assert "Put the answer first" in g


def test_trimmed_on_whole_lines_family_first(monkeypatch):
    monkeypatch.setattr(mg, "GUIDE_MAX_CHARS", 300)
    g = mg.guide_for("deepseek-v4-flash", None, 10)
    assert len(g) <= 300
    lines = g.splitlines()
    full = (mg.HEADER + "\n" + mg.guide_body("deepseek") + "\n" + mg.guide_body("weak")).splitlines()
    assert lines == full[:len(lines)]                    # a prefix of whole lines


@pytest.mark.parametrize("args", [
    (None, None, None), (123, 5, object()), ("", lambda m: 1 / 0, "x"),
    (["a"], {"b": 1}, float("inf")), ("deepseek", "deepseek", "nan", 42),
])
def test_guide_for_never_raises(args):
    out = mg.guide_for(*args)
    assert isinstance(out, str) and len(out) <= mg.GUIDE_MAX_CHARS


def test_missing_guides_dir_gives_empty_string(monkeypatch):
    monkeypatch.setattr(mg, "GUIDES_DIR", os.path.join(tempfile.gettempdir(), "no-such-guides-xyz"))
    assert mg.guide_for("deepseek-v4", None, 10) == ""
    assert mg.available_families() == []


def test_reads_are_cached_and_follow_edits(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="mg-test-")
    try:
        path = os.path.join(tmp, "weak.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("<!-- evidence: test -->\n- One.\n")
        monkeypatch.setattr(mg, "GUIDES_DIR", tmp)
        monkeypatch.setattr(mg, "_cache", {})
        assert mg.guide_body("weak") == "- One."
        calls = []
        real_open = open
        monkeypatch.setattr("builtins.open",
                            lambda *a, **k: calls.append(a) or real_open(*a, **k))
        assert mg.guide_body("weak") == "- One."
        assert calls == []                                # served from cache
        monkeypatch.setattr("builtins.open", real_open)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("<!-- evidence: test -->\n- Two.\n")
        st = os.stat(path)
        os.utime(path, (st.st_atime, st.st_mtime + 5))
        assert mg.guide_body("weak") == "- Two."
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------- scaffold

def test_scaffold_weak():
    s = mg.scaffold(60, "tools")
    assert s["max_step_scope"] == "one file or one function"
    assert s["always_verify"] is True
    assert s["context_budget_tokens"] == mg.WEAK_CONTEXT_TOKENS == 12000
    assert s["temperature"] is None
    assert s["checklist"] and all(isinstance(c, str) and c for c in s["checklist"])
    assert any("check" in c for c in s["checklist"])


def test_scaffold_very_weak_gets_smaller_context():
    assert mg.scaffold(44)["context_budget_tokens"] == 8000
    assert mg.scaffold(None)["context_budget_tokens"] == 8000


def test_scaffold_answer_checklist_has_no_tool_steps():
    items = mg.scaffold(30, "answer")["checklist"]
    assert not any("file" in c.lower() for c in items)


@pytest.mark.parametrize("score", [120, 133.6, 138.1])
def test_scaffold_strong_is_empty(score):
    assert mg.scaffold(score) == {}
    assert mg.scaffold(score, "tools") == {}


@pytest.mark.parametrize("args", [(object(),), ([], []), ("x", 5), (float("nan"), None)])
def test_scaffold_never_raises(args):
    assert isinstance(mg.scaffold(*args), dict)


def test_scaffold_returns_fresh_lists():
    a = mg.scaffold(10, "tools")
    a["checklist"].append("mutated")
    assert "mutated" not in mg.scaffold(10, "tools")["checklist"]


# --------------------------------------------------------------- sampling

def test_sampling_follows_the_card_for_that_version_only():
    assert mg.sampling_for("google/models/gemini-3.7-flash") == {"temperature": 1.0}
    assert mg.sampling_for("zenmux/z-ai/glm-4.7-flash-free", "tools") == {
        "temperature": 0.7, "top_p": 1.0}
    assert mg.sampling_for("glm/glm-4.7-flash")["temperature"] == 1.0
    assert mg.sampling_for("nvidia/z-ai/glm-5.3") == {}          # no card fetched for 5.3
    assert mg.sampling_for("groq/llama-3.3-70b-versatile") == {}
    assert mg.sampling_for(None) == {} and mg.sampling_for(42, object()) == {}


# --------------------------------------------------------------- purity

def test_module_is_pure_stdlib_and_never_imports_app():
    src = open(mg.__file__, encoding="utf-8").read()
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mods.add((node.module or "").split(".")[0])
    assert mods <= {"math", "os", "re", "threading"}
