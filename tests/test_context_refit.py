"""Context-window handling: learn the real limit, re-fit, and remember it.

MEASURED 2026-07-31 from a real Codex "build me a store" session: 10 of 13
requests were answered by llm7/gemini-3.1-flash-LITE — the weakest model in the
fleet — because the strong first hop 400'd every single time. Root cause:
cloudflare/@cf/qwen/qwen3-30b-a3b-fp8 has a 32,768-token window, but
_PROVIDER_TPM told the router cloudflare could take 120,000, so every large
turn was built ~3x too big and rejected.

Three separate bugs, one per section below.
"""
import pytest

import app


# --------------------------------------------------------------------------- #
# 1. The error bodies must be recognised — including the 413 phrasing
# --------------------------------------------------------------------------- #

CF_400 = ('{"errors":[{"message":"AiError: {\\"error\\":{\\"message\\":\\"This '
          "model's maximum context length is 32768 tokens. However, you requested "
          '64 output tokens\\"}}"}]}')
CF_413 = ('{"errors":[{"message":"AiError: Ai: The estimated number of input and '
          'maximum output tokens (42532) exceeded this model context window limit '
          '(32768)."}]}')


class _R:
    def __init__(self, text, status=400):
        self.text = text
        self.status_code = status

    def close(self):
        pass


@pytest.mark.parametrize("body", [CF_400, CF_413])
def test_both_cloudflare_phrasings_are_recognised_as_context_errors(body):
    """The 413 wording ('context window limit') was NOT matched before, so the
    real window was never learned from it and the re-fit had nothing to use."""
    assert app._SOFT_400_CONTEXT_RE.search(body)


@pytest.mark.parametrize("body", [CF_400, CF_413])
def test_the_real_window_is_extracted_not_the_requested_size(body):
    """CF_413 contains BOTH 42532 (what was sent) and 32768 (the limit). Learning
    42532 would be worse than useless — it is larger than the real window."""
    app._MODEL_MAX_INPUT.pop(("t", "m"), None)
    app._learn_context_limit("t", "m", _R(body))
    assert app._MODEL_MAX_INPUT.get(("t", "m")) == 32768


def test_an_unrelated_400_teaches_nothing():
    app._MODEL_MAX_INPUT.pop(("t", "m2"), None)
    app._learn_context_limit("t", "m2", _R('{"error":"invalid api key"}'))
    assert ("t", "m2") not in app._MODEL_MAX_INPUT


# --------------------------------------------------------------------------- #
# 2. Re-fit: apply the freshly-learned limit to THIS request, not just the next
# --------------------------------------------------------------------------- #

def _convo(turns):
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(turns):
        msgs.append({"role": "user", "content": ("x" * 400) + str(i)})
        msgs.append({"role": "assistant", "content": "ok " + str(i)})
    return msgs


def test_an_oversized_conversation_is_recompacted_to_the_learned_window():
    app._MODEL_MAX_INPUT[("t", "big")] = 8000
    payload = {"model": "big", "max_tokens": 4000, "messages": _convo(200)}
    assert app._est_tokens(payload["messages"]) > 8000
    refit = app._refit_payload_to_learned_ctx("t", payload)
    assert refit is not None
    assert app._est_tokens(refit["messages"]) <= 8000
    assert len(refit["messages"]) < len(payload["messages"])


def test_the_output_budget_is_capped_too():
    """max_tokens counts against the same window on most providers, so leaving a
    4000-token reply budget on an 8000-token window re-triggers the error."""
    app._MODEL_MAX_INPUT[("t", "big")] = 8000
    refit = app._refit_payload_to_learned_ctx(
        "t", {"model": "big", "max_tokens": 4000, "messages": _convo(200)})
    assert refit["max_tokens"] < 4000


def test_the_system_prompt_survives_recompaction():
    app._MODEL_MAX_INPUT[("t", "big")] = 8000
    refit = app._refit_payload_to_learned_ctx(
        "t", {"model": "big", "max_tokens": 100, "messages": _convo(200)})
    assert refit["messages"][0]["role"] == "system"


def test_no_refit_when_it_already_fits():
    """Then the 400 was about something else and recompacting would only lose
    context for no reason."""
    app._MODEL_MAX_INPUT[("t", "big")] = 500000
    assert app._refit_payload_to_learned_ctx(
        "t", {"model": "big", "max_tokens": 100, "messages": _convo(5)}) is None


def test_refit_never_raises_on_a_junk_payload():
    for bad in ({}, {"messages": None}, {"messages": []}, {"messages": "nope"}):
        assert app._refit_payload_to_learned_ctx("t", bad) is None


# --------------------------------------------------------------------------- #
# 3. Persistence — a context window is a fixed fact, not a per-boot discovery
# --------------------------------------------------------------------------- #

