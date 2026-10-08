"""Several REAL agent sessions, running in parallel, in the background.

WHAT THIS IS, AND WHAT IT IS NOT
--------------------------------
`swarm.py` fans one prompt out across several MODELS and picks a winner. That
is a chat-completion pipeline: every stage is one request, and nothing it does
touches a file.

This is the other thing entirely. Each worker here is a real agent SESSION --
its own CLI process, its own project directory, its own context window, its own
tool access -- and the conversation you are talking to orchestrates them. The
work is dispatched as a phased todo list where a phase waits for the phases it
depends on.

WHY EACH WORKER GETS ITS OWN SESSION
------------------------------------
Because a context window is the scarce resource. Handing one agent a five-phase
job means every phase pays for every earlier phase's transcript; handing five
agents one phase each means each pays for its own. The orchestrator still sees
everything, because it reads their SUMMARIES rather than their transcripts --
the distinction that keeps the parent's own window from being the bottleneck.

NO VISIBLE WINDOWS
------------------
Workers are ordinary background children. The console-window suppression lives
where the process is actually spawned (agentic_chat._tree_popen_kwargs), so
nothing here has to think about it -- which is the point of dispatching through
the existing session machinery instead of a second launcher.

EVERYTHING IS INJECTED
----------------------
`spawn`, `run_turn` and `planner` are parameters, not imports. app.py passes the
real ones (agentic_chat.start_session, send_message_stream_durable, and the
hub's own chat dispatch); the tests pass fakes. This module therefore has NO
import of app.py or agentic_chat -- the same cycle-avoidance the rest of the
codebase uses, and what lets the whole orchestrator be tested without spawning
a single process.

FAILURE IS EXPECTED, NOT EXCEPTIONAL
------------------------------------
A worker that dies, hangs or returns nothing marks itself failed and the wave
carries on. This mirrors swarm.py, which learned it the hard way: one dead
member must never take the run with it. A phase whose dependencies failed still
runs -- it simply gets less context, which every agent prompt already tolerates.
"""
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid

_log = logging.getLogger("free-llm-hub")
from collections import deque

import agent_servers                 # a leaf too: server rules, hub PID/port
import answer_check                  # a leaf like this one: no app import
import evidence                      # pure: observed test/build verdicts
import plan_check                    # pure: the design + the plan's dry run
import receipts                      # what was observed, on disk

_SUMMARY_THINK_RE = re.compile(r"<(think|thinking)>(.*?)</\1>", re.I | re.S)
_SUMMARY_THINK_OPEN_RE = re.compile(r"<(?:think|thinking)>(.*)$", re.I | re.S)
_SUMMARY_EMPTY_FENCE_RE = re.compile(r"```[\w+-]*\s*```")


def clean_summary(text):
    """A phase's reply as the report shows it: leaked reasoning blocks out.

    MEASURED 2026-09-27 (/agent multi run swarm-7cfdb7f6e0a6): phase 2's whole
    reply was "```html\\n<think>The user has successfully created ...</think>\\n```"
    and the report printed it verbatim -- a reasoning block in a code fence as
    the phase's result. The text OUTSIDE the blocks is the summary; when there
    is none, the reasoning's own words (tags and empty fences removed) stand in,
    so a phase that did its work is never emptied into "produced no result"."""
    if not isinstance(text, str):
        return ""
    raw = text.strip()
    if "<think" not in raw.lower():
        return raw
    inner = [m.group(2).strip() for m in _SUMMARY_THINK_RE.finditer(raw)]
    outside = _SUMMARY_THINK_RE.sub("", raw)
    m = _SUMMARY_THINK_OPEN_RE.search(outside)
    if m:
        inner.append(m.group(1).strip())
        outside = outside[:m.start()]
    outside = _SUMMARY_EMPTY_FENCE_RE.sub("", outside).strip()
    if outside:
        return outside
    return "\n\n".join(x for x in inner if x).strip() or raw

# How many workers may run at once. Each one is a real CLI process with a model
# behind it, so this is a RAM, CPU and rate-limit bound -- the user's own words:
# "it will consume the ram more". Owner, 2026-10-04: "I prefer 4 DIFFERENT
# models at once, or 5-6 if needed". The cap is the setting multi_parallel_max
# (default MAX_CONCURRENT = 6, range 1..MAX_PARALLEL_LIMIT); _concurrency() is
# the smallest of that, what the machine holds, how many distinct good models
# the fleet has and the 429 back-off.
MAX_CONCURRENT = 6
MAX_PARALLEL_LIMIT = 8
LEGACY_CONCURRENT = 4          # what the old fixed cap was: the no-numbers fallback
FLOOR_PARALLEL = 2             # fleet and 429 limits never go under this
BACKOFF_429_PER = 3            # one helper fewer per this many recent 429s...
BACKOFF_WINDOW = 120.0         # ...inside this many seconds

_HOOKS = {"fleet": None, "rate429": None}
_last_backoff = [None]


def set_fleet_counter(fn):
    """app registers fn() -> number of distinct healthy tool-capable models
    within the best-available band (quota-aware)."""
    _HOOKS["fleet"] = fn


def set_rate_counter(fn):
    """app registers fn(window_seconds) -> recent hop 429 count."""
    _HOOKS["rate429"] = fn


def parallel_cap():
    """The owner's setting multi_parallel_max, clamped 1..MAX_PARALLEL_LIMIT."""
    try:
        import config
        v = int(config.get_setting("multi_parallel_max", MAX_CONCURRENT))
    except Exception:                                            # noqa: BLE001
        v = MAX_CONCURRENT
    return max(1, min(MAX_PARALLEL_LIMIT, v))


def concurrency_info():
    """{now, max, by_machine, by_fleet, backoff, limited_by}: how the number of
    helpers allowed at once was reached. Never raises."""
    cap = parallel_cap()
    info = {"max": cap, "by_machine": cap, "by_fleet": None, "backoff": 0}
    try:
        import lowres
        m = lowres.machine()
        info["by_machine"] = min(lowres.workers(cap, m), lowres.ram_cores_cap(cap, m))
        if m.get("free_gb") is None:
            info["by_machine"] = min(info["by_machine"], LEGACY_CONCURRENT)
    except Exception:                                            # noqa: BLE001
        info["by_machine"] = cap
    n = min(cap, info["by_machine"])
    try:
        if _HOOKS["fleet"]:
            f = int(_HOOKS["fleet"]())
            if f >= 1:
                info["by_fleet"] = max(FLOOR_PARALLEL, f)
                n = min(n, info["by_fleet"])
    except Exception:                                            # noqa: BLE001
        pass
    try:
        if _HOOKS["rate429"]:
            back = int(_HOOKS["rate429"](BACKOFF_WINDOW)) // BACKOFF_429_PER
            if back > 0:
                info["backoff"] = back
                n = min(n, max(FLOOR_PARALLEL, n - back))
                if _last_backoff[0] != n:
                    _log.info("[multi] backing off to %d (429s)", n)
            _last_backoff[0] = n
    except Exception:                                            # noqa: BLE001
        pass
    info["now"] = max(1, n)
    if info["now"] >= cap:
        info["limited_by"] = None
    elif info["backoff"] and (not info["by_fleet"] or info["now"] < info["by_fleet"]) \
            and info["now"] < info["by_machine"]:
        info["limited_by"] = "429s"
    elif info["now"] == info["by_machine"]:
        info["limited_by"] = "machine"
    else:
        info["limited_by"] = "fleet"
    return info


def _concurrency():
    """The helpers allowed at once: the smallest of the setting, the machine
    (lowres.workers: 1-2 on a weak one, 1 while RAM is short; free RAM and
    cores), the fleet's distinct good models and the 429 back-off. Re-read
    before every spawn."""
    try:
        return concurrency_info()["now"]
    except Exception:                                            # noqa: BLE001
        return MAX_CONCURRENT


def spawn_allowed(n_running):
    """May one more helper start now? n_running < _concurrency(), and the live
    RAM/CPU governor (lowres.GOV) has room -- except that a run with NOTHING
    running always gets one (queued phases must make progress). The governor
    only ever stops NEW starts; it never touches a running helper."""
    if n_running >= _concurrency():
        return False
    if n_running <= 0:
        return True
    try:
        import lowres
        room = lowres.GOV.headroom()
        return room is None or room > 0
    except Exception:                                            # noqa: BLE001
        return True


def waiting_for_ram():
    """True while the governor is the only thing holding a queued phase."""
    try:
        import lowres
        return lowres.GOV.headroom() == 0
    except Exception:                                            # noqa: BLE001
        return False


# Hard ceiling on workers in a run. The planner is asked for fewer; this is the
# guard against a plan that ignores the ask.
MAX_AGENTS = 10
# One worker's wall clock -- the HARD cap. MEASURED 2026-09-12 on a real
# four-phase build: two phases legitimately ran 35 and 44 minutes and a third
# was still working when the old 900s cap "timed it out". The cap did not stop
# anything: the worker thread kept draining its CLI, so the phase was marked
# failed, the review started on files that were still being written, and
# forty minutes later the worker finished and flipped its own phase back to
# done -- "done" with "timed out after 900s" as its error. Two hours is a
# ceiling for a phase that is visibly working; a phase that is NOT is caught
# far sooner by the idle limit below.
AGENT_TIMEOUT = 2 * 3600.0
# No output at all for this long and the worker is stuck, whatever the clock
# says. Larger than agentic_chat's own stall watchdog (420s), which restarts
# a wedged CLI turn and produces events while doing it -- so a worker that
# goes this quiet has defeated that too.
AGENT_IDLE_TIMEOUT = 900.0
# Per-agent event ring. Enough to read what a worker did without holding a
# whole build's output in memory for every worker at once.
EVENT_BUFFER = 400
# What a parent's summary is clipped to when it is fed to a child. Same
# reasoning as swarm.DEP_CONTEXT_CHARS: dependencies are context, not the task.
DEP_CONTEXT_CHARS = 4000

# The conversation a run is a turn of, as the run carries it (see _Run.context):
# at most CONTEXT_CHARS in all; the planner sees up to PLAN_CONTEXT_CHARS of it,
# each worker WORKER_CONTEXT_CHARS, the (paid) manager MANAGER_CONTEXT_CHARS.
CONTEXT_CHARS = 6000
PLAN_CONTEXT_CHARS = 4000
WORKER_CONTEXT_CHARS = 2000
MANAGER_CONTEXT_CHARS = 1500
# The goal brief (taskboard) a run carries: the owner's <= 600-char cap, with
# a little headroom for its heading when it was built right at the limit.
GOAL_BRIEF_CHARS = 700
_CONTEXT_HEADING = ("--- conversation context (earlier in this conversation; "
                    "use it to understand the goal, do not redo finished work) ---")


def _clip_text(text, limit):
    """Head + tail, so a clipped context keeps how it starts AND ends."""
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    return text[:head] + "\n[... trimmed ...]\n" + text[-(limit - head - 20):]


def _with_context(goal, context, limit):
    """`goal`, plus the bounded context under its heading when there is any."""
    context = _clip_text(context, limit)
    return goal if not context else "%s\n\n%s\n%s" % (goal, _CONTEXT_HEADING, context)


# The last wave is a REVIEW: one agent that reads what every other agent did and
# finishes the job rather than reporting on it.
#
# REQUESTED: "pour le swarm les models doivent travailler ensemble pour trouver
# la plus meilleure solution pertinente". Phases alone are division of labour,
# not collaboration -- five agents each doing their own piece and nobody ever
# looking at the whole. swarm.py already ends its CHAT pipeline with review and
# synth for exactly this reason; this is the same idea where the workers are
# real sessions that can still fix what they find.
REVIEW_TITLE = "Review and finish"
_REVIEW_TASK = """Every other phase of this job is done. You are the last agent.

Read what the others produced (below), then CHECK THE ACTUAL PROJECT -- open the
files, run what can be run. Your job is not to summarise them; it is to find
what is missing, broken or inconsistent BETWEEN their pieces and to fix it
yourself.

Look for: files that reference something nobody created, two phases that solved
the same thing differently, anything a phase said it would do and did not, and
anything that plainly does not work.

Fix what you find. Change nothing that is already correct. Finish with a short
list of what you changed and anything still genuinely open."""


PENDING, RUNNING, DONE, FAILED, STOPPED = "pending", "running", "done", "failed", "stopped"

# What a phase that a spent budget stopped from starting is marked with: a
# STOPPED state (not FAILED -- nothing failed) and this reason, so the report
# and the page can say "left for next time", and a later "continue" re-runs it.
BUDGET_STOPPED_ERROR = "stopped (budget)"


# --------------------------------------------------------------------------- #
# Spend budgets (tokens / seconds / calls). Pure helpers: the run holds a cap
# and an injected `spent(run_id)` accountant; these compare the two.
# --------------------------------------------------------------------------- #
# The hub's spend accountant, registered once (app.set_spent_counter ->
# _run_spent): spent(run_id) -> {"tokens", "calls"}. A run with its own
# injected spent_fn (a test, or a caller that passes spent=) overrides it; a
# run with neither simply never trips a tokens/calls budget.
_SPENT_HOOK = [None]


def set_spent_counter(fn):
    """app registers how a run's token/call spend is measured, so no call site
    has to thread it through. Never required; None = only seconds budgets."""
    _SPENT_HOOK[0] = fn if callable(fn) else None


def normalize_budget(budget):
    """A {tokens, seconds, calls} dict -> a clean one (ints >= 0, or None per
    axis for "no cap on this axis"), or None when nothing is capped."""
    if not isinstance(budget, dict):
        return None
    out, any_set = {}, False
    for k in ("tokens", "seconds", "calls"):
        v = budget.get(k)
        if v is None:
            out[k] = None
            continue
        try:
            out[k] = max(0, int(v))
            any_set = True
        except (TypeError, ValueError):
            out[k] = None
    return out if any_set else None


def _fmt_tokens(n):
    n = int(n or 0)
    if n >= 1_000_000:
        return ("%.1fM" % (n / 1_000_000)).replace(".0M", "M")
    if n >= 1_000:
        return ("%.0fK" % (n / 1_000)) if n % 1000 else ("%dK" % (n // 1000))
    return str(n)


def _fmt_seconds(n):
    n = int(n or 0)
    if n < 60:
        return "%ds" % n
    if n < 3600:
        return "%d min" % round(n / 60)
    h, m = divmod(round(n / 60), 60)
    return "%dh %02dm" % (h, m)


def budget_check(budget, spent):
    """(reached, reason, message). `budget` from normalize_budget, `spent` a
    {tokens, calls, seconds} dict. Tokens, then calls, then seconds."""
    if not budget:
        return (False, None, None)
    st = int((spent or {}).get("tokens") or 0)
    sc = int((spent or {}).get("calls") or 0)
    ss = float((spent or {}).get("seconds") or 0)
    t, c, s = budget.get("tokens"), budget.get("calls"), budget.get("seconds")
    if t is not None and st >= t:
        return (True, "tokens", "Budget reached: %s of %s tokens"
                % (_fmt_tokens(st), _fmt_tokens(t)))
    if c is not None and sc >= c:
        return (True, "calls", "Budget reached: %d of %d calls" % (sc, c))
    if s is not None and ss >= s:
        return (True, "seconds", "Budget reached: %s of %s"
                % (_fmt_seconds(ss), _fmt_seconds(s)))
    return (False, None, None)


def budget_view(run):
    """What the Build page header shows for a run's budget, or None. One part
    per capped axis ({label, spent, cap, reached}) plus a one-line summary and
    the budget-reached note."""
    budget = getattr(run, "budget", None)
    if not budget:
        return None
    try:
        spent = run.budget_spent()
    except Exception:                                            # noqa: BLE001
        spent = {"tokens": 0, "calls": 0, "seconds": 0}
    parts = []
    for key, label, fmt in (("tokens", "tokens", _fmt_tokens),
                            ("seconds", "time", _fmt_seconds),
                            ("calls", "calls", lambda n: str(int(n or 0)))):
        cap = budget.get(key)
        if cap is None:
            continue
        got = spent.get(key) or 0
        parts.append({"key": key, "label": label, "spent": fmt(got),
                      "cap": fmt(cap), "reached": got >= cap})
    if not parts:
        return None
    line = "Budget: " + " · ".join(
        "%s / %s %s" % (p["spent"], p["cap"], p["label"]) for p in parts)
    return {"parts": parts, "line": line,
            "reached": bool(getattr(run, "budget_reached", None)),
            "note": getattr(run, "budget_note", None)}


_RUNS = {}
_LOCK = threading.RLock()
# Runs are kept so their transcripts stay readable after they finish; without a
# cap a long-lived hub accumulates every swarm it ever ran.
MAX_RUNS = 20

# WHERE A RUN LIVES BETWEEN RESTARTS.
#
# `_RUNS` is RAM, and the hub restarts itself every five hours to `git pull`.
# A swarm that took twenty minutes and finished at hour four vanished with it:
# no result to read back, no record it ever happened, and any run still going
# became a thread nobody could find. One JSON file per run fixes all three.
#
# REQUESTED: "memory management, and also context window, and also persistence,
# and also the orchestrator ... nothing can escape".
_STORE_ENV = "FREE_LLM_HUB_SWARM_DIR"
# How much of each worker's log is kept on disk. The full ring is EVENT_BUFFER
# (400) entries of raw CLI output per agent; persisting all of it would write a
# build transcript to disk on every phase. The tail is what a human reads when
# a phase failed -- the summaries, which are what the ORCHESTRATOR reads, are
# kept whole.
PERSIST_EVENTS = 60


def _store_root():
    env = os.environ.get(_STORE_ENV)
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(os.path.expanduser("~"), ".free-llm-hub", "swarm-runs")


def _run_path(run_id, root=None):
    rid = str(run_id or "")
    # Run ids are generated here (`swarm-` + hex) and never come from a user,
    # but this builds a filename, so it is checked like one anyway.
    if not re.match(r"^[A-Za-z0-9_-]{1,64}$", rid):
        return None
    return os.path.join(root or _store_root(), rid + ".json")


def _replace_with_retry(tmp, path, attempts=8, delay=0.05):
    """os.replace, retried while Windows reports the target locked.

    On Windows os.replace raises PermissionError while ANY handle has the
    target open -- a status read or load() walking the store at that instant.
    _persist swallowed that, so the run's record was silently not written.
    FOUND 2026-09-27: test_a_corrupt_run_file_is_skipped_not_fatal saw load()
    return 0 under full-suite load, passing 100/100 alone. memory.py's _save
    got the same retry for the same reason; this is its twin."""
    for i in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay * (i + 1))


def _read_json_with_retry(path, attempts=8, delay=0.05):
    """json-load a run file, retried while Windows reports it locked: a read
    that lands while _persist is replacing the file raises PermissionError,
    and load() used to count that GOOD file as corrupt and skip it."""
    for i in range(attempts):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay * (i + 1))


