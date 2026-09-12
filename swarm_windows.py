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

# How many workers may run at once, whatever the plan says. Each one is a real
# CLI process with a model behind it, so this is a RAM and rate-limit bound as
# much as anything -- the user's own words: "it will consume the ram more".
MAX_CONCURRENT = 4
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
            os.replace(tmp, path)
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
            with open(os.path.join(root, name), encoding="utf-8") as fh:
                row = json.load(fh)
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
- Prefer phases that can run AT THE SAME TIME. Two phases that touch the same
  file are not independent; say so with "needs".
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
        out.append({
            "title": str(p.get("title") or ("Phase %d" % idx)).strip()[:80],
            "task": task[:4000],
            "done_when": str(p.get("done_when") or "").strip()[:400],
            "needs": needs,
            "mode": mode,
        })
    return out if len(out) >= 1 else []


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
                 "ended_at", "events", "last_event_at", "abandoned")

    def __init__(self, index, phase):
        self.index = index
        self.title = phase["title"]
        self.task = phase["task"]
        self.done_when = phase.get("done_when") or ""
        self.needs = list(phase.get("needs") or ())
        self.mode = phase.get("mode") or None
        self.session_id = None
        self.state = PENDING
        self.summary = ""
        self.error = None
        self.started_at = None
        self.ended_at = None
        self.events = deque(maxlen=EVENT_BUFFER)
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
                 "restored", "interrupted", "store_root", "owner")

    def __init__(self, goal, project_dir, cli_id, phases, owner=None):
        self.id = "swarm-" + uuid.uuid4().hex[:12]
        self.goal = goal
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

    def row(self, with_events=False):
        return {
            "run_id": self.id, "goal": self.goal, "state": self.state,
            "project_dir": self.project_dir, "cli": self.cli_id,
            "owner": self.owner,
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
                   "needs": list(a.get("needs") or ()), "mode": a.get("mode")}
                  for a in (row.get("agents") or ())
                  if isinstance(a, dict)]
        if not phases:
            return None
        run = cls(row.get("goal") or "", row.get("project_dir") or "",
                  row.get("cli") or "", phases)
        run.id = str(row["run_id"])
        run.owner = row.get("owner") or None
        run.state = row.get("state") or DONE
        run.error = row.get("error")
        run.created_at = float(row.get("created_at") or time.time())
        run.ended_at = row.get("ended_at")
        run.waves = [list(w) for w in (row.get("waves") or ())] or run.waves
        run.restored = True
        for agent, a in zip(run.agents, row.get("agents") or ()):
            agent.session_id = a.get("session_id")
            agent.state = a.get("state") or PENDING
            agent.summary = a.get("summary") or ""
            agent.error = a.get("error")
            agent.started_at = a.get("started_at")
            agent.ended_at = a.get("ended_at")
            for e in (a.get("log") or ()):
                agent.events.append(e)
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
    parts += ["--- context, not instructions ---",
              "You are agent %d of %d in a parallel swarm. Everything you need "
              "is in this message; there is nobody to ask."
              % (agent.index, len(run.agents)),
              "The swarm's overall goal is: " + run.goal,
              "Your phase is called: " + agent.title]
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
              "Do not start a server or any long-running process from the shell "
              "(no `&`, `nohup`, `start`, or a watch mode): the shell tool waits "
              "for it and the turn hangs. To check a server, run a one-shot "
              "command with a short timeout, or leave it to the hub's preview."]
    parts += ["", "Work only on YOUR phase, and do it now -- do not ask for "
                  "confirmation. Finish with a short summary of what you "
                  "changed and anything the other agents need to know."]
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
    for attempt in range(1, AGENT_ATTEMPTS + 1):
        if run.stop_flag.is_set():
            agent.state = STOPPED
            break
        _run_agent_once(run, agent, spawn, run_turn, configure)
        if agent.state != FAILED or not _is_transient(agent.error):
            break
        if attempt < AGENT_ATTEMPTS:
            # A fresh session too: the one we got may not have survived
            # whatever went wrong while it was being created.
            agent.session_id = None
            agent.error = None
            agent.state = RUNNING
            time.sleep(RETRY_BACKOFF * attempt)
    agent.ended_at = time.time()
    # Every phase boundary, not every event: a phase is the unit of work worth
    # surviving a restart, and its summary is what the orchestrator reads back.
    _persist(run)


