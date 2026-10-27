"""Parallel agents spread over DIFFERENT strong models, in every mode and every CLI.

OWNER OBSERVATION (2026-10-27): "when it runs many sub-agents at once they use the same model; it
should work perfectly in all modes, categories, efforts and in all supported CLIs."

This file is the permanent proof. It builds ONE healthy fake fleet, then drives the REAL handlers
(`/v1/chat/completions`, `/v1/responses`, `/v1/messages`) through the Flask test client with N = 6
concurrent sessions shaped the way each CLI really sends them, against a fake upstream that records
which (provider, model) answered which session. For every mode x entry cell it asserts

  * DIVERSITY   distinct models among the 6 sessions >= min(6, models inside the owner's quality
                band that are allowed to lead),
  * QUALITY     nobody was served by a model more than `_SPREAD_MAX_DROP` under the best one: the
                weak tail never serves, and diversity never widens the 2.0-point `_AUTO_TOP_BAND`,
  * COHERENCE   a session keeps ITS model on its second turn (the pin is what keeps an agent's work
                coherent; diversity is across sessions, never within one).

THE OWNER RULE THAT MUST STAY INTACT: best AVAILABLE models first. Diversity is a tie-break among
comparable models, never a reason to pick a weaker one, and a fleet that is degraded all at once
legitimately collapses onto the one model that works (`test_a_degraded_fleet_...`).

WHAT THE AUDIT FOUND (see AGENTS.md "Parallel agents use different models"): the router keyed a
session by `_session_key(messages)` = sha256(system[:4000] + first USER message[:4000]). Codex puts
`# AGENTS.md instructions` and `<environment_context>` in its first USER messages, Claude Code puts the
CLAUDE.md `<system-reminder>` in front of the question, so for N codex / Claude Code sessions in one
folder that key was IDENTICAL: one shared pin, one shared model, and `_pinned_elsewhere(exclude_key)`
excluded the sibling's own pin so the spread never even saw it. opencode (question first, distinct
session ids) and CLIs whose first user message is the question were not affected.

HOW MUCH OF THIS IS REAL, AND HOW MUCH IS SIMULATED. The request SHAPES below were checked
(2026-10-27, structure and lengths only) against captures on the owner's machine:
  * codex   -- the rollouts under ~/.codex/sessions and the hub's isolated codex: developer message
               with TWO parts (permissions, skills), then ONE user message with two parts (the
               AGENTS.md block, then <environment_context>), then the task as its own user message.
               (The first harness modelled three separate user messages: wrong.)
  * claude  -- Claude Code's own transcript (api-request records): `system` is a LIST of three text
               blocks; the first user message is [CLAUDE.md <system-reminder>, two more
               <system-reminder>s, slash-command echoes, the question LAST]; Task sub-agents send the
               PARENT's sessionId with their own long task prompt.
Still SIMULATED (no capture to check against): opencode, the "kimi-like" no-id CLI, "bare-wrapped",
and the codex sub-agent prompt_cache_key (assumed = the sub-agent thread's own id). The fake upstream
and fleet are of course synthetic: this proves the pick / pin / spread plumbing end to end through
the real handlers, not what a live provider answers.
"""
import json
import re
import threading

import pytest
import requests

import app as A
import ctxwin

N = 6
SCHEMA = {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}

# --------------------------------------------------------------------------- #
# The healthy fleet: 11 tool-capable listings = 10 model identities over 8 providers.
#   six identities inside the 2.0-point band of the best (138.0), all allowed to lead a tool turn
#   (the tool-proven gemini-3.x flash sits BELOW the band, so `_may_lead_pool`'s threshold is 134.1),
#   one extra HOST of an in-band model (must not count as a second model),
#   and a tail far below (128 / 121 / 110) that must never serve while the band has room.
# Names are chosen so the real category patterns apply (coding, context, vision, seo, uncensored,
# fast ...); scores are fixed by the fake `_benchmark_score` so the test does not depend on the
# boards. No name matches `_TOOL_DIALECT_MISMATCH`, `_LOW_QUALITY_RE` or the version-rank families.
# --------------------------------------------------------------------------- #
FLEET = [
    (138.0, "nvidia", "z-ai/glm-5.3"),
    (137.9, "openrouter", "stealth/space-bunny-alpha"),
    (137.6, "dahl", "moonshotai/kimi-k3"),
    (137.2, "cerebras", "qwen3.8-max"),
    (136.8, "zenmux", "mimo-v2.6-pro"),
    (136.4, "kilocode", "hy4-pro"),
    (134.1, "google", "models/gemini-3.8-flash"),        # tool-proven, below the band
    (134.0, "g4f", "srv_mr9dda:z-ai/glm-5.3"),           # a second HOST of glm-5.3
    (128.0, "mistral", "mistral-large-3"),
    (121.0, "sambanova", "acme/orion-9"),
    (110.0, "cohere", "command-r-plus"),
]
SCORES = {m: s for s, _p, m in FLEET}


def _ident(model):
    return A._normalize_model_identity(model)


# --------------------------------------------------------------------------- #
# The world
# --------------------------------------------------------------------------- #

