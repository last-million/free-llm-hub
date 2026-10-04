"""evidence.classify: a verdict from what a test/build command PRINTED and
the exit code it RETURNED -- never from words alone.

Outputs below are the tools' real summary formats (pytest 8, unittest 3.12,
jest 29, vitest 1.x, mocha 10, cargo 1.8x, go 1.22, tsc 5.x, npm 10 / pnpm 9 /
yarn 1 script echoes).
"""
import pytest

import evidence as E

PYTEST_PASS = """============================= test session starts =============================
platform win32 -- Python 3.12.1, pytest-8.3.2, pluggy-1.5.0
rootdir: C:\\work\\app
collected 12 items

tests\\test_app.py ............                                           [100%]

============================= 12 passed in 0.31s ==============================
"""
PYTEST_FAIL = """collected 13 items

tests/test_app.py ..F.........F                                          [100%]

=================================== FAILURES ===================================
___________________________________ test_add ___________________________________
    def test_add():
>       assert add(1, 2) == 4
E       assert 3 == 4
=========================== short test summary info ============================
FAILED tests/test_app.py::test_add - assert 3 == 4
FAILED tests/test_app.py::test_sub - assert 1 == 0
========================= 2 failed, 11 passed in 0.42s =========================
"""
PYTEST_Q_PASS = "....................                                     [100%]\n20 passed, 1 skipped in 0.12s\n"
PYTEST_NONE = "============================ no tests ran in 0.01s =============================\n"
PYTEST_COLLECT_ERR = """==================================== ERRORS ====================================
_____________________ ERROR collecting tests/test_app.py ______________________
ImportError while importing test module
=========================== short test summary info ============================
ERROR tests/test_app.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
=============================== 1 error in 0.20s ===============================
"""

UNITTEST_PASS = "..........\n----------------------------------------------------------------------\nRan 10 tests in 0.004s\n\nOK (skipped=1)\n"
UNITTEST_FAIL = ("..F.E\n======================================================================\n"
                 "FAIL: test_x (test_mod.T.test_x)\n----------------------------------------------------------------------\n"
                 "AssertionError: 1 != 2\n\n----------------------------------------------------------------------\n"
                 "Ran 5 tests in 0.002s\n\nFAILED (failures=1, errors=1)\n")
UNITTEST_NONE = "\n----------------------------------------------------------------------\nRan 0 tests in 0.000s\n\nNO TESTS RAN\n"

JEST_PASS = """PASS src/sum.test.js
PASS src/app.test.js

Test Suites: 2 passed, 2 total
Tests:       1 skipped, 7 passed, 8 total
Snapshots:   0 total
Time:        1.234 s
Ran all test suites.
"""
JEST_FAIL = """FAIL src/sum.test.js
  ● sum › adds

    expect(received).toBe(expected) // Object.is equality

Test Suites: 1 failed, 1 passed, 2 total
Tests:       1 failed, 6 passed, 7 total
Snapshots:   0 total
Time:        1.1 s
"""
JEST_NONE = "No tests found, exiting with code 1\nRun with `--passWithNoTests` to exit with code 0\n"

VITEST_PASS = """
 RUN  v1.6.0 C:/work/app

 \u2713 src/sum.test.ts  (3 tests) 2ms
 \u2713 src/app.test.ts  (6 tests) 5ms

 Test Files  2 passed (2)
      Tests  9 passed (9)
   Start at  10:11:12
   Duration  412ms (transform 31ms, setup 0ms, collect 40ms, tests 7ms)
"""
VITEST_FAIL = """
 RUN  v1.6.0 C:/work/app

 \u276f src/sum.test.ts  (3 tests | 1 failed) 4ms
   \u00d7 sum > adds

 Test Files  1 failed | 1 passed (2)
      Tests  1 failed | 8 passed (9)
"""
VITEST_NONE = "\nNo test files found, exiting with code 1\n"

MOCHA_PASS = "\n  sum\n    \u2714 adds\n\n\n  5 passing (12ms)\n  1 pending\n\n"
MOCHA_FAIL = "\n  4 passing (10ms)\n  2 failing\n\n  1) sum\n       adds:\n     AssertionError\n"