def test_learned_windows_survive_a_restart_cycle():
    """They were in-memory only, so every restart forgot them and re-learned
    each by burning a real failed request on the best hop."""
    app._MODEL_MAX_INPUT[("cloudflare", "@cf/qwen/qwen3-30b-a3b-fp8")] = 32768
    blob = app._dead_state_dump()
    assert blob["model_max_input"]["cloudflare|@cf/qwen/qwen3-30b-a3b-fp8"] == 32768
    app._MODEL_MAX_INPUT.clear()
    app._dead_state_load(blob)
    assert app._MODEL_MAX_INPUT[("cloudflare", "@cf/qwen/qwen3-30b-a3b-fp8")] == 32768


def test_reload_keeps_the_SMALLER_limit():
    """Two sources disagreeing means the smaller one is the safe truth."""
    app._MODEL_MAX_INPUT[("p", "m")] = 16000
    app._dead_state_load({"model_max_input": {"p|m": 32768}})
    assert app._MODEL_MAX_INPUT[("p", "m")] == 16000
    app._dead_state_load({"model_max_input": {"p|m": 8000}})
    assert app._MODEL_MAX_INPUT[("p", "m")] == 8000


def test_garbage_in_the_saved_blob_is_ignored():
    before = dict(app._MODEL_MAX_INPUT)
    app._dead_state_load({"model_max_input": {"nopipe": 9999, "p|m3": "big",
                                              "p|m4": 12}})   # 12 is absurdly small
    assert ("p", "m3") not in app._MODEL_MAX_INPUT
    assert ("p", "m4") not in app._MODEL_MAX_INPUT
    app._MODEL_MAX_INPUT.clear()
    app._MODEL_MAX_INPUT.update(before)


# --------------------------------------------------------------------------- #
# 4. Proactive: read the window from the catalog, before anything is sent
# --------------------------------------------------------------------------- #

def test_context_windows_are_harvested_from_a_catalog():
    """Verified field names against live catalogs 2026-07-31: openrouter
    context_length, groq context_window, puter context."""
    app._MODEL_MAX_INPUT.clear()
    app._learn_ctx_from_catalog("p", {"data": [
        {"id": "a", "context_length": 1048576},
        {"id": "b", "context_window": 4000},
        {"id": "c", "context": 131072},
        {"id": "d", "top_provider": {"context_length": 65536}},
    ]})
    got = {k[1]: v for k, v in app._MODEL_MAX_INPUT.items()}
    assert got == {"a": 1048576, "b": 4000, "c": 131072, "d": 65536}


def test_output_caps_are_never_mistaken_for_the_input_window():
    """max_completion_tokens bounds the REPLY. Treating it as the input window
    would over-compact every conversation on that provider."""
    app._MODEL_MAX_INPUT.clear()
    app._learn_ctx_from_catalog("p", {"data": [
        {"id": "x", "max_completion_tokens": 50000, "max_output_length": 50000}]})
    assert ("p", "x") not in app._MODEL_MAX_INPUT


def test_a_real_rejection_outranks_an_optimistic_catalog_number():
    app._MODEL_MAX_INPUT.clear()
    app._MODEL_MAX_INPUT[("p", "m")] = 32768                 # learned from a 400
    app._learn_ctx_from_catalog("p", {"data": [{"id": "m", "context_length": 128000}]})
    assert app._MODEL_MAX_INPUT[("p", "m")] == 32768


@pytest.mark.parametrize("junk", [None, {}, [], {"data": "nope"},
                                  {"data": [None, 5, {"id": None}]}])
def test_catalog_harvest_never_raises(junk):
    app._learn_ctx_from_catalog("p", junk)


# --------------------------------------------------------------------------- #
# 5. A single oversized message — previously unrecoverable
# --------------------------------------------------------------------------- #

def test_one_giant_message_is_trimmed_instead_of_failing():
    """Compaction drops whole TURNS, so a single huge message (a pasted file, a
    big tool result) used to survive untouched and be rejected upstream, losing
    the hop. It is now trimmed head+tail."""
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "A" * 400000}]
    out, did = app._compact_to_budget(msgs, None, 8000)
    assert did is True
    assert app._est_tokens(out) <= 8000
    body = out[1]["content"]
    assert body.startswith("A") and body.endswith("A"), "head and tail must both survive"
    assert "characters omitted" in body, "the cut must be visible to the model"


def test_trimming_leaves_a_message_that_already_fits_alone():
    msgs = [{"role": "user", "content": "short"}]
    out, did = app._compact_to_budget(msgs, None, 8000)
    assert (out, did) == (msgs, False)


