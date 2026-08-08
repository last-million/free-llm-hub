"""Pytest bootstrap: give tmp_path a basetemp that actually works.

WHY THIS EXISTS. pytest derives `tmp_path` / `tmp_path_factory` from a
per-user base directory, `<system temp>/pytest-of-<user>`. On this machine that
directory exists but cannot be listed OR removed -- even `takeown` is denied --
so every test using `tmp_path` errored at COLLECTION:

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
"""
import os
import tempfile


def pytest_configure(config):
    if getattr(config.option, "basetemp", None):
        return                      # an explicit --basetemp always wins
    base = os.path.join(tempfile.gettempdir(), "hub-pytest-base")
    try:
        os.makedirs(base, exist_ok=True)
        # Prove it is usable before committing to it -- an unwritable base here
        # would swap one collection-time explosion for another.
        probe = os.path.join(base, ".write-probe")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        os.listdir(base)
    except OSError:
        return                      # leave pytest's own default in place
    config.option.basetemp = base
