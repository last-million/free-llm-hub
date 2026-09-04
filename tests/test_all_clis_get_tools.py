"""Every /agent backend runs with full tool access.

REPORTED 2026-09-04 from a real build turn (session b513710b..., opencode,
swarm): four and a half minutes spent, and the whole answer was

    "Blocked: no tool permissions granted in this session.
     Read, Write, Bash, PowerShell all denied - cannot create files yet."

`opencode run --help` says why: `--auto` ("auto-approve permissions that are not
explicitly denied") DEFAULTS TO FALSE, and in non-interactive `run` mode there
is no prompt to answer, so every tool is simply denied.

claude has carried --dangerously-skip-permissions and codex
--dangerously-bypass-approvals-and-sandbox since this module was written.
opencode was missed, which made it the one backend that could not do the job
this module exists for -- and its own first paragraph promises "full tool access
... with full permissions ON BY DEFAULT".

These tests pin the grant for all three, because the failure is silent: the CLI
exits 0, the turn looks successful, and the agent politely explains that it
cannot do anything.
"""
import agentic_chat as ac


def _source():
    with open("agentic_chat.py", encoding="utf-8") as f:
        return f.read()


def _argv_builder(name):
    """The code that builds one backend's argv.

    codex and opencode have their own functions; claude's is built inline in
    _build_argv, which is why this looks the name up rather than assuming a
    _build_argv_<cli> exists for every backend."""
    src = _source()
    marker = "def _build_argv_" + name + "("
    if marker not in src:
        marker = "def _build_argv("
    i = src.index(marker)
    return src[i:src.index("\ndef ", i + 10)]


def test_every_backend_has_argv_building_code():
    """If a fourth CLI is added, it needs a permission grant too."""
    for cli in ac._CLI_BIN:
        assert _argv_builder(cli).strip(), cli


def test_opencode_auto_approves_its_tools():
    """The exact fix: without --auto it can read nothing and write nothing."""
    assert '"--auto"' in _argv_builder("opencode")


def test_claude_skips_its_permission_prompts():
    assert "--dangerously-skip-permissions" in _argv_builder("claude")


def test_codex_bypasses_its_approvals():
    assert "--dangerously-bypass-approvals-and-sandbox" in _argv_builder("codex")


def test_no_backend_is_left_without_a_grant():
    """The property that actually matters, checked across every backend at once
    rather than as one test per CLI that a new backend would not be added to."""
    grants = {
        "opencode": "--auto",
        "claude": "--dangerously-skip-permissions",
        "codex": "--dangerously-bypass-approvals-and-sandbox",
    }
    for cli in ac._CLI_BIN:
        assert cli in grants, "backend %r has no known permission grant" % cli
        assert grants[cli] in _argv_builder(cli), cli


def test_the_flag_comes_before_the_prompt():
    """opencode takes the prompt POSITIONALLY, so a flag appended after it would
    be read as part of the message rather than as a flag."""
    body = _argv_builder("opencode")
    assert body.index('"--auto"') < body.index("args += [prompt]")


def test_the_reason_is_recorded_next_to_the_flag():
    """A flag whose own help text says "(dangerous!)" will be questioned later;
    the measurement that justifies it belongs beside it."""
    body = _argv_builder("opencode")
    assert "no tool permissions granted" in body
