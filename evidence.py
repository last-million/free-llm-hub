"""What a test or build command's OBSERVED result says -- and nothing more.

WHY THIS EXISTS. Until 2026-10-04 the hub never confirmed a worker's work.
A Multi phase was DONE when its final message existed; with a manager it was
"verified" when the manager agreed with that message; and memory filed a
"verified command" whenever the REPLY contained a word like "green",
"succeeded" or "no errors". Every one of those is a claim. Meanwhile the CLIs
were telling the hub what actually happened -- codex's command_execution
carries `exit_code`, Claude Code's tool_result carries `is_error` and
"Exit code N", opencode's tool part carries `state.metadata.exit` -- and the
parsers threw it away.

THE RULES (owner-approved, after the "evidence runner" design):

  * a verdict is PASS | FAIL | NO_TESTS | UNDETERMINED;
  * PASS needs exit code 0 AND the tool's own pass summary (for a build
    tool: exit 0 AND none of the tool's own error lines);
  * FAIL needs a non-zero exit AND a failure the tool itself reported;
  * a recognised "no tests ran" is NO_TESTS;
  * everything else is UNDETERMINED: an unknown tool, exit 0 with output no
    adapter recognises, a missing exit code, an exit code a pipe / `|| true`
    / `; echo` took over;
  * a generic word ("ok", "passed", "Finished", "green") never yields PASS.

Pure: no app import, no I/O, no subprocess. The hub never RUNS anything here;
it reads what the agent's CLI already ran (owner rule: observation only).
"""
import re
import shlex

PASS = "PASS"
FAIL = "FAIL"
NO_TESTS = "NO_TESTS"
UNDETERMINED = "UNDETERMINED"
VERDICTS = (PASS, FAIL, NO_TESTS, UNDETERMINED)

# What a parser keeps of a command's output. Every adapter reads a summary
# the tool prints LAST, so the tail is the part that matters.
OUTPUT_TAIL = 4000

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def tail(text, limit=OUTPUT_TAIL):
    """The last `limit` characters of a command's output, as str."""
    s = text if isinstance(text, str) else ("" if text is None else str(text))
    return s[-limit:] if len(s) > limit else s


def clean_output(text):
    """Output as an adapter reads it: no colour codes, \\n line ends, and a
    progress line rewritten with \\r reduced to what it finally showed."""
    s = _ANSI_RE.sub("", text if isinstance(text, str) else str(text or ""))
    s = s.replace("\r\n", "\n")
    if "\r" in s:
        s = "\n".join(line.rsplit("\r", 1)[-1] for line in s.split("\n"))
    return s


# --------------------------------------------------------------------------- #
# Reading the command
# --------------------------------------------------------------------------- #

_SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "pwsh", "powershell", "cmd"}
_BODY_FLAGS = {"-c", "-lc", "-ic", "-lic", "-command", "/c", "/k", "/s/c"}


def _base(token):
    """A command token's program name: no quotes, no directory, no .exe."""
    t = (token or "").strip().strip("'\"")
    t = re.split(r"[\\/]", t)[-1].lower()
    for ext in (".exe", ".cmd", ".bat", ".ps1", ".js", ".mjs", ".cjs"):
        if t.endswith(ext):
            t = t[: -len(ext)]
            break
    return t


def inner_command(command):
    """The command the shell actually ran, wrappers removed.

    codex reports argv shlex-JOINED (codex-rs thread_history.rs test:
    ["echo", "hello world"] -> "echo 'hello world'"), and on Windows that argv
    is the shell itself: 'C:\\...\\pwsh.exe' -NoProfile -Command 'pytest -q'
    (a real rollout, 2026-09-27, shows the argv). shlex.split undoes that join
    exactly. Claude Code and opencode report the raw command text, which is
    returned as it is (shlex would eat a Windows path's backslashes)."""
    cmd = " ".join(str(command or "").split()) if "\n" not in str(command or "") \
        else str(command).strip()
    for _ in range(3):                      # nested wrappers, bounded
        first = cmd.lstrip().split(None, 1)[0] if cmd.strip() else ""
        if first[:1] in "'\"":
            q = first[0]
            end = cmd.lstrip().find(q, 1)
            first = cmd.lstrip()[: end + 1] if end > 0 else first
        if _base(first) not in _SHELLS:
            return cmd
        try:
            argv = shlex.split(cmd, posix=True)
        except ValueError:
            return cmd
        body = None
        for i, tok in enumerate(argv[1:], 1):
            if tok.lower() in _BODY_FLAGS:
                body = " ".join(argv[i + 1:]) if i + 1 < len(argv) else ""
                break
        if body is None:
            return cmd
        cmd = body.strip()
    return cmd


def segments(command):
    """[(segment, operator_after)] of a shell command line, split on the
    top-level && || ; | & and newlines (quotes respected; `2>&1` is not a
    separator)."""
    out, buf, quote, i = [], [], None, 0
    s = command or ""
    while i < len(s):
        c = s[i]
        if quote:
            buf.append(c)
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "'\"":
            quote = c
            buf.append(c)
            i += 1
            continue
        two = s[i:i + 2]
        if two in ("&&", "||"):
            out.append(("".join(buf).strip(), two))
            buf = []
            i += 2
            continue
        if c in ";|\n":
            out.append(("".join(buf).strip(), c))
            buf = []
            i += 1
            continue
        if c == "&":
            prev = s[i - 1] if i else ""
            nxt = s[i + 1] if i + 1 < len(s) else ""
            if prev in "<>" or nxt == ">":            # 2>&1, &>file
                buf.append(c)
                i += 1
                continue
            out.append(("".join(buf).strip(), "&"))
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    out.append(("".join(buf).strip(), None))
    return [(seg, op) for seg, op in out if seg or op]


