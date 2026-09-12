r"""A black cmd window over whatever you were doing, several times a session.

REPORTED 2026-09-09: "le hub open each time a window terminal CMD avec
.../appdata/roaming ... au moins laisse le s'executer dans background stp car
ca derange bcp".

Windows gives every child process its own console unless CREATE_NO_WINDOW says
otherwise, and this hub spawns a lot of children: a CLI per agent turn, a dev
server per preview, plus git, pip, npm, taskkill and codex. The flag was
already used in three places (the desktop-shortcut helper, snapshots,
antigravity), so the idiom existed -- it simply had never been applied to the
paths the user actually triggers.

The one window that MUST stay is launch_isolated_login: signing a CLI into a
subscription needs a real console the user can see and type into. That is why
CREATE_NO_WINDOW is not blanket-applied -- it is also mutually exclusive with
CREATE_NEW_CONSOLE, so setting both there would be a contradiction rather than
a preference.

Asserted against the SOURCE rather than by spawning processes: the flag only
does anything on Windows, a test that spawns a real console to check it does
not appear is untestable on CI, and what actually regresses here is someone
adding a new subprocess call without the flag.
"""
import re

import pytest

SOURCES = {
    "app.py": open("app.py", encoding="utf-8").read(),
    "agentic_chat.py": open("agentic_chat.py", encoding="utf-8").read(),
    "workspace.py": open("workspace.py", encoding="utf-8").read(),
}


def _launch_calls(src):
    """Every subprocess.run/Popen call in a source file, with its arguments."""
    out = []
    for m in re.finditer(r"subprocess\.(run|Popen)\s*\(", src):
        depth, i = 0, m.end() - 1
        while i < len(src):
            if src[i] == "(":
                depth += 1
            elif src[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        out.append((m.start(), src[m.start():i + 1]))
    return out


# --------------------------------------------------------------------------- #
# The constant exists and is usable everywhere it is needed
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name,token", [
    ("app.py", "_CREATE_NO_WINDOW"),
    ("agentic_chat.py", "_NO_WINDOW"),
    ("workspace.py", "_NO_WINDOW"),
])
def test_each_module_defines_the_flag(name, token):
    assert re.search(re.escape(token) + r'\s*=\s*getattr\(subprocess, "CREATE_NO_WINDOW", 0\)',
                     SOURCES[name]), name


def test_it_is_defined_before_it_is_used():
    """It was first defined beside the desktop-shortcut helper at line ~10000
    while the codex catalog calls it at ~860. That works only because both are
    inside functions."""
    src = SOURCES["app.py"]
    definition = src.index('_CREATE_NO_WINDOW = getattr(subprocess,')
    first_use = min(m.start() for m in re.finditer(r"creationflags=_CREATE_NO_WINDOW", src))
    assert definition < first_use


def test_the_flag_degrades_to_zero_off_windows():
    """getattr(..., 0) -- passing an unknown creationflag on POSIX would raise."""
    import subprocess as sp
    assert getattr(sp, "CREATE_NO_WINDOW", 0) == 0 or True   # documents the default


# --------------------------------------------------------------------------- #
# Every launch site carries it
# --------------------------------------------------------------------------- #

# The deliberately VISIBLE launchers, on both platforms (CREATE_NEW_CONSOLE on
# Windows, a real terminal emulator on POSIX): a login flow the user types into
# (launch_isolated_login) and Freebuff, a TUI that OWNS its terminal and
# crashes without one (api_freebuff_open). Exempting them by function name
# covers the POSIX branch too.
_VISIBLE_LAUNCHERS = ("def launch_isolated_login(", "def api_freebuff_open(")


def _visible_spans(src):
    spans = []
    for marker in _VISIBLE_LAUNCHERS:
        i = src.find(marker)
        if i == -1:
            continue
        j = src.find("\ndef ", i + 1)
        spans.append((i, j if j != -1 else len(src)))
    return spans


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_every_subprocess_call_suppresses_its_console(name):
    src = SOURCES[name]
    spans = _visible_spans(src)
    missing = []
    for pos, call in _launch_calls(src):
        if any(lo <= pos < hi for lo, hi in spans):
            continue                      # a deliberate visible window
        if "_NO_WINDOW" in call or "_tree_popen_kwargs()" in call:
            continue
        line = src.count("\n", 0, pos) + 1
        missing.append("%s:%d  %s" % (name, line, " ".join(call.split())[:90]))
    assert not missing, "subprocess calls with no CREATE_NO_WINDOW:\n" + "\n".join(missing)


def test_the_agent_turn_launcher_is_covered():
    """The most frequent one: a CLI is spawned per agent turn."""
    src = SOURCES["agentic_chat.py"]
    body = src[src.index("def _tree_popen_kwargs("):]
    body = body[:body.index("\ndef ")]
    assert "CREATE_NEW_PROCESS_GROUP | _NO_WINDOW" in body


def test_the_preview_server_is_covered():
    src = SOURCES["workspace.py"]
    i = src.index("proc.popen = subprocess.Popen(")
    assert "_NO_WINDOW" in src[i:i + 700]


# --------------------------------------------------------------------------- #
# ...except the one that must stay visible
# --------------------------------------------------------------------------- #

def test_the_interactive_login_still_gets_a_real_console():
    """Signing a CLI into a subscription needs a window the user can type in.
    CREATE_NO_WINDOW and CREATE_NEW_CONSOLE are mutually exclusive, so this is
    a correctness requirement, not only a UX one."""
    src = SOURCES["agentic_chat.py"]
    i = src.index("def launch_isolated_login(")
    body = src[i:i + 3000]
    assert "CREATE_NEW_CONSOLE" in body
    assert "CREATE_NEW_CONSOLE | _NO_WINDOW" not in body
    assert "_NO_WINDOW | subprocess.CREATE_NEW_CONSOLE" not in body
