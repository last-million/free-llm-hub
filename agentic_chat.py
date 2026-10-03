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

import copy
import collections
import contextlib
import inspect
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

import agent_servers
import agentic_history
import memory

# The effort tiers a session can run in; the list itself lives with the
# conversation store, which is where a tier has to survive a restart.
QUALITIES = agentic_history.QUALITIES
import model_categories
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

# A FROZEN TURN IS NOT A SLOW ONE.
#
# _TURN_TIMEOUT is a WALL CLOCK on the whole turn, and a turn that legitimately
# builds something can run for twenty minutes. But a CLI wedged on a bash call
# that never returns produces NOTHING, and the wall clock cannot tell those two
# apart -- so a freeze cost the full thirty minutes of silence before anything
# happened, and the user watched a spinner for half an hour.
#
# REPORTED: "persistence even if he use bash etc, if he freeze etc, he should
# have python think and continue, and always work should be finished till the
# end."
#
# Silence is the signal. A working agent emits events continuously -- a tool
# call, a message, a token. Nothing at all for this long means wedged, not
# thinking, and the recovery already exists: the same resume path a timeout
# takes, which hands the CLI its own thread id back and continues the work
# rather than restarting it.
#
# Generous on purpose: the swarm's own per-hop deadline is 360s, and a single
# model call inside a turn can legitimately be quiet for minutes.
_STALL_TIMEOUT = int(os.environ.get("AGENTIC_CHAT_STALL", "420") or "420")


