"""Review findings on the 2026-09-27 changes.

1. The dangling-colon / bare-tool-name checks ran on a PARTIAL stream (the
   600-byte mid-stream judge), so "Changes made to parser.py:" + a list was
   thrown away as a non-answer. They now run only on a complete reply.
2. A "README.md for tally.py" phase was treated as the code phase for tally.py.
3. A tests phase needing core.py AND cli.py was rejected for not importing both.
4. Kimi Connect wrote a second [models."auto"] next to the user's own one.
5. Manager calls abandoned at their deadline were missing from manager_tokens.
6. Hidden-mode hover sidebar: group labels shrank (no width:100%).
No network.
"""
import json
import os
import re
import shutil
import tempfile
import threading
import time
import tomllib

import pytest

import app as A
import swarm

TOOLS = [{"type": "function", "function": {"name": "shell_command",
                                           "parameters": {"type": "object"}}}]


def _frames(deltas):
    # Real frames are ~200 bytes (id/model/created...), so three of them pass
    # the 600-byte mid-stream judge -- the boundary this regression lived on.
    out = ['data: ' + json.dumps({"id": "chatcmpl-" + "x" * 60, "object": "chat.completion.chunk",
                                  "model": "provider/some-model-name-" + "y" * 30,
                                  "choices": [{"index": 0, "delta": {"content": d}}]})
           for d in deltas]
    return [f.encode() for f in out] + [b'data: [DONE]']


def _payload():
    return {"model": "m", "messages": [{"role": "user", "content": "fix the parser"}],
            "tools": TOOLS}


# 1 ------------------------------------------------------------------------ #

def test_a_colon_at_the_mid_stream_judge_boundary_is_not_a_nonanswer():
    deltas = ["Changes", " made", " to parser.py:", "\n\n- fixed the off-by-one",
              "\n- added a test", "\n\nAll done."]
    status, _ = A._peek_until_content(iter(_frames(deltas)), 5,
                                      check=A._peek_check(_payload(), True))
    assert status == "content"


def test_partial_judge_skips_colon_and_bare_name_but_complete_does_not():
    chk = A._peek_check(_payload(), True)
    colon = ['data: ' + json.dumps({"choices": [{"delta": {"content": "Changes made to parser.py:"}}]})]
    bare = ['data: ' + json.dumps({"choices": [{"delta": {"content": "shell_command"}}]})]
    assert A._judge_peeked(colon, chk, complete=False) == "content"
    assert A._judge_peeked(bare, chk, complete=False) == "content"
    assert A._judge_peeked(colon, chk) == "nonanswer"
    assert A._judge_peeked(bare, chk) == "nonanswer"


def test_a_short_complete_dangling_reply_is_still_caught_on_the_stream():
    status, _ = A._peek_until_content(iter(_frames(["Let me check the tests:"])), 5,
                                      check=A._peek_check(_payload(), True))
    assert status == "nonanswer"


# 2 ------------------------------------------------------------------------ #

def test_a_readme_phase_naming_the_py_file_is_docs_not_code():
    ph = {"title": "README.md for tally.py", "task": "document usage"}
    assert swarm._is_docs_phase(ph)
    assert swarm._code_file(ph) == ""
    assert swarm._code_problems(ph, "# tally\n\nUsage: `python tally.py -l`") == []
    # a real code phase is unchanged
    assert swarm._code_file({"title": "tally.py", "task": "the CLI"}) == "tally.py"
    assert not swarm._is_docs_phase({"title": "tally.py", "task": "usage section in --help"})


# 3 ------------------------------------------------------------------------ #

CORE = "def add(a, b):\n    return a + b\n"
CLI = "import core\n\ndef main(argv=None):\n    return 0\n"


def test_tests_importing_one_of_several_dependencies_pass():
    ph = {"title": "test_core.py", "task": "pytest for core.py"}
    tests = "```python\nfrom core import add\n\ndef test_add():\n    assert add(1, 2) == 3\n```"
    assert swarm._code_problems(ph, tests, deps={"core": CORE, "cli": CLI}) == []