CARGO_PASS = """   Compiling app v0.1.0 (C:\\work\\app)
    Finished `test` profile [unoptimized + debuginfo] target(s) in 1.20s
     Running unittests src\\lib.rs (target\\debug\\deps\\app-1234.exe)

running 3 tests
test tests::a ... ok
test tests::b ... ok
test tests::c ... ok

test result: ok. 3 passed; 0 failed; 1 ignored; 0 measured; 0 filtered out; finished in 0.00s

   Doc-tests app

running 1 test
test src\\lib.rs - add (line 3) ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.21s
"""
CARGO_FAIL = """running 2 tests
test tests::a ... ok
test tests::b ... FAILED

failures:

---- tests::b stdout ----
thread 'tests::b' panicked at src\\lib.rs:12:9:
assertion `left == right` failed

test result: FAILED. 1 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s

error: test failed, to rerun pass `--lib`
"""
CARGO_COMPILE = """   Compiling app v0.1.0
error[E0425]: cannot find value `x` in this scope
 --> src\\lib.rs:3:5
error: could not compile `app` (lib test) due to 1 previous error
"""
CARGO_NONE = "running 0 tests\n\ntest result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s\n"

GO_PASS = "ok  \texample.com/app/calc\t0.004s\nok  \texample.com/app/web\t(cached)\n?   \texample.com/app/cmd\t[no test files]\n"
GO_PASS_V = "=== RUN   TestAdd\n--- PASS: TestAdd (0.00s)\n=== RUN   TestSub\n--- PASS: TestSub (0.00s)\nPASS\nok  \texample.com/app/calc\t0.005s\n"
GO_FAIL = ("--- FAIL: TestAdd (0.00s)\n    calc_test.go:9: got 3, want 4\nFAIL\n"
           "FAIL\texample.com/app/calc\t0.005s\nok  \texample.com/app/web\t0.003s\nFAIL\n")
GO_NONE = "?   \texample.com/app/cmd\t[no test files]\n?   \texample.com/app/calc\t[no test files]\n"

TSC_FAIL = ("src/app.ts(3,7): error TS2322: Type 'string' is not assignable to type 'number'.\n"
            "src/app.ts(9,1): error TS2304: Cannot find name 'foo'.\n")
TSC_PRETTY_FAIL = ("src/app.ts:3:7 - error TS2322: Type 'string' is not assignable to type 'number'.\n\n"
                   "3 const x: number = 'a';\n        ~\n\n\nFound 1 error in src/app.ts:3\n\n")

NPM_VITEST_PASS = "\n> app@1.0.0 test\n> vitest run\n" + VITEST_PASS
NPM_JEST_FAIL = "\n> app@1.0.0 test\n> jest --ci\n\n" + JEST_FAIL + \
    "npm error Lifecycle script `test` failed with error:\nnpm error code 1\n"
PNPM_BUILD_PASS = ("\n> app@0.1.0 build C:\\work\\app\n> tsc && vite build\n\n"
                   "vite v5.2.0 building for production...\n\u2713 34 modules transformed.\n"
                   "dist/index.html                 0.46 kB \u2502 gzip:  0.30 kB\n"
                   "\u2713 built in 1.24s\n")
NPM_BUILD_TSC_FAIL = "\n> app@0.1.0 build\n> tsc && vite build\n\n" + TSC_FAIL
NPM_NO_TEST = ('\n> app@1.0.0 test\n> echo "Error: no test specified" && exit 1\n\n'
               "Error: no test specified\n")
YARN_JEST_PASS = "yarn run v1.22.19\n$ jest\n" + JEST_PASS + "Done in 2.31s.\n"