def _tokens(segment):
    try:
        toks = shlex.split(segment, posix=False)
    except ValueError:
        toks = segment.split()
    return [t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "'\"" else t
            for t in toks]


def argv(command):
    """The command's words, wrappers removed (quotes off, backslashes kept)."""
    return _tokens(inner_command(command))


_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PLAIN_LAUNCHERS = {"env", "time", "nice", "command", "exec", "xvfb-run", "cross-env",
                    "dotenv", "npx", "pnpx", "bunx", "uvx"}
_RUN_LAUNCHERS = {"uv", "poetry", "pipenv", "pdm", "hatch", "rye"}   # "<x> run ..."
_PY_RE = re.compile(r"^(?:python|pythonw|py)(?:\d+(?:\.\d+)?)?$")
_TEST_SCRIPT_RE = re.compile(r"^(?:test|tests|t|tst)(?:[:._-][\w:.-]+)?$", re.I)
_BUILD_SCRIPT_RE = re.compile(r"^(?:build|typecheck|type-check|check-types|tsc)(?:[:._-][\w:.-]+)?$",
                              re.I)
_DIRECT_TOOLS = {"jest", "vitest", "mocha", "tsc", "pytest", "vite", "next", "webpack"}
_TSC_NOT_A_CHECK = {"-v", "--version", "-h", "--help", "--init", "-w", "--watch",
                    "--showconfig", "--all", "--listfilesonly"}


def _strip_launchers(toks):
    """Drop env assignments and launchers (npx, uv run, python -m, ...).
    Returns the tokens starting at the tool, or [] for a python script."""
    toks = list(toks)
    for _ in range(8):
        if not toks:
            return toks
        head = toks[0]
        b = _base(head)
        if _ENV_ASSIGN_RE.match(head):
            toks = toks[1:]
            continue
        if b == "timeout" and len(toks) > 2 and not toks[1].startswith("/"):
            # GNU timeout DURATION CMD (Windows `timeout /t` is a sleep)
            toks = toks[1:]
            while toks and toks[0].startswith("-"):
                toks = toks[1:]
            toks = toks[1:]                          # the duration
            continue
        if b in _PLAIN_LAUNCHERS:
            toks = toks[1:]
            while toks and (toks[0].startswith("-") or _ENV_ASSIGN_RE.match(toks[0])):
                toks = toks[1:]
            if toks and toks[0] == "--":
                toks = toks[1:]
            continue
        if b in _RUN_LAUNCHERS and len(toks) > 1 and toks[1] == "run":
            toks = toks[2:]
            while toks and toks[0].startswith("-"):
                toks = toks[1:]
            continue
        if b in ("pnpm", "npm", "yarn") and len(toks) > 1 and toks[1] == "exec":
            toks = toks[2:]
            continue
        if b == "coverage" and len(toks) > 1 and toks[1] == "run":
            toks = toks[2:]
            while toks and toks[0].startswith("-") and toks[0] != "-m":
                toks = toks[1:]
            if toks and toks[0] == "-m" and len(toks) > 1:
                toks = [toks[1]] + toks[2:]
            continue
        if _PY_RE.match(b):
            rest = toks[1:]
            while rest and rest[0].startswith("-") and rest[0] != "-m":
                rest = rest[2:] if rest[0] in ("-X", "-W") else rest[1:]
            if rest and rest[0] == "-m" and len(rest) > 1:
                toks = [rest[1]] + rest[2:]
                continue
            return []                               # `python script.py`: unknown
        return toks
    return toks


def _script_name(b, args):
    """The package-script a npm/pnpm/yarn/bun call runs, or None."""
    args = [a for a in args]
    while args and args[0].startswith("-"):
        # pnpm -r / --filter x / -C dir / yarn --cwd dir
        flag = args.pop(0)
        if flag in ("--filter", "-F", "-C", "--dir", "--cwd", "--prefix", "-w", "--workspace") \
                and args and not args[0].startswith("-"):
            args.pop(0)
    if b == "yarn" and args and args[0] == "workspace" and len(args) > 2:
        args = args[2:]
    if not args:
        return None
    if args[0] in ("run", "run-script", "rum", "urn"):
        return args[1] if len(args) > 1 else None
    if b == "npm":
        if args[0] in ("test", "t", "tst"):
            return "test"
        return None                                  # `npm build` is not a script run
    return args[0]                                   # pnpm/yarn/bun <script>


