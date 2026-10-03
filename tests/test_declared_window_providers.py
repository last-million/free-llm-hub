"""The window DECLARED to a CLI is one the fleet can hold when providers run dry,
and connected CLIs follow it when it changes.

MEASURED 2026-10-03: an OpenCode conversation grew to ~326K tokens and every
request got 503. The hub had declared limit.context = 500000 for coding-multi /
coding-max, so OpenCode never compacted. That figure was the 25th percentile
over one row per (provider, model) -- and the Gemini flash variants (plus g4f
relay copies of them), all 1M and all on ONE Google quota (exhausted, 429),
outnumbered everything else. The only other 1M model was space-bunny on
OpenRouter (daily limit used); nvidia glm-5.3 / kimi-k3 hold ~250K, kilocode
qwen3.8-27b 262K. Now:

  1. each NON-RELAY provider counts once (its largest eligible window), relays
     never, and the declaration is capped at the window the 3rd-largest such
     provider holds -- and never above the old percentile;
  2. app._resync_declared_windows rewrites ONLY the hub's own window fields of
     every CLI still wired to the hub (the configs on disk said 500000).

All fakes and temp homes, no network, never the owner's real files.
"""
import json
import os

import pytest

import agentic_chat as AC
import app as A

_REAL_RESYNC = A._resync_declared_windows            # before conftest stubs it

ROOT = "http://127.0.0.1:%d" % A.PORT
V1 = ROOT + "/v1"
KEY = "k-test-declared"
IDS = ("auto", "best", "coding", "coding-max", "coding-multi", "multi")


@pytest.fixture(autouse=True)
def _fresh_provider():
    saved = AC._window_provider
    A._declared_fleet_cache[1] = None
    yield
    AC.set_window_provider(saved)
    A._declared_fleet_cache[1] = None


def _fleet(monkeypatch, rows, coding=None):
    """rows: (pid, model, window, score). Every row is a coding model unless
    `coding` names the ones that are."""
    monkeypatch.setattr(A, "_declared_fleet", lambda: list(rows))
    members = set(coding) if coding is not None else {m for _p, m, _w, _s in rows}
    monkeypatch.setattr(A, "_mode_allows", lambda mode, pid, m, session_overrides=None:
                        mode == "coding" and m in members)
    real = A._mode_keys()
    monkeypatch.setattr(A, "_mode_keys", lambda: tuple(set(real) | {"coding"}))


def _gemini_flood(relay_copies):
    rows = [("google", "models/gemini-%s" % v, 1048576, 134.1)
            for v in ("3.5-flash", "3.6-flash", "3.7-flash", "3.8-flash", "flash-latest")]
    rows += [("g4f", "srv_m%02d:models/gemini-3.8-flash" % i, 1048576, 130.1)
             for i in range(relay_copies)]
    rows += [("openrouter", "stealth/space-bunny-alpha", 1000000, 137.7),
             ("nvidia", "z-ai/glm-5.3", 250000, 138.0),
             ("nvidia", "moonshotai/kimi-k3", 250000, 138.1),
             ("kilocode", "qwen/qwen3.8-27b:free", 262144, 134.1),
             ("dahl", "deepseek-ai/DeepSeek-V4-Flash", 163840, 134.0)]
    return rows


def _old_percentile(rows):
    wins = sorted(w for _p, _m, w, _s in rows if w)
    return wins[int(A._DECLARED_PCTL * (len(wins) - 1))]


# --------------------------------------------------------------------------- #
# 1. The rule
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("relay_copies", [2, 7])
def test_a_gemini_flood_declares_what_three_providers_hold(monkeypatch, relay_copies):
    rows = _gemini_flood(relay_copies)
    _fleet(monkeypatch, rows)
    if relay_copies == 7:
        assert _old_percentile(rows) >= 1000000        # the bug: 1M declared
    AC.set_window_provider(A._declared_window_for)
    for mid in IDS:
        got = A._declared_window_for(mid)
        assert 250000 <= got <= 262144, (mid, got)
        assert AC.declared_window(mid) == got
    # google 1M, openrouter 1M, kilocode 262144 -> the 3rd provider's window
    assert A._declared_window_for("coding-max") == min(_old_percentile(rows), 262144)