def _persist(run):
    """Write one run to disk. Best-effort: a swarm never fails because the
    record of it could not be written."""
    path = _run_path(getattr(run, "id", None),
                     root=getattr(run, "store_root", None))
    if not path:
        return False
    try:
        row = run.row(with_events=True)
        for a in row.get("agents") or ():
            log = a.get("log") or []
            a["log"] = log[-PERSIST_EVENTS:]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(row, fh, ensure_ascii=False)
            _replace_with_retry(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True
    except (OSError, ValueError, TypeError):
        return False


def _forget_file(run_id):
    path = _run_path(run_id)
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def load():
    """Read persisted runs back into memory. Called once at hub startup.

    A run that was still RUNNING when the process died has no thread walking it
    any more, and nothing will ever move it on -- so it is marked failed here
    rather than left displaying as live forever. Not resumable: its workers were
    CLI sessions belonging to a process that no longer exists."""
    root = _store_root()
    try:
        names = sorted(n for n in os.listdir(root) if n.endswith(".json"))
    except OSError:
        return 0
    loaded = 0
    for name in names:
        try:
            row = _read_json_with_retry(os.path.join(root, name))
            run = _Run.from_row(row)
        except (OSError, ValueError, TypeError, KeyError):
            continue
        if not run:
            continue
        with _LOCK:
            if run.id in _RUNS:
                continue                       # a live run outranks its file
            _RUNS[run.id] = run
        loaded += 1
        if run.interrupted:
            _persist(run)
    _evict()
    return loaded


class SwarmWindowsError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #

_PLAN_SYSTEM = """You break a software task into INDEPENDENT phases for parallel agents.

Each phase is given to a SEPARATE agent with its own fresh context. An agent
sees only its own task, the shared design, and the summaries of the phases it
declares in "needs".

Rules:
- DESIGN FIRST for a build or a feature (2+ phases): before the phases give
  "design" -- the components, the interfaces between them (function
  signatures, endpoints, file formats: the contracts agents working at the
  same time build to) and the data flow. A small fix or a one-phase plan
  gives "design": {}.
- "files" lists the paths a phase creates or edits (relative to the project).
  Two phases that run at the same time must not share a file.
- Every phase gets a concrete, checkable "done_when" (a command that passes,
  a file that exists, a behaviour you can see). Cover EVERY part the user
  listed. The plan is dry-run before any agent starts: shared files, missing
  parts and missing inputs are caught there.
- SPEED: phases that need nothing run AT THE SAME TIME (up to {helpers} agents). A
  chain where each phase needs the previous one is the SLOWEST plan; use a
  "needs" only when a phase cannot start without another phase's RESULT.
- Finding a problem and fixing it is ONE phase: the agent that investigates
  also fixes. Never plan "diagnose X" followed by "fix X".
- Split the work by file or area (backend, frontend, separate modules, docs)
  so agents work side by side. Two phases that touch the same file are not
  independent; say so with "needs".
- "needs" lists earlier phase numbers only (1-based). No self-references.
- Between 2 and %d phases. Fewer, larger phases beat many tiny ones.
- Each "task" must be self-contained: an agent cannot ask you a question.
- "mode" picks the KIND of model that phase gets. Choose from: {modes}.
  Pick the one that fits the work (writing code, long files, reading images,
  quick mechanical edits). Leave it out when nothing fits; do not guess.

Reply with JSON only:
{"goal": "...",
 "design": {"components": ["name: what it does"],
            "interfaces": ["the contract, e.g. GET /api/items -> [{id, name}]"],
            "data_flow": "..."},
 "phases": [{"title": "...", "task": "...", "done_when": "...", "files": ["path"],
             "needs": [], "mode": "coding"}]}
""" % MAX_AGENTS

# THE SAME ASK, WHEN A SUBSCRIPTION MANAGER PLANS.
#
# REQUESTED: the subscription model "plans, instructs, verifies and fixes; free
# models do the work". A free worker cannot ask what was meant, so a plan
# written by the stronger model is worth most when it spells out what a weak
# one would otherwise guess: what it starts from, what it must not do, what its
# final message must contain, and how it will be judged. Only the managed
# planner is asked for these -- the free planner keeps the prompt it has always
# had, so a hub with no manager plans exactly as before.
# WEB / UI GOALS ONLY (owner, 2026-10-04: slop prevented from the BEGINNING):
# the look is decided in the plan's design, so the first plan already passes
# slopcheck.check_design instead of spending its one re-ask. Not in the base
# prompts -- a non-web goal's prompt is byte-identical to before.
_WEB_PLAN_ASK = """
WEB/UI GOAL: "design" must also carry "visual", chosen for THIS product (no defaults):
"visual": {"palette": ["#hex role", ...4-6, text >=4.5:1], "type": "display face + body face",
 "layout": "the composition and why this product needs it, not hero+3 cards+CTA",
 "motion": "what moves + reduced-motion fallback", "copy": "where the real words come from"}.
"""


# REAL PARALLELISM (owner, 2026-10-04: "4 different models at once, or 5-6 if
# needed, working together"). Only for a goal big enough to fill the helpers
# and only when 3+ can run at once: a small task stays small (no ceremony).
_MICRO_ASK = """
PARALLEL CAPACITY: this machine runs up to {helpers} helpers AT ONCE, each on a
different model. This goal is big enough to use them: split every BIG phase
into MICRO-TASKS by file or component so {helpers} helpers can work at the same
time. Give each micro-task its own "files" list (the files it owns) with NO
file shared by two micro-tasks that run together, and keep ONE final
integrate/review phase that needs the rest. A big phase that cannot be split
further gets "parallel": true (a second helper then assists it).
"""

_SIZEABLE_CHARS = 280
_ENUM_RE = re.compile(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+\S")


def sizeable_goal(goal):
    """True for a goal big enough to fill several helpers: long, or listing
    three or more parts. A one-liner fix is not."""
    g = str(goal or "")
    return len(g) >= _SIZEABLE_CHARS or len(_ENUM_RE.findall(g)) >= 3


def plan_system(goal, managed=False, helpers=None):
    """The planner's system prompt for `goal` ({modes} still unfilled): the
    base prompt with the number of helpers that can run at once, plus the
    micro-task ask for a sizeable goal and the visual-decision ask for a
    web/UI goal."""
    try:
        n = int(helpers) if helpers else _concurrency()
    except Exception:                                            # noqa: BLE001
        n = MAX_CONCURRENT
    n = max(1, n)
    base = (_PLAN_SYSTEM_MANAGED if managed else _PLAN_SYSTEM).replace("{helpers}", str(n))
    if n >= 3 and sizeable_goal(goal):
        base += _MICRO_ASK.replace("{helpers}", str(n))
    try:
        import craft
        if craft.skill_enabled("web_design") and craft.is_web_ui(goal or ""):
            return base + _WEB_PLAN_ASK
    except Exception:                                            # noqa: BLE001
        pass
    return base


_PLAN_SYSTEM_MANAGED = _PLAN_SYSTEM.replace(
    "Reply with JSON only:",
    """- The agents are weaker models than you: be explicit. For each phase also give
  "inputs" (what it starts from: files, data, earlier phases), "constraints"
  (what it must not do or change), "output_format" (what its final message
  must contain) and "acceptance" (concrete, checkable criteria). Name every
  file that must exist afterwards in backticks, e.g. `src/app.py`.

Reply with JSON only:""").replace(
    '"needs": [], "mode": "coding"}]}',
    '"needs": [], "mode": "coding", "inputs": "...",\n'
    '             "constraints": "...", "output_format": "...",\n'
    '             "acceptance": "..."}]}')

# The optional per-phase brief fields a managed plan carries. Free-text, each
# clipped: they are instructions to a worker, not a place for a second task.
BRIEF_FIELDS = ("inputs", "constraints", "output_format", "acceptance")
BRIEF_CHARS = 1200


def _brief_text(value):
    """A brief field as one string: models answer with a string, a list, or
    nothing, and the worker prompt wants text."""
    if isinstance(value, (list, tuple)):
        value = "; ".join(str(v).strip() for v in value if str(v or "").strip())
    return str(value or "").strip()[:BRIEF_CHARS]


def _is_review(run, agent):
    """The appended "Review and finish" phase (see with_review): the last one,
    under that exact title."""
    return (agent.title == REVIEW_TITLE and bool(run.agents)
            and run.agents[-1] is agent)


def _extract_json(text):
    """The first JSON object in a model's reply. Models fence it, prefix it with
    prose, or both."""
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = [fence.group(1)] if fence else []
    start = text.find("{")
    if start != -1:
        candidates.append(text[start:text.rfind("}") + 1])
    for c in candidates:
        try:
            got = json.loads(c)
            if isinstance(got, dict):
                return got
        except ValueError:
            continue
    return None


def clean_phases(plan, max_phases=MAX_AGENTS, modes=(), notes=None):
    """Validated phases, or [] when the plan is unusable.

    `needs` is sanitised hard for the same reason swarm._clean_phases does it: a
    self-reference or a forward reference deadlocks the wave scheduler, and a
    plan that makes every phase depend on every earlier one is a sequential
    pipeline wearing a swarm's clothes.

    `notes` (a list, optional) receives what was changed, as {kind, text,
    phase} -- the plan's dry run (plan_check) reports it instead of the
    sanitising staying silent."""
    if not isinstance(plan, dict):
        return []
    note = notes.append if isinstance(notes, list) else (lambda _n: None)
    out = []
    raw_phases = [p for p in (plan.get("phases") or []) if isinstance(p, dict)] \
        if isinstance(plan.get("phases"), list) else []
    usable = [p for p in raw_phases if str(p.get("task") or "").strip()]
    if len(usable) > max_phases:
        note({"kind": "too_many_phases", "phase": None,
              "text": "%d phases planned; past the limit of %d the rest were dropped"
                      % (len(usable), max_phases)})
    for p in (plan.get("phases") or [])[:max_phases]:
        if not isinstance(p, dict):
            continue
        task = str(p.get("task") or "").strip()
        if not task:
            note({"kind": "no_task", "phase": None,
                  "text": "dropped a phase with no task (%s)"
                          % (str(p.get("title") or "untitled")[:40])})
            continue
        idx = len(out) + 1
        needs = []
        raw = p.get("needs")
        if isinstance(raw, list):
            for n in raw:
                try:
                    n = int(n)
                except (TypeError, ValueError):
                    continue
                if 1 <= n < idx and n not in needs:
                    needs.append(n)
                elif n not in needs:
                    note({"kind": "need_dropped", "phase": idx,
                          "text": "phase %d: dropped need %d (%s)" % (
                              idx, n, "itself" if n == idx else
                              "a later phase, could deadlock" if n > idx else "no such phase")})
        # WHICH KIND OF MODEL THIS PHASE GETS. Validated against the modes the
        # caller actually serves rather than trusted: a planner inventing
        # "mode": "genius" would otherwise reach set_session_mode and either
        # fail or, worse, silently restrict a phase to nothing.
        mode = str(p.get("mode") or "").strip().lower() or None
        if mode and modes and mode not in modes:
            mode = None
        row = {
            "title": str(p.get("title") or ("Phase %d" % idx)).strip()[:80],
            "task": task[:4000],
            "done_when": str(p.get("done_when") or "").strip()[:400],
            "needs": needs,
            "mode": mode,
        }
        # Only when present: a plain plan keeps exactly the shape it had.
        for key in BRIEF_FIELDS:
            text = _brief_text(p.get(key))
            if text:
                row[key] = text
        # The paths this phase OWNS (creates or edits) -- what the dry run
        # checks for two helpers working on one file at the same time.
        files = plan_check.norm_files(p.get("files"))
        if files:
            row["files"] = files
        if p.get("parallel") is True or str(p.get("parallel")).lower() == "true":
            row["parallel"] = True
        out.append(row)
    return merge_handoffs(out, notes=notes) if len(out) >= 1 else []


# A phase that only LOOKS (diagnose, locate, investigate...) and whose one
# follower is the phase that acts on what it found. Owner, 2026-09-30:
# "multi session mode should do jobs in parallel agents to speed up".
# MEASURED the same day, run swarm-4f2aab7204a4: "Locate code and diagnose
# contour defects" -> "Implement contour fixes" -> verify -> review, strictly
# one after another -- the first hand-off alone cost a whole CLI start, a
# re-read of the project and a summary the second agent had to trust.
_LOOK_ONLY_RE = re.compile(
    r"^\s*(?:locate|diagnos|investigat|analy[sz]|inspect|explore|research|"
    r"identify|find|understand|audit|reproduce|study|examine)", re.I)
_ACTS_RE = re.compile(
    r"\b(?:fix|implement|build|write|create|add|update|change|refactor|edit|"
    r"apply|patch|rewrite|remove|delete|migrate)\b", re.I)


def _looks_only(phase):
    title = str(phase.get("title") or "")
    return bool(_LOOK_ONLY_RE.search(title)) and not _ACTS_RE.search(title)


def merge_handoffs(phases, notes=None):
    """`phases` with every look-only phase folded into its ONE follower (the
    phase that needs it, when nothing else does): the follower's agent
    investigates first, then acts. Renumbers "needs". Never raises; a plan
    it cannot read comes back as it was. `notes` (optional list) gets one
    {kind: "merged", ...} per fold."""
    try:
        phases = [dict(p) for p in phases]
        merged = True
        while merged:
            merged = False
            for i, p in enumerate(phases, start=1):
                if not _looks_only(p):
                    continue
                followers = [j for j, q in enumerate(phases, start=1) if i in q["needs"]]
                if len(followers) != 1:
                    continue
                j = followers[0]
                f = phases[j - 1]
                f["task"] = ("First -- %s: %s\n\nThen -- %s" % (
                    p["title"], p["task"], f["task"]))[:4000]
                f["needs"] = sorted(set(p["needs"]) | (set(f["needs"]) - {i}))
                f["mode"] = f.get("mode") or p.get("mode")
                if p.get("parallel") or f.get("parallel"):
                    f["parallel"] = True
                if p.get("files") or f.get("files"):
                    f["files"] = list(dict.fromkeys(list(f.get("files") or [])
                                                    + list(p.get("files") or [])))
                if isinstance(notes, list):
                    notes.append({"kind": "merged", "phase": j - 1,
                                  "text": "merged look-only \"%s\" into \"%s\""
                                          % (p["title"][:40], f["title"][:40])})
                del phases[i - 1]
                for q in phases:                     # renumber after removing i
                    q["needs"] = [n - 1 if n > i else n for n in q["needs"]]
                merged = True
                break
        return phases
    except Exception:                                            # noqa: BLE001
        return phases


def with_review(phases):
    """Append the review phase, depending on everything before it.

    Not added when the planner produced a single phase: there is nothing to
    reconcile between one piece of work, and a second agent re-reading it is a
    whole extra model call to say "looks fine"."""
    phases = list(phases or [])
    if len(phases) < 2:
        return phases
    if phases and phases[-1].get("title") == REVIEW_TITLE:
        return phases                     # already has one
    phases.append({
        "title": REVIEW_TITLE,
        "task": _REVIEW_TASK,
        "done_when": "Everything the other phases produced works together.",
        "needs": list(range(1, len(phases) + 1)),
        "mode": None,
    })
    return phases


def waves(phases):
    """Phases grouped into dependency waves; everything inside one wave is
    independent. The plan as it is SHOWN and persisted (run.waves): the run
    itself does not wait for a whole wave -- _run_phases starts each phase as
    soon as what it needs has finished.

    An unsatisfiable graph degrades to "run the rest together" rather than
    hanging -- the same choice swarm._waves makes, for the same reason: a swarm
    that deadlocks is worse than one whose last phases get less context."""
    remaining = list(range(1, len(phases) + 1))
    done, out = set(), []
    while remaining:
        wave = [i for i in remaining if set(phases[i - 1]["needs"]) <= done]
        if not wave:
            wave = list(remaining)
        out.append(wave)
        done.update(wave)
        remaining = [i for i in remaining if i not in done]
    return out


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

class _Agent:
    __slots__ = ("index", "title", "task", "done_when", "needs", "mode",
                 "session_id", "state", "summary", "error", "started_at",
                 "ended_at", "events", "last_event_at", "abandoned",
                 "inputs", "constraints", "output_format", "acceptance",
                 "verified", "problems", "revisions", "past_sessions",
                 "event_total", "evidence", "reviewed", "claimed_unobserved",
                 "receipt", "files", "free_check", "widen", "slop",
                 "parallel", "pair_session", "pair_state", "pair_summary",
                 "pair_error", "pair_started_at", "pair_ended_at")

    def __init__(self, index, phase):
        self.index = index
        self.title = phase["title"]
        self.task = phase["task"]
        self.done_when = phase.get("done_when") or ""
        self.needs = list(phase.get("needs") or ())
        self.mode = phase.get("mode") or None
        # The paths the plan says this phase owns ([] when it named none).
        self.files = list(phase.get("files") or ())
        # The planner marked this phase big and not splittable further: a
        # second helper (another family) may assist it (PAIR mode, below).
        self.parallel = bool(phase.get("parallel"))
        self.pair_session = None
        self.pair_state = None          # None | running | done | failed | stopped
        self.pair_summary = ""
        self.pair_error = None
        self.pair_started_at = None
        self.pair_ended_at = None
        # The managed brief (see _PLAN_SYSTEM_MANAGED); "" for a plain plan.
        self.inputs = phase.get("inputs") or ""
        self.constraints = phase.get("constraints") or ""
        self.output_format = phase.get("output_format") or ""
        self.acceptance = phase.get("acceptance") or ""
        # VERIFIED MEANS OBSERVED (2026-10-04): True only when the hub saw a
        # test/build command the worker ran PASS (exit 0 + the tool's own
        # summary, evidence.classify) with no failure left behind it; False =
        # still failing after its one revision; None = nothing observed. A
        # manager that agreed with the SUMMARY sets `reviewed`, never
        # `verified`. The problems are what the revision and then the review
        # are told to fix.
        self.verified = None
        self.reviewed = False
        # The final message says tests/build pass, but no passing run was
        # observed: shown as "claimed, not checked" -- not a failure.
        self.claimed_unobserved = False
        # What the hub observed this phase's test/build commands do (evidence
        # rows, oldest first, capped at EVIDENCE_MAX) and the receipt file.
        self.evidence = []
        self.receipt = None
        # The ONE free verdict this phase may get without a manager
        # ({ok, severity, problems, reason}, or {asked, answered: False});
        # None = never asked. Persisted.
        self.free_check = None
        # The slop check of the web files this phase wrote ({line, high, medium,
        # low, warnings, problems}); None = no web file written. Persisted.
        self.slop = None
        # True only while a WIDER search attempt runs: sibling_sessions then
        # names this worker's own earlier sessions, so the hub picks another
        # model. Not persisted.
        self.widen = False
        self.problems = []
        self.revisions = 0
        self.session_id = None
        self.past_sessions = []
        self.state = PENDING
        self.summary = ""
        self.error = None
        self.started_at = None
        self.ended_at = None
        self.events = deque(maxlen=EVENT_BUFFER)
        # Every event ever drained, not capped like the ring above: lets a
        # follower tell which ring entries are NEW once the ring is full.
        self.event_total = 0
        self.last_event_at = None       # the idle limit is measured from this
        # Set by the wave when it gives up on this worker: the worker thread
        # may still be draining a CLI that has not noticed, and its late
        # result must not flip a phase the run has already moved past.
        self.abandoned = False

    def row(self, with_events=False):
        out = {
            "index": self.index, "title": self.title, "task": self.task,
            "done_when": self.done_when, "needs": list(self.needs),
            "mode": self.mode, "files": list(self.files),
            "parallel": bool(self.parallel),
            "pair": ({"session_id": self.pair_session, "state": self.pair_state,
                      "summary": self.pair_summary, "error": self.pair_error,
                      "started_at": self.pair_started_at,
                      "ended_at": self.pair_ended_at}
                     if self.pair_state else None),
            "session_id": self.session_id, "state": self.state,
            "summary": self.summary, "error": self.error,
            "started_at": self.started_at, "ended_at": self.ended_at,
            "events": len(self.events),
            "events_total": self.event_total,
            "inputs": self.inputs, "constraints": self.constraints,
            "output_format": self.output_format, "acceptance": self.acceptance,
            "verified": self.verified, "problems": list(self.problems),
            "revisions": self.revisions,
            "reviewed": bool(self.reviewed),
            "claimed_not_observed": bool(self.claimed_unobserved),
            "evidence": [dict(e) for e in self.evidence],
            "receipt": self.receipt,
            "check": check_of(self),
            "free_check": dict(self.free_check) if isinstance(self.free_check, dict) else None,
            "slop": dict(self.slop) if isinstance(self.slop, dict) else None,
        }
        if with_events:
            out["log"] = list(self.events)
        return out


# What a worker that was mid-phase when the hub died is marked with. ONE
# string, because resume_interrupted() reads it back to know which phases to
# run again.
INTERRUPTED_ERROR = "interrupted by a hub restart"


class _Run:
    __slots__ = ("id", "goal", "project_dir", "cli_id", "agents", "state",
                 "error", "created_at", "ended_at", "stop_flag", "lock", "waves",
                 "restored", "interrupted", "store_root", "owner",
                 "manager", "managed", "modes", "manager_tokens", "manager_calls",
                 "context", "resumes", "default_mode", "design", "plan_check",
                 "free_verdict", "search", "search_saved", "goal_brief",
                 "budget", "spent_fn", "budget_elapsed", "budget_walk_start",
                 "budget_note", "budget_reached")

    def __init__(self, goal, project_dir, cli_id, phases, owner=None,
                 manager=None, modes=(), context="", default_mode=None,
                 design=None, check_report=None, free_verdict=None, search=None,
                 goal_brief="", budget=None, spent=None):
        self.id = "swarm-" + uuid.uuid4().hex[:12]
        self.goal = goal
        # THE DESIGN the plan carried (plan_check.normalize_design; {} for a
        # small fix): every worker's prompt gets it, so helpers working side
        # by side build to the same interfaces. And the plan's DRY RUN report
        # (plan_check.summarize; None when no check ran). Both persisted.
        self.design = design if isinstance(design, dict) else {}
        self.plan_check = check_report if isinstance(check_report, dict) else None
        # WHAT THE CONVERSATION ALREADY ESTABLISHED (bounded, see
        # CONTEXT_CHARS): the owner session's memory block, its recap, the
        # previous run's result. The goal alone is one message; "make it
        # better" as a goal meant planning from three words. Persisted, so a
        # resumed run's workers are briefed the same way.
        self.context = _clip_text(context, CONTEXT_CHARS)
        # THE GOAL BEHIND THE RUN (taskboard, via app): the <= 600-char brief
        # naming the project's active goal and its open tasks. Every worker's
        # prompt carries it, the planner saw it, and a restored run keeps it
        # (persisted). "" = no goal, i.e. the prompt is exactly as before.
        self.goal_brief = _clip_text(goal_brief, GOAL_BRIEF_CHARS)
        # How many times this run was picked back up by a "continue".
        self.resumes = 0
        self.project_dir = project_dir
        self.cli_id = cli_id
        self.agents = [_Agent(i + 1, p) for i, p in enumerate(phases)]
        self.waves = waves(phases)
        self.state = PENDING
        self.error = None
        self.created_at = time.time()
        self.ended_at = None
        self.stop_flag = threading.Event()
        self.lock = threading.RLock()
        # True for a run read back from disk: it has no thread behind it, so it
        # can be read but never advances.
        self.restored = False
        self.interrupted = False
        # WHERE THIS RUN'S FILE LIVES, decided once. A run outlives the call
        # that started it by minutes, and re-reading the directory on every
        # write means a run that started under one setting finishes under
        # another -- so its later phases land somewhere its earlier ones did
        # not, and the file is left describing half a run.
        self.store_root = _store_root()
        # The CONVERSATION this run is a turn of (the "multi" tier), or None
        # for a run started from the Swarm tab, MCP or a shell. Persisted, so
        # a run resumed after a restart still answers into the right one.
        self.owner = owner or None
        # THE SUBSCRIPTION MANAGER, when the hub has one: `manager(system,
        # user, purpose, max_tokens) -> (text, tokens)`, injected like every
        # other model call here. None = no verification at all, i.e. the run
        # behaves exactly as it did before managers existed. Not persisted: a
        # callable does not survive a restart -- resume_interrupted(manager=)
        # re-attaches the hub's current one.
        self.manager = manager
        # Whether the person started this run WITH a manager -- persisted, so
        # a resume re-attaches one only to runs that had one, never to a run
        # started while the manager was off (that would spend subscription
        # tokens nobody opted into).
        self.managed = manager is not None
        self.modes = tuple(modes or ())
        # The category a phase runs in when the plan names none: the one the
        # user selected for the conversation (a "Review and finish" phase used
        # to run under "all"). Persisted, like owner.
        self.default_mode = default_mode or None
        # What the manager cost THIS run, planning included -- the hub's daily
        # budget is global, and "what did this job cost me" is per job.
        self.manager_tokens = 0
        self.manager_calls = 0
        # THE FREE VERIFIER (no manager): `free_verdict(phase_brief) ->
        # {"ok", "problems", "severity"} | None`, injected like the manager
        # and, like it, not persisted. None = no free verdict, as before.
        self.free_verdict = free_verdict if callable(free_verdict) else None
        # WIDER OR DEEPER (swarm.Search, or None = off): the run's Thompson
        # posteriors and every choice's outcome. `search_saved` is a restored
        # run's record ({posteriors, log}) until a resume re-attaches a policy.
        self.search = search
        self.search_saved = None
        # A SPEND BUDGET (tokens / seconds / calls), or None = unlimited, as
        # every run was before budgets existed. A heartbeat run always carries
        # one; a conversation's run carries the owner's optional cap. Reaching
        # it stops STARTING new phases -- running ones finish their turn (no
        # kill, no lost work) and the rest are marked "stopped (budget)".
        # Persisted; `spent` (the hub's own usage accounting, injected like the
        # manager) is re-attached on a resume, not persisted.
        self.budget = normalize_budget(budget)
        self.spent_fn = spent if callable(spent) else None
        # Active seconds across every walk of this run (a resume continues from
        # here), plus the current walk's start while one is going.
        self.budget_elapsed = 0.0
        self.budget_walk_start = None
        self.budget_note = None
        self.budget_reached = None

    def charge(self, tokens):
        with self.lock:
            self.manager_tokens += max(0, int(tokens or 0))
            self.manager_calls += 1

    # -- budget ------------------------------------------------------------- #
    def budget_active_seconds(self):
        """Seconds this run has spent WALKING: prior walks' time plus the
        current walk's, so a resume respects the remaining seconds."""
        base = self.budget_elapsed or 0.0
        if self.budget_walk_start:
            base += max(0.0, time.time() - self.budget_walk_start)
        return base

    def budget_spent(self):
        """{tokens, calls, seconds} spent by this run -- tokens/calls from the
        injected hub accounting of its worker sessions, seconds from the run
        clock. Measured, never guessed; fails open to zero."""
        spent = {"tokens": 0, "calls": 0}
        fn = self.spent_fn or _SPENT_HOOK[0]
        if callable(fn):
            try:
                got = fn(self.id) or {}
                spent["tokens"] = max(0, int(got.get("tokens") or 0))
                spent["calls"] = max(0, int(got.get("calls") or 0))
            except Exception:                                    # noqa: BLE001
                pass
        spent["seconds"] = self.budget_active_seconds()
        return spent

    def budget_status(self):
        """(reached, reason, message) for THIS run, now. ("", None, None) when
        it has no budget."""
        if not self.budget:
            return (False, None, None)
        return budget_check(self.budget, self.budget_spent())

    def row(self, with_events=False):
        return {
            "run_id": self.id, "goal": self.goal, "state": self.state,
            "project_dir": self.project_dir, "cli": self.cli_id,
            "owner": self.owner,
            "default_mode": self.default_mode,
            "managed": bool(self.managed or self.manager is not None),
            "manager_tokens": self.manager_tokens,
            "manager_calls": self.manager_calls,
            "context": self.context, "resumes": self.resumes,
            "goal_brief": self.goal_brief or "",
            "design": dict(self.design or {}),
            "plan_check": dict(self.plan_check) if self.plan_check else None,
            "search": (self.search.snapshot() if self.search is not None
                       else self.search_saved),
            "budget": dict(self.budget) if self.budget else None,
            "budget_elapsed": self.budget_active_seconds(),
            "budget_note": self.budget_note,
            "budget_reached": self.budget_reached,
            "error": self.error, "created_at": self.created_at,
            "ended_at": self.ended_at,
            "waves": [list(w) for w in self.waves],
            "agents": [a.row(with_events) for a in self.agents],
            "done": sum(1 for a in self.agents if a.state == DONE),
            "failed": sum(1 for a in self.agents if a.state == FAILED),
            "total": len(self.agents),
            "restored": self.restored,
        }

    @classmethod
    def from_row(cls, row):
        """Rebuild a run from what row() wrote, so every reader -- status,
        result, format_result, the settings page -- works on a restored run
        without knowing it is one."""
        if not isinstance(row, dict) or not row.get("run_id"):
            return None
        phases = [{"title": a.get("title") or "?", "task": a.get("task") or "",
                   "done_when": a.get("done_when") or "",
                   "needs": list(a.get("needs") or ()), "mode": a.get("mode"),
                   "files": [str(f) for f in (a.get("files") or ())
                             if isinstance(f, str)][:plan_check.MAX_FILES],
                   **{k: a.get(k) or "" for k in BRIEF_FIELDS}}
                  for a in (row.get("agents") or ())
                  if isinstance(a, dict)]
        if not phases:
            return None
        run = cls(row.get("goal") or "", row.get("project_dir") or "",
                  row.get("cli") or "", phases,
                  context=row.get("context") if isinstance(row.get("context"), str) else "",
                  design=plan_check.normalize_design(row.get("design")),
                  check_report=row.get("plan_check"))
        try:
            run.resumes = max(0, int(row.get("resumes") or 0))
        except (TypeError, ValueError):
            run.resumes = 0
        run.id = str(row["run_id"])
        run.goal_brief = _clip_text(row.get("goal_brief") or "", GOAL_BRIEF_CHARS)
        run.owner = row.get("owner") or None
        run.default_mode = row.get("default_mode") or None
        run.state = row.get("state") or DONE
        run.error = row.get("error")
        run.created_at = float(row.get("created_at") or time.time())
        run.ended_at = row.get("ended_at")
        run.waves = [list(w) for w in (row.get("waves") or ())] or run.waves
        run.restored = True
        try:
            run.manager_tokens = max(0, int(row.get("manager_tokens") or 0))
            run.manager_calls = max(0, int(row.get("manager_calls") or 0))
        except (TypeError, ValueError):
            pass
        # Rows written before "managed" existed: a managed run has paid for at
        # least its planning call.
        run.managed = bool(row["managed"]) if "managed" in row \
            else run.manager_calls > 0
        run.budget = normalize_budget(row.get("budget"))
        try:
            run.budget_elapsed = max(0.0, float(row.get("budget_elapsed") or 0.0))
        except (TypeError, ValueError):
            run.budget_elapsed = 0.0
        run.budget_note = row.get("budget_note") if isinstance(
            row.get("budget_note"), str) else None
        run.budget_reached = row.get("budget_reached") if isinstance(
            row.get("budget_reached"), str) else None
        saved = row.get("search")
        if isinstance(saved, dict) and isinstance(saved.get("log"), list):
            run.search_saved = {"posteriors": saved.get("posteriors") or {},
                                "log": [r for r in saved["log"] if isinstance(r, dict)]}
        for agent, a in zip(run.agents, row.get("agents") or ()):
            agent.free_check = a.get("free_check") if isinstance(a.get("free_check"), dict) \
                else None
            agent.slop = a.get("slop") if isinstance(a.get("slop"), dict) else None
            agent.verified = a.get("verified")
            agent.reviewed = bool(a.get("reviewed"))
            agent.claimed_unobserved = bool(a.get("claimed_not_observed"))
            agent.evidence = [e for e in (a.get("evidence") or ())
                              if isinstance(e, dict)][-EVIDENCE_MAX:]
            agent.receipt = a.get("receipt") if isinstance(a.get("receipt"), str) else None
            if "reviewed" not in a and agent.verified is True:
                # Written before 2026-10-04, when "verified" meant the manager
                # agreed with the summary: that is "reviewed" now.
                agent.verified, agent.reviewed = None, True
            agent.problems = [str(p) for p in (a.get("problems") or ())][:10]
            agent.revisions = int(a.get("revisions") or 0) if str(
                a.get("revisions") or 0).isdigit() else 0
            agent.session_id = a.get("session_id")
            agent.parallel = bool(a.get("parallel"))
            pr = a.get("pair") if isinstance(a.get("pair"), dict) else None
            if pr:
                agent.pair_session = pr.get("session_id")
                agent.pair_state = pr.get("state")
                agent.pair_summary = pr.get("summary") or ""
                agent.pair_error = pr.get("error")
                agent.pair_started_at = pr.get("started_at")
                agent.pair_ended_at = pr.get("ended_at")
                if agent.pair_state == "running":
                    agent.pair_state = "stopped"      # the hub restarted under it
            agent.state = a.get("state") or PENDING
            agent.summary = a.get("summary") or ""
            agent.error = a.get("error")
            agent.started_at = a.get("started_at")
            agent.ended_at = a.get("ended_at")
            for e in (a.get("log") or ()):
                agent.events.append(e)
            agent.event_total = len(agent.events)
        # Nothing is walking this run any more. Leaving a worker RUNNING would
        # show a swarm as live for the rest of the hub's life.
        for agent in run.agents:
            if agent.state in (PENDING, RUNNING):
                agent.state = FAILED
                agent.error = agent.error or INTERRUPTED_ERROR
                agent.ended_at = agent.ended_at or time.time()
                run.interrupted = True
        if run.state in (PENDING, RUNNING) and not run.interrupted:
            # Every phase had ended; only the run's own final write was
            # missing -- the process died (or the file was read) between the
            # last phase landing and _walk's closing _persist. The phases say
            # what happened; file that, not "interrupted". MEASURED as a flaky
            # test before it was a bug: a run read back with three done
            # phases and the state "failed / interrupted by a hub restart".
            run.state = FAILED if all(a.state == FAILED for a in run.agents) else DONE
            run.error = run.error or ("every phase failed" if run.state == FAILED else None)
            run.ended_at = run.ended_at or time.time()
        elif run.state in (PENDING, RUNNING):
            run.state = FAILED
            run.error = run.error or "interrupted by a hub restart"
            run.ended_at = run.ended_at or time.time()
            run.interrupted = True
        if run.interrupted:
            run.stop_flag.set()
        return run


def _evict():
    """Drop the oldest finished runs past the cap, from memory AND from disk.

    Both, or the directory becomes the unbounded thing the cap exists to
    prevent -- a hub that has run a thousand swarms keeping a thousand files."""
    dropped = []
    with _LOCK:
        if len(_RUNS) <= MAX_RUNS:
            return []
        for rid, r in sorted(_RUNS.items(), key=lambda kv: kv[1].created_at):
            if len(_RUNS) <= MAX_RUNS:
                break
            if r.state in (DONE, FAILED, STOPPED):
                _RUNS.pop(rid, None)
                dropped.append(rid)
    for rid in dropped:
        _forget_file(rid)
    return dropped


def _remember(run):
    with _LOCK:
        _RUNS[run.id] = run
    _persist(run)
    _evict()


def get(run_id):
    with _LOCK:
        return _RUNS.get(str(run_id))


def status(run_id, with_events=False):
    run = get(run_id)
    return run.row(with_events) if run else None


def list_runs():
    with _LOCK:
        runs = sorted(_RUNS.values(), key=lambda r: -r.created_at)
    return [r.row() for r in runs]


def stop(run_id):
    run = get(run_id)
    if not run:
        return False
    run.stop_flag.set()
    with run.lock:
        for a in run.agents:
            if a.state in (PENDING, RUNNING):
                a.state = STOPPED
        if run.state in (PENDING, RUNNING):
            run.state = STOPPED
            run.ended_at = time.time()
    _persist(run)
    return True


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #

DESIGN_CHARS = plan_check.DESIGN_CHARS


def design_block(run, agent=None):
    """The design block a worker's prompt carries (bounded to DESIGN_CHARS),
    or "" when the run has no design and no phase owns a file."""
    try:
        owners = [(a.index, a.title, list(a.files)) for a in run.agents
                  if getattr(a, "files", None)]
        return plan_check.render_design(
            getattr(run, "design", None) or {}, owners,
            agent.index if agent is not None else None,
            list(getattr(agent, "files", None) or ()) if agent is not None else (),
            DESIGN_CHARS)
    except Exception:                                            # noqa: BLE001
        return ""


def design_view(run):
    """{"line": "Design: 3 components, 4 interfaces", "text": the block} for
    the Build page's collapsed design row, or None when the run has none."""
    if run is None or not getattr(run, "design", None):
        return None
    return {"line": plan_check.design_line(run.design), "text": design_block(run)}


def _agent_prompt(run, agent):
    """One worker's whole brief.

    Parents contribute their SUMMARY, never their transcript. That is the
    difference between a swarm whose cost grows linearly and one that grows
    quadratically -- and it is why the orchestrator can read everything while no
    single worker has to."""
    # THE TASK LEADS. This file's first live run had both workers reply
    # "What's the shared goal? I need the task before I can start" -- they had
    # been handed the task, and answered the FRAMING instead, then went looking
    # in the project folder for a brief that does not carry a task. That is the
    # same failure _build_argv_codex already records in its own words: "leading
    # with the notice made the agent answer the notice instead of the user".
    # So the imperative comes first and every word of context comes after it.
    parts = [agent.task, ""]
    if agent.done_when:
        parts += ["DONE WHEN: " + agent.done_when, ""]
    # The managed brief, still ahead of the framing: these ARE instructions.
    # Absent on a plain plan, so that prompt is byte-for-byte what it was.
    for label, value in (("INPUTS", agent.inputs), ("CONSTRAINTS", agent.constraints),
                         ("ACCEPTANCE (you will be checked against this)",
                          agent.acceptance),
                         ("YOUR FINAL MESSAGE MUST CONTAIN", agent.output_format)):
        if value:
            parts += [label + ": " + value, ""]
    # THE GOAL BEHIND THIS WORK (taskboard, via app): why this run exists and
    # what else is still open under the same goal, so a worker's phase serves
    # the outcome and not just its own title. Ahead of the design, still an
    # instruction. "" (no active goal) = the prompt is unchanged.
    gb = getattr(run, "goal_brief", "")
    if gb:
        parts += ["THE GOAL BEHIND THIS WORK (keep every step serving it):",
                  gb, ""]
    # THE SHARED DESIGN (owner, 2026-10-04: "design, plan well in a perfect
    # architecture, then go"): helpers working at the same time build to the
    # same interfaces, and each knows which files are its own. Absent for a
    # plan with no design and no owned files -- that prompt is unchanged.
    design = design_block(run, agent)
    if design:
        parts += [design, ""]
    if agent.revisions and agent.problems:
        # A REVISION. The first attempt's files are still in the folder; what
        # this attempt needs is the list of what was wrong with them, stated
        # as the thing to do -- not the previous transcript.
        parts += ["A PREVIOUS ATTEMPT AT THIS PHASE WAS CHECKED AND REJECTED. Its "
                  "work is already in the project folder. Fix these problems:"]
        parts += ["- " + p for p in agent.problems]
        parts += [""]
    if _is_review(run, agent) and any(a.verified is not None or a.reviewed
                                      or a.claimed_unobserved for a in run.agents
                                      if a is not agent):
        # THE REVIEW KNOWS WHAT WAS ALREADY CHECKED, so it spends its turn on
        # what is left instead of re-deriving it from the summaries -- and
        # which of it was OBSERVED (a test run) versus only read (a summary).
        parts += ["VERIFICATION OF THE OTHER PHASES:"]
        for a in run.agents:
            if a is agent:
                continue
            check = check_of(a) or {}
            if a.verified is True:
                parts.append("- phase %d (%s): verified -- %s; leave it alone unless "
                             "something else breaks it" % (a.index, a.title,
                                                            check.get("text") or "observed"))
            elif a.verified is False:
                parts.append("- phase %d (%s): FAILED its checks -- fix: %s"
                             % (a.index, a.title, "; ".join(a.problems) or a.error or "?"))
            elif a.claimed_unobserved:
                parts.append("- phase %d (%s): says its tests/build pass, but no passing "
                             "run was observed -- run them" % (a.index, a.title))
            elif a.reviewed:
                parts.append("- phase %d (%s): reviewed by the manager (its summary, not "
                             "a test run)" % (a.index, a.title))
            else:
                parts.append("- phase %d (%s): %s, not checked" % (a.index, a.title, a.state))
        parts += ["Fix what is still failing first; do not redo verified work.", ""]
    parts += ["--- context, not instructions ---",
              "You are agent %d of %d in a parallel swarm. Everything you need "
              "is in this message; there is nobody to ask."
              % (agent.index, len(run.agents)),
              "The swarm's overall goal is: " + run.goal,
              "Your phase is called: " + agent.title]
    ctx = _clip_text(getattr(run, "context", ""), WORKER_CONTEXT_CHARS)
    if ctx:
        # The conversation this run continues: what "it" is, what was already
        # decided and built. Context, like the rest of this section.
        parts += ["", "WHAT THE CONVERSATION ALREADY ESTABLISHED:", ctx]
    deps = [a for a in run.agents if a.index in agent.needs]
    finished = [a for a in deps if a.summary]
    if finished:
        parts += ["", "WHAT THE PHASES YOU DEPEND ON PRODUCED:"]
        for d in finished:
            parts.append("--- phase %d (%s) ---" % (d.index, d.title))
            parts.append(d.summary[:DEP_CONTEXT_CHARS])
    missing = [a for a in deps if not a.summary]
    if missing:
        parts += ["",
                  "NOTE: phase(s) %s did not produce a result. Do your own phase "
                  "anyway and state what you had to assume."
                  % ", ".join(str(a.index) for a in missing)]
    # WHERE THE FILES GO, spelled out. MEASURED 2026-09-12: workers on Windows
    # ran `pwd` in the bash tool, got a POSIX spelling of the folder
    # (/tmp/claude/...), and passed THAT to the write tool -- which resolved
    # it to C:\\tmp\\..., so the files landed outside the project and the
    # review phase found nothing. The folder is known; say it, in the form
    # the write tool understands, and say not to trust the other one.
    parts += ["", "THE PROJECT FOLDER IS: " + run.project_dir,
              "Every file you create or edit must be inside that folder. Use "
              "paths relative to it, or that exact absolute spelling. Do not "
              "reuse a path printed by a shell (`pwd` may print it in another "
              "form) and never write to /, /tmp or /workspace.",
              # Detached spellings measured to return, and the hub's PID and
              # port so a worker knows which python is not its to kill (live
              # 2026-09-27: an agent listed python processes and killed one).
              agent_servers.worker_rules()]
    parts += ["", "Work only on YOUR phase, and do it now -- do not ask for "
                  "confirmation. Finish with a short summary of what you "
                  "changed and anything the other agents need to know.",
              # THE TODO LIST STAYS TRUE. MEASURED 2026-09-30: a run's workers
              # never touched PROGRESS.md, so the conversation's list still
              # said "ALL GATES COMPLETE" from the previous turn while four
              # new phases were being worked. Owner: "he should always update
              # his todolist and progress".
              "Keep PROGRESS.md in the project folder true for YOUR phase: "
              "under a heading \"## %s\" keep one line \"- [ ] Phase %d: %s\" "
              "(add it when missing), and when your phase is done tick it "
              "\"- [x]\" with one short line of what you verified. Leave the "
              "other lines as they are." % (run.id, agent.index, agent.title)]
    return "\n".join(parts)


def _drain(agent, events):
    """Consume one worker's event stream into its ring buffer, and keep the last
    assistant message as its summary.

    Tolerant on purpose: this is fed by a real CLI's normalized stream, and a
    worker that emits a shape we do not recognise should still be recorded
    rather than crash the wave."""
    last_text = ""
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("event") == "tool_result":
            # What the CLI reported a command did -- evidence, kept apart
            # from the event log (a 4000-char tail per command would push
            # the phase's real activity out of the ring and the run file).
            agent.last_event_at = time.time()
            note_result(agent, ev)
            continue
        agent.events.append(ev)
        agent.event_total = getattr(agent, "event_total", 0) + 1
        agent.last_event_at = time.time()
        # "event" is what agentic_chat actually emits; "type" is the OpenAI
        # streaming spelling. MEASURED on the first live run: reading only
        # "type" meant every event fell through, every worker looked like it
        # had produced nothing, and two agents that had answered perfectly well
        # were both recorded as failed.
        kind = ev.get("event") or ev.get("type")
        if kind in ("message", "output", "done"):
            text = ev.get("text") or ev.get("content") or ""
            if isinstance(text, str) and text.strip():
                last_text = text
        elif kind in ("error", "stopped"):
            # `detail` is where agentic_chat actually puts the reason -- the
            # other two keys are usually absent, so every failure was recorded
            # as the word "error" and the run told you nothing about why. The
            # first live run failed two phases on "database is locked" and the
            # stored error for both was, literally, "error".
            agent.error = str(ev.get("detail") or ev.get("error")
                              or ev.get("text") or kind)[:400]
    return last_text


# A FAILURE THAT IS NOT THE AGENT'S FAULT.
#
# MEASURED on the first real run of this module against a live hub: a plan with
# three independent phases started three opencode workers at the same instant,
# and two died immediately with
#
#     502  Error: Unexpected error / database is locked
#
# opencode keeps ONE SQLite database for the whole machine
# (~/.local/share/opencode/opencode.db). Three processes opening it together is
# three writers on one file, and the two that lose the race get that. Nothing
# was wrong with the model, the prompt or the work -- the phase simply never
# started, which is exactly what "beaucoup de models dans les swarm no answer"
# looks like from the outside.
#
# Two answers, both here. The wave staggers its spawns so the collision mostly
# does not happen, and a worker that dies on a transient error like this gets
# another go rather than being written off.
_TRANSIENT_ERRORS = (
    "database is locked",       # opencode's shared SQLite store
    "database table is locked",
    "resource temporarily unavailable",
    "sqlite_busy",
    "ebusy",
    "eagain",
    "being used by another process",
)
# Attempts per worker, total. Two is enough for a lock that clears in seconds
# and short enough that a genuinely broken phase still fails quickly.
AGENT_ATTEMPTS = 3
RETRY_BACKOFF = 4.0
# A worker whose CLI ended its turn with NO reply gets ONE more go in a fresh
# session (the project files it already wrote stay). MEASURED 2026-10-03, run
# swarm-bc02d72cd325 phase 1: seven minutes of real work (reads, a capture
# script, a screenshot), then the provider's stream died mid-answer ("SSE
# passthrough error: Response ended prematurely"), opencode exited 0 with no
# message and the phase was written off as "opencode produced no reply.".
# Only once: a phase that twice ends with nothing is a real failure.
_NO_REPLY_MARK = "produced no reply"
NO_REPLY_RETRIES = 1
# Seconds between starting one worker and the next in the same wave. Cheap
# insurance: the collision above is concentrated in the first moments, when
# every worker is opening the same database.
SPAWN_STAGGER = 2.0


def _is_transient(text):
    low = str(text or "").lower()
    return any(marker in low for marker in _TRANSIENT_ERRORS)


def _run_agent(run, agent, spawn, run_turn, configure=None):
    if run.stop_flag.is_set():
        agent.state = STOPPED
        return
    agent.state = RUNNING
    agent.started_at = time.time()
    agent.last_event_at = agent.started_at
    verify = _should_verify(run, agent)
    # OBSERVED EVIDENCE is checked with or without a manager: a test/build
    # command the worker ran that FAILED (and never passed after) is a
    # problem the phase gets its one revision for, at zero manager cost.
    check = not _is_review(run, agent)
    before = _snapshot(run.project_dir)
    outcome = _attempts(run, agent, spawn, run_turn, configure, hold=verify or check)
    if verify or check:
        try:
            _verify_and_revise(run, agent, spawn, run_turn, configure, outcome, before,
                               managed=verify)
        except Exception as exc:                                 # noqa: BLE001
            # Verification is an extra, never the thing that loses a phase:
            # whatever the worker produced stands as it would have without it.
            _log.warning("[swarm] verification of phase %d raised: %s", agent.index, exc)
            if agent.state == RUNNING:
                agent.state = DONE if agent.summary else FAILED
    else:
        _settle_checks(agent)
    agent.ended_at = time.time()
    _write_phase_receipt(run, agent, before)
    # Every phase boundary, not every event: a phase is the unit of work worth
    # surviving a restart, and its summary is what the orchestrator reads back.
    _persist(run)


def _attempts(run, agent, spawn, run_turn, configure=None, hold=False):
    """One go at the phase, retried on transient errors only. Returns the
    outcome (DONE / FAILED / STOPPED).

    `hold`: a phase that is about to be verified stays RUNNING on DONE, so the
    page does not show it finished, then running again, then finished."""
    outcome = FAILED
    no_reply_left = NO_REPLY_RETRIES
    for attempt in range(1, AGENT_ATTEMPTS + 1):
        if run.stop_flag.is_set():
            agent.state = STOPPED
            return STOPPED
        outcome = _run_agent_once(run, agent, spawn, run_turn, configure, hold=hold)
        if agent.state != FAILED:
            break
        if not _is_transient(agent.error):
            if no_reply_left <= 0 or _NO_REPLY_MARK not in str(agent.error or "").lower():
                break
            no_reply_left -= 1
            _log.info("[swarm] phase %d ended with no reply; one more go in a fresh "
                      "session", agent.index)
        if attempt < AGENT_ATTEMPTS:
            # A fresh session too: the one we got may not have survived
            # whatever went wrong while it was being created.
            _retire_session(agent)
            agent.error = None
            agent.state = RUNNING
            time.sleep(RETRY_BACKOFF * attempt)
    return outcome


# --------------------------------------------------------------------------- #
# Verification: the manager checks, the free models fix
# --------------------------------------------------------------------------- #
#
# REQUESTED: the subscription manager "plans, instructs, verifies and fixes;
# free models do the work; minimal subscription tokens". So a phase is checked
# CHEAPLY first -- did it answer, is its answer sane (answer_check), do the
# files its acceptance names exist -- and the manager is asked only when all of
# that passes, on a compact summary, never a transcript. A phase that fails gets
# ONE revision (a fresh worker told what was wrong); still failing, it is handed
# to the review agent by name instead of being silently counted as done.
REVISIONS = 1
# What of a phase's final message the manager sees. Its verdict is about the
# work, and the files list below says what the work touched.
VERIFY_TEXT_CHARS = 3000
VERIFY_MAX_TOKENS = 400
PLAN_MAX_TOKENS = 3000
VERIFY_FILES = 40
# The folder walk that says what a phase changed: bounded, and blind to the
# directories that are nobody's work product.
_SNAPSHOT_MAX_FILES = 5000
_SNAPSHOT_SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", ".next",
                  "dist", "build", ".cache", ".pytest_cache", ".mypy_cache"}

_VERIFY_SYSTEM = """You verify ONE phase of a multi-agent software job, from a summary.
Judge only against the phase's task and acceptance criteria. Missing or broken
work is a problem; style preferences are not. Be brief.

Reply with JSON only:
{"ok": true|false, "problems": ["concrete thing to fix", ...], "mode": "<optional>"}
"mode" is optional: name one of {modes} ONLY if a different kind of model would
clearly do this phase better on its retry."""


def _should_verify(run, agent):
    return run.manager is not None and not _is_review(run, agent)


# --------------------------------------------------------------------------- #
# Observed evidence: what the worker's CLI REPORTED its checks did
# --------------------------------------------------------------------------- #
#
# MEASURED 2026-10-04 (read-only audit): without a manager any phase that ended
# with a summary was DONE; with one, "verified" meant the manager agreed with
# that summary. The CLIs report each command's exit code (agentic_chat's
# tool_result events) -- that, classified by evidence.py, is what decides now.
EVIDENCE_MAX = 30
# What of the observed results the manager's verdict brief carries.
OBSERVED_LINES = 8


def note_result(agent, ev):
    """File one tool_result event on the phase when it ran a test/build
    check. Never raises."""
    try:
        if not evidence.detect(ev.get("command")):
            return
        row = evidence.from_event(ev)
        row["command"] = row["command"][:300]
        row["attempt"] = agent.revisions
        row["at"] = time.time()
        agent.evidence.append(row)
        del agent.evidence[:-EVIDENCE_MAX]
    except Exception:                                            # noqa: BLE001
        pass


def check_of(agent):
    """{"kind", "text"} -- what the phase's result rests on, honestly
    labelled -- or None when there is nothing to say:

        observed_fail  "2 failed (observed)"       a check failed, never re-passed
        observed_pass  "12 passed (observed)"      a check passed, nothing failing
        claimed        "claimed, not checked"      says it passes, none observed
        reviewed       "reviewed"                  the manager read the summary
        no_tests       "no tests ran (observed)"
    """
    ev = list(getattr(agent, "evidence", None) or ())
    text = evidence.label(ev)
    if evidence.outstanding_failures(ev):
        return {"kind": "observed_fail", "text": text}
    if evidence.passes(ev):
        out = {"kind": "observed_pass", "text": text}
        if getattr(agent, "reviewed", False):
            out["reviewed"] = True
        return out
    if getattr(agent, "claimed_unobserved", False):
        return {"kind": "claimed", "text": "claimed, not checked"}
    if getattr(agent, "reviewed", False):
        return {"kind": "reviewed", "text": "reviewed"}
    if text:
        return {"kind": "no_tests", "text": text}
    return None


def _observed_problems(agent):
    """A problem per check that FAILED and was never re-run green (newest
    last, at most three), stated as the thing to fix."""
    out = []
    for f in evidence.outstanding_failures(agent.evidence)[-3:]:
        cmd = " ".join(evidence.inner_command(f.get("command") or "").split())[:160]
        out.append("`%s` failed when you ran it (exit %s: %s). Fix the cause and run it "
                   "again until it passes." % (cmd, f.get("exit_code") if f.get("exit_code")
                                               is not None else "non-zero",
                                               f.get("line") or f.get("tool") or "failure"))
    return out


def _observed_lines(agent):
    lines = []
    for e in agent.evidence[-OBSERVED_LINES:]:
        cmd = " ".join(evidence.inner_command(e.get("command") or "").split())[:120]
        lines.append("- `%s` -> exit %s, %s%s" % (
            cmd, e.get("exit_code") if e.get("exit_code") is not None else "unknown",
            e.get("verdict"), (": " + e["line"]) if e.get("line") else ""))
    return lines


def _settle_checks(agent):
    """`verified` / `claimed_unobserved` from what was observed: verified only
    with an observed PASS and nothing failing behind it."""
    fails = evidence.outstanding_failures(agent.evidence)
    ok = evidence.passes(agent.evidence)
    if ok and not fails:
        agent.verified = True
    elif agent.verified is True:
        agent.verified = None
    agent.claimed_unobserved = bool(not ok and evidence.claims_checks_passed(agent.summary))


def _write_phase_receipt(run, agent, before):
    """receipts/<run id>/<phase>.json when the phase ran test/build checks."""
    if not agent.evidence:
        return
    try:
        path = receipts.write(
            run.id, "multi-phase", agent.evidence, cwd=run.project_dir,
            changed=_changed_files(before, run.project_dir),
            started_at=agent.started_at, ended_at=agent.ended_at, number=agent.index,
            extra={"run_id": run.id, "phase": agent.index, "title": agent.title,
                   "session_id": agent.session_id, "phase_state": agent.state,
                   "verified": agent.verified, "reviewed": bool(agent.reviewed),
                   "slop": agent.slop})
        if path:
            agent.receipt = path
    except Exception:                                            # noqa: BLE001
        pass


def _snapshot(folder):
    """{relative path: (mtime, size)} for the project folder, or {} when it
    cannot be read. Never raises."""
    out = {}
    try:
        if not folder or not os.path.isdir(folder):
            return out
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if d not in _SNAPSHOT_SKIP]
            for name in files:
                path = os.path.join(root, name)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                out[os.path.relpath(path, folder).replace("\\", "/")] = (st.st_mtime, st.st_size)
                if len(out) >= _SNAPSHOT_MAX_FILES:
                    return out
    except Exception:                                            # noqa: BLE001
        pass
    return out