def _segment_tool(segment):
    """{tool, kind, wrapper, script, args} when a segment runs a test or
    build tool, else None."""
    toks = _strip_launchers(_tokens(segment))
    if not toks:
        return None
    b = _base(toks[0])
    args = toks[1:]
    if b in ("pytest", "py.test"):
        return {"tool": "pytest", "kind": "test", "wrapper": None, "args": args}
    if b == "unittest":
        return {"tool": "unittest", "kind": "test", "wrapper": None, "args": args}
    if b in ("jest", "vitest", "mocha", "_mocha"):
        return {"tool": "mocha" if b == "_mocha" else b, "kind": "test", "wrapper": None,
                "args": args}
    if b == "tsc":
        if any(a.lower() in _TSC_NOT_A_CHECK for a in args):
            return None
        return {"tool": "tsc", "kind": "build", "wrapper": None, "args": args}
    if b == "cargo":
        sub = [a for a in args if not a.startswith(("-", "+"))][:1]
        if sub == ["test"]:
            return {"tool": "cargo test", "kind": "test", "wrapper": None,
                    "args": args[args.index("test") + 1:]}
        return None
    if b == "go":
        if args[:1] == ["test"]:
            return {"tool": "go test", "kind": "test", "wrapper": None, "args": args[1:]}
        return None
    if b == "node" and "--test" in args:
        return {"tool": "node --test", "kind": "test", "wrapper": None, "args": args}
    if b == "bun" and args[:1] == ["test"]:
        return {"tool": "bun test", "kind": "test", "wrapper": None, "args": args[1:]}
    if b == "vite" and args[:1] == ["build"]:
        return {"tool": "vite build", "kind": "build", "wrapper": None, "args": args[1:]}
    if b == "next" and args[:1] == ["build"]:
        return {"tool": "next build", "kind": "build", "wrapper": None, "args": args[1:]}
    if b in ("webpack", "webpack-cli") and "--watch" not in args and "serve" not in args:
        return {"tool": "webpack", "kind": "build", "wrapper": None, "args": args}
    if b in ("npm", "pnpm", "yarn", "bun"):
        script = _script_name(b, args)
        if not script:
            return None
        if b != "npm" and _base(script) in _DIRECT_TOOLS and script in args:
            # `pnpm vitest run`, `yarn tsc --noEmit`: the package's own bin.
            return _segment_tool(" ".join(args[args.index(script):]))
        if _TEST_SCRIPT_RE.match(script):
            kind = "test"
        elif _BUILD_SCRIPT_RE.match(script):
            kind = "build"
        else:
            return None
        if kind == "test" and script == "test" and b != "bun":
            label = "%s test" % b
        else:
            label = "%s run %s" % (b, script)
        return {"tool": label, "kind": kind, "wrapper": b, "script": script, "args": []}
    return None


def _check_segments(command):
    """[(index, descriptor)] of the segments that run a test/build tool, plus
    the segment list itself."""
    segs = segments(inner_command(command))
    found = []
    for i, (seg, _op) in enumerate(segs):
        d = _segment_tool(seg)
        if d:
            found.append((i, d))
    return found, segs


def detect(command):
    """The first test/build tool `command` runs (its descriptor), or None.
    What decides whether an observed result is evidence at all."""
    found, _ = _check_segments(command)
    return found[0][1] if found else None


_FILTER_FLAGS = {"-k", "-m", "-t", "--testnamepattern", "-run", "--run", "--grep", "-g",
                 "--filter", "--testpathpattern", "-p", "--package", "--exact"}


def command_key(command):
    """One spelling per check: `python -m pytest -q` and `pytest -q` are the
    same run, so a later PASS of either clears a FAIL of the other."""
    found, segs = _check_segments(command)
    if not found:
        return " ".join(str(command or "").lower().split())[:200]
    i, d = found[-1]
    if d.get("wrapper"):
        return "%s %s" % (d["wrapper"], d.get("script") or "")
    args = [a for a in d.get("args") or () if a]
    return " ".join([d["tool"]] + args).lower()[:200]


def scope_of(command):
    """(tool, positional paths, filtered?) of a check -- what part of the
    suite it ran. A run with no paths and no filter is the WHOLE suite."""
    d = detect(command)
    if not d:
        return None, (), False
    paths, filtered, skip = [], False, False
    for a in d.get("args") or ():
        if skip:
            skip = False
            continue
        low = a.lower()
        if low.split("=", 1)[0] in _FILTER_FLAGS:
            filtered = True
            skip = "=" not in low
            continue
        if a.startswith("-"):
            continue
        paths.append(a.replace("\\", "/").rstrip("/"))
    return d["tool"], tuple(paths), filtered


def covers(pass_command, fail_command):
    """True when a PASS of `pass_command` also re-ran what `fail_command` ran:
    the same check, or the same tool over the whole suite / a folder that
    contains the failing path."""
    if command_key(pass_command) == command_key(fail_command):
        return True
    tp, pp, fp = scope_of(pass_command)
    tf, pf, ff = scope_of(fail_command)
    if not tp or tp != tf or fp:
        return False
    if not pp:
        return True                         # the whole suite, unfiltered
    if not pf:
        return False                        # a part does not cover the whole
    return all(any(f == p or f.startswith(p + "/") or f.startswith(p + "::")
                   for p in pp) for f in pf)


# --------------------------------------------------------------------------- #
# Adapters: each reads ONE tool's own summary format
# --------------------------------------------------------------------------- #

def _result(tool, verdict, passed=0, failed=0, skipped=0, line="", version=None, kind=None):
    return {"tool": tool, "verdict": verdict, "passed": int(passed or 0),
            "failed": int(failed or 0), "skipped": int(skipped or 0),
            "line": (line or "")[:200], "version": version, "kind": kind}


def _counts(body, words):
    got = {}
    for n, w in re.findall(r"(\d+)\s+([A-Za-z]+)", body or ""):
        w = w.lower()
        for key, names in words.items():
            if w in names:
                got[key] = got.get(key, 0) + int(n)
    return got


