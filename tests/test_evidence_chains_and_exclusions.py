"""Observed evidence: a check chained after another, and a run that left the
failing tests out, never clear an earlier failure (2026-10-30).

1. `pytest a; pytest b` (or `||`, or a newline) ends with b's exit code only.
   The old reading looked for masking only AFTER THE LAST check, so a's
   failure vanished behind b's pass: PASS, and the earlier FAIL of a counted
   as fixed. Now a ZERO exit after any operator other than `&&` from the
   first check on -> UNDETERMINED; a non-zero one is still the last check's
   own failure and stays an observed FAIL (keyed on every check of the line).
2. A command that runs several checks is credited as a whole: its FAIL is
   covered only when every one of its checks was re-run green, by one later
   PASS or by separate later PASSes (the old code keyed it on its LAST check
   and scoped it on its FIRST).
3. `--ignore` / `--deselect` / `--exclude` / `--shard` / forge's match flags
   mark a run filtered, and their value is not read as a path, so a run that
   skipped the failing tests does not clear them.

Pure: evidence.py only, no I/O.
"""
import pytest

import evidence as E

A_FAIL = "============ 1 failed, 4 passed in 0.20s ============\n"
B_PASS = "============ 6 passed in 0.31s ============\n"
ALL_PASS = "============ 12 passed in 0.50s ============\n"


def _row(command, code, out):
    return dict(E.classify(command, code, out), command=command)


def _fail_a():
    return _row("pytest tests/test_a.py", 1, A_FAIL)


# --------------------------------------------------------------------------- #
# 1. A check followed by `;` / `||` / a newline and another check
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("sep", ["; ", " || ", "\n", " | ", " & "])
def test_a_second_check_after_a_non_and_operator_is_undetermined(sep):
    cmd = "pytest tests/test_a.py%spytest tests/test_b.py" % sep
    r = E.classify(cmd, 0, A_FAIL + B_PASS)
    assert r["verdict"] == E.UNDETERMINED, (sep, r)


B_FAIL = "============ 2 failed, 3 passed in 0.25s ============\n"
A_PASS = "============ 5 passed in 0.20s ============\n"


@pytest.mark.parametrize("sep", ["; ", " || ", "\n", " | ", " & "])
def test_a_nonzero_exit_after_a_non_and_operator_is_still_an_observed_failure(sep):
    # The exit code is the LAST check's own: non-zero = b really failed. The
    # line is keyed on all its checks, so the FAIL is safe to keep.
    cmd = "pytest tests/test_a.py%spytest tests/test_b.py" % sep
    r = E.classify(cmd, 1, A_PASS + B_FAIL)
    assert r["verdict"] == E.FAIL, (sep, r)
    assert r["failed"] >= 2
    row = dict(r, command=cmd)
    assert E.outstanding_failures([row]) == [row]
    assert E.label([row]).endswith("failed (observed)")


def test_a_nonzero_exit_masked_after_the_last_check_stays_undetermined():
    for cmd in ("pytest tests/a; pytest tests/b; echo done",
                "pytest tests/a; pytest tests/b | tail -5",
                "pytest tests/a || pytest tests/b || true"):
        assert E.classify(cmd, 1, A_PASS + B_FAIL)["verdict"] == E.UNDETERMINED, cmd


@pytest.mark.parametrize("sep", ["; ", " || ", "\n"])
def test_the_failure_of_the_first_check_stays_outstanding(sep):
    cmd = "pytest tests/test_a.py%spytest tests/test_b.py" % sep
    fail_a = _fail_a()
    seq = _row(cmd, 0, A_FAIL + B_PASS)
    assert seq["verdict"] != E.PASS
    assert E.outstanding_failures([fail_a, seq]) == [fail_a]
    assert E.label([fail_a, seq]) == "1 failed (observed)"
    assert E.passes([fail_a, seq]) == []


def test_the_same_through_a_codex_shell_wrapper():
    cmd = "/bin/bash -lc 'pytest tests/test_a.py; pytest tests/test_b.py'"
    assert E.classify(cmd, 0, A_FAIL + B_PASS)["verdict"] == E.UNDETERMINED


