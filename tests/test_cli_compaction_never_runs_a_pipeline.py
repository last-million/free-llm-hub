"""A CLI's own compaction request is ONE summary, never a pipeline job.

MEASURED 2026-10-03 (hub activity, terminal OpenCode on `coding-multi`): every
compaction ran the whole Multi pipeline -- plan, phases "Extract anchor state" /
"Assemble anchored summary" / "Render anchored Markdown summary", review,
synthesis -- 300-600 s per summary, so OpenCode sat on "compaction". Its prompt
(packages/core/src/session/compaction.ts buildPrompt) matched none of the
spellings ctxwin knew.
"""
import ctxwin
import app as A


# opencode buildPrompt, first compaction (no prior summary).
OPENCODE_NEW = (
    "<conversation>\nuser: build the parser\nassistant: done, tests pass\n</conversation>\n\n"
    "Create a new anchored summary from the conversation history in the <conversation> "
    "tags above so another coding agent can continue the work.\n\n"
    "Output exactly the Markdown structure shown inside <template> and keep the section "
    "order unchanged. Do not include the <template> tags in your response.\n<template>\n"
    "## Objective\n...\n## Next Move\n1. [immediate concrete action, or \"(none)\"]\n"
    "</template>")

# opencode buildPrompt, a later compaction (prior summary carried).
OPENCODE_UPDATE = (
    "<conversation>\nuser: now the renderer\n</conversation>\n\n"
    "Here is the summary of the conversation before the <conversation> above:\n\n"
    "<prior-summary>\n## Objective\nParser.\n</prior-summary>\n\n"
    "The <prior-summary> summarizes everything that happened before the <conversation>. "
    "Construct a new summary that combines both. The <prior-summary> is discarded after "
    "this: anything you do not carry into the new summary is lost.\n"
    "Output exactly the Markdown structure shown inside <template> ...")

KIMI = ("---\n\nThe above is a list of messages in an agent conversation. You are now "
        "given a task to compact this conversation context according to specific "
        "priorities and rules.")

GEMINI_SYSTEM = ("You are a specialized system component responsible for distilling chat "
                 "history into a structured XML <state_snapshot>.")


def _user(text):
    return [{"role": "user", "content": text}]


def test_opencode_first_compaction_is_recognised():
    assert ctxwin.is_compaction_request(_user(OPENCODE_NEW))


def test_opencode_update_compaction_is_recognised():
    assert ctxwin.is_compaction_request(_user(OPENCODE_UPDATE))


def test_kimi_and_gemini_compactions_are_recognised():
    assert ctxwin.is_compaction_request(_user(KIMI))
    msgs = [{"role": "system", "content": GEMINI_SYSTEM},
            {"role": "user", "content": "<history>...</history>"}]
    assert ctxwin.is_compaction_request(msgs)


def test_ordinary_requests_about_summaries_are_not_compactions():
    for text in ("Write a summary of the README for the release notes.",
                 "Add an anchored heading to the docs page",
                 "Combine both lists into one and sort them."):
        assert not ctxwin.is_compaction_request(_user(text)), text


def test_a_compaction_always_takes_the_one_model_fast_path(monkeypatch):
    # Even a big tool-carrying conversation, even with the fast path flag off.
    monkeypatch.setattr(A.config, "get_flag",
                        lambda name, default=None: False if name == "swarm_fast_path"
                        else default)
    history = []
    for i in range(40):
        history += [{"role": "user", "content": "step %d " % i + "x" * 3000},
                    {"role": "assistant", "content": "ok " + "y" * 3000}]
    msgs = history + _user(OPENCODE_UPDATE)
    tools = [{"type": "function", "function": {"name": "read", "parameters": {}}}]
    assert A._swarm_fast_path({"model": "multi"}, msgs, tools=tools)


def test_a_multi_compaction_is_served_by_one_strong_model(monkeypatch):
    seen = {}

    def fake_chat(body):
        seen["model"] = body.get("model")
        return A.jsonify({"choices": [{"message": {"role": "assistant",
                                                   "content": "## Objective\n..."}}]})

    monkeypatch.setattr(A, "_chat_completions_uncached", fake_chat)
    with A.app.test_request_context("/v1/chat/completions", method="POST"):
        resp = A._swarm_completion({"model": "multi", "messages": _user(OPENCODE_NEW)})
    assert seen["model"] == "best"
    assert resp.headers.get("X-Free-LLM-Hub-Pipeline") == A._PIPELINE_FAST_NOTE