def _changed_files(before, folder):
    if before is None:
        return []
    after = _snapshot(folder)
    return sorted(p for p, sig in after.items() if before.get(p) != sig)


# A file an acceptance line names. Backticked names always count; a bare one
# only with a real file extension -- and never a framework written like a file
# ("Node.js", "Next.js"), which is the false positive that matters here.
_FILE_EXTS = ("html|htm|css|scss|js|mjs|cjs|ts|tsx|jsx|py|json|md|txt|yml|yaml|toml|"
              "sql|sh|bat|ps1|go|rs|java|kt|c|cc|cpp|h|hpp|cs|php|rb|vue|svelte|xml|"
              "svg|csv|ini|cfg|lock|gradle|swift|dart")
_BARE_FILE_RE = re.compile(r"(?<![\w/.@-])((?:[\w-]+/)*[\w-]+\.(?:%s))(?![\w/])" % _FILE_EXTS, re.I)
_TICK_FILE_RE = re.compile(r"`([^`\s*?]+\.[A-Za-z0-9]{1,8})`")
_NOT_FILES = {"node.js", "next.js", "vue.js", "nuxt.js", "express.js", "react.js",
              "three.js", "chart.js", "d3.js", "socket.io", "p5.js", "alpine.js",
              "ember.js", "backbone.js", "angular.js", "deno.js", "bun.js"}