class _Resp:
    """A fake upstream answer: one READ-ONLY native tool call (so no verifier is ever involved)."""
    status_code = 200
    text = ""

    def __init__(self, tool=True):
        self.headers = {}
        self.tool = tool        # a tool-free request is answered with plain text

    def json(self):
        if not self.tool:
            return {"id": "chatcmpl-x", "object": "chat.completion", "created": 1,
                    "choices": [{"index": 0, "message": {
                        "role": "assistant", "content": "Done. The billing module compiles and "
                                                         "all of its tests pass on this machine."},
                        "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 40, "completion_tokens": 12, "total_tokens": 52}}
        return {"id": "chatcmpl-x", "object": "chat.completion", "created": 1,
                "choices": [{"index": 0, "message": {
                    "role": "assistant", "content": None, "tool_calls": [{
                        "id": "call_9", "type": "function",
                        "function": {"name": "bash", "arguments": "{\"command\": \"ls src\"}"}}]},
                    "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 12, "total_tokens": 52}}

    def close(self):
        pass

    def _frames(self, sep):
        if not self.tool:
            text = self.json()["choices"][0]["message"]["content"]
            yield b"data: " + json.dumps({"choices": [{"index": 0, "delta": {
                "role": "assistant", "content": text}, "finish_reason": None}]}).encode() + sep
            yield b"data: " + json.dumps({"choices": [{"index": 0, "delta": {},
                                                       "finish_reason": "stop"}]}).encode() + sep
            yield b"data: [DONE]" + sep
            return
        first = {"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [{
            "index": 0, "id": "call_9", "type": "function",
            "function": {"name": "bash", "arguments": "{\"command\": \"ls src\"}"}}]},
            "finish_reason": None}]}
        last = {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}
        yield b"data: " + json.dumps(first).encode() + sep
        yield b"data: " + json.dumps(last).encode() + sep
        yield b"data: [DONE]" + sep

    def iter_lines(self, decode_unicode=False):
        return self._frames(b"")

    def iter_content(self, chunk_size=None):
        return self._frames(b"\n\n")


class World:
    """What answered which session. `session_of` reads the TASK-<i> marker the test puts in every
    request, so it works from the request thread and from the roles path's worker threads alike."""

    def __init__(self):
        self.lock = threading.Lock()
        self.served = []                       # (session index, pid, model) of the AGENTIC requests
        self.side = []                         # the same for tool-free side requests

    def dispatch(self, pid, payload, stream):
        text = json.dumps(payload.get("messages") or [], default=str)
        marks = re.findall(r"TASK-(\d+)", text)
        tool = bool(payload.get("tools"))
        with self.lock:
            (self.served if tool else self.side).append(
                (int(marks[-1]) if marks else -1, pid, payload.get("model")))
        return _Resp(tool=tool)

    def by_session(self):
        out = {}
        for i, pid, model in self.served:
            out.setdefault(i, []).append((pid, model))
        return out


@pytest.fixture
def world(monkeypatch):
    by_pid = {}
    for _s, pid, model in FLEET:
        by_pid.setdefault(pid, []).append(model)
    monkeypatch.setattr(A, "_available_providers", lambda *a, **k: list(by_pid))
    monkeypatch.setattr(A, "_prefetch_auto_models",
                        lambda pids: {p: list(by_pid.get(p, ())) for p in pids})
    monkeypatch.setattr(A, "_auto_models", lambda pid: list(by_pid.get(pid, ())))
    monkeypatch.setattr(A, "_provider_capable", lambda pid, est: True)
    monkeypatch.setattr(A, "_supports_tools", lambda pid, m: True)
    monkeypatch.setattr(A, "_is_fast", lambda pid, m: True)
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, m: SCORES.get(m, 100.0))
    monkeypatch.setattr(A, "_context_ok", lambda pid, m, est: True)
    monkeypatch.setattr(A, "_sub_available_providers", lambda: [])
    # A HEALTHY fleet: no learned penalty, no scarcity, no board bonus (the boards are data that
    # changes; this file is about the pick / pin / spread plumbing).
    for name in ("_reliability_penalty", "_latency_penalty", "_answer_quality_penalty",
                 "_sustain_penalty"):
        monkeypatch.setattr(A, name, lambda *a, **k: 0.0)
    monkeypatch.setattr(A, "_ev_agentic_bonus", lambda m: 0.0)
    monkeypatch.setattr(A, "_ev_has_agentic_evidence", lambda m: False)
    # request-path stubs that would touch disk / the network / the owner's config
    monkeypatch.setattr(A, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(A, "_model_block_reason", lambda pid, m: None)
    monkeypatch.setattr(A, "_is_provider_dead", lambda pid: False)
    monkeypatch.setattr(A, "_is_trivial_turn", lambda *a, **k: False)
    monkeypatch.setattr(A, "_cm_multi_cli_intercept", lambda *a, **k: None)   # no real Multi run
    for name in ("_record_chat_usage", "_save_perf_stats", "_act_pick", "_note_ttft",
                 "_note_provider_timeout", "_throttle_failed_hop", "_record_outcome"):
        monkeypatch.setattr(A, name, lambda *a, **k: None)

    def _no_network(*a, **k):
        raise AssertionError("a hermetic routing test reached the network")
    monkeypatch.setattr(requests.sessions.Session, "request", _no_network)

    w = World()
    monkeypatch.setattr(A, "_dispatch_chat", w.dispatch)
    _reset_state()
    yield w
    _reset_state()


def _reset_state():
    """Six sessions that are alive at once = six agentic pins; nothing else is carried over."""
    with A._session_pin_lock:
        A._session_pins.clear()
    with A._worker_model_lock:
        A._WORKER_MODEL.clear()
        A._WORKER_PID.clear()
    with A._route_log_lock:
        A._ROUTE_LOG.clear()
    with A._inflight_lock:
        A._PROVIDER_INFLIGHT.clear()


# --------------------------------------------------------------------------- #
# The CLI shapes. Each builds the request body + headers one CLI sends for session `i`.
# The question carries a TASK-<i> marker so the fake upstream can tell the sessions apart.
# --------------------------------------------------------------------------- #

def _task(i, same=False):
    n = 0 if same else i
    return ("TASK-%d: implement part %d of the billing module -- add the endpoint, the "
            "validation and the tests, then run them." % (i, n))


OPENCODE_SYSTEM = "You are opencode, an interactive CLI tool that helps users with software " \
                  "engineering tasks. " * 80
CODEX_INSTRUCTIONS = "You are Codex, a coding agent running in the Codex CLI. " * 220
# codex's developer message has two parts (measured: 363 and ~5100 chars) -- permissions, skills
CODEX_DEVELOPER = "<permissions instructions>\nFilesystem sandboxing defines which files can " \
                  "be read or written.\n</permissions instructions>"
CODEX_SKILLS = "<skills_instructions>\n" + "A skill is a set of local instructions.\n" * 120 \
               + "</skills_instructions>"
# the AGENTS.md block (measured 3.9K-17.9K chars) and the environment block (0.4K-2.4K) are the TWO
# PARTS OF ONE user message; the task comes in the next user message
CODEX_AGENTS_MD = ("# AGENTS.md instructions for /repo\n\n<INSTRUCTIONS>\n"
                   + "Use 4 spaces. Keep functions small. Run the tests before you finish.\n" * 60
                   + "</INSTRUCTIONS>")
CODEX_ENV = ("<environment_context>\n  <cwd>/repo</cwd>\n  <shell>powershell</shell>\n"
             "  <current_date>2026-10-27</current_date>\n</environment_context>")
# Claude Code: `system` is a LIST of three text blocks (measured 57 / 7101 / 5829 chars)
CLAUDE_SYSTEM = [{"type": "text", "text": "x-anthropic-billing-header: cc_version=2.1.283; cch=00000;"},
                 {"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude. " * 120},
                 {"type": "text", "text": "# Tone and style\nBe concise. " * 200}]
# the CLAUDE.md the user keeps (this one is ~6 KB; the owner's real one is 500K), then two more
# <system-reminder> blocks (measured 1268 and 580 chars) in the SAME first user message
CLAUDE_REMINDER = ("<system-reminder>\nAs you answer the user's questions, you can use the "
                   "following context:\n# claudeMd\n" + "Project rule: keep diffs small.\n" * 200
                   + "</system-reminder>")
CLAUDE_REMINDER_2 = "<system-reminder>\n# currentDate\nToday's date is 2026-10-27.\n" \
                    + "Relevant context line.\n" * 50 + "</system-reminder>"
CLAUDE_REMINDER_3 = "<system-reminder>\nThe following skills are available.\n" \
                    + "- skill: does a thing\n" * 25 + "</system-reminder>"
CLAUDE_ENV = "# Environment\nPrimary working directory: /repo\nPlatform: win32"
# the preamble a lead pastes into every Task sub-agent it starts (a template), then each one's task
TEMPLATE = ("You are one of several parallel agents. Rules shared by all of them: stay in your own "
            "files, run the tests before you finish, never touch the lock file. " * 20)
BARE_SYSTEM = "You are a coding agent. Use the tools to do what the user asks. " * 60
PREAMBLE = "Repository context (auto-generated, identical for every run):\n" + "src/a.py\n" * 700


def _call_item(i):
    return {"id": "c%d" % i, "name": "bash", "args": "{\"command\": \"ls src\"}"}


def _chat_tools():
    return [{"type": "function", "function": {"name": "bash", "description": "run",
                                              "parameters": SCHEMA}}]


def build_request(cli, i, model, turn=1, effort=None, same_task=False, stream=False,
                  claude_md=None, tools=True):
    """(path, post kwargs) for session `i` of `cli`. `turn` 2 appends one tool round;
    `claude_md` replaces the CLAUDE.md reminder block of a Claude Code request; `tools=False`
    is a tool-free side request (a title, a CLI's own compaction call, a plain question)."""
    path, kw = _build_request(cli, i, model, turn, effort, same_task, stream, claude_md)
    if not tools:
        kw["json"].pop("tools", None)
    return path, kw


def _build_request(cli, i, model, turn, effort, same_task, stream, claude_md):
    task = _task(i, same_task)
    if cli in ("opencode", "kimi-like", "bare-wrapped"):
        if cli == "opencode":
            system, user, headers = OPENCODE_SYSTEM, task, {
                "X-Session-Id": "ses_%02d" % i, "x-session-affinity": "ses_%02d" % i,
                "User-Agent": "opencode/1.19.0"}
        elif cli == "kimi-like":                 # no session id of any kind (pi, openclaw, hermes,
            system, user, headers = BARE_SYSTEM, task, {}   # qwen, kimi ... as far as is known)
        else:                                    # a CLI whose first user message is a long preamble
            system, user, headers = BARE_SYSTEM, PREAMBLE + "\n" + task, {}
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        if turn > 1:
            msgs += [{"role": "assistant", "content": None, "tool_calls": [{
                          "id": "c1", "type": "function",
                          "function": {"name": "bash", "arguments": "{\"command\": \"ls src\"}"}}]},
                     {"role": "tool", "tool_call_id": "c1", "content": "a.py b.py"}]
        return "/v1/chat/completions", {
            "json": {"model": model, "stream": stream, "messages": msgs, "tools": _chat_tools()},
            "headers": headers}
    if cli == "codex":
        items = [{"type": "message", "role": "developer",
                  "content": [{"type": "input_text", "text": CODEX_DEVELOPER},
                              {"type": "input_text", "text": CODEX_SKILLS}]},
                 {"type": "message", "role": "user",
                  "content": [{"type": "input_text", "text": CODEX_AGENTS_MD},
                              {"type": "input_text", "text": CODEX_ENV}]},
                 {"type": "message", "role": "user",
                  "content": [{"type": "input_text", "text": task}]}]
        if turn > 1:
            items += [{"type": "function_call", "call_id": "c1", "name": "bash",
                       "arguments": "{\"command\": \"ls src\"}"},
                      {"type": "function_call_output", "call_id": "c1", "output": "a.py b.py"}]
        body = {"model": model, "stream": stream, "instructions": CODEX_INSTRUCTIONS,
                "input": items, "prompt_cache_key": "019e-thread-%02d" % i,
                "tools": [{"type": "function", "name": "bash", "description": "run",
                           "parameters": SCHEMA}]}
        if effort:
            body["reasoning"] = {"effort": effort}
        return "/v1/responses", {"json": body, "headers": {"User-Agent": "codex_cli_rs/0.164"}}
    if cli in ("claude", "claude-subagents", "claude-subagents-templated"):
        # claude-subagents: ONE Claude Code session, six Task sub-agents -> the SAME session id
        # (measured: the sub-agents of a session send the parent's sessionId). The "templated"
        # variant starts every sub-agent from one long shared preamble (> the 2000 chars the
        # content hash reads from the head), so only the TAIL of its prompt tells it apart.
        shared_id = cli != "claude"
        sid = "%08d-aaaa-4bbb-8ccc-000000000000" % (0 if shared_id else i)
        if cli == "claude-subagents-templated":
            task = TEMPLATE + "\n" + task
        reminder = claude_md if claude_md else CLAUDE_REMINDER
        msgs = [{"role": "user", "content": [{"type": "text", "text": reminder},
                                             {"type": "text", "text": CLAUDE_REMINDER_2},
                                             {"type": "text", "text": CLAUDE_REMINDER_3},
                                             {"type": "text", "text": task}]},
                {"role": "system", "content": CLAUDE_ENV}]
        if turn > 1:
            msgs = [msgs[0],
                    {"role": "assistant", "content": [{"type": "tool_use", "id": "c1",
                                                       "name": "bash",
                                                       "input": {"command": "ls src"}}]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1",
                                                  "content": "a.py b.py"}]},
                    msgs[1]]
        body = {"model": model, "max_tokens": 4000, "stream": stream, "system": CLAUDE_SYSTEM,
                "messages": msgs, "metadata": {"user_id": "user_ab12_account_x_session_" + sid},
                "tools": [{"name": "bash", "description": "run", "input_schema": SCHEMA}]}
        return "/v1/messages", {"json": body, "headers": {
            "X-Claude-Code-Session-Id": sid, "User-Agent": "claude-cli/2.1.283"}}
    raise AssertionError(cli)


