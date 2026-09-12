r"""An opencode turn reaches the hub with its session on it, like the others.

claude and codex are pointed at <hub>/build/<session_id> -- an env var and a
per-session config file. opencode's provider lives in ONE seeded file shared
by every session, so its turns arrived with no session at all. MEASURED
2026-09-12 on a multi-session run: every worker request was "source: cli,
project: None", and a conversation's own model rules -- its allow/block lists,
its category edits, its mode fallback -- never applied to an opencode session.

OPENCODE_CONFIG_CONTENT is a local-scope config opencode deep-merges over the
files, so one option can be overridden per process. Verified live: a run with
it set showed up as "source: build".

Two smaller things found on the same run, kept here because they were fixed
together:
  * two workers starting together both pinned the same model -- each read
    "nothing pinned yet" before either had pinned. The pick is atomic now.
  * the hidden launcher gave the hub no console, so nothing it logged went
    anywhere. It keeps a small rotating log next to config.json now.
"""
import json
import logging
import os
import threading

import pytest

import agentic_chat as AC
import app as A


APP = open("app.py", encoding="utf-8").read()


# --------------------------------------------------------------------------- #
# The session rides in the environment
# --------------------------------------------------------------------------- #

@pytest.fixture
def isolated_opencode(monkeypatch, tmp_path):
    monkeypatch.setattr(AC, "_isolated_bin", lambda cli: str(tmp_path / "opencode.cmd"))
    monkeypatch.setattr(AC, "_isolated_config_dir", lambda cli: str(tmp_path / "cfg"))
    monkeypatch.setattr(AC, "_seed_opencode_config", lambda path: None)
    monkeypatch.setattr(AC, "_port", lambda: 8787)
    yield


def test_the_turn_carries_the_session_url(isolated_opencode, tmp_path):
    env = AC._agentic_env("opencode", str(tmp_path), "normal", "abc123")
    cfg = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert cfg == {"provider": {"free-llm-hub": {"options": {
        "baseURL": "http://127.0.0.1:8787/build/abc123/v1"}}}}


def test_it_names_the_provider_the_seed_registers(isolated_opencode, tmp_path):
    """An override under any other name is a second, half-configured provider."""
    env = AC._agentic_env("opencode", str(tmp_path), "normal", "abc123")
    assert list(json.loads(env["OPENCODE_CONFIG_CONTENT"])["provider"]) == [AC._OPENCODE_PROVIDER_ID]
    src = open("agentic_chat.py", encoding="utf-8").read()
    body = src[src.index("def _seed_opencode_config("):]
    body = body[:body.index("\ndef ", 10)]
    assert "_OPENCODE_PROVIDER_ID: {" in body


def test_no_session_means_no_override(isolated_opencode, tmp_path):
    env = AC._agentic_env("opencode", str(tmp_path), "normal", None)
    assert "OPENCODE_CONFIG_CONTENT" not in env


def test_the_users_own_install_is_left_alone(monkeypatch, tmp_path):
    """A global opencode has the user's own providers; a partial override
    named after ours would be a broken provider in their list."""
    monkeypatch.setattr(AC, "_isolated_bin", lambda cli: None)
    env = AC._agentic_env("opencode", str(tmp_path), "normal", "abc123")
    assert "OPENCODE_CONFIG_CONTENT" not in env


def test_the_other_clis_do_not_get_it(isolated_opencode, tmp_path, monkeypatch):
    monkeypatch.setattr(AC, "_apply_claude_hub_fallback", lambda *a, **k: None)
    monkeypatch.setattr(AC, "_apply_codex_hub_fallback", lambda *a, **k: None)
    for cli in ("claude", "codex"):
        env = AC._agentic_env(cli, str(tmp_path), "normal", "abc123")
        assert "OPENCODE_CONFIG_CONTENT" not in env, cli


def test_the_hub_reads_the_session_off_that_url():
    """The other half: app.py strips /build/<sid> and remembers it."""
    with A.app.test_request_context("/build/abc123/v1/chat/completions"):
        pass
    m = A._BUILD_PREFIX_RE.match("/build/abc123/v1/chat/completions")
    assert m and m.group(1) == "abc123" and m.group(2) == "/v1/chat/completions"


# --------------------------------------------------------------------------- #
# Siblings starting together
# --------------------------------------------------------------------------- #

def test_the_first_turn_pick_is_atomic():
    i = APP.index("with _spread_pick_lock:")
    body = APP[i:i + 700]
    assert "_spread_pool(_pool, _skey)" in body
    assert "_session_pin_set(_skey, pid, model)" in body
    assert isinstance(A._spread_pick_lock, type(threading.Lock()))


def test_the_pin_helpers_take_a_different_lock():
    """The pick holds _spread_pick_lock and calls the helpers, which take
    _session_pin_lock; the same lock would deadlock the first turn."""
    assert A._spread_pick_lock is not A._session_pin_lock


def test_two_siblings_picking_together_land_on_different_models(monkeypatch):
    """The scenario itself: three siblings, three candidates, every pick made
    through the same code path -- each lands on its own model."""
    pool = [(90.0, "a", "model-a"), (89.0, "b", "model-b"), (88.0, "c", "model-c")]
    A._session_pins.clear()
    seen = []

    def pick(key):
        with A._spread_pick_lock:
            p = A._spread_pool(list(pool), key)
            s, pid, model = max(p)
            A._session_pin_set(key, pid, model)
            seen.append(model)
    threads = [threading.Thread(target=pick, args=("k%d" % i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(seen) == ["model-a", "model-b", "model-c"]
    A._session_pins.clear()


# --------------------------------------------------------------------------- #
# A log that survives the hidden launcher
# --------------------------------------------------------------------------- #

def test_the_hub_keeps_a_log_next_to_its_config():
    assert A.HUB_LOG_PATH
    assert os.path.dirname(A.HUB_LOG_PATH) == A.config.state_dir()
    assert os.path.basename(A.HUB_LOG_PATH) == "hub.log"


def _file_handler():
    from logging.handlers import RotatingFileHandler
    hs = [h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)]
    assert hs, "no rotating file handler on the root logger"
    return hs[0]


def test_it_is_attached_to_the_root_logger_where_every_module_logs():
    h = _file_handler()
    assert os.path.normcase(h.baseFilename) == os.path.normcase(A.HUB_LOG_PATH)
    assert h.level <= logging.INFO


def test_what_the_hub_logs_lands_in_it():
    """Through the handler itself: pytest's own log capture sits between the
    loggers and the handlers while a test runs, so this drives the handler
    the way the root logger would."""
    h = _file_handler()
    rec = logging.LogRecord("free-llm-hub", logging.INFO, __file__, 1,
                            "[test] the file log is attached %d", (424242,), None)
    h.handle(rec)
    h.flush()
    with open(A.HUB_LOG_PATH, encoding="utf-8") as fh:
        assert "INFO [test] the file log is attached 424242" in fh.read()


def test_the_access_log_stays_out_of_it():
    """One line per request is what /activity is for; here it would bury the
    lines the file exists to keep."""
    h = _file_handler()
    rec = logging.LogRecord("werkzeug", logging.INFO, __file__, 1,
                            '127.0.0.1 - - "GET /api/status HTTP/1.1" 200 -', (), None)
    assert not h.filter(rec)
    rec = logging.LogRecord("free-llm-hub", logging.INFO, __file__, 1, "[spread] x", (), None)
    assert h.filter(rec)


def test_it_rotates_rather_than_grows():
    h = _file_handler()
    assert 0 < h.maxBytes <= 10 * 1024 * 1024 and h.backupCount >= 1
