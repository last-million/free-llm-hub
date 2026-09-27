"""The hub DETECTS each model's context window instead of assuming one.

MEASURED 2026-09-27 on the live fleet: 43 of 123 alive models (35%) had a
known window; nvidia (57), google (11), dahl, llm7, glm... publish none in
their OpenAI-compatible /models, while known windows span 4096..1,000,000. So:

  1. every common catalog field shape is read (vLLM max_model_len, Google's
     inputTokenLimit, OpenRouter's top_provider, limit(s).context, ...), and
     Google's NATIVE list is harvested because its compat list states nothing;
  2. a model unknown on one provider INHERITS the window other catalogs state
     for the same identity (lower median, flagged "inferred"), below any
     direct or learned figure and still shrunk by a real 400/413;
  3. a documented per-family table covers identities no catalog describes;
  4. /api/tracking and /api/model-windows report window + source;
  5. the window DECLARED to each CLI follows the fleet behind each hub id
     (agentic_chat.declared_window), with the fixed 128000 as fail-safe.

All fakes, no network.
"""
import json
import time

import pytest

import agentic_chat as AC
import app as A


class _Resp:
    def __init__(self, status=200, payload=None, text=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text if text is not None else json.dumps(self._payload)

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def isolated_windows():
    tables = (A._MODEL_MAX_INPUT, A._MODEL_LEARNED_AT, A._MODEL_CATALOG_CTX,
              A._MODEL_MAX_OUTPUT, A._REF_CATALOG_CTX)
    saved = [(d, dict(d)) for d in tables]
    ref_at = A._REF_CATALOG_AT[0]
    provider = AC._window_provider
    native = dict(A._CTX_NATIVE_LAST)
    for d in tables:
        d.clear()
    A._ctx_index_touch()
    A._declared_fleet_cache[1] = None
    yield
    for d, snap in saved:
        d.clear()
        d.update(snap)
    A._REF_CATALOG_AT[0] = ref_at
    AC.set_window_provider(provider)
    A._CTX_NATIVE_LAST.clear()
    A._CTX_NATIVE_LAST.update(native)
    A._declared_fleet_cache[1] = None
    A._ctx_index_touch()


# --------------------------------------------------------------------------- #
# 1. Every catalog field shape
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("row,want", [
    ({"id": "m", "context_length": 131072}, 131072),            # openrouter/together
    ({"id": "m", "context_window": 32768}, 32768),               # groq
    ({"id": "m", "context": 65536}, 65536),                      # puter
    ({"id": "m", "max_context_length": 128000}, 128000),         # mistral
    ({"id": "m", "max_input_tokens": 200000}, 200000),           # github models
    ({"id": "m", "max_model_len": 16384}, 16384),                # vLLM / SGLang
    ({"name": "models/g", "inputTokenLimit": 1048576}, 1048576),  # Google native
    ({"id": "m", "input_token_limit": 30720}, 30720),
    ({"id": "m", "n_ctx": 8192}, 8192),                          # llama.cpp server
    ({"id": "m", "top_provider": {"context_length": 40960}}, 40960),
    ({"id": "m", "limits": {"context": 262144}}, 262144),
    ({"id": "m", "limit": {"context": 204800, "output": 8192}}, 204800),
    ({"id": "m", "capabilities": {"limits": {"max_context_window_tokens": 64000}}}, 64000),
    ({"id": "m", "properties": [{"property_id": "context_window", "value": "24000"}]},
     24000),                                                     # cloudflare
    ({"id": "m", "context_length": "131072"}, 131072),           # numeric string
])
def test_every_catalog_field_shape_is_read(row, want):
    assert A._catalog_row_ctx(row) == want
    A._learn_ctx_from_catalog("pshape", {"data": [row]})
    mid = row.get("id") or row.get("name")
    assert A._MODEL_CATALOG_CTX[("pshape", mid)] == want
    assert A._window_info("pshape", mid) == (want, "catalog")


def test_an_output_cap_is_never_read_as_the_window():
    """max_tokens / max_completion_tokens bound the REPLY."""
    row = {"id": "m", "max_tokens": 8192, "max_completion_tokens": 4096}
    assert A._catalog_row_ctx(row) is None
    assert A._learn_ctx_from_catalog("pout", {"data": [row]}) == 0


def test_max_tokens_counts_only_where_the_provider_documents_it(monkeypatch):
    real = A.prov.get_provider
    monkeypatch.setattr(A.prov, "get_provider", lambda pid: (
        {"catalog_max_tokens_is_context": True} if pid == "pdoc" else real(pid)))
    A._learn_ctx_from_catalog("pdoc", {"data": [{"id": "m", "max_tokens": 32768}]})
    assert A._MODEL_CATALOG_CTX[("pdoc", "m")] == 32768


def test_insane_or_boolean_values_are_ignored():
    for v in (True, 12, 10 ** 9, "lots", None):
        assert A._catalog_row_ctx({"id": "m", "context_length": v}) is None


# --------------------------------------------------------------------------- #
# 1b. Google's native list
# --------------------------------------------------------------------------- #

def test_google_native_catalog_records_input_token_limit(monkeypatch):
    assert A.prov.get_provider("google").get("ctx_models_url")
    pages = [
        _Resp(200, {"models": [{"name": "models/gemini-9-flash", "inputTokenLimit": 1048576,
                                "outputTokenLimit": 65536}],
                    "nextPageToken": "p2"}),
        _Resp(200, {"models": [{"name": "models/gemma-9-27b-it", "inputTokenLimit": 131072}]}),
    ]
    seen = []

    def fake_get(url, headers=None, timeout=None):
        seen.append((url, headers))
        return pages.pop(0)

    monkeypatch.setattr(A.requests, "get", fake_get)
    n = A._harvest_native_ctx_catalog("google", {"api_key": "google-test-key"})
    assert n == 2
    assert A._window_info("google", "models/gemini-9-flash") == (1048576, "catalog")
    assert A._window_info("google", "models/gemma-9-27b-it") == (131072, "catalog")
    # the key rides in Google's own header, never as a bearer; page 2 is fetched
    assert seen[0][1] == {"x-goog-api-key": "google-test-key"}
    assert "pageToken=p2" in seen[1][0]


def test_native_harvest_is_throttled_and_skipped_for_custom_bases(monkeypatch):
    started = []
    monkeypatch.setattr(A.threading, "Thread",
                        lambda target=None, args=(), **kw: type(
                            "T", (), {"start": lambda self: started.append(args)})())
    A._CTX_NATIVE_LAST.pop("google", None)
    assert A._maybe_harvest_native_ctx("google", {"api_key": "k"}) is True
    assert A._maybe_harvest_native_ctx("google", {"api_key": "k"}) is False   # throttled
    assert A._maybe_harvest_native_ctx("groq", {"api_key": "k"}) is False     # no native list
    assert len(started) == 1
    # a custom base_url's key may not be Google's: the fetch itself refuses
    monkeypatch.setattr(A.requests, "get", lambda *a, **k: pytest.fail("fetched"))
    assert A._harvest_native_ctx_catalog("google", {"api_key": "k",
                                                    "base_url": "https://x.invalid"}) == 0


# --------------------------------------------------------------------------- #
# 2. Cross-provider inference by identity
# --------------------------------------------------------------------------- #

def _cat(pid, mid, win):
    A._learn_ctx_from_catalog(pid, {"data": [{"id": mid, "context_length": win}]})


def test_an_unknown_model_inherits_the_same_identity_window():
    _cat("openrouter", "acme/zorblax-9:free", 262144)
    _cat("kilocode", "acme/zorblax-9", 262144)
    _cat("puter", "zorblax-9", 1000000)
    # nvidia publishes nothing: inferred from the others (lower median)
    assert A._window_info("nvidia", "acme/zorblax-9") == (262144, "inferred")
    assert A._window_info("tokenrouter", "acme/zorblax-9-free") == (262144, "inferred")


def test_one_relays_odd_cap_does_not_drag_the_inference():
    for pid in ("openrouter", "kilocode", "puter", "requesty"):
        _cat(pid, "acme/zorblax-flash", 1048576)
    _cat("g4f", "acme/zorblax-flash", 8000)
    assert A._window_info("google", "models/zorblax-flash") == (1048576, "inferred")


def test_a_looser_identity_is_tried_when_the_exact_one_is_unknown():
    _cat("openrouter", "acme/zorblax-8x22b-instruct", 65536)
    assert A._window_info("nvidia", "acme/zorblax-8x22b-v0.1") == (65536, "inferred")
    # ...but a version never collapses to a bare family name
    assert A._ctx_loose_ident("deepseek-v3") == "deepseek-v3"


def test_priority_direct_beats_inferred_and_learned_beats_both():
    _cat("openrouter", "acme/zorblax-9", 262144)
    _cat("nvidia", "acme/zorblax-9", 131072)          # nvidia's own row: direct
    assert A._window_info("nvidia", "acme/zorblax-9") == (131072, "catalog")
    A._set_learned_ctx("nvidia", "acme/zorblax-9", 65536)
    assert A._window_info("nvidia", "acme/zorblax-9") == (65536, "learned")


def test_a_real_400_still_shrinks_an_inferred_window():
    _cat("openrouter", "acme/zorblax-9", 262144)
    assert A._window_info("dahl", "acme/zorblax-9") == (262144, "inferred")
    A._learn_context_limit("dahl", "acme/zorblax-9", _Resp(
        400, text='{"error":"This model\'s maximum context length is 32768 tokens"}'))
    assert A._window_info("dahl", "acme/zorblax-9") == (32768, "learned")
    assert A._model_ctx_budget("dahl", "acme/zorblax-9") == 32768


def test_a_learned_window_larger_than_the_inferred_one_wins_too():
    _cat("openrouter", "acme/zorblax-9", 131072)
    A._set_learned_ctx("dahl", "acme/zorblax-9", 200000)
    assert A._window_info("dahl", "acme/zorblax-9") == (200000, "learned")


def test_router_aliases_and_tiny_ids_are_never_inferred():
    """openrouter/auto (a router, 2M) and a g4f 'pa:<hash>:auto' share only
    the word."""
    _cat("openrouter", "openrouter/auto", 2000000)
    _cat("openrouter", "vendor/m", 131072)
    assert A._window_info("g4f", "pa:657cce02:auto") == (None, "default")
    assert A._window_info("other", "m") == (None, "default")


def test_openrouters_public_catalog_feeds_inference():
    n = A._learn_ctx_reference([
        {"id": "acme/zorblax-11", "canonical_slug": "acme/zorblax-11-20260901",
         "context_length": 400000},
        {"id": "acme/zorblax-11:free", "context_length": 200000},
        {"id": "acme/no-window"},
    ])
    assert n >= 1
    # the smaller of two listings of one identity (a :free variant)
    assert A._window_info("nvidia", "acme/zorblax-11") == (200000, "inferred")


def test_the_keyed_openrouter_catalog_outvotes_its_public_copy():
    A._learn_ctx_reference([{"id": "acme/zorblax-12", "context_length": 1000000}])
    _cat("openrouter", "acme/zorblax-12", 131072)
    assert A._window_info("nvidia", "acme/zorblax-12") == (131072, "inferred")


def test_inference_can_only_lower_a_measured_provider_row():
    """An inferred figure describes the MODEL; a host may serve less."""
    _cat("openrouter", "acme/zorblax-big", 1048576)
    _cat("openrouter", "acme/zorblax-small", 65536)
    assert A._model_ctx_info("nvidia", "acme/zorblax-big") == (A._PROVIDER_TPM["nvidia"], "table")
    assert A._model_ctx_info("nvidia", "acme/zorblax-small") == (65536, "inferred")
    # no provider row: the inferred figure replaces the default guess
    assert A._model_ctx_info("no-such-provider", "acme/zorblax-big") == (1048576, "inferred")


def test_routing_uses_an_inferred_window_but_not_a_family_guess():
    _cat("openrouter", "acme/zorblax-small", 65536)
    assert A._context_ok("dahl", "acme/zorblax-small", 100000) is False
    assert A._context_ok("dahl", "acme/zorblax-small", 30000) is True
    # a reference figure alone never blocks a model
    assert A._window_info("dahl", "meta/llama-3.3-70b-instruct")[1] == "reference"
    assert A._context_ok("dahl", "meta/llama-3.3-70b-instruct", 500000) is True


# --------------------------------------------------------------------------- #
# 3. Reference table (lowest priority)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("mid,want", [
    ("meta/llama-3.3-70b-instruct", 131072),
    ("nvidia/llama-3.1-nemotron-ultra-253b-v1", 131072),
    ("solidrust/Hermes-3-Llama-3.1-8B-AWQ", 131072),
    ("nvidia/llama3-chatqa-1.5-70b", 8192),
    ("meta/llama2-70b", 4096),
    ("nvidia/nemotron-4-340b-instruct", 4096),
    ("microsoft/phi-3-vision-128k-instruct", 131072),        # stated in the id
    ("writer/palmyra-fin-70b-32k", 32768),
    ("mistralai/mixtral-8x22b-v0.1", 65536),
    ("mistralai/mistral-7b-instruct-v0.3", 32768),
    ("google/gemma-2b", 8192),
    ("google/gemma-3-12b-it", 131072),
    ("models/gemini-flash-lite-latest", 1048576),
    ("openai/gpt-oss-20b", 131072),
    ("z-ai/glm-4.5-flash", 131072),
])
def test_the_reference_table_covers_documented_families(mid, want):
    assert A._reference_ctx(mid) == want
    assert A._window_info("nvidia", mid) == (want, "reference")


