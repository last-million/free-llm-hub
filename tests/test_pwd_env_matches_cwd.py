"""opencode was writing into the HUB'S OWN SOURCE TREE instead of the
project a session was started for.

`cwd=sess.project_dir` was always passed correctly to Popen -- confirmed with
a raw `cmd.exe /c cd` probe that echoed back the exact right path. The bug was
one level up: `_agentic_env()` builds its child env from `dict(os.environ)`,
which carries PWD from whatever shell launched the hub. `run.bat`/`run.sh`
both `cd` into the HUB'S OWN directory before starting Python, so PWD in the
hub's own process is always the hub's repo -- for every user, not a dev-only
quirk. Popen's `cwd=` changes the OS-level working directory but never
touches PWD in the environment dict, and opencode reads PWD to decide its
project root instead of asking the OS for the real one.

Root-caused by elimination, one variable at a time, each confirmed live:
isolated config-home (fresh AND the shared one) -- ruled out. Isolated
data-home (fresh) -- ruled out. A completely fresh `npm install` from a
neutral directory -- ruled out. `--pure` (disables external plugins) --
ruled out. The target being a real git repo -- ruled out. The machine's REAL,
non-isolated opencode config and data directories moved out of the way
entirely -- ruled out. Setting `PWD` to match `cwd` was the one change that
fixed it, every time, confirmed via a real end-to-end session through the
live hub (a file landed in the intended project folder, not the hub's own
repo root).
"""
import agentic_chat as ac


def test_agentic_env_sets_pwd_to_match_the_project_directory():
    env = ac._agentic_env("opencode", project_dir=r"C:\Users\x\calvoun-projects\demo")
    assert env["PWD"] == r"C:\Users\x\calvoun-projects\demo"


def test_pwd_is_left_alone_when_no_project_dir_is_given():
    """The binary-identity check calls _agentic_env("claude") with no cwd
    override of its own -- nothing to resync PWD against, so it must not be
    invented or blanked."""
    import os
    env = ac._agentic_env("claude")
    assert env.get("PWD") == os.environ.get("PWD")


def test_every_real_turn_spawn_passes_project_dir_to_agentic_env():
    """Two call sites actually spawn a turn (send_message, send_message_stream).
    Both must resync PWD -- fixing only one would leave the bug alive on
    whichever path the fix missed."""
    src = open(ac.__file__, encoding="utf-8", errors="replace").read()
    assert src.count("env=_agentic_env(sess.cli_id, sess.project_dir)") == 2, (
        "expected both send_message() and send_message_stream() to resync PWD")


def test_pwd_override_survives_the_hub_pointing_strip():
    """_agentic_env also strips any var whose VALUE points back at the hub
    (ANTHROPIC_BASE_URL and friends). PWD must not be caught in that net --
    it is a real path, not a hub callback."""
    env = ac._agentic_env("opencode", project_dir=r"C:\Users\x\calvoun-projects\demo")
    assert "PWD" in env
