"""Auto-update must not depend on branch upstream tracking.

MEASURED 2026-08-30: git-filter-repo removes the 'origin' remote by design, and
re-adding it does NOT restore the branch's upstream. A bare `git pull --ff-only`
then dies with

    There is no tracking information for the current branch.

and auto-update is silently off -- the hub keeps running, reports a pull
failure only on a diagnostics endpoint nobody reads, and never updates again.
Naming the remote and branch explicitly removes that whole failure mode: the
remote was already validated by _origin_is_trusted(), and the branch is simply
the one we are on.
"""
from unittest import mock

import app


def _run(git_impl):
    with mock.patch.object(app, "_origin_is_trusted", return_value=True), \
            mock.patch.object(app, "_git", side_effect=git_impl):
        app._auto_update_state.pop("last_result", None)
        return app._do_git_update_check()


def test_the_pull_names_its_remote_and_branch():
    seen = []

    def fake_git(*args):
        seen.append(args)
        if args[0] == "status":
            return 0, "", ""
        if args[0] == "rev-parse" and "--abbrev-ref" in args:
            return 0, "main\n", ""
        if args[0] == "rev-parse":
            return 0, "abc123", ""
        return 0, "Already up to date.", ""

    _run(fake_git)
    pulls = [a for a in seen if a and a[0] == "pull"]
    assert pulls, "no pull was attempted"
    assert pulls[0] == ("pull", "--ff-only", "origin", "main"), pulls[0]


def test_it_follows_whatever_branch_is_checked_out():
    def fake_git(*args):
        if args[0] == "status":
            return 0, "", ""
        if args[0] == "rev-parse" and "--abbrev-ref" in args:
            return 0, "release\n", ""
        if args[0] == "rev-parse":
            return 0, "abc123", ""
        return 0, "Already up to date.", ""

    seen = []
    with mock.patch.object(app, "_origin_is_trusted", return_value=True), \
            mock.patch.object(app, "_git",
                              side_effect=lambda *a: (seen.append(a), fake_git(*a))[1]):
        app._do_git_update_check()
    pulls = [a for a in seen if a and a[0] == "pull"]
    assert pulls[0][-1] == "release", pulls[0]


def test_an_unreadable_branch_name_falls_back_to_main():
    """Detached HEAD or a failed rev-parse must not produce `git pull origin ''`."""
    def fake_git(*args):
        if args[0] == "status":
            return 0, "", ""
        if args[0] == "rev-parse" and "--abbrev-ref" in args:
            return 1, "", "fatal"
        if args[0] == "rev-parse":
            return 0, "abc123", ""
        return 0, "Already up to date.", ""

    seen = []
    with mock.patch.object(app, "_origin_is_trusted", return_value=True), \
            mock.patch.object(app, "_git",
                              side_effect=lambda *a: (seen.append(a), fake_git(*a))[1]):
        app._do_git_update_check()
    pulls = [a for a in seen if a and a[0] == "pull"]
    assert pulls[0] == ("pull", "--ff-only", "origin", "main"), pulls[0]