def test_three_providers_holding_1m_declare_1m(monkeypatch):
    rows = [("google", "models/gemini-3.%d-flash" % v, 1048576, 134.1) for v in (6, 7, 8)]
    rows += [("openrouter", "stealth/space-bunny-alpha", 1048576, 137.7),
             ("dahl", "deepseek-ai/DeepSeek-V4-Flash-0731", 1048576, 134.0),
             ("llm7", "GLM-5.3-Flash", 1048576, 133.6),
             ("nvidia", "z-ai/glm-5.3", 250000, 138.0)]
    _fleet(monkeypatch, rows)
    for mid in IDS:
        assert A._declared_window_for(mid) == 1000000          # clamped max


def test_relays_never_count_as_a_provider(monkeypatch):
    assert A._is_relay_pid("g4f") and A._is_relay_pid("g4f-space")
    rows = [("google", "models/gemini-3.%d-flash" % v, 1048576, 134.1) for v in (6, 7, 8)]
    rows += [("openrouter", "stealth/space-bunny-alpha", 1000000, 137.7),
             ("kilocode", "qwen/qwen3.8-27b:free", 262144, 134.1),
             ("nvidia", "z-ai/glm-5.3", 250000, 138.0)]
    rows += [("g4f", "srv_%d:z-ai/glm-5.3" % i, 1000000, 134.0) for i in range(5)]
    rows += [("g4f-space", "srv_%d:kimi-k3" % i, 1048576, 134.1) for i in range(3)]
    _fleet(monkeypatch, rows)
    assert _old_percentile(rows) == 1000000
    assert A._declared_window_for("coding-max") == 262144
    assert A._declared_window_for("auto") == 262144


def test_only_relay_windows_known_is_too_little(monkeypatch):
    rows = [("g4f", "srv_%d:gemini-3.8-flash" % i, 1048576, 130.1) for i in range(6)]
    rows += [("google", "models/gemini-3.%d-flash" % v, None, 134.1) for v in (6, 7, 8)]
    _fleet(monkeypatch, rows)
    assert A._declared_window_for("coding") is None
    AC.set_window_provider(A._declared_window_for)
    assert AC.declared_window("coding") == AC._CODEX_CONTEXT_WINDOW


def test_too_few_known_still_returns_none(monkeypatch):
    rows = [(p, "m-%s" % p, 262144, 134.0) for p in ("google", "nvidia", "dahl", "kilocode")]
    rows += [("openrouter", "u%d" % i, None, 134.0) for i in range(20)]
    _fleet(monkeypatch, rows)
    assert A._declared_window_for("auto") is None
    assert A._declared_window_for("coding-max") is None


def test_fewer_than_three_providers_declare_what_every_one_holds(monkeypatch):
    rows = [("google", "models/gemini-%d" % i, 1048576, 134.1) for i in range(6)]
    rows += [("nvidia", "z-ai/glm-5.3", 250000, 138.0)]
    _fleet(monkeypatch, rows)
    assert _old_percentile(rows) == 1048576
    assert A._declared_window_for("best") == 250000


def test_pinned_ids_keep_their_own_window(monkeypatch):
    _fleet(monkeypatch, _gemini_flood(7))
    assert A._declared_window_for("google/models/gemini-3.6-flash") == 1048576
    assert A._declared_window_for("nvidia/z-ai/glm-5.3") == 250000
    AC.set_window_provider(A._declared_window_for)
    assert AC.declared_window("google/models/gemini-3.6-flash") == 1000000   # clamped


# --------------------------------------------------------------------------- #
# 2. Connected CLIs follow a changed declared window
# --------------------------------------------------------------------------- #