def _acceptance_files(text):
    found = []
    for rx in (_TICK_FILE_RE, _BARE_FILE_RE):
        for m in rx.finditer(text or ""):
            name = m.group(1).strip().strip("'\"")
            if "://" in name or name.lower() in _NOT_FILES:
                continue
            if rx is _BARE_FILE_RE and "/" not in name and name.lower().endswith(".js") \
                    and name[0].isupper():
                continue                    # "Vue.js"-style names, capitalised
            if name not in found:
                found.append(name)
    return found[:20]


SLOP_REVISION_MAX = 8


def _slop_scan(run, agent, changed):
    """The slop check (slopcheck via verify, no model call) of the web files
    this phase wrote: sets agent.slop and returns the HIGH findings as
    problems for the phase's one revision. Medium/low stay warnings in
    agent.slop. A phase that wrote no web file is never scanned; any failure
    means no findings."""
    try:
        import craft
        import verify
        if not craft.skill_enabled("web_design"):
            return []
        docs = verify.read_web_files(run.project_dir, changed)
        if not docs:
            agent.slop = None
            return []
        rep = verify.slop_report(docs)
    except Exception:                                            # noqa: BLE001
        return []
    c = rep["counts"]
    agent.slop = {"line": rep["line"] or "Slop check: clean", "high": c["high"],
                  "medium": c["medium"], "low": c["low"], "score": rep.get("score"),
                  "files": len(docs), "warnings": list(rep["warnings"])[:SLOP_REVISION_MAX],
                  "problems": list(rep["high"])[:SLOP_REVISION_MAX]}
    return list(rep["high"])[:SLOP_REVISION_MAX]