def test_unknown_families_stay_unknown():
    for mid in ("pollinations-openai-fast", "qwen/qwen3.8-27b", "acme/zorblax-9"):
        assert A._reference_ctx(mid) is None
    assert A._window_info("pollinations", "openai-fast") == (None, "default")


def test_any_catalog_beats_the_reference_table():
    _cat("openrouter", "meta/llama-3.3-70b-instruct", 65536)
    assert A._window_info("nvidia", "meta/llama-3.3-70b-instruct") == (65536, "inferred")


# --------------------------------------------------------------------------- #
# 4. Reporting
# --------------------------------------------------------------------------- #

def test_coverage_counts_every_source():
    _cat("openrouter", "acme/zorblax-9", 262144)
    _cat("nvidia", "acme/own", 32768)
    A._set_learned_ctx("nvidia", "acme/burnt", 16000)
    cov = A._ctx_coverage([("nvidia", "acme/own"), ("nvidia", "acme/burnt"),
                           ("nvidia", "acme/zorblax-9"),
                           ("nvidia", "meta/llama-3.3-70b-instruct"),
                           ("pollinations", "openai-fast")])
    assert cov == {"known": 4, "total": 5,
                   "by_source": {"catalog": 1, "learned": 1, "inferred": 1,
                                 "reference": 1, "default": 1}}


