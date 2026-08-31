"""Calvoun Free LLM Hub -- per-turn FILE snapshots for an agent session.

WHY THIS EXISTS. agentic_history.py's checkpoints are transcript bookmarks and
its module docstring says so in capitals: they record an index into the turn
list and "do NOT snapshot the project folder's files on disk in any way ...
Any UI built on top of this must not imply otherwise." That was honest, and it
was also the whole limitation -- asked for directly 2026-08-31: "in /agent that
he be able to have checkpoints buttons for previous message to restaure
conversation and code and rerun it".

Restoring the conversation without restoring the CODE is worse than useless: the
transcript would claim a state the files no longer have.

HOW: a SHADOW git repository, one per session, living in the hub's own state
directory and never inside the user's project:

    git --git-dir=~/.free-llm-hub/snapshots/<session>.git --work-tree=<project>

Chosen over copying the folder because git stores only what changed between
turns, so a 40-turn session costs about one copy of the project rather than
forty; and over `git init` inside the project because that would collide with
the user's OWN repository, rewrite their index, and put our commits in their
history. With a separate --git-dir the project directory is left exactly as the
agent left it -- no .git, no config, no ignore file, nothing.

MEASURED before building on it: a snapshot taken, then index.html edited,
sub/a.txt deleted and extra.js created; after restore index.html was back,
sub/a.txt was back, extra.js was gone, and the project still contained no .git
of its own.

SAFETY
  - `git clean -fd`, deliberately NOT -ff, PLUS an explicit exclude for every
    directory holding a nested repository. -fd alone was not enough and this
    module's own test proved it: git protects an untracked directory that IS a
    repo, but only at its top level, so a checkout at libs/theirs sat inside
    libs/, which is not a repo, and plain -fd removed the lot.
  - Heavy build output is never snapshotted (see _EXCLUDES) -- node_modules is
    not source, it is the single fastest way to make this unusably slow, and it
    is reproducible from the manifest that IS snapshotted.
  - A project too big to snapshot quickly is refused rather than hanging the
    turn (MAX_FILES).
  - Every operation is scoped to one session's own project_dir, and every one
    of them fails open: a snapshot that cannot be taken must never take the
    agent turn down with it.

Pure stdlib plus the `git` binary, which is checked for once and cached.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time

_LOCK = threading.RLock()

# Never snapshotted: reproducible build output and other tools' state. Anything
# here is either derivable from a file that IS snapshotted, or not source.
_EXCLUDES = (
    "node_modules/", ".venv/", "venv/", "__pycache__/", ".next/", ".nuxt/",
    "dist/", "build/", ".cache/", ".parcel-cache/", "target/", "vendor/",
    ".pytest_cache/", ".mypy_cache/", ".turbo/", "*.pyc", "*.log",
    ".DS_Store", "Thumbs.db",
)

# A project with more files than this is refused rather than snapshotted -- the
# point is a snapshot that costs a second, not one that stalls the turn.
MAX_FILES = 20000
# Any single git call that outruns this is abandoned; a slow snapshot must
# never hold up the agent.
TIMEOUT = 90

_git_ok = None


def _root():
    env = os.environ.get("FREE_LLM_HUB_CONFIG")
    base = os.path.dirname(os.path.abspath(os.path.expanduser(env))) if env else os.path.join(
        os.path.expanduser("~"), ".free-llm-hub")
    return os.path.join(base, "snapshots")


def _safe_name(session_id):
    keep = "-_"
    return "".join(c for c in str(session_id or "") if c.isalnum() or c in keep)[:64] or "unknown"


def _git_dir(session_id):
    return os.path.join(_root(), _safe_name(session_id) + ".git")


def git_available():
    """True when a usable `git` is on PATH. Checked once; the answer does not
    change inside one hub process."""
    global _git_ok
    if _git_ok is None:
        _git_ok = bool(shutil.which("git"))
    return _git_ok


def _run(args, cwd=None, timeout=TIMEOUT):
    """Run git, returning (ok, stdout). Never raises."""
    try:
        p = subprocess.run(["git"] + args, cwd=cwd, capture_output=True,
                           text=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return p.returncode == 0, (p.stdout or "").strip()
    except Exception:                                            # noqa: BLE001
        return False, ""


def _base_args(session_id, project_dir):
    return ["--git-dir=" + _git_dir(session_id), "--work-tree=" + project_dir,
            "-c", "user.email=hub@free-llm-hub.local", "-c", "user.name=Calvoun Hub",
            "-c", "core.autocrlf=false", "-c", "core.safecrlf=false"]


def _too_big(project_dir):
    """Walk far enough to know it is too big, then stop. Excluded directories
    are not descended into, so a node_modules tree costs nothing to skip."""
    skip = {e.rstrip("/") for e in _EXCLUDES if e.endswith("/")}
    n = 0
    for dirpath, dirnames, filenames in os.walk(project_dir):
        dirnames[:] = [d for d in dirnames if d not in skip and d != ".git"]
        n += len(filenames)
        if n > MAX_FILES:
            return True
    return False


def _nested_repo_excludes(project_dir):
    """`git clean -e` patterns for every directory holding someone else's git
    repository, so a restore cannot delete one.

    FOUND BY THIS MODULE'S OWN TEST. `git clean -fd` protects an untracked
    directory that IS a repository, but only at its own top level -- a repo at
    libs/theirs sits inside libs/, which is not a repo, so plain -fd removed
    libs/ and everything under it. Excluding the containing path is what
    actually keeps a user's nested checkout alive."""
    out = []
    root = os.path.abspath(project_dir)
    skip = {e.rstrip("/") for e in _EXCLUDES if e.endswith("/")}
    for dirpath, dirnames, _files in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        if ".git" in dirnames and os.path.abspath(dirpath) != root:
            rel = os.path.relpath(dirpath, root).replace(os.sep, "/")
            out.append("/" + rel + "/")
            dirnames[:] = []                 # do not walk into their repo
    return out