def _cheap_problems(run, agent, outcome):
    """What is plainly wrong without asking anyone. [] when nothing is.

    First of all: a test/build command the worker ran that FAILED and was
    never re-run green -- observed, so no model has to judge it."""
    if outcome != DONE or not agent.summary:
        return [agent.error or "the agent produced no result"]
    problems = _observed_problems(agent)
    try:
        verdict = answer_check.inspect(agent.summary, prompt_text=agent.task)
        if not verdict.get("ok", True):
            problems.append("the agent's final message is broken (%s); redo the "
                            "phase and end with a clean summary"
                            % ", ".join(verdict.get("reasons") or ["unreadable"]))
    except Exception:                                            # noqa: BLE001
        pass
    for name in _acceptance_files(agent.acceptance):
        path = name if os.path.isabs(name) else os.path.join(run.project_dir or "", name)
        if not os.path.exists(path):
            problems.append("`%s` is named in the acceptance criteria but does not "
                            "exist in the project folder" % name)
    return problems


def _manager_call(run, system, user, purpose, max_tokens):
    """One manager call's text, its tokens charged to the run. "" on any
    failure -- the caller then carries on exactly as if there were no manager."""
    if run is None or run.manager is None:
        return ""
    text, tokens, called = _ask_manager(run.manager, system, user, purpose, max_tokens)
    if called:
        run.charge(tokens)
    return text


def _ask_manager(manager, system, user, purpose, max_tokens):
    """(text, tokens, charged?) from the injected manager. Never raises. A call
    that returned nothing and cost nothing (disabled, over budget) is not
    counted as a call."""
    try:
        got = manager(system, user, purpose, max_tokens)
        text, tokens = got if isinstance(got, tuple) else (got, 0)
        tokens = max(0, int(tokens or 0))
    except Exception as exc:                                     # noqa: BLE001
        _log.warning("[swarm] manager %s raised: %s", purpose, exc)
        return "", 0, False
    text = str(text or "")
    return text, tokens, bool(text or tokens)


def _verdict_brief(run, agent, changed):
    """The lines a verdict is asked on: goal, phase, task, acceptance,
    context excerpt, changed files, the OBSERVED checks and the final
    message. The manager's and the free verifier's brief alike."""
    brief = ["Overall goal: " + (run.goal or "")[:600],
             "Phase %d: %s" % (agent.index, agent.title),
             "Task: " + agent.task[:1500]]
    if agent.done_when:
        brief.append("Done when: " + agent.done_when)
    if agent.acceptance:
        brief.append("Acceptance: " + agent.acceptance)
    ctx = _clip_text(getattr(run, "context", ""), MANAGER_CONTEXT_CHARS)
    if ctx:
        brief.append("Conversation context (excerpt): " + ctx)
    brief.append("Files changed during the phase: "
                 + (", ".join(changed[:VERIFY_FILES]) or "(none)")
                 + (" (+%d more)" % (len(changed) - VERIFY_FILES)
                    if len(changed) > VERIFY_FILES else ""))
    # WHAT WAS OBSERVED, not only what the summary says: each test/build
    # command the worker ran, with the exit code its CLI reported and the
    # tool's own summary line.
    observed = _observed_lines(agent)
    brief.append("Test/build commands the hub OBSERVED the agent run (exit code + "
                 "the tool's own summary): " + ("\n" + "\n".join(observed) if observed
                                                 else "(none ran)"))
    if not evidence.passes(agent.evidence) and evidence.claims_checks_passed(agent.summary):
        brief.append("NOTE: the final message says tests/build pass, but no passing "
                     "run was observed.")
    text = agent.summary or ""
    if len(text) > VERIFY_TEXT_CHARS:
        text = text[:VERIFY_TEXT_CHARS] + "\n[...clipped]"
    brief += ["", "The agent's final message:", text]
    return brief


def _manager_verdict(run, agent, changed):
    """(problems, suggested mode, answered) from the manager. answered=False
    when it had nothing to say -- no answer, over budget, unreadable. Fail-OPEN:
    a verdict that cannot be read never fails a phase the cheap checks passed,
    but it does not call the phase verified either."""
    brief = _verdict_brief(run, agent, changed)
    system = _VERIFY_SYSTEM.replace("{modes}", ", ".join(run.modes) or "coding")
    raw = _manager_call(run, system, "\n".join(brief), "verify", VERIFY_MAX_TOKENS)
    got = _extract_json(raw)
    if not isinstance(got, dict):
        return [], None, False
    if got.get("ok") is not False:
        return [], None, True
    problems = [str(p).strip()[:300] for p in (got.get("problems") or ())
                if str(p or "").strip()][:8]
    mode = str(got.get("mode") or "").strip().lower() or None
    return (problems or ["the manager rejected the phase without saying why; "
                         "re-check it against the task and acceptance"]), mode, True


# --------------------------------------------------------------------------- #
# Pipelines verify and search (2026-10-04): a FREE verdict without a manager,
# and WIDER-or-DEEPER extra attempts where observed test counts score a phase
# --------------------------------------------------------------------------- #

FREE_VERDICTS_PER_PHASE = 1
_HIGH_SEVERITY = ("high", "critical", "blocker", "severe")
# A final message that admits the work is not complete.
_INCOMPLETE_RE = re.compile(
    r"\b(?:could\s*n[o']t|unable\s+to|not\s+(?:yet\s+)?implemented|todo|stubbed|"
    r"placeholder|skipped|partially|left\s+out|did\s*n[o']t\s+(?:finish|complete)|"
    r"still\s+failing|not\s+tested|untested)\b", re.I)
_SOURCE_EXT_RE = re.compile(
    r"\.(?:py|js|mjs|cjs|ts|tsx|jsx|go|rs|java|rb|php|c|cc|cpp|h|hpp|cs|swift|kt|"
    r"scala|sh|ps1|html?|css|scss|vue|svelte|sql)$", re.I)


def _risky_outcome(agent, changed):
    """Why a finished phase that NO observed check settled deserves a free
    verdict -- "" when nothing about it is risky (docs only, nothing claimed):
    it claims tests/build pass with none observed, admits it is incomplete,
    or changed source files while no check ran at all."""
    summary = agent.summary or ""
    if evidence.claims_checks_passed(summary):
        return "claims checks pass, none observed"
    if _INCOMPLETE_RE.search(summary):
        return "says the work is incomplete"
    if not agent.evidence and any(_SOURCE_EXT_RE.search(str(p)) for p in (changed or ())):
        return "changed source files, no check ran"
    return ""


