"""Calvoun Free LLM Hub — hub_mcp: the hub as an MCP server (streamable HTTP).

WHY THE HUB SPEAKS MCP AT ALL
-----------------------------
Agent CLIs (Kimi Code, Codex, Claude Code, opencode) already talk to the hub
as an OpenAI-compatible endpoint, but a crew is not a model call: it is a
5-20 minute multi-agent build. MCP gives those CLIs the crews as NATIVE
TOOLS — the agent decides to delegate a whole project to `crew_code` the way
it decides to read a file, with a schema it can see and a job handle it can
poll, instead of a chat request that looks hung for a quarter of an hour.

WHY A HAND-ROLLED PROTOCOL MODULE
---------------------------------
The MCP streamable-HTTP surface the hub needs is tiny: one POST endpoint
carrying single JSON-RPC 2.0 messages, five methods, three tools. Pulling in
the official SDK (and its pydantic/anyio stack) for that would be the
heaviest dependency in a stdlib-only repo, and most of it — SSE streams,
sessions, batches — is machinery the hub would never turn on. This module is
therefore a pure function, `handle_rpc(payload) -> (response|None, status)`:
app.py owns HTTP, this module owns the protocol, and either side is testable
without the other.

WHY THE RUNNER IS INJECTED
--------------------------
hub_mcp must not import crews: crews imports swarm, which app.py wires with
its dispatch closure, and a circular import at startup is the kind of bug
that only shows up in the hidden supervisor launch. So the crew execution is
a one-line contract — runner(messages, crew_name) -> final text, blocking —
handed in via init() once app.py has built it from crews.run +
crews.format_answer. Until init() runs, tools/call answers a clean JSON-RPC
error instead of exploding.

WHY THREADS AND A CAPPED JOB DICT
---------------------------------
`crew_start` exists for clients whose tool timeout is shorter than a crew
run: it returns a job_id immediately and lets the client poll `crew_result`.
A daemon thread per job is enough — the hub is a single-process Flask app
whose crew pipeline is itself thread-based, and jobs die with the process by
design (a half-built crew artefact is worthless after a restart anyway). The
dict is capped so a polling-happy client cannot grow memory without bound:
past the cap, the oldest FINISHED jobs are evicted first — a running job is
never evicted out from under its poller. Eviction runs both when a job is
created and when one finishes: a burst of simultaneous starts can push the
dict past the cap with everything still running (nothing evictable yet), and
without the finish-time pass it would stay there forever.
"""
import json
import threading
import time
import uuid

# The one MCP revision this module speaks. Pinned in `initialize` so clients
# negotiate exactly what is implemented here — no more, no less.
PROTOCOL_VERSION = "2025-03-26"

SERVER_NAME = "free-llm-hub"
# Overridable via init(version=...) — app.py passes its own hub version stamp.
SERVER_VERSION = "0.1.0"

# Crew names accepted by the tools. "auto" asks crews.detect_crew to pick.
CREW_NAMES = ("auto", "code", "research", "write", "design")

# Finished jobs past this count are evicted oldest-first (see docstring).
MAX_JOBS = 50

_RUNNER = None  # set by init(); None = tools/call fails cleanly
_JOBS = {}  # job_id -> {"status", "text", "error", "created"}
_JOBS_LOCK = threading.Lock()


def init(runner, version=None):
    """Wire the crew execution contract: runner(messages, crew_name) -> str,
    blocking. Called once by app.py after crews.run/format_answer exist."""
    global _RUNNER, SERVER_VERSION
    _RUNNER = runner
    if version:
        SERVER_VERSION = version


# ---------------------------------------------------------------------------
# Tool catalogue — the schemas every MCP client shows its model.
# ---------------------------------------------------------------------------

def _tools():
    crew_prop = {
        "type": "string",
        "enum": list(CREW_NAMES),
        "description": (
            "Which specialist crew to run. 'auto' (default) picks from the "
            "task text."
        ),
    }
    task_prop = {
        "type": "string",
        "description": "The full task or project brief for the crew.",
    }
    return [
        {
            "name": "crew_run",
            "description": (
                "Run a free-llm-hub multi-agent crew (planner -> workers -> "
                "reviewer -> fix pass) on a task and return the final "
                "artefact. WARNING: this BLOCKS for the whole run, typically "
                "5-20 minutes. If your client has a short tool timeout, use "
                "crew_start + crew_result instead."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"task": task_prop, "crew": crew_prop},
                "required": ["task"],
            },
        },
        {
            "name": "crew_start",
            "description": (
                "Start a crew run in the background and return a job_id "
                "immediately. Poll with crew_result. Prefer this over "
                "crew_run when tool calls time out quickly."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"task": task_prop, "crew": crew_prop},
                "required": ["task"],
            },
        },
        {
            "name": "crew_result",
            "description": (
                "Poll a job started by crew_start. Returns "
                '{"status": "running"|"done"|"error", ...}.'
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job_id returned by crew_start.",
                    },
                },
                "required": ["job_id"],
            },
        },
    ]


# ---------------------------------------------------------------------------
# Job bookkeeping (crew_start / crew_result)
# ---------------------------------------------------------------------------