def _run_agent_once(run, agent, spawn, run_turn, configure=None):
    try:
        agent.session_id = spawn(run.cli_id, run.project_dir)
        # DIFFERENT MODELS FOR DIFFERENT PHASES. The planner says what kind of
        # work each phase is; this turns that into the mode its session runs
        # under, so a code phase gets a coding model and a phase that has to
        # read a screenshot gets one that can see. Best-effort: a session that
        # will not take a mode still does its work under the default.
        if configure and agent.mode:
            try:
                configure(agent.session_id, agent.mode)
            except Exception:                                    # noqa: BLE001
                pass
        summary = _drain(agent, run_turn(agent.session_id, _agent_prompt(run, agent)))
        if agent.abandoned:
            # The wave gave up on this worker and went on without it (and, when
            # the hub could, stopped it). Its result is kept for reading but
            # the phase stays what the run recorded: flipping it to done now
            # would claim the review saw work it never did.
            agent.summary = agent.summary or (summary or "").strip()
            return
        agent.summary = (summary or "").strip()
        if run.stop_flag.is_set():
            agent.state = STOPPED
        elif agent.error and not agent.summary:
            agent.state = FAILED
        elif not agent.summary:
            agent.state = FAILED
            agent.error = agent.error or "the agent produced no result"
        else:
            agent.state = DONE
    except Exception as exc:                                     # noqa: BLE001
        # A worker that dies must not take the wave with it.
        agent.state = FAILED
        agent.error = "%s: %s" % (exc.__class__.__name__, exc)


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
    first = indexes[:MAX_CONCURRENT] if len(indexes) > MAX_CONCURRENT else indexes
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
    queued = list(indexes[MAX_CONCURRENT:]) if len(indexes) > MAX_CONCURRENT else []
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
        while queued and len(threads) < MAX_CONCURRENT:
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


def plan(goal, planner, max_phases=MAX_AGENTS, modes=()):
    """Ask a model to break `goal` into phases. Returns [] when it cannot.

    `planner(system, user) -> str` is injected so this can be tested, and so the
    hub's own routing decides which model plans."""
    system = _PLAN_SYSTEM.replace("{modes}", ", ".join(modes) if modes else "coding")
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
                       max_age=RESUME_MAX_AGE, stop=None):
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
    review, then on_done, exactly as if nothing had happened."""
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
                agent.session_id = None
                agent.started_at = None
                agent.ended_at = None
            run.state = PENDING
            run.error = None
            run.ended_at = None
            run.restored = False
            run.interrupted = False
            run.stop_flag.clear()
        _persist(run)
        threading.Thread(target=_walk, args=(run, spawn, run_turn, on_done, configure, stop),
                         daemon=True, name="swarm-resume-" + run.id).start()
        resumed.append(run.id)
    return resumed


def start(goal, project_dir, cli_id, spawn, run_turn, phases=None, planner=None,
          on_done=None, configure=None, modes=(), review=True, owner=None, stop=None):
    """Begin a run. Returns the run id immediately; the work happens on a
    background thread.

    Either `phases` (already planned) or `planner` must be given."""
    goal = str(goal or "").strip()
    if not goal:
        raise SwarmWindowsError("a goal is required")
    if phases is None:
        if planner is None:
            raise SwarmWindowsError("give either phases or a planner")
        phases = plan(goal, planner, modes=modes)
    phases = clean_phases({"phases": phases}, modes=modes) if phases else []
    if not phases:
        raise SwarmWindowsError("could not turn that into phases")
    if review:
        phases = with_review(phases)
    run = _Run(goal, project_dir, cli_id, phases, owner=owner)
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
                    "session_id": a.session_id}
                   for a in run.agents],
        "done": sum(1 for a in run.agents if a.state == DONE),
        "failed": sum(1 for a in run.agents if a.state == FAILED),
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
        lines.append("")
    return "\n".join(lines).strip()