@pytest.fixture
def home(monkeypatch, tmp_path):
    d = str(tmp_path / "home")
    os.makedirs(d)
    monkeypatch.setattr(A, "_home", lambda: d)
    monkeypatch.setenv("XDG_CONFIG_HOME", os.path.join(d, ".config"))
    monkeypatch.setenv("OPENCLAW_CONFIG", os.path.join(d, ".openclaw", "openclaw.json"))
    monkeypatch.setenv("HERMES_HOME", os.path.join(d, ".hermes"))
    monkeypatch.setenv("KIMI_CODE_HOME", os.path.join(d, ".kimi-code"))
    monkeypatch.setattr(AC, "_isolated_config_dir",
                        lambda cid: os.path.join(d, ".free-llm-hub", "isolated-clis", cid, "config"))
    store = {}
    monkeypatch.setattr(A.config, "set_setting", lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr(A.config, "get_setting", lambda k, default=None: store.get(k, default))
    return d


_CLIS = ("opencode", "pi", "qwen", "openclaw", "aider", "hermes", "kimi", "claude", "codex")
_WINDOW_KEYS = {"context", "output", "contextWindow", "contextWindowSize", "max_input_tokens",
                "context_length", "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
                "CLAUDE_CODE_MAX_CONTEXT_TOKENS", "context_window", "max_context_window",
                "auto_compact_token_limit"}


def _strip(obj):
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items() if k not in _WINDOW_KEYS}
    if isinstance(obj, list):
        return [_strip(v) for v in obj]
    return obj


def _load(path):
    if path.endswith((".yaml", ".yml")):
        import yaml
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if path.endswith(".toml"):
        return [ln for ln in text.splitlines() if "max_context_size" not in ln]
    return json.loads(text)


def _fake_codex_catalog(home_dir, monkeypatch):
    """A hub catalog as _refresh_codex_catalog writes it, and a fake refresh
    (the real one runs the codex binary) that rewrites it from the declared
    windows."""
    cat = os.path.join(home_dir, ".codex", "model_catalog.json")
    calls = []

    def write():
        ents = [{"slug": mid, "display_name": "%s (Calvoun hub)" % mid,
                 "context_window": AC.declared_window(mid),
                 "max_context_window": AC.declared_window(mid)}
                for mid in (A.MODE_ALL, "coding")]
        ents.append({"slug": "gpt-x", "display_name": "GPT X", "context_window": 272000})
        A._cli_write_text(cat, json.dumps({"models": ents}, indent=2) + "\n")

    def refresh(config_path=None):
        calls.append(config_path)
        write()
    write()
    monkeypatch.setattr(A, "_refresh_codex_catalog", refresh)
    return cat, calls


def _connect_everything(home_dir, monkeypatch):
    for cid in _CLIS:
        e = dict(A._get_cli_entry(cid))
        res = A._AUTOFIXERS[e["autofix"]](e, KEY, ROOT, V1, "groq/some-model")
        assert res["ok"], (cid, res)
    AC._seed_opencode_config(AC._isolated_config_dir("opencode"))
    cat, calls = _fake_codex_catalog(home_dir, monkeypatch)
    paths = {
        "opencode": A._p_opencode(),
        "opencode-isolated": os.path.join(AC._isolated_config_dir("opencode"),
                                          "opencode", "opencode.json"),
        "pi": A._p_pi_models(), "openclaw": A._p_openclaw(),
        "qwen": os.path.join(os.path.dirname(A._p_qwen_env()), "settings.json"),
        "aider": A._p_aider_metadata(), "hermes": A._p_hermes(), "kimi": A._p_kimi(),
        "claude": A._p_claude(), "codex": cat,
    }
    for k, p in paths.items():
        assert os.path.isfile(p), k
    return paths, calls