@pytest.mark.parametrize("command, code, out, verdict", [
    ("cd app && pytest -q && echo ok", 0, ALL_PASS, E.PASS),
    ("cd app; pytest -q", 0, ALL_PASS, E.PASS),                 # `;` BEFORE the check
    ("echo start\npytest -q", 0, ALL_PASS, E.PASS),
    ("pytest tests/a && pytest tests/b", 0, B_PASS + ALL_PASS, E.PASS),
    ("pytest tests/a && pytest tests/b", 1, A_FAIL, E.FAIL),
    ("npx tsc --noEmit && npx vite build", 0, "✓ built in 1.2s\n", E.PASS),
    ("pytest -q; echo done", 0, ALL_PASS, E.UNDETERMINED),      # unchanged
    ("pytest -q | tail -5", 0, ALL_PASS, E.UNDETERMINED),       # unchanged
])
def test_and_chains_and_single_checks_read_as_before(command, code, out, verdict):
    assert E.classify(command, code, out)["verdict"] == verdict


# --------------------------------------------------------------------------- #
# 2. A multi-check command is credited as a whole
# --------------------------------------------------------------------------- #

def test_a_failed_and_chain_is_not_cleared_by_rerunning_only_its_last_check():
    chain = "pytest tests/test_a.py && pytest tests/test_b.py"
    fail = _row(chain, 1, A_FAIL)                 # a failed; b never ran
    assert fail["verdict"] == E.FAIL
    only_b = _row("pytest tests/test_b.py", 0, B_PASS)
    assert not E.covers("pytest tests/test_b.py", chain)
    assert E.outstanding_failures([fail, only_b]) == [fail]


def test_a_failed_and_chain_is_not_cleared_by_rerunning_only_its_first_check():
    chain = "pytest tests/test_a.py && pytest tests/test_b.py"
    assert not E.covers("pytest tests/test_a.py", chain)


def test_a_failed_and_chain_is_cleared_when_every_check_reran_green():
    chain = "pytest tests/test_a.py && pytest tests/test_b.py"
    fail = _row(chain, 1, A_FAIL)
    assert E.covers("pytest", chain)                                    # whole suite
    assert E.covers("pytest tests", chain)                              # enclosing folder
    assert E.covers("pytest tests/test_b.py && pytest tests/test_a.py", chain)
    again = _row(chain, 0, B_PASS + B_PASS)
    assert E.outstanding_failures([fail, again]) == []


@pytest.mark.parametrize("order", ["ab", "ba"])
def test_a_failed_chain_is_cleared_by_separate_green_reruns_of_each_check(order):
    chain = "pytest tests/test_a.py && pytest tests/test_b.py"
    fail = _row(chain, 1, A_FAIL)
    pass_a = _row("pytest tests/test_a.py", 0, A_PASS)
    pass_b = _row("pytest tests/test_b.py", 0, B_PASS)
    rows = [fail] + ([pass_a, pass_b] if order == "ab" else [pass_b, pass_a])
    assert E.outstanding_failures(rows) == []
    assert E.label(rows).endswith("passed (observed)")


def test_separate_reruns_count_only_after_the_failure_and_only_when_all_ran():
    chain = "pytest tests/test_a.py && pytest tests/test_b.py"
    fail = _row(chain, 1, A_FAIL)
    pass_a = _row("pytest tests/test_a.py", 0, A_PASS)
    pass_b = _row("pytest tests/test_b.py", 0, B_PASS)
    assert E.outstanding_failures([pass_b, fail, pass_a]) == [fail]   # b ran BEFORE
    assert E.outstanding_failures([fail, pass_a]) == [fail]           # b never re-ran
    filtered = _row("pytest -k fast tests/test_b.py", 0, B_PASS)
    assert E.outstanding_failures([fail, pass_a, filtered]) == [fail]


