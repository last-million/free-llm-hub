"""Conversations are compacted before they outgrow the models (2026-10-11).

ROOT CAUSE, from the hub's own codex rollouts under
~/.free-llm-hub/isolated-clis/codex/config/sessions (read-only):

  PROVEN -- rollout .../2026/10/07/rollout-...-01a11461... (the ~504K Build-page
  incident): on a SERVED turn codex auto-compacts from the usage the hub
  reports. One turn reported last_token_usage.input_tokens=610042 against a
  believed window of 258400 (the hub's resync value then, model_context_window
  honoured from config); codex emitted a `context_compacted` event and the next
  turn dropped to 13821 and the session carried on. No codex rollout on this
  machine EVER received a context_length_exceeded.

  INFERRED (the hours-long loop; those hub.log lines rotated away): on an
  OVERFLOW the hub answers with the native context error or a 503 -- a FAILED
  turn with NO usage report -- so codex's compaction never fires from it and it
  retries the same oversized turn.

THE FIX (flag context_grow_guard, default on): when a proven-auto-compact CLI
(codex) is about to get the native overflow reply, the hub instead returns a
SHORT SUCCESSFUL reply whose reported usage is at/over that CLI's own
auto-compaction threshold, so the CLI compacts on its next turn. Fires once per
conversation per _CG_FIRE_TTL; a repeat inside that window falls back to the
native error, so it can never loop. Off = byte-identical native reply.

All hermetic: a fake upstream and a fake fleet (reused from
test_oversized_conversation), no network.
"""
import json

import pytest

import app as A
import agentic_chat as AC

from test_oversized_conversation import (          # noqa: F401  (imported fixtures)
    world, codex_session, _codex_items, _codex_body, CODEX_UA, SID,
    H_RELAY, H_SMALL, MEASURED, BIG, _conv, _sse_events, _post_responses)

LIMIT = 300000      # the codex compact limit these tests pin via the fixture below


@pytest.fixture(autouse=True)
def _clean_guard(monkeypatch):
    A._CG_FIRED.clear()
    # A fixed codex auto-compact limit, so the reported figure is deterministic.
    monkeypatch.setattr(AC, "declared_compact_limit", lambda mid=None, cli=None: LIMIT)
    yield
    A._CG_FIRED.clear()


def _flag_off(monkeypatch):
    real = A.config.get_flag
    monkeypatch.setattr(A.config, "get_flag",
                        lambda k, d=None, r=real: False if k == "context_grow_guard"
                        else r(k, d))


def _responses_done_usage(r):
    done = [d for n, d in _sse_events(r.get_data(as_text=True)) if n == "response.completed"]
    assert done, r.get_data(as_text=True)[:400]
    return done[-1]["response"]["usage"]


def _responses_failed(r):
    return [d for n, d in _sse_events(r.get_data(as_text=True)) if n == "response.failed"]


# --------------------------------------------------------------------------- #
# 1. The proven cause: a codex overflow becomes a successful compaction nudge
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("stream", [False, True])
def test_codex_overflow_returns_a_successful_compaction_nudge(world, stream):
    world.fleet(MEASURED)
    world.set_chain([H_RELAY, H_SMALL])
    r = _post_responses(BIG, stream=stream, headers=CODEX_UA)
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    raw = r.get_data(as_text=True)
    assert "compacting it now" in raw and "send your message again" in raw
    if stream:
        assert not _responses_failed(r)             # NOT a context error
        usage = _responses_done_usage(r)
    else:
        usage = r.get_json()["usage"]
    assert usage["input_tokens"] >= LIMIT           # clears codex's auto-compact threshold
    assert world.calls() == 0                       # no model was called


def test_chat_and_anthropic_success_shapes_carry_the_usage(world):
    with A.app.test_request_context("/v1/chat/completions"):
        resp, status, hdrs = A._cg_success_reply("openai", False, 333333, "auto")
    assert status == 200
    body = resp.get_json()
    assert body["usage"]["prompt_tokens"] == 333333
    assert "compacting it now" in body["choices"][0]["message"]["content"]
    assert hdrs["X-Free-LLM-Hub-Model"] == "context-grow-guard"

    with A.app.test_request_context("/v1/messages"):
        resp, status, _h = A._cg_success_reply("anthropic", False, 333333, "auto")
    body = resp.get_json()
    assert status == 200
    assert body["usage"]["input_tokens"] == 333333
    assert "compacting it now" in json.dumps(body)


# --------------------------------------------------------------------------- #
# 2. The hop-exhaustion path is guarded too (not only the front door)
# --------------------------------------------------------------------------- #