def test_the_model_windows_endpoint_reports_window_and_source(monkeypatch):
    _cat("openrouter", "acme/zorblax-9", 262144)
    monkeypatch.setattr(A, "_enabled_keyed", lambda: ["nvidia"])
    monkeypatch.setattr(A, "_prefetch_auto_models", lambda pids: {
        "nvidia": ["acme/zorblax-9", "meta/llama-3.3-70b-instruct", "acme/mystery"]})
    monkeypatch.setattr(A, "_is_provider_dead", lambda pid: False)
    with A.app.test_request_context("/api/model-windows"):
        data = A.api_model_windows().get_json()
    rows = {r["model"]: r for r in data["models"]}
    assert rows["acme/zorblax-9"]["source"] == "inferred"
    assert rows["acme/zorblax-9"]["window"] == 262144
    assert rows["meta/llama-3.3-70b-instruct"]["source"] == "reference"
    assert rows["acme/mystery"]["source"] == "default" and rows["acme/mystery"]["window"] is None
    assert data["coverage"]["known"] == 2 and data["coverage"]["total"] == 3
    assert data["declared"]["auto"] == AC._CODEX_CONTEXT_WINDOW   # nothing registered


def test_tracking_rows_carry_window_and_source():
    src = open("app.py", encoding="utf-8").read()
    body = src[src.index("def api_tracking("):]
    body = body[:body.index("\n@app.route")]
    assert '"ctx_window": ctx_w, "ctx_source": ctx_src' in body
    assert '"ctx_coverage"' in body


