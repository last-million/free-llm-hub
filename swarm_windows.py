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

# How many workers may run at once, whatever the plan says. Each one is a real
# CLI process with a model behind it, so this is a RAM and rate-limit bound as
# much as anything -- the user's own words: "it will consume the ram more".
MAX_CONCURRENT = 4


def _concurrency():
    """MAX_CONCURRENT, lowered by low-resource mode (lowres.workers): 1-2 on a
    weak machine, 1 while free RAM is short. Re-read before every spawn, so a
    run that starts when RAM is fine still backs off if it runs short."""
    try:
        import lowres
        return lowres.workers(MAX_CONCURRENT)
    except Exception:                                            # noqa: BLE001
        return MAX_CONCURRENT
# Hard ceiling on workers in a run. The planner is asked for fewer; this is the
# guard against a plan that ignores the ask.
MAX_AGENTS = 8
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
sees only its own task plus the summaries of the phases it declares in "needs".

Rules:
- SPEED: phases that need nothing run AT THE SAME TIME (up to 4 agents). A
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
{"goal": "...", "phases": [{"title": "...", "task": "...", "done_when": "...",
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
    '                            "constraints": "...", "output_format": "...",\n'
    '                            "acceptance": "..."}]}')

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


def clean_phases(plan, max_phases=MAX_AGENTS, modes=()):
    """Validated phases, or [] when the plan is unusable.

    `needs` is sanitised hard for the same reason swarm._clean_phases does it: a
    self-reference or a forward reference deadlocks the wave scheduler, and a
    plan that makes every phase depend on every earlier one is a sequential
    pipeline wearing a swarm's clothes."""
    if not isinstance(plan, dict):
        return []
    out = []
    for p in (plan.get("phases") or [])[:max_phases]:
        if not isinstance(p, dict):
            continue
        task = str(p.get("task") or "").strip()
        if not task:
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
        out.append(row)
    return merge_handoffs(out) if len(out) >= 1 else []


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


def merge_handoffs(phases):
    """`phases` with every look-only phase folded into its ONE follower (the
    phase that needs it, when nothing else does): the follower's agent
    investigates first, then acts. Renumbers "needs". Never raises; a plan
    it cannot read comes back as it was."""
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
    independent and runs concurrently.

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
                 "event_total")

    def __init__(self, index, phase):
        self.index = index
        self.title = phase["title"]
        self.task = phase["task"]
        self.done_when = phase.get("done_when") or ""
        self.needs = list(phase.get("needs") or ())
        self.mode = phase.get("mode") or None
        # The managed brief (see _PLAN_SYSTEM_MANAGED); "" for a plain plan.
        self.inputs = phase.get("inputs") or ""
        self.constraints = phase.get("constraints") or ""
        self.output_format = phase.get("output_format") or ""
        self.acceptance = phase.get("acceptance") or ""
        # Verification (only when a manager is wired): None = never checked,
        # True = passed, False = still failing after its one revision. The
        # problems are what the revision and then the review are told to fix.
        self.verified = None
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
            "mode": self.mode,
            "session_id": self.session_id, "state": self.state,
            "summary": self.summary, "error": self.error,
            "started_at": self.started_at, "ended_at": self.ended_at,
            "events": len(self.events),
            "events_total": self.event_total,
            "inputs": self.inputs, "constraints": self.constraints,
            "output_format": self.output_format, "acceptance": self.acceptance,
            "verified": self.verified, "problems": list(self.problems),
            "revisions": self.revisions,
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
                 "context", "resumes", "default_mode")

    def __init__(self, goal, project_dir, cli_id, phases, owner=None,
                 manager=None, modes=(), context="", default_mode=None):
        self.id = "swarm-" + uuid.uuid4().hex[:12]
        self.goal = goal
        # WHAT THE CONVERSATION ALREADY ESTABLISHED (bounded, see
        # CONTEXT_CHARS): the owner session's memory block, its recap, the
        # previous run's result. The goal alone is one message; "make it
        # better" as a goal meant planning from three words. Persisted, so a
        # resumed run's workers are briefed the same way.
        self.context = _clip_text(context, CONTEXT_CHARS)
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

    def charge(self, tokens):
        with self.lock:
            self.manager_tokens += max(0, int(tokens or 0))
            self.manager_calls += 1

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
                   **{k: a.get(k) or "" for k in BRIEF_FIELDS}}
                  for a in (row.get("agents") or ())
                  if isinstance(a, dict)]
        if not phases:
            return None
        run = cls(row.get("goal") or "", row.get("project_dir") or "",
                  row.get("cli") or "", phases,
                  context=row.get("context") if isinstance(row.get("context"), str) else "")
        try:
            run.resumes = max(0, int(row.get("resumes") or 0))
        except (TypeError, ValueError):
            run.resumes = 0
        run.id = str(row["run_id"])
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
        for agent, a in zip(run.agents, row.get("agents") or ()):
            agent.verified = a.get("verified")
            agent.problems = [str(p) for p in (a.get("problems") or ())][:10]
            agent.revisions = int(a.get("revisions") or 0) if str(
                a.get("revisions") or 0).isdigit() else 0
            agent.session_id = a.get("session_id")
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
    if agent.revisions and agent.problems:
        # A REVISION. The first attempt's files are still in the folder; what
        # this attempt needs is the list of what was wrong with them, stated
        # as the thing to do -- not the previous transcript.
        parts += ["A PREVIOUS ATTEMPT AT THIS PHASE WAS CHECKED AND REJECTED. Its "
                  "work is already in the project folder. Fix these problems:"]
        parts += ["- " + p for p in agent.problems]
        parts += [""]
    if _is_review(run, agent) and any(a.verified is not None for a in run.agents):
        # THE REVIEW KNOWS WHAT WAS ALREADY CHECKED, so it spends its turn on
        # what is left instead of re-deriving it from the summaries.
        parts += ["VERIFICATION OF THE OTHER PHASES:"]
        for a in run.agents:
            if a is agent:
                continue
            if a.verified is True:
                parts.append("- phase %d (%s): verified -- leave it alone unless "
                             "something else breaks it" % (a.index, a.title))
            elif a.verified is False:
                parts.append("- phase %d (%s): FAILED its checks -- fix: %s"
                             % (a.index, a.title, "; ".join(a.problems) or a.error or "?"))
            else:
                parts.append("- phase %d (%s): %s, not verified by the manager"
                             % (a.index, a.title, a.state))
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
    before = _snapshot(run.project_dir) if verify else None
    outcome = _attempts(run, agent, spawn, run_turn, configure, hold=verify)
    if verify:
        try:
            _verify_and_revise(run, agent, spawn, run_turn, configure, outcome, before)
        except Exception as exc:                                 # noqa: BLE001
            # Verification is an extra, never the thing that loses a phase:
            # whatever the worker produced stands as it would have without it.
            _log.warning("[swarm] verification of phase %d raised: %s", agent.index, exc)
            if agent.state == RUNNING:
                agent.state = DONE if agent.summary else FAILED
    agent.ended_at = time.time()
    # Every phase boundary, not every event: a phase is the unit of work worth
    # surviving a restart, and its summary is what the orchestrator reads back.
    _persist(run)