def test_the_refit_budget_is_deliberately_conservative():
    """_est_tokens is chars/4, tuned for prose; code runs ~2.2 chars/token, so
    the estimate can be ~1.8x optimistic. MEASURED: a payload estimated at
    27,780 tokens still overflowed a 32,768 window. Re-fitting to the full
    estimated window fails twice and loses the hop anyway."""
    app._MODEL_MAX_INPUT[("t", "m")] = 32768
    refit = app._refit_payload_to_learned_ctx(
        "t", {"model": "m", "max_tokens": 100, "messages": _convo(400)})
    assert refit is not None
    assert app._est_tokens(refit["messages"]) <= 32768 * 0.55


# --------------------------------------------------------------------------- #
# 6. Continuity — "make it better" must EDIT the project, not restart it
# --------------------------------------------------------------------------- #

def _project_convo(turns=120):
    """Build-a-project conversation: a brief, lots of work, then a follow-up."""
    msgs = [{"role": "system", "content": "You are a coding agent."},
            {"role": "user", "content": "Build me an online store called Solaris "
                                        "selling solar panels."}]
    for i in range(turns):
        msgs.append({"role": "assistant",
                     "content": "Created src/components/Product%d.tsx and updated "
                                "src/App.tsx. " % i + "x" * 900})
        msgs.append({"role": "user", "content": "ok continue " + str(i)})
    msgs.append({"role": "user", "content": "now make it better"})
    return msgs


def _text_of(messages):
    return " ".join(m["content"] for m in messages
                    if isinstance(m, dict) and isinstance(m.get("content"), str))


def test_the_original_brief_survives_compaction():
    """Keeping only the NEWEST turns loses the message that says what is being
    built — so a follow-up like "make it better" had nothing to refer to and the
    model started a fresh project."""
    out, did = app._compact_to_budget(_project_convo(), None, 8000)
    assert did is True
    assert "Solaris selling solar panels" in _text_of(out)


def test_the_notice_tells_the_model_to_edit_not_restart():
    out, _ = app._compact_to_budget(_project_convo(), None, 8000)
    assert "do not start a new project" in _text_of(out)


def test_files_from_dropped_turns_are_named():
    """A bare "earlier conversation was truncated" is not actionable; the file
    list is what tells the model the project already exists."""
    out, _ = app._compact_to_budget(_project_convo(), None, 8000)
    assert "src/App.tsx" in _text_of(out)


def test_the_latest_turn_is_still_kept():
    out, _ = app._compact_to_budget(_project_convo(), None, 8000)
    assert "now make it better" in _text_of(out)


def test_compaction_still_fits_the_budget_with_the_brief_pinned():
    out, _ = app._compact_to_budget(_project_convo(), None, 8000)
    assert app._est_tokens(out) <= 8000


def test_a_giant_opening_brief_is_capped_not_pinned_whole():
    """Otherwise pinning the brief would itself blow the window it protects."""
    msgs = [{"role": "user", "content": "BRIEF " + ("z" * 200000)}]
    msgs += [{"role": "user", "content": "turn %d" % i} for i in range(400)]
    out, did = app._compact_to_budget(msgs, None, 8000)
    assert did is True
    assert app._est_tokens(out) <= 8000
    assert "BRIEF" in _text_of(out)


@pytest.mark.parametrize("text,expected", [
    ("edited src/App.tsx and lib/cart.ts", ["src/App.tsx", "lib/cart.ts"]),
    ("see ./index.html", ["index.html"]),
    ("no files here, just prose about a store.", []),
    ("version 1.5 of the plan", []),          # not a path
])
def test_path_extraction_is_narrow(text, expected):
    assert app._mentioned_paths([{"role": "assistant", "content": text}]) == expected


# --------------------------------------------------------------------------- #
# 7. Summarised compaction — the recap must never cost the request any latency
# --------------------------------------------------------------------------- #

def test_the_summarizer_never_blocks_the_request(monkeypatch):
    """A recap is a full model round-trip — MEASURED at over two minutes on the
    free fleet. Compaction runs on EVERY hop, so doing it inline would make large
    requests unusable. First call schedules and returns None immediately."""
    calls = []
    monkeypatch.setattr(app.threading, "Thread",
                        lambda **kw: type("T", (), {"start": lambda s: calls.append(kw)})())
    app._summary_cache.clear()
    app._summary_inflight.clear()
    dropped = [{"role": "user", "content": "build the store " * 40},
               {"role": "assistant", "content": "made src/App.tsx " * 40}]
    assert app._summarize_dropped(dropped) is None
    assert len(calls) == 1, "should have scheduled exactly one background worker"


def test_a_ready_recap_is_returned_from_cache():
    app._summary_cache.clear()
    app._summary_inflight.clear()
    dropped = [{"role": "user", "content": "build the store " * 40},
               {"role": "assistant", "content": "made src/App.tsx " * 40}]
    key, _ = app._summary_key(dropped)
    app._summary_cache[key] = "GOAL: a store. STATE: src/App.tsx exists."
    assert app._summarize_dropped(dropped).startswith("GOAL: a store")