def test_tests_importing_none_name_the_module_the_file_is_for():
    ph = {"title": "test_cli.py", "task": "pytest"}
    tests = "```python\ndef test_x():\n    assert True\n```"
    probs = swarm._code_problems(ph, tests, deps={"core": CORE, "cli": CLI})
    assert any("never import cli " in p for p in probs), probs
    assert swarm._tested_stem(ph, {"core": CORE, "cli": CLI}) == "cli"
    assert swarm._tested_stem({"title": "Tests", "task": "cover cli.py"},
                              {"core": CORE, "cli": CLI}) == "cli"


def test_bad_names_are_still_caught_on_the_imported_module():
    ph = {"title": "test_core.py", "task": "pytest"}
    tests = "```python\nfrom core import nope\n\ndef test_x():\n    assert nope()\n```"
    probs = swarm._code_problems(ph, tests, deps={"core": CORE, "cli": CLI})
    assert any("nope" in p for p in probs), probs


# 4 ------------------------------------------------------------------------ #

USER_AUTO = ('default_model = "kimi-code/k3"\n\n[providers."managed:kimi-code"]\n'
             'type = "kimi"\n\n[models."auto"]\nprovider = "managed:kimi-code"\n'
             'model = "k3"\nmax_context_size = 1000\n')


@pytest.fixture
def home(monkeypatch):
    d = tempfile.mkdtemp(prefix="hub-pytest-kimiauto-")
    monkeypatch.setattr(A, "_home", lambda: d)
    monkeypatch.delenv("KIMI_CODE_HOME", raising=False)
    store = {}
    monkeypatch.setattr(A.config, "set_setting", lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr(A.config, "get_setting", lambda k, default=None: store.get(k, default))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_kimi_apply_never_emits_a_duplicate_auto_alias():
    text = A._kimi_apply_text(USER_AUTO, "http://127.0.0.1:1/v1", "k")
    tomllib.loads(text)                               # valid TOML
    assert len(re.findall(r'(?m)^\[models\."auto"\]', text)) == 1


def test_kimi_connect_refuses_over_a_users_own_auto_alias(home):
    path = os.path.join(home, ".kimi-code", "config.toml")
    os.makedirs(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write(USER_AUTO)
    e = dict(A._get_cli_entry("kimi"))
    res = A._autofix_kimi(e, "k", "http://127.0.0.1:1", "http://127.0.0.1:1/v1", "x")
    assert res["ok"] is False and 'models."auto"' in res["reason"]
    with open(path, encoding="utf-8") as f:
        assert f.read() == USER_AUTO                  # untouched
    assert not os.path.exists(path + ".freehub-bak")


# 5 ------------------------------------------------------------------------ #

def test_abandoned_manager_calls_still_count_their_tokens(monkeypatch):
    release = threading.Event()

    def manager(msgs, max_tokens, purpose):
        if purpose == "plan":
            release.wait(5)
            return ("late plan", "mgr", 70)
        return ("", None, 0)                          # refused: never ran

    monkeypatch.setitem(swarm.MANAGER_DEADLINES, "plan", 0.2)

    def dispatch(msgs, max_tokens, **kw):
        release.set()
        time.sleep(0.3)                               # the late manager call lands
        return ("free answer", "free/model")

    out = swarm.run([{"role": "user", "content": "say hi"}], dispatch, manager=manager)
    assert out["manager_tokens"] == 70
    assert out["manager_calls"] == 1


# 6 ------------------------------------------------------------------------ #

def test_hidden_mode_hover_labels_are_full_width_with_a_caret_for_keyboard_too():
    html = open(os.path.join(os.path.dirname(A.__file__), "templates", "index.html"),
                encoding="utf-8").read()
    rule = re.search(r"body\.cx-hidden \.cx-sidebar:hover \.cx-group-label,\s*"
                     r"body\.cx-hidden \.cx-sidebar:focus-within \.cx-group-label\{([^}]*)\}",
                     html)
    assert rule and "width:100%" in rule.group(1).replace(" ", "")
    assert re.search(r"body\.cx-hidden \.cx-sidebar:focus-within \.cx-group-label "
                     r"\.gl-caret\{\s*display:block", html)