def test_startup_logs_coverage():
    src = open("app.py", encoding="utf-8").read()
    body = src[src.index("def _warm_catalogs_async("):]
    body = body[:body.index("\ndef ")]
    assert "_log_ctx_coverage(" in body
    main = src[src.index('if __name__ == "__main__":'):]
    assert "agentic_chat.set_window_provider(_declared_window_for)" in main
    assert "_start_ctx_reference_refresh()" in main


def test_the_reference_catalog_survives_a_restart():
    A._learn_ctx_reference([{"id": "acme/zorblax-13", "context_length": 300000}])
    blob = A._dead_state_dump()
    assert blob["model_ctx_reference"]["zorblax-13"] == 300000
    A._REF_CATALOG_CTX.clear()
    A._dead_state_load(blob)
    assert A._REF_CATALOG_CTX["zorblax-13"] == 300000
    assert A._window_info("nvidia", "acme/zorblax-13") == (300000, "inferred")


# --------------------------------------------------------------------------- #
# 5. Declared windows
# --------------------------------------------------------------------------- #

def test_declared_window_defaults_when_nothing_is_registered():
    AC.set_window_provider(None)
    assert AC.declared_window("auto") == AC._CODEX_CONTEXT_WINDOW == 128000
    assert AC.declared_compact_limit("auto") == AC._CODEX_COMPACT_LIMIT


