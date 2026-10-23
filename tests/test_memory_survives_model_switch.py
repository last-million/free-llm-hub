"""Switching model or effort mid-conversation keeps the same conversation memory.

The rolling recap, the exact-facts block and the pipeline context are all keyed
by `ctxwin.conversation_key`. The owner's requirement (2026-10-23): going from
`coding-swarm` to `coding-max`, from Normal to Multi, or from one category to
another must never start a new memory. So the key may depend on the SESSION and
the conversation's content, never on the model/effort the request names.
"""
import pytest

import ctxwin
import app as A

# every spelling a CLI can send for "the same chat, different model or effort"
MODELS = ["auto", "best", "swarm", "multi", "coding", "coding-max", "coding-swarm",
          "coding-multi", "reasoning-max", "fast", "crew", "crew-code",
          "g4f/srv_x:poe/GLM-5.3-Flash-T", "nvidia/z-ai/glm-5.3"]

MESSAGES = [
    {"role": "system", "content": "You are a coding agent. Be careful."},
    {"role": "user", "content": "Build the zest contracts filter, then run it."},
    {"role": "assistant", "content": "Starting."},
    {"role": "user", "content": "go"},
]


def _keys(**kw):
    return {m: ctxwin.conversation_key(body=dict(kw.get("body", {}), model=m),
                                       headers=kw.get("headers"),
                                       agent_sid=kw.get("agent_sid"),
                                       messages=kw.get("messages"))
            for m in MODELS}


def test_a_cli_session_header_gives_one_key_for_every_model():
    for name in ctxwin.SESSION_HEADERS:
        keys = _keys(headers={name: "ses_e9602c99fffegePhb27sR8uQzm"}, messages=MESSAGES)
        assert len(set(keys.values())) == 1, (name, keys)


def test_codex_prompt_cache_key_gives_one_key_for_every_model():
    keys = _keys(body={"prompt_cache_key": "019a-codex-thread-1"}, messages=MESSAGES)
    assert len(set(keys.values())) == 1, keys


def test_claude_code_metadata_session_gives_one_key_for_every_model():
    meta = {"user_id": "user_abc_account__session_6f1d2c3e-aaaa-bbbb-cccc-1234567890ab"}
    keys = _keys(body={"metadata": meta}, messages=MESSAGES)
    assert len(set(keys.values())) == 1, keys


def test_the_hub_agent_session_gives_one_key_for_every_model():
    keys = _keys(agent_sid="47a25faa", messages=MESSAGES)
    assert len(set(keys.values())) == 1, keys


def test_a_client_with_no_session_id_is_keyed_by_its_content_not_its_model():
    keys = _keys(messages=MESSAGES)
    assert len(set(keys.values())) == 1, keys


def test_the_chat_grows_but_the_key_does_not():
    # The same chat a few turns later (more messages) still finds its memory.
    short = ctxwin.conversation_key(body={"model": "coding-swarm"}, messages=MESSAGES)
    longer = MESSAGES + [{"role": "assistant", "content": "Done step 1."},
                         {"role": "user", "content": "now step 2"}]
    assert ctxwin.conversation_key(body={"model": "coding-max"}, messages=longer) == short


def test_a_different_conversation_gets_a_different_key():
    other = [{"role": "system", "content": "You are a coding agent. Be careful."},
             {"role": "user", "content": "Write a landing page for a bakery."}]
    assert (ctxwin.conversation_key(body={"model": "coding-swarm"}, messages=MESSAGES)
            != ctxwin.conversation_key(body={"model": "coding-swarm"}, messages=other))


@pytest.mark.parametrize("bare,compound", [("max", "coding-max"), ("swarm", "coding-swarm"),
                                           ("multi", "coding-multi")])
def test_the_category_and_effort_split_only_rewrites_the_model(bare, compound):
    # `_apply_category_effort` rewrites body["model"] to the bare tier; whatever it
    # does, the key was computed from the session and the messages, not from it.
    cat, eff = A._split_category_effort(compound) or (None, None)
    assert cat == "coding" and eff == bare