def _ensure_repo(session_id, project_dir):
    gd = _git_dir(session_id)
    if os.path.isdir(gd):
        return True
    try:
        os.makedirs(_root(), exist_ok=True)
    except OSError:
        return False
    ok, _ = _run(["init", "--bare", "--quiet", gd])
    if not ok:
        return False
    # The exclude list lives in the SHADOW repo, so the project never gets a
    # .gitignore it did not ask for.
    try:
        info = os.path.join(gd, "info")
        os.makedirs(info, exist_ok=True)
        with open(os.path.join(info, "exclude"), "w", encoding="utf-8") as f:
            f.write("\n".join(_EXCLUDES) + "\n")
    except OSError:
        pass
    return True


def take(session_id, project_dir, label=""):
    """Snapshot `project_dir` as it is right now. Returns a commit id, or None.

    Called BEFORE a turn runs, so restoring a snapshot returns the files to the
    state they were in before that turn touched anything. Fails open: any
    problem returns None and the turn proceeds unsnapshotted rather than not at
    all."""
    if not session_id or not project_dir or not os.path.isdir(project_dir):
        return None
    if not git_available():
        return None
    try:
        with _LOCK:
            if _too_big(project_dir):
                return None
            if not _ensure_repo(session_id, project_dir):
                return None
            base = _base_args(session_id, project_dir)
            ok, _ = _run(base + ["add", "-A", "."], cwd=project_dir)
            if not ok:
                return None
            msg = label or ("turn @ " + time.strftime("%Y-%m-%d %H:%M:%S"))
            # --allow-empty: a turn that changed nothing still deserves a point
            # to come back to, or the indexes stop lining up with the turns.
            ok, _ = _run(base + ["commit", "--allow-empty", "--quiet", "-m", msg],
                         cwd=project_dir)
            if not ok:
                return None
            ok, out = _run(base + ["rev-parse", "HEAD"], cwd=project_dir)
            return out if ok and out else None
    except Exception:                                            # noqa: BLE001
        return None


def restore(session_id, project_dir, commit):
    """Put `project_dir` back to `commit`. Returns True on success.

    DESTRUCTIVE, and the caller is expected to have confirmed with a human:
    files created after this commit are removed and edits since are discarded.
    `git clean -fd` (never -ff) leaves a nested git repository alone, so a
    checkout the user made inside their project is not destroyed."""
    if not (session_id and project_dir and commit):
        return False
    if not os.path.isdir(project_dir) or not git_available():
        return False
    if not os.path.isdir(_git_dir(session_id)):
        return False
    try:
        with _LOCK:
            base = _base_args(session_id, project_dir)
            ok, _ = _run(base + ["cat-file", "-e", commit + "^{commit}"], cwd=project_dir)
            if not ok:
                return False                     # unknown commit: change nothing
            ok, _ = _run(base + ["checkout", "--force", commit, "--", "."],
                         cwd=project_dir)
            if not ok:
                return False
            # Anything created after the snapshot. -fd, never -ff, plus an
            # explicit exclude for every nested repository (see
            # _nested_repo_excludes -- -fd alone is not enough).
            clean = ["clean", "-fdq"]
            for pat in _nested_repo_excludes(project_dir):
                clean += ["-e", pat]
            _run(base + clean, cwd=project_dir)
            # Leave HEAD where the history is, so later snapshots keep stacking
            # onto the same line rather than starting a detached one.
            _run(base + ["reset", "--soft", commit], cwd=project_dir)
            return True
    except Exception:                                            # noqa: BLE001
        return False


def exists(session_id, commit):
    """True when this session's shadow repo holds that commit."""
    if not (session_id and commit) or not git_available():
        return False
    if not os.path.isdir(_git_dir(session_id)):
        return False
    ok, _ = _run(["--git-dir=" + _git_dir(session_id), "cat-file", "-e",
                  commit + "^{commit}"])
    return ok


def discard(session_id):
    """Delete a session's snapshots. Used when its conversation is pruned --
    otherwise the shadow repos outlive the history they belong to."""
    gd = _git_dir(session_id)
    if not os.path.isdir(gd):
        return False
    def _force(func, path, _exc):
        """git makes its pack files read-only, and on Windows read-only means
        undeletable -- rmtree just fails. Clear the bit and retry."""
        try:
            os.chmod(path, 0o700)
            func(path)
        except Exception:                                        # noqa: BLE001
            pass

    try:
        try:                                  # py3.12+ renamed the hook
            shutil.rmtree(gd, onexc=_force)
        except TypeError:
            shutil.rmtree(gd, onerror=_force)
        return not os.path.isdir(gd)
    except Exception:                                            # noqa: BLE001
        return False


def size_bytes(session_id):
    """How much disk one session's snapshots take, for the UI to be honest
    about. 0 when there are none."""
    gd = _git_dir(session_id)
    total = 0
    for dirpath, _dirs, files in os.walk(gd):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, fn))
            except OSError:
                pass
    return total