def _probe_after_setting(raw):
    """AGENTIC_CHAT_SERVER_PROBE: seconds of silence before the early server
    check; 0 turns it off. Bounded to 15..600 so a typo cannot make it fire
    on every pause or never."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 60.0
    return 0.0 if v <= 0 else min(600.0, max(15.0, v))


# THE EARLY SERVER CHECK. MEASURED live 2026-09-27: an opencode turn whose shell
# was blocked on a server it had started in the foreground waited the full
# _STALL_TIMEOUT twice (2 x 420 s) before anything was said -- and opencode
# reports a command only once it returned, so its stream never shows what is
# blocking. After this much silence the watchdog asks
# agent_servers.early_server_diagnosis, every _SERVER_PROBE_EVERY seconds until
# the stall deadline, whether the shell tool is waiting on a server (and on
# nothing else); a yes resumes at once with the server instruction. A long
# test run, install, build or a model thinking is never a yes.
_SERVER_PROBE_AFTER = _probe_after_setting(os.environ.get("AGENTIC_CHAT_SERVER_PROBE", "60"))
_SERVER_PROBE_EVERY = 10.0
# The watchdog's longest sleep between checks.
_WATCH_TICK_MAX = 5.0

# A FAILURE THAT IS NOT THE TURN'S FAULT.
#
# MEASURED live: opencode keeps ONE SQLite database for the whole machine
# (~/.local/share/opencode/opencode.db). Two of its processes writing at once
# and the loser gets
#
#     502  Error: Unexpected error / database is locked
#
# and the turn is over before it began. swarm_windows already retries this for
# its workers, but a NORMAL turn had no such protection -- so sending a message
# on /agent while a swarm was running was a 502 for the ordinary path and a
# retry for the orchestrated one, which is backwards: the person watching is
# the one who notices.
#
# REPORTED as: "http 502".
#
# One retry, and only for a failure that says it is temporary. A model that
# refused, a CLI that is not signed in, a prompt that is wrong -- those fail
# the same way twice and retrying spends the tokens again to learn nothing.
_TRANSIENT_FAILURES = (
    "database is locked",           # opencode's shared SQLite store
    "database table is locked",
    "sqlite_busy",
    "resource temporarily unavailable",
    "being used by another process",
    "ebusy",
    "eagain",
)


# Long enough for the other writer to finish its transaction, short enough
# that a person does not read it as a hang.
_TRANSIENT_RETRY_WAIT = 3.0


def _looks_transient(detail):
    low = str(detail or "").lower()
    return any(marker in low for marker in _TRANSIENT_FAILURES)

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

# ...BUT ONLY ON THE SHELL PATH. Every number above is arithmetic against
# cmd.exe's ~8191-character command line, and cmd.exe is no longer in the way:
# _launcher resolves the .cmd shim to the real program and runs it directly
# (see _resolve_shim). CreateProcess allows 32,767 characters, four times what
# the shim did.
#
# REPORTED: "some requests got http 400, it's like no answer". A message over
# the cap is refused with a 400 and the turn never runs -- which is exactly
# what pasting a long prompt, a stack trace or a file into the agent looks
# like from the outside.
#
# Measured worst case on the direct path: 8071 characters of argv with a
# 5600-char message, so roughly 2,500 of overhead. 24,000 + 2,500 leaves ~6,000
# characters of headroom under the real limit.
#
# The shell number stays for the fallback: a shim this hub cannot read still
# goes through cmd.exe, and the old cap is still the true one there.
_MAX_MESSAGE_CHARS_DIRECT = 24000


def max_message_chars(cli_id=None):
    """The cap for THIS cli, which depends on how its binary is launched."""
    if os.name != "nt":
        return _MAX_MESSAGE_CHARS_DIRECT
    try:
        path = _resolve_bin(cli_id) if cli_id else None
        if not path:
            return _MAX_MESSAGE_CHARS
        argv = _launcher(path)
        shell = os.path.basename(argv[0]).lower() in ("cmd.exe", "cmd")
        return _MAX_MESSAGE_CHARS if shell else _MAX_MESSAGE_CHARS_DIRECT
    except Exception:                                            # noqa: BLE001
        return _MAX_MESSAGE_CHARS       # unknown: keep the safe number

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
                 quality: str = "normal", session_id: str = None,
                 mode: str = None) -> dict:
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
                # THE SESSION ID, PER PROCESS. claude and codex are pointed at
                # <hub>/build/<session_id> through an env var and a per-session
                # config file; opencode's provider lives in ONE seeded file
                # shared by every session, so its turns reached the hub with no
                # session on them at all. MEASURED 2026-09-12 on a multi-session
                # run: every worker request was "source: cli, project: None",
                # which also meant a conversation's own model rules -- its
                # allow/block lists, its category edits, its mode fallback --
                # never applied to an opencode session, the one CLI that runs
                # against this hub by default.
                #
                # OPENCODE_CONFIG_CONTENT is a local-scope config opencode deep-
                # merges over the files, so ONE option can be overridden per
                # process without touching the shared seed. Verified live: a
                # run with this set showed up as "source: build".
                if session_id:
                    env["OPENCODE_CONFIG_CONTENT"] = json.dumps({
                        "provider": {_OPENCODE_PROVIDER_ID: {"options": {
                            "baseURL": _hub_base_url(session_id) + "/v1"}}}})
            elif cli_id == "claude":
                _apply_claude_hub_fallback(env, path, quality, session_id, mode)
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


def _apply_claude_hub_fallback(env, config_home, quality="normal", session_id=None,
                               mode=None):
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
        _unseed_claude_picker(config_home)   # hub ids would now go to Anthropic
        return
    env["ANTHROPIC_BASE_URL"] = _hub_base_url(session_id)
    env["ANTHROPIC_AUTH_TOKEN"] = config.get_local_api_key() or "free-llm-hub"
    # "best" is `auto` that never drops to the cheap tier (app.py's
    # _is_orchestrate accepts it; _route_by_difficulty lifts `simple` when it
    # sees it). Sent as the MODEL so the choice travels with every turn the CLI
    # makes, including the small intermediate ones -- there is no other channel
    # back to the hub, since the CLI subprocess is an ordinary API client and
    # carries no session identity of its own.
    # One mapping, not a second copy of it: a mode id missing here would be a
    # turn routed as plain `auto` while the UI said otherwise.
    # claude_model_id: Claude Code swallows "best" as its own alias.
    env["ANTHROPIC_MODEL"] = claude_model_id(_hub_model_for(quality, mode))
    # The same mapping Connect writes for a real install (claude_hub_env), with
    # ONE difference: opus maps to this session's own tier, not `max`. A normal
    # turn is launched with `--model opus` (_MODEL_ALIAS), so opus->max would
    # silently lift every normal session into the max tier.
    env.update(claude_hub_env(opus=env["ANTHROPIC_MODEL"]))
    # The picker rows are what make these ids known (behavesAs); only a settings
    # file can carry them, so the isolated copy's own settings.json gets them.
    _seed_claude_picker(config_home)


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

# --------------------------------------------------------------------------- #
# DECLARED windows follow the fleet. The two figures above are now only the
# FAIL-SAFE: app.py registers a provider at startup (set_window_provider) that
# returns, per hub id, a window most of the models that id can route to
# actually hold (the 25th percentile of their known windows, capped at what
# three distinct non-relay providers hold -- see app._declared_window_for;
# app._resync_declared_windows carries a changed figure into the configs of
# the CLIs still wired to the hub). This module must not import app (cycle), hence
# the callback. Unregistered, failing, or "too few known" -> the fixed default.
# A writer passes cli=<its CLI>: a CLI whose own compaction follows the usage
# the hub reports is told the REACH window instead (the biggest its tier can
# reach), and the hub scales that usage per request by declared / live window
# (app._STEERED_CLIS, app._reported_prompt_tokens).
# --------------------------------------------------------------------------- #
_DECLARED_WINDOW_MIN = 32000
_DECLARED_WINDOW_MAX = 1000000
_window_provider = None
_window_provider_takes_cli = False


def set_window_provider(fn):
    """Register `fn(model_id) -> int | None` (None/invalid = use the default),
    or `fn(model_id, cli=None)` when the figure depends on WHICH CLI is told
    (app: the CLIs steered by live window get the reach window, see
    app._STEERED_CLIS). Pass None to unregister."""
    global _window_provider, _window_provider_takes_cli
    _window_provider = fn if callable(fn) else None
    takes = False
    if _window_provider is not None:
        try:
            params = inspect.signature(_window_provider).parameters.values()
            takes = any(p.name == "cli" or p.kind == p.VAR_KEYWORD for p in params)
        except (TypeError, ValueError):
            takes = False
    _window_provider_takes_cli = takes


# --------------------------------------------------------------------------- #
# Silence explained by the hub itself: no model could serve the turn
# --------------------------------------------------------------------------- #
# MEASURED 2026-09-28 (session 47a25faa, opencode, ~54K tokens): the CLI's
# requests kept ending 504 at the hub's deadline and it retried them silently,
# so the watchdog said "looks wedged, resuming", resumed into the same outage
# and failed 25 minutes in with no reason. app.py registers a probe over its
# per-session ledger; when the silence is fully explained by requests the hub
# could not serve, the turn stops with that reason instead of resuming.
_upstream_probe = None


def set_upstream_probe(fn):
    """Register `fn(session_id, since_wall_ts) -> dict | None` (see
    app._agent_upstream_probe). Pass None to unregister."""
    global _upstream_probe
    _upstream_probe = fn if callable(fn) else None


def _upstream_outage(sess, since_wall):
    """The probe's report when this session's requests failed to find a model
    since `since_wall` and none succeeded after, else None. Never raises."""
    fn = _upstream_probe
    if fn is None:
        return None
    try:
        rep = fn(getattr(sess, "id", None), since_wall)
    except Exception:                                            # noqa: BLE001
        return None
    if not isinstance(rep, dict) or not rep.get("failures") or rep.get("ok_after"):
        return None
    return rep


def outage_detail(cli_id, rep):
    """The failure text for a turn stopped by _upstream_outage."""
    why = str(rep.get("why") or "").strip()
    n = int(rep.get("failures") or 0)
    return ("No free model could answer this turn: %s request%s in a row found no "
            "model in time%s. Nothing is wrong with your project -- the models that "
            "can hold this conversation were all busy or failing. Send \"continue\" "
            "in a few minutes to pick up where %s stopped."
            % (n, "" if n == 1 else "s", (" (%s)" % why) if why else "", cli_id))


def declared_window(model_id=None, cli=None):
    """The context window to tell a CLI for hub id `model_id` (auto, best,
    a category, a compound like coding-swarm, or None for auto). `cli` names
    the CLI whose config carries it (None = the safe figure every CLI may be
    told). Always an int in [_DECLARED_WINDOW_MIN, _DECLARED_WINDOW_MAX];
    never raises."""
    fn = _window_provider
    if fn is None:
        return _CODEX_CONTEXT_WINDOW
    try:
        if cli is not None and _window_provider_takes_cli:
            v = fn(model_id, cli=cli)
        else:
            v = fn(model_id)
    except Exception:                                            # noqa: BLE001
        return _CODEX_CONTEXT_WINDOW
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        return _CODEX_CONTEXT_WINDOW
    return int(max(_DECLARED_WINDOW_MIN, min(_DECLARED_WINDOW_MAX, int(v))))


def declared_compact_limit(model_id=None, cli=None):
    """When to auto-compact for that id: the same 75% of the declared window
    _CODEX_COMPACT_LIMIT is of _CODEX_CONTEXT_WINDOW."""
    return int(declared_window(model_id, cli=cli) * _CODEX_COMPACT_LIMIT
               // _CODEX_CONTEXT_WINDOW)
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
    _set_top_key("model_context_window", declared_window("auto", cli="codex"),
                 quote=False, keep_existing=True)
    _set_top_key("model_auto_compact_token_limit",
                 declared_compact_limit("auto", cli="codex"),
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


# WHAT A CLI'S MODEL PICKER OFFERS. One list, used by BOTH writers -- this
# seed for the isolated /agent copy, and app._autofix_opencode for a terminal
# opencode connected from the dashboard. It lived in two hardcoded copies and
# fell behind twice: first when swarm was added, then when the category modes
# were, and each time the ids worked perfectly while being invisible in the
# picker, because opencode reads THIS list and never /v1/models.
#
# TWO FAMILIES, and the labels say which. Asked for as "in cli's i want /model
# to select which mode, and /effort for auto, best, swarm". opencode has no
# /effort command and the hub cannot add one -- slash commands are the CLI's
# own UI, and the model list is the only channel the hub has. So the split is
# made where it CAN be made: in the names, so one picker still reads as groups
# whichever order opencode sorts them in.
#
#   EFFORT    how hard to try      auto / best / max / swarm / multi
#   MODE      which kind of model  coding, reasoning, uncensored, ...
#   MODE+EFF  both at once         coding-max, coding-swarm, coding-multi, ...
#
# The third group is why the picker is no longer tiny. codex makes the two
# choices on two screens; opencode has one flat list and can pick only ONE id,
# so "the coding models AND run them as a swarm" has to BE one id --
# "coding-swarm". _split_category_effort in app.py routes it. opencode's model
# picker is a fuzzy finder, so a longer list stays usable: typing "coding swarm"
# filters straight to it. REQUESTED 2026-09-13: "in opencode I want to select
# both the category of models and also the effort mode".
#
# crew, crew-code, crew-research, crew-write, crew-design, team and plan are
# deliberately NOT here. They are multi-stage pipelines aimed at the dashboard,
# and putting all seven in front of the choices a CLI actually makes is noise.
# The hub still answers to them, so typing one by hand still works -- they are
# only absent from the list.
_OPENCODE_EFFORT = {
    "auto":  "effort: auto -- orchestrated, best free model per task",
    "best":  "effort: max -- strongest free models only, never the cheap tier",
    "max":   "effort: max -- strongest free models only, never the cheap tier",
    "swarm": "effort: swarm -- several models per turn, best answer wins",
    "multi": "effort: multi -- phased crew (plan -> work -> review)",
}

# The heavier tiers a category can be paired with (auto is the bare category
# id, so it makes no compound). The value is the tail of the picker label.
_OPENCODE_COMPOUND_TIERS = (
    ("max", "strongest only"),
    ("swarm", "several per turn"),
    ("multi", "phased crew"),
)


# How much output to reserve out of the window above. opencode computes its
# compaction threshold as `context - output`, so this is the headroom a reply is
# allowed before history has to be summarised. Small enough that a session uses
# most of its window, large enough that a long answer is not cut in half.
_HUB_MAX_OUTPUT = 16384


def _opencode_hub_models():
    """{id: {...}} for a CLI picker: the effort tiers, then the modes.

    EVERY ENTRY MUST DECLARE `limit`. It is optional in opencode's schema and
    the hub used to omit it, which reads back as limit.context == 0 -- and the
    auto-compaction check is, verbatim from the shipped binary:

        if(e.model.limit.context===0) return !1;

    i.e. a model with no declared window never compacts. History then grows
    until the turn stops working and the only way out is to quit the session
    and open a new one. REPORTED exactly that way ("the session gets full and I
    should go out from conversation and reopen it again"). The hub itself
    answers an 858K-token session fine -- this was never the gateway refusing,
    it was the CLI never being told when to summarise.

    Modes come from model_categories, the same table the router filters on, so
    a category added there appears in the picker on the next start instead of
    going missing. model_categories is a leaf module with no imports of its own,
    which is what lets this file use it without the app.py cycle the rest of it
    avoids.

    An effort id always wins: "swarm" is both a category name and the swarm
    PIPELINE's id, and the pipeline is the one a CLI has to send.

    `limit.context` is PER ID (declared_window): the window the models behind
    that id actually hold, not one figure for every mode. The output reserve
    stays _HUB_MAX_OUTPUT, capped at a quarter of a small window."""
    def spec(mid, name, attachment=False):
        # A FRESH limit dict per entry: one shared object means a later edit to
        # any single model silently rewrites all ten, and json.dump would not
        # show the aliasing.
        ctx = declared_window(mid, cli="opencode")
        return {"name": name,
                "limit": {"context": ctx,
                          "output": min(_HUB_MAX_OUTPUT, ctx // 4)},
                "tool_call": True,
                "temperature": True,
                "attachment": attachment}

    out = {k: spec(k, v) for k, v in _OPENCODE_EFFORT.items()}
    for key, label, _help in model_categories.labels():
        # Only the vision mode routes to models that can take an image. Saying
        # so on every mode would invite the CLI to send one into a chain that
        # cannot serve it.
        out.setdefault(key, spec(key, "mode: %s -- %s only" % (key, label.lower()),
                                 attachment=(key == "vision")))
        # ...and the same category combined with each heavier effort, so the one
        # pick a flat-list CLI makes can carry BOTH axes ("coding + swarm").
        # Skip any label that is itself an effort id (model_categories lists
        # "swarm" as a category, but routing's category set does not, so a
        # "swarm-swarm" compound would not route -- it must not be offered).
        if key in _OPENCODE_EFFORT:
            continue
        for tier, tail in _OPENCODE_COMPOUND_TIERS:
            cid = "%s-%s" % (key, tier)
            if cid in out:
                continue
            out[cid] = spec(cid, "%s + %s -- %s models, %s" % (
                key, tier, label.lower(), tail), attachment=(key == "vision"))
    return out


_OPENCODE_HUB_MODELS = _opencode_hub_models()


# --------------------------------------------------------------------------- #
# Claude Code: the hub's ids as real /model entries
# --------------------------------------------------------------------------- #
# MEASURED 2026-09-27 with Claude Code 2.1.283 (installed binary, a fresh
# CLAUDE_CONFIG_DIR, pointed at this hub):
#     claude -p "What is 2 plus 2?" --model auto
#     stderr: "auto" isn't described by this version's model catalog; update
#             Claude Code, or map it with behavesAs on a modelPicker row ...
#             [claude-code:unrecognized_model] {"model":"auto",...}
# plus an assumed 200k window. The binary's settings schema names the fix:
#     modelPicker: {options: [{model, label?, description?, behavesAs?}],
#                   replaceBuiltInOptions?}
# honored from user settings and --settings (never from a project checkout).
# With one row per hub id carrying behavesAs the same call prints NOTHING on
# stderr, and the SDK `initialize` reply lists exactly "Default" plus those rows
# -- that list IS the /model picker. Verified for auto, max, multi, fast and
# coding-swarm, via --model and via ANTHROPIC_MODEL alike.
#
# Rejected, on evidence from the same binary:
#   modelOverrides      maps an id Claude Code KNOWS to a provider id: one hub id
#                       per Claude model, never a row of its own.
#   ANTHROPIC_CUSTOM_MODEL_OPTION   a single row, hidden by replaceBuiltInOptions.
#   CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY   a discovered id this version
#                       does not know is "not offered until Claude Code is
#                       updated" unless a picker row gives it behavesAs anyway.
#   CLAUDE_CODE_MAX_CONTEXT_TOKENS alone   fixes the window, but the
#                       [claude-code:unrecognized_model] line stays.
#
# THE WINDOW. behavesAs makes an id "known", and a known model's window is the
# one of the model it behaves as: CLAUDE_CODE_MAX_CONTEXT_TOKENS=128000 was
# MEASURED ignored then (debug log: effectiveWindow=180000).
# CLAUDE_CODE_AUTO_COMPACT_WINDOW is what holds (effectiveWindow=108000, i.e.
# 128000 minus the 20000 output reserve). Both are set: MAX_CONTEXT_TOKENS still
# covers an id typed by hand that has no row (crew-code, team, plan ...).
# Neither accepts a per-model figure, so both take declared_window(None).

# The model whose client-side handling (prompt profile, capabilities, effort
# defaults) a hub id borrows. A long-lived id this and later builds know, with
# a 200k window -- above the declared one, so the compact window above is what
# binds, never a 1M assumption. MEASURED on the wire with it: thinking
# {"type": "adaptive"}, output_config {"effort": "high"}, max_tokens 32000;
# the hub answered every such request in the live runs. A declared figure above
# 200k no longer binds: Claude Code caps its auto-compact window at the model's
# own ("the minimum of this setting and your model's maximum context window",
# 2.1.288), which is why app._cli_declared_window steers it against
# min(declared, 200000).
_CLAUDE_BEHAVES_AS = "claude-sonnet-4-6"

# Claude Code 2.1.283's own model aliases (the literal list in its binary). A
# hub id spelled like one never reaches the hub: MEASURED, `--model best` went
# out as model="claude-fable-5-1" -- plain auto routing, not the hub's best
# tier. `max` is the same tier on the hub (_quality_route_kwargs), so Claude
# Code is always handed `max` instead.
_CLAUDE_CODE_ALIASES = frozenset({
    "default", "sonnet", "opus", "haiku", "fable", "best", "opusplan",
    "sonnet[1m]", "opus[1m]", "fable[1m]"})

# What Claude Code's own family aliases resolve to while it runs on the hub
# (ANTHROPIC_DEFAULT_<FAMILY>_MODEL). opus is the strongest tier, sonnet the
# everyday one, haiku -- the model Claude Code uses for its small background
# calls -- the fast category. MEASURED: `--model opus` then sends "max",
# `--model haiku` sends "fast", neither with an unrecognised-model notice.
_CLAUDE_FAMILY_IDS = (("OPUS", "max"), ("SONNET", "auto"), ("HAIKU", "fast"))


def claude_model_id(mid):
    """The id to hand Claude Code for hub id `mid` ('best' -> 'max', see
    _CLAUDE_CODE_ALIASES); anything else unchanged."""
    if isinstance(mid, str) and mid.strip().lower() == "best":
        return "max"
    return mid


def claude_model_picker():
    """The `modelPicker` settings value: one row per id of the opencode picker
    (tiers, categories, category+effort compounds -- the same list, same
    labels), minus ids Claude Code would read as its own alias.
    replaceBuiltInOptions hides the built-in Opus/Sonnet/Haiku rows: against the
    hub they are only more names for `auto`."""
    rows = []
    for mid, spec in _opencode_hub_models().items():
        if mid.lower() in _CLAUDE_CODE_ALIASES:
            continue
        head, _sep, tail = str(spec.get("name") or mid).partition(" -- ")
        head = head.strip() or mid
        rows.append({"model": mid, "label": head,
                     "description": tail.strip() or head,
                     "behavesAs": _CLAUDE_BEHAVES_AS})
    return {"replaceBuiltInOptions": True, "options": rows}


def claude_hub_env(opus=None):
    """Env vars Claude Code needs beside BASE_URL/TOKEN/MODEL to run on the hub:
    the family-alias mapping and the declared window. `opus` overrides the opus
    mapping (an /agent session maps it to its own tier -- see
    _apply_claude_hub_fallback). A family whose target has no picker row is
    left unmapped rather than pointed at an id Claude Code would not know."""
    win = str(int(declared_window(None)))
    env = {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": win,
           "CLAUDE_CODE_MAX_CONTEXT_TOKENS": win}
    ids = {r["model"] for r in claude_model_picker()["options"]}
    for family, mid in _CLAUDE_FAMILY_IDS:
        if family == "OPUS" and opus:
            mid = claude_model_id(opus)
        if mid in ids:
            env["ANTHROPIC_DEFAULT_%s_MODEL" % family] = mid
    return env


def _is_claude_hub_picker(value):
    """True for a modelPicker every row of which is a hub id (i.e. ours)."""
    if not isinstance(value, dict):
        return False
    rows = value.get("options")
    if not isinstance(rows, list) or not rows:
        return False
    ids = set(_opencode_hub_models()) | {"best"}
    return all(isinstance(r, dict) and r.get("model") in ids for r in rows)


def _claude_settings_file(config_home):
    return os.path.join(config_home, "settings.json")


def _read_json_dict(path):
    """(dict, ok). ok=False only when the file exists but is not a JSON object."""
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}, True
    except (OSError, ValueError):
        return None, False
    return (data, True) if isinstance(data, dict) else (None, False)


def _write_json_atomic(path, data):
    tmp = path + ".tmp-%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def _seed_claude_picker(config_home):
    """Put the hub's modelPicker into the isolated copy's user settings, which is
    where Claude Code reads it (env vars cannot carry it). Never creates the
    directory, keeps every other key, leaves a picker that is not ours alone,
    and skips the write when nothing changed. Never raises."""
    try:
        if not config_home or not os.path.isdir(config_home):
            return
        path = _claude_settings_file(config_home)
        data, ok = _read_json_dict(path)
        if not ok:
            return
        cur = data.get("modelPicker")
        if cur is not None and not _is_claude_hub_picker(cur):
            return
        want = claude_model_picker()
        if cur == want:
            return
        data["modelPicker"] = want
        _write_json_atomic(path, data)
    except Exception:                                            # noqa: BLE001
        pass


def _unseed_claude_picker(config_home):
    """The revert: once the isolated copy is signed in, the hub's ids would be
    sent to Anthropic, so our picker goes (and the file, if it held nothing
    else). Never raises."""
    try:
        if not config_home or not os.path.isdir(config_home):
            return
        path = _claude_settings_file(config_home)
        data, ok = _read_json_dict(path)
        if not ok or not _is_claude_hub_picker(data.get("modelPicker")):
            return
        data.pop("modelPicker", None)
        if data:
            _write_json_atomic(path, data)
        else:
            os.remove(path)
    except Exception:                                            # noqa: BLE001
        pass


def _upgrade_opencode_seed(target):
    """Top up a config WE wrote. No-op for one we do not recognise, and no
    write at all when nothing is missing.

    Two levels, because one of them was not enough. Adding MISSING IDS is what
    this did first, for a seed that predated the quality modes. But every
    install that ran before `limit` was declared has all ten ids present and
    all ten of them limitless -- so an id-level check finds nothing to do and
    the session-never-compacts bug survives the upgrade forever. Missing FIELDS
    are filled in too.

    Only absent fields. A label the user renamed, or a window they raised
    because they know their own fleet, is theirs and stays. One exception: a
    window still at the old FIXED figure every entry used to get
    (_CODEX_CONTEXT_WINDOW, with its _HUB_MAX_OUTPUT reserve) is ours, not a
    choice, and follows the declared window for that id."""
    try:
        with open(target, encoding="utf-8") as fh:
            cfg = json.load(fh)
        prov = (cfg.get("provider") or {}).get("free-llm-hub")
        if not isinstance(prov, dict):
            return                          # not ours -- the user's own config
        models = prov.get("models")
        if not isinstance(models, dict):
            return
        changed = False
        for mid, spec in _opencode_hub_models().items():
            cur = models.get(mid)
            if not isinstance(cur, dict):
                models[mid] = copy.deepcopy(spec)
                changed = True
                continue
            for field, value in spec.items():
                if field not in cur:
                    cur[field] = copy.deepcopy(value)
                    changed = True
            lim = cur.get("limit")
            if (isinstance(lim, dict) and lim.get("context") == _CODEX_CONTEXT_WINDOW
                    and lim.get("output") in (None, _HUB_MAX_OUTPUT)
                    and spec["limit"] != {"context": lim.get("context"),
                                          "output": lim.get("output")}):
                lim.update(copy.deepcopy(spec["limit"]))
                changed = True
        if not changed:
            return
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
        os.replace(tmp, target)
    except Exception:                                            # noqa: BLE001
        pass                    # a stale seed is a clear error later, not a crash


def _opencode_limit_is_hubs(lim):
    """Is this `limit` one the hub wrote? _opencode_hub_models always pairs a
    context with output = min(_HUB_MAX_OUTPUT, context // 4) -- the old fixed
    128000/16384 included. A pair that breaks that shape (the user's raised
    window with their own reserve, say 999999/4096) is theirs and stays."""
    if not isinstance(lim, dict):
        return False
    ctx = lim.get("context")
    if isinstance(ctx, bool) or not isinstance(ctx, int) or ctx <= 0:
        return False
    return lim.get("output") in (None, min(_HUB_MAX_OUTPUT, ctx // 4))


def resync_opencode_windows(target):
    """Bring the hub's own `limit` of every hub id in an opencode config the
    hub WIRED (provider free-llm-hub pointing at this hub) to the current
    declared window. Only those two fields of existing entries change -- no
    entry is added (that is _upgrade_opencode_seed's job), nothing else is
    touched, and nothing is written when nothing changed. Unlike
    _upgrade_opencode_seed, which runs at boot before the fleet is known and so
    follows only the old fixed figure, this follows ANY window the hub wrote
    (app._resync_declared_windows calls it once the fleet is warm). Returns
    True when the file was rewritten. Never raises."""
    try:
        if not target or not os.path.isfile(target):
            return False
        with open(target, encoding="utf-8-sig") as fh:
            cfg = json.load(fh)
        prov = (cfg.get("provider") or {}).get(_OPENCODE_PROVIDER_ID) \
            if isinstance(cfg, dict) else None
        if not isinstance(prov, dict) or \
                not _points_at_hub((prov.get("options") or {}).get("baseURL")):
            return False                    # not wired to the hub: not ours to edit
        models = prov.get("models")
        if not isinstance(models, dict):
            return False
        changed = False
        for mid, spec in _opencode_hub_models().items():
            cur = models.get(mid)
            lim = cur.get("limit") if isinstance(cur, dict) else None
            if not _opencode_limit_is_hubs(lim):
                continue
            want = spec["limit"]
            if lim.get("context") != want["context"] or lim.get("output") != want["output"]:
                lim["context"], lim["output"] = want["context"], want["output"]
                changed = True
        if changed:
            _write_json_atomic(target, cfg)
        return changed
    except Exception:                                            # noqa: BLE001
        return False


# The provider id the seed registers this hub under -- also what the per-
# session OPENCODE_CONFIG_CONTENT override (see _agentic_env) has to name.
_OPENCODE_PROVIDER_ID = "free-llm-hub"


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
                _OPENCODE_PROVIDER_ID: {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "Calvoun Free LLM Hub",
                    "options": {"baseURL": "http://127.0.0.1:%d/v1" % _port(),
                                "apiKey": key},
                    # Built NOW, so the per-id windows are the current ones.
                    "models": _opencode_hub_models(),
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


# CMD.EXE EATS EVERYTHING AFTER THE FIRST LINE.
#
# An npm-installed CLI on Windows is a .cmd shim, and a batch file cannot be
# run by CreateProcess -- so the launcher went through `cmd.exe /c shim.cmd
# <args>`. cmd.exe treats a newline as a COMMAND SEPARATOR, quoted or not, so
# every multi-line argument was silently truncated at its first line break.
#
# MEASURED through this exact path: 111 characters of prompt in, 59 out --
# "It must define exactly two public functions:" arrived and the two function
# signatures on the following lines did not. Two swarm workers reported it in
# their own words ("your message got cut off after 'two public functions:'"),
# which is what sent me looking.
#
# The blast radius was everything this module sends positionally:
#   * a swarm phase task, which the planner writes as a numbered list;
#   * any message with a line break typed on the /agent page;
#   * opencode's standing instruction, appended after "\n\n---\n", so it
#     never arrived at all;
#   * claude's --append-system-prompt, whose parts are joined with blank lines,
#     so only the first of them was ever delivered.
#
# The shim is a two-line batch file whose only job is to run a real program, so
# the fix is to run that program directly and never involve a shell. Verified
# on the same prompt through the same code: 111 in, 111 out.
_SHIM_TARGET_CACHE = {}


def _resolve_shim(path):
    r"""The real argv behind a Windows .cmd shim, or None to keep using cmd.exe.

    npm writes two shapes, both ending in a line that forwards %*:
        "%dp0%\node_modules\opencode-ai\bin\opencode.exe"   %*
        "%_prog%"  "%dp0%\node_modules\@openai\codex\bin\codex.js" %*
    so this takes the quoted tokens off that line and expands the two variables
    npm uses. Anything it does not recognise, or that does not exist on disk,
    returns None -- an unreadable shim must fall back to what worked before
    rather than fail the turn."""
    key = os.path.abspath(path)
    if key in _SHIM_TARGET_CACHE:
        return _SHIM_TARGET_CACHE[key]
    argv = None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        forward = [ln for ln in text.splitlines() if "%*" in ln]
        if forward:
            tokens = re.findall(r'"([^"]+)"', forward[-1])
            here = os.path.dirname(key)
            out = []
            for tok in tokens:
                tok = tok.replace("%dp0%", here + os.sep).replace("%~dp0", here + os.sep)
                if "%_prog%" in tok or tok.strip().lower() in ("%_prog%", "node"):
                    node = shutil.which("node")
                    if not node:
                        out = []
                        break
                    tok = node
                elif "%" in tok:
                    out = []          # a variable we do not know how to expand
                    break
                else:
                    tok = os.path.normpath(tok)
                    if not os.path.isfile(tok):
                        out = []
                        break
                out.append(tok)
            # No further check on out[0]: every branch above already proved its
            # own token -- an interpreter by resolving it on PATH, a path by
            # finding the file. Re-testing the first one with isfile() only
            # rejects a perfectly good interpreter that lives somewhere
            # os.path cannot stat the way we expect.
            if out:
                argv = out
    except (OSError, ValueError):
        argv = None
    _SHIM_TARGET_CACHE[key] = argv
    return argv


def _launcher(path):
    """argv prefix that can actually execute `path`.

    Unlike _sub_launcher() in app.py, which hands its prompt over on STDIN and
    so never cared, this module passes the prompt POSITIONALLY -- which is why
    the shell in the middle mattered here and not there."""
    if os.name == "nt" and os.path.splitext(path)[1].lower() in (".cmd", ".bat"):
        direct = _resolve_shim(path)
        if direct:
            return list(direct)
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

def _tree_pids(pid):
    """The process and every descendant, deepest first, or [] without psutil.
    Deepest first: killing the parent before its children detaches them from
    the tree, and on Windows a detached console child lives on."""
    try:
        import psutil
        root = psutil.Process(pid)
        kids = root.children(recursive=True)
        return [k.pid for k in reversed(kids)] + [pid]
    except Exception:                                            # noqa: BLE001
        return []


def _signal_tree(pid, hard):
    """Signal the WHOLE tree under `pid`, children first.

    MEASURED 2026-09-12: a wedged worker was restarted by the stall watchdog
    and its two bash.exe children -- a `python app.py &` the model had
    started -- were still running twelve minutes later. `taskkill /T` was
    asked AFTER proc.terminate() had already killed the CLI itself, so the
    tree it was told to walk had no root, and a soft taskkill never reaches
    a console process anyway. Children first, by pid, then the parent; the
    taskkill sweep stays as the fallback for a psutil that is missing."""
    try:
        if os.name == "nt":
            pids = _tree_pids(pid)
            for child in pids:
                subprocess.run(["taskkill", "/PID", str(child), "/T", "/F"],
                               capture_output=True, timeout=10, creationflags=_NO_WINDOW)
            if not pids:
                argv = ["taskkill", "/PID", str(pid), "/T", "/F"]
                subprocess.run(argv, capture_output=True, timeout=10,
                               creationflags=_NO_WINDOW)
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
    # THE TREE FIRST, while the CLI is still alive to be its root: once
    # proc.terminate() has run, the grandchildren (a Bash-tool child, the
    # server the model started with `&`) are nobody's children and a tree
    # walk from the dead pid finds nothing.
    _signal_tree(proc.pid, hard=False)
    try:
        proc.terminate()
    except Exception:
        pass
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
                 "tools_notified", "quality", "mode", "stop_pending")

    def __init__(self, cli_id, project_dir, quality="normal", mode=None):
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
        # A Stop pressed while the turn owns the session but no CLI process is
        # alive (before the first one starts, or between two -- a retry, an
        # auto-continue): honoured by the next process the turn starts.
        self.stop_pending = False
        self.tools_notified = False        # missing-toolchain notice, once per session
        # "normal" | "max". Chosen once, when the session starts. "max" launches
        # the CLI with ANTHROPIC_MODEL=best instead of auto, so every turn it
        # sends is routed at the top tier and never drops to the cheap one.
        self.quality = quality if quality in QUALITIES else "normal"
        # PER-PROJECT MODE. A model_categories key ("coding", "reasoning",
        # "vision", ...) restricting this session to that kind of model, or None
        # for whatever the hub's global setting says. Carried to the hub as the
        # session's model id, exactly as `quality` is -- see _hub_model_for --
        # so one project can run on the coding models while another runs on the
        # reasoning ones, with no global setting to remember to change back.
        self.mode = mode or None


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


def start_session(cli_id, project_dir, create_new=False, quality="normal",
                  mode=None) -> str:
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
    sess = _Session(cli_id, abs_dir, quality=quality, mode=mode)
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
    seen_before = None          # what sat under this id when we looked
    if session_id and _SAFE_SESSION_ID_RE.match(str(session_id)):
        with _REGISTRY_LOCK:
            existing = _REGISTRY.get(str(session_id))
            seen_before = existing
            if existing is not None:
                with existing.proc_lock:
                    live_proc = existing.proc
                if live_proc is not None and live_proc.poll() is None:
                    return existing.id
                # ...and between processes: a turn is one CLI process per
                # round, with nothing running for a few seconds during a
                # transient retry or an auto-continue, and a multi-session
                # turn is no process of this session's at all. The live
                # buffer knows a turn is on either way.
                if turn_is_live(str(session_id)):
                    return existing.id
    sid = start_session(cli_id, project_dir)      # all the same validation
    # WHAT THE CONVERSATION WAS RUNNING AS. start_session builds a default
    # session (normal / no mode), so resuming used to hand back a conversation
    # that had lost its own configuration: pick Swarm and the uncensored models
    # today, come back after the 5-hourly auto-update restart, and the same
    # thread quietly continued on Normal with every model. The project folder
    # was already remembered; this is the rest of "continue where I left off".
    #
    # Read OUTSIDE the registry lock: it touches the history file, and
    # _REGISTRY_LOCK is held on the hot path of every turn.
    restored = {}
    if session_id:
        try:
            restored = agentic_history.get_conversation(str(session_id)) or {}
        except Exception:                                        # noqa: BLE001
            restored = {}
    with _REGISTRY_LOCK:
        sess = _REGISTRY.pop(sid)
        if session_id and _SAFE_SESSION_ID_RE.match(str(session_id)):
            # TWO RESUMES AT ONCE -- two tabs reloading the same conversation
            # -- both passed the live-check above and both built a session.
            # The second to get here used to overwrite the first in the
            # registry, orphaning a session a turn may already be running on.
            # If what sits under the id now is not what was there when we
            # looked, another resume got here first: it wins, ours is dropped.
            # (An entry that WAS there -- a finished session being re-pointed
            # at its saved thread -- is replaced, as before.)
            now_there = _REGISTRY.get(str(session_id))
            if now_there is not None and now_there is not seen_before:
                return now_there.id
            sess.id = str(session_id)
        sess.native_session_id = native_session_id or None
        quality = restored.get("quality")
        if quality in QUALITIES:
            sess.quality = quality
        mode = restored.get("mode")
        if mode:
            sess.mode = mode
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

# The dashboard's subscription model picker (app._sub_selected_model) stores
# the user's choice under these settings; the agent turn reads the SAME key so
# a pick applies to one-shot hops and /agent turns alike. Unset keeps the
# defaults above (claude: _MODEL_ALIAS; codex: no --model, config.toml decides).
_SUB_MODEL_SETTING = {"claude": "sub_claude_model", "codex": "sub_codex_model"}
_SUB_MODEL_SAFE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\[\]/-]{0,79}$")


def _sub_model_setting(cli_id: str) -> str:
    """The picked subscription model for this CLI, or "" when unset/invalid.
    Validated because it travels in argv: a value that could read as a flag
    is ignored, never passed. Never raises."""
    key = _SUB_MODEL_SETTING.get(cli_id)
    if not key:
        return ""
    try:
        v = config.get_setting(key, None)
    except Exception:                                            # noqa: BLE001
        return ""
    v = v.strip() if isinstance(v, str) else ""
    return v if _SUB_MODEL_SAFE_RE.match(v) else ""


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

def _session_model_id(sess) -> str:
    """The hub model id one session's turns must carry, or None to send none.

    Three launchers repeated `if quality in ("max","swarm")` and then built the
    id, which was fine while a quality tier was the only thing that could
    override the model. A per-project MODE is a second one, and adding it to
    three copies of the same condition is how the fourth launcher gets
    forgotten. Normal-quality, no-mode sessions still get None, so the CLI's own
    config keeps deciding exactly as before."""
    quality = getattr(sess, "quality", "normal")
    mode = getattr(sess, "mode", None)
    if quality not in ("max", "swarm", "multi") and not mode:
        return None
    return _hub_model_for(quality, mode)


def _hub_model_for(quality: str = None, mode: str = None) -> str:
    """The hub-side model id that carries a session's mode.

    "best" is app._is_orchestrate's quality_mode (auto that never drops to the
    cheap tier); "swarm" is app._is_swarm_model's parallel best-of-N fan-out;
    "auto" is ordinary routing. Anything unrecognised falls back to auto rather
    than inventing a model name nothing serves."""
    if mode:
        # BOTH axes, as the compound the pickers list ("coding-max",
        # "coding-swarm", see _OPENCODE_COMPOUND_TIERS; app routes it with
        # _split_category_effort). It used to return the bare mode for "max"
        # and bare "swarm" for swarm, dropping one of the two choices.
        # MEASURED 2026-09-28, session 47a25faa (Max + coding): every request
        # asked for "coding" -- the Normal tier -- and two agent turns were
        # served by liquid/lfm-2.5-2.6b, a 2.6B model.
        q = quality or "normal"
        # "multi" as a single turn (a question the run did not split, or the
        # parent's own turn) is the top tier, like the no-mode branch below:
        # it used to send the bare mode -- the Normal tier -- so the owner's
        # Multi + coding session answered on gemini flash (2026-09-30).
        if q == "multi":
            q = "max"
        if q in ("max", "swarm"):
            return "%s-%s" % (mode, q)
        return mode
    # "multi" is not a model either: the message becomes a swarm_windows run
    # and the workers carry their own modes. The parent session itself rarely
    # runs a turn in that tier, and when it does it deserves the best tier.
    return {"max": "best", "swarm": "swarm", "multi": "best"}.get(quality or "normal", "auto")


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


# The brief's ABSOLUTE path, not "this folder contains <name>". MEASURED
# 2026-09-28 (opencode log, run 1476d09d): opencode's instance directory,
# session directory and PWD were all the session's project folder (a temp
# folder), yet the model's first tool call read
# C:\Users\hamza\Desktop\Projects\opencode-evals\.calvoun-brief-<id>.md --
# a folder that does not exist on this machine. It was handed a bare file
# name and "this folder", and it made up the folder. An absolute path leaves
# nothing to make up.
_BRIEF_POINTER = (
    "READ FIRST: %s -- this task's required standards and what this "
    "conversation already established. Open it at that exact path and follow it."
)


def _brief_pointer_path(has_brief, project_dir=None):
    """What the pointer names: the brief's absolute path when the project
    folder is known, else its bare name in the working directory (older
    callers, and the cmd.exe fallback with no room left -- _pointer_dir)."""
    name = has_brief if isinstance(has_brief, str) else BRIEF_FILENAME
    if project_dir:
        try:
            return os.path.join(os.path.abspath(project_dir), name)
        except (TypeError, ValueError):
            pass
    return name + " in your working directory"


def _pointer_dir(sess, bin_path, text):
    """The project folder for the brief pointer, or None for the bare name.

    Always the folder when the CLI is launched directly (CreateProcess: 32,767
    characters, _MAX_MESSAGE_CHARS_DIRECT leaves ~6,000 spare). On the cmd.exe
    fallback the worst-case turn-1 argv sits ~120 characters under ~8191, and
    an absolute path can be 260: there the path is paid for out of the
    MESSAGE's own headroom -- used when the message is at least that much
    under _MAX_MESSAGE_CHARS, else the bare name (what shipped before)."""
    pdir = getattr(sess, "project_dir", None)
    if not pdir:
        return None
    try:
        head = os.path.basename(_launcher(bin_path)[0]).lower()
    except Exception:                                            # noqa: BLE001
        head = "cmd.exe"                  # unknown: assume the tight path
    if head not in ("cmd.exe", "cmd"):
        return pdir
    try:
        cost = len(os.path.abspath(pdir)) + 1
    except (TypeError, ValueError):
        return None
    return pdir if len(text or "") + cost <= _MAX_MESSAGE_CHARS else None

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
    "PROGRESS.md, as a markdown checklist (- [ ] todo, - [x] done, - [~] in "
    "progress). A reply-only checklist does not survive: your "
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


def _system_prompt_addition(text: str = "", has_brief: bool = False,
                            project_dir: str = None) -> str:
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
        # has_brief is the FILENAME when one was written (a per-session name in
        # a shared folder), and True from older callers -- both mean "there is
        # one", and only the first knows what it is called. With project_dir
        # the pointer names the ABSOLUTE path (see _BRIEF_POINTER).
        parts.append(_BRIEF_POINTER % _brief_pointer_path(has_brief, project_dir))
    # chr(10) rather than a backslash-n literal: this file gets edited through
    # tooling that has repeatedly turned that escape into a RAW newline, which
    # splits the string across lines and makes the module unimportable.
    sep = chr(10) + chr(10)
    return sep.join(p for p in parts if p)


BRIEF_FILENAME = ".calvoun-brief.md"
# How long a brief written for a session that is gone is left lying in the
# project. Long enough that a session paused overnight still finds its own,
# short enough that a folder does not collect them.
_BRIEF_STALE_AFTER = 48 * 3600


def brief_filename(session_id=None):
    """The brief file for one session, or the shared one when there is no id.

    ONE FILE PER PROJECT WAS WRONG FOR A SWARM. Workers share a project
    directory, so four of them rewrote the same .calvoun-brief.md within
    seconds of each other -- harmless while it held only craft standards, which
    are generic, and not harmless at all now that it carries what a
    CONVERSATION has established. Worker B could read worker A's decisions as
    its own.

    The pointer in the prompt already interpolates this name, so a per-session
    file costs nothing but the id's characters (worst-case turn-1 argv measured
    at 8058 of the ~8191 ceiling, and a session id is 32)."""
    sid = str(session_id or "").strip()
    if not sid or not _SAFE_SESSION_ID_RE.match(sid):
        return BRIEF_FILENAME
    # A PREFIX, not the whole id. The pointer to this file rides in argv, and
    # the worst-case turn-1 command line is already within ~150 characters of
    # cmd.exe's ceiling -- 32 hex characters of session id in a filename is
    # real budget spent on nothing. Twelve is 48 bits: the collision it guards
    # against is two workers in ONE folder at ONE time, not a global namespace.
    # MEASURED after this change: claude 8071, codex 8055, opencode 8003.
    return ".calvoun-brief-%s.md" % sid[:12]


def _sweep_stale_briefs(project_dir, keep):
    """Delete per-session briefs nobody is coming back for. Best-effort."""
    try:
        cutoff = time.time() - _BRIEF_STALE_AFTER
        for name in os.listdir(project_dir):
            if (name.startswith(".calvoun-brief-") and name.endswith(".md")
                    and name != keep):
                path = os.path.join(project_dir, name)
                if os.path.getmtime(path) < cutoff:
                    os.unlink(path)
    except (OSError, ValueError):
        pass

# How much remembered context rides along with the brief. It goes in the FILE,
# never in argv: the worst-case turn-1 command line already measures 8006 chars
# against cmd.exe's ~8191 ceiling, so there is not room in the prompt for two
# hundred characters, let alone two thousand. The default; a session whose
# model window is known gets memory.budget_for_window of it (up to 6000, never
# more than ~5% of the window).
_MEMORY_BUDGET = memory.MEMORY_BUDGET_DEFAULT


def _memory_block(sess, turn_text=""):
    """What this conversation already established, or "".

    THE MEMORY WAS WRITE-ONLY UNTIL NOW. memory.remember_summary has been
    filing the compaction recap since the memory manager landed, and nothing
    ever read it back into a turn -- so a conversation that had been compacted
    still forgot everything it had done, which is the whole complaint the
    module was built for ("les agents ne continuent pas jusqu'au bout ...
    utilise le memory manager")."""
    try:
        # project_dir is what makes the LONG horizon outlive the conversation:
        # "this repo uses pnpm, not npm" is true tomorrow too, and re-learning
        # it in every new session is the cost this removes.
        # THE TURN IS THE QUERY. Without it every remembered fact ships on
        # every message -- forty facts at 300 characters is 12,000 characters
        # spent on a turn that asked one question. With it, a fact travels when
        # the turn is actually about it; the job itself and anything phrased as
        # a rule travel always.
        sid = getattr(sess, "id", None)
        block = memory.context_block(sid,
                                     budget_chars=memory.budget_for_window(
                                         _session_context_window(sess)),
                                     project_dir=getattr(sess, "project_dir", None),
                                     query=turn_text or "")
    except Exception:                                            # noqa: BLE001
        return ""
    try:
        # Handed over: whatever made this turn due (a stopping place, a list
        # the agent had not seen) has now been delivered, including on a first
        # turn, which ships without asking _due_for_restate.
        if sid:
            memory.mark_memory_delivered(sid)
    except Exception:                                            # noqa: BLE001
        pass
    return block


def write_task_brief(project_dir, text, memory_block="", session_id=None):
    """Write the craft brief for `text` into the project, return True if any.

    WHY A FILE AND NOT MORE PROMPT: the prompt travels as a POSITIONAL argv
    argument through `cmd.exe /c <shim.cmd> ...` on Windows, and that command
    line dies at ~8191 chars -- which is exactly why _MAX_MESSAGE_CHARS exists.
    The briefs are ~9,000 chars on their own; inlining them would have taken the
    worst case to roughly 15,700 and broken every turn on this platform.

    An agent has file tools. So the standards go in a file it reads, the prompt
    spends ~200 characters telling it to, and the full brief arrives intact.

    Rewritten each turn it applies, so the standards always match the CURRENT
    request rather than whatever the first message happened to be about.

    IT CARRIES THE CONVERSATION'S MEMORY TOO, for the same reason and at no
    extra cost: the pointer to this file is already in the prompt, so what the
    session already established rides in for free instead of competing with the
    user's own message for the ~185 characters of argv headroom that are left.

    One file per PROJECT, not per session. Swarm workers share a project
    directory and will overwrite each other's copy; that is tolerable here
    because they are phases of one job whose summaries the review phase shares
    deliberately anyway. It would not be tolerable for anything private."""
    try:
        brief = craft.system_message(text or "")
        memory_block = (memory_block or "").strip()
        name = brief_filename(session_id)
        path = os.path.join(project_dir, name)
        header = ("<!-- Written by Calvoun Free LLM Hub for THIS task. "
                  "Safe to delete; it is regenerated whenever it applies. -->")
        # WHERE THE FILES GO -- first, before any standard. MEASURED 2026-09-12
        # in a normal session on Windows: the model ran `pwd` in the bash tool,
        # got a POSIX spelling, and wrote index.html, style.css and
        # PROGRESS.md under /workspace -- C:\workspace -- while the project
        # folder stayed empty and the review found nothing. The swarm workers
        # already had this line in their prompt; a normal turn's own
        # instruction rides in argv, which on the shell path has ~60
        # characters to spare, so it goes in this file instead, which the
        # agent is told to read first.
        folder = ("## The project folder" + chr(10) + chr(10) + "THE PROJECT FOLDER IS: "
                  + os.path.abspath(project_dir) + chr(10)
                  + "Every file you create or edit must be inside it. Use paths "
                  "relative to it, or that exact absolute spelling. Do not reuse a "
                  "path printed by a shell (`pwd` may print it in another form) and "
                  "never write to /, /tmp or /workspace.")
        # SERVERS, and which python is the hub. This used to be one line --
        # "Do not start a server ... (no `&`, `nohup`, `start`...)" -- which
        # contradicted craft.SHIP's "start it in the BACKGROUND" in the same
        # file, never named the hub, and did not stop the live 2026-09-27
        # opencode turn that parked `python app.py` in its shell, then listed
        # and killed python processes. The rules, with the detached spellings
        # measured on this machine, live in agent_servers.
        parts = [header, folder, agent_servers.brief_section()]
        if memory_block:
            parts.append("## What this conversation already established"
                         + chr(10) + chr(10) + memory_block)
        if brief:
            parts.append(brief["content"])
        sep = chr(10) + chr(10)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(sep.join(parts) + chr(10))
        _sweep_stale_briefs(project_dir, name)
        _git_exclude_briefs(project_dir)
        return name
    except Exception:                                            # noqa: BLE001
        return False        # standards are a bonus; never cost the user a turn


# The brief lives IN the project folder, so in a git repo it showed up as an
# untracked file and got swept into the agent's own `git add -A` commits --
# the conversation's memory, published with the code. Excluded through the
# repo's LOCAL .git/info/exclude, never .gitignore: that file is the user's,
# versioned, and editing it would itself be a change to commit.
_BRIEF_EXCLUDE_PATTERN = ".calvoun-brief*.md"
_EXCLUDED_REPOS = set()


def _git_common_dir(project_dir):
    """The git directory holding info/exclude for the repo `project_dir` is
    in (walking up), or None when it is not in one. Handles a worktree's
    `.git` FILE (gitdir: ...) and its `commondir`."""
    here = os.path.abspath(project_dir)
    while True:
        dot = os.path.join(here, ".git")
        gitdir = None
        if os.path.isdir(dot):
            gitdir = dot
        elif os.path.isfile(dot):
            try:
                with open(dot, encoding="utf-8", errors="replace") as fh:
                    line = fh.read(4096).strip()
                if line.startswith("gitdir:"):
                    gitdir = line[len("gitdir:"):].strip()
                    if not os.path.isabs(gitdir):
                        gitdir = os.path.join(here, gitdir)
            except OSError:
                gitdir = None
        if gitdir and os.path.isdir(gitdir):
            common = os.path.join(gitdir, "commondir")
            if os.path.isfile(common):
                try:
                    with open(common, encoding="utf-8", errors="replace") as fh:
                        rel = fh.read(4096).strip()
                    if rel:
                        cand = rel if os.path.isabs(rel) else os.path.join(gitdir, rel)
                        if os.path.isdir(cand):
                            return os.path.normpath(cand)
                except OSError:
                    pass
            return os.path.normpath(gitdir)
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent


def _git_exclude_briefs(project_dir):
    """Make sure the repo `project_dir` sits in ignores the brief files.
    Once per repo per process; best-effort, never raises."""
    try:
        common = _git_common_dir(project_dir)
        if not common or common in _EXCLUDED_REPOS:
            return bool(common)
        info = os.path.join(common, "info")
        path = os.path.join(info, "exclude")
        existing = ""
        if os.path.isfile(path):
            with open(path, encoding="utf-8", errors="replace") as fh:
                existing = fh.read()
        if _BRIEF_EXCLUDE_PATTERN not in [l.strip() for l in existing.splitlines()]:
            os.makedirs(info, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                if existing and not existing.endswith(chr(10)):
                    fh.write(chr(10))
                fh.write("# Calvoun Free LLM Hub: per-session agent briefs" + chr(10)
                         + _BRIEF_EXCLUDE_PATTERN + chr(10))
        _EXCLUDED_REPOS.add(common)
        return True
    except Exception:                                            # noqa: BLE001
        return False


def remove_task_brief(project_dir, session_id):
    """Delete this session's brief from its project (the conversation is
    being deleted). Only the per-session file: the shared one belongs to no
    single conversation. Never raises."""
    try:
        name = brief_filename(session_id)
        if not project_dir or name == BRIEF_FILENAME:
            return False
        os.unlink(os.path.join(project_dir, name))
        return True
    except Exception:                                            # noqa: BLE001
        return False


def _claude_model_for(sess) -> str:
    """--model for one claude turn: the session's mode when the hub is serving
    it, the subscription's picked model otherwise (default: the long-stable
    "opus" alias)."""
    _mid = _session_model_id(sess)
    if _mid and _hub_backs("claude"):
        return claude_model_id(_mid)   # never "best": Claude Code's own alias
    return _sub_model_setting("claude") or _MODEL_ALIAS


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
    # claude gets --append-system-prompt on every turn, but with `text` blanked
    # after turn 1 the addition collapses to almost nothing -- so it had the
    # same "told once, before compaction ate it" problem codex and opencode had,
    # just by a different route.
    first = not sess.native_session_id
    fresh = first or _due_for_restate(sess)
    addition = _system_prompt_addition(
        text if fresh else "",
        has_brief=fresh and write_task_brief(sess.project_dir, text,
                                             memory_block=_memory_block(sess, text),
                                             session_id=getattr(sess, "id", None)),
        project_dir=_pointer_dir(sess, bin_path, text))
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
    if sess.native_session_id and not _due_for_restate(sess):
        addition = ""
    else:
        addition = _system_prompt_addition(
            text, has_brief=write_task_brief(sess.project_dir, text,
                                             memory_block=_memory_block(sess, text),
                                             session_id=getattr(sess, "id", None)),
            project_dir=_pointer_dir(sess, bin_path, text))
    prompt = (text + "\n\n---\n(Standing instruction for this session: " + addition + ")") \
        if addition else text
    base = ["exec"]
    # Only Max/Swarm touch argv; Normal keeps the shipped shape byte for byte
    # and lets config.toml's model = "auto" decide, exactly as before.
    _mid = _session_model_id(sess)
    if _mid and _hub_backs("codex"):
        base += ["--model", _mid]
    elif not _hub_backs("codex") and _sub_model_setting("codex"):
        # A real ChatGPT subscription serves this turn: honour the model the
        # user picked for it. Unset (the default) adds nothing, as before.
        base += ["--model", _sub_model_setting("codex")]
    if sess.native_session_id:
        base += ["resume", sess.native_session_id, "--json",
                 "--dangerously-bypass-approvals-and-sandbox", prompt]
    else:
        base += ["--json", "--dangerously-bypass-approvals-and-sandbox",
                 "--skip-git-repo-check", prompt]
    return _launcher(bin_path) + base



def _due_for_restate(sess):
    """Should this turn carry the standing instructions again?

    They shipped on turn 1 and never again -- for codex and opencode literally
    `addition = ""` from turn 2 -- so a forty-turn session was following rules
    it was told about once, before compaction had eaten the message carrying
    them. That is the "les agents ne continuent pas jusqu'au bout" complaint
    from the other side: an agent that has forgotten it was told to finish.

    Every RESTATE_EVERY turns, not every turn: a repeated notice reads as a
    repeated user instruction (the codex failure recorded in _build_argv_codex),
    and it costs tokens on requests that did not need it. The counter is the
    durable one, because _Session.turn_count resets to 0 on resume and the
    5-hourly auto-update restart resumes everything."""
    try:
        sid = getattr(sess, "id", None)
        if not sid:
            return False
        if memory.should_restate_rules(sid):
            memory.mark_rules_restated(sid)
            return True
    except Exception:                                            # noqa: BLE001
        pass
    return False

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
    if sess.native_session_id and not _due_for_restate(sess):
        addition = ""
    else:
        addition = _system_prompt_addition(
            text, has_brief=write_task_brief(sess.project_dir, text,
                                             memory_block=_memory_block(sess, text),
                                             session_id=getattr(sess, "id", None)),
            project_dir=_pointer_dir(sess, bin_path, text))
    prompt = (text + "\n\n---\n(Standing instruction for this session: " + addition + ")") \
        if addition else text
    # --auto: "auto-approve permissions that are not explicitly denied".
    #
    # It defaults to FALSE, which in non-interactive `run` mode means every tool
    # is denied and there is no prompt to answer -- so the agent can read
    # nothing and write nothing. MEASURED on a real build turn (session
    # b513710b..., 2026-09-04): four and a half minutes spent, and the entire
    # answer was "Blocked: no tool permissions granted in this session. Read,
    # Write, Bash, PowerShell all denied -- cannot create files yet."
    #
    # claude has carried --dangerously-skip-permissions and codex
    # --dangerously-bypass-approvals-and-sandbox since this module was written;
    # opencode was simply missed, which made it the one backend that could not
    # do the job this module exists for. This module's contract, stated in its
    # first paragraph, is full tool access against a folder the user picked.
    args = ["run", "--auto", "--format", "json"]
    # opencode wants provider/model. NOT gated on _hub_backs: unlike claude and
    # codex, opencode is not a subscription -- it brings no provider of its own
    # and is signed in to whatever the user configured, while the hub-seeded
    # config is what points it here (see _seed_opencode_config). Normal still
    # sends no flag at all, so a project's own opencode.json keeps winning by
    # default; asking for Max or Swarm is an explicit override of it.
    _mid = _session_model_id(sess)
    if _mid:
        args += ["--model", "free-llm-hub/" + _mid]
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
            creationflags=_NO_WINDOW,
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


# --------------------------------------------------------------------------- #
# A turn's memory: when it starts, when it ends
# --------------------------------------------------------------------------- #

class _HubNudge(str):
    """A message the HUB sends (the auto-continue nudge), not the user.

    A plain str to everything that runs it; the type is what lets the turn
    tell it apart from a user who happens to type the same words. It is not
    counted as a turn and not remembered as the user's -- four nudges used to
    fill a third of the twelve "last few turns" slots with the hub talking to
    itself."""
    __slots__ = ()


def _keep_native(sess, native_id):
    """Keep the CLI's own thread id the moment a turn reports it: on the live
    session (what the next turn resumes) and in the saved conversation (what a
    resume after a restart reads). Never raises."""
    if not native_id:
        return
    try:
        if sess.native_session_id != native_id:
            sess.native_session_id = native_id
        agentic_history.set_native_session_id(getattr(sess, "id", None), native_id)
    except Exception:                                            # noqa: BLE001
        pass


def _memory_turn_start(session_id, text, project_dir=None):
    """What memory records when a VALIDATED turn starts. Never raises.

    A DURABLE turn count: _Session.turn_count resets to 0 when a session is
    resumed, and the 5-hourly auto-update restart resumes everything -- so it
    cannot answer "how long has this conversation been going", which is what
    _due_for_restate depends on."""
    try:
        # The FIRST message is the job. Everything else in memory is derived
        # (a recap of turns that have scrolled away); this is the one thing a
        # session must not lose, and losing it is what "the agent stopped
        # before the end" looks like from the inside -- it no longer knows what
        # the end was. Recorded as a fact, so context_block never trims it.
        if memory.note_turn(session_id) == 1:
            memory.remember_fact(session_id, "The original request: "
                                 + " ".join((text or "").split())[:240],
                                 project_dir=project_dir)
        # SHORT horizon: a one-line trace of the turn, kept for the moment
        # compaction drops the real thing out of the window.
        memory.remember_recent(session_id, text, "user")
        # A PROGRESS.md the user edited since the last turn changes the list
        # without the agent having seen it (should_restate_rules picks it up).
        if project_dir:
            memory.update_tasks_from_project(session_id, project_dir)
        # "continue" / "reprends" with something to continue: hand over the
        # stopping place and the list now, not on the next scheduled restate.
        if memory.asks_to_resume(text) and memory.has_unfinished_work(session_id):
            memory.request_restate(session_id)
    except Exception:                                            # noqa: BLE001
        pass


def _memory_turn_end(session_id, request, project_dir=None, reply=None,
                     interrupted=False, why=None, doing=(), partial="", tools=()):
    """What memory records when a turn ends. Never raises.

    A finished turn refreshes the task list (the project's PROGRESS.md first,
    the reply's own checklist second) as one the agent has SEEN -- it wrote
    it -- and clears any earlier stopping place; a turn that did not finish
    files where it got to."""
    try:
        if reply and not interrupted:
            memory.clear_interrupted(session_id)
            memory.remember_recent(session_id, reply, "agent")
            memory.update_tasks(session_id, reply, project_dir, seen=True)
            # LONG horizon: what this turn made durable (no model call).
            memory.harvest_facts(session_id, request=request, reply=reply,
                                 project_dir=project_dir, tools=tools)
        elif interrupted:
            memory.update_tasks(session_id, partial or "", project_dir)
            memory.note_interrupted(session_id, request=request, doing=list(doing or []),
                                    partial=partial or "", why=why or "error",
                                    project_dir=project_dir)
    except Exception:                                            # noqa: BLE001
        pass


def turn_busy(session_id):
    """True while a turn owns this session: its lock is held, or its live
    buffer is still open (between the processes of one turn -- a retry, an
    auto-continue -- and for a multi-session turn, which holds no lock)."""
    with _REGISTRY_LOCK:
        sess = _REGISTRY.get(session_id)
    try:
        if sess is not None and sess.turn_lock.locked():
            return True
    except Exception:                                            # noqa: BLE001
        pass
    return turn_is_live(session_id)


def precheck_turn(session_id, text, cap=True):
    """The checks a turn would fail, run BEFORE anything is recorded for it.
    Returns None when the turn may run, else (status, detail).

    The routes recorded the user's message into the transcript first and only
    then learned the turn was refused -- so a second tab sending while a turn
    ran left a phantom user turn in the history."""
    if not _master_on():
        return 403, "Agentic chat is turned off (agentic_chat_enabled=False)."
    with _REGISTRY_LOCK:
        sess = _REGISTRY.get(session_id)
    if sess is None:
        return 404, "No such agentic session."
    if not isinstance(text, str) or not text.strip():
        return 400, "Message text is required."
    if cap:
        limit = max_message_chars(getattr(sess, "cli_id", None))
        if len(text) > limit:
            return 400, "Message is %d chars; capped at %d per turn." % (len(text), limit)
    if turn_busy(session_id):
        return 409, "A turn is already running for this session."
    return None


def _session_context_window(sess):
    """The model window this session's CLI is told it has, in tokens, or None.

    Codex and opencode are configured by the hub itself with
    _CODEX_CONTEXT_WINDOW (model_context_window / limit.context); claude's
    models are 200K. Only used to size the memory block."""
    cli = getattr(sess, "cli_id", None)
    if cli in ("codex", "opencode"):
        return _CODEX_CONTEXT_WINDOW
    if cli == "claude":
        return 200000
    return None


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
    _cap = max_message_chars(sess.cli_id)
    if len(text) > _cap:
        return 400, None, ("Message is %d chars; capped at %d per turn here (keeps the "
                           "command line under the Windows limit for how this CLI is "
                           "launched)." % (len(text), _cap))
    supported, reason = _SUPPORT.get(sess.cli_id, (False, "unknown CLI"))
    if not supported:
        return 403, None, "%s agentic mode is not currently supported: %s" % (sess.cli_id, reason)
    if not sess.turn_lock.acquire(blocking=False):
        return 409, None, "A turn is already running for this session."
    sess.stop_pending = False
    # THE PLAIN ROUTE KEPT NO MEMORY AT ALL: no turn count, no trace, no task
    # list, no stopping place -- a conversation driven through it looked, to
    # the next streamed turn, like it had never happened. Same bookkeeping as
    # the streaming path now, marker included (see memory.begin_inflight).
    outcome = [None, None, None]            # status, reply, detail
    started = [False]
    try:
        try:
            memory.begin_inflight(session_id, text, getattr(sess, "project_dir", None))
        except Exception:                                        # noqa: BLE001
            pass
        outcome[:] = _send_message_locked(sess, text, started)
        return tuple(outcome)
    finally:
        sess.turn_lock.release()
        status, reply, detail = outcome
        if started[0]:
            _memory_turn_end(session_id, text, getattr(sess, "project_dir", None),
                             reply=reply if status == 200 else None,
                             interrupted=status != 200,
                             why=("stopped" if status == 499 else
                                  _sanitize(str(detail or "error"), 60)))
        try:
            memory.end_inflight(session_id)
        except Exception:                                        # noqa: BLE001
            pass


def _send_message_locked(sess, text, started):
    """send_message's turn, run while it holds the session's turn lock.
    Sets started[0] once the turn is past its last refusal and really runs."""
    bin_path = _resolve_bin(sess.cli_id)
    if not bin_path:
        return 502, None, "'%s' is no longer on PATH." % _CLI_BIN[sess.cli_id]
    if _should_check_binary_identity(sess):
        ok, detail = _verify_claude_binary_identity(bin_path)
        if not ok:
            return _BINARY_IDENTITY_FAIL_STATUS, None, detail
    # Past every refusal: THIS is a turn (see _memory_turn_start).
    started[0] = True
    _memory_turn_start(getattr(sess, "id", None), text, getattr(sess, "project_dir", None))
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
    transient_retry_used = False
    while True:
        was_resume = bool(sess.native_session_id)
        argv = _build_argv(sess, bin_path, text)
        try:
            proc = subprocess.Popen(
                argv, cwd=sess.project_dir,
                env=_agentic_env(sess.cli_id, sess.project_dir,
                                  getattr(sess, "quality", "normal"),
                                  getattr(sess, "id", None),
                                  getattr(sess, "mode", None)),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                **_tree_popen_kwargs())
        except (OSError, ValueError) as exc:
            return 502, None, "%s failed to start: %s" % (sess.cli_id, exc.__class__.__name__)
        sess.last_interrupted = False
        with sess.proc_lock:
            sess.proc = proc
            stop_now, sess.stop_pending = sess.stop_pending, False
        if stop_now:
            sess.last_interrupted = True
            _terminate(proc)
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
            # The thread a stopped FIRST turn created is the only handle to
            # what it did; without it the next message starts over.
            _keep_native(sess, _best_effort_native_id(sess.cli_id, stdout))
            return 499, None, "Turn was stopped."
        if timed_out:
            salvaged = _best_effort_native_id(sess.cli_id, stdout)
            if salvaged:
                _keep_native(sess, salvaged)
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
            if _looks_transient(detail) and not transient_retry_used:
                transient_retry_used = True
                time.sleep(_TRANSIENT_RETRY_WAIT)
                continue
            return 502, None, detail
        if native_id:
            _keep_native(sess, native_id)
        sess.turn_count += 1
        return 200, result_text, None


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


def _stall_diagnosis(sess, proc, last_tool, last_line_at, marker=None, since=None):
    """At a stall, before the kill: is the CLI's shell blocked on a server it
    started? agent_servers.diagnose_stall's dict, or None. Never raises --
    this runs on the watchdog thread, which must still kill the process.

    The tree under the CLI plus this turn's orphans (processes carrying
    `marker`, started after the last line `since`): a server launched with
    `start /B` or a bare `&` outlives its shell and is in no tree."""
    try:
        cmd, at = last_tool[0], last_tool[1]
        pid = getattr(proc, "pid", None)
        procs = list(agent_servers.session_processes(pid) or [])
        procs += list(agent_servers.orphan_processes(
            marker, since, {p.get("pid") for p in procs} | {pid}) or [])
        return agent_servers.diagnose_stall(
            pid, cli_id=getattr(sess, "cli_id", None),
            last_tool=cmd, tool_was_last=(at is not None and at == last_line_at),
            processes=procs)
    except Exception:                                            # noqa: BLE001
        return None


def _early_server_check(sess, proc, last_tool, last_line_at, marker, since):
    """The early (~60 s) server check: agent_servers.early_server_diagnosis's
    dict or None. Never raises (watchdog thread)."""
    try:
        cmd, at = last_tool[0], last_tool[1]
        announced = (getattr(sess, "cli_id", None) in agent_servers.TOOL_EVENT_AT_START
                     and at is not None and at == last_line_at)
        return agent_servers.early_server_diagnosis(
            getattr(proc, "pid", None), marker=marker, since=since, now=time.time(),
            last_tool=cmd if announced else None)
    except Exception:                                            # noqa: BLE001
        return None


def _stop_turn_servers(diag, marker):
    """After the CLI's tree kill: stop the diagnosed servers the tree kill
    could not reach (orphans) and record what is still alive in
    diag["survivors"], so the resume prompt never claims a port is free when
    it is not. Only processes carrying this turn's marker are touched."""
    try:
        diag["survivors"] = agent_servers.stop_processes(
            diag.get("stop") or diag.get("pids") or [], marker)
    except Exception:                                            # noqa: BLE001
        diag["survivors"] = []


def _server_resume_prompt(sess, original, diag, resumed):
    """What the turn is resumed with after a server-blocked stall.

    Resuming the CLI's own thread: the instruction alone -- the thread already
    holds the task, and re-sending the user's words is what left the model
    guessing why it had been stopped. Starting over (no thread id): the task
    first, then the instruction, as long as the pair fits the per-turn cap;
    past it the task alone (the brief file carries the same rules)."""
    note = agent_servers.resume_instruction(diag, (diag or {}).get("silent")
                                            or _STALL_TIMEOUT)
    if resumed:
        return _HubNudge(note)
    combined = str(original) + chr(10) + chr(10) + "---" + chr(10) + note
    if len(combined) <= max_message_chars(getattr(sess, "cli_id", None)):
        return combined
    return original


def send_message_stream(session_id, text):
    """Generator: run ONE turn, yielding normalized progress events as they occur.
    Same validation / turn-lock / tree-kill model as send_message(). Always ends
    with exactly one {"event":"done",...}, {"event":"error",...}, or
    {"event":"stopped"}. Never raises."""
    # The durable turn count and the user's trace are kept by
    # _memory_turn_start -- AFTER the validation below, once this turn holds
    # the lock. Counted before it, a second tab's rejected send (409) or an
    # empty/oversized one (400) was a phantom turn in memory.
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
    _cap = max_message_chars(getattr(sess, "cli_id", None))
    if len(text) > _cap:
        yield err(400, "Message is %d chars; capped at %d per turn."
                  % (len(text), _cap)); return
    supported, reason = _SUPPORT.get(sess.cli_id, (False, "unknown CLI"))
    if not supported:
        yield err(403, "%s agentic mode is not supported: %s" % (sess.cli_id, reason)); return
    if not sess.turn_lock.acquire(blocking=False):
        yield err(409, "A turn is already running for this session."); return
    sess.stop_pending = False
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

        # Validated, holding the lock, past every refusal: THIS is a turn. The
        # hub's own auto-continue nudge is not a user turn and is not counted
        # as one.
        if not isinstance(text, _HubNudge):
            _memory_turn_start(session_id, text, sess.project_dir)

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
        transient_retry_used = False
        # A stall whose shell is blocked on a server the turn started is NOT a
        # wedge: the model did something specific and can be told what. The
        # first one resumes with that instruction without spending the wedge
        # retry above (see _stall_diagnosis / agent_servers).
        server_resume_used = False
        original_text = text
        attempt = 0
        while True:
            was_resume = bool(sess.native_session_id)
            argv = _build_argv(sess, bin_path, text, stream=True)
            child_env = _agentic_env(sess.cli_id, sess.project_dir,
                                     getattr(sess, "quality", "normal"),
                                     getattr(sess, "id", None),
                                     getattr(sess, "mode", None))
            # Inherited by every process this attempt's CLI starts, detached
            # ones included: how the watchdog recognises (and is allowed to
            # stop) a server that outlived the shell that launched it.
            attempt += 1
            turn_marker = "%s/%d/%s" % (getattr(sess, "id", None) or "agent", attempt,
                                        uuid.uuid4().hex[:12])
            child_env[agent_servers.TURN_MARKER] = turn_marker
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
                stop_now, sess.stop_pending = sess.stop_pending, False
            if stop_now:
                sess.last_interrupted = True
                _terminate(proc)

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
            stalled = [False]
            last_event = [time.monotonic()]
            # The same instant on the wall clock: process start times are
            # compared with it (a server started after the last line belongs
            # to a command that has not returned).
            last_line_wall = [time.time()]
            # The last tool event and the time of the line that carried it:
            # equal to last_event[0] at a stall means it was the very last
            # thing the CLI printed.
            last_tool = [None, None]
            stall_diag = [None]
            watchdog_stop = threading.Event()

            def _watch():
                """Kill on EITHER the wall clock or a silence, whichever first.

                Both end in the same place -- timed_out[0], which the code
                below already knows how to resume from -- because the right
                response to a wedged turn and an over-long one is the same:
                hand the CLI its thread id back and carry on."""
                started = time.monotonic()
                # Poll fast enough to honour whichever deadline is SHORTER.
                # A hub configured with a 30-second turn timeout must not wait
                # five seconds to notice it passed, and a test that sets one
                # of these to a fraction of a second is asking the same thing.
                tick = min(_WATCH_TICK_MAX,
                           max(0.02, min(_TURN_TIMEOUT, _STALL_TIMEOUT or _TURN_TIMEOUT,
                                         _SERVER_PROBE_AFTER or _TURN_TIMEOUT) / 4.0))
                next_probe = 0.0
                while not watchdog_stop.wait(tick):
                    now = time.monotonic()
                    if now - started > _TURN_TIMEOUT:
                        timed_out[0] = True
                        _terminate(proc)
                        return
                    silent = now - last_event[0]
                    # Both silences end the same way below; the diagnosis
                    # runs BEFORE the kill -- the tree is only walkable while
                    # the CLI is alive to be its root.
                    if _STALL_TIMEOUT and silent > _STALL_TIMEOUT:
                        diag = _stall_diagnosis(sess, proc, last_tool, last_event[0],
                                                turn_marker, last_line_wall[0])
                        if diag:
                            _log.warning("agentic turn silent for %ds: shell blocked "
                                         "on %r ports=%s via=%s (session=%s cli=%s)",
                                         _STALL_TIMEOUT, diag.get("command"),
                                         diag.get("ports"), diag.get("source"),
                                         getattr(sess, "id", "?"),
                                         getattr(sess, "cli_id", "?"))
                        else:
                            _log.warning("agentic turn produced nothing for %ds "
                                         "(session=%s cli=%s) -- treating as wedged",
                                         _STALL_TIMEOUT, getattr(sess, "id", "?"),
                                         getattr(sess, "cli_id", "?"))
                    elif (_SERVER_PROBE_AFTER and silent > _SERVER_PROBE_AFTER
                            and now >= next_probe):
                        # Not a wedge yet: only a server blocking the shell
                        # (and nothing else) ends the attempt this early.
                        next_probe = now + _SERVER_PROBE_EVERY
                        diag = _early_server_check(sess, proc, last_tool, last_event[0],
                                                   turn_marker, last_line_wall[0])
                        if not diag:
                            continue
                        diag["silent"] = int(silent)
                        _log.warning("agentic turn silent for %ds: shell blocked on "
                                     "server %r ports=%s pids=%s orphans=%s -- "
                                     "resuming early (session=%s cli=%s)",
                                     int(silent), diag.get("command"),
                                     diag.get("ports"), diag.get("pids"),
                                     diag.get("orphans"), getattr(sess, "id", "?"),
                                     getattr(sess, "cli_id", "?"))
                    else:
                        continue
                    stall_diag[0] = diag
                    stalled[0] = True
                    timed_out[0] = True
                    _terminate(proc)
                    if diag:
                        # What the tree kill cannot reach: orphans.
                        _stop_turn_servers(diag, turn_marker)
                    return

            timer = threading.Thread(target=_watch, daemon=True,
                                     name="agentic-watchdog-%s"
                                          % getattr(sess, "id", "?"))
            timer.start()

            native_id = None
            final_text = None
            final_error = None
            last_message_text = None
            try:
                for line in proc.stdout:
                    # Any line at all is proof of life -- including one that
                    # parses to nothing we act on.
                    last_event[0] = time.monotonic()
                    last_line_wall[0] = time.time()
                    for e in parse(line):
                        if "_native" in e:
                            native_id = e["_native"]
                            # KEPT THE MOMENT IT IS KNOWN. It sat in this local
                            # until the turn ended cleanly, so stopping a first
                            # turn returned before it was saved and the next
                            # message started a brand-new CLI thread.
                            _keep_native(sess, native_id)
                        if "_final" in e:
                            final_text = e["_final"]
                        if "_final_error" in e:
                            final_error = e["_final_error"]
                        if e.get("event") == "message" and e.get("text"):
                            last_message_text = e["text"]
                        if e.get("event") == "tool":
                            last_tool[0], last_tool[1] = e.get("text"), last_event[0]
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
                watchdog_stop.set()
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
                # SILENT BECAUSE ITS SHELL IS BLOCKED ON A SERVER. MEASURED
                # live 2026-09-27: resumed with the user's own words again,
                # the model had no idea why it was stopped, hunted python
                # processes, killed one, and blocked the same way -- "produced
                # nothing for 420s twice". Resume with what happened and the
                # detached spelling instead; the first time does not spend
                # the wedge retry, and the notice says "server running on
                # port N", not "looks wedged".
                diag = stall_diag[0] if stalled[0] else None
                if diag and (not server_resume_used or not timeout_retry_used):
                    again = server_resume_used
                    if again:
                        timeout_retry_used = True
                    server_resume_used = True
                    yield {"event": "notice",
                           "text": agent_servers.stall_notice(
                               diag, diag.get("silent") or _STALL_TIMEOUT, again=again)}
                    # The next attempt resumes whenever the session holds a
                    # thread id -- captured now or carried from before.
                    text = _server_resume_prompt(sess, original_text, diag,
                                                 bool(sess.native_session_id))
                    continue
                if diag:
                    yield err(504, agent_servers.failure_detail(sess.cli_id, diag)); return
                # SILENT BECAUSE NO MODEL COULD SERVE IT (see _upstream_outage):
                # resuming would send the same conversation into the same
                # outage, so stop and say why.
                outage = (_upstream_outage(sess, last_line_wall[0]) if stalled[0] else None)
                if outage:
                    _log.warning("agentic turn stopped: %d request(s) found no model "
                                 "(session=%s cli=%s why=%s)", outage.get("failures", 0),
                                 getattr(sess, "id", "?"), sess.cli_id, outage.get("why"))
                    yield err(503, outage_detail(sess.cli_id, outage)); return
                if not timeout_retry_used:
                    timeout_retry_used = True
                    # Not about a server this time: the request itself again,
                    # never a server note left over from an earlier resume.
                    text = original_text
                    yield {"event": "notice",
                          "text": (("Nothing for %ds — looks wedged, %s."
                                    % (_STALL_TIMEOUT,
                                       "resuming" if native_id else "trying again"))
                                   if stalled[0] else
                                   ("Still working after %ds — %s."
                                    % (_TURN_TIMEOUT,
                                       "resuming" if native_id else "trying again")))}
                    continue
                yield err(504, "%s %s (retried once)."
                         % (sess.cli_id,
                            ("produced nothing for %ds twice" % _STALL_TIMEOUT)
                            if stalled[0] else
                            ("timed out after %ds" % _TURN_TIMEOUT))); return

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
                # A fresh thread knows nothing of a server-resume note meant
                # for the old one: it gets the request itself again.
                text = original_text
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
                if _looks_transient(detail) and not transient_retry_used:
                    # The CLI said itself that this was temporary. Resuming is
                    # free when there is a thread id -- the work already done
                    # is still there -- so the only thing spent is the wait.
                    transient_retry_used = True
                    if native_id:
                        sess.native_session_id = native_id
                    yield {"event": "notice",
                          "text": "%s was busy (%s) -- %s."
                                  % (sess.cli_id,
                                     _sanitize(detail, 60).strip(),
                                     "resuming" if native_id else "trying again")}
                    time.sleep(_TRANSIENT_RETRY_WAIT)
                    continue
                yield err(502, detail); return
            sess.turn_count += 1
            yield {"event": "done", "text": _sanitize(final_text), "native": native_id}
            return
    finally:
        if timer:
            watchdog_stop.set()
        with sess.proc_lock:
            if sess.proc is proc:
                sess.proc = None
        sess.turn_lock.release()



# --------------------------------------------------------------------------- #
# Keeping an agent going to the end of the job
# --------------------------------------------------------------------------- #
#
# REPORTED 2026-09-10: "pourquoi les agents ne continuent pas et ils s'arretent
# et ils continuent pas jusqu'au bout".
#
# A CLI agent ends its turn when IT decides it is done, and the common failure
# is that it decides that in the middle: it writes a todo list, does the first
# item, describes the next one, and stops. Nothing was broken -- the process
# exited zero, the reply was recorded, and the work is half finished. Until now
# the only fix was a human typing "continue".
#
# The hub already knows a turn ended and already has the machinery to send
# another. So it reads the reply, and when the reply itself says the work is
# not finished, it sends the nudge instead of waiting for someone to notice.
#
# Deliberately narrow, because the failure mode of the opposite mistake is a
# CLI running forever on a job that IS done:
#   * it stops at _MAX_AUTO_CONTINUE, always;
#   * a reply that asks a QUESTION is never continued -- being asked something
#     is the agent doing its job, and answering it is not the hub's to do;
#   * a reply with no sign of unfinished work is left alone;
#   * an interrupted turn is never continued.
_MAX_AUTO_CONTINUE = 4
_CONTINUE_NUDGE = (
    "Continue. Work through the remaining items yourself and do not stop to "
    "report progress -- the todo list is the report. If something genuinely "
    "blocks you, say what it is; otherwise keep going until the job is done."
)

# An unchecked markdown checkbox is the strongest possible signal: the agent
# wrote the list itself and left items on it.
_UNCHECKED_RE = re.compile(r"^\s*[-*]\s*\[\s\]", re.M)
# "next I will", "now I'll", "let me now" -- an intention, stated at the end.
_NEXT_STEP_RE = re.compile(
    r"\b(next|then|now|after that|remaining|still need|todo|to do)\b[^.\n]{0,80}"
    r"\b(i(?:'|\u2019)?ll|i will|let me|we(?:'|\u2019)?ll|we will|going to)\b",
    re.I)
_ALT_NEXT_RE = re.compile(
    r"\b(i(?:'|\u2019)?ll|i will|let me|going to|we(?:'|\u2019)?ll)\b[^.\n]{0,60}"
    r"\b(next|now|then|continue|proceed)\b", re.I)
# Said plainly enough that continuing would be wrong.
_FINISHED_RE = re.compile(
    r"\b(all done|everything (is )?done|finished|complete[d]?|"
    r"nothing (else|more) (to do|left)|ready to use|that(?:'|\u2019)?s everything)\b",
    re.I)


def looks_unfinished(text):
    """Does this reply say, in its own words, that the work is not done?

    Read from the AGENT's own output rather than guessed from tool counts: an
    agent that stops after one tool call may be finished, and one that made
    thirty may not be. What it wrote is the only evidence of what it thinks is
    left."""
    if not text or not isinstance(text, str):
        return False
    body = text.strip()
    if not body:
        return False
    # Being asked a question is the agent doing its job. Answering it is the
    # user's, and a nudge would talk over them.
    tail = body[-400:]
    if "?" in tail:
        return False
    if _UNCHECKED_RE.search(body):
        return True
    if _FINISHED_RE.search(tail):
        return False
    return bool(_NEXT_STEP_RE.search(tail) or _ALT_NEXT_RE.search(tail))


# THE TURN YOU CAN COME BACK TO.
#
# REPORTED: "in /agent when I refresh the page I don't see running what he
# was doing". A turn's events went from the CLI to whoever was reading the
# SSE response and nowhere else, so a reload mid-turn -- the most natural
# thing to do when a page looks stuck -- got a spinner that said "still
# working" over an empty panel, and nothing of what the agent was actually
# doing until the turn ended.
#
# Every turn now mirrors its events into a per-session ring buffer for as long
# as it runs, and follow_turn() replays that buffer and then stays attached
# until the turn ends. The page that reloads gets the same lines it would have
# had, from the beginning, live. Bounded (the last _LIVE_KEEP events), because
# a long build turn emits thousands of tool lines and the reply itself is
# recorded to history by the turn, not by this.
_LIVE_KEEP = 600
_LIVE = {}                      # session_id -> _LiveTurn
_LIVE_LOCK = threading.Lock()


class _LiveTurn:
    __slots__ = ("events", "dropped", "done", "cond", "started_at")

    def __init__(self):
        self.events = collections.deque(maxlen=_LIVE_KEEP)
        self.dropped = 0            # events that fell off the front
        self.done = False
        self.cond = threading.Condition()
        self.started_at = time.time()


def _live_begin(session_id):
    turn = _LiveTurn()
    with _LIVE_LOCK:
        _LIVE[session_id] = turn
    return turn


def _live_put(session_id, ev):
    with _LIVE_LOCK:
        turn = _LIVE.get(session_id)
    if turn is None:
        return
    with turn.cond:
        if len(turn.events) == turn.events.maxlen:
            turn.dropped += 1
        turn.events.append(ev)
        turn.cond.notify_all()


def _live_end(session_id):
    with _LIVE_LOCK:
        turn = _LIVE.get(session_id)
    if turn is None:
        return
    with turn.cond:
        turn.done = True
        turn.cond.notify_all()
    # Kept a short while after the end so a page that reloads just as the
    # turn finishes still sees it ended rather than "nothing running"; the
    # next turn on this session replaces it anyway.
    def _forget():
        with _LIVE_LOCK:
            if _LIVE.get(session_id) is turn:
                _LIVE.pop(session_id, None)
    threading.Timer(120, _forget).start()


def turn_is_live(session_id):
    """True while a turn on this session is still producing events."""
    with _LIVE_LOCK:
        turn = _LIVE.get(session_id)
    return bool(turn is not None and not turn.done)


def follow_turn(session_id, wait=0.5):
    """Replay what the running turn has emitted so far, then keep yielding
    until it ends. Yields nothing at all when no turn is live; a page that
    gets nothing knows to load the transcript instead."""
    with _LIVE_LOCK:
        turn = _LIVE.get(session_id)
    if turn is None:
        return
    # ABSOLUTE positions. Event number n of the turn sits at buffer index
    # n - dropped, and `sent` is the number of the next event this reader
    # owes. (The first version re-based `sent` on every read against the
    # drops since the LAST read, which is only right when nothing dropped
    # across two reads -- a follower slower than the turn skipped events.)
    with turn.cond:
        sent = turn.dropped
    if sent:
        yield {"event": "notice",
               "text": "%d earlier lines of this turn are not shown." % sent}
    while True:
        with turn.cond:
            if sent < turn.dropped:
                # Fell behind by more than the buffer holds: say so, catch up.
                lost = turn.dropped - sent
                sent = turn.dropped
                fresh = [{"event": "notice",
                          "text": "%d lines of this turn were not shown." % lost}]
                fresh += list(turn.events)
            else:
                fresh = list(turn.events)[sent - turn.dropped:]
            if not fresh and not turn.done:
                turn.cond.wait(wait)
                continue
            sent = turn.dropped + len(turn.events)
            done = turn.done
        for ev in fresh:
            yield ev
        if done and not fresh:
            return
        if done:
            # One more look: events appended between the read and `done`
            # being set are still in the buffer.
            continue


def live_run(session_id, producer, context=None):
    """Run `producer` (any generator of turn events) on its own thread,
    mirror every event into the live buffer, and relay to whoever reads this.
    The generic half of what send_message_stream_durable does for a CLI
    turn, for turns that are not one CLI process -- the multi-session run.

    `context` is a context manager entered ON THE THREAD around the producer.
    MEASURED 2026-09-12: the multi-session planner routes through the hub's
    own chain, which reads the request (`g`, headers) -- moved onto a thread
    it answered "" twice in seven seconds and the turn died at "could not
    turn that into phases". The route hands over a copy of its request
    context, and the producer runs inside it as it did before."""
    q = queue.Queue()

    def _run():
        _live_begin(session_id)
        try:
            with (context if context is not None else contextlib.nullcontext()):
                for ev in producer:
                    q.put(ev)
                    _live_put(session_id, ev)
        except Exception as exc:                                 # noqa: BLE001
            ev = {"event": "error", "status": 500, "detail": _sanitize(str(exc), 300)}
            q.put(ev)
            _live_put(session_id, ev)
        finally:
            _live_end(session_id)
            q.put(None)

    threading.Thread(target=_run, daemon=True).start()
    while True:
        ev = q.get()
        if ev is None:
            return
        yield ev


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
    if turn_busy(session_id):
        # A second tab (or a double click) while a turn runs. Refused BEFORE
        # anything is touched: starting the thread below replaced the running
        # turn's live buffer, and its 409 was filed in memory as the running
        # turn's "stopping place".
        yield {"event": "error", "status": 409,
               "detail": "A turn is already running for this session."}
        return

    def _run():
        final_reply = None
        _live_begin(session_id)
        # What the turn was doing, for the moment it does not finish: its
        # last tool calls and the text it had written. Filed by
        # memory.note_interrupted and handed to the next turn, so "continue"
        # continues (see memory.resume_block).
        doing = collections.deque(maxlen=memory.INTERRUPT_DOING)
        # Every tool call of the turn (bounded), for the fact harvest: which
        # files it wrote, which test/build commands it ran.
        tools_all = collections.deque(maxlen=200)
        partial = [""]
        why = [None]
        rejected = False
        project_dir = (sess_info or {}).get("project_dir")
        # If the PROCESS dies mid-turn, the finally below never runs; this
        # marker is what the next boot turns into the stopping place. Written
        # on the turn's FIRST event, once send_message_stream holds the turn
        # lock -- not before: a send that loses the race past turn_busy (409)
        # overwrote the RUNNING turn's marker, then its finally unlinked it,
        # so a crash of the live turn left no stopping place at all.
        began = False
        try:
            prompt, rounds = text, 0
            while True:
                final_reply = None
                interrupted = False
                seen_any = False
                for ev in send_message_stream(session_id, prompt):
                    if not began and ev.get("event") != "error":
                        began = True
                        try:
                            memory.begin_inflight(session_id, text, project_dir)
                        except Exception:                        # noqa: BLE001
                            pass
                    # The nudge itself is not shown as a user turn: the reader
                    # asked for one thing and should see one conversation.
                    q.put(ev)
                    _live_put(session_id, ev)       # for a page that reloads
                    kind = ev.get("event")
                    if kind == "tool" and ev.get("text"):
                        doing.append(ev["text"])
                        tools_all.append(ev["text"])
                        memory.touch_inflight(session_id, doing=list(doing))
                    elif kind == "message" and ev.get("text"):
                        partial[0] = ev["text"]
                        memory.touch_inflight(session_id, partial=partial[0])
                    if kind == "done":
                        final_reply = ev.get("text")
                    elif kind in ("error", "stopped"):
                        interrupted = True
                        why[0] = ("stopped" if kind == "stopped"
                                  else _sanitize(str(ev.get("detail") or "error"), 60))
                        # Refused before it ran (a race past turn_busy above):
                        # not this conversation's stopping place.
                        if (kind == "error" and not seen_any and rounds == 0
                                and ev.get("status") == 409):
                            rejected = True
                    seen_any = True
                if interrupted or rounds >= _MAX_AUTO_CONTINUE:
                    break
                if not looks_unfinished(final_reply):
                    break
                rounds += 1
                prompt = _HubNudge(_CONTINUE_NUDGE)
                nudge = {"event": "notice",
                         "text": "The agent stopped with work left on its own list "
                                 "-- continuing (%d of %d)." % (rounds, _MAX_AUTO_CONTINUE)}
                q.put(nudge)
                _live_put(session_id, nudge)
        finally:
            _live_end(session_id)
            if sess_info and final_reply:
                try:
                    after = get_session(session_id) or {}
                    agentic_history.record_turn(
                        session_id, sess_info["cli"], sess_info["project_dir"],
                        "agent", final_reply,
                        native_session_id=after.get("native_session_id"))
                except Exception:
                    pass
            # THE LIST AND THE STOPPING PLACE. A finished turn refreshes the
            # task list (the project's PROGRESS.md first, the reply's own
            # checklist second) and clears any earlier stopping place; a turn
            # that did not finish files where it got to.
            try:
                if rejected:
                    pass
                elif final_reply and not interrupted:
                    memory.clear_interrupted(session_id)
                    memory.remember_recent(session_id, final_reply, "agent")
                    # The agent wrote this list itself: seen, not news.
                    memory.update_tasks(session_id, final_reply,
                                        project_dir, seen=True)
                    # LONG horizon: decisions, preferences, files written and
                    # commands that passed -- no model call.
                    memory.harvest_facts(session_id, request=text, reply=final_reply,
                                         project_dir=project_dir, tools=list(tools_all))
                elif interrupted:
                    memory.update_tasks(session_id, partial[0], project_dir)
                    memory.note_interrupted(session_id, request=text, doing=list(doing),
                                            partial=partial[0], why=why[0] or "error",
                                            project_dir=project_dir)
            except Exception:                                    # noqa: BLE001
                pass
            if began:            # never another turn's marker (see above)
                try:
                    memory.end_inflight(session_id)
                except Exception:                                # noqa: BLE001
                    pass
            q.put(None)          # sentinel: no more events, thread is done

    threading.Thread(target=_run, daemon=True).start()
    while True:
        ev = q.get()
        if ev is None:
            return
        yield ev


# Windows opens a console window for every child process unless told not to.
# The hub spawns a lot of them -- a CLI per agent turn, a dev server per
# preview, git, pip, taskkill -- and each one flashed a black cmd window over
# whatever the user was doing. REPORTED as "le hub open each time a window
# terminal CMD ... ca derange bcp".
#
# CREATE_NO_WINDOW is safe to OR into CREATE_NEW_PROCESS_GROUP (they control
# different things) but is MUTUALLY EXCLUSIVE with CREATE_NEW_CONSOLE, which is
# why the one deliberately visible window -- the interactive CLI login -- does
# not get it.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _tree_popen_kwargs():
    """Extra Popen kwargs so a subsequent stop_session() can kill the WHOLE
    process tree (see _signal_tree) instead of only the immediate child -- and
    so it does that without a console window (see _NO_WINDOW)."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | _NO_WINDOW}
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
            # No process alive, but a turn may own the session (before its
            # first process, or between two). MEASURED 2026-09-30: once the
            # page showed "working" for that whole span, a Stop there found
            # nothing to kill and the next process ran on. Leave the Stop
            # for the turn's next process (checked under this same lock).
            if sess.turn_lock.locked():
                sess.stop_pending = True
                return True
            return False
    sess.last_interrupted = True
    _terminate(proc)
    return True


def set_session_mode(session_id, mode):
    """Set one session's model MODE. False when the session does not exist.

    Takes effect on the session's NEXT turn: the mode leaves here as the model
    id its CLI is launched with, and the CLI process for a turn already running
    was started with the old one. No restart is needed beyond that -- which is
    the point of carrying it as a model id rather than as launch configuration.
    """
    with _REGISTRY_LOCK:
        sess = _REGISTRY.get(session_id)
        if sess is None:
            return False
        sess.mode = mode or None
    # Persisted for the same reason quality is: a live session does not survive
    # the 5-hourly auto-update restart, and "this conversation uses the coding
    # models" has to still be true tomorrow.
    agentic_history.set_mode(session_id, mode)
    return True


def _durable_turns(sess):
    """How many turns this conversation has really had. Never raises."""
    try:
        return max(int(sess.turn_count or 0),
                   int(memory.get(sess.id).get("turns") or 0))
    except Exception:                                            # noqa: BLE001
        return int(getattr(sess, "turn_count", 0) or 0)


def get_session(session_id):
    """Status dict for one session, or None if it doesn't exist. Never raises."""
    with _REGISTRY_LOCK:
        sess = _REGISTRY.get(session_id)
    if sess is None:
        return None
    with sess.proc_lock:
        proc = sess.proc
    running = bool(proc is not None and proc.poll() is None)
    # A turn is running for its WHOLE length, not only while a CLI process is
    # alive: between the processes of one turn (a retry, an auto-continue) the
    # process slot is empty but the turn still owns the session. MEASURED
    # 2026-09-30: a page opened in that gap showed an idle conversation.
    if not running:
        try:
            running = turn_busy(sess.id)
        except Exception:                                        # noqa: BLE001
            pass
    return {
        "session_id": sess.id,
        "cli": sess.cli_id,
        "quality": getattr(sess, "quality", "normal"),
        "mode": getattr(sess, "mode", None),
        "project_dir": sess.project_dir,
        # The DURABLE count when there is one. _Session.turn_count resets to 0
        # on resume, and the 5-hourly auto-update restart resumes everything --
        # so the settings panel showed "0 turns" for a conversation forty turns
        # deep, which is precisely the question that panel exists to answer.
        "turn_count": _durable_turns(sess),
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
    if quality not in QUALITIES:
        return None
    with _REGISTRY_LOCK:
        sess = _REGISTRY.get(session_id)
        if sess is None:
            return None
        sess.quality = quality
    # Outside the registry lock: this touches the history file.
    agentic_history.set_quality(session_id, quality)
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
