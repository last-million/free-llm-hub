"""Pytest bootstrap: give tmp_path a basetemp that actually works, and never
let a test touch the real ~/.free-llm-hub/ state.

WHY THE BASETEMP PART EXISTS. pytest derives `tmp_path` / `tmp_path_factory`
from a per-user base directory, `<system temp>/pytest-of-<user>`. On this
machine that directory exists but cannot be listed OR removed -- even
`takeown` is denied -- so every test using `tmp_path` errored at COLLECTION:

    PermissionError: [WinError 5] ... 'C:\\...\\Temp\\pytest-of-hamza'

That was 238 errors across 11 files (test_agentic_chat 95, test_agentic_history
34, test_isolated_subscriptions 29, test_settings_export_import 23,
test_image_generation 12, ...). They were NOT failures, which is what made them
easy to keep scrolling past: those tests never ran at all, so a whole slice of
the suite was silently unverified while the summary line still said "passed".

Rather than depend on a directory whose permissions we cannot repair, point
pytest at a fresh base we create ourselves. Deliberately under the SYSTEM temp
dir and not inside the repo: this checkout lives in a OneDrive-synced folder,
and putting churn-heavy per-test directories there would have the sync client
racing the tests for the same files.

`basetemp` is only set when the user has not passed --basetemp explicitly, so
this never overrides a deliberate choice on CI or another machine.

WHY THE FREE_LLM_HUB_CONFIG PART EXISTS. config.py / usage_history.py /
quota.py / quick_history.py / image_history.py / agentic_history.py all
resolve their on-disk path through `os.environ.get("FREE_LLM_HUB_CONFIG")`,
re-read fresh on every call (never a frozen constant -- config.CONFIG_PATH is
computed once but never referenced again; the real reads/writes all call the
`_default_config_path()`-style function directly). 25 test files already rely
on this and set the env var themselves via monkeypatch.setenv(...), file by
file.

The other ~55 files never set it. Three of them (test_starved_retry.py,
test_outcome_learning.py, test_upstream_nonanswer.py) drive a REAL
app.app.test_client().post("/v1/chat/completions", ...) with only
_dispatch_chat/_build_chain monkeypatched -- everything downstream of "the
fake upstream answered" is the real code, including _record_chat_usage ->
usage_history.record(), which saves to disk on every single call, no
debounce. Found 2026-08-08 by noticing the REAL usage dashboard was 87%
"groq/llama" -- a model id that exists ONLY as a fixture literal in these
tests, never in the provider registry. Confirmed the write is synchronous and
unconditional by reading usage_history.record(); confirmed no test file
between them isolates; confirmed (by reading each of the ~11 other files that
also open a test_client but don't monkeypatch _dispatch_chat) that none of
them reach a real, unmocked upstream call -- they all reject before dispatch
(400 for a missing/tool-calling turn) or hit a non-chat route entirely, so
this was the whole blast radius, not the first sighting of a wider problem.

Fix: isolate for EVERY test by default, the same way the 25 opt-in files
already do it, so the next new test file gets this for free instead of having
to remember it. `if already set: return` mirrors the basetemp guard just
above -- a file's own monkeypatch.setenv still wins for the duration of that
test, and an operator/CI override of the real env var is never touched.
"""
import os
import tempfile

import pytest


@pytest.fixture(autouse=True)
def _isolated_claude_settings_stay_out_of_the_real_home(monkeypatch):
    """THE SAME PROBLEM, ONE MORE FILE. The /agent claude fallback writes the
    hub's modelPicker into the isolated copy's settings.json
    (agentic_chat._seed_claude_picker). 16 tests across 5 files build a real
    claude env against the REAL ~/.free-llm-hub/isolated-clis/claude/config
    (found 2026-09-27: the file reappeared after every suite run). Writes
    aimed at that one directory land in a sandbox instead; any other config
    home -- every test that passes its own -- is untouched."""
    try:
        import agentic_chat as ac
        real = os.path.normcase(os.path.abspath(ac._isolated_config_dir("claude")))
        orig = ac._claude_settings_file
    except Exception:                                            # noqa: BLE001
        return
    sandbox = os.path.join(tempfile.gettempdir(), "hub-pytest-hub-state",
                           "isolated-claude-config")

    def redirected(config_home):
        if os.path.normcase(os.path.abspath(config_home)) == real:
            os.makedirs(sandbox, exist_ok=True)
            return os.path.join(sandbox, "settings.json")
        return orig(config_home)

    monkeypatch.setattr(ac, "_claude_settings_file", redirected)


