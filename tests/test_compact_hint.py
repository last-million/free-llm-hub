"""The "too long for every model" refusal tells the client how to compact.

A conversation that overflows every window is refused with the protocol's
NATIVE context error (shape + code unchanged, so the CLI still treats it as a
context error and compacts), but the human-readable message is made actionable:
it names the client's own built-in compaction command when the hub verified one
(codex / claude / opencode all print "/compact", verified READ-ONLY from each
installed binary's own strings), else a generic way out.

All hermetic: the message is built from constants and the request's User-Agent;
no network, no routing, no upstream. Pins:

  1. message per CLI (UA-identified): a verified CLI is told its "/compact"
     command; an unverified / unknown one gets the generic sentence.
  2. the native error shape + code is unchanged on every protocol.
  3. flag off (context_compact_hint) -> the message is byte-identical to before.
  4. no usage/token number is altered: the hint only appends text to the
     message; every structured field (incl. the size numbers) is untouched, and
     no body carries a usage field.
"""
import json

import app as A
import ctxwin

ORIG, WINDOW = 504077, 262144
NATIVE_OPENAI = ("This model's maximum context length is 262144 tokens. However, "
                 "your messages resulted in 504077 tokens. Please reduce the "
                 "length of the messages.")
NATIVE_ANTHROPIC_PREFIX = "prompt is too long: 504077 tokens > 262144 maximum"
EXPECTED_SIZES = " (~504,077 tokens; the largest holds 262,144)"


def _force_flag(monkeypatch, value):
    """Make config.get_flag('context_compact_hint') return `value`; every other
    flag read keeps its caller's default (none is read on this path)."""
    monkeypatch.setattr(A.config, "get_flag",
                        lambda name, default=False: value if name == "context_compact_hint"
                        else default)


def _openai_message(ua):
    with A.app.test_request_context("/v1/chat/completions",
                                    headers={"User-Agent": ua}):
        resp, status, _hdrs = A._native_overflow_reply(
            "openai", False, ORIG, WINDOW, "auto", {})
        assert status == 400
        return resp.get_json()["error"]["message"]


# --------------------------------------------------------------------------- #
# 1. message per CLI
# --------------------------------------------------------------------------- #

def test_verified_clis_are_told_their_compact_command(monkeypatch):
    _force_flag(monkeypatch, True)
    for ua in ("codex_cli/1.0", "claude-cli/2.1.293", "opencode/1.18.35"):
        msg = _openai_message(ua)
        assert msg.startswith(NATIVE_OPENAI), ua
        assert msg.endswith(
            " This conversation is too long for every available model"
            + EXPECTED_SIZES
            + ". Type /compact to shrink it, or start a new conversation."), ua
        assert "/compact" in msg, ua
    # The map that drives it is exactly the three verified CLIs.
    assert A._CH_COMPACT_CMD == {"codex": "/compact", "claude": "/compact",
                                 "opencode": "/compact"}


def test_unverified_or_unknown_clients_get_the_generic_sentence(monkeypatch):
    _force_flag(monkeypatch, True)
    for ua in ("qwen-code/1.0",            # steered CLI, command NOT verified
               "aider/0.1", "OpenAI/Python 1.2", "python-requests/2.32",
               "", "Mozilla/5.0"):
        msg = _openai_message(ua)
        assert msg.startswith(NATIVE_OPENAI), ua
        assert msg.endswith(
            " This conversation is too long for every available model"
            + EXPECTED_SIZES
            + ". Start a new conversation or shorten the history."), ua
        assert "/compact" not in msg, ua


def test_sizes_fall_back_when_the_window_is_unknown(monkeypatch):
    _force_flag(monkeypatch, True)
    with A.app.test_request_context("/v1/chat/completions",
                                    headers={"User-Agent": "codex_cli/1.0"}):
        # window 0: no "largest holds" clause, but still actionable.
        msg = A._native_overflow_reply("openai", False, ORIG, 0, "auto", {}
                                       )[0].get_json()["error"]["message"]
    assert "(~504,077 tokens)." in msg
    assert "the largest holds" not in msg
    assert "Type /compact to shrink it" in msg


# --------------------------------------------------------------------------- #
# 2. native error shape + code unchanged on every protocol
# --------------------------------------------------------------------------- #

def test_openai_shape_and_code_unchanged(monkeypatch):
    _force_flag(monkeypatch, True)
    with A.app.test_request_context("/v1/chat/completions",
                                    headers={"User-Agent": "codex_cli/1.0"}):
        body = A._native_overflow_reply("openai", False, ORIG, WINDOW, "auto", {}
                                        )[0].get_json()
    err = body["error"]
    assert err["type"] == "invalid_request_error"
    assert err["param"] == "messages"
    assert err["code"] == "context_length_exceeded"
    assert "usage" not in json.dumps(body)


