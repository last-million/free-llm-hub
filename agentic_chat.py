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

Codex is intentionally NOT enabled for this feature (see _SUPPORT below):
official docs list only --last/--all/--image/PROMPT/SESSION_ID as accepted
`codex exec resume` flags (no approval/sandbox override in that table), and
multiple open upstream reports (openai/codex #9144, #5322) describe
`--dangerously-bypass-approvals-and-sandbox` being silently ignored after
`codex exec resume`. Since every turn from turn 2 onward NEEDS resume+bypass
together, and this feature runs fully non-interactively (no human to click
through an approval prompt that silently came back), an unverified combination
here could hang or block a turn with no recovery. Scoped down rather than
guessed at -- see start_session().

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
import os
import shutil
import signal
import subprocess
import threading
import time
import uuid

import config
import vision_status

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

_CLI_BIN = {"claude": "claude", "codex": "codex"}

# Claude Code is the only currently-working agentic backend (see _SUPPORT) --
# this is the API default when a caller omits `cli`, AND the value the
# dashboard's CLI picker should preselect (read via default_cli()).
_DEFAULT_CLI = "claude"

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
    "codex": (False, (
        "Codex agentic mode is not enabled: full-permission bypass "
        "(--dangerously-bypass-approvals-and-sandbox) is not confirmed to survive "
        "`codex exec resume` -- the official resume flag table lists only "
        "--last/--all/--image/PROMPT/SESSION_ID (no approval/sandbox override), and open "
        "upstream reports (openai/codex #9144, #5322) describe the bypass flag being "
        "silently ignored after resume. Every turn from turn 2 onward needs resume+bypass "
        "together here, and this feature is fully non-interactive -- a silently-reverted "
        "approval prompt would have no human to answer it and no clean way to recover. "
        "Scoped down rather than risking an unverified flag combination."
    )),
}

# Each agentic turn can run real tool use (file edits, shell commands), so this
# is deliberately much longer than app.py's one-shot _SUB_TIMEOUT (120s).
# Configurable, mirroring the PORT env-var convention already used in app.py.
_TURN_TIMEOUT = int(os.environ.get("AGENTIC_CHAT_TIMEOUT", "600") or "600")

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
_MAX_MESSAGE_CHARS = 6000

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
            installed = bool(shutil.which(_CLI_BIN[cid]))
        except Exception:
            installed = False
        out[cid] = {"supported": ok, "reason": reason, "installed": installed}
    return out


def default_cli() -> str:
    """The CLI id start_session() defaults to when the caller omits `cli`, and
    the value the dashboard's CLI picker should preselect. See _DEFAULT_CLI."""
    return _DEFAULT_CLI


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


def _agentic_env() -> dict:
    """Child env with every hub-pointing override stripped. Everything else
    (PATH, HOME, the user's own settings) passes through unchanged."""
    env = dict(os.environ)
    for k in list(env.keys()):
        if _points_at_hub(env.get(k)):
            env.pop(k, None)
    return env


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


def _sanitize(text, limit=None):
    """Never let a provider key (or the local key) leak into an error surfaced
    to the client. `limit=None` leaves successful result text un-truncated;
    error/detail strings pass a small limit, mirroring app.py's _sanitize()."""
    s = str(text if text is not None else "")
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
                 "created_at", "proc", "proc_lock", "turn_lock", "last_interrupted")

    def __init__(self, cli_id, project_dir):
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


def start_session(cli_id, project_dir, create_new=False) -> str:
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
    bin_path = shutil.which(_CLI_BIN[cli_id])
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
    sess = _Session(cli_id, abs_dir)
    with _REGISTRY_LOCK:
        _REGISTRY[sess.id] = sess
    _remember_recent_project(abs_dir)
    return sess.id


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


def _system_prompt_addition() -> str:
    """Extra --append-system-prompt text for this turn, or "" for none.
    Two independently-gated, additive pieces -- never raises (a diagnostics
    read failing must never block a turn from running)."""
    parts = []
    if test_verification_enabled():
        parts.append(_TEST_VERIFICATION_SNIPPET)
    try:
        available = vision_status.status().get("available")
    except Exception:
        available = True  # fail closed on the NOTICE (stay silent), not on the turn
    if not available:
        parts.append(_VISION_GAP_SNIPPET)
    return " ".join(parts)


def _build_argv(sess: _Session, bin_path: str, text: str):
    if sess.cli_id != "claude":
        # Unreachable in practice -- start_session() already refuses any CLI
        # whose _SUPPORT entry is False, and _SUPPORT only allows "claude"
        # through today. Fail loudly rather than silently mis-running it if
        # that ever changes without updating this function.
        raise AgenticError("No known invocation for CLI '%s'." % sess.cli_id, 400)
    args = ["-p", text]
    if sess.native_session_id:
        args += ["--resume", sess.native_session_id]
    args += ["--output-format", "json", "--dangerously-skip-permissions",
             "--model", _MODEL_ALIAS]
    addition = _system_prompt_addition()
    if addition:
        args += ["--append-system-prompt", addition]
    return _launcher(bin_path) + args


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
    if data.get("is_error"):
        msg = data.get("result") or data.get("error") or "unknown error"
        return None, native_id, _sanitize(str(msg), 500)
    result = data.get("result")
    if not isinstance(result, str) or not result:
        return None, native_id, "claude returned no result text."
    return _sanitize(result), native_id, None


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
            env=_agentic_env())
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
        bin_path = shutil.which(_CLI_BIN[sess.cli_id])
        if not bin_path:
            return 502, None, "'%s' is no longer on PATH." % _CLI_BIN[sess.cli_id]
        if _should_check_binary_identity(sess):
            ok, detail = _verify_claude_binary_identity(bin_path)
            if not ok:
                return _BINARY_IDENTITY_FAIL_STATUS, None, detail
        argv = _build_argv(sess, bin_path, text)
        try:
            proc = subprocess.Popen(
                argv, cwd=sess.project_dir, env=_agentic_env(),
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
            return 504, None, "%s timed out after %ds." % (sess.cli_id, _TURN_TIMEOUT)
        result_text, native_id, detail = _parse_claude_json(stdout, stderr, proc.returncode)
        if result_text is None:
            detail = detail or "%s produced no output." % sess.cli_id
            status = 403 if _looks_like_auth_error(detail) else 502
            return status, None, detail
        if native_id:
            sess.native_session_id = native_id
        sess.turn_count += 1
        return 200, result_text, None
    finally:
        sess.turn_lock.release()


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
        "project_dir": sess.project_dir,
        "turn_count": sess.turn_count,
        "currently_running": running,
        "created_at": sess.created_at,
        "has_native_session": bool(sess.native_session_id),
    }


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
