"""Auto-update must not be disabled by an untracked file.

Found live 2026-08-30: `git status --porcelain` counts UNTRACKED files, so a
single stray note in the working tree ('.calvoun-brief.md') had been parking
every 5-hourly check at "skipped: local uncommitted changes" -- indefinitely,
and with nothing in the UI saying the hub had stopped updating itself.

Only a modification to a TRACKED file can make `pull --ff-only` clobber real
work, so only that should block. The narrow case this gives up (an incoming
commit adding a path that already exists untracked) still fails safely: git
refuses the merge and the pull-failure branch reports it.
"""
from unittest import mock

import app


def _run_check(git_impl):
    with mock.patch.object(app, "_origin_is_trusted", return_value=True), \
            mock.patch.object(app, "_git", side_effect=git_impl):
        app._auto_update_state.pop("last_result", None)
        return app._do_git_update_check()


def test_dirtiness_is_checked_without_untracked_files():
    """The mechanism: the status call must pass --untracked-files=no."""
    seen = []

    def fake_git(*args):
        seen.append(args)
        if args[0] == "status":
            return 0, "", ""
        if args[0] == "rev-parse":
            return 0, "abc123", ""
        return 0, "Already up to date.", ""

    _run_check(fake_git)
    status_calls = [a for a in seen if a and a[0] == "status"]
    assert status_calls, "no git status call was made"
    assert "--untracked-files=no" in status_calls[0], status_calls[0]


def test_an_untracked_file_no_longer_blocks_the_update():
    """With --untracked-files=no, git reports a clean tree, so the pull runs."""
    pulled = []

    def fake_git(*args):
        if args[0] == "status":
            return 0, "", ""          # untracked files excluded -> clean
        if args[0] == "rev-parse":
            return 0, "abc123", ""
        if args[0] == "pull":
            pulled.append(args)
            return 0, "Already up to date.", ""
        return 0, "", ""

    result = _run_check(fake_git)
    assert pulled, "pull was never attempted: " + str(result)
    assert "uncommitted" not in result, result


def test_a_real_modified_tracked_file_still_blocks():
    """The guard must still do its actual job -- never clobber real edits."""
    pulled = []

    def fake_git(*args):
        if args[0] == "status":
            return 0, " M app.py\n", ""    # a tracked file really is modified
        if args[0] == "pull":
            pulled.append(args)
            return 0, "", ""
        return 0, "abc123", ""

    result = _run_check(fake_git)
    assert result == "skipped: local uncommitted changes", result
    assert not pulled, "a dirty tracked tree must never be pulled over"