CLIS = ("opencode", "kimi-like", "codex", "claude")


def run_cell(world, cli, model, turns=1, effort=None, threaded=False, same_task=False,
             stream=False, cli_for_i=None):
    """Drive N sessions of `cli` on `model`; returns {session index: [(pid, model) per turn]}."""
    _reset_state()
    world.served.clear()
    errors = []

    def one(i, turn):
        c = A.app.test_client()
        path, kw = build_request(cli_for_i(i) if cli_for_i else cli, i, model, turn, effort,
                                 same_task, stream)
        r = c.post(path, **kw)
        r.get_data()
        if r.status_code != 200:
            errors.append((cli, model, i, turn, r.status_code, r.get_data()[:160]))

    for turn in range(1, turns + 1):
        if threaded:
            ts = [threading.Thread(target=one, args=(i, turn)) for i in range(N)]
            for t in ts:
                t.start()
            for t in ts:
                t.join(60)
        else:
            for i in range(N):
                one(i, turn)
    assert not errors, errors[:3]
    return world.by_session()


# --------------------------------------------------------------------------- #
# The oracle: what an ideal router may do with this fleet (policy functions, not the pick logic)
# --------------------------------------------------------------------------- #

def expected_for(category=None):
    """(min distinct models the 6 sessions must reach, best score of the category's pool).

    The pool is the fleet restricted to the category (fail-open to the whole fleet when the category
    has no member, exactly as `_apply_mode` does), then to the models allowed to LEAD a tool turn
    (`_may_lead_pool`, fail-open), then the owner's quality band: within `_AUTO_TOP_BAND` of the best."""
    pool = list(FLEET)
    if category:
        members = [c for c in pool if A._mode_allows(category, c[1], c[2])]
        pool = members or pool
    lead = A._may_lead_pool(pool) or pool
    best = max(c[0] for c in lead)
    band = {_ident(c[2]) for c in lead if c[0] >= best - A._AUTO_TOP_BAND}
    return min(N, len(band)), max(c[0] for c in pool)