def _attempts(run, agent, spawn, run_turn, configure=None, hold=False):
    """One go at the phase, retried on transient errors only. Returns the
    outcome (DONE / FAILED / STOPPED).

    `hold`: a phase that is about to be verified stays RUNNING on DONE, so the
    page does not show it finished, then running again, then finished."""
    outcome = FAILED
    for attempt in range(1, AGENT_ATTEMPTS + 1):
        if run.stop_flag.is_set():
            agent.state = STOPPED
            return STOPPED
        outcome = _run_agent_once(run, agent, spawn, run_turn, configure, hold=hold)
        if agent.state != FAILED or not _is_transient(agent.error):
            break
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


def _cheap_problems(run, agent, outcome):
    """What is plainly wrong without asking anyone. [] when nothing is."""
    if outcome != DONE or not agent.summary:
        return [agent.error or "the agent produced no result"]
    problems = []
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


def _manager_verdict(run, agent, changed):
    """(problems, suggested mode, answered) from the manager. answered=False
    when it had nothing to say -- no answer, over budget, unreadable. Fail-OPEN:
    a verdict that cannot be read never fails a phase the cheap checks passed,
    but it does not call the phase verified either."""
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
    text = agent.summary or ""
    if len(text) > VERIFY_TEXT_CHARS:
        text = text[:VERIFY_TEXT_CHARS] + "\n[...clipped]"
    brief += ["", "The agent's final message:", text]
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