# THE USER'S OWN CLI CONFIGS. FOUND 2026-09-27: after a full-suite run the
# user's terminal ~/.config/opencode/opencode.json had lost its free-llm-hub
# provider block -- a test drove the real Connect/Disconnect code against the
# real home. The hub-state isolation above covers ~/.free-llm-hub only; the
# connectors write under ~/.config, ~/.codex, ~/.claude, ~/.kimi, AppData...
# So for the whole run every home-like variable points at a sandbox, and a
# tripwire fingerprints the real files before the run and FAILS the run if any
# of them changed -- the next leak is caught the first time, not noticed a day
# later as "my CLI stopped working".
_HOME_VARS = ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "APPDATA", "LOCALAPPDATA")
_REAL_CLI_FILES = (
    (".config", "opencode", "opencode.json"),
    (".config", "opencode", "opencode.jsonc"),
    (".codex", "config.toml"),
    (".codex", "model_catalog.json"),
    (".kimi", "config.toml"),
    (".qwen", ".env"),
    (".pi", "agent", "models.json"),
)
# Claude's settings.json is also written by the user's own Claude Code (/model,
# permissions), so only the keys the hub's connector owns are compared.
_CLAUDE_SETTINGS = (".claude", "settings.json")
_CLAUDE_HUB_ENV = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL",
                   "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                   "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_AUTO_COMPACT_WINDOW")


def _fingerprint_real_cli_configs(home):
    import hashlib
    import json
    out = {}
    for parts in _REAL_CLI_FILES:
        p = os.path.join(home, *parts)
        try:
            with open(p, "rb") as fh:
                out[p] = hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            out[p] = None
    p = os.path.join(home, *_CLAUDE_SETTINGS)
    try:
        with open(p, encoding="utf-8-sig") as fh:
            data = json.load(fh)
        env = data.get("env") if isinstance(data.get("env"), dict) else {}
        out[p] = json.dumps({"env": {k: env.get(k) for k in _CLAUDE_HUB_ENV},
                             "modelPicker": data.get("modelPicker")}, sort_keys=True)
    except (OSError, ValueError, AttributeError):
        out[p] = None
    return out


def _sandbox_the_home(config):
    real_home = os.path.expanduser("~")
    config._real_home = real_home
    config._real_cli_before = _fingerprint_real_cli_configs(real_home)
    sandbox = os.path.join(tempfile.gettempdir(), "hub-pytest-home")
    config._saved_home_env = {v: os.environ.get(v) for v in _HOME_VARS}
    try:
        os.makedirs(os.path.join(sandbox, "AppData", "Roaming"), exist_ok=True)
        os.makedirs(os.path.join(sandbox, "AppData", "Local"), exist_ok=True)
        os.makedirs(os.path.join(sandbox, ".config"), exist_ok=True)
    except OSError:
        return
    os.environ["HOME"] = sandbox
    os.environ["USERPROFILE"] = sandbox
    os.environ["XDG_CONFIG_HOME"] = os.path.join(sandbox, ".config")
    os.environ["APPDATA"] = os.path.join(sandbox, "AppData", "Roaming")
    os.environ["LOCALAPPDATA"] = os.path.join(sandbox, "AppData", "Local")


def pytest_unconfigure(config):
    for var, val in (getattr(config, "_saved_home_env", None) or {}).items():
        if val is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = val


def pytest_sessionfinish(session, exitstatus):
    config = session.config
    before = getattr(config, "_real_cli_before", None)
    if not before:
        return
    after = _fingerprint_real_cli_configs(config._real_home)
    changed = [p for p in before if before[p] != after.get(p)]
    if changed:
        tr = config.pluginmanager.get_plugin("terminalreporter")
        msg = ("A TEST CHANGED THE USER'S REAL CLI CONFIG (tests must use the sandbox "
               "home): " + ", ".join(changed))
        if tr:
            tr.write_line(msg, red=True, bold=True)
        else:
            print(msg)
        session.exitstatus = 1