def _free_verdict_problems(run, agent, changed):
    """Problems from the injected FREE verifier, or []. At most
    FREE_VERDICTS_PER_PHASE per phase; asked only when observed evidence
    decides nothing (an observed PASS = ok, an observed FAIL = the existing
    revision) and the outcome is risky/claimed. Only a not-ok verdict of
    HIGH severity is a problem; anything else -- ok, lower severity, no
    answer, an exception -- leaves the phase as it was."""
    fv = getattr(run, "free_verdict", None)
    if fv is None or agent.free_check is not None:
        return []
    if evidence.passes(agent.evidence) or evidence.outstanding_failures(agent.evidence):
        return []
    reason = _risky_outcome(agent, changed)
    if not reason:
        return []
    brief = {"run_id": run.id, "goal": run.goal, "phase": agent.index,
             "title": agent.title, "task": agent.task, "done_when": agent.done_when,
             "acceptance": agent.acceptance, "summary": agent.summary or "",
             "changed_files": list(changed or ())[:VERIFY_FILES],
             "observed": _observed_lines(agent), "reason": reason,
             "text": "\n".join(_verdict_brief(run, agent, changed))}
    try:
        v = fv(brief)
    except Exception as exc:                                     # noqa: BLE001
        _log.warning("[swarm] free verdict of phase %d raised: %s", agent.index, exc)
        v = None
    if not isinstance(v, dict):
        agent.free_check = {"asked": True, "answered": False, "reason": reason}
        return []
    raw = v.get("problems")
    raw = [raw] if isinstance(raw, str) else (raw if isinstance(raw, (list, tuple)) else [])
    problems = [str(p).strip()[:300] for p in raw if str(p or "").strip()][:8]
    severity = str(v.get("severity") or "").strip().lower()
    ok = v.get("ok") is not False
    agent.free_check = {"asked": True, "answered": True, "ok": ok, "severity": severity,
                        "problems": problems, "reason": reason}
    if ok or severity not in _HIGH_SEVERITY:
        return []
    return problems or ["a reviewer found a high-severity problem in this phase; "
                        "re-check it against the task and acceptance"]


def _observed_score(agent):
    """The phase's observed-test SCORE: the failures still outstanding (a
    check's failed-test count, at least 1 per failing check). 0 = nothing
    observed failing -- the acceptance an extra attempt aims for."""
    return sum(max(1, int(f.get("failed") or 0))
               for f in evidence.outstanding_failures(agent.evidence))


def _search_time_ok(agent, last_secs):
    """An extra attempt past today's one revision only while the phase's own
    budget (AGENT_TIMEOUT from its start) still holds one more attempt as
    long as the last one took."""
    began = agent.started_at or time.time()
    return (time.time() - began) + max(0.0, last_secs or 0.0) <= AGENT_TIMEOUT


def _deeper_attempt(run, agent, run_turn):
    """DEEPER: the SAME worker -- its session, so its model and its whole
    context -- continues with the problems to fix. Returns the outcome; the
    caller publishes the state (hold semantics, like _attempts)."""
    if run.stop_flag.is_set():
        agent.state = STOPPED
        return STOPPED
    try:
        summary = _drain(agent, run_turn(agent.session_id, _agent_prompt(run, agent)))
    except Exception as exc:                                     # noqa: BLE001
        agent.error = "%s: %s" % (exc.__class__.__name__, exc)
        return FAILED
    if agent.abandoned:
        agent.summary = agent.summary or clean_summary(summary or "")
        return FAILED
    agent.summary = clean_summary(summary or "")
    if run.stop_flag.is_set():
        agent.state = STOPPED
        return STOPPED
    if not agent.summary:
        agent.error = agent.error or "the agent produced no result"
        return FAILED
    return DONE


def _verify_and_revise(run, agent, spawn, run_turn, configure, outcome, before,
                       managed=True):
    """Check the phase; on failure run it ONCE more with the problems as
    instructions; still failing, mark it for the review. Sets the phase's
    final state (it was held RUNNING by _attempts).

    `managed` False (no manager): ONLY observed evidence is checked -- an
    empty or broken phase fails exactly as it did before, unretried; a phase
    whose observed test/build run failed gets the same one revision. With a
    FREE verifier (run.free_verdict) a phase no observed check settled and
    whose outcome is risky/claimed gets ONE free verdict; not ok at HIGH
    severity = the same one revision.

    WIDER OR DEEPER (run.search): when the problems include an observed
    failure (the scorer: _observed_score), each revision is either WIDER (a
    fresh worker in a fresh session, its earlier sessions named to the hub so
    it picks another model) or DEEPER (the same session continues), chosen
    by Thompson sampling, and a second one may follow while the phase's time
    budget holds (at most swarm.SEARCH_MAX_EXTRA). Without run.search, or
    with no observed failure, the one revision runs exactly as before."""
    policy = getattr(run, "search", None)
    rounds = max(REVISIONS, policy.max_extra) if policy is not None else REVISIONS
    last_secs = (time.time() - agent.started_at) if agent.started_at else 0.0
    for round_ in range(rounds + 1):
        if run.stop_flag.is_set() or agent.abandoned or outcome == STOPPED \
                or agent.state == STOPPED:
            if agent.state == RUNNING:
                agent.state = STOPPED if run.stop_flag.is_set() else outcome
            _settle_checks(agent)
            return
        if not managed and (outcome != DONE or not agent.summary):
            if agent.state == RUNNING:
                agent.state = outcome if outcome != DONE else FAILED
            _settle_checks(agent)
            return
        changed = _changed_files(before, run.project_dir)
        slop = _slop_scan(run, agent, changed) if (outcome == DONE and agent.summary) else []
        if managed:
            problems = _cheap_problems(run, agent, outcome) + slop
        else:
            problems = _observed_problems(agent) + slop
            if not problems:
                problems = _free_verdict_problems(run, agent, changed)
        mode, answered = None, False
        if not problems and managed:
            problems, mode, answered = _manager_verdict(run, agent, changed)
        if not problems:
            # Passed. "Verified" only with an observed passing check;
            # "reviewed" when the manager read the summary and agreed. With
            # the budget spent it stays unchecked, and done -- as it would
            # have been with no manager at all.
            agent.reviewed = bool(answered)
            agent.problems = []
            agent.error = None
            agent.state = DONE
            _settle_checks(agent)
            return
        agent.problems = problems
        scored = policy is not None and bool(evidence.outstanding_failures(agent.evidence))
        if round_ >= REVISIONS and not (scored and round_ < rounds
                                        and _search_time_ok(agent, last_secs)):
            break
        # THE REVISION: a fresh worker, told exactly what was wrong. A different
        # KIND of model when the manager named one that fits; otherwise the
        # same mode, where the router's weighted pick still lands elsewhere.
        if mode and mode in run.modes and mode != agent.mode:
            agent.mode = mode
        agent.revisions += 1
        previous = agent.summary
        choice, forced, score_before = None, False, 0
        if scored:
            score_before = _observed_score(agent)
            choice = policy.choose()
            if choice == "deeper" and not agent.session_id:
                choice, forced = "wider", True       # no session left to continue
        t0 = time.time()
        if choice == "deeper":
            agent.error = None
            agent.state = RUNNING
            agent.last_event_at = time.time()
            _persist(run)
            outcome = _deeper_attempt(run, agent, run_turn)
        else:
            _retire_session(agent)
            agent.error = None
            agent.state = RUNNING
            agent.last_event_at = time.time()
            _persist(run)
            agent.widen = choice == "wider"
            try:
                outcome = _attempts(run, agent, spawn, run_turn, configure, hold=True)
            finally:
                agent.widen = False
        last_secs = time.time() - t0
        if not agent.summary:
            # A revision that produced nothing must not erase what the first
            # attempt did say -- the review reads it.
            agent.summary = previous
        if choice is not None:
            if outcome == FAILED and previous and not run.stop_flag.is_set() \
                    and not agent.abandoned:
                # A SEARCH attempt that produced nothing improves nothing; the
                # earlier attempt's work (on disk, its summary) still stands and
                # is scored again -- not written off as a failed phase.
                outcome = DONE
                agent.error = None
                agent.state = RUNNING
            score_after = _observed_score(agent)
            policy.note(choice, score_after < score_before, run_id=run.id,
                        phase=agent.index, title=agent.title[:80], attempt=agent.revisions,
                        score_before=score_before, score_after=score_after,
                        accepted=score_after == 0, forced=forced,
                        seconds=round(last_secs, 1))
            _persist(run)
    if agent.state == STOPPED or run.stop_flag.is_set() or agent.abandoned:
        _settle_checks(agent)
        return
    _settle_checks(agent)
    agent.verified = False
    agent.reviewed = False
    agent.state = FAILED
    agent.error = ("did not pass verification: " + "; ".join(agent.problems))[:400]


def _run_agent_once(run, agent, spawn, run_turn, configure=None, hold=False):
    try:
        agent.session_id = spawn(run.cli_id, run.project_dir)
        # DIFFERENT MODELS FOR DIFFERENT PHASES. The planner says what kind of
        # work each phase is; this turns that into the mode its session runs
        # under, so a code phase gets a coding model and a phase that has to
        # read a screenshot gets one that can see. Best-effort: a session that
        # will not take a mode still does its work under the default.
        mode = agent.mode or getattr(run, "default_mode", None)
        if configure and mode:
            try:
                configure(agent.session_id, mode)
            except Exception:                                    # noqa: BLE001
                pass
        if run.stop_flag.is_set():
            # Stop landed while the session was being made: the scheduler's
            # one Stop pass found no session to stop, so no CLI turn either.
            agent.state = STOPPED
            return STOPPED
        summary = _drain(agent, run_turn(agent.session_id, _agent_prompt(run, agent)))
        if agent.abandoned:
            # The wave gave up on this worker and went on without it (and, when
            # the hub could, stopped it). Its result is kept for reading but
            # the phase stays what the run recorded: flipping it to done now
            # would claim the review saw work it never did.
            agent.summary = agent.summary or clean_summary(summary or "")
            return FAILED
        agent.summary = clean_summary(summary or "")
        if run.stop_flag.is_set():
            agent.state = STOPPED
        elif agent.error and not agent.summary:
            agent.state = FAILED
        elif not agent.summary:
            agent.state = FAILED
            agent.error = agent.error or "the agent produced no result"
        elif hold:
            # Finished, but not yet checked: _verify_and_revise publishes it.
            return DONE
        else:
            agent.state = DONE
        return agent.state
    except Exception as exc:                                     # noqa: BLE001
        # A worker that dies must not take the wave with it.
        agent.state = FAILED
        agent.error = "%s: %s" % (exc.__class__.__name__, exc)
        return FAILED


def _give_up(agent, why, stop=None):
    """The wave stops waiting for this worker: mark it, and stop its CLI when
    the hub gave us a way to, so it does not keep writing into the folder
    the review phase is about to read."""
    agent.abandoned = True
    agent.state = FAILED
    agent.error = why
    agent.ended_at = time.time()
    if stop and agent.session_id:
        try:
            stop(agent.session_id)
        except Exception:                                        # noqa: BLE001
            pass


# How often the scheduler looks at its workers: a finished phase's dependents
# start within this, and Stop reaches the running CLIs within this.
_SCHED_TICK = 0.05


def _phase_needs(agent):
    """The phase numbers `agent` waits for, as ints (a hand-edited run file
    may hold strings); [] for anything unreadable."""
    out = []
    for n in (getattr(agent, "needs", None) or ()):
        try:
            out.append(int(n))
        except (TypeError, ValueError):
            continue
    return out


# --------------------------------------------------------------------------- #
# PAIR mode: two helpers (different families) on ONE big phase
# --------------------------------------------------------------------------- #
# Owner, 2026-10-04: "always working TOGETHER on the same task or micro-task,
# whenever the task really needs multiple models". A phase that is big (many
# files, or the planner marked it "parallel") and was not split further gets a
# co-pilot in its own session on another model. Simple and safe by design:
# it READS and reviews freely, writes only files NO phase owns (never the
# lead's, plan_check ownership), runs no installs or servers, coordinates
# through its own PROGRESS.md line, ends when the lead ends, and its notes
# are merged into the phase summary. It only starts in a spare slot (nothing
# else waiting for one) -- setting multi_pair_phases: "auto" (default, on when
# there is a spare slot) | true | false.
PAIR_MIN_FILES = 4
PAIR_GRACE = 5.0                 # seconds the lead's end waits for the pair's stop


def pair_flag():
    """The multi_pair_phases setting: True (on), False (off), or None = auto."""
    try:
        import config
        v = config.get_setting("multi_pair_phases", "auto")
    except Exception:                                            # noqa: BLE001
        return None
    if v in (False, "false", "off", "0", 0):
        return False
    if v in (True, "true", "on", "1", 1):
        return True
    return None


def pair_eligible(run, agent, review=None):
    """May `agent` get a co-pilot? Flag not off, a real (non-review) phase
    that is big -- parallel:true or >= PAIR_MIN_FILES owned files -- with no
    pair yet. The spare-slot condition is the scheduler's."""
    try:
        if pair_flag() is False:
            return False
        if agent is review or getattr(agent, "pair_state", None) or _is_review(run, agent):
            return False
        return bool(getattr(agent, "parallel", False)
                    or len(getattr(agent, "files", None) or ()) >= PAIR_MIN_FILES)
    except Exception:                                            # noqa: BLE001
        return False


def _pair_prompt(run, agent):
    owned = []
    for a in run.agents:
        for f in (a.files or ()):
            owned.append(f)
    mine = list(agent.files or ())
    lines = [
        "You are the CO-PILOT of another helper who owns this phase. Work "
        "WITH it, in your own session, on the SAME task:", "", agent.task, ""]
    if agent.done_when:
        lines += ["DONE WHEN (for the pair): " + agent.done_when, ""]
    lines += [
        "RULES (hard):",
        "- Read anything and review the lead's progress freely.",
        "- NEVER edit or overwrite a file the lead owns: %s."
        % (", ".join(mine) or "any file the lead creates for this phase"),
        "- Write ONLY new files that no phase owns (owned by phases: %s). Pick "
        "tests, docs, fixtures or helper modules the lead did not claim."
        % (", ".join(owned) or "none declared"),
        "- Do NOT run installs, package managers or servers.",
        "- Coordinate through PROGRESS.md in the project folder: under \"## %s\" "
        "keep one line \"- [ ] Phase %d pair: <what you take>\" (tick it when "
        "done) and read the lead's line to avoid overlap." % (run.id, agent.index),
        "- Stop when your part is done; the lead finishes the phase. End with a "
        "SHORT summary: files you wrote, problems you found in the lead's work.",
        "", "THE PROJECT FOLDER IS: " + run.project_dir,
        "The overall goal is: " + run.goal, "Phase: " + agent.title,
        "", agent_servers.worker_rules()]
    return "\n".join(lines)


class _PairSink:
    """What _drain needs of an agent, for the pair's own stream."""

    def __init__(self):
        self.events = deque(maxlen=EVENT_BUFFER)
        self.event_total = 0
        self.last_event_at = None
        self.error = None
        self.evidence = []
        self.revisions = 0


def _run_pair(run, agent, spawn, run_turn, configure=None):
    """The co-pilot's thread. Never raises; the lead is never affected."""
    agent.pair_started_at = time.time()
    sink = _PairSink()
    try:
        agent.pair_session = spawn(run.cli_id, run.project_dir)
        mode = agent.mode or getattr(run, "default_mode", None)
        if configure and mode:
            try:
                configure(agent.pair_session, mode)
            except Exception:                                    # noqa: BLE001
                pass
        if run.stop_flag.is_set():
            agent.pair_state = "stopped"
            return
        text = _drain(sink, run_turn(agent.pair_session, _pair_prompt(run, agent)))
        agent.pair_summary = clean_summary(text or "")
        if agent.pair_state == "stopped":
            return
        agent.pair_error = sink.error
        agent.pair_state = "done" if agent.pair_summary else "failed"
    except Exception as exc:                                     # noqa: BLE001
        agent.pair_state = "failed"
        agent.pair_error = "%s: %s" % (exc.__class__.__name__, exc)
    finally:
        agent.pair_ended_at = time.time()


def _live_pairs(pairs):
    return sum(1 for t, _s in pairs.values() if t.is_alive())


def _finish_pair(agent, entry, stop=None):
    """The lead ended: stop its co-pilot if still going, then merge what it
    found into the phase summary. Never raises."""
    if not entry:
        return
    try:
        t = entry[0]
        if t.is_alive():
            agent.pair_state = "stopped"
            if stop and agent.pair_session:
                try:
                    stop(agent.pair_session)
                except Exception:                                # noqa: BLE001
                    pass
            t.join(PAIR_GRACE)
        note = (agent.pair_summary or "").strip()
        if note and agent.state == DONE:
            agent.summary = ((agent.summary or "").rstrip()
                             + "\n\nCO-PILOT HELPER (second model) NOTES:\n" + note[:2000])
    except Exception:                                            # noqa: BLE001
        pass