@pytest.mark.parametrize("value,want", [
    (200000, 200000), (10, 32000), (5_000_000, 1_000_000),
    (None, 128000), (0, 128000), ("big", 128000), (True, 128000)])
def test_declared_window_is_clamped_and_fail_safe(value, want):
    AC.set_window_provider(lambda mid: value)
    assert AC.declared_window("auto") == want


def test_a_raising_provider_falls_back():
    def boom(mid):
        raise RuntimeError("x")
    AC.set_window_provider(boom)
    assert AC.declared_window("coding") == AC._CODEX_CONTEXT_WINDOW


def _fleet(monkeypatch, rows, cats=None):
    """rows: (pid, model, window, score). cats: {category: {models}}."""
    monkeypatch.setattr(A, "_declared_fleet", lambda: list(rows))
    cats = cats or {}
    monkeypatch.setattr(A, "_mode_allows", lambda mode, pid, m, session_overrides=None:
                        m in cats.get(mode, ()))


def test_declared_is_the_25th_percentile_of_capable_candidates(monkeypatch):
    strong = [("p", "s%d" % i, w, 90.0) for i, w in enumerate(
        (65536, 131072, 200000, 262144, 262144, 1000000, 1048576, 1048576, 1048576))]
    weak = [("p", "w%d" % i, 4096, 20.0) for i in range(20)]   # simple-tier helpers
    _fleet(monkeypatch, strong + weak)
    # 9 strong windows sorted; index int(0.25 * 8) = 2 -> 200000
    assert A._declared_window_for("auto") == 200000
    assert A._declared_window_for(None) == 200000
    assert A._declared_window_for("best") == 200000


def test_declared_per_category_and_compound(monkeypatch):
    rows = [("p", "c%d" % i, 65536, 90.0) for i in range(6)] + \
           [("p", "v%d" % i, 1048576, 90.0) for i in range(6)]
    cats = {"coding": {"c%d" % i for i in range(6)},
            "vision": {"v%d" % i for i in range(6)}}
    _fleet(monkeypatch, rows, cats)
    real_keys = A._mode_keys()
    monkeypatch.setattr(A, "_mode_keys", lambda: tuple(set(real_keys) | {"coding", "vision"}))
    assert A._declared_window_for("coding") == 65536
    assert A._declared_window_for("coding-swarm") == 65536
    assert A._declared_window_for("vision/max") == 1000000     # clamped
    AC.set_window_provider(A._declared_window_for)
    assert AC.declared_window("coding") == 65536


def test_too_few_known_windows_use_the_default(monkeypatch):
    rows = [("p", "k%d" % i, 262144, 90.0) for i in range(4)]
    rows += [("p", "u%d" % i, None, 90.0) for i in range(20)]
    _fleet(monkeypatch, rows)
    assert A._declared_window_for("auto") is None
    AC.set_window_provider(A._declared_window_for)
    assert AC.declared_window("auto") == AC._CODEX_CONTEXT_WINDOW


def test_declared_is_clamped_to_the_floor(monkeypatch):
    _fleet(monkeypatch, [("p", "t%d" % i, 8192, 90.0) for i in range(8)])
    assert A._declared_window_for("auto") == 32000