def check_cell(label, got, want, best_in_pool, turns=1):
    """The three promises. `got` = {session: [(pid, model) ...]}."""
    assert sorted(got) == list(range(N)), (label, "a session was never served", sorted(got))
    first = [got[i][0] for i in range(N)]
    distinct = {_ident(m) for _p, m in first}
    print("[matrix] %-44s distinct=%d expected>=%d  %s" % (
        label, len(distinct), want, sorted(distinct)))
    assert len(distinct) >= want, (label, "COLLAPSED", len(distinct), "<", want, sorted(distinct))
    floor = best_in_pool - A._SPREAD_MAX_DROP
    for _p, m in first:
        assert SCORES[m] >= floor, (label, "served below the quality window", m, SCORES[m], floor)
    if turns > 1:
        for i in range(N):
            ids = {_ident(m) for _p, m in got[i]}
            assert len(ids) == 1, (label, "session %d changed model mid-task" % i, got[i])


# --------------------------------------------------------------------------- #
# 0. The fleet is what this file says it is, and the oracle's inputs are the real policy
# --------------------------------------------------------------------------- #

def test_the_fleet_is_healthy_and_has_room(world):
    assert len(FLEET) >= 8 and len({p for _s, p, _m in FLEET}) >= 5
    ids = {_ident(m) for _s, _p, m in FLEET}
    assert len(ids) == len(FLEET) - 1, "the g4f copy of glm-5.3 is one identity, not two"
    want, best = expected_for(None)
    assert best == 138.0
    assert want == 6, "six identities inside the 2.0 band, all allowed to lead"
    assert all(A._may_lead_pool(FLEET))  # sanity: the lead gate is not what limits THIS fleet
    lead = {_ident(c[2]) for c in A._may_lead_pool(FLEET)}
    assert {_ident(m) for s, _p, m in FLEET if s >= 136.0} <= lead