def test_a_second_request_does_not_start_a_second_worker(monkeypatch):
    started = []
    monkeypatch.setattr(app.threading, "Thread",
                        lambda **kw: type("T", (), {"start": lambda s: started.append(1)})())
    app._summary_cache.clear()
    app._summary_inflight.clear()
    dropped = [{"role": "user", "content": "x " * 500}]
    app._summarize_dropped(dropped)
    app._summarize_dropped(dropped)
    assert len(started) == 1, "in-flight guard failed; duplicate work scheduled"


def test_too_little_dropped_text_is_not_worth_a_call():
    assert app._summary_key([{"role": "user", "content": "hi"}]) == (None, None)


@pytest.mark.parametrize("raw,expect", [
    ("<think>plan</think>GOAL: build a store", "GOAL: build a store"),
    ("<thinking>x</thinking>\n\nGOAL: y", "GOAL: y"),
    ("<think>truncated mid-thought, never closed", ""),
    ("GOAL: plain answer", "GOAL: plain answer"),
])
def test_chain_of_thought_is_stripped_from_a_recap(raw, expect):
    """MEASURED: a recap came back starting "<think>Here's a thinking process:".
    Injecting a model's scratchpad into the next model's context is noise, and
    worse, it reads as instructions."""
    assert app._strip_thinking(raw) == expect


def test_compaction_uses_a_recap_when_one_is_supplied():
    out, did = app._compact_to_budget(
        _project_convo(), None, 8000,
        summarizer=lambda dropped: "GOAL: Solaris store. DECISIONS: Stripe over PayPal.")
    assert did is True
    text = _text_of(out)
    assert "Recap of the dropped turns" in text
    assert "Stripe over PayPal" in text, "decisions must survive, not just filenames"


def test_a_failing_summarizer_falls_back_to_the_structural_notice():
    def boom(dropped):
        raise RuntimeError("summariser down")
    with pytest.raises(RuntimeError):
        app._compact_to_budget(_project_convo(), None, 8000, summarizer=boom)
    # ...and with the real fail-open summarizer (returns None), the notice stands.
    out, did = app._compact_to_budget(_project_convo(), None, 8000,
                                      summarizer=lambda d: None)
    assert did is True and "do not start a new project" in _text_of(out)


# --------------------------------------------------------------------------- #
# How much of the window we actually use.
#
# USER 2026-08-01: "he should use maximum context window for each model".
# The factor was 0.55, chosen on the belief that _est_tokens under-counts code.
# Measured against real usage.prompt_tokens from a live provider, it OVER-counts
# every shape of traffic (prose 2.20x, code 1.26x, code+tools 1.14x), so 0.55 on
# top of that spent under half of each window.
# --------------------------------------------------------------------------- #

def test_the_estimator_is_conservative_not_optimistic():
    """If this ever flips, the refit factor below is unsafe and must come down.
    Ratios are char-shape based, so they hold without a network call."""
    prose = [{"role": "user", "content": "the quiet morning air carried rain " * 60}]
    code = [{"role": "user", "content": "def f(x):\n    return x + 1\n" * 60}]
    # chars/4 + overhead must exceed a real tokenizer's count for both shapes;
    # a real tokenizer lands near chars/4.3 for prose and chars/3.2 for this code.
    assert app._est_tokens(prose) > len(prose[0]["content"]) / 4.3
    assert app._est_tokens(code) > len(code[0]["content"]) / 4.3


def test_refit_uses_most_of_the_window(monkeypatch):
    """A request re-fitted to a fraction of the window wastes the capacity the
    user is paying context for."""
    monkeypatch.setattr(app, "_model_ctx_budget", lambda pid, model: 32768)
    captured = {}

    def fake_compact(msgs, tools, budget):
        captured["budget"] = budget
        return msgs, True

    monkeypatch.setattr(app, "_compact_to_budget", fake_compact)
    app._refit_payload_to_learned_ctx(
        "p", {"model": "m", "messages": [{"role": "user", "content": "x"}]})
    assert captured["budget"] >= 32768 * 0.75, "using too little of the window"
    assert captured["budget"] <= 32768 * 0.9, "no headroom left for the response"


def test_max_tokens_is_still_capped_against_the_same_window(monkeypatch):
    """Output shares the window on most providers, so a huge max_tokens would
    re-trigger the overflow this function exists to fix."""
    monkeypatch.setattr(app, "_model_ctx_budget", lambda pid, model: 32768)
    monkeypatch.setattr(app, "_compact_to_budget", lambda m, t, b: (m, True))
    out = app._refit_payload_to_learned_ctx(
        "p", {"model": "m", "max_tokens": 100000,
              "messages": [{"role": "user", "content": "x"}]})
    assert out["max_tokens"] < 32768
