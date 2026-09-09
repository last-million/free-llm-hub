r"""Never kill a server the hub cannot prove it started.

REPORTED 2026-09-09: "he should always check the ports where he wants to deploy
the app if used or no before, to not erase a running project".

TWO PLACES KILLED ON A GUESS.

1. shutdown() ends a session by killing whatever holds the project's adopted
   port. Adoption itself is deliberately FAIL-OPEN -- adopt()'s own comment says
   so: "the agent naming a url IS evidence, so silence (an unreadable cwd, a
   missing title) is not enough to override it; only positive contradiction is."

   That is the right rule for SHOWING a preview and the wrong one for KILLING.
   Both ownership checks go quiet in ordinary conditions: _port_owner_dir
   returns None whenever psutil cannot read the listener's cwd (routine on
   Windows for another user's or an elevated process), and _fingerprint returns
   None for any project with no <title> -- an API, an Express app. With both
   silent, a port is adopted on _http_ok alone.

   And the URL does not have to be a server the agent started. noticeUrlsIn
   scrapes ANY localhost URL out of the stream, including one merely mentioned
   in prose. So: agent writes "your dev server usually runs on
   http://localhost:3000", hub adopts :3000 on no evidence, session ends, the
   user's own dev server dies.

   Showing the wrong preview is a cosmetic mistake. Killing the wrong process
   destroys work. Adoption stays fail-open; the kill is now fail-CLOSED.

2. sweep_own_range() kills EVERY listener in 5800-5899 at hub start, with no
   check at all beyond "not the hub's own pid". The reasoning is written down
   and is mostly sound -- that range is handed out by this hub and adopt()
   refuses it -- but "mostly ours" is not "ours", and the cost of being wrong is
   somebody's running server. Previews now carry a marker in their environment,
   so a leaked one is recognised rather than assumed; a process that cannot be
   recognised is left alone, and _free_port already skips a busy port.
"""
import os

import pytest

import workspace as W


@pytest.fixture(autouse=True)
def _clean():
    W._adopted.clear()
    W._procs.clear()
    yield
    W._adopted.clear()
    W._procs.clear()


# --------------------------------------------------------------------------- #
# What "proven" means
# --------------------------------------------------------------------------- #

def test_the_operating_system_naming_our_folder_is_proof(tmp_path, monkeypatch):
    proj = str(tmp_path)
    monkeypatch.setattr(W, "_http_ok", lambda port, **k: True)
    monkeypatch.setattr(W, "_port_owner_dir", lambda port: proj)
    W.adopt(proj, "http://127.0.0.1:3000", source="agent")
    assert W._adopted[proj]["proven"] is True


def test_the_served_title_matching_is_proof(tmp_path, monkeypatch):
    proj = str(tmp_path)
    monkeypatch.setattr(W, "_http_ok", lambda port, **k: True)
    monkeypatch.setattr(W, "_port_owner_dir", lambda port: None)
    monkeypatch.setattr(W, "_fingerprint", lambda d: "My Shop")
    monkeypatch.setattr(W, "_served_title", lambda port, **k: "My Shop")
    W.adopt(proj, "http://127.0.0.1:3000", source="agent")
    assert W._adopted[proj]["proven"] is True


def test_silence_is_not_proof(tmp_path, monkeypatch):
    """The exact reported shape: unreadable cwd, no <title> to compare."""
    proj = str(tmp_path)
    monkeypatch.setattr(W, "_http_ok", lambda port, **k: True)
    monkeypatch.setattr(W, "_port_owner_dir", lambda port: None)
    monkeypatch.setattr(W, "_fingerprint", lambda d: None)
    W.adopt(proj, "http://127.0.0.1:3000", source="agent")
    assert W._adopted[proj]["proven"] is False


def test_it_is_still_adopted_though(tmp_path, monkeypatch):
    """Fail-open on DISPLAY is deliberate and stays. The preview still shows."""
    proj = str(tmp_path)
    monkeypatch.setattr(W, "_http_ok", lambda port, **k: True)
    monkeypatch.setattr(W, "_port_owner_dir", lambda port: None)
    monkeypatch.setattr(W, "_fingerprint", lambda d: None)
    W.adopt(proj, "http://127.0.0.1:3000", source="agent")
    assert W._adopted[proj]["port"] == 3000


# --------------------------------------------------------------------------- #
# ...and what it changes at session end
# --------------------------------------------------------------------------- #