def _windows(paths):
    """Every declared figure each file carries, per CLI."""
    oc = lambda p: {m: v["limit"]["context"] for m, v in
                    _load(p)["provider"]["free-llm-hub"]["models"].items()}
    pi = {m["id"]: m["contextWindow"] for m in _load(paths["pi"])["providers"]["free-llm-hub"]["models"]}
    ow = {m["id"]: m["contextWindow"] for m in
          _load(paths["openclaw"])["models"]["providers"]["freehub"]["models"]}
    qw = {p["id"]: p["generationConfig"]["contextWindowSize"]
          for p in _load(paths["qwen"])["modelProviders"]["openai"]}
    ai = {k: v["max_input_tokens"] for k, v in _load(paths["aider"]).items()}
    hp = _load(paths["hermes"])["providers"]["free-llm-hub"]
    he = dict({m: v["context_length"] for m, v in hp["models"].items()}, top=hp["context_length"])
    with open(paths["kimi"], encoding="utf-8") as f:
        ki = [int(ln.split("=")[1]) for ln in f if ln.startswith("max_context_size")]
    env = _load(paths["claude"])["env"]
    cl = {k: env[k] for k in ("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_CODE_MAX_CONTEXT_TOKENS")}
    cx = {e["slug"]: e["context_window"] for e in _load(paths["codex"])["models"]
          if e["display_name"].endswith("(Calvoun hub)")}
    return {"opencode": oc(paths["opencode"]), "opencode-isolated": oc(paths["opencode-isolated"]),
            "pi": pi, "openclaw": ow, "qwen": qw, "aider": ai, "hermes": he, "kimi": ki,
            "claude": cl, "codex": cx}


def _all_values(win):
    out = set()
    for v in win.values():
        out |= set(v.values()) if isinstance(v, dict) else set(v)
    return out


def test_resync_rewrites_only_the_connected_clis_window_fields(home, monkeypatch):
    AC.set_window_provider(lambda mid: 500000)            # what Connect wrote
    paths, calls = _connect_everything(home, monkeypatch)
    assert _all_values(_windows(paths)) == {500000, "500000"}
    # The user's own pair under the hub block stays (opencode's documented rule).
    oc = _load(paths["opencode"])
    oc["provider"]["free-llm-hub"]["models"]["best"]["limit"] = {"context": 999999, "output": 4096}
    with open(paths["opencode"], "w", encoding="utf-8") as f:
        json.dump(oc, f, indent=2)
    before = {k: _load(p) for k, p in paths.items()}

    AC.set_window_provider(lambda mid: 262144)            # the fleet's figure now
    done = _REAL_RESYNC()
    assert {c for c, _p in done} == set(_CLIS)
    assert {os.path.normcase(p) for _c, p in done} == {os.path.normcase(p) for p in paths.values()}
    assert len(calls) == 1                                 # codex catalog rebuilt once
    win = _windows(paths)
    assert win["opencode"].pop("best") == 999999           # theirs
    assert _all_values(win) == {262144, "262144"}
    for k, p in paths.items():                             # nothing else moved
        after = _load(p)
        if p.endswith(".toml"):
            assert after == before[k], k
        else:
            assert _strip(after) == _strip(before[k]), k
    assert _load(paths["opencode"])["provider"]["free-llm-hub"]["models"]["auto"]["limit"] == \
        {"context": 262144, "output": AC._HUB_MAX_OUTPUT}

    # Unchanged figures: a no-op -- no write, no catalog rebuild.
    stamps = {p: os.stat(p).st_mtime_ns for p in paths.values()}
    assert _REAL_RESYNC() == []
    assert len(calls) == 1
    assert {p: os.stat(p).st_mtime_ns for p in paths.values()} == stamps


