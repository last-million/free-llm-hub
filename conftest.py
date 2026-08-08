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


def pytest_configure(config):
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