def _adopt(monkeypatch, proj, proven, port=3000):
    monkeypatch.setattr(W, "_http_ok", lambda p, **k: True)
    monkeypatch.setattr(W, "_port_owner_dir", lambda p: proj if proven else None)
    monkeypatch.setattr(W, "_fingerprint", lambda d: None)
    W.adopt(proj, "http://127.0.0.1:%d" % port, source="agent")


def test_an_unproven_port_survives_the_session_ending(tmp_path, monkeypatch):
    """This is the user's dev server on :3000."""
    proj = str(tmp_path)
    _adopt(monkeypatch, proj, proven=False)
    killed = []
    monkeypatch.setattr(W, "_kill_listener", lambda port: killed.append(port) or True)
    W.shutdown(proj)
    assert killed == [], "killed a process it could not prove it started"


def test_a_proven_port_is_still_cleaned_up(tmp_path, monkeypatch):
    """The leak this was written for: a dev server outliving its session."""
    proj = str(tmp_path)
    _adopt(monkeypatch, proj, proven=True)
    killed = []
    monkeypatch.setattr(W, "_kill_listener", lambda port: killed.append(port) or True)
    W.shutdown(proj)
    assert killed == [3000]


def test_the_hubs_own_port_is_still_never_touched(tmp_path, monkeypatch):
    proj = str(tmp_path)
    monkeypatch.setattr(W, "_http_ok", lambda p, **k: True)
    monkeypatch.setattr(W, "_port_owner_dir", lambda p: proj)
    W._adopted[proj] = {"url": "x", "port": W._hub_port(), "since": 0,
                        "touched_at": 0, "source": "agent", "proven": True}
    killed = []
    monkeypatch.setattr(W, "_kill_listener", lambda port: killed.append(port) or True)
    W.shutdown(proj)
    assert killed == []


def test_an_old_record_without_the_field_is_not_killed(tmp_path, monkeypatch):
    """A record written before this shipped has no `proven` key. Defaulting it
    to True would reintroduce the bug for exactly one session per upgrade."""
    proj = str(tmp_path)
    W._adopted[proj] = {"url": "x", "port": 3000, "since": 0, "touched_at": 0,
                        "source": "agent"}
    killed = []
    monkeypatch.setattr(W, "_kill_listener", lambda port: killed.append(port) or True)
    W.shutdown(proj)
    assert killed == []


# --------------------------------------------------------------------------- #
# The boot sweep
# --------------------------------------------------------------------------- #

def test_previews_are_marked_so_they_can_be_recognised_later(tmp_path):
    """Without a marker, "is this leaked preview ours?" can only be guessed."""
    env = W._env_for(str(tmp_path), 5801)
    assert env.get(W._PREVIEW_MARKER) == os.path.abspath(str(tmp_path))


def test_the_sweep_leaves_a_process_it_cannot_recognise(monkeypatch):
    monkeypatch.setattr(W, "_port_open", lambda port: port == 5801)
    monkeypatch.setattr(W, "_is_hub_preview", lambda port: False)
    killed = []
    monkeypatch.setattr(W, "_kill_listener", lambda port: killed.append(port) or True)
    assert W.sweep_own_range() == 0
    assert killed == []


def test_the_sweep_still_reclaims_a_leaked_preview(monkeypatch):
    """99 of 100 ports were held by orphaned previews on this machine -- that
    problem is real and the sweep must still solve it."""
    monkeypatch.setattr(W, "_port_open", lambda port: port == 5801)
    monkeypatch.setattr(W, "_is_hub_preview", lambda port: True)
    killed = []
    monkeypatch.setattr(W, "_kill_listener", lambda port: killed.append(port) or True)
    assert W.sweep_own_range() == 1
    assert killed == [5801]


def test_recognition_never_raises(monkeypatch):
    """psutil raises AccessDenied routinely. An exception here must read as
    'not recognised', which means 'do not kill'."""
    class Boom:
        def __getattr__(self, name):
            raise RuntimeError("no psutil for you")
    monkeypatch.setitem(__import__("sys").modules, "psutil", Boom())
    assert W._is_hub_preview(5801) is False


def test_the_sweep_only_looks_at_its_own_range():
    assert W.PORT_RANGE == (5800, 5899)
    src = open("workspace.py", encoding="utf-8").read()
    body = src.split("def sweep_own_range(", 1)[1]
    body = body[:body.index("\ndef ")]
    assert "PORT_RANGE" in body
    assert "_is_hub_preview" in body