# pytest: "===== 2 failed, 10 passed, 1 skipped in 0.42s =====" (-q drops the
# banner: "12 passed in 0.31s"), "no tests ran in 0.01s".
_PYTEST_SUMMARY_RE = re.compile(
    r"^[=\s]*((?:\d+ (?:passed|failed|errors?|skipped|xfailed|xpassed|warnings?|"
    r"deselected|rerun)(?:, )?)+)\s+in\s+[\d.]+s\b[^\n]*$", re.M)
_PYTEST_NONE_RE = re.compile(r"^[=\s]*no tests ran(?:\s+in\s+[\d.]+s)?\b", re.M | re.I)
_PYTEST_VERSION_RE = re.compile(r"\bpytest-(\d+(?:\.\d+)+)")


def _pytest(out, zero, nonzero):
    version = (_PYTEST_VERSION_RE.findall(out) or [None])[-1]
    summ = _PYTEST_SUMMARY_RE.findall(out)
    if summ:
        body = summ[-1].strip().rstrip(",")
        c = _counts(body, {"passed": ("passed",), "failed": ("failed",),
                           "errors": ("error", "errors"), "skipped": ("skipped",),
                           "deselected": ("deselected",)})
        failed = c.get("failed", 0) + c.get("errors", 0)
        passed, skipped = c.get("passed", 0), c.get("skipped", 0)
        if nonzero and failed:
            return _result("pytest", FAIL, passed, failed, skipped, body, version)
        if zero and passed and not failed:
            return _result("pytest", PASS, passed, 0, skipped, body, version)
        if not passed and not failed and (c.get("deselected") or skipped) and not zero:
            return _result("pytest", NO_TESTS, 0, 0, skipped, body, version)
        return _result("pytest", UNDETERMINED, passed, failed, skipped, body, version)
    if _PYTEST_NONE_RE.search(out) or re.search(r"^collected 0 items\b", out, re.M):
        return _result("pytest", NO_TESTS, line="no tests ran", version=version)
    return _result("pytest", UNDETERMINED, version=version)


# unittest: "Ran 12 tests in 0.004s" then "OK" / "OK (skipped=1)" /
# "FAILED (failures=1, errors=2)"; 3.12+: "NO TESTS RAN" (exit 5).
_UT_RAN_RE = re.compile(r"^Ran (\d+) tests? in [\d.]+s\s*$", re.M)
_UT_END_RE = re.compile(r"^(OK|FAILED|NO TESTS RAN)(?: \(([^)]*)\))?\s*$", re.M)


