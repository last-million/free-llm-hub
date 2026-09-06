"""One hub per config directory -- but never at the cost of refusing to start.

TWO COPIES SHARING ~/.free-llm-hub is how every API key on this install was lost:
both processes read and write config.json and secret.key, one could not read the
key for a moment, and the config was then saved without the secrets it could not
decrypt. Three live app.py processes were found at once -- a scheduled task
starting one at logon, racing others started by hand. The launchers already
refuse a second start; nothing stopped `python app.py` directly.

THE FAILURE MODE TO AVOID IS THE GUARD ITSELF. Refusing to start locks the user
out of their own hub, which is worse than the race it prevents. The first draft
was refuted on exactly that: `except OSError: return False` treated "this
filesystem cannot lock" (NFS without lockd, some SMB and FUSE mounts, which
report ENOLCK / ENOTSUP / EINVAL) as "another instance holds it", and would have
refused forever. That is the same mistake that cost the keys -- inability to
verify read as proof -- moved to the start path.

So: only real contention refuses. Everything else starts and says why.
"""
import errno
import os
from unittest import mock

import pytest

import app as A


@pytest.fixture(autouse=True)
def no_force(monkeypatch):
    monkeypatch.delenv("HUB_FORCE", raising=False)
    A._INSTANCE_LOCK.clear()
    yield
    A._INSTANCE_LOCK.clear()


def _with_lock_error(exc):
    """Make whichever locking primitive this platform uses raise `exc`."""
    try:
        import msvcrt
        return mock.patch("msvcrt.locking", side_effect=exc)
    except ImportError:
        import fcntl
        return mock.patch("fcntl.flock", side_effect=exc)


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #

def test_a_first_instance_starts(tmp_path, monkeypatch):
    monkeypatch.setattr(A.config, "_config_path",
                        lambda: str(tmp_path / "config.json"))
    assert A._claim_single_instance()


def test_the_lock_is_held_for_the_life_of_the_process(tmp_path, monkeypatch):
    """Released by the kernel when the process dies, so it cannot go stale after
    a crash or a taskkill -- which is why this is a byte-range lock and not a
    pidfile."""
    monkeypatch.setattr(A.config, "_config_path",
                        lambda: str(tmp_path / "config.json"))
    A._claim_single_instance()
    assert A._INSTANCE_LOCK, "the handle must be kept, or the lock is dropped"


def test_it_does_not_reuse_the_config_lock(tmp_path, monkeypatch):
    """config.py takes a BLOCKING cross-process lock on config.json.lock for
    every write. Holding that here would deadlock the hub against its own first
    save."""
    monkeypatch.setattr(A.config, "_config_path",
                        lambda: str(tmp_path / "config.json"))
    A._claim_single_instance()
    assert (tmp_path / "instance.lock").exists()
    assert not (tmp_path / "config.json.lock").exists()


# --------------------------------------------------------------------------- #
# Real contention, and only that, refuses
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("err", [errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK,
                                 errno.EDEADLK, 36])
def test_a_held_lock_refuses(tmp_path, monkeypatch, err):
    monkeypatch.setattr(A.config, "_config_path",
                        lambda: str(tmp_path / "config.json"))
    with _with_lock_error(OSError(err, "held")):
        assert A._claim_single_instance() is False


@pytest.mark.parametrize("err", [errno.ENOLCK, errno.ENOTSUP, errno.EINVAL,
                                 errno.EBADF, errno.EPERM])
def test_a_filesystem_that_cannot_lock_still_starts(tmp_path, monkeypatch, err):
    """The refuted draft turned every one of these into a permanent refusal."""
    monkeypatch.setattr(A.config, "_config_path",
                        lambda: str(tmp_path / "config.json"))
    with _with_lock_error(OSError(err, "unsupported")):
        assert A._claim_single_instance() is True


def test_an_unopenable_lock_file_still_starts(monkeypatch):
    monkeypatch.setattr(A.config, "_config_path",
                        lambda: "/nonexistent-root/deep/config.json")
    with mock.patch("builtins.open", side_effect=OSError(errno.EROFS, "read-only")):
        assert A._claim_single_instance() is True


def test_an_unexpected_error_still_starts(tmp_path, monkeypatch):
    monkeypatch.setattr(A.config, "_config_path",
                        lambda: str(tmp_path / "config.json"))
    with _with_lock_error(RuntimeError("something else entirely")):
        assert A._claim_single_instance() is True


# --------------------------------------------------------------------------- #
# The escape hatch both launchers already document
# --------------------------------------------------------------------------- #

def test_hub_force_overrides(tmp_path, monkeypatch):
    monkeypatch.setattr(A.config, "_config_path",
                        lambda: str(tmp_path / "config.json"))
    monkeypatch.setenv("HUB_FORCE", "1")
    with _with_lock_error(OSError(errno.EACCES, "held")):
        assert A._claim_single_instance() is True


def test_hub_force_is_the_documented_name():
    """run.bat and run.sh both tell the user to set HUB_FORCE=1. A guard with a
    different escape hatch would be the only unoverridable refusal in the start
    path."""
    for f in ("run.bat", "run.sh"):
        if os.path.exists(f):
            assert "HUB_FORCE" in open(f, encoding="utf-8", errors="replace").read()


# --------------------------------------------------------------------------- #
# Where it runs
# --------------------------------------------------------------------------- #

def test_it_is_claimed_immediately_before_the_bind():
    """Late, so the window in which the lock is held but nothing is listening is
    as small as possible -- the startup sweep before it can take tens of
    seconds."""
    src = open("app.py", encoding="utf-8").read()
    claim = src.index("if not _claim_single_instance():")
    bind = src.index("server = make_server(HOST, PORT, app, threaded=True)")
    assert claim < bind
    assert bind - claim < 400, "the claim drifted away from the bind"


def test_it_only_runs_under_main():
    """Importing app.py -- which every test does -- must never take the lock."""
    src = open("app.py", encoding="utf-8").read()
    main = src.index('if __name__ == "__main__":')
    assert src.index("if not _claim_single_instance():") > main