def test_the_fleet_uses_what_the_hub_really_sends(monkeypatch):
    """groq's hard per-request cap and a measured provider row bound a
    model's declared contribution, like its routing budget."""
    _cat("openrouter", "acme/zorblax-big", 1048576)
    _cat("groq", "acme/groq-own", 131072)
    monkeypatch.setattr(A, "_cached_catalogs", lambda: {
        "nvidia": ["acme/zorblax-big"], "groq": ["acme/groq-own"]})
    monkeypatch.setattr(A, "_is_provider_dead", lambda pid: False)
    monkeypatch.setattr(A, "_is_model_blocked_by_user", lambda pid, m: False)
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, m: 90.0)
    fleet = {(p, m): w for p, m, w, _s in A._declared_fleet()}
    assert fleet[("nvidia", "acme/zorblax-big")] == A._PROVIDER_TPM["nvidia"]
    assert fleet[("groq", "acme/groq-own")] == A._PROVIDER_TPM["groq"]


def test_opencode_declares_a_window_per_id():
    AC.set_window_provider(lambda mid: {"auto": 200000, "coding": 64000}.get(mid))
    models = AC._opencode_hub_models()
    assert models["auto"]["limit"] == {"context": 200000, "output": AC._HUB_MAX_OUTPUT}
    assert models["coding"]["limit"] == {"context": 64000, "output": 16000}
    assert models["best"]["limit"]["context"] == AC._CODEX_CONTEXT_WINDOW
    for spec in models.values():
        assert spec["limit"]["output"] <= spec["limit"]["context"] // 4


def test_opencode_repair_follows_the_declared_window_but_keeps_the_users(tmp_path):
    AC.set_window_provider(lambda mid: 200000)
    p = tmp_path / "opencode.json"
    p.write_text(json.dumps({"provider": {"free-llm-hub": {"models": {
        "auto": {"name": "x", "limit": {"context": 128000, "output": 16384}},
        "best": {"name": "y", "limit": {"context": 999999, "output": 4096}}}}}}),
        encoding="utf-8")
    AC._upgrade_opencode_seed(str(p))
    got = json.loads(p.read_text(encoding="utf-8"))["provider"]["free-llm-hub"]["models"]
    assert got["auto"]["limit"]["context"] == 200000          # the old fixed figure: ours
    assert got["best"]["limit"] == {"context": 999999, "output": 4096}   # theirs


def test_codex_catalog_declares_a_window_per_entry():
    AC.set_window_provider(lambda mid: 400000 if mid == A.MODE_ALL else 64000)
    template = {"slug": "gpt-x", "priority": 1, "visibility": "list",
                "context_window": 272000, "max_context_window": 272000,
                "auto_compact_token_limit": 200000}
    entries = A._codex_catalog_models({"models": [template]})
    hub = {e["slug"]: e for e in entries if e["slug"] != "gpt-x"}
    assert hub[A.MODE_ALL]["context_window"] == 400000
    assert hub[A.MODE_ALL]["max_context_window"] == 400000
    assert hub[A.MODE_ALL]["auto_compact_token_limit"] == 300000
    other = [e for s, e in hub.items() if s != A.MODE_ALL]
    assert other and all(e["context_window"] == 64000 for e in other)
    assert all(e["auto_compact_token_limit"] == 48000 for e in other)
    # the built-in entry is left exactly as codex shipped it
    builtin = [e for e in entries if e["slug"] == "gpt-x"][0]
    assert builtin["context_window"] == 272000


def test_codex_fallback_config_uses_the_declared_window():
    AC.set_window_provider(lambda mid: 200000)
    text = AC._codex_hub_fallback_text("")
    assert "model_context_window = 200000" in text
    assert "model_auto_compact_token_limit = 150000" in text


def test_pi_openclaw_and_kimi_declarations_use_it():
    src = open("app.py", encoding="utf-8").read()
    for fn in ("def _pi_provider_block(", "def _autofix_openclaw("):
        body = src[src.index(fn):]
        body = body[:body.index("\ndef ")]
        assert "agentic_chat.declared_window(" in body, fn
    AC.set_window_provider(lambda mid: 262144)
    block = A._pi_provider_block("k", "http://127.0.0.1:1/v1")
    assert all(m["contextWindow"] == 262144 for m in block["models"])
    assert "max_context_size = 262144" in A._kimi_apply_text("", "http://x/v1", "k")