# --------------------------------------------------------------------------- #
# 1. THE MATRIX, part 1 -- base modes x every CLI shape (2 turns: diversity AND coherence)
# --------------------------------------------------------------------------- #

BASE_MODES = ("auto", "best", "swarm", "multi")


@pytest.mark.parametrize("cli", CLIS)
@pytest.mark.parametrize("model", BASE_MODES)
def test_base_modes_on_every_cli(world, cli, model):
    got = run_cell(world, cli, model, turns=2)
    want, best = expected_for(None)
    check_cell("%s / %s" % (cli, model), got, want, best, turns=2)


# --------------------------------------------------------------------------- #
# 2. part 2 -- every category as the model id, on every CLI (1 turn)
# --------------------------------------------------------------------------- #

CATEGORIES = tuple(A._mode_keys())


def test_the_category_list_is_what_the_matrix_covers():
    assert set(CATEGORIES) == {"coding", "reasoning", "context", "vision", "uncensored", "fast",
                               "seo", "specialist"}, "a new category needs a matrix row"


@pytest.mark.parametrize("cli", CLIS)
@pytest.mark.parametrize("cat", CATEGORIES)
def test_categories_on_every_cli(world, cli, cat):
    got = run_cell(world, cli, cat)
    want, best = expected_for(cat)
    check_cell("%s / %s" % (cli, cat), got, want, best)


# --------------------------------------------------------------------------- #
# 3. part 3 -- effort tiers: compound ids "<category>-<effort>" (opencode / claude), and codex's
#    (category, reasoning level) pair, which is how codex expresses both axes
# --------------------------------------------------------------------------- #

EFFORTS = ("auto", "best", "max", "swarm", "multi")
COMPOUNDS = ["%s-%s" % (c, e) for c in ("coding", "context", "seo") for e in EFFORTS]


@pytest.mark.parametrize("model", COMPOUNDS)
def test_compound_ids_on_chat_and_messages(world, model):
    cat = model.rsplit("-", 1)[0]
    want, best = expected_for(cat)
    for cli in ("opencode", "claude"):
        check_cell("%s / %s" % (cli, model), run_cell(world, cli, model), want, best)


@pytest.mark.parametrize("effort", ("low", "medium", "high", "xhigh"))
@pytest.mark.parametrize("cat", ("coding", "context", "seo"))
def test_codex_category_and_reasoning_level(world, cat, effort):
    want, best = expected_for(cat)
    check_cell("codex / %s @ %s" % (cat, effort),
               run_cell(world, "codex", cat, effort=effort), want, best)


# --------------------------------------------------------------------------- #
# 4. the same cells with the sessions arriving AT ONCE (the pick is serialised by a lock),
#    and as real streams
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("cli", CLIS)
@pytest.mark.parametrize("model", ("auto", "swarm"))
def test_simultaneous_first_turns(world, cli, model):
    want, best = expected_for(None)
    check_cell("%s / %s (threads)" % (cli, model),
               run_cell(world, cli, model, threaded=True), want, best)


@pytest.mark.parametrize("cli", ("opencode", "codex", "claude"))
def test_streaming_requests_route_the_same(world, cli):
    want, best = expected_for(None)
    check_cell("%s / auto (stream)" % cli, run_cell(world, cli, "auto", stream=True), want, best)


# --------------------------------------------------------------------------- #
# 5. SESSION KEYS -- the question of whether concurrent sessions really get DISTINCT keys
# --------------------------------------------------------------------------- #

def _keys(cli, **kw):
    """The key the router would pin each of the N sessions under (inside a real request context)."""
    out = []
    for i in range(N):
        path, req = build_request(cli, i, "auto", **kw)
        body = req["json"]
        with A.app.test_request_context(path, json=body, headers=req["headers"]):
            if cli == "codex":
                msgs = A._responses_to_chat(body)
            elif cli.startswith("claude"):
                msgs = A._anthropic_to_openai_messages(body)
            else:
                msgs = body["messages"]
            msgs, _n = A._normalize_openai_messages(msgs)
            A._ctx_begin(body, msgs, 1000, signal=False)       # what every handler does first
            out.append(A._agent_pin_key(msgs))
    return out