def _unittest(out, zero, nonzero):
    ran = _UT_RAN_RE.findall(out)
    ends = _UT_END_RE.findall(out)
    if not ends and not ran:
        return _result("unittest", UNDETERMINED)
    n = int(ran[-1]) if ran else 0
    word, detail = ends[-1] if ends else ("", "")
    c = {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", detail or "")}
    failed = c.get("failures", 0) + c.get("errors", 0) + c.get("unexpected_successes", 0)
    skipped = c.get("skipped", 0)
    line = ("Ran %d tests: %s" % (n, word + (" (%s)" % detail if detail else ""))).strip()
    if word == "NO TESTS RAN" or (ran and n == 0 and word != "FAILED"):
        return _result("unittest", NO_TESTS, line=line)
    if word == "FAILED" and nonzero:
        return _result("unittest", FAIL, max(0, n - failed - skipped), failed, skipped, line)
    if word == "OK" and zero and n > 0:
        return _result("unittest", PASS, max(0, n - skipped), 0, skipped, line)
    return _result("unittest", UNDETERMINED, max(0, n - failed - skipped), failed, skipped, line)


# jest: "Tests:       1 failed, 1 skipped, 5 passed, 7 total" and
# "Test Suites: 1 failed, 2 passed, 3 total"; "No tests found, exiting with code 1".
_JEST_TESTS_RE = re.compile(r"^Tests:\s+(.*?\d+ total)\s*$", re.M)
_JEST_SUITES_RE = re.compile(r"^Test Suites:\s+(.*?\d+ total)\s*$", re.M)
_JEST_NONE_RE = re.compile(r"^No tests found, exiting with code \d+", re.M)
_JEST_WORDS = {"passed": ("passed",), "failed": ("failed",), "skipped": ("skipped", "todo", "pending")}


def _jest(out, zero, nonzero):
    tests = _JEST_TESTS_RE.findall(out)
    suites = _JEST_SUITES_RE.findall(out)
    if _JEST_NONE_RE.search(out) and not tests:
        return _result("jest", NO_TESTS, line="no tests found")
    if not tests and not suites:
        return _result("jest", UNDETERMINED)
    c = _counts(tests[-1] if tests else "", _JEST_WORDS)
    s = _counts(suites[-1] if suites else "", _JEST_WORDS)
    passed, failed, skipped = c.get("passed", 0), c.get("failed", 0), c.get("skipped", 0)
    line = "Tests: " + tests[-1] if tests else "Test Suites: " + suites[-1]
    if nonzero and (failed or s.get("failed")):
        return _result("jest", FAIL, passed, failed or s.get("failed", 0), skipped, line)
    if zero and passed and not failed and not s.get("failed"):
        return _result("jest", PASS, passed, 0, skipped, line)
    return _result("jest", UNDETERMINED, passed, failed, skipped, line)


# vitest: " Test Files  1 failed | 2 passed (3)" / "      Tests  1 failed | 9 passed (10)"
# (no colon, a "(total)" at the end); "No test files found, exiting with code 1".
_VI_FILES_RE = re.compile(r"^\s*Test Files\s+(.+?)\s*\((\d+)\)\s*$", re.M)
_VI_TESTS_RE = re.compile(r"^\s*Tests\s+(.+?)\s*\((\d+)\)\s*$", re.M)
_VI_NONE_RE = re.compile(r"No test (?:files|suite) found", re.I)
_VI_VERSION_RE = re.compile(r"^\s*RUN\s+v(\d+(?:\.\d+)+)", re.M)


def _vitest(out, zero, nonzero):
    version = (_VI_VERSION_RE.findall(out) or [None])[-1]
    tests = _VI_TESTS_RE.findall(out)
    files = _VI_FILES_RE.findall(out)
    if not tests and not files:
        if _VI_NONE_RE.search(out):
            return _result("vitest", NO_TESTS, line="no test files found", version=version)
        return _result("vitest", UNDETERMINED, version=version)
    c = _counts(tests[-1][0] if tests else "", _JEST_WORDS)
    f = _counts(files[-1][0] if files else "", _JEST_WORDS)
    passed, failed, skipped = c.get("passed", 0), c.get("failed", 0), c.get("skipped", 0)
    line = ("Tests " + tests[-1][0]) if tests else ("Test Files " + files[-1][0])
    if nonzero and (failed or f.get("failed")):
        return _result("vitest", FAIL, passed, failed or f.get("failed", 0), skipped, line, version)
    if zero and passed and not failed and not f.get("failed"):
        return _result("vitest", PASS, passed, 0, skipped, line, version)
    return _result("vitest", UNDETERMINED, passed, failed, skipped, line, version)


# mocha: "  5 passing (12ms)", "  2 failing", "  1 pending".
_MO_PASS_RE = re.compile(r"^\s*(\d+) passing\b", re.M)
_MO_FAIL_RE = re.compile(r"^\s*(\d+) failing\s*$", re.M)
_MO_PEND_RE = re.compile(r"^\s*(\d+) pending\s*$", re.M)


def _mocha(out, zero, nonzero):
    p, f, s = _MO_PASS_RE.findall(out), _MO_FAIL_RE.findall(out), _MO_PEND_RE.findall(out)
    if not p and not f:
        return _result("mocha", UNDETERMINED)
    passed = int(p[-1]) if p else 0
    failed = int(f[-1]) if f else 0
    skipped = int(s[-1]) if s else 0
    line = "%d passing" % passed + (", %d failing" % failed if failed else "") + \
        (", %d pending" % skipped if skipped else "")
    if nonzero and failed:
        return _result("mocha", FAIL, passed, failed, skipped, line)
    if zero and passed and not failed:
        return _result("mocha", PASS, passed, 0, skipped, line)
    if zero and not passed and not failed:
        return _result("mocha", NO_TESTS, 0, 0, skipped, line)
    return _result("mocha", UNDETERMINED, passed, failed, skipped, line)


# cargo test: one "test result: ok. 3 passed; 0 failed; 1 ignored; ..." per
# test binary (doc-tests included); a compile error is "error[E0425]: ..." /
# "error: could not compile `x`".
_CARGO_RESULT_RE = re.compile(r"^test result: (ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored",
                              re.M)
_CARGO_COMPILE_RE = re.compile(r"^error(?:\[E\d+\])?: |^error: could not compile", re.M)


def _cargo(out, zero, nonzero):
    rows = _CARGO_RESULT_RE.findall(out)
    compile_errors = [m for m in _CARGO_COMPILE_RE.findall(out)]
    if not rows:
        if nonzero and compile_errors and re.search(r"could not compile", out):
            return _result("cargo test", FAIL, 0, len(compile_errors), 0,
                           "could not compile (%d errors)" % len(compile_errors))
        return _result("cargo test", UNDETERMINED)
    passed = sum(int(r[1]) for r in rows)
    failed = sum(int(r[2]) for r in rows)
    skipped = sum(int(r[3]) for r in rows)
    bad = any(r[0] == "FAILED" for r in rows)
    line = "%d passed; %d failed; %d ignored" % (passed, failed, skipped)
    if nonzero and (bad or failed):
        return _result("cargo test", FAIL, passed, failed or 1, skipped, line)
    if zero and not bad and not failed:
        if passed:
            return _result("cargo test", PASS, passed, 0, skipped, line)
        return _result("cargo test", NO_TESTS, 0, 0, skipped, line)
    return _result("cargo test", UNDETERMINED, passed, failed, skipped, line)


# go test: "ok  \texample.com/pkg\t0.004s", "FAIL\texample.com/pkg\t0.01s",
# "--- FAIL: TestX (0.00s)", "?   \texample.com/pkg\t[no test files]",
# "FAIL\texample.com/pkg [build failed]".
_GO_OK_RE = re.compile(r"^ok\s+\S+\s+(?:[\d.]+s|\(cached\))(.*)$", re.M)
_GO_FAILPKG_RE = re.compile(r"^FAIL\s+\S+(?:\s+[\d.]+s|\s+\[(?:build|setup) failed\])", re.M)
_GO_NOFILES_RE = re.compile(r"^\?\s+\S+\s+\[no test files\]", re.M)


def _gotest(out, zero, nonzero):
    oks = _GO_OK_RE.findall(out)
    fails = _GO_FAILPKG_RE.findall(out)
    t_fail = len(re.findall(r"^\s*--- FAIL: ", out, re.M))
    t_pass = len(re.findall(r"^\s*--- PASS: ", out, re.M))
    t_skip = len(re.findall(r"^\s*--- SKIP: ", out, re.M))
    nofiles = _GO_NOFILES_RE.findall(out)
    real_oks = [o for o in oks if "[no tests to run]" not in o]
    if nonzero and (fails or t_fail):
        return _result("go test", FAIL, t_pass, t_fail or len(fails), t_skip,
                       "%d package(s) FAIL" % max(1, len(fails)))
    if zero and real_oks and not fails and not t_fail:
        if t_pass:
            return _result("go test", PASS, t_pass, 0, t_skip, "%d passed" % t_pass)
        return _result("go test", PASS, len(real_oks), 0, t_skip,
                       "%d package(s) ok" % len(real_oks))
    if zero and not real_oks and (nofiles or oks) and not fails:
        return _result("go test", NO_TESTS, line="no test files")
    return _result("go test", UNDETERMINED, t_pass, t_fail, t_skip)


# node --test: TAP "# tests 5 / # pass 5 / # fail 0" or the spec reporter's
# "ℹ tests 5 / ℹ pass 5 / ℹ fail 0".
_NODE_RE = re.compile(r"^(?:#|\u2139)\s+(tests|pass|fail|skipped|todo)\s+(\d+)\s*$", re.M)


def _nodetest(out, zero, nonzero):
    c = {}
    for k, v in _NODE_RE.findall(out):
        c[k] = int(v)
    if "tests" not in c:
        return _result("node --test", UNDETERMINED)
    passed, failed = c.get("pass", 0), c.get("fail", 0)
    skipped = c.get("skipped", 0) + c.get("todo", 0)
    line = "%d tests, %d pass, %d fail" % (c["tests"], passed, failed)
    if nonzero and failed:
        return _result("node --test", FAIL, passed, failed, skipped, line)
    if zero and passed and not failed:
        return _result("node --test", PASS, passed, 0, skipped, line)
    if zero and c["tests"] == 0:
        return _result("node --test", NO_TESTS, line=line)
    return _result("node --test", UNDETERMINED, passed, failed, skipped, line)


# bun test: " 12 pass", " 1 fail", "Ran 13 tests across 2 files. [40.00ms]".
_BUN_RE = re.compile(r"^\s*(\d+) (pass|fail|skip|todo)\s*$", re.M)
_BUN_RAN_RE = re.compile(r"^Ran (\d+) tests? across \d+ files?", re.M)


def _buntest(out, zero, nonzero):
    c = {}
    for n, k in _BUN_RE.findall(out):
        c[k] = int(n)
    ran = _BUN_RAN_RE.findall(out)
    if not ran:
        if re.search(r"^\s*0 test files matching|No tests found", out, re.M):
            return _result("bun test", NO_TESTS, line="no tests found")
        return _result("bun test", UNDETERMINED)
    passed, failed = c.get("pass", 0), c.get("fail", 0)
    skipped = c.get("skip", 0) + c.get("todo", 0)
    line = "%d pass, %d fail" % (passed, failed)
    if nonzero and failed:
        return _result("bun test", FAIL, passed, failed, skipped, line)
    if zero and passed and not failed:
        return _result("bun test", PASS, passed, 0, skipped, line)
    if int(ran[-1]) == 0:
        return _result("bun test", NO_TESTS, line=line)
    return _result("bun test", UNDETERMINED, passed, failed, skipped, line)


# tsc: "src/a.ts(3,7): error TS2322: ..." (plain) / "src/a.ts:3:7 - error
# TS2322: ..." (pretty) / "error TS5058: ..." (global), "Found 2 errors in 1 file."
_TSC_ERR_RE = re.compile(r"^(?:.+\(\d+,\d+\): |.+:\d+:\d+ - )?error TS\d+:", re.M)
_TSC_FOUND_RE = re.compile(r"^Found (\d+) errors?\b", re.M)


def _tsc_errors(out):
    return len(_TSC_ERR_RE.findall(out))


def _tsc(out, zero, nonzero):
    n = _tsc_errors(out)
    found = _TSC_FOUND_RE.findall(out)
    if nonzero and n:
        count = int(found[-1]) if found else n
        return _result("tsc", FAIL, 0, count, 0, "tsc: %d type error%s" % (count, "s" * (count != 1)),
                       kind="build")
    if zero and not n and not found:
        return _result("tsc", PASS, 0, 0, 0, "tsc: no type errors", kind="build")
    return _result("tsc", UNDETERMINED, 0, n, 0, kind="build")


# Bundlers, each in its own words (never a generic word):
#   vite  "✓ built in 1.24s"            / "error during build:"
#   next  "✓ Compiled successfully"      / "Failed to compile." "Build error occurred"
#   webpack "webpack 5.9 compiled successfully in 812 ms" / "compiled with 2 errors"
_BUNDLERS = (
    ("vite build", re.compile(r"^\s*(?:\u2713\s+)?built in [\d.]+\s*m?s\s*$", re.M),
     re.compile(r"^error during build:|^\[vite\]: Rollup failed|^\s*\u2717 Build failed", re.M)),
    ("next build", re.compile(r"^\s*(?:\u2713\s+)?Compiled successfully\b", re.M),
     re.compile(r"^\s*Failed to compile\.|Build error occurred|^Type error: ", re.M)),
    ("webpack", re.compile(r"\bcompiled successfully in \d+(?:\.\d+)?\s*m?s\b", re.M),
     re.compile(r"\bcompiled with \d+ errors?\b|^ERROR in ", re.M)),
)


def _bundler(tool, out, zero, nonzero):
    for name, ok_re, err_re in _BUNDLERS:
        if name != tool:
            continue
        errs = len(err_re.findall(out))
        if nonzero and errs:
            return _result(tool, FAIL, 0, errs, 0, "%s: build failed" % tool, kind="build")
        # A type error printed by the build (a TS plugin) still blocks a pass,
        # but it is the type checker's failure to report, not the bundler's.
        if zero and ok_re.search(out) and not errs and not _tsc_errors(out):
            return _result(tool, PASS, 0, 0, 0, "%s: built" % tool, kind="build")
        return _result(tool, UNDETERMINED, kind="build")
    return _result(tool, UNDETERMINED, kind="build")


_ADAPTERS = {
    "pytest": _pytest, "unittest": _unittest, "jest": _jest, "vitest": _vitest,
    "mocha": _mocha, "cargo test": _cargo, "go test": _gotest,
    "node --test": _nodetest, "bun test": _buntest, "tsc": _tsc,
}
# The order a wrapper's output is sniffed in when its script is not echoed.
_TEST_SNIFF = ("vitest", "jest", "mocha", "pytest", "node --test", "bun test",
               "unittest", "go test", "cargo test")


def _adapt(tool, out, zero, nonzero):
    fn = _ADAPTERS.get(tool)
    if fn is not None:
        r = fn(out, zero, nonzero)
    else:
        r = _bundler(tool, out, zero, nonzero)
    r["kind"] = r.get("kind") or ("build" if tool in ("tsc", "vite build", "next build",
                                                       "webpack") else "test")
    return r


# npm/pnpm echo the script: "> app@1.0.0 test" then "> vitest run" (pnpm adds
# the folder after the name); yarn v1 echoes "$ vitest run".
_NPM_ECHO_RE = re.compile(r"^> \S+@\S+ ([\w:.-]+)(?: .*)?\n> (.+)$", re.M)
_YARN_ECHO_RE = re.compile(r"^\$ (.+)$", re.M)
_NPM_LIFECYCLE_FAIL_RE = re.compile(
    r"^npm (?:ERR!|error) (?:Test failed|Lifecycle script `[^`]+` failed|code ELIFECYCLE)"
    r"|^\s*ELIFECYCLE\s+(?:Test|Command) failed|^error Command failed with exit code \d+",
    re.M)
_NO_TEST_SPECIFIED_RE = re.compile(r"Error: no test specified", re.I)


def _wrapper(desc, out, zero, nonzero):
    label, kind = desc["tool"], desc["kind"]
    inner = None
    m = _NPM_ECHO_RE.findall(out)
    if m:
        inner = m[-1][1]
    else:
        y = _YARN_ECHO_RE.findall(out)
        if y:
            inner = y[-1]
    if inner and _NO_TEST_SPECIFIED_RE.search(inner + "\n" + out):
        return _result(label, NO_TESTS, line="no test script", kind=kind)
    tools = []
    if inner:
        tools = [d["tool"] for _, d in _check_segments(inner)[0] if not d.get("wrapper")]
    if not tools:
        # Not echoed (yarn berry, bun run): the tool is recognised by its
        # OWN summary or not at all.
        sniff = _TEST_SNIFF if kind == "test" else ()
        for t in sniff:
            r = _adapt(t, out, zero, nonzero)
            if r["verdict"] != UNDETERMINED or r["line"]:
                tools = [t]
                break
        if not tools and kind == "build":
            for name, ok_re, err_re in _BUNDLERS:
                if ok_re.search(out) or err_re.search(out):
                    tools = [name]
                    break
            if not tools and _tsc_errors(out):
                tools = ["tsc"]
    if tools:
        r = _combine([_adapt(t, out, zero, nonzero) for t in tools])
        r["tool"] = "%s > %s" % (label, " + ".join(dict.fromkeys(tools)))
        return r
    if nonzero and _NPM_LIFECYCLE_FAIL_RE.search(out):
        return _result(label, FAIL, line="%s: the script failed" % label, kind=kind)
    return _result(label, UNDETERMINED, kind=kind)


def _combine(results):
    """One verdict for a command that ran several checks on one exit code."""
    if len(results) == 1:
        return results[0]
    verdicts = [r["verdict"] for r in results]
    if FAIL in verdicts:
        v = FAIL
    elif all(x == PASS for x in verdicts):
        v = PASS
    elif all(x == NO_TESTS for x in verdicts):
        v = NO_TESTS
    else:
        v = UNDETERMINED
    return {"tool": " + ".join(r["tool"] for r in results), "verdict": v,
            "passed": sum(r["passed"] for r in results),
            "failed": sum(r["failed"] for r in results),
            "skipped": sum(r["skipped"] for r in results),
            "line": "; ".join(r["line"] for r in results if r["line"])[:200],
            "version": next((r["version"] for r in results if r.get("version")), None),
            "kind": "build" if all(r.get("kind") == "build" for r in results) else "test"}


def classify(command, exit_code, output, is_error=None):
    """{"tool", "verdict", "passed", "failed", "skipped", "line", "version",
    "kind"} for one observed command run. See the module docstring for the
    rules; this never raises."""
    try:
        return _classify(command, exit_code, output, is_error)
    except Exception:                                            # noqa: BLE001
        return _result(None, UNDETERMINED, line="could not read the result")


def _classify(command, exit_code, output, is_error):
    found, segs = _check_segments(command)
    if not found:
        return _result(None, UNDETERMINED, line="not a recognised test or build command")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        try:
            exit_code = int(exit_code) if exit_code is not None and str(exit_code).strip() \
                .lstrip("-").isdigit() else None
        except (TypeError, ValueError):
            exit_code = None
    # WHOSE EXIT CODE IS IT? Only the check's own when nothing after it can
    # replace it: `pytest | tail`, `pytest; echo done`, `pytest || true` all
    # end with someone else's status. `cd x && pytest && echo ok` keeps it.
    last = found[-1][0]
    masked = any(op not in ("&&", None) for _seg, op in segs[last:])
    if masked:
        exit_code = None
    nonzero = (exit_code is not None and exit_code != 0) or \
        (exit_code is None and is_error is True and not masked)
    zero = exit_code == 0 and is_error is not True
    out = clean_output(tail(output))
    results = []
    for _i, d in found:
        if d.get("wrapper"):
            results.append(_wrapper(d, out, zero, nonzero))
        else:
            results.append(_adapt(d["tool"], out, zero, nonzero))
    r = _combine(results)
    if exit_code is None and r["verdict"] == PASS:
        r["verdict"] = UNDETERMINED                 # never PASS without exit 0
    if exit_code == 0 and r["verdict"] == FAIL:
        r["verdict"] = UNDETERMINED                 # never FAIL on exit 0
    return r


def from_event(ev):
    """A parser's tool_result event -> one evidence row: the classification
    plus what was observed (command, exit code, is_error, times). The output
    itself is not kept -- the verdict, counts and summary line are."""
    ev = ev if isinstance(ev, dict) else {}
    r = classify(ev.get("command"), ev.get("exit_code"), ev.get("output_tail"),
                 ev.get("is_error"))
    r.update({"command": str(ev.get("command") or "")[:2000],
              "exit_code": ev.get("exit_code") if isinstance(ev.get("exit_code"), int)
              and not isinstance(ev.get("exit_code"), bool) else None,
              "is_error": ev.get("is_error") if isinstance(ev.get("is_error"), bool) else None,
              "started_at": ev.get("started_at"), "ended_at": ev.get("ended_at")})
    return r


# --------------------------------------------------------------------------- #
# Reading a list of observed results
# --------------------------------------------------------------------------- #

def outstanding_failures(results):
    """The FAILs nothing later re-ran green: the latest run of each check that
    failed, unless a later PASS covers it (same check, or the same tool over
    the whole suite / an enclosing folder). Oldest first."""
    out = []
    items = [r for r in (results or ()) if isinstance(r, dict) and r.get("command")]
    for i, r in enumerate(items):
        if r.get("verdict") != FAIL:
            continue
        later = items[i + 1:]
        if any(x.get("verdict") == PASS and covers(x["command"], r["command"]) for x in later):
            continue
        key = command_key(r["command"])
        if any(x.get("verdict") == FAIL and command_key(x["command"]) == key for x in later):
            continue                        # the later failure of it is the one reported
        out.append(r)
    return out


def passes(results):
    return [r for r in (results or ()) if isinstance(r, dict) and r.get("verdict") == PASS]


def label(results):
    """A short, honest label for what was OBSERVED, or "" when nothing was:
    "2 failed (observed)", "12 passed (observed)", "tsc: no type errors
    (observed)", "no tests ran (observed)"."""
    fails = outstanding_failures(results)
    if fails:
        f = fails[-1]
        n = f.get("failed") or 0
        if f.get("kind") == "build" or not n:
            what = f.get("line") or "%s failed" % (f.get("tool") or "check")
        else:
            what = "%d failed" % n
        return "%s (observed)" % what
    ok = passes(results)
    if ok:
        tests = [r for r in ok if r.get("kind") != "build"]
        builds = [r for r in ok if r.get("kind") == "build"]
        parts = []
        if tests:
            r = tests[-1]
            parts.append("%d passed" % r["passed"] if r.get("passed") else (r.get("line") or "passed"))
        if builds:
            parts.append(builds[-1].get("line") or "build ok")
        return "%s (observed)" % "; ".join(parts)
    if any(isinstance(r, dict) and r.get("verdict") == NO_TESTS for r in (results or ())):
        return "no tests ran (observed)"
    return ""


# A final message that says tests or a build passed. Only ever used to say a
# claim went UNCHECKED -- never to call anything verified.
CLAIM_RE = re.compile(
    r"\b(?:all\s+)?(?:\d+\s+)?(?:unit\s+|e2e\s+|integration\s+)?tests?\s+(?:now\s+|all\s+)?"
    r"(?:pass(?:ed|es|ing)?|are\s+(?:passing|green)|succeed(?:ed)?)\b"
    r"|\b\d+\s+passed\b|\b\d+\s+passing\b"
    r"|\bbuild\s+(?:now\s+)?(?:succeed(?:s|ed)|pass(?:es|ed)|is\s+green|is\s+clean|ok\b|successful)"
    r"|\bcompiles?\s+(?:cleanly|without\s+errors)"
    r"|\b(?:tsc|type[- ]?check(?:ing|s)?)\b[^.\n]{0,40}\b(?:clean|pass(?:es|ed)?|no\s+errors)\b",
    re.I)


def claims_checks_passed(text):
    return bool(CLAIM_RE.search(text or ""))