@pytest.mark.parametrize("command, code, out, verdict, passed, failed", [
    ("pytest -q", 0, PYTEST_PASS, E.PASS, 12, 0),
    ("python -m pytest", 1, PYTEST_FAIL, E.FAIL, 11, 2),
    ("pytest -q", 0, PYTEST_Q_PASS, E.PASS, 20, 0),
    ("pytest -q", 5, PYTEST_NONE, E.NO_TESTS, 0, 0),
    ("pytest", 2, PYTEST_COLLECT_ERR, E.FAIL, 0, 1),
    (r".venv\Scripts\python.exe -m pytest tests", 0, PYTEST_PASS, E.PASS, 12, 0),
    ("uv run pytest -x", 0, PYTEST_PASS, E.PASS, 12, 0),
    ("python -m unittest discover -s tests", 0, UNITTEST_PASS, E.PASS, 9, 0),
    ("python -m unittest", 1, UNITTEST_FAIL, E.FAIL, 3, 2),
    ("python -m unittest", 5, UNITTEST_NONE, E.NO_TESTS, 0, 0),
    ("npx jest", 0, JEST_PASS, E.PASS, 7, 0),
    ("npx jest --ci", 1, JEST_FAIL, E.FAIL, 6, 1),
    ("npx jest", 1, JEST_NONE, E.NO_TESTS, 0, 0),
    ("npx vitest run", 0, VITEST_PASS, E.PASS, 9, 0),
    ("pnpm vitest run", 1, VITEST_FAIL, E.FAIL, 8, 1),
    ("npx vitest run", 1, VITEST_NONE, E.NO_TESTS, 0, 0),
    ("npx mocha", 0, MOCHA_PASS, E.PASS, 5, 0),
    ("mocha test/", 2, MOCHA_FAIL, E.FAIL, 4, 2),
    ("cargo test", 0, CARGO_PASS, E.PASS, 4, 0),
    ("cargo test --lib", 101, CARGO_FAIL, E.FAIL, 1, 1),
    ("cargo test", 101, CARGO_COMPILE, E.FAIL, 0, 2),
    ("cargo test", 0, CARGO_NONE, E.NO_TESTS, 0, 0),
    ("go test ./...", 0, GO_PASS, E.PASS, 2, 0),
    ("go test -v ./calc", 0, GO_PASS_V, E.PASS, 2, 0),
    ("go test ./...", 1, GO_FAIL, E.FAIL, 0, 1),
    ("go test ./...", 0, GO_NONE, E.NO_TESTS, 0, 0),
    ("npx tsc --noEmit", 0, "", E.PASS, 0, 0),
    ("npx tsc --noEmit", 2, TSC_FAIL, E.FAIL, 0, 2),
    ("tsc --noEmit --pretty", 1, TSC_PRETTY_FAIL, E.FAIL, 0, 1),
    ("npm test", 0, NPM_VITEST_PASS, E.PASS, 9, 0),
    ("npm test", 1, NPM_JEST_FAIL, E.FAIL, 6, 1),
    ("pnpm run build", 0, PNPM_BUILD_PASS, E.PASS, 0, 0),
    ("npm run build", 2, NPM_BUILD_TSC_FAIL, E.FAIL, 0, 2),
    ("npm test", 1, NPM_NO_TEST, E.NO_TESTS, 0, 0),
    ("yarn test", 0, YARN_JEST_PASS, E.PASS, 7, 0),
])
def test_each_adapter_reads_its_tools_own_summary(command, code, out, verdict, passed, failed):
    r = E.classify(command, code, out)
    assert r["verdict"] == verdict, r
    assert (r["passed"], r["failed"]) == (passed, failed), r
    assert set(r) >= {"tool", "verdict", "passed", "failed", "skipped", "line"}


def test_the_tool_and_version_are_named():
    r = E.classify("pytest -q", 0, PYTEST_PASS)
    assert r["tool"] == "pytest" and r["version"] == "8.3.2" and r["line"] == "12 passed"
    assert E.classify("npx vitest run", 0, VITEST_PASS)["version"] == "1.6.0"
    assert E.classify("npm test", 0, NPM_VITEST_PASS)["tool"] == "npm test > vitest"
    assert E.classify("pnpm run build", 0, PNPM_BUILD_PASS)["tool"] == \
        "pnpm run build > tsc + vite build"


# --------------------------------------------------------------------------- #
# UNDETERMINED: never PASS from words, never without exit 0
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("command, code, out", [
    ("pytest -q", None, PYTEST_PASS),                  # no exit code
    ("pytest -q", 0, "All tests passed! ok green"),    # words, no summary
    ("pytest -q", 0, "Finished. ok."),
    ("pytest -q", 1, "something broke"),               # failure not recognised
    ("pytest -q", 0, PYTEST_FAIL),                     # exit 0 contradicts the summary
    ("pytest -q", 1, PYTEST_PASS),                     # non-zero contradicts it
    ("ls -la", 0, "total 3"),                          # not a check
    ("python app.py", 0, PYTEST_PASS),                 # a script, not a known tool
    ("make test", 0, PYTEST_PASS),                     # unknown wrapper
    ("pytest -q | tail -5", 0, PYTEST_PASS),           # the exit code is tail's
    ("pytest -q; echo done", 0, PYTEST_PASS),
    ("pytest -q || true", 0, PYTEST_FAIL),
    ("npm run build", 0, "\n> app@1.0.0 build\n> node build.js\n\nDone.\n"),
    ("npm test", 0, "\n> app@1.0.0 test\n> node run-tests.js\n\nall good\n"),
    ("cargo build", 0, "    Finished `dev` profile target(s) in 0.5s\n"),
    ("npx tsc --version", 0, "Version 5.4.5\n"),
    ("npx tsc --noEmit", 0, TSC_FAIL),
])
def test_anything_else_is_undetermined(command, code, out):
    assert E.classify(command, code, out)["verdict"] == E.UNDETERMINED


def test_a_generic_word_never_yields_pass():
    for word in ("ok", "passed", "Finished", "green", "success", "All good, tests pass"):
        assert E.classify("pytest", 0, word)["verdict"] != E.PASS
        assert E.classify("npm test", 0, word)["verdict"] != E.PASS