@pytest.mark.parametrize("cli", ("opencode", "kimi-like", "codex", "claude", "claude-subagents",
                                 "claude-subagents-templated"))
def test_concurrent_sessions_get_distinct_keys(cli):
    keys = _keys(cli)
    assert len(set(keys)) == N, (cli, keys)


def test_the_old_content_key_collided_for_codex_and_claude_code():
    """The defect, kept as a regression proof: `_session_key` hashes the first USER message, which is
    the AGENTS.md / CLAUDE.md wrapper for these CLIs, so six different tasks shared ONE key."""
    for cli in ("codex", "claude"):
        old = set()
        for i in range(N):
            path, req = build_request(cli, i, "auto")
            body = req["json"]
            msgs = (A._responses_to_chat(body) if cli == "codex"
                    else A._anthropic_to_openai_messages(body))
            old.add(A._session_key(msgs))
        assert len(old) == 1, (cli, "the old key no longer collides -- is the proof still needed?")
    # ...and it does NOT collide where the question comes first, which is why opencode was fine.
    assert len({A._session_key(build_request("opencode", i, "auto")[1]["json"]["messages"])
                for i in range(N)}) == N


def test_identical_prompts_with_distinct_session_ids_still_spread(world):
    """Six terminals running the SAME prompt (a /batch, a retry) are told apart by their own session
    id: the id is part of the key, the content alone is not enough."""
    for cli in ("opencode", "codex", "claude"):
        want, best = expected_for(None)
        check_cell("%s / auto (same prompt, own ids)" % cli,
                   run_cell(world, cli, "auto", same_task=True), want, best)


def test_subagents_of_one_claude_code_session_spread(world):
    """Claude Code Task sub-agents share ONE session id; the id alone must not merge them."""
    want, best = expected_for(None)
    check_cell("claude-subagents / auto (one session id)",
               run_cell(world, "claude-subagents", "auto", turns=2), want, best, turns=2)


def test_a_long_preamble_before_the_question_does_not_merge_sessions(world):
    """A CLI with no session id whose first user message is a long preamble, then the question."""
    want, best = expected_for(None)
    check_cell("bare-wrapped / auto", run_cell(world, "bare-wrapped", "auto"), want, best)


def test_a_session_keeps_its_key_when_the_history_grows():
    """Pin coherence needs the key to survive the tool rounds of ONE conversation."""
    for cli in ("opencode", "kimi-like", "codex", "claude"):
        assert _keys(cli, turn=1) == _keys(cli, turn=2), cli


def test_the_hub_agent_session_still_wins():
    """A Build / Multi worker session (`/build/<sid>`) is keyed by its own id, exactly as before."""
    with A.app.test_request_context("/v1/chat/completions",
                                    environ_base={"flh.build_session": "w-7"}):
        assert A._agent_pin_key([{"role": "user", "content": "x" * 40}]) == "w-7"


def test_sub_agents_started_from_one_template_still_spread(world):
    """A lead that pastes the same long preamble into every Task sub-agent: the first 2000
    characters of every instruction are identical, only the tail differs."""
    want, best = expected_for(None)
    check_cell("claude-subagents-templated / auto",
               run_cell(world, "claude-subagents-templated", "auto", turns=2), want, best, turns=2)


def test_a_huge_claude_md_cannot_hide_the_question():
    """The owner's real CLAUDE.md block is ~500K characters; the old key read the first 4000 of it."""
    big = "<system-reminder>\n" + "Project rule: keep diffs small.\n" * 16000 + "</system-reminder>"
    assert len(big) > 500_000
    assert len(set(_keys("claude-subagents", claude_md=big))) == N
    assert len(set(_keys("claude", claude_md=big))) == N


# --- the key across a CLI's own compaction -------------------------------------------------------

def _key_of(cli, i, mutate=None, **kw):
    path, req = build_request(cli, i, "auto", **kw)
    body = req["json"]
    if mutate:
        mutate(body)
    with A.app.test_request_context(path, json=body, headers=req["headers"]):
        if cli == "codex":
            msgs = A._responses_to_chat(body)
        elif cli.startswith("claude"):
            msgs = A._anthropic_to_openai_messages(body)
        else:
            msgs = body["messages"]
        msgs, _n = A._normalize_openai_messages(msgs)
        A._ctx_begin(body, msgs, 1000, signal=False)
        return A._agent_pin_key(msgs)


def _compact(cli):
    """What a CLI sends after it replaced its history by a summary: the first real instruction
    is now the summary, the tool rounds are gone; session ids and headers are unchanged."""
    summary = "SUMMARY of the work so far: the billing module is half done. TASK-0 continues."

    def mutate(body):
        if cli == "codex":
            body["input"][2]["content"][0]["text"] = summary
            del body["input"][3:]
        elif cli.startswith("claude"):
            body["messages"][0]["content"][-1]["text"] = summary
        else:
            body["messages"][1]["content"] = summary
    return mutate


@pytest.mark.parametrize("cli", ("opencode", "codex"))
def test_an_agent_with_its_own_session_id_keeps_its_key_through_compaction(cli):
    """A key that moves mid-job brings back model switching: the id must not depend on the history."""
    assert _key_of(cli, 3, turn=2) == _key_of(cli, 3, _compact(cli))