def test_anthropic_shape_unchanged_and_prefix_preserved(monkeypatch):
    _force_flag(monkeypatch, True)
    with A.app.test_request_context("/v1/messages",
                                    headers={"User-Agent": "claude-cli/2.1"}):
        resp, status, _h = A._native_overflow_reply(
            "anthropic", False, ORIG, WINDOW, "auto", {})
    body = resp.get_json()
    assert status == 400
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"
    # Claude Code keys reactive compaction on this exact prefix.
    assert body["error"]["message"].startswith(NATIVE_ANTHROPIC_PREFIX)
    assert body["error"]["message"].endswith(
        ". Type /compact to shrink it, or start a new conversation.")


def test_responses_stream_failed_event_keeps_code_and_carries_hint(monkeypatch):
    _force_flag(monkeypatch, True)
    with A.app.test_request_context("/v1/responses",
                                    headers={"User-Agent": "codex_cli/1.0"}):
        # The streamed Responses path returns a bare Response (no status tuple).
        resp = A._native_overflow_reply("responses", True, ORIG, WINDOW, "auto", {})
        raw = b"".join(resp.response).decode("utf-8")
    events = [json.loads(line[len("data: "):])
              for line in raw.splitlines() if line.startswith("data: ")]
    failed = [e for e in events if e.get("response", {}).get("status") == "failed"]
    assert failed, raw
    err = failed[0]["response"]["error"]
    assert err["code"] == "context_length_exceeded"      # codex still compacts
    assert "/compact" in err["message"]


# --------------------------------------------------------------------------- #
# 3. flag off -> byte-identical to the native message
# --------------------------------------------------------------------------- #

def test_flag_off_is_byte_identical_on_every_protocol(monkeypatch):
    _force_flag(monkeypatch, False)
    with A.app.test_request_context("/v1/chat/completions",
                                    headers={"User-Agent": "codex_cli/1.0"}):
        oa = A._native_overflow_reply("openai", False, ORIG, WINDOW, "auto", {}
                                      )[0].get_json()
        an = A._native_overflow_reply("anthropic", False, ORIG, WINDOW, "auto", {}
                                      )[0].get_json()
    assert oa == ctxwin.openai_overflow_body(ORIG, WINDOW)
    assert an == ctxwin.anthropic_overflow_body(ORIG, WINDOW)
    # And the "on" path really differs, so the test above is meaningful.
    _force_flag(monkeypatch, True)
    assert _openai_message("codex_cli/1.0") != oa["error"]["message"]


def test_ch_compact_hint_returns_empty_when_flag_off(monkeypatch):
    _force_flag(monkeypatch, False)
    with A.app.test_request_context("/v1/chat/completions",
                                    headers={"User-Agent": "codex_cli/1.0"}):
        assert A._ch_compact_hint(ORIG, WINDOW) == ("", None, None)


# --------------------------------------------------------------------------- #
# 4. no usage / token number is altered anywhere
# --------------------------------------------------------------------------- #

def test_hint_only_appends_text_numbers_are_untouched():
    """With a hint, every structured field (the size numbers included) is
    identical to the no-hint body; only the message string grows, by exactly
    the hint. Proven at the pure ctxwin layer, both protocols."""
    hint = " SOME ACTIONABLE SENTENCE."
    for build in (ctxwin.openai_overflow_body, ctxwin.anthropic_overflow_body):
        plain = build(ORIG, WINDOW)
        withh = build(ORIG, WINDOW, hint=hint)
        # Pull the messages out and blank them: everything else must match.
        pm = plain["error"]["message"]
        wm = withh["error"]["message"]
        assert wm == pm + hint                       # text-only append
        plain["error"]["message"] = withh["error"]["message"] = ""
        assert plain == withh                        # all other fields identical
        assert "usage" not in json.dumps(build(ORIG, WINDOW, hint=hint))


def test_responses_events_carry_no_usage_and_keep_the_code():
    created, failed = ctxwin.responses_overflow_events(ORIG, WINDOW, "auto",
                                                       hint=" HINT.")
    for ev in (created, failed):
        assert "usage" not in json.dumps(ev)
    assert failed["error"]["code"] == "context_length_exceeded"
    assert failed["error"]["message"].endswith(" HINT.")
    # No-hint events are byte-identical except the appended message text.
    c0, f0 = ctxwin.responses_overflow_events(ORIG, WINDOW, "auto")
    assert f0["error"]["message"] + " HINT." == failed["error"]["message"]