def test_resync_never_touches_a_cli_that_is_not_wired_to_the_hub(home, monkeypatch):
    AC.set_window_provider(lambda mid: 262144)
    elsewhere = "http://example.invalid/v1"
    files = {
        A._p_opencode(): json.dumps({"provider": {
            "mine": {"models": {"auto": {"limit": {"context": 500000, "output": 16384}}}},
            "free-llm-hub": {"options": {"baseURL": elsewhere},
                             "models": {"auto": {"limit": {"context": 500000, "output": 16384}}}}}}),
        A._p_pi_models(): json.dumps({"providers": {"free-llm-hub": {
            "baseUrl": elsewhere, "models": [{"id": "auto", "contextWindow": 500000}]}}}),
        A._p_openclaw(): json.dumps({"models": {"providers": {"freehub": {
            "baseUrl": elsewhere, "models": [{"id": "auto", "contextWindow": 500000}]}}}}),
        A._p_claude(): json.dumps({"env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "500000"}}),
        A._p_aider_metadata(): json.dumps({"openai/auto": {"max_input_tokens": 500000}}),
        A._p_aider(): "model: gpt-4o\n",
        os.path.join(os.path.dirname(A._p_qwen_env()), "settings.json"): json.dumps(
            {"modelProviders": {"openai": [{"id": "auto", "baseUrl": elsewhere,
                                            "generationConfig": {"contextWindowSize": 500000}}]}}),
        A._p_hermes(): "providers:\n  free-llm-hub:\n    api: %s\n    context_length: 500000\n"
                       % elsewhere,
        A._p_kimi(): ('[providers.free-hub]\ntype = "openai"\nbase_url = "%s"\n\n'
                      '[models."auto"]\nprovider = "free-hub"\nmodel = "auto"\n'
                      'max_context_size = 500000\n' % elsewhere),
        A._p_codex(): 'model = "gpt-5"\n',
    }
    for p, text in files.items():
        A._cli_write_text(p, text)
    monkeypatch.setattr(A, "_refresh_codex_catalog",
                        lambda config_path=None: pytest.fail("codex is not wired"))
    stamps = {p: os.stat(p).st_mtime_ns for p in files}
    assert _REAL_RESYNC() == []
    for p, text in files.items():
        with open(p, encoding="utf-8") as f:
            assert f.read() == text, p
    assert {p: os.stat(p).st_mtime_ns for p in files} == stamps


def test_kimi_resync_keeps_every_other_byte():
    AC.set_window_provider(lambda mid: {"auto": 262144}.get(mid, 250000))
    text = ('default_model = "auto"\r\n\r\n[providers.free-hub]\r\ntype = "openai"\r\n'
            'base_url = "%s"\r\n\r\n[models."auto"]\r\nprovider = "free-hub"\r\n'
            'model = "auto"\r\nmax_context_size = 500000  # hub\r\n\r\n'
            '[models."best"]\r\nprovider = "other"\r\nmax_context_size = 500000\r\n' % V1)
    new, changed = A._kimi_resync_text(text)
    assert changed
    assert new == text.replace("max_context_size = 500000  # hub",
                               "max_context_size = 262144  # hub")
    assert A._kimi_resync_text(new) == (new, False)


def test_one_failing_cli_never_stops_the_others(monkeypatch):
    names = [fn for _c, fn in A._DECLARED_RESYNCERS]
    for fn in names:
        monkeypatch.setattr(A, fn, (lambda n: lambda: ["/tmp/%s" % n])(fn))

    def boom():
        raise RuntimeError("unreadable")
    monkeypatch.setattr(A, "_resync_pi_windows", boom)
    done = _REAL_RESYNC()
    assert len(done) == len(names) - 1
    assert "pi" not in {c for c, _p in done}


def test_the_periodic_check_writes_only_when_the_figures_change(monkeypatch):
    runs = []
    monkeypatch.setattr(A, "_resync_declared_windows", lambda: runs.append(1) or [])
    monkeypatch.setattr(A, "_declared_resync_last", [None])
    AC.set_window_provider(lambda mid: 262144)
    A._resync_declared_windows_if_changed()
    A._resync_declared_windows_if_changed()
    assert len(runs) == 1
    AC.set_window_provider(lambda mid: 250000)
    A._resync_declared_windows_if_changed()
    assert len(runs) == 2
    A._resync_declared_windows_if_changed(force=True)      # the boot pass
    assert len(runs) == 3

    def boom():
        raise RuntimeError("x")
    monkeypatch.setattr(A, "_declared_window_signature", boom)
    assert A._resync_declared_windows_if_changed() == []     # never raises


def test_the_boot_and_the_periodic_check_are_wired():
    src = open("app.py", encoding="utf-8").read()
    warm = src[src.index("def _warm_catalogs_async("):]
    warm = warm[:warm.index("\ndef ")]
    assert "_resync_declared_windows_if_changed(force=True)" in warm
    main = src[src.index('if __name__ == "__main__":'):]
    assert "_start_declared_window_resync()" in main
    conf = open(os.path.join("tests", "conftest.py"), encoding="utf-8").read()
    assert '"_resync_declared_windows"' in conf and '"_start_declared_window_resync"' in conf