def test_a_passing_and_chain_covers_each_of_its_checks():
    assert E.covers("pytest tests/b && pytest tests/a", "pytest tests/a/test_x.py")
    assert E.covers("pytest tests/b && pytest tests/a", "pytest tests/b")
    assert not E.covers("pytest tests/b && pytest tests/a", "pytest tests/c")


def test_a_later_failure_of_only_the_last_check_does_not_hide_the_chain_failure():
    chain = "pytest tests/test_a.py && pytest tests/test_b.py"
    fail_chain = _row(chain, 1, A_FAIL)
    fail_b = _row("pytest tests/test_b.py", 1, A_FAIL)
    pass_b = _row("pytest tests/test_b.py", 0, B_PASS)
    assert E.outstanding_failures([fail_chain, fail_b, pass_b]) == [fail_chain]


def test_single_check_keys_and_coverage_are_unchanged():
    assert E.command_key("python -m pytest -q") == E.command_key("pytest -q")
    assert E.command_key("cd app && pytest -q") == E.command_key("pytest -q")
    assert E.covers("pytest -q", "pytest -q tests/test_a.py")
    assert E.covers("pytest tests", "pytest tests/test_a.py::test_x")
    assert not E.covers("pytest tests/test_b.py", "pytest tests/test_a.py")
    assert not E.covers("pytest -k fast", "pytest -q")
    assert not E.covers("npx jest", "pytest -q")
    assert E.covers("ls", "ls") and not E.covers("ls", "pytest")


# --------------------------------------------------------------------------- #
# 3. A run that excluded the failing tests clears nothing
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("passing, failing", [
    ("pytest --ignore=tests/test_a.py", "pytest tests/test_a.py"),
    ("pytest --ignore tests/test_a.py", "pytest tests/test_a.py"),
    ("pytest --ignore=tests/test_a.py", "pytest -q"),
    ("pytest --ignore-glob=*test_a*", "pytest tests/test_a.py"),
    ("pytest --deselect tests/test_a.py::test_bad", "pytest tests/test_a.py::test_bad"),
    ("pytest --deselect=tests/test_a.py::test_bad", "pytest tests/test_a.py"),
    ("npx jest --testPathIgnorePatterns=src/a", "npx jest src/a.test.js"),
    ("npx jest --shard=1/3", "npx jest"),
    ("npx vitest run --exclude src/a.test.ts", "npx vitest run src/a.test.ts"),
    ("npx mocha --ignore test/a.spec.js", "npx mocha test/a.spec.js"),
    ("go test -skip TestBad ./...", "go test ./..."),
    ("forge test --no-match-test testBad", "forge test --match-test testBad"),
    ("forge test --nmp test/Vault.t.sol", "forge test test/Vault.t.sol"),
    ("forge test --match-path test/A.t.sol", "forge test"),
])
def test_a_run_that_left_the_failing_tests_out_does_not_cover_them(passing, failing):
    assert not E.covers(passing, failing), (passing, failing)


def test_the_excluded_value_is_not_read_as_a_path():
    assert E.scope_of("pytest --ignore tests/a.py tests/b.py") == \
        ("pytest", ("tests/b.py",), True)
    assert E.scope_of("pytest --deselect tests/a.py::t") == ("pytest", (), True)
    assert E.scope_of("pytest tests") == ("pytest", ("tests",), False)


def test_an_ignored_run_leaves_the_failure_outstanding():
    fail = _row("pytest tests/test_a.py", 1, A_FAIL)
    skipped = _row("pytest --ignore=tests/test_a.py", 0, B_PASS)
    assert skipped["verdict"] == E.PASS                  # the run itself did pass
    assert E.outstanding_failures([fail, skipped]) == [fail]
    assert E.label([fail, skipped]) == "1 failed (observed)"


def test_a_failure_of_a_filtered_run_is_still_covered_by_the_whole_suite():
    assert E.covers("pytest", "pytest --ignore=tests/slow.py")
    assert E.covers("pytest", "pytest --deselect tests/a.py::t tests/b.py")
