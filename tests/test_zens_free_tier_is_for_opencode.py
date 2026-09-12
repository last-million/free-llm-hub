r"""OpenCode Zen's free tier answers OpenCode, and the hub now knows it.

MEASURED 2026-09-12, chasing why two swarm workers took 400 seconds to write a
two-line file. Every one of their tool turns went:

    opencode-zen/mimo-v2.5-free          400 MissingSessionID
                                         "OpenCode's free tier can only be
                                          used in OpenCode"
    opencode-zen/deepseek-v4-flash-free  400 "Model is unavailable"
    groq/qwen3.8-27b                     RequestException
    -> 503 to the CLI, which retried, and drew the same three again.

The gate is a header. OpenCode sends `x-opencode-session` to providers named
"opencode..." and the same id as `X-Session-Id` to every other provider, this
hub included. A request FROM OpenCode therefore carries what Zen asks for:
forward it. A request from anything else does not, and nothing is invented
for it: the hop is skipped instead of being spent on a 400.

And "Model is unavailable" is a missing model, which "not available" already
was.
"""
import pytest

import app as A


APP = open("app.py", encoding="utf-8").read()


def _ctx(ua, headers=None):
    h = {"User-Agent": ua}
    h.update(headers or {})
    return A.app.test_request_context("/v1/chat/completions", headers=h)


# --------------------------------------------------------------------------- #
# Whose request this is
# --------------------------------------------------------------------------- #

def test_opencodes_own_session_id_is_read():
    with _ctx("opencode/1.2.3", {"X-Session-Id": "ses_abc"}):
        assert A._opencode_session_id() == "ses_abc"


def test_the_affinity_spelling_counts_too():
    with _ctx("opencode/1.2.3", {"x-session-affinity": "ses_aff"}):
        assert A._opencode_session_id() == "ses_aff"


def test_another_clients_session_header_is_not_an_opencode_session():
    """Forwarding codex's or a script's id as an OpenCode session would be
    inventing what the provider asks for."""
    with _ctx("codex_cli_rs/0.9", {"X-Session-Id": "ses_codex"}):
        assert A._opencode_session_id() is None
    with _ctx("python-requests/2.32", {"X-Session-Id": "x"}):
        assert A._opencode_session_id() is None


def test_no_request_at_all_is_no_session():
    assert A._opencode_session_id() is None


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #

def test_zens_free_tier_is_skipped_for_everyone_else():
    with _ctx("codex_cli_rs/0.9"):
        assert A._zen_client_only("opencode-zen", "mimo-v2.5-free") is True


def test_and_offered_to_opencode():
    with _ctx("opencode/1.2.3", {"X-Session-Id": "ses_abc"}):
        assert A._zen_client_only("opencode-zen", "mimo-v2.5-free") is False


def test_only_the_free_tier_is_gated():
    with _ctx("codex_cli_rs/0.9"):
        assert A._zen_client_only("opencode-zen", "some-paid-model") is False


def test_other_providers_are_not_touched():
    with _ctx("codex_cli_rs/0.9"):
        assert A._zen_client_only("groq", "qwen/qwen3.8-27b-free") is False


def test_routing_sees_it_at_the_one_seam(monkeypatch):
    """_is_model_dead is what every candidate pool and _build_chain consult."""
    monkeypatch.setattr(A, "_is_model_blocked_by_user", lambda p, m: False)
    monkeypatch.setattr(A, "_is_model_dead_upstream", lambda p, m: False)
    with _ctx("codex_cli_rs/0.9"):
        assert A._is_model_dead("opencode-zen", "mimo-v2.5-free") is True
    with _ctx("opencode/1.2.3", {"X-Session-Id": "ses_abc"}):
        assert A._is_model_dead("opencode-zen", "mimo-v2.5-free") is False


# --------------------------------------------------------------------------- #
# The header goes out
# --------------------------------------------------------------------------- #

def test_the_session_is_forwarded_under_zens_name():
    with _ctx("opencode/1.2.3", {"X-Session-Id": "ses_abc"}):
        assert A._zen_headers("opencode-zen") == {"x-opencode-session": "ses_abc"}


def test_nothing_is_sent_when_there_is_nothing_to_forward():
    with _ctx("codex_cli_rs/0.9"):
        assert A._zen_headers("opencode-zen") == {}
    with _ctx("opencode/1.2.3", {"X-Session-Id": "ses_abc"}):
        assert A._zen_headers("groq") == {}


def test_both_outbound_posts_carry_it():
    body = APP[APP.index("def _upstream_chat("):]
    body = body[:body.index("\ndef ", 10)]
    assert body.count("**_zen_headers(pid)") == 2, "the first try and the context re-fit"


# --------------------------------------------------------------------------- #
# "unavailable" is missing
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text", [
    '{"error":{"type":"server_error","message":"Error from provider (Console): '
    'Upstream request failed: Model is unavailable."}}',
    "Model is currently unavailable",
    "model unavailable",
    "The model gpt-x is not available",
])
def test_an_unavailable_model_is_a_missing_one(text):
    assert A._MISSING_MODEL_RE.search(text)


def test_a_plain_bad_request_is_still_not(monkeypatch):
    assert not A._MISSING_MODEL_RE.search("invalid_request_error: messages[0].content is empty")


# --------------------------------------------------------------------------- #
# The Settings table says why
# --------------------------------------------------------------------------- #

def test_the_table_has_a_word_for_it():
    body = APP[APP.index("def api_tracking("):]
    body = body[:body.index("\n@app.route")]
    assert '"opencode-only" if _zen_client_only(pid, m)' in body
    assert ".tk-opencode-only{" in open("templates/index.html", encoding="utf-8").read()