def test_the_hop_exhaustion_path_also_guards(world, monkeypatch):
    world.fleet([("nvidia", "fake-big-262k", 262144)])
    world.set_chain([("nvidia", "fake-big-262k")])

    def overflow(pid, payload, stream):
        A._ctx_note_overflow(262144, pid=pid, model=payload.get("model"))
        raise A._ContextOverflow("would lose most of its history")

    monkeypatch.setattr(A, "_dispatch_chat", overflow)
    # 250K is under 1.15 x 262144, so the front door lets it through to the chain,
    # where every hop overflows -> _ctx_overflow_reply -> the guard.
    r = _post_responses(_conv(250000), stream=False, headers=CODEX_UA)
    assert r.status_code == 200
    assert "compacting it now" in r.get_data(as_text=True)
    assert r.get_json()["usage"]["input_tokens"] >= LIMIT


# --------------------------------------------------------------------------- #
# 3. Never loops: a second overflow in the window is the native error
# --------------------------------------------------------------------------- #

def test_a_second_overflow_falls_back_to_the_native_error(world):
    world.fleet(MEASURED)
    world.set_chain([H_RELAY])
    r1 = _post_responses(BIG, stream=False, headers=CODEX_UA)
    assert r1.status_code == 200                                   # guarded success
    # same conversation (ua:codex), within _CG_FIRE_TTL: the CLI should have
    # compacted; if it did not, we do NOT nudge again -> today's native error.
    r2 = _post_responses(BIG, stream=False, headers=CODEX_UA)
    assert r2.status_code == 400
    assert r2.get_json()["error"]["code"] == "context_length_exceeded"
    # once the fired mark ages out (TTL), a later overflow re-arms the guard.
    A._CG_FIRED.clear()
    r3 = _post_responses(BIG, stream=False, headers=CODEX_UA)
    assert r3.status_code == 200


# --------------------------------------------------------------------------- #
# 4. Flag off = byte-identical native reply; scope is codex only
# --------------------------------------------------------------------------- #

def test_flag_off_is_the_native_reply(world, monkeypatch):
    _flag_off(monkeypatch)
    world.fleet(MEASURED)
    world.set_chain([H_RELAY])
    r = _post_responses(BIG, stream=False, headers=CODEX_UA)
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "context_length_exceeded"
    assert A._CG_FIRED == {}                         # nothing was recorded


@pytest.mark.parametrize("ua", [None, "opencode/1.18.35", "claude-cli/2.1.293",
                                "python-requests/2.32"])
def test_a_non_codex_client_gets_the_native_reply(world, ua):
    world.fleet(MEASURED)
    world.set_chain([H_RELAY])
    headers = {"User-Agent": ua} if ua else None
    r = _post_responses(BIG, stream=False, headers=headers)
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "context_length_exceeded"


# --------------------------------------------------------------------------- #
# 5. The reported figure and the conversation key
# --------------------------------------------------------------------------- #

def test_report_floor_uses_the_codex_compact_limit(world):
    assert A._cg_report_floor("codex") == LIMIT + A._CG_REPORT_MARGIN
    assert A._cg_report_floor("something-else") == A._CG_REPORT_DEFAULT


def test_conv_key_prefers_the_build_session_then_the_cli(world):
    with A.app.test_request_context("/build/%s/v1/responses" % SID,
                                    environ_base={"flh.build_session": SID}):
        assert A._cg_conv_key("codex") == "sid:" + SID
    with A.app.test_request_context("/v1/responses", headers=CODEX_UA):
        assert A._cg_conv_key("codex") == "ua:codex"


# --------------------------------------------------------------------------- #
# 6. The live shape: a codex /build session, the compound id `coding-max`
# --------------------------------------------------------------------------- #

def test_a_codex_build_session_is_guarded_end_to_end(codex_session):
    world = codex_session
    world.fleet([("nvidia", "fake-glm-250k", 250000), ("kilocode", "fake-qwen-262k", 262144),
                 H_SMALL + (65536,), H_RELAY + (None,)])
    world.set_chain([H_RELAY, H_SMALL])
    # the oversized bulk is the latest message, which clearing never touches, so
    # the conversation is genuinely too big for any model even after clearing.
    items = _codex_items(0, tail="x " * 1_000_000)
    r = A.app.test_client().post("/build/%s/v1/responses" % SID,
                                 json=_codex_body(items, stream=True), headers=CODEX_UA)
    assert r.status_code == 200
    assert not _responses_failed(r)
    assert _responses_done_usage(r)["input_tokens"] >= LIMIT
    assert "send your message again" in r.get_data(as_text=True)
    assert world.calls() == 0