@pytest.mark.parametrize("cli", ("kimi-like", "claude-subagents"))
def test_the_two_shapes_that_need_content_re_pick_once_at_compaction(cli):
    """KNOWN LIMIT, pinned so it stays a decision and not a surprise. A CLI with no session id has
    nothing but its first instruction to be recognised by, and the sub-agents of one Claude Code
    session share an id and are told apart by it: when the CLI replaces that first instruction by a
    summary the agent is re-picked ONCE (and then keeps the new key for as long as the summary
    stays first). Not per-turn switching: the key is still stable while the history grows."""
    before = _key_of(cli, 3, turn=1)
    assert before == _key_of(cli, 3, turn=2)
    after = _key_of(cli, 3, _compact(cli), turn=2)
    assert after != before
    assert after == _key_of(cli, 3, _compact(cli), turn=1)


# --- what must NOT change -----------------------------------------------------------------------

# --- tool-free side requests of an agent (a title, a CLI's own compaction call, a plain question) --

def _pin_key_of_session(cli, i):
    return "w-%d" % i if cli == "worker" else _key_of(cli, i)


def run_with_side(world, cli, order, turns=2):
    """Each of the N sessions sends tool-free requests with ITS OWN session id before and/or after
    its agentic turns. `worker` = a hub Build/Multi worker (`/build/<sid>`), the owner's main use."""
    _reset_state()
    world.served.clear()
    world.side.clear()
    worker = cli == "worker"
    base = "opencode" if worker else cli

    def post(i, turn, tools):
        path, kw = build_request(base, i, "auto", turn if tools else 1, tools=tools)
        if worker:
            kw["environ_overrides"] = {"flh.build_session": "w-%d" % i}
        r = A.app.test_client().post(path, **kw)
        r.get_data()
        assert r.status_code == 200, (cli, i, turn, tools, r.status_code, r.get_data()[:200])

    for turn in range(1, turns + 1):
        for i in range(N):
            if order in ("before", "both"):
                post(i, turn, False)
            post(i, turn, True)
            if order in ("after", "both"):
                post(i, turn, False)
    return world.by_session()


@pytest.mark.parametrize("order", ("before", "after", "both"))
@pytest.mark.parametrize("cli", ("opencode", "codex", "claude", "worker"))
def test_tool_free_side_requests_leave_an_agents_pin_alone(world, cli, order):
    """A tool-free request carrying the agent's own session id must not seed, flip or drop its
    pin: it would come from the always-best chat path (no spread), `_session_pin_set(...,
    agentic=False)` overwrites the row, and the agent would lose its hold on the model."""
    got = run_with_side(world, cli, order)
    want, best = expected_for(None)
    check_cell("%s side requests %s" % (cli, order), got, want, best, turns=2)
    assert world.side, "the side requests were never routed -- the cell proves nothing"
    for i in range(N):
        row = A._session_pins.get(_pin_key_of_session(cli, i))
        assert row is not None, (cli, order, i, "the agent has no pin")
        assert row[3] is True, (cli, order, i, "its pin was flipped to non-agentic")
        assert _ident(row[1]) == _ident(got[i][0][1]), (cli, order, i, "its pin moved", row[1], got[i])


def test_a_conversation_too_short_to_tell_apart_gets_no_key_inside_a_request_either():
    """The old `_session_key` returned None under 16 characters of basis. A CLI with no session id
    that opens with "hi" must not now be pinned alongside every other "hi"."""
    short = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
    body = {"messages": short}
    with A.app.test_request_context("/v1/chat/completions", json=body):
        A._ctx_begin(body, short, 10, signal=False)
        assert A._session_key(short) is None
        assert A._agent_pin_key(short) is None
    # with enough to go on it is keyed -- and a session id is a basis whatever the content
    longer = [{"role": "system", "content": "s" * 40}, {"role": "user", "content": "hi"}]
    with A.app.test_request_context("/v1/chat/completions", json={"messages": longer}):
        A._ctx_begin({"messages": longer}, longer, 10, signal=False)
        assert A._agent_pin_key(longer) is not None
    with A.app.test_request_context("/v1/chat/completions", json=body,
                                    headers={"X-Session-Id": "ses_x"}):
        A._ctx_begin(body, short, 10, signal=False)
        assert A._agent_pin_key(short) is not None


def test_a_helper_call_inside_a_request_never_inherits_the_conversations_pin():
    """The recap / fact-extraction / intent / enhancement helpers route from inside a request with
    their own messages: they keep the content key, not the conversation's pin."""
    path, req = build_request("codex", 0, "auto")
    body = req["json"]
    with A.app.test_request_context(path, json=body, headers=req["headers"]):
        msgs, _n = A._normalize_openai_messages(A._responses_to_chat(body))
        A._ctx_begin(body, msgs, 1000, signal=False)
        pin = A._agent_pin_key(msgs)
        helper = [{"role": "system", "content": "Summarise the conversation in 5 lines. " * 3},
                  {"role": "user", "content": "USER:\nfix the billing module\n\nAGENT:\ndone"}]
        assert A._agent_pin_key(helper) == A._session_key(helper) != pin
        assert A._agent_pin_key(msgs) == pin                      # and the request itself still gets it
        assert A._agent_pin_key(list(msgs)) == pin                # also a copy of the same messages


def test_nothing_to_key_on_gives_no_key_as_before():
    assert A._agent_pin_key([]) is None
    assert A._agent_pin_key([{"role": "user", "content": "hi"}]) is None
    with A.app.test_request_context("/v1/chat/completions", json={"messages": []}):
        A._ctx_begin({"messages": []}, [], 10, signal=False)
        assert A._agent_pin_key([]) is None


def test_outside_a_request_the_old_key_is_used():
    msgs = [{"role": "system", "content": "s" * 30}, {"role": "user", "content": "u" * 30}]
    assert A._agent_pin_key(msgs) == A._session_key(msgs)