def test_a_build_passes_only_on_exit_0_and_no_error_lines_in_its_format():
    assert E.classify("tsc --noEmit", 0, "")["verdict"] == E.PASS
    assert E.classify("tsc --noEmit", 0, TSC_FAIL)["verdict"] == E.UNDETERMINED
    assert E.classify("npx vite build", 0, "\u2713 built in 1.2s\n")["verdict"] == E.PASS
    assert E.classify("npx vite build", 0, "\u2713 built in 1.2s\n" + TSC_FAIL)["verdict"] \
        == E.UNDETERMINED
    assert E.classify("npx vite build", 0, "building...\n")["verdict"] == E.UNDETERMINED


def test_claude_is_error_counts_as_a_nonzero_exit():
    assert E.classify("pytest -q", None, PYTEST_FAIL, is_error=True)["verdict"] == E.FAIL
    assert E.classify("pytest -q", 0, PYTEST_PASS, is_error=True)["verdict"] == E.UNDETERMINED


# --------------------------------------------------------------------------- #
# Reading the command
# --------------------------------------------------------------------------- #

def test_codex_shell_wrappers_are_unwrapped():
    # codex-rs joins argv with shlex; on Windows argv[0] is the shell itself.
    win = ("'C:\\Program Files\\WindowsApps\\Microsoft.PowerShell_7.6.6.0_x64__8wekyb3d8bbwe"
           "\\pwsh.exe' -NoProfile -Command 'python -m pytest -q'")
    assert E.inner_command(win) == "python -m pytest -q"
    assert E.inner_command("/bin/bash -lc 'cd web && npm test'") == "cd web && npm test"
    assert E.inner_command('cmd.exe /d /s /c "npm run build"') == "npm run build"
    assert E.classify(win, 0, PYTEST_PASS)["verdict"] == E.PASS
    assert E.detect("/bin/bash -lc 'cd web && npm test'")["tool"] == "npm test"


def test_detect_names_only_real_checks():
    assert E.detect("cd app && pytest -q")["tool"] == "pytest"
    assert E.detect("OPENAI_API_KEY=x pytest -q")["tool"] == "pytest"
    assert E.detect("npx --yes jest --ci")["tool"] == "jest"
    assert E.detect("poetry run pytest")["tool"] == "pytest"
    assert E.detect("yarn tsc --noEmit")["tool"] == "tsc"
    assert E.detect("pnpm --filter web test")["tool"] == "pnpm test"
    assert E.detect("go test ./...")["tool"] == "go test"
    assert E.detect("bun test")["tool"] == "bun test"
    for cmd in ("cat pytest.ini", "ls", "npm install", "git status", "python app.py",
                "npx tsc --init", "echo pytest", "cargo build", "go vet ./..."):
        assert E.detect(cmd) is None, cmd


def test_command_keys_and_coverage():
    assert E.command_key("python -m pytest -q") == E.command_key("pytest -q")
    assert E.covers("pytest -q", "pytest -q tests/test_a.py")
    assert E.covers("pytest tests", "pytest tests/test_a.py::test_x")
    assert not E.covers("pytest tests/test_b.py", "pytest tests/test_a.py")
    assert not E.covers("pytest -k fast", "pytest -q")
    assert not E.covers("npx jest", "pytest -q")


def test_outstanding_failures_and_labels():
    fail = dict(E.classify("pytest -q", 1, PYTEST_FAIL), command="pytest -q")
    ok = dict(E.classify("python -m pytest -q", 0, PYTEST_PASS), command="python -m pytest -q")
    assert E.outstanding_failures([fail]) == [fail]
    assert E.outstanding_failures([fail, ok]) == []
    assert E.label([fail]) == "2 failed (observed)"
    assert E.label([fail, ok]) == "12 passed (observed)"
    piped = dict(E.classify("pytest -q | tail", 0, PYTEST_PASS), command="pytest -q | tail")
    assert piped["verdict"] == E.UNDETERMINED
    assert E.outstanding_failures([fail, piped]) == [fail], "an unreadable rerun clears nothing"
    tsc = dict(E.classify("tsc --noEmit", 2, TSC_FAIL), command="tsc --noEmit")
    assert E.label([tsc]) == "tsc: 2 type errors (observed)"
    none = dict(E.classify("pytest", 5, PYTEST_NONE), command="pytest")
    assert E.label([none]) == "no tests ran (observed)"
    assert E.label([]) == ""


def test_claims_are_recognised():
    for text in ("All 12 tests pass.", "Tests are green now", "12 passed", "The build succeeded",
                 "tsc reports no errors", "compiles cleanly"):
        assert E.claims_checks_passed(text), text
    for text in ("I wrote the page.", "Added a footer and a test file."):
        assert not E.claims_checks_passed(text), text


def test_classify_never_raises():
    for args in ((None, None, None), (123, "x", object()), ("pytest", "0", b"bytes")):
        r = E.classify(*args)
        assert r["verdict"] in E.VERDICTS