def _run_phases(run, indexes, spawn, run_turn, configure=None, stop=None):
    """See _run_phases_loop. The live RAM/CPU monitor (lowres) ticks while a
    run walks; it only ever stops NEW starts."""
    monitor = False
    try:
        import lowres
        lowres.acquire_monitor()
        monitor = True
    except Exception:                                            # noqa: BLE001
        pass
    try:
        _run_phases_loop(run, indexes, spawn, run_turn, configure, stop)
    finally:
        if monitor:
            try:
                lowres.release_monitor()
            except Exception:                                    # noqa: BLE001
                pass


def _run_phases_loop(run, indexes, spawn, run_turn, configure=None, stop=None):
    """Run phases `indexes` of `run`, each AS SOON AS every phase it needs has
    finished -- not when its whole wave has. Owner, 2026-10-04: "each phase
    should work in parallel if it does not need to wait for other things".
    Waves made phase 3 (needs 1) wait for a slow phase 2 only because 2 shared
    phase 1's wave.

    "Finished" is what the wave barrier meant: the worker's thread ended
    (DONE, FAILED or STOPPED, its verification and revision included) or the
    run gave up on it (_give_up). A failed dependency does not block: its
    dependents still run and are told it produced nothing (_agent_prompt).
    A phase already DONE is not run again (a resumed run) and counts as
    finished, like a dependency outside `indexes`.

    Ready phases start in plan order, at most _concurrency() at once (re-read
    before every start), SPAWN_STAGGER apart. A graph that can never be
    satisfied (a cycle, a need that names no phase) runs the rest regardless
    of needs once nothing is running -- waves()' "run the rest together". The
    review phase starts only after every other phase here has finished."""
    total = len(run.agents)
    order = []
    for i in indexes:
        if i not in order and run.agents[i - 1].state != DONE:
            order.append(i)
    if not order:
        return
    order.sort()
    pending = list(order)
    unsettled = set(order)          # in this call and not finished yet
    running = []                    # [thread, agent, index, spawned_at]
    pairs = {}                      # phase index -> [thread, spawned_at] (PAIR mode)
    last = run.agents[-1]
    review = last if getattr(last, "title", None) == REVIEW_TITLE else None
    forced = False
    budget_stopped = False
    last_spawn = None

    def ready(i):
        agent = run.agents[i - 1]
        if agent is review and unsettled - {i}:
            return False
        if forced:
            return True
        return not any(n in unsettled or not 1 <= n <= total
                       for n in _phase_needs(agent))

    while pending or running:
        now = time.time()
        for entry in list(running):
            t, agent, i, spawned = entry
            if not t.is_alive():
                running.remove(entry)
                _finish_pair(agent, pairs.pop(i, None), stop)
                unsettled.discard(i)
                continue
            # Measured from THIS start too: a resumed phase still carries the
            # previous walk's times until its thread writes new ones.
            began = max(agent.started_at or 0.0, spawned)
            quiet = now - max(agent.last_event_at or 0.0, began)
            if now - began > AGENT_TIMEOUT:
                _give_up(agent, "timed out after %ds" % int(AGENT_TIMEOUT), stop)
            elif quiet > AGENT_IDLE_TIMEOUT:
                _give_up(agent, "no output for %ds" % int(quiet), stop)
            else:
                continue
            running.remove(entry)
            _finish_pair(agent, pairs.pop(i, None), stop)
            unsettled.discard(i)
        if run.stop_flag.is_set():
            # STOP MEANS STOP. The flag alone only stopped NEW phases: every
            # worker already running kept its CLI going -- editing the folder
            # and calling the hub, which kept working for it -- until its turn
            # ended by itself. Stop each one's CLI (the hub's stop_session
            # kills the process tree; its open request to the hub then ends
            # too, see app.py "Client disconnect stops the work").
            if stop:
                for _t, agent, _i, _s in running:
                    for sid in (agent.session_id, getattr(agent, "pair_session", None)):
                        if sid:
                            try:
                                stop(sid)
                            except Exception:                    # noqa: BLE001
                                pass
            break
        # BUDGET: a spent budget stops STARTING new phases. Running ones (and
        # their co-pilots) finish their current turn -- no kill, no lost work;
        # the rest are marked "stopped (budget)" once nothing is running, and
        # the run says so. Checked once; the note holds the first reach.
        if getattr(run, "budget", None) and not budget_stopped:
            reached, reason, msg = run.budget_status()
            if reached:
                budget_stopped = True
                run.budget_reached = reason
                run.budget_note = msg
                _log.info("[swarm] %s on run %s -- no new phases will start",
                          msg, run.id)
        if budget_stopped:
            if not running and not pairs:
                break
            time.sleep(_SCHED_TICK)
            continue
        can = [i for i in pending if ready(i)]
        if not can and not running and pending:
            forced = True
            can = [i for i in pending if ready(i)]
        while can:
            # Staggered, not simultaneous. Every worker opens the same CLI
            # state store in its first moments, and starting three at the same
            # instant is what put two of them on "database is locked" (see
            # _TRANSIENT_ERRORS). Seconds against a phase that runs for minutes.
            if SPAWN_STAGGER and last_spawn is not None \
                    and time.time() - last_spawn < SPAWN_STAGGER:
                break
            # The cap is what stops a plan with eight independent phases
            # spawning eight CLI processes at once; re-read, so a run that
            # started with RAM to spare backs off when it runs short.
            if not spawn_allowed(len(running) + _live_pairs(pairs)):
                break
            i = can.pop(0)
            pending.remove(i)
            agent = run.agents[i - 1]
            # `configure` goes to EVERY start: without it, phases past the
            # concurrency cap once silently ran under the default model.
            t = threading.Thread(target=_run_agent,
                                 args=(run, agent, spawn, run_turn, configure),
                                 daemon=True, name="swarm-%s-%d" % (run.id, i))
            t.start()
            last_spawn = time.time()        # after start: the gap is never short
            running.append([t, agent, i, last_spawn])
            try:
                import lowres
                lowres.GOV.note_spawn()
            except Exception:                                    # noqa: BLE001
                pass
        # PAIR mode: only into slots NOTHING else is waiting for.
        if not can and not [i for i in pending if run.agents[i - 1] is not review]:
            for t, agent, i, _s in running:
                if i in pairs or not t.is_alive() or not pair_eligible(run, agent, review):
                    continue
                if SPAWN_STAGGER and last_spawn is not None \
                        and time.time() - last_spawn < SPAWN_STAGGER:
                    break
                if not spawn_allowed(len(running) + _live_pairs(pairs)):
                    break
                pt = threading.Thread(target=_run_pair,
                                      args=(run, agent, spawn, run_turn, configure),
                                      daemon=True, name="pair-%s-%d" % (run.id, i))
                agent.pair_state = "running"
                pt.start()
                last_spawn = time.time()
                pairs[i] = [pt, last_spawn]
                try:
                    import lowres
                    lowres.GOV.note_spawn()
                except Exception:                                # noqa: BLE001
                    pass
        time.sleep(_SCHED_TICK)
    if budget_stopped:
        _mark_budget_stopped(run)


def _mark_budget_stopped(run):
    """Every phase a spent budget kept from starting (still PENDING once the
    running ones finished) is marked STOPPED/"stopped (budget)", and the run's
    note gains how many are left for next time. Returns that count."""
    left = 0
    for a in run.agents:
        if a.state == PENDING:
            a.state = STOPPED
            a.error = BUDGET_STOPPED_ERROR
            a.ended_at = a.ended_at or time.time()
            left += 1
    base = run.budget_note or "Budget reached"
    if left and " left for next time" not in base:
        run.budget_note = "%s — %d task%s left for next time" % (
            base, left, "" if left == 1 else "s")
    return left


def _run_wave(run, indexes, spawn, run_turn, configure=None, stop=None):
    """One group of phases through the scheduler. A wave's phases need nothing
    from each other, so this is the old wave exactly; kept for callers that
    hand it one."""
    _run_phases(run, indexes, spawn, run_turn, configure, stop)


def _walk(run, spawn, run_turn, on_done=None, configure=None, stop=None):
    # BUDGET CLOCK: this walk's active seconds count toward the run's seconds
    # budget, on top of what earlier walks spent (a resume continues from
    # there). Started here, folded into budget_elapsed when the walk ends.
    if getattr(run, "budget", None):
        run.budget_walk_start = time.time()
    try:
        run.state = RUNNING
        # The whole plan at once, dependency-driven (_run_phases). run.waves
        # stays the plan's DISPLAY ("first ... then ..."), not a barrier.
        if not run.stop_flag.is_set():
            _run_phases(run, list(range(1, len(run.agents) + 1)), spawn, run_turn,
                        configure, stop)
        if run.stop_flag.is_set():
            run.state = STOPPED
        elif all(a.state == FAILED for a in run.agents):
            run.state = FAILED
            run.error = "every phase failed"
        else:
            run.state = DONE
    except Exception as exc:                                     # noqa: BLE001
        run.state = FAILED
        run.error = "%s: %s" % (exc.__class__.__name__, exc)
    finally:
        run.ended_at = time.time()
        if run.budget_walk_start:
            run.budget_elapsed = run.budget_active_seconds()
            run.budget_walk_start = None
        _persist(run)
        if on_done:
            try:
                on_done(run)
            except Exception:                                    # noqa: BLE001
                pass
        for hook in list(_RUN_END_HOOKS):
            try:
                hook(run)
            except Exception:                                    # noqa: BLE001
                pass


# Called with the run once it has ended, whatever its state and whoever
# started it (a conversation's multi turn, the MCP tools, a resume) -- see
# add_run_end_hook. app.py cleans each worker's per-session brief out of the
# project folder there.
_RUN_END_HOOKS = []


def _retire_session(agent):
    """Forget a worker's session for a fresh one, remembering the old id so
    the run-end hooks can still clean up after it."""
    if agent.session_id:
        agent.past_sessions = list(getattr(agent, "past_sessions", None) or []) + [
            agent.session_id]
    agent.session_id = None


def add_run_end_hook(fn):
    if fn not in _RUN_END_HOOKS:
        _RUN_END_HOOKS.append(fn)


def worker_session_ids(run):
    """Every session a run's workers used, revisions' earlier ones included."""
    out = []
    for a in getattr(run, "agents", None) or []:
        for sid in list(getattr(a, "past_sessions", None) or []) + [a.session_id]:
            if sid and sid not in out:
                out.append(sid)
    return out


# A second ask when the first answer was not a plan. MEASURED 2026-09-12: a
# multi-session turn died at "could not turn that into phases" eight seconds
# in, on a goal the same planner had turned into two clean phases three times
# that hour -- one model's one bad answer (prose, a truncated object, an empty
# reply) ended the whole turn. The router's weighted pick rarely lands on the
# same model twice in a row, and the nudge tells the next one what went wrong.
PLAN_ATTEMPTS = 2
_PLAN_NUDGE = ("\n\n(Your previous reply could not be read as the JSON object "
               "described. Reply with that JSON object only -- no prose, no "
               "fences, nothing before the opening brace.)")


def _plan_design(obj, phases):
    """The design a plan object carries, {} for a one-phase plan (a small fix
    needs no ceremony) or when there is none."""
    if not isinstance(obj, dict) or len(phases or ()) <= 1:
        return {}
    return plan_check.normalize_design(obj.get("design"))


def plan(goal, planner, max_phases=MAX_AGENTS, modes=(), manager=None, context="",
         out=None):
    """Ask a model to break `goal` into phases. Returns [] when it cannot.

    `out` (a dict, optional) receives "design" (the plan's design, {} for a
    small fix) and "notes" (what clean_phases changed) of the plan returned.

    `planner(system, user) -> str` is injected so this can be tested, and so the
    hub's own routing decides which model plans.

    `manager(system, user) -> str`, when given, plans FIRST with the richer
    brief (_PLAN_SYSTEM_MANAGED) -- once, no nudge: a second paid ask is not
    worth it when the free planner is right here. Its "" (disabled, over
    budget, failed) or an unreadable plan falls through to the free planner,
    unchanged.

    `context` (the conversation so far, see _Run.context) follows the goal
    under its own heading: up to PLAN_CONTEXT_CHARS for the free planner,
    MANAGER_CONTEXT_CHARS for the manager. "" = the goal alone, as before."""
    mode_list = ", ".join(modes) if modes else "coding"
    out = out if isinstance(out, dict) else {}

    def _read(raw):
        obj, notes = _extract_json(raw), []
        got = clean_phases(obj, max_phases, modes, notes=notes)
        if got:
            out["design"], out["notes"] = _plan_design(obj, got), notes
        return got

    if manager is not None:
        try:
            raw = manager(plan_system(goal, True).replace("{modes}", mode_list),
                          _with_context(goal, context, MANAGER_CONTEXT_CHARS))
        except Exception as exc:                                 # noqa: BLE001
            _log.warning("[swarm] manager planner raised: %s", exc)
            raw = ""
        phases = _read(raw)
        if phases:
            return phases
        if raw:
            _log.warning("[swarm] manager plan unreadable (%d chars); free planner "
                         "takes over", len(raw))
    system = plan_system(goal).replace("{modes}", mode_list)
    goal = _with_context(goal, context, PLAN_CONTEXT_CHARS)
    ask = goal
    for attempt in range(1, PLAN_ATTEMPTS + 1):
        try:
            raw = planner(system, ask)
        except Exception as exc:                                 # noqa: BLE001
            _log.warning("[swarm] planner raised on attempt %d: %s", attempt, exc)
            return []
        phases = _read(raw)
        if phases:
            return phases
        _log.warning("[swarm] planner attempt %d was not a plan (%d chars): %r",
                     attempt, len(raw or ""), (raw or "")[:200])
        ask = goal + _PLAN_NUDGE
    return []


# How old an interrupted run may be and still be picked back up. The hub
# restarts itself every five hours; a run interrupted yesterday is one the
# person has long since redone or given up on.
RESUME_MAX_AGE = 6 * 3600


def interrupted_runs():
    """Runs a restart left mid-way that were NOT picked back up (still
    interrupted). Never raises."""
    with _LOCK:
        return [r for r in _RUNS.values()
                if getattr(r, "restored", False) and getattr(r, "interrupted", False)]


def _attach_checks(run, free_verdict=None, search=None):
    """Re-attach the free verifier and the search policy on a resume (neither
    survives a restart as a callable/object; the search's saved log seeds the
    new policy). Absent kwargs leave the run as it is."""
    if callable(free_verdict):
        run.free_verdict = free_verdict
    if search and run.search is None:
        run.search = _search_policy(search, run)


def _attach_budget(run, budget=None, spent=None):
    """Re-attach the spend accountant (never persisted, like the manager) and,
    when the caller passes one, replace the cap. The accumulated seconds stay,
    so a resumed run respects the remaining budget. The budget-reached note is
    cleared so the resumed run can earn a fresh one (and, if the cap is already
    spent, stop again at once)."""
    if callable(spent):
        run.spent_fn = spent
    if budget is not None:
        run.budget = normalize_budget(budget)
    run.budget_reached = None
    run.budget_note = None


def resume_interrupted(spawn, run_turn, configure=None, on_done=None,
                       max_age=RESUME_MAX_AGE, stop=None, manager=None, modes=None,
                       should_resume=None, free_verdict=None, search=None, spent=None):
    """Pick up every run the last process left mid-way. Returns their ids.

    THE WORK GETS FINISHED. A run whose process died was marked failed and
    left there: "interrupted by a hub restart" on every phase that had not
    finished, no review, and -- for a run that was a conversation's turn --
    no reply ever. MEASURED 2026-09-12, twice in one night: a four-phase build
    killed at 00:49 and another at 02:27 by hub restarts, each the person's
    real work, each shown as failed. Asked for in as many words: "work should
    always be finished till the end".

    Phases that were DONE keep their summaries; the ones that were running or
    waiting run again, in fresh sessions, in the same folder -- so a worker
    that had written half its files continues from what is on disk. Then the
    review, then on_done, exactly as if nothing had happened.

    `manager` (same contract as start's) RE-ATTACHES the subscription
    manager: a callable does not survive a restart, so without it every
    resumed phase finished unverified and the report said "not verified by
    the manager" for work the person had paid a manager to check. The run's
    manager_tokens/manager_calls were restored from disk, so the cost keeps
    accumulating on the same run. `modes` restores the worker-mode keys the
    manager's verdict may suggest (also not persisted). None = as before."""
    resumed = []
    now = time.time()
    with _LOCK:
        candidates = [r for r in _RUNS.values() if r.restored and r.interrupted]
    for run in candidates:
        if now - run.created_at > max_age:
            continue
        # A conversation's run continues by itself only when that
        # conversation asked to (app._multi_should_auto_resume).
        try:
            if should_resume is not None and not should_resume(run):
                continue
        except Exception:                                        # noqa: BLE001
            continue
        with run.lock:
            todo = [a for a in run.agents
                    if a.state == FAILED and a.error == INTERRUPTED_ERROR]
            if not todo:
                continue
            for agent in todo:
                agent.state = PENDING
                agent.error = None
                _retire_session(agent)
                agent.started_at = None
                agent.ended_at = None
                agent.free_check = None
            run.state = PENDING
            run.error = None
            run.ended_at = None
            run.restored = False
            run.interrupted = False
            run.stop_flag.clear()
            if manager is not None and run.managed:
                run.manager = manager
            if modes and not run.modes:
                run.modes = tuple(modes)
            _attach_checks(run, free_verdict, search)
            # The cap rode to disk with the run (budget=None keeps it); only
            # the accountant is re-attached, so a budgeted run picked up after
            # a restart still stops where it should.
            _attach_budget(run, budget=None, spent=spent)
        _persist(run)
        threading.Thread(target=_walk, args=(run, spawn, run_turn, on_done, configure, stop),
                         daemon=True, name="swarm-resume-" + run.id).start()
        resumed.append(run.id)
    return resumed