def test_the_flag_off_restores_the_old_key_for_everyone(monkeypatch):
    real = A.config.get_flag
    monkeypatch.setattr(A.config, "get_flag",
                        lambda name, default=False: False if name == "agent_pin_key"
                        else real(name, default))
    for cli in ("codex", "claude"):
        keys = _keys(cli)
        assert len(set(keys)) == 1, (cli, "flag off must be the old, colliding key")
    monkeypatch.undo()
    assert len(set(_keys("codex"))) == N


def test_short_instructions_hash_exactly_as_the_recap_key_always_did():
    """`fallback_conversation_key(tail=)` must not move a recap key stored by an earlier hub."""
    msgs = [{"role": "system", "content": "s" * 300}, {"role": "user", "content": "short question"}]
    assert ctxwin.fallback_conversation_key(msgs) == ctxwin.fallback_conversation_key(msgs, tail=1000)
    long_a = [{"role": "system", "content": "s"}, {"role": "user", "content": "p" * 2500 + "A"}]
    long_b = [{"role": "system", "content": "s"}, {"role": "user", "content": "p" * 2500 + "B"}]
    assert ctxwin.fallback_conversation_key(long_a) == ctxwin.fallback_conversation_key(long_b)
    assert (ctxwin.fallback_conversation_key(long_a, tail=1000)
            != ctxwin.fallback_conversation_key(long_b, tail=1000))


def test_pin_key_names_the_agent_not_the_cli():
    class H(dict):
        def get(self, k, d=None):
            return super().get(k, d)
    msgs = [{"role": "user", "content": "do the thing, please " * 5}]
    # one id per agent: the id alone, whatever the first instruction says
    other = [{"role": "user", "content": "something else entirely " * 5}]
    assert (ctxwin.pin_key("hdr:ses_1", H({"X-Session-Id": "ses_1"}), msgs)
            == ctxwin.pin_key("hdr:ses_1", H({"X-Session-Id": "ses_1"}), other))
    assert ctxwin.pin_key("pck:t1", None, msgs) == ctxwin.pin_key("pck:t1", None, other)
    # an id other agents share: the agent's own first instruction is part of the key
    cc = H({"X-Claude-Code-Session-Id": "s1"})
    assert ctxwin.pin_key("hdr:s1", cc, msgs) != ctxwin.pin_key("hdr:s1", cc, other)
    assert ctxwin.pin_key("meta:s1", None, msgs) != ctxwin.pin_key("meta:s1", None, other)
    assert ctxwin.pin_key(None, cc, msgs) is None and ctxwin.pin_key("", cc, msgs) is None
    assert len(ctxwin.pin_key("pck:t1")) == 20


# --------------------------------------------------------------------------- #
# 6. Multi: the helpers of ONE run (Build page, and the CLI Multi tier, both reach the router as
#    `/build/<sid>` sessions) get different strong models through the real router
# --------------------------------------------------------------------------- #

def test_multi_helpers_get_distinct_models_through_the_router(world, monkeypatch):
    import swarm_windows as SW
    run = SW._Run("goal", ".", "opencode", SW.clean_phases({"phases": [
        {"title": "p%d" % i, "task": "t"} for i in range(N)]}))
    for i, a in enumerate(run.agents):
        a.session_id = "w-%d" % i
    SW._remember(run)
    try:
        c = A.app.test_client()
        for i in range(N):
            path, kw = build_request("opencode", i, "auto")
            kw["environ_overrides"] = {"flh.build_session": "w-%d" % i}
            r = c.post(path, **kw)
            assert r.status_code == 200
        got = world.by_session()
    finally:
        with SW._LOCK:
            SW._RUNS.pop(run.id, None)
    want, best = expected_for(None)
    check_cell("multi helpers (6 workers of one run)", got, want, best)


# --------------------------------------------------------------------------- #
# 7. What is BY DESIGN, pinned so nobody "fixes" it blindly
# --------------------------------------------------------------------------- #

def test_a_tool_free_request_takes_the_single_best_model_by_design(world):
    """Plain chat has no sessions to spread: `route_always_best` (default on) = the strongest model
    every time. Sub-agents always carry tools, so this is not 'parallel agents using one model'."""
    served = set()
    for i in range(N):
        _reset_state()
        world.side.clear()
        body = {"model": "auto", "stream": False,
                "messages": [{"role": "system", "content": BARE_SYSTEM},
                             {"role": "user", "content": _task(i)}]}
        r = A.app.test_client().post("/v1/chat/completions", json=body)
        assert r.status_code == 200
        served |= {_ident(m) for _i, _p, m in world.side}
    assert served == {_ident("z-ai/glm-5.3")}


def test_a_degraded_fleet_collapses_onto_the_one_model_that_works(world, monkeypatch):
    """Everything but one model is out: six sessions legitimately share it (and still get served)."""
    only = "z-ai/glm-5.3"
    monkeypatch.setattr(A, "_is_model_skipped", lambda pid, m: m != only or pid != "nvidia")
    got = run_cell(world, "opencode", "auto")
    assert {_ident(m) for i in range(N) for _p, m in got[i]} == {_ident(only)}


def test_the_weak_tail_never_serves_while_the_band_has_room(world):
    got = run_cell(world, "opencode", "auto", turns=2)
    served = {m for i in range(N) for _p, m in got[i]}
    assert not served & {"mistral-large-3", "acme/orion-9", "command-r-plus"}