def _verify_and_revise(run, agent, spawn, run_turn, configure, outcome, before):
    """Check the phase; on failure run it ONCE more with the problems as
    instructions; still failing, mark it for the review. Sets the phase's
    final state (it was held RUNNING by _attempts)."""
    for round_ in range(REVISIONS + 1):
        if run.stop_flag.is_set() or agent.abandoned or outcome == STOPPED \
                or agent.state == STOPPED:
            if agent.state == RUNNING:
                agent.state = STOPPED if run.stop_flag.is_set() else outcome
            return
        changed = _changed_files(before, run.project_dir)
        problems = _cheap_problems(run, agent, outcome)
        mode, answered = None, False
        if not problems:
            problems, mode, answered = _manager_verdict(run, agent, changed)
        if not problems:
            # Passed. "Verified" only when the manager actually said so; with
            # the budget spent it stays unchecked, and done -- as it would
            # have been with no manager at all.
            agent.verified = True if answered else None
            agent.problems = []
            agent.error = None
            agent.state = DONE
            return
        agent.problems = problems
        if round_ >= REVISIONS:
            break
        # THE REVISION: a fresh worker, told exactly what was wrong. A different
        # KIND of model when the manager named one that fits; otherwise the
        # same mode, where the router's weighted pick still lands elsewhere.
        if mode and mode in run.modes and mode != agent.mode:
            agent.mode = mode
        agent.revisions += 1
        previous = agent.summary
        _retire_session(agent)
        agent.error = None
        agent.state = RUNNING
        agent.last_event_at = time.time()
        _persist(run)
        outcome = _attempts(run, agent, spawn, run_turn, configure, hold=True)
        if not agent.summary:
            # A revision that produced nothing must not erase what the first
            # attempt did say -- the review reads it.
            agent.summary = previous
    if agent.state == STOPPED or run.stop_flag.is_set() or agent.abandoned:
        return
    agent.verified = False
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


def _run_wave(run, indexes, spawn, run_turn, configure=None, stop=None):
    # A resumed run (resume_interrupted) walks its waves again; the phases
    # that finished before the interruption keep their summaries and are not
    # run twice.
    indexes = [i for i in indexes if run.agents[i - 1].state != DONE]
    if not indexes:
        return
    threads = []
    cap = _concurrency()
    first = indexes[:cap] if len(indexes) > cap else indexes
    for n, i in enumerate(first):
        agent = run.agents[i - 1]
        t = threading.Thread(target=_run_agent,
                             args=(run, agent, spawn, run_turn, configure),
                             daemon=True, name="swarm-%s-%d" % (run.id, i))
        t.start()
        threads.append((t, agent))
        # Staggered, not simultaneous. Every worker opens the same CLI state
        # store in its first moments, and starting three at the same instant is
        # what put two of them on "database is locked" (see _TRANSIENT_ERRORS).
        # Seconds against a phase that runs for minutes.
        if SPAWN_STAGGER and n + 1 < len(first) and not run.stop_flag.is_set():
            time.sleep(SPAWN_STAGGER)
    # Anything past the concurrency cap runs as soon as a slot frees, which is
    # what the cap is FOR -- a plan with eight independent phases must not spawn
    # eight CLI processes at once.
    queued = list(indexes[cap:]) if len(indexes) > cap else []
    while threads or queued:
        now = time.time()
        for t, agent in list(threads):
            t.join(timeout=0.05)
            if not t.is_alive():
                threads.remove((t, agent))
                continue
            began = agent.started_at or now
            quiet = now - (agent.last_event_at or began)
            if now - began > AGENT_TIMEOUT:
                _give_up(agent, "timed out after %ds" % int(AGENT_TIMEOUT), stop)
                threads.remove((t, agent))
            elif quiet > AGENT_IDLE_TIMEOUT:
                _give_up(agent, "no output for %ds" % int(quiet), stop)
                threads.remove((t, agent))
        if queued:
            cap = _concurrency()
        while queued and len(threads) < cap:
            i = queued.pop(0)
            agent = run.agents[i - 1]
            # `configure` HAS to be passed here too. Without it, every phase
            # past the concurrency cap silently ran under the default model
            # instead of the one its mode asked for -- so a plan with five
            # phases gave the fifth the wrong model, and only ever the fifth,
            # which is exactly the kind of bug that never shows up in a
            # three-phase test.
            t = threading.Thread(target=_run_agent,
                                 args=(run, agent, spawn, run_turn, configure),
                                 daemon=True, name="swarm-%s-%d" % (run.id, i))
            t.start()
            threads.append((t, agent))
        if run.stop_flag.is_set():
            break
        time.sleep(0.02)


def _walk(run, spawn, run_turn, on_done=None, configure=None, stop=None):
    try:
        run.state = RUNNING
        for wave in run.waves:
            if run.stop_flag.is_set():
                break
            _run_wave(run, wave, spawn, run_turn, configure, stop)
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