def pytest_configure(config):
    _sandbox_the_home(config)
    base = os.path.join(tempfile.gettempdir(), "hub-pytest-base")

    # Two independent guards, each with its OWN "explicit wins" check -- an
    # operator passing --basetemp is not also an opinion about hub-state
    # isolation, and vice versa. Bundling them under one early return would
    # mean a --basetemp run silently skips hub-state isolation too.
    if not getattr(config.option, "basetemp", None):
        try:
            os.makedirs(base, exist_ok=True)
            # Prove it is usable before committing to it -- an unwritable base
            # here would swap one collection-time explosion for another.
            probe = os.path.join(base, ".write-probe")
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("ok")
            os.remove(probe)
            os.listdir(base)
        except OSError:
            pass                     # leave pytest's own default in place
        else:
            config.option.basetemp = base

    if not os.environ.get("FREE_LLM_HUB_CONFIG"):
        # A SIBLING of `base`, deliberately -- `base` is handed to pytest as
        # basetemp above, and pytest owns/prunes that directory itself (found
        # by this exact bug: nesting hub-state/ under it here first, its
        # config.json vanished between pytest_configure and the first test,
        # every agentic-chat test failed with agentic_chat_enabled=False, and
        # it reproduced only through the full `pytest` entrypoint -- a bare
        # `python -c` calling this same code left the file sitting there fine,
        # because nothing was cleaning it).
        hub_state = os.path.join(tempfile.gettempdir(), "hub-pytest-hub-state")
        try:
            os.makedirs(hub_state, exist_ok=True)
        except OSError:
            pass                     # best-effort; a test that needs this will fail loudly on its own
        else:
            os.environ["FREE_LLM_HUB_CONFIG"] = os.path.join(hub_state, "config.json")
            # A fresh config defaults agentic_chat_enabled to False (an explicit
            # opt-in for a real first-run user). But every test that exercises
            # /agent resume, streaming or timeout behaviour was written assuming
            # it is already on -- 22 tests across 6 files (test_agent_resume,
            # test_claude_stream_and_stale_resume, test_durable_stream,
            # test_stream_last_message_fallback, test_timeout_retry,
            # test_workspace) never toggled it themselves and were silently
            # passing only because the REAL config on this machine has it on.
            # Isolating them exposed that. Seed it on by default here, once, the
            # same way a returning user's real config already has it; any test
            # that specifically wants the OFF state already sets that itself
            # (grep tests/ for config.set_flag("agentic_chat_enabled", False) --
            # every such test is explicit, so this default never fights them).
            try:
                import agentic_chat as _ac
                _ac.set_master_enabled(True)
            except Exception:
                pass                 # best-effort; those 6 files fail loudly on their own if this didn't take

    # THE SAME PROBLEM, TWO MORE DIRECTORIES. memory.py and swarm_windows.py do
    # NOT resolve their path through FREE_LLM_HUB_CONFIG -- they each have their
    # own env var and default to ~/.free-llm-hub/, so the isolation above did
    # not cover them. Found 2026-09-10 by reading the REAL memory directory: 65
    # conversation files, every single one named "test-claude-..." or
    # "durable-test-...", written by tests that drive the real send_message
    # path. Exactly the usage_history sighting recorded above, one directory
    # over. Same shape of fix, same "explicit wins" guard, so a new test file
    # gets this without having to remember it.
    for var, name in (("FREE_LLM_HUB_MEMORY_DIR", "memory"),
                      ("FREE_LLM_HUB_SWARM_DIR", "swarm-runs")):
        if os.environ.get(var):
            continue
        path = os.path.join(tempfile.gettempdir(), "hub-pytest-hub-state", name)
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            pass                     # best-effort: both modules already treat
                                     # an unwritable directory as "no memory"
        else:
            os.environ[var] = path
