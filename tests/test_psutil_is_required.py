"""psutil must be pinned: without it, leaked previews are never reclaimed.

workspace._kill_listener() opens with `try: import psutil / except ImportError:
return False`, and sweep_own_range() is built on it. So on an install without
psutil the reclaim silently does NOTHING -- no error, no log line, just a hub
that never frees a preview port.

MEASURED 2026-08-30 on a live machine: 18 orphaned `python -m http.server`
processes, 7 still holding ports 5800-5806 and refusing connections. A user
opening a preview URL in that state gets a dead server, which is what
"all pages 404" looks like from the browser.

PORT_RANGE is 100 ports wide, so the failure is slow and quiet: previews work
fine for weeks, then stop, and nothing points at the cause.
"""
import io
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _requirements():
    return io.open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8").read()


def test_psutil_is_pinned_in_requirements():
    req = _requirements()
    assert re.search(r"^psutil==\d+\.\d+", req, re.M), \
        "psutil missing from requirements.txt -- preview reclaim silently no-ops"


def test_every_requirement_is_pinned_exactly():
    """The file's own rule: exact versions, never a widened lower bound."""
    for line in _requirements().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        assert "==" in line, "unpinned requirement: %r" % line


def test_the_reclaim_really_does_depend_on_psutil():
    """Guards the reason this pin exists: if _kill_listener stops importing
    psutil, this test should be revisited rather than silently kept."""
    src = io.open(os.path.join(ROOT, "workspace.py"), encoding="utf-8").read()
    i = src.find("def _kill_listener")
    assert i != -1
    body = src[i:i + 600]
    assert "import psutil" in body
    assert "return False" in body, "the ImportError path must still fail closed"


def test_psutil_is_actually_importable_here():
    """Belt and braces: the pin is worthless if the environment lacks it."""
    import psutil  # noqa: F401