def plan(goal, planner, max_phases=MAX_AGENTS, modes=(), manager=None, context=""):
    """Ask a model to break `goal` into phases. Returns [] when it cannot.

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
    if manager is not None:
        try:
            raw = manager(_PLAN_SYSTEM_MANAGED.replace("{modes}", mode_list),
                          _with_context(goal, context, MANAGER_CONTEXT_CHARS))
        except Exception as exc:                                 # noqa: BLE001
            _log.warning("[swarm] manager planner raised: %s", exc)
            raw = ""
        phases = clean_phases(_extract_json(raw), max_phases, modes)
        if phases:
            return phases
        if raw:
            _log.warning("[swarm] manager plan unreadable (%d chars); free planner "
                         "takes over", len(raw))
    system = _PLAN_SYSTEM.replace("{modes}", mode_list)
    goal = _with_context(goal, context, PLAN_CONTEXT_CHARS)
    ask = goal
    for attempt in range(1, PLAN_ATTEMPTS + 1):
        try:
            raw = planner(system, ask)
        except Exception as exc:                                 # noqa: BLE001
            _log.warning("[swarm] planner raised on attempt %d: %s", attempt, exc)
            return []
        phases = clean_phases(_extract_json(raw), max_phases, modes)
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


def resume_interrupted(spawn, run_turn, configure=None, on_done=None,
                       max_age=RESUME_MAX_AGE, stop=None, manager=None, modes=None):
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
    Lets the hub give each worker a different strong model."""
    if not session_id:
        return []
    with _LOCK:
        runs = list(_RUNS.values())
    for run in runs:
        agents = list(getattr(run, "agents", None) or ())
        if any(a.session_id == session_id for a in agents):
            return [a.session_id for a in agents
                    if a.session_id and a.session_id != session_id]
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
           manager=None, modes=None, context=None, default_mode=None):
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
            agent.revisions = 0
        run.state = PENDING
        run.error = None
        run.ended_at = None
        run.restored = False
        run.interrupted = False
        run.stop_flag.clear()
        run.resumes += 1
        if context is not None:
            run.context = _clip_text(context, CONTEXT_CHARS)
        if manager is not None and run.managed:
            run.manager = manager
        if modes and not run.modes:
            run.modes = tuple(modes)
        if default_mode:
            run.default_mode = default_mode
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


def start(goal, project_dir, cli_id, spawn, run_turn, phases=None, planner=None,
          on_done=None, configure=None, modes=(), review=True, owner=None, stop=None,
          manager=None, context="", default_mode=None):
    """Begin a run. Returns the run id immediately; the work happens on a
    background thread.

    Either `phases` (already planned) or `planner` must be given.

    `manager(system, user, purpose, max_tokens) -> (text, tokens)` is the
    hub's subscription manager, or None. With it, the manager plans (the free
    `planner` is the fallback) and verifies each phase; without it, the run is
    exactly what it always was.

    `context` is the conversation this run continues (bounded to
    CONTEXT_CHARS): the planner, every worker and the manager's plan and
    verdicts see it under its own heading. "" = the goal alone."""
    goal = str(goal or "").strip()
    if not goal:
        raise SwarmWindowsError("a goal is required")
    meter = _PlanMeter(manager) if manager is not None else None
    if phases is None:
        if planner is None:
            raise SwarmWindowsError("give either phases or a planner")
        extra = {"context": context} if context else {}
        phases = plan(goal, planner, modes=modes,
                      manager=meter.ask if meter else None, **extra)
    phases = clean_phases({"phases": phases}, modes=modes) if phases else []
    if not phases:
        raise SwarmWindowsError("could not turn that into phases")
    if review:
        phases = with_review(phases)
    run = _Run(goal, project_dir, cli_id, phases, owner=owner,
               manager=manager, modes=modes, context=context,
               default_mode=default_mode)
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
                    "problems": list(a.problems)}
                   for a in run.agents],
        "done": sum(1 for a in run.agents if a.state == DONE),
        "failed": sum(1 for a in run.agents if a.state == FAILED),
        "manager_tokens": run.manager_tokens,
        "manager_calls": run.manager_calls,
    }


def format_result(run_id):
    """The run as text a model can read back."""
    res = result(run_id)
    if not res:
        return ""
    lines = ["Swarm run %s - %s (%d/%d phases done)"
             % (res["run_id"], res["state"], res["done"], len(res["phases"])),
             "Goal: " + res["goal"], ""]
    for p in res["phases"]:
        lines.append("### Phase %d - %s [%s]" % (p["index"], p["title"], p["state"]))
        lines.append(p["summary"] or ("(no result: %s)" % (p["error"] or "unknown")))
        if p.get("verified") is False and p.get("problems"):
            # Only a manager-verified run ever sets this; a plain run's report
            # reads exactly as before.
            lines.append("Still failing its checks: " + "; ".join(p["problems"]))
        lines.append("")
    return "\n".join(lines).strip()
