"""Receipts: what the hub OBSERVED a test or build command do, on disk.

One JSON file per Multi phase and per /agent turn that ran a test or build
command, under `state_dir()/receipts/<run or session>/<n>.json`:

    command / argv, cwd, git HEAD (when the folder is a repository), the tool
    and its version when the output named it, started/ended, exit code, the
    tool's counts, the verdict (evidence.classify), source "observed", and
    the SHA-256 of the files the phase/turn CHANGED (capped: 40 files, 5 MB).

Why on disk and not just in memory: "verified" is a claim until something can
be checked after the fact. A receipt says which command, at which commit, over
which file contents, returned what -- written by the json library, never
hand-assembled. LRU-pruned to MAX_RECEIPTS.

The hub runs nothing in the project to make one (owner rule: observation
only). The git HEAD is read from the .git files; only when that fails is the
existing snapshots git helper asked, with a short timeout.
"""
import hashlib
import json
import os
import re
import tempfile
import threading
import time

import evidence

MAX_RECEIPTS = 500
HASH_MAX_FILES = 40
HASH_MAX_BYTES = 5 * 1024 * 1024
GIT_TIMEOUT = 5
SCHEMA = 1

_LOCK = threading.Lock()


def root():
    """`state_dir()/receipts` -- the hub's own state folder, never a project."""
    try:
        import config
        base = config.state_dir()
    except Exception:                                            # noqa: BLE001
        base = os.path.join(os.path.expanduser("~"), ".free-llm-hub")
    return os.path.join(base, "receipts")


def _safe(name):
    return re.sub(r"[^A-Za-z0-9_-]", "", str(name or ""))[:64] or "unknown"


# --------------------------------------------------------------------------- #
# git HEAD, without running anything when the files say it
# --------------------------------------------------------------------------- #

def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _git_dirs(start):
    """(gitdir, commondir) for the repository holding `start`, or (None, None)."""
    d = os.path.abspath(start)
    for _ in range(40):
        dot = os.path.join(d, ".git")
        if os.path.isdir(dot):
            return dot, dot
        if os.path.isfile(dot):                      # a worktree / submodule
            txt = _read(dot) or ""
            if txt.startswith("gitdir:"):
                gd = txt.split(":", 1)[1].strip()
                gd = gd if os.path.isabs(gd) else os.path.normpath(os.path.join(d, gd))
                common = _read(os.path.join(gd, "commondir"))
                if common:
                    common = common if os.path.isabs(common) else \
                        os.path.normpath(os.path.join(gd, common))
                return gd, common or gd
            return None, None
        parent = os.path.dirname(d)
        if parent == d:
            return None, None
        d = parent
    return None, None


def _ref(gitdir, common, ref):
    for base in (gitdir, common):
        if not base:
            continue
        sha = _read(os.path.join(base, *ref.split("/")))
        if sha and re.fullmatch(r"[0-9a-f]{40,64}", sha):
            return sha
        packed = _read(os.path.join(base, "packed-refs")) or ""
        for line in packed.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref and re.fullmatch(r"[0-9a-f]{40,64}", parts[0]):
                return parts[0]
    return None


def git_head(cwd):
    """The commit the folder is at, or None when it is no repository (or
    nothing could tell). Never raises."""
    try:
        if not cwd or not os.path.isdir(cwd):
            return None
        gitdir, common = _git_dirs(cwd)
        if not gitdir:
            return None
        head = _read(os.path.join(gitdir, "HEAD")) or ""
        if re.fullmatch(r"[0-9a-f]{40,64}", head):
            return head
        if head.startswith("ref:"):
            sha = _ref(gitdir, common, head.split(":", 1)[1].strip())
            if sha:
                return sha
        import snapshots                                  # the existing helper
        if snapshots.git_available():
            ok, out = snapshots._run(["rev-parse", "HEAD"], cwd=cwd, timeout=GIT_TIMEOUT)
            if ok and re.fullmatch(r"[0-9a-f]{40,64}", out or ""):
                return out
    except Exception:                                            # noqa: BLE001
        pass
    return None


# --------------------------------------------------------------------------- #
# What changed, by content
# --------------------------------------------------------------------------- #

def hash_files(cwd, rel_paths, max_files=None, max_bytes=None):
    """[{path, sha256, bytes}] for the changed files, capped (HASH_MAX_FILES
    files, HASH_MAX_BYTES hashed in all). A file past the byte budget (or
    gone) is listed with sha256 None and why."""
    max_files = HASH_MAX_FILES if max_files is None else max_files
    max_bytes = HASH_MAX_BYTES if max_bytes is None else max_bytes
    rows, budget = [], max_bytes
    for rel in list(rel_paths or ())[:max_files]:
        path = rel if os.path.isabs(rel) else os.path.join(cwd or "", rel)
        row = {"path": str(rel).replace("\\", "/"), "sha256": None, "bytes": None}
        try:
            size = os.path.getsize(path)
            row["bytes"] = size
            if size > budget:
                row["skipped"] = "over the %s hashing budget" % (
                    "%d MB" % (max_bytes // (1024 * 1024)) if max_bytes >= 1024 * 1024
                    else "%d-byte" % max_bytes)
            else:
                h = hashlib.sha256()
                with open(path, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 16), b""):
                        h.update(chunk)
                row["sha256"] = h.hexdigest()
                budget -= size
        except OSError:
            row["skipped"] = "deleted or unreadable"
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #

def overall(results):
    """One verdict for a list of classified results: an outstanding FAIL wins,
    then a PASS, then "no tests"; else UNDETERMINED."""
    if evidence.outstanding_failures(results):
        return evidence.FAIL
    if evidence.passes(results):
        return evidence.PASS
    vs = [r.get("verdict") for r in results or () if isinstance(r, dict)]
    if vs and all(v == evidence.NO_TESTS for v in vs):
        return evidence.NO_TESTS
    return evidence.UNDETERMINED


def _row(r):
    cmd = str(r.get("command") or "")
    inner = evidence.inner_command(cmd)
    try:
        argv = evidence.argv(inner)
    except Exception:                                            # noqa: BLE001
        argv = [inner]
    return {"command": cmd[:2000], "argv": argv[:200],
            "tool": r.get("tool"), "version": r.get("version"),
            "exit_code": r.get("exit_code"), "is_error": r.get("is_error"),
            "passed": r.get("passed", 0), "failed": r.get("failed", 0),
            "skipped": r.get("skipped", 0), "verdict": r.get("verdict"),
            "line": r.get("line") or "", "started_at": r.get("started_at"),
            "ended_at": r.get("ended_at"), "source": "observed"}


def _next_number(folder):
    nums = [int(m.group(1)) for m in (re.match(r"^(\d+)\.json$", n) for n in os.listdir(folder)) if m]
    return (max(nums) + 1) if nums else 1


def write(scope, kind, results, cwd=None, changed=(), started_at=None, ended_at=None,
          number=None, extra=None):
    """Write one receipt; its path, or None when there is nothing to write or
    the write failed. Never raises."""
    results = [r for r in (results or ()) if isinstance(r, dict)]
    if not results:
        return None
    try:
        changed = list(changed or ())
        rec = {
            "schema": SCHEMA, "kind": kind, "scope": str(scope or ""),
            "source": "observed",
            "cwd": cwd, "git_head": git_head(cwd),
            "started_at": started_at, "ended_at": ended_at or time.time(),
            "verdict": overall(results),
            "results": [_row(r) for r in results],
            "changed_files": hash_files(cwd, changed),
            "changed_files_total": len(changed),
            "changed_files_capped": len(changed) > HASH_MAX_FILES,
            "hash_limits": {"files": HASH_MAX_FILES, "bytes": HASH_MAX_BYTES},
        }
        if isinstance(extra, dict):
            for k, v in extra.items():
                rec.setdefault(k, v)
        with _LOCK:
            folder = os.path.join(root(), _safe(scope))
            os.makedirs(folder, exist_ok=True)
            n = int(number) if number else _next_number(folder)
            path = os.path.join(folder, "%d.json" % n)
            fd, tmp = tempfile.mkstemp(prefix=".receipt-", suffix=".tmp", dir=folder)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(rec, fh, indent=1, ensure_ascii=False)
                os.replace(tmp, path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            prune()
        return path
    except Exception:                                            # noqa: BLE001
        return None


def prune(keep=None):
    """Keep the `keep` (default MAX_RECEIPTS) most recently written receipts;
    drop the rest (and the folders they leave empty). Returns how many were
    removed."""
    keep = MAX_RECEIPTS if keep is None else int(keep)
    base = root()
    files = []
    try:
        for scope in os.listdir(base):
            folder = os.path.join(base, scope)
            if not os.path.isdir(folder):
                continue
            for name in os.listdir(folder):
                if name.endswith(".json"):
                    p = os.path.join(folder, name)
                    try:
                        files.append((os.path.getmtime(p), p))
                    except OSError:
                        pass
    except OSError:
        return 0
    if len(files) <= keep:
        return 0
    files.sort()
    removed = 0
    for _mtime, p in files[: len(files) - keep]:
        try:
            os.unlink(p)
            removed += 1
        except OSError:
            pass
        folder = os.path.dirname(p)
        try:
            if not os.listdir(folder):
                os.rmdir(folder)
        except OSError:
            pass
    return removed


def read(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def short_path(path):
    """`receipts/<scope>/<n>.json` -- how a memory fact names a receipt."""
    try:
        rel = os.path.relpath(path, os.path.dirname(root()))
        return rel.replace("\\", "/")
    except ValueError:
        return str(path)
