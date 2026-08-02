"""codex's auth failure showed as a raw, truncated stderr dump instead of a
clean message like claude's.

"opencode work in build agent chat but not codex and claude code" -- claude
was already correct (a prior fix); codex's isolated copy is ALSO simply not
signed in yet, same as claude, but the ERROR reporting for it was broken.

MEASURED against an unauthenticated isolated codex: the authoritative failure
arrives as one clean JSON line --

    {"type":"turn.failed","error":{"message":"unexpected status 401
     Unauthorized: Missing bearer or basic authentication in header, ..."}}

-- which nothing read. The parser fell through to raw stderr, which repeats
"HTTP error: 401 Unauthorized" once per reconnect attempt (five of them)
behind boilerplate, and got truncated to a fixed display length BEFORE
completing the word "401" -- cutting it to "HTTP error: 4". That is exactly
the substring the auth-error check looks for, so the failure was shown as a
raw, ugly, mid-word-truncated blob instead of a clean 403 with the isolated
copy's login command.
"""
import agentic_chat as ac

_TURN_FAILED = (
    '{"type":"turn.failed","error":{"message":"unexpected status 401 '
    'Unauthorized: Missing bearer or basic authentication in header, '
    'url: https://api.openai.com/v1/responses"}}'
)


def test_turn_failed_is_read_by_the_streaming_parser():
    events = ac._codex_stream_events(_TURN_FAILED)
    errors = [e for e in events if "_final_error" in e]
    assert errors and errors[0]["_final_error"].startswith("unexpected status 401")
    assert not any("_final" in e for e in events), "a failure must not become a reply"


def test_turn_failed_is_read_by_the_non_streaming_parser():
    text, native_id, detail = ac._parse_codex_json(_TURN_FAILED, "", 1)
    assert text is None
    assert detail.startswith("unexpected status 401")


def test_the_clean_message_is_recognised_as_an_auth_failure():
    assert ac._looks_like_auth_error("unexpected status 401 Unauthorized: Missing bearer")


def test_turn_failed_wins_over_the_noisier_item_error_notice():
    """Both can appear in the same transcript (the WebSocket fallback notice,
    then the decisive turn.failed) -- the clean, single-sentence one is what
    becomes the reported failure, not whichever arrived first."""
    lines = (
        '{"type":"item.completed","item":{"type":"error","message":'
        '"Falling back from WebSockets to HTTPS transport. unexpected status '
        '401 Unauthorized"}}\n' + _TURN_FAILED
    )
    text, native_id, detail = ac._parse_codex_json(lines, "", 1)
    assert text is None
    assert detail.startswith("unexpected status 401")


def test_a_successful_turn_is_unaffected():
    ok = '{"type":"item.completed","item":{"type":"agent_message","text":"Hello!"}}'
    events = ac._codex_stream_events(ok)
    assert {"event": "message", "text": "Hello!"} in events
    assert {"_final": "Hello!"} in events
    text, _, detail = ac._parse_codex_json(ok, "", 0)
    assert text == "Hello!" and detail is None


def test_the_repeated_stderr_truncation_trap_is_what_broke_it():
    """Pin the actual mechanism, not just the symptom: five identical retry
    lines, truncated to a fixed length, cut "401" apart before it completed --
    the same shape measured live."""
    noisy_stderr = "Reading additional input from stdin...\n" + "\n".join(
        "ERROR failed to connect to websocket: HTTP error: 401 Unauthorized, "
        "url: wss://api.openai.com/v1/responses" for _ in range(5)
    )
    truncated = ac._sanitize(noisy_stderr.strip(), 400)
    assert not truncated.rstrip().endswith("401"), (
        "if this stops truncating mid-word the repro no longer matches what "
        "was actually observed -- worth knowing, not a reason to weaken the fix")