def _evict_finished_locked():
    """Drop oldest finished jobs while over the cap. Caller holds _JOBS_LOCK.
    Running jobs are never evicted, so a burst of starts can leave the dict
    over the cap until the first finish — hence this also runs there."""
    while len(_JOBS) > MAX_JOBS:
        finished = [
            (j["created"], jid)
            for jid, j in _JOBS.items()
            if j["status"] != "running"
        ]
        if not finished:
            break
        del _JOBS[min(finished)[1]]


def _new_job():
    job_id = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        _JOBS[job_id] = {
            "status": "running",
            "text": None,
            "error": None,
            "created": time.time(),
        }
        _evict_finished_locked()
    return job_id


def _finish_job(job_id, status, text=None, error=None):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is not None:
            job["status"] = status
            job["text"] = text
            job["error"] = error
        _evict_finished_locked()


def _run_job(job_id, messages, crew_name):
    """Thread body: a crew failure must land on the job, never kill the
    thread silently — the poller is waiting for an answer either way."""
    try:
        text = _RUNNER(messages, crew_name)
        _finish_job(job_id, "done", text=str(text))
    except Exception as exc:
        _finish_job(job_id, "error",
                    error="%s: %s" % (type(exc).__name__, exc))


# ---------------------------------------------------------------------------
# tools/call
# ---------------------------------------------------------------------------

def _validate_crew_args(arguments):
    """Shared crew_run/crew_start validation. Returns (messages, crew_name)
    or raises ValueError with a client-facing message."""
    task = arguments.get("task")
    if not isinstance(task, str) or not task.strip():
        raise ValueError("'task' must be a non-empty string")
    crew = arguments.get("crew", "auto")
    if crew not in CREW_NAMES:
        raise ValueError(
            "'crew' must be one of: %s" % ", ".join(CREW_NAMES))
    return [{"role": "user", "content": task}], crew


def _text_result(obj, is_error=False):
    result = {
        "content": [{"type": "text", "text": obj if isinstance(obj, str)
                     else json.dumps(obj, ensure_ascii=False)}],
    }
    if is_error:
        result["isError"] = True
    return result


def _call_tool(params):
    """Returns a JSON-RPC error dict OR a tools/call result dict."""
    if not isinstance(params, dict):
        return _error(-32602, "tools/call requires params")
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        return _error(-32602, "'arguments' must be an object")
    if name not in ("crew_run", "crew_start", "crew_result"):
        return _error(-32602, "Unknown tool: %r" % (name,))
    if _RUNNER is None:
        return _error(-32603, "crew runner not wired")

    if name == "crew_result":
        job_id = arguments.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            return _error(-32602, "'job_id' must be a non-empty string")
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            job = dict(job) if job is not None else None
        if job is None:
            return _text_result("unknown job_id: %s" % job_id, is_error=True)
        out = {"status": job["status"]}
        if job["status"] == "done":
            out["text"] = job["text"]
        elif job["status"] == "error":
            out["error"] = job["error"]
        return _text_result(out)

    # crew_run / crew_start share the same arguments.
    try:
        messages, crew_name = _validate_crew_args(arguments)
    except ValueError as exc:
        return _error(-32602, str(exc))

    if name == "crew_start":
        job_id = _new_job()
        threading.Thread(
            target=_run_job, args=(job_id, messages, crew_name),
            daemon=True,
        ).start()
        return _text_result({"job_id": job_id})

    # crew_run — synchronous. isError on crew failure, per MCP convention.
    try:
        return _text_result(str(_RUNNER(messages, crew_name)))
    except Exception as exc:
        return _text_result(
            "crew run failed: %s: %s" % (type(exc).__name__, exc),
            is_error=True)


# ---------------------------------------------------------------------------
# JSON-RPC plumbing
# ---------------------------------------------------------------------------

def _error(code, message):
    return {"code": code, "message": message}


def _respond(rpc_id, result=None, error=None, status=200):
    msg = {"jsonrpc": "2.0", "id": rpc_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    return msg, status


def handle_rpc(payload):
    """Handle ONE JSON-RPC 2.0 message. Returns (response_dict, http_status);
    (None, 204) for notifications, which must not be answered. Never raises:
    any malformed input becomes a JSON-RPC error object."""
    try:
        if not isinstance(payload, dict):
            return _respond(None, error=_error(-32600, "Invalid Request"),
                            status=400)
        rpc_id = payload.get("id")
        method = payload.get("method")
        if payload.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return _respond(rpc_id, error=_error(-32600, "Invalid Request"),
                            status=400)

        # Notifications carry no id and get no response — including unknown
        # notification methods, which JSON-RPC says to ignore.
        if "id" not in payload:
            return None, 204

        if method == "initialize":
            return _respond(rpc_id, result={
                "protocolVersion": PROTOCOL_VERSION,
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "capabilities": {"tools": {}},
            })
        if method == "ping":
            return _respond(rpc_id, result={})
        if method == "tools/list":
            return _respond(rpc_id, result={"tools": _tools()})
        if method == "tools/call":
            out = _call_tool(payload.get("params"))
            if "code" in out:  # JSON-RPC error, not a tool result
                return _respond(rpc_id, error=out)
            return _respond(rpc_id, result=out)
        return _respond(rpc_id, error=_error(-32601, "Method not found"))
    except Exception as exc:  # the never-raises contract, belt and braces
        return _respond(None, error=_error(
            -32603, "Internal error: %s" % type(exc).__name__))