def last_run_for(owner):
    """The most recent run that was a turn of conversation `owner`, or None."""
    if not owner:
        return None
    with _LOCK:
        mine = [r for r in _RUNS.values() if getattr(r, "owner", None) == owner]
    return max(mine, key=lambda r: r.created_at) if mine else None


def worker_info(session_id):
    """{run_id, index, title, owner, state} when `session_id` is (or was) a
    worker of a run, else None -- lets a list of sessions say "helper 2 of
    this conversation's run" instead of showing a bare folder."""
    if not session_id:
        return None
    with _LOCK:
        runs = list(_RUNS.values())
    for run in runs:
        for a in list(getattr(run, "agents", None) or ()):
            if a.session_id == session_id or session_id in (getattr(a, "past_sessions", None) or ()):
                return {"run_id": run.id, "index": a.index, "title": a.title,
                        "owner": getattr(run, "owner", None), "state": a.state,
                        "run_state": run.state}
    return None


def sibling_sessions(session_id):
    """Session ids of the OTHER workers of the run `session_id` works for
    (those that have one yet), in phase order; [] when it is no worker.
    Lets the hub give each worker a different strong model.

    During a WIDER search attempt (agent.widen) the asking worker's OWN
    earlier sessions are named too, after the others: "a fresh attempt by a
    different model" through the hub's existing rotation, no new wiring."""
    if not session_id:
        return []
    with _LOCK:
        runs = list(_RUNS.values())
    for run in runs:
        agents = list(getattr(run, "agents", None) or ())
        me = next((a for a in agents if a.session_id == session_id
                   or getattr(a, "pair_session", None) == session_id), None)
        if me is not None:
            out = [s for a in agents
                   for s in (a.session_id, getattr(a, "pair_session", None))
                   if s and s != session_id]
            if getattr(me, "widen", False):
                out += [s for s in (getattr(me, "past_sessions", None) or ())
                        if s and s != session_id and s not in out]
            return out
    return []


def unfinished(run):
    """The phases of an ENDED run a "continue" should pick back up: every
    phase that did not finish (failed, stopped, never started). [] while the
    run is still going, or when every phase is done."""
    if run is None or run.state in (PENDING, RUNNING):
        return []
    return [a for a in run.agents if a.state != DONE and not _is_review(run, a)] or \
        [a for a in run.agents if a.state != DONE]


def resume(run_id, spawn, run_turn, configure=None, on_done=None, stop=None,
           manager=None, modes=None, context=None, default_mode=None,
           free_verdict=None, search=None, goal_brief=None, budget=None,
           spent=None):
    """Pick an ENDED run back up where it stopped. Returns the run id, or None
    when there is nothing to resume.

    REPORTED: in a conversation set to Multi sessions, "continue" after a run
    that left phases failed was treated as NEW work -- a fresh run whose goal
    was literally "continue", planned from that one word, that rebuilt things
    the first run had finished. A continue now re-runs exactly the phases that
    did not finish (in fresh sessions, the same folder, so a worker that wrote
    half its files carries on from what is on disk), then the review again,
    and the same run's report answers -- like resume_interrupted, for a run
    that ended rather than one a restart cut off.

    `context` (optional) REPLACES the run's conversation context -- the
    conversation has moved on since the run started."""
    run = get(run_id)
    if run is None:
        return None
    with run.lock:
        todo = unfinished(run)
        if not todo:
            return None
        review = [a for a in run.agents if _is_review(run, a) and a not in todo]
        for agent in todo + review:
            agent.state = PENDING
            agent.error = None
            _retire_session(agent)
            agent.started_at = None
            agent.ended_at = None
            agent.abandoned = False
            agent.verified = None
            agent.reviewed = False
            agent.claimed_unobserved = False
            agent.revisions = 0
            agent.free_check = None
        run.state = PENDING
        run.error = None
        run.ended_at = None
        run.restored = False
        run.interrupted = False
        run.stop_flag.clear()
        run.resumes += 1
        if context is not None:
            run.context = _clip_text(context, CONTEXT_CHARS)
        if goal_brief is not None:
            gb = goal_brief() if callable(goal_brief) else goal_brief
            run.goal_brief = _clip_text(gb, GOAL_BRIEF_CHARS)
        if manager is not None and run.managed:
            run.manager = manager
        if modes and not run.modes:
            run.modes = tuple(modes)
        if default_mode:
            run.default_mode = default_mode
        _attach_checks(run, free_verdict, search)
        _attach_budget(run, budget, spent)
    _persist(run)
    threading.Thread(target=_walk, args=(run, spawn, run_turn, on_done, configure, stop),
                     daemon=True, name="swarm-continue-" + run.id).start()
    return run.id


class _PlanMeter:
    """Charges the planning call before the run it belongs to exists."""

    def __init__(self, manager):
        self.manager = manager
        self.tokens = 0
        self.calls = 0

    def ask(self, system, user):
        text, tokens, called = _ask_manager(self.manager, system, user, "plan",
                                            PLAN_MAX_TOKENS)
        if called:
            self.tokens += tokens
            self.calls += 1
        return text


def dry_run(goal, phases, design, project_dir, planner=None, modes=(), context="",
            notes=(), max_phases=MAX_AGENTS):
    """The plan's DRY RUN, before any worker starts: (phases, design, report).

    plan_check.check_plan finds what the plan would get wrong and applies what
    it can (a dependency between two phases that would edit one file at the
    same time, or between a phase and the earlier one that writes what it
    reads). What only the planner can fix (a part the user listed that no
    phase covers) earns ONE re-ask of the FREE `planner`, with the findings
    and the plan they were found in; the revised plan is taken when it fixes
    more than it breaks. Whatever is still wrong is surfaced as a warning and
    the run goes ahead (fail open). No model call besides that one re-ask, no
    command run."""
    fixed, report = plan_check.check_plan(phases, design, goal, project_dir,
                                          notes=notes, max_phases=max_phases)
    replan = [f for f in report["findings"] if f.get("action") == "replan"]
    if replan and planner is not None:
        mode_list = ", ".join(modes) if modes else "coding"
        ask = plan_check.replan_ask(_with_context(goal, context, PLAN_CONTEXT_CHARS),
                                    replan, fixed, design)
        try:
            raw = planner(_PLAN_SYSTEM.replace("{helpers}", str(_concurrency()))
                          .replace("{modes}", mode_list), ask)
        except Exception as exc:                                 # noqa: BLE001
            _log.warning("[swarm] plan re-ask raised: %s", exc)
            raw = ""
        obj, notes2 = _extract_json(raw), []
        again = clean_phases(obj, max_phases, modes, notes=notes2)
        if again:
            design2 = _plan_design(obj, again) or (design if len(again) > 1 else {})
            fixed2, report2 = plan_check.check_plan(
                again, design2, goal, project_dir, notes=notes2, max_phases=max_phases)
            still = [f for f in report2["findings"] if f.get("action") == "replan"]
            if len(still) < len(replan):
                gone = [f.get("part") or f["text"] for f in replan
                        if f["text"] not in {s["text"] for s in still}]
                report2["findings"].insert(0, {
                    "kind": "replanned", "action": "fixed", "phase": None,
                    "text": "re-planned to cover " + ", ".join('"%s"' % g for g in gone)})
                fixed, design, report = fixed2, design2, report2
        report["replanned"] = True
    for f in report["findings"]:
        if f.get("action") == "replan":        # fail open: run, but say so
            f["action"] = "warn"
    return fixed, design, report


def _search_policy(search, run=None):
    """The `search=` kwarg -> a swarm.Search (or None = off). A run read back
    from disk keeps what it learnt: its saved log seeds the posteriors."""
    if not search:
        return None
    import swarm                        # lazy: this module stays a leaf at import
    saved = getattr(run, "search_saved", None) if run is not None else None
    return swarm.make_search(search, log=(saved or {}).get("log"))


def start(goal, project_dir, cli_id, spawn, run_turn, phases=None, planner=None,
          on_done=None, configure=None, modes=(), review=True, owner=None, stop=None,
          manager=None, context="", default_mode=None, free_verdict=None, search=None,
          goal_brief=None, budget=None, spent=None):
    """Begin a run. Returns the run id immediately; the work happens on a
    background thread.

    Either `phases` (already planned) or `planner` must be given.

    `manager(system, user, purpose, max_tokens) -> (text, tokens)` is the
    hub's subscription manager, or None. With it, the manager plans (the free
    `planner` is the fallback) and verifies each phase; without it, the run is
    exactly what it always was.

    `context` is the conversation this run continues (bounded to
    CONTEXT_CHARS): the planner, every worker and the manager's plan and
    verdicts see it under its own heading. "" = the goal alone.

    `free_verdict(phase_brief) -> {"ok", "problems", "severity"} | None` is
    the FREE verifier for a run with no manager (see _free_verdict_problems;
    phase_brief is a dict whose "text" is the rendered brief). `search`
    (True or a swarm.Search) turns on wider-or-deeper revisions where an
    observed test run scores a phase. Both default to the run as before."""
    goal = str(goal or "").strip()
    if not goal:
        raise SwarmWindowsError("a goal is required")
    # THE GOAL BEHIND THE RUN (taskboard, via app): a string, or a callable
    # resolved once here. The planner sees it ahead of the conversation
    # context; every worker's prompt carries it (see _agent_prompt). "" = off.
    gb = goal_brief() if callable(goal_brief) else goal_brief
    gb = _clip_text(gb, GOAL_BRIEF_CHARS)
    meter = _PlanMeter(manager) if manager is not None else None
    info = {}
    if phases is None:
        if planner is None:
            raise SwarmWindowsError("give either phases or a planner")
        plan_context = (gb + "\n\n" + context).strip() if gb else context
        extra = {"context": plan_context} if plan_context else {}
        phases = plan(goal, planner, modes=modes,
                      manager=meter.ask if meter else None, out=info, **extra)
    notes = list(info.get("notes") or ())
    phases = clean_phases({"phases": phases}, modes=modes, notes=notes) if phases else []
    if not phases:
        raise SwarmWindowsError("could not turn that into phases")
    design = info.get("design") or {}
    report = None
    # DESIGN, PLAN, DRY RUN, THEN GO (owner, 2026-10-04). Fail open: a check
    # that breaks never stops the run.
    try:
        phases, design, report = dry_run(goal, phases, design, project_dir,
                                         planner=planner, modes=modes, context=context,
                                         notes=notes)
    except Exception as exc:                                     # noqa: BLE001
        _log.warning("[swarm] plan dry run failed (run goes ahead): %s", exc)
    if review:
        phases = with_review(phases)
    if report is not None:
        try:
            report["max_parallel"] = _concurrency()
        except Exception:                                        # noqa: BLE001
            pass
        plan_check.summarize(report, phases)
        _log.info("[swarm] %s", report.get("line"))
    run = _Run(goal, project_dir, cli_id, phases, owner=owner,
               manager=manager, modes=modes, context=context,
               default_mode=default_mode, design=design, check_report=report,
               free_verdict=free_verdict, search=_search_policy(search),
               goal_brief=gb, budget=budget, spent=spent)
    if meter:
        run.manager_tokens, run.manager_calls = meter.tokens, meter.calls
    _remember(run)
    threading.Thread(target=_walk, args=(run, spawn, run_turn, on_done, configure, stop),
                     daemon=True, name="swarm-walk-" + run.id).start()
    return run.id


def result(run_id):
    """Everything the run produced, for the orchestrator to read.

    Summaries rather than transcripts, deliberately: this is what the parent
    conversation pastes into its own context, and a swarm whose result is five
    full agent transcripts is a swarm that blows up the window it was supposed
    to protect. The transcripts stay readable per agent via status(with_events)."""
    run = get(run_id)
    if not run:
        return None
    return {
        "run_id": run.id, "goal": run.goal, "state": run.state,
        "phases": [{"index": a.index, "title": a.title, "state": a.state,
                    "summary": a.summary, "error": a.error, "mode": a.mode,
                    "session_id": a.session_id, "verified": a.verified,
                    "reviewed": bool(a.reviewed), "check": check_of(a),
                    "receipt": a.receipt,
                    "problems": list(a.problems)}
                   for a in run.agents],
        "done": sum(1 for a in run.agents if a.state == DONE),
        "failed": sum(1 for a in run.agents if a.state == FAILED),
        "verified": sum(1 for a in run.agents if a.verified is True),
        "reviewed": sum(1 for a in run.agents if a.reviewed and a.verified is not True),
        "manager_tokens": run.manager_tokens,
        "manager_calls": run.manager_calls,
        "budget": budget_view(run),
    }


def format_result(run_id):
    """The run as text a model can read back.

    DONE IS NOT VERIFIED: the header counts phases verified by an OBSERVED
    test/build run apart from the ones only reviewed (a manager read the
    summary) -- shown whenever anything was checked at all; a run where
    nothing was reads exactly as before."""
    res = result(run_id)
    if not res:
        return ""
    head = "%d/%d phases done" % (res["done"], len(res["phases"]))
    if any(p.get("check") for p in res["phases"]):
        head += ", %d verified by an observed test/build run" % res["verified"]
        if res["reviewed"]:
            head += ", %d only reviewed" % res["reviewed"]
    lines = ["Swarm run %s - %s (%s)" % (res["run_id"], res["state"], head),
             "Goal: " + res["goal"], ""]
    for p in res["phases"]:
        lines.append("### Phase %d - %s [%s]" % (p["index"], p["title"], p["state"]))
        lines.append(p["summary"] or ("(no result: %s)" % (p["error"] or "unknown")))
        check = p.get("check")
        if check:
            lines.append("Checked: %s" % (_CHECK_WORDS.get(check["kind"], "%s") % check["text"]))
        if p.get("verified") is False and p.get("problems"):
            # Only a checked phase ever sets this; a plain run's report reads
            # exactly as before.
            lines.append("Still failing its checks: " + "; ".join(p["problems"]))
        lines.append("")
    budget = res.get("budget")
    if budget and budget.get("note"):
        # Said in the conversation: "Budget reached: ... -- N tasks left for
        # next time." A run with no budget, or one that finished inside it,
        # adds nothing.
        lines.append(budget["note"])
    return "\n".join(lines).strip()


# How the report words each kind of check (check_of): what was RUN versus
# what was only READ.
_CHECK_WORDS = {
    "observed_pass": "%s",
    "observed_fail": "%s",
    "no_tests": "%s",
    "claimed": "%s -- the summary says tests/build pass, but no passing run was observed",
    "reviewed": "%s by the manager (it read the summary; no test run was observed)",
}


# --------------------------------------------------------------------------- #
# What the page says about parallelism (calm: waiting for RAM is not an error)
# --------------------------------------------------------------------------- #

def parallel_view(run=None):
    """{at_once, max, line, ram_line, waiting, pairs} for the helpers panel and
    the conversation. `at_once` counts running helpers and co-pilots of `run`.
    Never raises."""
    out = {"at_once": 0, "max": MAX_CONCURRENT, "line": "", "ram_line": "",
           "waiting": False, "pairs": []}
    try:
        info = concurrency_info()
        out["max"] = info["now"]
        out["cap"] = info["max"]
        out["limited_by"] = info["limited_by"]
        agents = list(getattr(run, "agents", None) or ())
        out["at_once"] = sum(1 for a in agents if a.state == RUNNING) + sum(
            1 for a in agents if getattr(a, "pair_state", None) == "running")
        out["pairs"] = [a.index for a in agents if getattr(a, "pair_state", None)]
        out["line"] = "%d helper%s at once (max %d)" % (
            out["at_once"], "" if out["at_once"] == 1 else "s", info["now"])
        import lowres
        g = lowres.GOV
        if g.fresh() and g.free_gb is not None and lowres.mode() != "off":
            out["ram_line"] = ("RAM: %.1f GB free, keeping %.1f GB for your other programs"
                               % (g.free_gb, g.reserve or 0.0))
            out["waiting"] = bool(
                g.headroom() == 0 and any(a.state == PENDING for a in agents))
            if out["waiting"]:
                out["ram_line"] += " · waiting for " + (
                    "the CPU to calm down" if g.cpu_hold else "RAM to free up")
    except Exception:                                            # noqa: BLE001
        pass
    return out
