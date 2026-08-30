"""Calvoun Free LLM Hub -- agentic chat: run the user's OWN Claude Code / Codex
subscription as a REAL CODING AGENT (full tool access: file read/write/edit/
bash) against a project folder the user picks, with full permissions ON BY
DEFAULT and a Stop button that can interrupt a turn mid-flight.

ADDITIVE to the existing `_SUB_PROVIDERS` / `_sub_run` / `_subscription_chat`
system in app.py -- that path is a one-shot, NO-tool-access, text-only
orchestration fallback and is completely untouched by this module. This is a
sibling capability with a different contract:

  * one agentic session == one (cli, project_dir) pair, explicitly chosen by
    the user when the session is started -- there is no default folder.
  * each turn (one user message) is exactly ONE subprocess invocation. Turn 1
    has no session yet; turn 2+ passes --resume with the CLI-native session id
    captured from turn 1's JSON response. This is NOT a long-lived process --
    only the CURRENTLY-RUNNING turn's subprocess exists at any moment, which is
    what makes Stop simple and safe (terminate whichever subprocess is
    in-flight for that session, if any).
  * full-tool-access, no-confirmation flags are ALWAYS included --
    "--dangerously-skip-permissions" for Claude. This ONLY ever runs once the
    module-level master flag (config flag "agentic_chat_enabled", default
    OFF) is on AND the user has explicitly started a session.

Codex IS enabled (and is the default), after live verification on codex-cli
0.144.5 (2026-07-17): `codex exec --json` runs with full tool access under
`--dangerously-bypass-approvals-and-sandbox`, and -- the combination the earlier
scoping could not confirm from docs alone -- that bypass flag DOES survive
`codex exec resume <thread_id>` for turn 2+ (verified end to end: a resumed turn
ran shell commands with no approval hang). Writes land in the subprocess cwd
(this module spawns with cwd=project_dir), so resume needs no -C. Codex's prompt
is positional and it has no --append-system-prompt, so the optional test/vision
notice is prepended into the prompt text. See _build_argv_codex().

Prompt delivery: the message text travels as a POSITIONAL argv argument, not
stdin. This diverges from `_sub_run()` in app.py on purpose: every real,
confirmed-working example of `claude -p ... --resume <id>` in the current
official docs passes the prompt as a positional string, and neither of the two
official docs pages fetched for this feature confirm (or deny) that resume
mode still honors a piped-stdin prompt. Rather than guess, this always uses the
documented, confirmed shape. To stay safely under cmd.exe's ~8191-char command
line ceiling (this hub's Windows launcher wraps an npm .cmd shim in
`cmd.exe /c ...`, exactly like `_sub_launcher()` in app.py), the message body
is capped well below that limit -- see _MAX_MESSAGE_CHARS. Trust model:
identical to the existing _SUB_* code -- this hub trusts its own local user's
input (the prompt, like project_dir, comes from the same local operator
running the hub), so no further escaping/validation is attempted beyond that
length cap. Stdin is closed (subprocess.DEVNULL) on every invocation: with
--dangerously-skip-permissions there should be no interactive read to service,
so closing it outright turns any unexpected read into an immediate EOF instead
of a silent hang, rather than leaving stdin open and unused.

Kill safety: killing just the top PID can leave orphaned Bash/MCP child
processes behind (an open, unresolved risk called out directly in Claude
Code's own GitHub issue tracker, e.g. #76306, #76942, #77783). So Stop always
targets the WHOLE process tree -- `taskkill /T` on Windows, a fresh POSIX
process group (os.setsid) signaled via os.killpg elsewhere -- not just the
immediate child, escalating from a soft signal to a hard kill after a short
grace period.

In-memory only (module-level dict), deliberately NOT persisted to disk: a
session with a live subprocess handle makes no sense to survive a hub restart,
unlike the JSON-file-backed usage/image history.

Claude Code is the ONLY currently-working backend (see _SUPPORT) -- so it is
the default `cli` wherever a default is offered: start_session()'s own default
when a caller omits `cli`, AND the value the dashboard's CLI picker should
preselect (default_cli() exposes this for the frontend).

Best-model injection: every invocation (turn 1 and every --resume turn --
permission-mode flags are already known not to persist across --resume, and
--model is treated the same way defensively) passes `--model opus` explicitly.
See _MODEL_ALIAS below for why "opus", not "fable", was chosen.

Binary-identity safety check: this machine (and potentially others) may have a
local CLI-wrapper shim sitting earlier on PATH than the real Claude Code
binary, silently rerouting calls through a different backend with no signal to
the caller. Since this feature explicitly promises "this runs your real Claude
Code subscription", the FIRST turn of every new session runs the resolved
binary with `--version` and confirms the output contains the literal substring
"Claude Code" (confirmed real shape: "2.1.212 (Claude Code)") before trusting
it -- see _verify_claude_binary_identity(). Codex is skipped: its local shim on
this machine is a confirmed-safe passthrough for any argument-bearing
invocation, and this check is specifically about the claude-only GPT-proxy risk
just discovered.

Test-verification + vision-gap system-prompt injection: see
_system_prompt_addition() / _TEST_VERIFICATION_SNIPPET / _VISION_GAP_SNIPPET
below. Two independent, additive pieces of --append-system-prompt text (a
real, confirmed CLI-usable flag -- see the comment above _build_argv()):
(1) when the "agentic_test_verification_enabled" config flag is on, tells the
agent that using its own tool access (e.g. Playwright, if installed) to
verify a change is expected this session -- this flag changes NOTHING about
the CLI invocation itself (Claude Code already has bash/tool access), it only
gates this notice; (2) when vision_status.status() reports no vision-capable
model connected, tells the agent to mention that gap honestly if relevant to
what the user asked. Both are additive-only text; neither is fabricated when
its condition doesn't hold.

Pure stdlib + this hub's own `config`/`vision_status` modules: json, os,
shutil, signal, subprocess, threading, time, uuid.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid

import agentic_history
import config
import craft
import vision_status
import workspace

_log = logging.getLogger("free-llm-hub")

# --------------------------------------------------------------------------- #
# Config / constants
# --------------------------------------------------------------------------- #

_MASTER_FLAG = "agentic_chat_enabled"          # config flag, default OFF

# Test-and-verification opt-in -- a SEPARATE, GLOBAL (not per-session) config
# flag. Turning this on does not change the CLI invocation's tool access at
# all (Claude Code already has bash, which can already run Playwright if it's
# installed) -- it only controls whether _system_prompt_addition() below tells
# the agent that testing/verifying its own work this session is expected.
_TEST_VERIFICATION_FLAG = "agentic_test_verification_enabled"

_CLI_BIN = {"claude": "claude", "codex": "codex", "opencode": "opencode"}

# A session id reaches agentic_history, which turns it into a FILENAME. Reusing
# a caller-supplied id (resume_session) is therefore only safe against the same
# whitelist that module applies -- anything else could write outside its folder.
_SAFE_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# Which backend integrates BEST with this hub, as opposed to which is default.
# Claude Code has --append-system-prompt (a real system channel) and a clean
# --resume. Codex has neither, which is why its notice has to be inlined into
# the prompt text -- the ordering bug that made the agent answer our notice
# instead of the user's task. Both work; this only drives the "recommended"
# label in the picker.
_RECOMMENDED_CLI = "claude"

# Codex is the default agentic backend (the user's explicit choice) -- verified
# on codex-cli 0.144.5 that `codex exec --json` runs with full tool access and
# that --dangerously-bypass-approvals-and-sandbox survives `codex exec resume`.
# Claude Code remains fully supported and selectable. This is the API default
# when a caller omits `cli`, AND the value the dashboard's CLI picker preselects
# (read via default_cli()).
_DEFAULT_CLI = "codex"

# Maps our cli_id ("claude"/"codex") -> the subscription-provider id
# (_SUB_PROVIDERS key in app.py) that owns the isolated-install mechanism this
# feature reuses for one-click install. Duplicated here (not imported) for the
# same import-cycle-avoidance reason as _agentic_env()/_launcher() below --
# keep in sync with _SUB_PROVIDERS's own "cli_id" fields if that table changes.
_INSTALL_PROVIDER_ID = {"claude": "sub-claude", "codex": "sub-codex"}

# Facts from the confirmed research pass (see module docstring for the codex
# reasoning). `supported=False` means start_session() refuses cleanly with the
# given reason instead of attempting an unverified flag combination.
_SUPPORT = {
    "claude": (True, None),
    # Codex enabled after live verification on codex-cli 0.144.5 (2026-07-17):
    # `codex exec --json` streams JSONL events and runs shell/file tools under
    # --dangerously-bypass-approvals-and-sandbox, and that bypass flag DOES
    # survive `codex exec resume <thread_id>` for turn 2+ (empirically confirmed
    # end to end -- the resumed turn executed a command with no approval hang),
    # which the earlier scoping could not confirm from docs. Writes go to the
    # subprocess cwd (we spawn with cwd=project_dir), so resume needs no -C.
    "codex": (True, None),
    # OpenCode enabled after live verification on opencode-ai 1.18.11
    # (2026-08-01): `opencode run --format json` emits one JSON event per line
    # (step_start / tool_use / text / step_finish), every event carries
    # sessionID, and `--session <id>` continues that session. Writes go to the
    # subprocess cwd, same as codex.
    #
    # ONE HARD REQUIREMENT, found the slow way: it must be spawned with stdin
    # CLOSED. With an open pipe it loads its config, logs "init", and then
    # blocks forever -- measured at 0 bytes of output after 200s, twice, with
    # no error. The same invocation with stdin at /dev/null answered in under
    # a second. send_message/send_message_stream already pass
    # stdin=subprocess.DEVNULL for every CLI, which is what makes this safe.
    "opencode": (True, None),
}

# Each agentic turn can run real tool use (file edits, shell commands), so this
# is deliberately much longer than app.py's one-shot _SUB_TIMEOUT (120s).
# Configurable, mirroring the PORT env-var convention already used in app.py.
# Was 600 (10 min). Measured live, on THIS hub, for a TRIVIAL one-file-write
# turn on free-tier routing: codex took ~5 minutes. A real ask -- "build me a
# site", many tool calls, many model round trips within one turn -- can need
# far longer than that on a free model, and 10 minutes was routinely not
# enough. Raised to something that actually matches observed latency; still
# overridable via AGENTIC_CHAT_TIMEOUT for anyone who wants it tighter.
_TURN_TIMEOUT = int(os.environ.get("AGENTIC_CHAT_TIMEOUT", "1800") or "1800")

# Keep the prompt safely under cmd.exe's ~8191-char command-line ceiling once
# wrapped in `cmd.exe /c <shim.cmd> ...` on Windows (this hub's ONLY launch path
# for an npm-installed CLI there) -- see module docstring. One flat constant,
# not OS-specific, so behavior is uniform and predictable everywhere.
#
# Verified (not guessed) against the OPTIONAL --append-system-prompt addition
# below (_system_prompt_addition()): worst case, EVERY other argv piece at its
# longest (a 260-char shim path, a 36-char --resume uuid, every flag, both
# system-prompt snippets concatenated) plus this 6000-char cap totals ~7030
# chars -- a >1150-char buffer under the ~8191 ceiling. If either snippet's
# text grows meaningfully, recheck this arithmetic rather than assume it still
# fits.
#
# RECHECKED 2026-08-01, exactly as that last line asks. The craft briefs had to
# start reaching agent sessions (app.py only injects them into requests that go
# THROUGH the hub, and an agent session never does -- see write_task_brief).
# They are ~9,000 chars: inlining them would have put the worst case near 15,700
# and broken every turn on Windows. So they go in a FILE in the project and the
# prompt spends ~200 chars pointing at it. Measured after the change: 931 argv
# chars for codex, 896 for claude, both with a full brief in play.
# 6000 was 215 chars TOO HIGH, and it shipped. MEASURED 2026-08-08 on the
# TURN-1 path (no native_session_id yet, so _system_prompt_addition really is
# sent as argv) with every optional block live -- vision-gap notice firing,
# test-verification on, a brief matched, message at the cap:
#     claude 8406 / codex 8390 / opencode 8329   vs the ~8191 ceiling
# The existing guard test never saw it: it sets native_session_id, so it only
# ever measured the RESUME path (6273), where the addition is not sent at all.
# 5600 restores real headroom on the worst case; the resume path, which is
# every turn after the first, was never close.
_MAX_MESSAGE_CHARS = 5600

# SIGTERM (or the Windows "soft" taskkill attempt) -> SIGKILL/"hard" taskkill
# escalation grace period, seconds.
_KILL_GRACE = 5

# Substrings that mean "the subscription session itself is the problem" (e.g.
# it expired mid-agentic-session) -> report 403, not a generic 502, so the
# client can tell "sign back in" apart from "the run genuinely failed".
# Mirrors app.py's own _SUB_AUTH_ERR list (duplicated, not imported -- see the
# module-level note on avoiding a circular import with app.py).
_AUTH_ERR_SUBSTRINGS = ("not logged in", "not authenticated", "unauthorized", "401",
                        "please run /login", "please login", "please run `claude login`",
                        "run claude login", "invalid api key", "no credentials",
                        "authentication_error", "session expired", "oauth")


def _looks_like_auth_error(detail) -> bool:
    low = (detail or "").lower()
    return any(s in low for s in _AUTH_ERR_SUBSTRINGS)


# A session's --resume/--session id is only good against the config directory
# it was minted under. Isolation (added the same day as this table) gave every
# CLI a FRESH config directory with no history in it -- so a session id from
# before that change, or from an even earlier reset, is unknown to it. Each CLI
# reports that with its own wording; measured directly, one command at a time:
#
#   claude    --resume <id>            "No conversation found with session ID: <id>"
#   codex     exec resume <id>         "no rollout found for thread id <id> (code -32600)"
#   opencode  --session <id>           "Session not found"
#
# Losing the user's message to a confusing error over something they did not
# cause is worse than silently starting the conversation over, so this is a
# regex table keyed by cli_id, used to trigger ONE transparent retry (see
# send_message / send_message_stream) rather than surfacing the error at all.
_STALE_RESUME_PATTERNS = {
    "claude": re.compile(r"no conversation found with session id", re.I),
    "codex": re.compile(r"no rollout found for thread id", re.I),
    "opencode": re.compile(r"session not found", re.I),
}


def _is_stale_resume_error(cli_id, detail) -> bool:
    pat = _STALE_RESUME_PATTERNS.get(cli_id)
    return bool(pat and detail and pat.search(str(detail)))


# How to sign in to the hub's OWN copy of each CLI. Isolation means the copy the
# agent chat drives has its own config directory, so it starts out logged into
# nothing -- and "not logged in" is a baffling message when the CLI in your own
# terminal is clearly signed in. Say which copy, and give the exact command.
_ISOLATED_LOGIN_CMD = {
    "claude": "claude  (it walks you through login on first launch)",
    "codex": "codex login",
    "opencode": "opencode auth login",
}


def _auth_help(cli_id: str) -> str:
    """One line telling the user how to authenticate the isolated copy."""
    var = _ISOLATED_CONFIG_ENV.get(cli_id)
    cmd = _ISOLATED_LOGIN_CMD.get(cli_id)
    if not (var and cmd):
        return ""
    path = _isolated_config_dir(cli_id)
    if os.name == "nt":
        shell = "$env:%s = '%s'; %s" % (var, path, cmd)
    else:
        shell = "%s='%s' %s" % (var, path, cmd)
    return (" This session runs the hub's OWN isolated copy of %s, which keeps "
            "its settings in %s and is signed in separately from the %s you use "
            "by hand. Sign it in once with:  %s" % (cli_id, path, cli_id, shell))


def _master_on() -> bool:
    return bool(config.get_flag(_MASTER_FLAG, False))


def master_enabled() -> bool:
    """Public read of the master opt-in flag, for the dashboard settings panel."""
    return _master_on()


def set_master_enabled(value: bool) -> None:
    config.set_flag(_MASTER_FLAG, bool(value))


def test_verification_enabled() -> bool:
    """Public read of the test-verification opt-in, for the dashboard settings
    panel and for _system_prompt_addition() below."""
    return bool(config.get_flag(_TEST_VERIFICATION_FLAG, False))


def set_test_verification_enabled(value: bool) -> None:
    config.set_flag(_TEST_VERIFICATION_FLAG, bool(value))


def cli_support() -> dict:
    """{'claude': {'supported': bool, 'reason': str|None, 'installed': bool},
    'codex': {...}} -- for the dashboard to show which CLI(s) this feature
    actually offers, and whether each is already installed (so the picker can
    offer a one-click Install button proactively, before the user even tries to
    start a session). `installed` is a plain shutil.which() probe -- cheap,
    read-only, never raises."""
    out = {}
    for cid, (ok, reason) in _SUPPORT.items():
        try:
            iso = _isolated_bin(cid)
            installed = bool(iso or shutil.which(_CLI_BIN[cid]))
        except Exception:
            iso, installed = None, False
        out[cid] = {"supported": ok, "reason": reason, "installed": installed,
                    # Whether the copy we would actually RUN is the hub's own,
                    # so the picker can say so instead of leaving the user to
                    # wonder which install a session is about to touch.
                    "isolated": bool(iso),
                    "recommended": cid == _RECOMMENDED_CLI,
                    # Isolation on purpose means a SEPARATE, initially-empty
                    # credential store from the CLI the user already uses by
                    # hand -- so "installed" is not "ready". Checked directly
                    # against the isolated config dir, never the real one, so
                    # this can never read the user's own login as a green
                    # light for the hub's copy.
                    "signed_in": (not bool(iso)) or _isolated_signed_in(cid)}
    return out


# The file that appears in a CLI's config dir once login actually succeeds.
# opencode has no entry: it is never a subscription for the hub's purposes --
# its isolated copy is seeded with the hub's own free models and needs no
# login (see _seed_opencode_config).
_ISOLATED_CREDENTIAL_FILE = {"claude": ".credentials.json", "codex": "auth.json"}


def _isolated_signed_in(cli_id: str) -> bool:
    fname = _ISOLATED_CREDENTIAL_FILE.get(cli_id)
    if not fname:
        return True
    return os.path.isfile(os.path.join(_isolated_config_dir(cli_id), fname))


# The subcommand that signs the ISOLATED copy in, per CLI. claude has no
# login-only flag (confirmed against --help) -- a bare launch is what already
# walks a fresh profile through login on its own, per Anthropic's own CLI
# design, so that is what gets opened.
_LOGIN_ARGS = {"claude": [], "codex": ["login"], "opencode": ["auth", "login"]}


def launch_isolated_login(cli_id: str):
    """Open the isolated copy's OWN login flow in a real, visible window, so
    signing in is one click from the dashboard instead of "copy this
    PowerShell line into a terminal yourself" -- asked for directly: "it
    should work in the browser".

    Deliberately NOT captured/streamed back through the API: a login flow is
    interactive (an OAuth browser tab, a device code, a paste-your-key
    prompt) and the hub has no business intercepting credentials as they are
    entered. It opens the CLI in ITS OWN window and gets out of the way.
    Returns (ok, detail)."""
    if cli_id not in _LOGIN_ARGS:
        return False, "No known login flow for '%s'." % cli_id
    bin_path = _isolated_bin(cli_id)
    if not bin_path:
        return False, "The isolated copy of %s is not installed yet." % cli_id
    config_dir = _isolated_config_dir(cli_id)
    try:
        os.makedirs(config_dir, exist_ok=True)
    except OSError as exc:
        return False, "Could not prepare %s: %s" % (config_dir, exc)
    env = _agentic_env(cli_id)                # sets the isolated config-dir var
    argv = _launcher(bin_path) + _LOGIN_ARGS[cli_id]
    try:
        if os.name == "nt":
            # A REAL console the user can see and type into -- CREATE_NO_WINDOW
            # (used everywhere else this hub shells out) is the opposite of
            # what a login prompt needs.
            subprocess.Popen(argv, cwd=config_dir, env=env,
                             creationflags=subprocess.CREATE_NEW_CONSOLE)
        else:
            # Best-effort: try common terminal emulators in turn. None of this
            # machine's own testing runs on POSIX, so this is deliberately
            # simple rather than guessed-at further; if none are found the
            # caller's response says exactly that instead of silently doing
            # nothing.
            term_argv = None
            for term, flag in (("x-terminal-emulator", "-e"), ("gnome-terminal", "--"),
                               ("konsole", "-e"), ("xterm", "-e")):
                if shutil.which(term):
                    term_argv = [term, flag] + argv
                    break
            if term_argv is None:
                return False, ("Could not find a terminal to open. Run this "
                               "yourself: %s='%s' %s" % (_ISOLATED_CONFIG_ENV[cli_id],
                                                         config_dir, " ".join(argv)))
            subprocess.Popen(term_argv, cwd=config_dir, env=env)
    except Exception as exc:                                     # noqa: BLE001
        return False, "Could not open %s: %s" % (cli_id, exc)
    return True, None


def _isolated_bin(cli_id: str):
    """The hub's OWN isolated copy of the CLI, or None.

    The hub can install a CLI into ~/.free-llm-hub/isolated-clis/<cli> with its
    own npm prefix and its own config dir (CODEX_HOME / CLAUDE_CONFIG_DIR), so
    driving it as an agent never disturbs the user's interactive setup. That
    mechanism already existed; the agent chat just was not using it — every
    session resolved the GLOBAL binary through shutil.which(), which is the
    same install the user types into by hand.

    Path construction mirrors app.py's _isolated_bin_path: npm's --prefix layout
    puts the launcher in <prefix>/bin on POSIX and directly in <prefix> on
    Windows, and shutil.which(path=...) resolves PATHEXT for us.
    """
    try:
        install_dir = os.path.join(os.path.expanduser("~"), ".free-llm-hub",
                                   "isolated-clis", cli_id, "install")
        search = os.pathsep.join([install_dir, os.path.join(install_dir, "bin")])
        return shutil.which(_CLI_BIN[cli_id], path=search)
    except Exception:
        return None


def _resolve_bin(cli_id: str):
    """Isolated copy first, the user's own install second.

    Preferring the isolated one is the point: it is the copy the hub controls,
    with its own credentials and settings, so an agent session cannot pick up or
    disturb whatever the user has configured for interactive use. Falling back
    keeps every existing setup working — nobody has to install anything twice.
    """
    return _isolated_bin(cli_id) or shutil.which(_CLI_BIN[cli_id])


_DEFAULT_CLI_FLAG = "agent_default_cli"        # config key, holds the user's pick


def default_cli() -> str:
    """The CLI id start_session() defaults to when the caller omits `cli`, and
    the value the dashboard's CLI picker preselects.

    The USER'S choice wins over _DEFAULT_CLI: picking a CLI in the dashboard
    saves it, so the next session (and the next day) starts on the one they
    actually use, without a Save button to remember to press. A stored value
    naming a CLI this build cannot drive is ignored rather than obeyed."""
    chosen = config.get_value(_DEFAULT_CLI_FLAG)
    if isinstance(chosen, str) and chosen in _SUPPORT:
        return chosen
    return _DEFAULT_CLI


def set_default_cli(cli_id: str) -> str:
    """Remember which CLI the dashboard should start on. Returns what is now
    stored, so the caller never has to guess whether it took."""
    if cli_id not in _SUPPORT:
        raise AgenticError("Unknown CLI '%s'." % cli_id, 400)
    config.set_value(_DEFAULT_CLI_FLAG, cli_id)
    return default_cli()


class AgenticError(Exception):
    """Raised only by start_session() for a caller mistake (bad cli id, missing/
    invalid project_dir, master flag off, unsupported CLI, not-yet-installed
    CLI). `.status` is the HTTP status the caller should map this to.

    `.code` (optional) is a short machine-readable string the frontend can
    switch on instead of string-matching `.message` -- currently only
    "cli_not_installed" is used, paired with `.extra["install_provider"]` (see
    _INSTALL_PROVIDER_ID) so the frontend can call the EXISTING
    /api/subscriptions/<pid>/install-isolated route directly instead of just
    failing. `.extra` (any additional kwargs) is merged into the JSON error
    response verbatim by the route handler."""

    def __init__(self, message, status=400, code=None, **extra):
        super().__init__(message)
        self.status = status
        self.code = code
        self.extra = extra


# --------------------------------------------------------------------------- #
# Env / launcher helpers -- deliberately DUPLICATED (not imported) from
# app.py's _sub_env()/_sub_launcher(), to keep this module import-cycle-free
# (app.py imports this module; this module must not import app.py back). The
# logic is a handful of lines and must stay behavior-identical to the
# original: strip any env var pointing at THIS hub's own origin, so the CLI
# always talks to its real upstream and never gets redirected back into the
# hub (hub -> CLI -> hub loop guard), and route a .cmd/.bat shim through
# cmd.exe on Windows since CreateProcess cannot exec a batch file directly.
# --------------------------------------------------------------------------- #

def _port() -> int:
    return int(os.environ.get("PORT", "8787") or "8787")


def _hub_fragments():
    p = _port()
    return ["127.0.0.1:%d" % p, "localhost:%d" % p, "[::1]:%d" % p]


def _points_at_hub(val) -> bool:
    return isinstance(val, str) and any(fr in val for fr in _hub_fragments())


# Where each CLI keeps its own settings and credentials, and the env var that
# moves it. Isolation is only half done without this: running the hub's own
# COPY of a binary while it reads ~/.claude means an agent session can still
# pick up, change, or invalidate the login the user's own terminal depends on.
#
#   claude    CLAUDE_CONFIG_DIR   documented
#   codex     CODEX_HOME          documented
#   opencode  XDG_CONFIG_HOME     verified live: it logged
#             "loading path=<XDG_CONFIG_HOME>\opencode\opencode.json"
_ISOLATED_CONFIG_ENV = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME",
                        "opencode": "XDG_CONFIG_HOME"}


def _isolated_config_dir(cli_id: str) -> str:
    """~/.free-llm-hub/isolated-clis/<cli>/config — mirrors app.py's own path
    helper (duplicated for the same import-cycle reason as _launcher())."""
    return os.path.join(os.path.expanduser("~"), ".free-llm-hub",
                        "isolated-clis", cli_id, "config")


def _agentic_env(cli_id: str = None, project_dir: str = None,
                 quality: str = "normal", session_id: str = None) -> dict:
    """Child env with every hub-pointing override stripped, PWD resynced to
    match the subprocess cwd we are about to give it, and the CLI pointed at
    the hub's OWN config directory.

    THE PWD FIX, found live and confirmed with certainty (isolated
    reproduction, one variable at a time -- config-home, data-home, a fresh
    install, `--pure`, git-repo-ness, the machine's real global opencode state
    moved out of the way entirely -- all ruled out before this): every child
    here starts from `dict(os.environ)`, which carries PWD from whatever shell
    launched the hub. run.bat/run.sh both `cd` into the HUB'S OWN directory
    before starting Python, so PWD in the hub's own process is always the
    hub's repo -- for every user, every time, not just a dev workstation
    quirk. Popen's `cwd=` argument changes the OS-level working directory but
    never touches PWD in the environment dict, and opencode reads PWD to
    decide its project root rather than asking the OS for the real one.
    Measured: with a stale PWD, opencode wrote into the HUB'S OWN SOURCE TREE
    -- confirmed reproducible from a dozen different angles -- and setting
    PWD to match cwd was the one change that fixed it outright, every time.

    Everything else (PATH, HOME, the user's own settings) passes through
    unchanged. The config redirect only happens when we are actually running
    the hub's isolated copy: if the session fell back to the user's global
    install, moving its config dir would hide the login it already has and the
    session would fail asking them to authenticate something that IS
    authenticated."""
    env = dict(os.environ)
    for k in list(env.keys()):
        if _points_at_hub(env.get(k)):
            env.pop(k, None)
    if project_dir:
        env["PWD"] = project_dir
    if cli_id and _isolated_bin(cli_id):
        var = _ISOLATED_CONFIG_ENV.get(cli_id)
        if var:
            path = _isolated_config_dir(cli_id)
            try:
                os.makedirs(path, exist_ok=True)
            except OSError:
                return env                      # unwritable: better shared than broken
            env[var] = path
            if cli_id == "opencode":
                _seed_opencode_config(path)
            elif cli_id == "claude":
                _apply_claude_hub_fallback(env, path, quality, session_id)
            elif cli_id == "codex":
                _apply_codex_hub_fallback(path, session_id)
    return env


def _hub_base_url(session_id: str = None) -> str:
    """The hub's own URL, optionally tagged with the agent session it is for.

    A hub-launched CLI is pointed at <hub>/build/<session_id>. app.py strips
    that prefix in WSGI before routing, so nothing about the API changes -- it
    exists purely so the activity view can say a call came from the dashboard's
    /build page and WHICH project it belongs to. An agent CLI forwards none of
    its environment, so the URL is the only channel that carries this."""
    base = "http://127.0.0.1:%d" % _port()
    return base + "/build/" + session_id if session_id else base


def _apply_claude_hub_fallback(env, config_home, quality="normal", session_id=None):
    """No subscription, no problem: an isolated copy that has never been
    signed in runs against THIS HUB'S OWN FREE MODELS instead, same as
    opencode already does -- asked for directly: "/agent CLIs should work
    isolated and with our local hub llm, of course".

    Claude Code takes ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN/ANTHROPIC_MODEL
    as env vars (the identical shape app.py's own _autofix_claude writes into
    ~/.claude/settings.json for the user's REAL install to connect it to this
    same hub) -- so this needs no config file, just the same three vars, set
    directly on the isolated child's own environment.

    Conditional on purpose: these vars OVERRIDE stored login credentials
    whenever Claude Code sees them, so setting them unconditionally would make
    a real sign-in silently useless forever. Once .credentials.json exists,
    this stops touching the env at all and the real subscription -- generally
    the stronger option -- takes over on its own."""
    if _isolated_signed_in("claude"):
        return
    env["ANTHROPIC_BASE_URL"] = _hub_base_url(session_id)
    env["ANTHROPIC_AUTH_TOKEN"] = config.get_local_api_key() or "free-llm-hub"
    # "best" is `auto` that never drops to the cheap tier (app.py's
    # _is_orchestrate accepts it; _route_by_difficulty lifts `simple` when it
    # sees it). Sent as the MODEL so the choice travels with every turn the CLI
    # makes, including the small intermediate ones -- there is no other channel
    # back to the hub, since the CLI subprocess is an ordinary API client and
    # carries no session identity of its own.
    env["ANTHROPIC_MODEL"] = {"max": "best", "swarm": "swarm"}.get(quality, "auto")


# Bare minimum codex needs to treat the hub as a provider -- the same shape
# app.py's _codex_apply_text writes for a real, interactive install. Ported
# rather than reused, for two reasons: agentic_chat.py cannot import app.py
# (import cycle -- app.py imports this module), same as _launcher() and
# _isolated_config_dir(); and this needs a REVERT path _codex_apply_text has
# no reason to have, since a real install is never expected to un-connect
# itself the moment a login appears.
#
# MEASURED, and the reason this is additive rather than "only write a fresh
# file": codex writes its OWN config.toml on the very first invocation in ANY
# directory, unprompted -- a [projects.'<path>'] trust-level entry, with no
# provider config at all. So by the time this ever runs, the file essentially
# always already exists, and treating "the file exists" as "do not touch it"
# meant the fallback silently never activated after the first codex run on
# the machine. The real question is narrower: does it carry an EXPLICIT
# model_provider (someone, or an earlier real login, deliberately chose a
# provider)? Only that is left alone.
_CODEX_HUB_MARKER = "# free-llm-hub: isolated fallback -- removed automatically once signed in"

# What the hub tells codex about its own capacity.
#
# REPORTED 2026-08-30, printed on every hub-backed codex turn:
#   "Model metadata for `swarm` not found. Defaulting to fallback metadata;
#    this can degrade performance and cause issues."
# Codex looks the model up in a built-in metadata table to learn the context
# window and max output. The hub's ids ("auto", "best", "swarm") are ROUTING
# VERBS, not real model names, so that lookup misses every time and codex falls
# back to a built-in default -- i.e. it guesses how much context it may use.
# Pre-existing rather than new: the same warning appeared for "auto" long
# before the quality modes existed (captured in test_codex_agentic.py's live
# fixture back in July).
#
# 128000 is the same context size the hub already states to other CLIs (see the
# Kimi Code setup text in app.py) -- one assumption, written down once. Both
# are marker-tagged like every other line the hub adds, so a later real sign-in
# strips exactly these and never caps a genuine subscription.
#
# VERIFIED against the config reference and the installed binary, not guessed:
# model_context_window and model_auto_compact_token_limit are documented keys;
# `model_max_output_tokens` is NOT one (it was tried here first and removed --
# an unrecognised key is also what --strict-config exists to reject).
#
# The WARNING LINE ITSELF is deliberately left alone. Silencing it needs
# model_catalog_json, an undocumented internal schema: a probe against
# codex 0.146.0 got a catalog accepted only after it named `slug`,
# `display_name`, `context_window`, `max_output_tokens`,
# `auto_compact_token_limit`, `supported_reasoning_levels`, `shell_type`, and
# more still behind those (`visibility`, `service_tiers`, `availability_nux`,
# ...). Codex REFUSES TO START when that file misses a field it wants, so
# writing it would hand every user a codex that breaks the next time OpenAI
# adds one. A cosmetic warning is the cheaper of the two.
_CODEX_CONTEXT_WINDOW = 128000
# When codex compacts history. It defaults this off the context window, so a
# guessed window means a badly-timed compaction too; stated at 75% of ours.
_CODEX_COMPACT_LIMIT = 96000
_CODEX_TOP_TABLE_RE = re.compile(r"^\s*\[")
_CODEX_MODEL_PROVIDER_RE = re.compile(r"^\s*model_provider\s*=", re.M)


def _codex_toml_top_and_rest(text):
    """Split config.toml into (top-level bare keys, everything from the first
    [table] header on) -- TOML only allows bare keys before the first table,
    which is what makes a plain string-insert safe here."""
    top, rest, in_rest = [], [], False
    for ln in (text or "").splitlines():
        if not in_rest and _CODEX_TOP_TABLE_RE.match(ln):
            in_rest = True
        (rest if in_rest else top).append(ln)
    return top, rest


def _codex_hub_fallback_text(existing, session_id=None):
    """Point config.toml at the hub, ADDITIVELY: only the model_provider/model
    top keys and a [model_providers.freehub] table are touched. Everything
    else -- notably codex's own [projects.*] trust entries -- passes through
    verbatim, the same guarantee app.py's real-install version makes. Every
    line this adds carries the marker, which is what lets a later sign-in
    remove EXACTLY these lines and nothing codex or the user wrote."""
    top, rest = _codex_toml_top_and_rest(existing)

    def _set_top_key(name, value, quote=True, keep_existing=False):
        """Write one marker-tagged top-level key.

        `keep_existing` leaves a value the USER set alone. Found by this file's
        own test: without it, a user's own `model_context_window = 999999` was
        overwritten by ours and then DELETED outright by the revert path, since
        revert strips marker-tagged lines and by then the only such line was
        the one we had written over theirs. Used for the capacity hints, where
        the user's number is as good as ours; deliberately NOT used for
        model/model_provider, whose existing overwrite behaviour is what points
        an unsigned-in copy at the hub in the first place."""
        pat = re.compile(r"^\s*%s\s*=" % re.escape(name))
        rendered = '"%s"' % value if quote else str(value)
        line = '%s = %s  %s' % (name, rendered, _CODEX_HUB_MARKER)
        for i, ln in enumerate(top):
            if pat.match(ln):
                if keep_existing and _CODEX_HUB_MARKER not in ln:
                    return                      # the user's own value; leave it
                top[i] = line
                return
        top.insert(0, line)

    # Drop any top key WE wrote in an older version and no longer write. The
    # marker means "the hub owns this line", so the hub has to clean up after
    # itself when its own set of keys changes -- otherwise a key that turned
    # out to be wrong (this happened: `model_max_output_tokens`, which is not a
    # real codex key at all) sits in the user's config forever, and the very
    # flag meant to catch that, --strict-config, rejects the whole file over it.
    _ours = ("model_provider", "model", "model_context_window",
             "model_auto_compact_token_limit")
    top[:] = [ln for ln in top
              if _CODEX_HUB_MARKER not in ln
              or ln.split("=", 1)[0].strip() in _ours]

    _set_top_key("model_provider", "freehub")
    _set_top_key("model", "auto")
    # Unquoted: TOML would read a quoted value as a string, and codex wants an
    # integer here.
    _set_top_key("model_context_window", _CODEX_CONTEXT_WINDOW,
                 quote=False, keep_existing=True)
    _set_top_key("model_auto_compact_token_limit", _CODEX_COMPACT_LIMIT,
                 quote=False, keep_existing=True)

    cleaned, skip = [], False
    for ln in rest:
        if _CODEX_TOP_TABLE_RE.match(ln):
            skip = (ln.strip() == "[model_providers.freehub]")
        if not skip:
            cleaned.append(ln)

    # No standalone marker line ABOVE the table: the "drop the old
    # [model_providers.freehub] table" scan below only recognizes the table
    # header itself, so a comment line preceding it survived every re-apply
    # and piled up one copy per turn -- measured, two applies back to back
    # were not byte-identical. The table name is already unique and already
    # matched on removal, so it needs no separate marker.
    block = ["[model_providers.freehub]",
            'name = "Calvoun Free LLM Hub"',
            'base_url = "%s/v1"' % _hub_base_url(session_id),
            'wire_api = "responses"']
    bearer = config.get_local_api_key()
    if bearer:
        block.append('experimental_bearer_token = "%s"' % bearer)

    new_text = "\n".join(top + cleaned).rstrip("\n")
    return (new_text + "\n\n" if new_text else "") + "\n".join(block) + "\n"


def _revert_codex_hub_fallback_text(existing):
    """Strip exactly what _codex_hub_fallback_text added -- every line is
    marker-tagged, so this can never remove a REAL provider choice, only the
    fallback's own. codex's [projects.*] entries and anything else survive."""
    top, rest = _codex_toml_top_and_rest(existing)
    top = [ln for ln in top if _CODEX_HUB_MARKER not in ln]
    cleaned, skip = [], False
    for ln in rest:
        if ln.strip() == _CODEX_HUB_MARKER:
            skip = True
            continue
        if _CODEX_TOP_TABLE_RE.match(ln):
            skip = (ln.strip() == "[model_providers.freehub]")
        if not skip:
            cleaned.append(ln)
    combined = (top + cleaned)
    while combined and not combined[-1].strip():
        combined.pop()
    return "\n".join(combined) + ("\n" if combined else "")


def _best_effort_native_id(cli_id, stdout):
    """Whatever thread/session id can be salvaged from the OUTPUT OF A KILLED
    PROCESS, for the non-streaming path -- used only when a turn is killed for
    exceeding _TURN_TIMEOUT, so a retry can RESUME instead of restarting from
    zero. codex and opencode emit JSONL from the first line on regardless of
    stream/non-stream mode, so an early, COMPLETE line (thread.started /
    system init) survives even though the final line was cut off mid-write;
    each parser already skips unparseable lines rather than raising. claude's
    non-streaming --output-format json is a single blob written all at once
    on completion -- a kill mid-turn leaves no complete JSON at all, so there
    is nothing to recover and this correctly returns None for it."""
    if cli_id == "codex":
        _, native_id, _ = _parse_codex_json(stdout, "", 1)
        return native_id
    if cli_id == "opencode":
        _, native_id, _ = _parse_opencode_json(stdout, "", 1)
        return native_id
    return None


def _write_codex_toml(path, text):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except OSError:
        pass                          # a missing fallback surfaces as a clear auth error later


def _apply_codex_hub_fallback(config_home, session_id=None):
    """The file-based counterpart to _apply_claude_hub_fallback: codex reads
    its provider from config.toml, not env vars, so an isolated copy that has
    never been signed in gets a config.toml pointing at this hub -- same free
    models opencode already falls back to, asked for directly."""
    path = os.path.join(config_home, "config.toml")
    try:
        existing = open(path, encoding="utf-8", errors="replace").read() if os.path.isfile(path) else ""
    except OSError:
        return
    if _isolated_signed_in("codex"):
        # Signed in now: remove OUR OWN prior fallback (if any) so the real
        # login takes over. codex's [projects.*] entries and anything else in
        # the file are untouched -- only marker-tagged lines are ever removed.
        if _CODEX_HUB_MARKER in existing:
            _write_codex_toml(path, _revert_codex_hub_fallback_text(existing))
        return
    # Idempotent re-apply (already ours) is fine; a REAL, explicit provider
    # choice already in the file (not ours, not empty) is never overwritten.
    if existing and _CODEX_HUB_MARKER not in existing and _CODEX_MODEL_PROVIDER_RE.search(existing):
        return
    _write_codex_toml(path, _codex_hub_fallback_text(existing, session_id))


# The model ids the hub answers to, in the shape opencode wants. Keep in step
# with _hub_model_for(): a mode whose id is missing here is a failed turn, not
# a fallback.
_OPENCODE_HUB_MODELS = {
    "auto": {"name": "auto (best free, orchestrated)"},
    "best": {"name": "best (max quality -- never the cheap tier)"},
    "swarm": {"name": "swarm (several models per turn, best answer wins)"},
}


def _upgrade_opencode_seed(target):
    """Add any missing hub model ids to a seed WE wrote. No-op for a config we
    do not recognise, and no write at all when nothing is missing."""
    try:
        with open(target, encoding="utf-8") as fh:
            cfg = json.load(fh)
        prov = (cfg.get("provider") or {}).get("free-llm-hub")
        if not isinstance(prov, dict):
            return                          # not ours -- the user's own config
        models = prov.get("models")
        if not isinstance(models, dict):
            return
        missing = {k: v for k, v in _OPENCODE_HUB_MODELS.items() if k not in models}
        if not missing:
            return
        models.update(missing)
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
        os.replace(tmp, target)
    except Exception:                                            # noqa: BLE001
        pass                    # a stale seed is a clear error later, not a crash


def _seed_opencode_config(config_home):
    """Give the isolated opencode a provider: this hub.

    claude and codex ARE subscriptions -- isolating them means signing the
    hub's copy into the same account, and the hub must stay out of it. opencode
    is different: it brings no provider of its own, so an isolated config with
    nothing in it means every turn fails with ProviderAuthError before the
    agent does any work at all.

    So the hub's own copy is pointed at the hub, which is the one provider we
    know exists and costs nothing. Written ONCE -- if the file is already there
    it is left alone, including when the user has configured it themselves.
    A project's own opencode.json still wins over this at run time, which is
    how a project can pin a specific model.

    "Left alone" now has one narrow exception: a seed WE wrote that predates
    the quality modes lists only `auto`, and an openai-compatible provider will
    not serve a model it does not list -- so `--model free-llm-hub/swarm` would
    fail on every install that ran opencode before this shipped, forever, since
    the early return above meant our own file was never revisited. A file that
    is recognisably ours and merely out of date is topped up in place; anything
    the user wrote is still never touched."""
    target = os.path.join(config_home, "opencode", "opencode.json")
    if os.path.exists(target):
        _upgrade_opencode_seed(target)
        return
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        key = config.load_config().get("local_api_key") or "free-llm-hub"
        payload = {
            "$schema": "https://opencode.ai/config.json",
            "provider": {
                "free-llm-hub": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "Calvoun Free LLM Hub",
                    "options": {"baseURL": "http://127.0.0.1:%d/v1" % _port(),
                                "apiKey": key},
                    "models": _OPENCODE_HUB_MODELS,
                },
            },
            "model": "free-llm-hub/auto",
        }
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, target)
    except Exception:                                            # noqa: BLE001
        pass                    # a missing seed is a clear error later, not a crash


def _launcher(path):
    """argv prefix that can actually execute `path` (see _sub_launcher() in
    app.py -- identical logic, duplicated to avoid a circular import)."""
    if os.name == "nt" and os.path.splitext(path)[1].lower() in (".cmd", ".bat"):
        return [os.environ.get("COMSPEC") or "cmd.exe", "/c", path]
    return [path]


# --------------------------------------------------------------------------- #
# Secret scrubbing -- reuses config.py directly (a leaf module both app.py and
# this module can safely import with no cycle), so this stays byte-consistent
# with app.py's own _secret_values()/_sanitize() without importing app.py.
# --------------------------------------------------------------------------- #

def _secret_values():
    vals = []
    try:
        cfg = config.load_config()
        for pcfg in (cfg.get("providers") or {}).values():
            if not isinstance(pcfg, dict):
                continue
            for key in (pcfg.get("api_keys") or []):
                if key:
                    vals.append(key)
            legacy = pcfg.get("api_key")
            if legacy:
                vals.append(legacy)
        local = cfg.get("local_api_key")
        if local:
            vals.append(local)
    except Exception:
        pass
    return vals


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _sanitize(text, limit=None):
    """Never let a provider key (or the local key) leak into an error surfaced
    to the client. `limit=None` leaves successful result text un-truncated;
    error/detail strings pass a small limit, mirroring app.py's _sanitize().

    Also strips ANSI colour codes: opencode's CLI errors (`\\x1b[91m\\x1b[1mError:
    \\x1b[0mSession not found`) come from raw stderr, and those escape bytes have
    no business landing in a chat bubble."""
    s = str(text if text is not None else "")
    s = _ANSI_RE.sub("", s)
    for secret in _secret_values():
        if secret and secret in s:
            s = s.replace(secret, "***")
    return s[:limit] if limit else s


# --------------------------------------------------------------------------- #
# Process-tree kill -- addresses the orphaned-child-process risk called out in
# the research (killing only the top PID can leave Bash/MCP grandchildren
# running). Best-effort, never raises.
# --------------------------------------------------------------------------- #

def _signal_tree(pid, hard):
    try:
        if os.name == "nt":
            argv = ["taskkill", "/PID", str(pid), "/T"] + (["/F"] if hard else [])
            subprocess.run(argv, capture_output=True, timeout=10)
        else:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGKILL if hard else signal.SIGTERM)
    except Exception:
        pass


def _terminate(proc) -> None:
    """Soft-signal the WHOLE process tree, escalate to a hard kill after a
    short grace period if it hasn't exited. Never raises.

    Calls proc.terminate()/kill() (the standard, always-correct way to signal
    the immediate child) AND _signal_tree() (taskkill /T / killpg, which
    additionally reaches grandchildren -- e.g. a Bash-tool child process --
    that terminate()/kill() alone would leave orphaned)."""
    try:
        proc.terminate()
    except Exception:
        pass
    _signal_tree(proc.pid, hard=False)
    try:
        proc.wait(timeout=_KILL_GRACE)
        return
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass
    _signal_tree(proc.pid, hard=True)
    try:
        proc.wait(timeout=_KILL_GRACE)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Session registry
# --------------------------------------------------------------------------- #

class _Session:
    __slots__ = ("id", "cli_id", "project_dir", "native_session_id", "turn_count",
                 "created_at", "proc", "proc_lock", "turn_lock", "last_interrupted",
                 "tools_notified", "quality")

    def __init__(self, cli_id, project_dir, quality="normal"):
        self.id = uuid.uuid4().hex
        self.cli_id = cli_id
        self.project_dir = project_dir
        self.native_session_id = None      # the CLI's OWN session id, captured turn 1
        self.turn_count = 0
        self.created_at = time.time()
        self.proc = None                   # currently-running Popen, or None
        self.proc_lock = threading.Lock()  # guards .proc
        self.turn_lock = threading.Lock()  # only one turn may run at a time
        self.last_interrupted = False
        self.tools_notified = False        # missing-toolchain notice, once per session
        # "normal" | "max". Chosen once, when the session starts. "max" launches
        # the CLI with ANTHROPIC_MODEL=best instead of auto, so every turn it
        # sends is routed at the top tier and never drops to the cheap one.
        self.quality = quality if quality in ("normal", "max", "swarm") else "normal"


_REGISTRY: dict[str, _Session] = {}
_REGISTRY_LOCK = threading.RLock()


def _prepare_new_project_dir(abs_dir, original):
    """create_new=True path for start_session(): validate abs_dir does NOT
    already exist as a non-empty directory (refuse to silently reuse/overwrite
    something the user didn't mean to), that its PARENT directory exists and is
    writable, then create it. Raises AgenticError on any problem."""
    if os.path.exists(abs_dir):
        if not os.path.isdir(abs_dir):
            raise AgenticError("project_dir '%s' already exists and is not a "
                               "directory." % original, 400)
        if os.listdir(abs_dir):
            raise AgenticError("project_dir '%s' already exists and is not empty "
                               "-- refusing to reuse it. Pass create_new=false to "
                               "open it as an existing project instead."
                               % original, 400)
        return  # exists as an empty directory -- fine to reuse as the new project
    parent = os.path.dirname(abs_dir)
    if not parent or not os.path.isdir(parent):
        raise AgenticError("Cannot create project_dir '%s': parent directory "
                           "'%s' does not exist." % (original, parent), 400)
    if not os.access(parent, os.W_OK):
        raise AgenticError("Cannot create project_dir '%s': parent directory "
                           "'%s' is not writable." % (original, parent), 400)
    try:
        os.makedirs(abs_dir, exist_ok=True)
    except OSError as exc:
        raise AgenticError("Failed to create project_dir '%s': %s"
                           % (original, exc), 400)


# --------------------------------------------------------------------------- #
# Recent projects -- in-memory only (module-level list), same lifetime/
# durability tradeoff as _REGISTRY: last N distinct project_dir values used
# THIS process lifetime, so the dashboard can show recently-used folders
# instead of a blank text box every time. Cross-restart persistence is a
# separate, later history feature -- not this one.
# --------------------------------------------------------------------------- #

_RECENT_PROJECTS_MAX = 10
_recent_projects: list = []
_recent_projects_lock = threading.Lock()


def _remember_recent_project(abs_dir):
    with _recent_projects_lock:
        if abs_dir in _recent_projects:
            _recent_projects.remove(abs_dir)
        _recent_projects.insert(0, abs_dir)
        del _recent_projects[_RECENT_PROJECTS_MAX:]


def get_recent_projects():
    """Last _RECENT_PROJECTS_MAX distinct project_dir values start_session()
    has used this process lifetime, most-recently-used first. Never raises."""
    with _recent_projects_lock:
        return list(_recent_projects)


def start_session(cli_id, project_dir, create_new=False, quality="normal") -> str:
    """Validate + register a new agentic session, return its session_id.
    Raises AgenticError (with a caller-friendly .status) on any invalid input.
    Never spawns a subprocess -- that only happens on the first send_message().

    cli_id may be omitted (None/"") -- defaults to _DEFAULT_CLI ("claude"); see
    default_cli(). When create_new is True, project_dir is a NEW folder that
    must NOT already exist as a non-empty directory -- it (and, note, NOT any
    missing grandparent -- only the immediate parent is required to already
    exist) is created via os.makedirs(). When create_new is False (default),
    project_dir must already exist as a directory, same as before this
    parameter was added."""
    if not _master_on():
        raise AgenticError("Agentic chat is turned off (agentic_chat_enabled=False). "
                           "Enable it via POST /api/agent/settings first.", 403)
    if not cli_id:
        cli_id = _DEFAULT_CLI
    if not isinstance(cli_id, str) or cli_id not in _SUPPORT:
        raise AgenticError("cli must be 'claude' or 'codex' (got %r)." % (cli_id,), 400)
    if not project_dir or not isinstance(project_dir, str):
        raise AgenticError("project_dir is required.", 400)
    if create_new and not os.path.isabs(os.path.expanduser(project_dir.strip())):
        # A bare name the user typed (as opposed to an absolute path from
        # Browse-for-folder or the ~/calvoun-projects suggestion new_project_dir()
        # auto-fills) must NOT resolve against the HUB SERVER's own cwd -- that
        # cwd is this repo's root when launched the normal way, which silently
        # created new projects INSIDE the hub's own source tree (same bug class
        # as the opencode PWD incident: server-process state leaking into a
        # user-chosen path). Anchor it under the same ~/calvoun-projects
        # convention new_project_dir() already uses instead.
        calvoun_projects = os.path.join(os.path.expanduser("~"), "calvoun-projects")
        # _prepare_new_project_dir below only requires the IMMEDIATE parent to
        # already exist (by design -- it won't create missing grandparents for
        # an arbitrary user path); this base folder is OURS to guarantee, the
        # same way new_project_dir() already does for the auto-suggested path.
        os.makedirs(calvoun_projects, exist_ok=True)
        project_dir = os.path.join(calvoun_projects, project_dir.strip())
    abs_dir = os.path.abspath(os.path.expanduser(project_dir))
    if create_new:
        _prepare_new_project_dir(abs_dir, project_dir)
    elif not os.path.isdir(abs_dir):
        raise AgenticError("project_dir '%s' does not exist or is not a directory."
                           % project_dir, 400)
    # Installed check BEFORE the supported-mode check, and for BOTH clis: a
    # not-yet-installed codex should still surface as "installable" (users may
    # want it ready for when full agentic support lands), not get masked by the
    # "not currently supported" message below.
    bin_path = _resolve_bin(cli_id)
    if not bin_path:
        raise AgenticError(
            "'%s' is not installed (not found on PATH). It can be installed "
            "with one click." % _CLI_BIN[cli_id],
            400, code="cli_not_installed",
            install_provider=_INSTALL_PROVIDER_ID.get(cli_id))
    supported, reason = _SUPPORT[cli_id]
    if not supported:
        raise AgenticError("%s agentic mode is not currently supported: %s"
                           % (cli_id, reason), 400)
    sess = _Session(cli_id, abs_dir, quality=quality)
    with _REGISTRY_LOCK:
        _REGISTRY[sess.id] = sess
    _remember_recent_project(abs_dir)
    return sess.id


def resume_session(cli_id, project_dir, native_session_id, session_id=None) -> str:
    """Rebuild a session that continues an EXISTING CLI thread.

    Sessions live in memory, so a hub restart drops them -- but the CLI's own
    conversation does not: `codex exec resume <thread_id>` and
    `claude --resume <id>` both pick it up with the model's full context intact.
    All that was missing was somewhere to keep that id across a restart.

    So this takes the native id recorded with the transcript and hands back a
    live session already pointed at it, which is what makes "continue" in the
    history list actually continue rather than start over. Reuses the ORIGINAL
    session_id when given one, so the transcript on disk keeps accumulating into
    the same conversation instead of forking a second one.

    THE BUG THIS GUARDS (found live): every /agent/<id> page load calls the
    /resume route unconditionally, including for a session that is genuinely
    still mid-turn. Without this check, the swap below would silently replace
    the live Session -- proc handle and all -- with a fresh, proc-less stand-in
    under the same id, orphaning the hub's only handle to the real process. The
    real subprocess keeps running (nothing here can stop it -- send_message_
    stream holds its own reference), but currently_running now reads False, so
    sending a new message to that same id would start a SECOND process on top
    of it, in the same project folder."""
    if session_id and _SAFE_SESSION_ID_RE.match(str(session_id)):
        with _REGISTRY_LOCK:
            existing = _REGISTRY.get(str(session_id))
            if existing is not None:
                with existing.proc_lock:
                    live_proc = existing.proc
                if live_proc is not None and live_proc.poll() is None:
                    return existing.id
    sid = start_session(cli_id, project_dir)      # all the same validation
    with _REGISTRY_LOCK:
        sess = _REGISTRY.pop(sid)
        if session_id and _SAFE_SESSION_ID_RE.match(str(session_id)):
            sess.id = str(session_id)
        sess.native_session_id = native_session_id or None
        _REGISTRY[sess.id] = sess
        return sess.id


def new_project_dir():
    """Create a fresh, uniquely-named empty project folder under
    ~/calvoun-projects and return its absolute path -- powers the dashboard's
    one-click "Create new project" (auto-name + auto-create, no typing). Retries
    on a name clash; OSError (e.g. permission) propagates to the caller."""
    import time
    base = os.path.join(os.path.expanduser("~"), "calvoun-projects")
    os.makedirs(base, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for i in range(1, 100):
        name = "project-%s" % stamp if i == 1 else "project-%s-%d" % (stamp, i)
        path = os.path.join(base, name)
        if not os.path.exists(path):
            os.makedirs(path)
            return path
    path = os.path.join(base, "project-%s-%x" % (stamp, abs(hash(stamp)) & 0xffff))
    os.makedirs(path, exist_ok=True)
    return path


# Claude Code CLI --model alias (confirmed via a live WebFetch against the
# current code.claude.com/docs/en/cli-reference: --model accepts the aliases
# sonnet|opus|haiku|fable, or a full model name). We deliberately pin "opus",
# NOT "fable", even though Anthropic's own docs describe Fable 5 as the single
# most capable model overall:
#   - "opus" is a long-stable alias; "fable" is new enough that there is no
#     confirmation the installed Claude Code build (this machine: 2.1.212)
#     actually resolves it -- an unverified guess here could make every single
#     agentic-chat call fail, which is exactly the risk this feature must not
#     take (see module docstring).
#   - Fable 5 changes response shape in ways _parse_claude_json() does not
#     handle (always-on thinking, no assistant prefill, a "refusal" stop
#     reason) and requires 30-day data retention -- it would hard-fail under a
#     ZDR org this hub has no visibility into.
#   - "opus" auto-tracks future Opus releases, matching "strongest currently-
#     available model" without pinning a specific date-suffixed model string.
# Passed on EVERY invocation (turn 1 and every --resume turn), since permission
# flags are already known not to persist across --resume and --model is
# treated the same way defensively.
_MODEL_ALIAS = "opus"


# --------------------------------------------------------------------------- #
# Carrying the session's model-quality mode to the hub.
#
# MEASURED 2026-08-30 on a live codex session whose stored quality was "swarm":
# the hub's activity row for its turns read {"cli": "Codex", "model_req":
# "auto"}. The mode was saved, displayed and persisted, and then lost at the
# only boundary that matters -- the CLI subprocess, which is an ordinary API
# client and carries no session identity of its own. The MODEL NAME is the one
# channel that reaches the hub on every single turn, including the small
# intermediate ones, so the mode travels as the model.
#
# Sent as a per-invocation --model flag rather than written into the CLI's
# config file: the isolated config dir is shared by every session of that CLI,
# so a second session turning at the same moment would race the first one's
# mode. argv cannot be raced.
#
# Verified against the installed binaries (`codex exec --help`, `opencode run
# --help`) rather than assumed -- both accept `-m, --model`.
# --------------------------------------------------------------------------- #

def _hub_model_for(quality: str = None) -> str:
    """The hub-side model id that carries a session's mode.

    "best" is app._is_orchestrate's quality_mode (auto that never drops to the
    cheap tier); "swarm" is app._is_swarm_model's parallel best-of-N fan-out;
    "auto" is ordinary routing. Anything unrecognised falls back to auto rather
    than inventing a model name nothing serves."""
    return {"max": "best", "swarm": "swarm"}.get(quality or "normal", "auto")


def _hub_backs(cli_id: str) -> bool:
    """True when this CLI's requests actually reach THIS hub.

    Same condition _apply_claude_hub_fallback / _apply_codex_hub_fallback use to
    decide whether to point the child here at all: an isolated copy that has
    never been signed in. Once a real subscription exists the child talks to its
    own vendor, where "swarm" is not a model -- sending it would fail the turn
    outright, which is worse than not applying the mode."""
    return bool(_isolated_bin(cli_id)) and not _isolated_signed_in(cli_id)


# --------------------------------------------------------------------------- #
# System-prompt injection -- CONFIRMED via a live doc fetch (code.claude.com/
# docs/en/cli-reference, 2026-07) that `--append-system-prompt` is a real,
# CLI-usable (not SDK-only) flag that works alongside `-p`. Like `--model`, it
# is documented as NOT persisting across `--resume`, so (mirroring the
# existing _MODEL_ALIAS handling) it must be passed on EVERY turn, not just
# turn 1. Additive-only: an empty result here changes argv not at all.
# --------------------------------------------------------------------------- #

_TEST_VERIFICATION_SNIPPET = (
    "Testing/verification is expected this session: after making a change, use "
    "your existing tool access (Playwright, if installed) to actually run and "
    "verify the result before declaring it done, rather than assuming it works."
)

_VISION_GAP_SNIPPET = (
    "Note: no vision-capable model is currently connected in this hub (no enabled "
    "provider with a valid key exposes an image-input model), so you cannot analyze "
    "images or screenshots directly. If relevant to what the user asked, mention this "
    "honestly and offer: report back once one becomes available, rely on the automatic "
    "background recheck already running, or skip vision-dependent work for now."
)


_BRIEF_POINTER = (
    "This folder contains %s -- required standards for this task. "
    "READ IT FIRST and follow it."
)

_RESTATE_SNIPPET = (
    "Before you create anything, restate in ONE line what you are building and "
    "for whom, naming the actual subject and place from the request. If the "
    "request is unclear or contradicts itself, ask one short question instead "
    "of guessing. Never substitute a generic template for the subject you were "
    "given."
)

_PLANNING_SNIPPET = (
    "For any non-trivial task: think it through step by step first, then break "
    "it into phases with a visible todo list -- your own native planning/task "
    "tool if you have one, and a real file in this project either way, "
    "PROGRESS.md or similar. A reply-only checklist does not survive: your "
    "OWN context can get compacted mid-task, and this conversation can be "
    "resumed later, possibly as a fresh thread with none of your prior "
    "reasoning -- a file on disk is the only copy of the plan that outlives "
    "either. Before starting work each turn, check for that file and read it "
    "first if it exists -- it may already have progress on this task from "
    "before; do not redo completed work or re-derive decisions already made, "
    "that wastes real time and tokens for nothing. Update the file as you go: "
    "what's done, what's in progress, what's next, and any decision worth "
    "remembering -- not just at the end. Work through phases in order; "
    "independent steps within a phase can run in parallel. Mark each item "
    "done as you actually finish it, not all at once at the end -- the list "
    "is how the user tracks real progress, not a formality to produce and "
    "then ignore."
)


def _system_prompt_addition(text: str = "", has_brief: bool = False) -> str:
    """Extra system-prompt text for this turn, or "" for none. Never raises --
    a diagnostics read failing must not block a turn from running.

    THE CRAFT BRIEFS HAVE TO BE INJECTED HERE, not on the gateway path.
    app.py injects them into requests that pass THROUGH the hub, but an agent
    session never does: _agentic_env() deliberately strips every hub-pointing
    variable so the CLI cannot call back into us, and the hub log confirms it --
    zero /v1/responses hits while a session ran a full turn. So for the main way
    people actually build things here, the design/SEO/image/security rules were
    reaching nothing.

    That is not academic. Asked for "a restaurant in Fez, Morocco", a session
    produced "Calvoun Store - Premium Products / Discover Premium Quality /
    Elevate your lifestyle", selling wireless headphones. "Discover", "Elevate"
    and the generic-template shape are all named in the WEB_DESIGN ANTI list --
    the list simply never arrived.

    _RESTATE_SNIPPET is the other half of that failure: the request mixed
    "store website" with "restaurant in fez", and the model resolved the
    ambiguity silently and wrongly. One restated line surfaces that in seconds
    instead of after a full build."""
    parts = []
    if text:
        parts.append(_PLANNING_SNIPPET)
    if test_verification_enabled():
        parts.append(_TEST_VERIFICATION_SNIPPET)
    try:
        available = vision_status.status().get("available")
    except Exception:
        available = True  # fail closed on the NOTICE (stay silent), not on the turn
    if not available:
        parts.append(_VISION_GAP_SNIPPET)
    if text:
        parts.append(_RESTATE_SNIPPET)
    if has_brief:
        parts.append(_BRIEF_POINTER % BRIEF_FILENAME)
    # chr(10) rather than a backslash-n literal: this file gets edited through
    # tooling that has repeatedly turned that escape into a RAW newline, which
    # splits the string across lines and makes the module unimportable.
    sep = chr(10) + chr(10)
    return sep.join(p for p in parts if p)


BRIEF_FILENAME = ".calvoun-brief.md"


def write_task_brief(project_dir, text):
    """Write the craft brief for `text` into the project, return True if any.

    WHY A FILE AND NOT MORE PROMPT: the prompt travels as a POSITIONAL argv
    argument through `cmd.exe /c <shim.cmd> ...` on Windows, and that command
    line dies at ~8191 chars -- which is exactly why _MAX_MESSAGE_CHARS exists.
    The briefs are ~9,000 chars on their own; inlining them would have taken the
    worst case to roughly 15,700 and broken every turn on this platform.

    An agent has file tools. So the standards go in a file it reads, the prompt
    spends ~200 characters telling it to, and the full brief arrives intact.

    Rewritten each turn it applies, so the standards always match the CURRENT
    request rather than whatever the first message happened to be about."""
    try:
        brief = craft.system_message(text or "")
        if not brief:
            return False
        path = os.path.join(project_dir, BRIEF_FILENAME)
        header = ("<!-- Written by Calvoun Free LLM Hub for THIS task. "
                  "Safe to delete; it is regenerated whenever it applies. -->")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(header + chr(10) + chr(10) + brief["content"] + chr(10))
        return True
    except Exception:                                            # noqa: BLE001
        return False        # standards are a bonus; never cost the user a turn


def _claude_model_for(sess) -> str:
    """--model for one claude turn: the session's mode when the hub is serving
    it, the long-stable "opus" alias otherwise."""
    quality = getattr(sess, "quality", "normal")
    if quality in ("max", "swarm") and _hub_backs("claude"):
        return _hub_model_for(quality)
    return _MODEL_ALIAS


def _build_argv(sess: _Session, bin_path: str, text: str, stream=False):
    if sess.cli_id == "codex":
        return _build_argv_codex(sess, bin_path, text)  # --json serves stream + non-stream
    if sess.cli_id == "opencode":
        return _build_argv_opencode(sess, bin_path, text)      # ditto
    if sess.cli_id != "claude":
        # Fail loudly rather than silently mis-running an unknown CLI.
        raise AgenticError("No known invocation for CLI '%s'." % sess.cli_id, 400)
    args = ["-p", text]
    if sess.native_session_id:
        args += ["--resume", sess.native_session_id]
    args += ["--output-format", "stream-json" if stream else "json",
             # BOTH bypass flags, not just one: MEASURED live, a real turn hit
             # "Claude requested permissions to write to <path>, but you
             # haven't granted it yet" / toolDenialKind "user-rejected" on a
             # Write tool call despite --dangerously-skip-permissions being
             # present -- claude 2.1.220, stdin closed so nothing could ever
             # answer the prompt it still raised. --permission-mode
             # bypassPermissions is a DISTINCT, separately-implemented bypass
             # mechanism (confirmed via `claude --help`, and confirmed safe to
             # pass alongside the other flag with a fresh, uncontaminated
             # config dir: exit 0, file written, permissionMode resolves to
             # "bypassPermissions"). Belt-and-suspenders: if one mechanism has
             # a gap the other doesn't, only one needs to hold. The turn is
             # ALSO robust if a prompt gets raised anyway -- see
             # send_message_stream's last_message_text fallback, which keeps
             # whatever the model said instead of reporting "no reply".
             "--dangerously-skip-permissions", "--permission-mode", "bypassPermissions",
             # The mode rides on --model. An explicit CLI flag is not something
             # the ANTHROPIC_MODEL env var _apply_claude_hub_fallback sets can
             # win against, so setting only that env var left every turn asking
             # for "opus" -- ordinary routing -- no matter which mode was picked.
             # Normal keeps _MODEL_ALIAS exactly as before.
             "--model", _claude_model_for(sess)]
    if stream:
        args += ["--verbose"]  # claude -p requires --verbose alongside stream-json
    first = not sess.native_session_id
    addition = _system_prompt_addition(
        text if first else "",
        has_brief=first and write_task_brief(sess.project_dir, text))
    if addition:
        args += ["--append-system-prompt", addition]
    return _launcher(bin_path) + args


def _build_argv_codex(sess: "_Session", bin_path: str, text: str):
    """Codex agentic invocation (codex-cli 0.144.5, live-verified).

    Turn 1:  codex exec --json --dangerously-bypass-approvals-and-sandbox
                          --skip-git-repo-check <prompt>
    Turn 2+: codex exec resume <thread_id> --json
                          --dangerously-bypass-approvals-and-sandbox <prompt>

    The prompt is POSITIONAL (codex has no -p). The project dir is the subprocess
    cwd (set by send_message's Popen), NOT -C -- that is what makes `resume` (which
    has no -C flag) still write to the right folder. Codex has no
    --append-system-prompt, so the optional test/vision notice is prepended into
    the prompt text. --skip-git-repo-check is only passed on the fresh turn (the
    `resume` subcommand doesn't accept it and the repo was already checked)."""
    # THE USER'S TASK GOES FIRST, and the standing notice only on the FIRST turn.
    #
    # Claude gets these through --append-system-prompt, a real system channel.
    # Codex has no such flag, so they are inlined into the prompt — and inlining
    # them AHEAD of the task, on EVERY turn, broke the agent outright. Observed
    # verbatim: the user asked four times for a restaurant website and got back
    # "That's noted as a standing instruction — I'll verify changes by actually
    # running them... What would you like me to work on?" and then "You've sent
    # that instruction three times now... I won't be acting on anything until you
    # give me the actual task."
    #
    # It was answering the NOTICE, because the notice was the opening line of
    # every message and the real request read as trailing context. Repeating it
    # each turn made it look like the user kept sending the same instruction.
    #
    # So: task first, notice appended and clearly marked as ancillary, and only
    # while there is no thread to resume — `resume` already carries the earlier
    # turns, so re-sending it is pure noise.
    if sess.native_session_id:
        addition = ""
    else:
        addition = _system_prompt_addition(
            text, has_brief=write_task_brief(sess.project_dir, text))
    prompt = (text + "\n\n---\n(Standing instruction for this session: " + addition + ")") \
        if addition else text
    base = ["exec"]
    # Only Max/Swarm touch argv; Normal keeps the shipped shape byte for byte
    # and lets config.toml's model = "auto" decide, exactly as before.
    quality = getattr(sess, "quality", "normal")
    if quality in ("max", "swarm") and _hub_backs("codex"):
        base += ["--model", _hub_model_for(quality)]
    if sess.native_session_id:
        base += ["resume", sess.native_session_id, "--json",
                 "--dangerously-bypass-approvals-and-sandbox", prompt]
    else:
        base += ["--json", "--dangerously-bypass-approvals-and-sandbox",
                 "--skip-git-repo-check", prompt]
    return _launcher(bin_path) + base


def _build_argv_opencode(sess: "_Session", bin_path: str, text: str):
    """OpenCode agentic invocation (opencode-ai 1.18.11, live-verified).

    Turn 1:  opencode run --format json <prompt>
    Turn 2+: opencode run --format json --session <sessionID> <prompt>

    The prompt is POSITIONAL, like codex. The project dir is the subprocess cwd
    (set by send_message's Popen), which is also where opencode looks for a
    project-local opencode.json -- so a project configured to talk to this hub
    just works.

    No --append-system-prompt equivalent exists, so the standing notice is
    inlined into the prompt AFTER the task and only on the first turn, for
    exactly the reason recorded in _build_argv_codex: leading with the notice
    made the agent answer the notice instead of the user."""
    if sess.native_session_id:
        addition = ""
    else:
        addition = _system_prompt_addition(
            text, has_brief=write_task_brief(sess.project_dir, text))
    prompt = (text + "\n\n---\n(Standing instruction for this session: " + addition + ")") \
        if addition else text
    args = ["run", "--format", "json"]
    # opencode wants provider/model. NOT gated on _hub_backs: unlike claude and
    # codex, opencode is not a subscription -- it brings no provider of its own
    # and is signed in to whatever the user configured, while the hub-seeded
    # config is what points it here (see _seed_opencode_config). Normal still
    # sends no flag at all, so a project's own opencode.json keeps winning by
    # default; asking for Max or Swarm is an explicit override of it.
    quality = getattr(sess, "quality", "normal")
    if quality in ("max", "swarm"):
        args += ["--model", "free-llm-hub/" + _hub_model_for(quality)]
    if sess.native_session_id:
        args += ["--session", sess.native_session_id]
    args += [prompt]
    return _launcher(bin_path) + args


def _parse_opencode_json(stdout, stderr, returncode):
    """Parse `opencode run --format json` JSONL -> (text, session_id, detail).

    Every event carries sessionID, so the id for `--session` comes from the
    first one that has it. The reply is the LAST `text` part: a turn that used
    tools emits step_start / tool_use / step_finish and then a fresh step whose
    text part is the actual answer."""
    text_parts, session_id = [], None
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue                       # log noise interleaved on stdout
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        if not session_id and isinstance(ev.get("sessionID"), str):
            session_id = ev["sessionID"]
        part = ev.get("part") or {}
        if ev.get("type") == "text" and isinstance(part.get("text"), str):
            if part["text"].strip():
                text_parts.append(part["text"])
        elif ev.get("type") == "error":
            err = ev.get("error") or {}
            data = err.get("data") or {}
            msg = data.get("message") or err.get("name") or "opencode reported an error"
            return None, session_id, _sanitize(str(msg), 400)
    if text_parts:
        return text_parts[-1], session_id, None
    if returncode != 0:
        detail = _sanitize((stderr or stdout or "").strip()[-400:], 400)
        return None, session_id, detail or ("opencode exited with %s" % returncode)
    return None, session_id, "opencode produced no reply."


def _opencode_stream_events(line):
    """One `opencode run --format json` line -> normalized event dicts."""
    line = (line or "").strip()
    if not line.startswith("{"):
        return []
    try:
        ev = json.loads(line)
    except ValueError:
        return []
    if not isinstance(ev, dict):
        return []
    out = []
    sid = ev.get("sessionID")
    if isinstance(sid, str) and sid:
        out.append({"_native": sid})       # idempotent; the session keeps the first
    etype = ev.get("type")
    part = ev.get("part") or {}
    if etype == "tool_use":
        tool = part.get("tool") or part.get("name")
        if isinstance(tool, str) and tool:
            detail = ""
            state = part.get("state") or {}
            inp = state.get("input") if isinstance(state, dict) else None
            if isinstance(inp, dict):
                # Whichever of these a tool carries is the useful bit: which
                # file, or which command. A bare tool name says nothing.
                for k in ("filePath", "path", "command", "pattern", "query"):
                    if isinstance(inp.get(k), str) and inp[k]:
                        detail = " " + inp[k][:160]
                        break
            out.append({"event": "tool", "text": tool + detail})
    elif etype == "text":
        txt = part.get("text")
        if isinstance(txt, str) and txt.strip():
            out.append({"event": "message", "text": txt})
            out.append({"_final": txt})
    elif etype == "error":
        err = ev.get("error") or {}
        data = err.get("data") or {}
        msg = data.get("message") or err.get("name")
        if isinstance(msg, str) and msg:
            out.append({"event": "notice", "text": msg})
    return out


def _parse_codex_json(stdout, stderr, returncode):
    """Parse `codex exec --json` JSONL stdout -> (text, native_session_id, detail).

    `text` is None on failure. Collects the LAST agent_message as the reply and
    the thread.started id for --resume. Non-JSON log noise interleaved on stdout
    (e.g. "Reading additional input from stdin...") is skipped. `item.type ==
    "error"` events are notices (model-metadata / service-tier warnings, often
    repeated once per retry) -- kept only as a FALLBACK.

    MEASURED (an unauthenticated isolated copy -- isolation is new the same day
    this was found): the authoritative failure comes from a `turn.failed` event,
    a single clean line --

        {"type":"turn.failed","error":{"message":"unexpected status 401
         Unauthorized: Missing bearer or basic authentication in header, ..."}}

    -- which this did not read at all. The fallback WAS reached (raw stderr),
    but stderr repeats the same "HTTP error: 401 Unauthorized" line once per
    reconnect attempt (five of them) preceded by boilerplate, so truncating to
    a fixed length for display cut the string apart mid-word ("HTTP error: 4")
    before it ever completed "401" -- which is exactly the substring the
    auth-error check looks for. turn.failed is one clean sentence; prefer it."""
    native_id = None
    final_text = None
    turn_failed = None
    last_error = None
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        etype = ev.get("type")
        if etype == "thread.started":
            tid = ev.get("thread_id")
            if isinstance(tid, str) and tid:
                native_id = tid
        elif etype == "turn.failed":
            msg = (ev.get("error") or {}).get("message")
            if isinstance(msg, str) and msg:
                turn_failed = msg
        elif etype in ("item.completed", "item.started"):
            item = ev.get("item") or {}
            itype = item.get("type")
            if itype == "agent_message":
                txt = item.get("text")
                if isinstance(txt, str) and txt.strip():
                    final_text = txt
            elif itype == "error":
                msg = item.get("message")
                if isinstance(msg, str) and msg and not _is_benign_codex_notice(msg):
                    last_error = msg
    if final_text is not None:
        return _sanitize(final_text), native_id, None
    detail = turn_failed or last_error or (stderr or "").strip() or (
        "codex exited %d with no agent message." % returncode)
    return None, native_id, _sanitize(str(detail), 500)


def _claude_result_text(ev):
    """The text of a claude `result`-shaped event: normally the "result"
    string, but an EXECUTION failure -- a --resume id the current profile has
    never seen, for one -- comes back with NO "result" field at all, only an
    "errors" list.

    MEASURED (isolation gave every CLI a fresh, empty profile the same day
    this was found): --resume <unknown-id> against the isolated config produces

        {"type":"result","subtype":"error_during_execution","is_error":true,
         "errors":["No conversation found with session ID: <id>"], ...}

    -- no "result" key whatsoever. Reading only data.get("result") made that
    shape invisible: result_text stayed None, detail stayed None, and the
    fallback "claude produced no reply." replaced a perfectly good reason with
    a useless one. Returns None if neither field has usable text."""
    res = ev.get("result")
    if isinstance(res, str) and res:
        return res
    errs = ev.get("errors")
    if isinstance(errs, list):
        joined = "; ".join(str(e) for e in errs if e)
        if joined:
            return joined
    return None


def _parse_claude_json(stdout, stderr, returncode):
    """-> (text, native_session_id, detail). `text` is None on any failure."""
    raw = (stdout or "").strip()
    if not raw:
        err = _sanitize((stderr or "").strip(), 500)
        return None, None, ("claude exited %d with no output. %s" % (returncode, err)).strip()
    try:
        data = json.loads(raw)
    except ValueError:
        if returncode == 0:
            # Not JSON, but the process succeeded -- surface it verbatim rather
            # than silently discarding a real answer over a parsing hiccup.
            return _sanitize(raw), None, None
        return None, None, _sanitize(raw, 500)
    if not isinstance(data, dict):
        return None, None, "Unexpected JSON shape from claude."
    native_id = data.get("session_id") if isinstance(data.get("session_id"), str) else None
    text = _claude_result_text(data)
    if data.get("is_error"):
        msg = text or data.get("error") or "unknown error"
        return None, native_id, _sanitize(str(msg), 500)
    if not text:
        return None, native_id, "claude returned no result text."
    return _sanitize(text), native_id, None


# --------------------------------------------------------------------------- #
# Binary-identity safety check -- see module docstring for the GPT-proxy risk
# this guards against. Claude-only (codex's local shim on this machine is a
# confirmed-safe passthrough), and only the very first turn of a session (a
# wrapper that reroutes turn 1 would reroute every turn -- no need to re-pay
# the subprocess cost every time).
# --------------------------------------------------------------------------- #

_VERSION_CHECK_TIMEOUT = 10  # seconds -- a plain `--version` call, must stay fast
_EXPECTED_CLAUDE_VERSION_MARKER = "Claude Code"
# Fail-closed status when the resolved "claude" binary's `--version` output does
# NOT contain _EXPECTED_CLAUDE_VERSION_MARKER. Deliberately distinct from the
# generic 502 (CLI ran and failed) -- 502 means "your CLI/subscription had a
# problem", this means "the hub refused to trust this binary at all", and a
# caller/UI needs to tell those apart.
_BINARY_IDENTITY_FAIL_STATUS = 500


def _should_check_binary_identity(sess: "_Session") -> bool:
    return sess.cli_id == "claude" and sess.turn_count == 0


def _verify_claude_binary_identity(bin_path):
    """Run `<bin_path> --version` and confirm the output contains the literal
    substring "Claude Code" -- the confirmed real shape (e.g. "2.1.212 (Claude
    Code)"). Returns (ok, detail); `detail` is set only when ok is False. Never
    raises -- any failure to even run the check (missing binary, timeout,
    garbled output) is reported as NOT verified, so the caller fails closed
    rather than proceeding under an unverified binary."""
    try:
        proc = subprocess.run(
            _launcher(bin_path) + ["--version"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=_VERSION_CHECK_TIMEOUT,
            # "claude" by name, not sess.cli_id: this check exists only for
            # claude and runs before any session object is in scope. It also
            # WANTS the isolated config -- the point is to identify the exact
            # binary a turn will run, under the exact environment it will run in.
            env=_agentic_env("claude"))
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return False, ("could not run '%s --version' to verify it's really "
                       "Claude Code (%s)." % (bin_path, exc.__class__.__name__))
    out = (proc.stdout or "") + (proc.stderr or "")
    if _EXPECTED_CLAUDE_VERSION_MARKER not in out:
        return False, ("resolved claude binary does not appear to be Claude "
                       "Code -- a wrapper or shim may be intercepting it "
                       "(`%s --version` did not contain '%s')."
                       % (bin_path, _EXPECTED_CLAUDE_VERSION_MARKER))
    return True, None


def send_message(session_id, text):
    """Run ONE subprocess turn. Never raises. Returns (status, text, detail):
      200 -> text is the assistant's reply
      400 -> bad input (empty/oversized message)
      403 -> master flag off, OR the CLI reports the subscription session
             itself is the problem (e.g. expired mid-session) -- see
             _looks_like_auth_error(); detail always disambiguates the two
      404 -> no such session
      409 -> a turn is already running for this session
      499 -> the turn was stopped via stop_session()
      500 -> the hub refused to trust the resolved "claude" binary: its
             `--version` output didn't contain "Claude Code" (checked once, on
             the first turn of a session -- see _verify_claude_binary_identity)
      502 -> ran but failed / produced nothing (and it wasn't an auth problem)
      504 -> timed out after the configured turn timeout
    """
    if not _master_on():
        return 403, None, "Agentic chat is turned off (agentic_chat_enabled=False)."
    with _REGISTRY_LOCK:
        sess = _REGISTRY.get(session_id)
    if sess is None:
        return 404, None, "No such agentic session."
    if not isinstance(text, str) or not text.strip():
        return 400, None, "Message text is required."
    if len(text) > _MAX_MESSAGE_CHARS:
        return 400, None, ("Message is %d chars; capped at %d per turn here (keeps the "
                           "command line safely under Windows' ~8191-char limit)."
                           % (len(text), _MAX_MESSAGE_CHARS))
    supported, reason = _SUPPORT.get(sess.cli_id, (False, "unknown CLI"))
    if not supported:
        return 403, None, "%s agentic mode is not currently supported: %s" % (sess.cli_id, reason)
    if not sess.turn_lock.acquire(blocking=False):
        return 409, None, "A turn is already running for this session."
    try:
        bin_path = _resolve_bin(sess.cli_id)
        if not bin_path:
            return 502, None, "'%s' is no longer on PATH." % _CLI_BIN[sess.cli_id]
        if _should_check_binary_identity(sess):
            ok, detail = _verify_claude_binary_identity(bin_path)
            if not ok:
                return _BINARY_IDENTITY_FAIL_STATUS, None, detail
        # One retry, at most, and only for one specific cause: --resume/--session
        # pointing at an id the CURRENT config directory has never heard of.
        # Isolation gave every CLI a FRESH config the day this was found, so
        # any session id captured before that (or from an even earlier reset)
        # is stale against it. See _STALE_RESUME_PATTERNS for the measured
        # per-CLI wording. Losing the user's actual message to a confusing
        # "no conversation found" error is worse than quietly starting the
        # conversation over, so the retry is silent: same text, no resume.
        stale_retry_used = False
        # See the matching comment in send_message_stream: a turn killed for
        # exceeding _TURN_TIMEOUT used to just fail, no retry, the request
        # silently discarded -- reported live. One bounded retry, and if a
        # thread/session id can be salvaged from what the killed process DID
        # produce (codex/opencode stream JSONL from the first line on; claude
        # non-streaming JSON is all-or-nothing and has nothing to salvage),
        # the retry RESUMES instead of starting the whole task over.
        timeout_retry_used = False
        while True:
            was_resume = bool(sess.native_session_id)
            argv = _build_argv(sess, bin_path, text)
            try:
                proc = subprocess.Popen(
                    argv, cwd=sess.project_dir,
                    env=_agentic_env(sess.cli_id, sess.project_dir,
                                      getattr(sess, "quality", "normal"),
                                      getattr(sess, "id", None)),
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace",
                    **_tree_popen_kwargs())
            except (OSError, ValueError) as exc:
                return 502, None, "%s failed to start: %s" % (sess.cli_id, exc.__class__.__name__)
            sess.last_interrupted = False
            with sess.proc_lock:
                sess.proc = proc
            timed_out = False
            try:
                stdout, stderr = proc.communicate(timeout=_TURN_TIMEOUT)
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate(proc)
                try:
                    stdout, stderr = proc.communicate(timeout=_KILL_GRACE)
                except Exception:
                    stdout, stderr = "", ""
            with sess.proc_lock:
                sess.proc = None
            if sess.last_interrupted:
                return 499, None, "Turn was stopped."
            if timed_out:
                salvaged = _best_effort_native_id(sess.cli_id, stdout)
                if salvaged:
                    sess.native_session_id = salvaged
                if not timeout_retry_used:
                    timeout_retry_used = True
                    continue
                return 504, None, ("%s timed out after %ds (retried once)."
                                   % (sess.cli_id, _TURN_TIMEOUT))
            parser = {"codex": _parse_codex_json,
                      "opencode": _parse_opencode_json}.get(sess.cli_id, _parse_claude_json)
            result_text, native_id, detail = parser(stdout, stderr, proc.returncode)
            if (result_text is None and was_resume and not stale_retry_used
                    and _is_stale_resume_error(sess.cli_id, detail)):
                sess.native_session_id = None
                stale_retry_used = True
                continue
            if result_text is None:
                detail = detail or "%s produced no output." % sess.cli_id
                if _looks_like_auth_error(detail):
                    # Isolation means the copy we drive has its own login.
                    # Without this, the message is "not logged in" about a CLI
                    # the user can see is logged in -- true, and impossible to
                    # act on.
                    return 403, None, detail + _auth_help(sess.cli_id)
                return 502, None, detail
            if native_id:
                sess.native_session_id = native_id
            sess.turn_count += 1
            return 200, result_text, None
    finally:
        sess.turn_lock.release()


# --------------------------------------------------------------------------- #
# Live streaming — same one-subprocess-per-turn + tree-kill model as
# send_message(), but stdout is read line-by-line and normalized into progress
# events AS the turn runs, so the dashboard can show the agent working live
# (the commands it runs, its messages) instead of a spinner + a final dump.
# Events carry `_native` (resume id) / `_final` (final reply) internally; only
# keys under "event" are forwarded to the client.
# --------------------------------------------------------------------------- #

def _is_benign_codex_notice(msg):
    """True for codex's own routine per-turn noise, not a real problem.

    MEASURED, reported live, and matches a real captured event already fixed
    in this file's own tests (test_codex_agentic.CODEX_EVENTS, item_1):
    'Model metadata for `auto` not found.' -- sometimes followed by
    'Defaulting to fallback metadata; this can degrade performance and cause
    issues.' as the user saw it live, but the captured fixture shows the
    short form alone is a complete, separate event -- match on the stable
    core, not the longer sentence, or the short form slips through. Fires on
    EVERY codex turn: the hub deliberately writes model="auto" into codex's
    config.toml (see _codex_hub_fallback_text) as the sentinel that tells the
    HUB's own /v1 endpoint to auto-route; codex's own local model-metadata
    table (compiled into its binary, unrelated to hub routing) just has no
    entry literally named "auto". Harmless and not actionable by the user,
    but it fires so early -- often before the first real tool call -- that it
    was the ONLY thing visible in the transcript for long stretches, reading
    as an alarming error instead of the routine noise it is."""
    return isinstance(msg, str) and "model metadata for" in msg.lower() \
        and "not found" in msg.lower()


def _codex_stream_events(line):
    """One `codex exec --json` JSONL line -> list of normalized event dicts."""
    line = (line or "").strip()
    if not line.startswith("{"):
        return []
    try:
        ev = json.loads(line)
    except ValueError:
        return []
    if not isinstance(ev, dict):
        return []
    etype = ev.get("type")
    out = []
    if etype == "thread.started":
        tid = ev.get("thread_id")
        if isinstance(tid, str) and tid:
            out.append({"_native": tid})
    elif etype == "item.started":
        item = ev.get("item") or {}
        if item.get("type") == "command_execution":
            cmd = item.get("command")
            if isinstance(cmd, str) and cmd:
                out.append({"event": "tool", "text": cmd})
    elif etype == "item.completed":
        item = ev.get("item") or {}
        it = item.get("type")
        if it == "agent_message":
            txt = item.get("text")
            if isinstance(txt, str) and txt.strip():
                out.append({"event": "message", "text": txt})
                out.append({"_final": txt})
        elif it == "command_execution":
            ag = item.get("aggregated_output") or item.get("output")
            if isinstance(ag, str) and ag.strip():
                out.append({"event": "output", "text": ag[:4000]})
        elif it == "error":
            msg = item.get("message")
            if isinstance(msg, str) and msg and not _is_benign_codex_notice(msg):
                out.append({"event": "notice", "text": msg})
    elif etype == "turn.failed":
        # The authoritative failure, one clean sentence -- see the matching
        # comment on _parse_codex_json. Without this, a failed turn (e.g. the
        # isolated copy not yet signed in) fell through to raw stderr, which
        # repeats "HTTP error: 401 Unauthorized" once per reconnect attempt and
        # got truncated mid-word before completing "401" -- the exact substring
        # the auth-error check looks for.
        msg = (ev.get("error") or {}).get("message")
        if isinstance(msg, str) and msg:
            out.append({"_final_error": msg})
    return out


def _claude_stream_events(line):
    """One `claude --output-format stream-json` line -> normalized event dicts."""
    line = (line or "").strip()
    if not line.startswith("{"):
        return []
    try:
        ev = json.loads(line)
    except ValueError:
        return []
    if not isinstance(ev, dict):
        return []
    etype = ev.get("type")
    out = []
    if etype == "system" and isinstance(ev.get("session_id"), str):
        out.append({"_native": ev["session_id"]})
    elif etype == "assistant":
        # MEASURED: an auth failure ("Not logged in · Please run /login")
        # arrives as a normal-looking assistant event carrying that text as a
        # content block, distinguished only by a SIBLING field on the outer
        # event -- is_api_error_message: true, error: "authentication_failed".
        # Not checking it meant the failure streamed to the chat exactly like
        # a real (if useless) answer. The terminal "result" event below
        # carries the authoritative text via _final_error; skip this one.
        if ev.get("is_api_error_message"):
            return out
        msg = ev.get("message") or {}
        for block in (msg.get("content") or []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text"):
                out.append({"event": "message", "text": block["text"]})
            elif block.get("type") == "tool_use":
                inp = block.get("input") or {}
                desc = (inp.get("command") or inp.get("file_path") or inp.get("path")
                        or (json.dumps(inp)[:200] if inp else ""))
                out.append({"event": "tool", "text": "%s: %s" % (block.get("name") or "tool", desc)})
    elif etype == "user":
        # MEASURED gap, reported live: this parser only ever surfaced the
        # TOOL CALL ("Bash: npm run build") and never what it actually did --
        # a long-running or silent command left "Working..." as the only
        # visible text for minutes. Claude Code sends a completed tool's
        # result back as a synthetic user turn (the same shape the Anthropic
        # Messages API uses for tool_result), not as an assistant event --
        # codex's parser already has the equivalent (item.completed /
        # command_execution -> "output"); this brings claude to parity with
        # the SAME event name, so the frontend needs no changes.
        msg = ev.get("message") or {}
        for block in (msg.get("content") or []):
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            content = block.get("content")
            if isinstance(content, list):
                text = "".join(b.get("text", "") for b in content
                               if isinstance(b, dict) and b.get("type") == "text")
            elif isinstance(content, str):
                text = content
            else:
                text = ""
            if text.strip():
                out.append({"event": "output", "text": text[:4000]})
    elif etype == "result":
        if isinstance(ev.get("session_id"), str):
            out.append({"_native": ev["session_id"]})
        text = _claude_result_text(ev)
        if text:
            if ev.get("is_error"):
                # A failure with real text (an execution error, an errors[]
                # array) is not a reply -- send_message_stream treats
                # _final_error as a terminal ERROR, distinct from _final.
                out.append({"_final_error": text})
            else:
                out.append({"_final": text})
                out.append({"event": "message", "text": text})
    return out


def _last_assistant_text_from_transcript(path):
    """The last real assistant text block in a claude session's OWN persisted
    JSONL transcript (~/.claude-style projects/<encoded-path>/<session-id>.jsonl
    -- a format DISTINCT from --output-format stream-json, written by the CLI
    itself as it goes, independent of whatever it did or didn't flush to this
    process's stdout pipe). Tolerant of a partial/truncated last line (a kill
    mid-write) and of lines that aren't the shape expected at all -- this is
    read-only forensics on a file this hub does not control the format of,
    never allowed to raise."""
    last_text = None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(ev, dict) or ev.get("type") != "assistant":
                    continue
                msg = ev.get("message")
                if not isinstance(msg, dict) or msg.get("role") != "assistant":
                    continue
                for block in (msg.get("content") or []):
                    if (isinstance(block, dict) and block.get("type") == "text"
                            and block.get("text")):
                        last_text = block["text"]
    except OSError:
        return None
    return last_text


def _recover_text_from_claude_transcript(config_dir, native_id):
    """LAST RESORT when the hub's own stdout capture came back with NOTHING
    usable at all (no clean result event, no streamed message text either) --
    MEASURED TWICE on real production turns that hit an unresolvable
    permission gate: the reply the user needed was sitting in claude's own
    on-disk session log the entire time. Matches by native_id (session_id),
    the one stable identifier already captured from the stream's own init
    event, via a filename search -- NOT by reconstructing Claude Code's
    internal project-path encoding scheme, which is undocumented and not
    this hub's to depend on. Never raises; returns None on anything short of
    a clean recovery, same contract as every other fallback in this file."""
    if not native_id or not config_dir:
        return None
    target = native_id + ".jsonl"
    try:
        base = os.path.join(config_dir, "projects")
        if not os.path.isdir(base):
            return None
        for root, _dirs, files in os.walk(base):
            if target in files:
                return _last_assistant_text_from_transcript(os.path.join(root, target))
    except OSError:
        return None
    return None


def send_message_stream(session_id, text):
    """Generator: run ONE turn, yielding normalized progress events as they occur.
    Same validation / turn-lock / tree-kill model as send_message(). Always ends
    with exactly one {"event":"done",...}, {"event":"error",...}, or
    {"event":"stopped"}. Never raises."""
    def err(status, detail, code=None):
        ev = {"event": "error", "status": status, "detail": detail}
        if code:
            ev["code"] = code
            # The picker's CURRENT selection can drift from the CLI this
            # session actually runs (changed after Start, before the next
            # message) -- send the session's own cli_id explicitly rather
            # than let the frontend assume the two still match.
            ev["cli"] = sess.cli_id
        return ev
    if not _master_on():
        yield err(403, "Agentic chat is turned off (agentic_chat_enabled=False)."); return
    with _REGISTRY_LOCK:
        sess = _REGISTRY.get(session_id)
    if sess is None:
        yield err(404, "No such agentic session."); return
    if not isinstance(text, str) or not text.strip():
        yield err(400, "Message text is required."); return
    if len(text) > _MAX_MESSAGE_CHARS:
        yield err(400, "Message is %d chars; capped at %d per turn." % (len(text), _MAX_MESSAGE_CHARS)); return
    supported, reason = _SUPPORT.get(sess.cli_id, (False, "unknown CLI"))
    if not supported:
        yield err(403, "%s agentic mode is not supported: %s" % (sess.cli_id, reason)); return
    if not sess.turn_lock.acquire(blocking=False):
        yield err(409, "A turn is already running for this session."); return
    proc = None
    timer = None
    timed_out = [False]
    stderr_buf = []
    try:
        # Say what this project needs BEFORE running anything, in the chat, in
        # plain language and with the download page. Without it a machine
        # missing Node fails somewhere inside npm with "[WinError 2] The system
        # cannot find the file specified" -- true, and useless to someone who
        # has never installed a toolchain. Once per session: it is a fact about
        # the computer, not about the turn.
        if not sess.tools_notified:
            sess.tools_notified = True
            try:
                notice = workspace.missing_tools_message(sess.project_dir)
            except Exception:                                    # noqa: BLE001
                notice = None
            if notice:
                yield {"event": "notice", "text": notice}
        bin_path = _resolve_bin(sess.cli_id)
        if not bin_path:
            yield err(502, "'%s' is no longer on PATH." % _CLI_BIN[sess.cli_id]); return
        if _should_check_binary_identity(sess):
            ok, detail = _verify_claude_binary_identity(bin_path)
            if not ok:
                yield err(_BINARY_IDENTITY_FAIL_STATUS, detail); return

        parse = {"codex": _codex_stream_events,
                 "opencode": _opencode_stream_events}.get(sess.cli_id, _claude_stream_events)
        # Same one-retry, stale-resume-only recovery as send_message() -- see
        # the comment there for the measured per-CLI error text this catches.
        # The retry is safe to run silently here too: the failure happens at
        # num_turns=0, before any tool ran or any text streamed, so nothing
        # user-visible has to be un-shown.
        stale_retry_used = False
        # A SEPARATE one-shot retry for hitting _TURN_TIMEOUT: reported live,
        # a real turn was killed for exceeding it and just stopped -- no
        # retry, no resume, the user's request silently discarded. Free-tier
        # agentic turns are legitimately slow (measured: ~5 minutes for a
        # TRIVIAL one-file write), so a kill on the first sign of a long turn
        # is the wrong default. This one is NOT silent, unlike stale-resume:
        # real tool calls and real text may already have streamed to the
        # user by the time it fires, so staying quiet about starting over
        # would be its own kind of confusing.
        timeout_retry_used = False
        while True:
            was_resume = bool(sess.native_session_id)
            argv = _build_argv(sess, bin_path, text, stream=True)
            child_env = _agentic_env(sess.cli_id, sess.project_dir,
                                     getattr(sess, "quality", "normal"),
                                     getattr(sess, "id", None))
            try:
                proc = subprocess.Popen(
                    argv, cwd=sess.project_dir, env=child_env,
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                    **_tree_popen_kwargs())
            except (OSError, ValueError) as exc:
                yield err(502, "%s failed to start: %s" % (sess.cli_id, exc.__class__.__name__)); return
            sess.last_interrupted = False
            with sess.proc_lock:
                sess.proc = proc

            stderr_buf[:] = []
            drain_done = threading.Event()
            # Drain stderr in a background thread so a full stderr pipe can
            # never deadlock the stdout read loop (codex/claude both log a lot
            # to stderr).
            def _drain():
                try:
                    for l in proc.stderr:
                        stderr_buf.append(l)
                        if len(stderr_buf) > 200:
                            del stderr_buf[0]
                except Exception:
                    pass
                finally:
                    drain_done.set()
            threading.Thread(target=_drain, daemon=True).start()

            timed_out[0] = False
            def _kill_on_timeout():
                timed_out[0] = True
                _terminate(proc)
            timer = threading.Timer(_TURN_TIMEOUT, _kill_on_timeout)
            timer.daemon = True
            timer.start()

            native_id = None
            final_text = None
            final_error = None
            last_message_text = None
            try:
                for line in proc.stdout:
                    for e in parse(line):
                        if "_native" in e:
                            native_id = e["_native"]
                        if "_final" in e:
                            final_text = e["_final"]
                        if "_final_error" in e:
                            final_error = e["_final_error"]
                        if e.get("event") == "message" and e.get("text"):
                            last_message_text = e["text"]
                        if e.get("event"):
                            yield e
            except Exception as exc:
                # Was a silent `pass` -- a decode error or broken pipe here
                # left zero trace, indistinguishable from "the process just
                # legitimately produced nothing." Log it so the next "no
                # reply" report can tell those two apart.
                _log.warning("agentic stream read failed (session=%s cli=%s): %r",
                             sess.id, sess.cli_id, exc)
            try:
                proc.wait(timeout=_KILL_GRACE)
            except Exception:
                pass
            if timer:
                timer.cancel()
            # The stdout loop above only returns once the process has closed
            # stdout, which happens at or after it closes stderr too -- but
            # the DRAIN THREAD reading stderr is a separate scheduling unit,
            # so without this wait, reading stderr_buf next can race it and
            # see an empty list even though the text WAS written. Bounded
            # short: this is a thread that is already almost certainly done,
            # not a process we are waiting on.
            drain_done.wait(timeout=0.5)
            if final_text is None and final_error is None and last_message_text:
                # The process ended (killed, crashed, or exited oddly) WITHOUT
                # ever emitting a clean terminal result/summary event -- but
                # real assistant text WAS already streamed first. MEASURED: a
                # claude turn hit an unresolvable Write-permission gate
                # (--dangerously-skip-permissions did not stop an interactive-
                # style prompt this one time; stdin is closed so nothing could
                # ever answer it), the model asked "Should I proceed and write
                # the files?" as its last streamed line, and the underlying
                # process then never produced a closing `type:"result"` event
                # -- so final_text stayed None and a real, useful reply was
                # reported as "produced no reply" and silently discarded.
                final_text = last_message_text
            if final_text is None and final_error is None and native_id and sess.cli_id == "claude":
                # LAST RESORT, one tier further: MEASURED TWICE on real
                # production turns, the hub's OWN stdout capture came back
                # with NOTHING usable at all (no _final, no last_message_text
                # either -- the permission-denial recovery text never even
                # reached this process's pipe, likely lost with whatever
                # buffered-but-unflushed stdout the child held when it ended)
                # while claude's OWN persisted session transcript on disk had
                # the real reply the whole time. Recovered by native_id (the
                # one stable id already captured from the stream's own init
                # event) rather than by reconstructing Claude Code's internal
                # project-path encoding, which is undocumented and not this
                # hub's to depend on.
                recovered = _recover_text_from_claude_transcript(
                    child_env.get("CLAUDE_CONFIG_DIR"), native_id)
                if recovered:
                    final_text = recovered
            if sess.last_interrupted:
                yield {"event": "stopped"}; return
            if timed_out[0]:
                # Save whatever thread/session id was already captured from
                # the stream BEFORE this decision -- without it, native_id sat
                # in a local variable this whole time and the code below the
                # loop (the only place that normally saves it) is never
                # reached on a timeout, so a retry -- or even just the user's
                # own next message -- restarted the whole task from zero
                # instead of continuing the one already under way.
                if native_id:
                    sess.native_session_id = native_id
                if not timeout_retry_used:
                    timeout_retry_used = True
                    yield {"event": "notice",
                          "text": ("Still working after %ds — %s." %
                                   (_TURN_TIMEOUT,
                                    "resuming" if native_id else "trying again"))}
                    continue
                yield err(504, "%s timed out after %ds (retried once)."
                         % (sess.cli_id, _TURN_TIMEOUT)); return

            stderr_text = _sanitize("".join(stderr_buf).strip(), 400)
            if final_text is None and final_error is None:
                # Black-box recorder: every fallback tier (last-streamed-text,
                # disk-transcript recovery) already ran above and STILL came
                # up empty. Log the raw state so a real "no reply" report can
                # be diagnosed from THIS run's own log lines instead of
                # re-guessing from the CLI's on-disk transcript after the
                # fact -- that transcript is a different artifact and has
                # already been observed to show clean, complete replies on a
                # turn the hub itself reported as empty.
                try:
                    _log.warning(
                        "no-reply fallback exhausted (session=%s cli=%s): "
                        "native_id=%r last_message_text=%r returncode=%r "
                        "timed_out=%r stderr_buf=%r",
                        getattr(sess, "id", "?"), sess.cli_id, native_id,
                        (last_message_text[:200] if last_message_text else last_message_text),
                        proc.returncode, timed_out[0], "".join(stderr_buf)[:2000])
                except Exception:
                    pass
            stale_source = final_error or stderr_text
            if (final_text is None and was_resume and not stale_retry_used
                    and _is_stale_resume_error(sess.cli_id, stale_source)):
                sess.native_session_id = None
                stale_retry_used = True
                continue

            if native_id:
                sess.native_session_id = native_id
            if final_text is None:
                detail = final_error or stderr_text or ("%s produced no reply." % sess.cli_id)
                if _looks_like_auth_error(detail):
                    # A structured signal, not prose-sniffing: the frontend
                    # offers a one-click Sign in button on this code+cli pair
                    # instead of pattern-matching the message text (which is
                    # meant for a human, and free to change).
                    yield err(403, detail + _auth_help(sess.cli_id),
                             code="cli_not_signed_in"); return
                yield err(502, detail); return
            sess.turn_count += 1
            yield {"event": "done", "text": _sanitize(final_text), "native": native_id}
            return
    finally:
        if timer:
            timer.cancel()
        with sess.proc_lock:
            if sess.proc is proc:
                sess.proc = None
        sess.turn_lock.release()


def send_message_stream_durable(session_id, text):
    """Same external contract as send_message_stream (a generator yielding the
    same normalized events, always ending the way that one does) but the real
    turn runs on its own background thread instead of being driven by whoever
    is reading this generator.

    THE BUG THIS FIXES (found live): send_message_stream is a plain generator.
    app.py's SSE route only calls next() on it -- and only reaches the code
    that persists the agent's reply -- when Flask's WSGI layer is actively
    writing to a connected client. If that client goes away mid-turn (tab
    closed, laptop slept, network dropped) nothing ever pulls the generator
    again: the underlying CLI process keeps running and genuinely finishes,
    but the hub never notices, so a completed reply was silently thrown away.
    Measured live: a real turn ran two full agent replies and exited clean,
    with nothing in the wrong beyond the tab that started it going away --
    and none of it was ever saved.

    A background thread has no such dependency -- it keeps calling next() on
    its own regardless of who, if anyone, is reading the queue it feeds. That
    thread is what now persists the reply, so a turn survives being
    unwatched. The queue itself still relays events live, so a client that
    STAYS connected sees no difference at all."""
    sess_info = get_session(session_id)
    q = queue.Queue()

    def _run():
        final_reply = None
        try:
            for ev in send_message_stream(session_id, text):
                q.put(ev)
                if ev.get("event") == "done":
                    final_reply = ev.get("text")
        finally:
            if sess_info and final_reply:
                try:
                    after = get_session(session_id) or {}
                    agentic_history.record_turn(
                        session_id, sess_info["cli"], sess_info["project_dir"],
                        "agent", final_reply,
                        native_session_id=after.get("native_session_id"))
                except Exception:
                    pass
            q.put(None)          # sentinel: no more events, thread is done

    threading.Thread(target=_run, daemon=True).start()
    while True:
        ev = q.get()
        if ev is None:
            return
        yield ev


def _tree_popen_kwargs():
    """Extra Popen kwargs so a subsequent stop_session() can kill the WHOLE
    process tree (see _signal_tree) instead of only the immediate child."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"preexec_fn": os.setsid}


def stop_session(session_id) -> bool:
    """Interrupt the CURRENTLY-running turn for this session, if any. Returns
    whether anything was actually running to stop. Never raises. Does NOT
    depend on the master flag: a kill switch must still be able to kill."""
    with _REGISTRY_LOCK:
        sess = _REGISTRY.get(session_id)
    if sess is None:
        return False
    with sess.proc_lock:
        proc = sess.proc
    if proc is None or proc.poll() is not None:
        return False
    sess.last_interrupted = True
    _terminate(proc)
    return True


def get_session(session_id):
    """Status dict for one session, or None if it doesn't exist. Never raises."""
    with _REGISTRY_LOCK:
        sess = _REGISTRY.get(session_id)
    if sess is None:
        return None
    with sess.proc_lock:
        proc = sess.proc
    running = bool(proc is not None and proc.poll() is None)
    return {
        "session_id": sess.id,
        "cli": sess.cli_id,
        "quality": getattr(sess, "quality", "normal"),
        "project_dir": sess.project_dir,
        "turn_count": sess.turn_count,
        "currently_running": running,
        "created_at": sess.created_at,
        "has_native_session": bool(sess.native_session_id),
        # The CLI's own thread id. Recorded with the transcript so a
        # conversation can be CONTINUED after a hub restart -- sessions live in
        # memory, but the CLI's thread does not, and this is the handle to it.
        # Not a secret: a local id for a local process, meaningless elsewhere.
        "native_session_id": sess.native_session_id,
    }


def set_quality(session_id, quality):
    """Change a LIVE session's model quality. Returns the stored value, or None
    for an unknown session.

    Safe to change mid-conversation because the CLI is re-spawned for every
    turn (see the two _agentic_env call sites) -- the child's environment, and
    therefore ANTHROPIC_MODEL, is built fresh each time. The turn already in
    flight keeps the mode it started with; the next one picks this up."""
    if quality not in ("normal", "max", "swarm"):
        return None
    with _REGISTRY_LOCK:
        sess = _REGISTRY.get(session_id)
        if sess is None:
            return None
        sess.quality = quality
        return quality


def list_sessions():
    """All active sessions (for the dashboard to restore UI state). Never raises."""
    with _REGISTRY_LOCK:
        ids = list(_REGISTRY.keys())
    out = []
    for sid in ids:
        row = get_session(sid)
        if row is not None:
            out.append(row)
    return out


def end_session(session_id) -> bool:
    """Stop the session if running, then drop it from the registry entirely
    (distinct from stop_session(), which only interrupts the current turn but
    keeps the session resumable). Returns whether a session existed to end."""
    with _REGISTRY_LOCK:
        existed = session_id in _REGISTRY
    if not existed:
        return False
    stop_session(session_id)
    with _REGISTRY_LOCK:
        _REGISTRY.pop(session_id, None)
    return True
