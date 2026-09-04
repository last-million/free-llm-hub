"""Pi and Agent Zero: detected, and connectable.

ASKED 2026-09-04: "can you please make also agent zero and pi agent to be
compatible with our local hub and to be detected too".

Both were researched before anything was written, because they are configured in
completely different ways and guessing would have produced two cards that look
right and work never:

  PI (pi.dev, earendil-works/pi) is a coding-agent CLI with its own multi-provider
  LLM layer. Its docs name ~/.pi/agent/models.json as the place to add an
  OpenAI-compatible endpoint -- the same file its Ollama / vLLM / LM Studio
  instructions use. That is a file we can write, so Pi gets a real one-click
  Connect.

  AGENT ZERO (agent0ai/agent-zero) is a Python WEB APP, not a CLI. Reading its
  models.py shows it talks to providers through LiteLLM and carries api_base /
  api_key on its ModelConfig -- so it speaks our OpenAI surface natively. But the
  endpoint is chosen in its own Settings UI, and a Docker install keeps that
  inside the container, so there is no file we can safely write. It gets
  detection and exact instructions, and autofix is None ON PURPOSE rather than a
  button that quietly does nothing.

The dishonest version of this feature would have been two identical cards with
Connect buttons. The difference between them is the whole point.
"""
import json
import os
from unittest import mock

import pytest

import app as A


def _entry(cid):
    return next(e for e in A.CLI_REGISTRY if e["id"] == cid)


# --------------------------------------------------------------------------- #
# Both are known to the hub
# --------------------------------------------------------------------------- #

def test_both_are_in_the_registry():
    ids = {e["id"] for e in A.CLI_REGISTRY}
    assert {"pi", "agent-zero"} <= ids


def test_both_speak_the_openai_surface():
    for cid in ("pi", "agent-zero"):
        assert _entry(cid)["kind"] == "openai", cid


def test_they_appear_on_the_connect_page():
    rows = {r["id"]: r for r in [A._cli_row(e) for e in A.CLI_REGISTRY]}
    assert "pi" in rows and "agent-zero" in rows


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

def test_pi_is_detected_by_its_binary():
    with mock.patch.object(A.shutil, "which",
                           side_effect=lambda b: "/usr/bin/pi" if b == "pi" else None):
        installed, path = A._cli_installed(_entry("pi"))
    assert installed and path == "/usr/bin/pi"


def test_pi_is_also_detected_by_its_config(tmp_path):
    """A pi installed somewhere off PATH still has ~/.pi/agent/models.json."""
    cfg = tmp_path / "models.json"
    cfg.write_text("{}", encoding="utf-8")
    entry = dict(_entry("pi"))
    entry["config_paths"] = [str(cfg)]
    with mock.patch.object(A.shutil, "which", return_value=None):
        installed, path = A._cli_installed(entry)
    assert installed and path == str(cfg)


def test_agent_zero_is_detected_by_its_checkout(tmp_path):
    """It is a web app, so the checkout IS the install."""
    (tmp_path / "models.py").write_text("", encoding="utf-8")
    (tmp_path / "agent.py").write_text("", encoding="utf-8")
    with mock.patch.object(A, "_p_agent_zero_roots", return_value=[str(tmp_path)]):
        installed, path = A._detect_agent_zero()
    assert installed and path == str(tmp_path)


def test_agent_zero_needs_both_marker_files(tmp_path):
    """models.py alone is far too common a filename to prove anything."""
    (tmp_path / "models.py").write_text("", encoding="utf-8")
    with mock.patch.object(A, "_p_agent_zero_roots", return_value=[str(tmp_path)]):
        assert A._detect_agent_zero() == (False, None)


def test_a_missing_agent_zero_says_what_it_is(tmp_path):
    """"Not installed (looked for: )" -- the empty-bins message -- tells nobody
    anything. It has no binary to look for."""
    with mock.patch.object(A, "_p_agent_zero_roots", return_value=[str(tmp_path)]):
        row = A._cli_row(_entry("agent-zero"))
    assert row["installed"] is False
    assert "web app" in row["detail"]
    assert "looked for: )" not in row["detail"]


# --------------------------------------------------------------------------- #
# Pi: the one-click connect
# --------------------------------------------------------------------------- #

@pytest.fixture
def pi_cfg(tmp_path, monkeypatch):
    path = tmp_path / ".pi" / "agent" / "models.json"
    monkeypatch.setattr(A, "_p_pi_models", lambda: str(path))
    entry = dict(_entry("pi"))
    entry["write_path"] = str(path)
    return path, entry


def _connect(entry):
    return A._autofix_pi(entry, "hub-key", "http://127.0.0.1:8787",
                         "http://127.0.0.1:8787/v1", "auto")


def test_connect_writes_a_provider_pi_understands(pi_cfg):
    path, entry = pi_cfg
    out = _connect(entry)
    assert out["ok"]
    data = json.loads(path.read_text(encoding="utf-8"))
    prov = data["providers"]["free-llm-hub"]
    assert prov["baseUrl"] == "http://127.0.0.1:8787/v1"
    assert prov["apiKey"] == "hub-key"
    # the one value Pi's docs pin for an OpenAI-compatible endpoint
    assert prov["api"] == "openai-completions"


def test_the_models_carry_what_pi_needs_to_load_them(pi_cfg):
    """Pi has no catalog for a provider it has never heard of: a model missing
    contextWindow or maxTokens either fails to load or is treated as tiny."""
    path, entry = pi_cfg
    _connect(entry)
    models = json.loads(path.read_text(encoding="utf-8"))["providers"]["free-llm-hub"]["models"]
    assert {m["id"] for m in models} == {"auto", "best", "swarm"}
    for m in models:
        assert m["contextWindow"] > 0 and m["maxTokens"] > 0
        assert m["name"] and m["input"]


def test_the_models_are_reported_as_free(pi_cfg):
    """Pi shows a running cost; anything but zero invents a bill."""
    path, entry = pi_cfg
    _connect(entry)
    models = json.loads(path.read_text(encoding="utf-8"))["providers"]["free-llm-hub"]["models"]
    assert all(m["cost"]["input"] == 0.0 and m["cost"]["output"] == 0.0 for m in models)


def test_connecting_keeps_the_users_other_providers(pi_cfg):
    """Additive, like every other writer here."""
    path, entry = pi_cfg
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"providers": {"ollama": {"baseUrl": "x"}},
                                "somethingElse": 1}), encoding="utf-8")
    _connect(entry)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["providers"]["ollama"] == {"baseUrl": "x"}
    assert data["somethingElse"] == 1


def test_connecting_twice_is_idempotent(pi_cfg):
    path, entry = pi_cfg
    _connect(entry)
    first = path.read_text(encoding="utf-8")
    _connect(entry)
    assert path.read_text(encoding="utf-8") == first


def test_an_unparseable_config_is_backed_up_not_silently_lost(pi_cfg):
    path, entry = pi_cfg
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    out = _connect(entry)
    assert out["ok"] and out["backup_path"]
    assert os.path.isfile(out["backup_path"])


def test_the_key_is_never_echoed_back(pi_cfg):
    _path, entry = pi_cfg
    out = _connect(entry)
    assert "hub-key" not in json.dumps(out["applied"])


def test_connect_tells_the_user_the_next_step(pi_cfg):
    _path, entry = pi_cfg
    assert "/model" in _connect(entry)["restart_hint"]


# --------------------------------------------------------------------------- #
# Pi: disconnect
# --------------------------------------------------------------------------- #

def test_disconnect_removes_only_our_provider(pi_cfg):
    path, entry = pi_cfg
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"providers": {"ollama": {"baseUrl": "x"}}}), encoding="utf-8")
    _connect(entry)
    A._disconnect_pi(entry)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "free-llm-hub" not in data["providers"]
    assert data["providers"]["ollama"] == {"baseUrl": "x"}


def test_disconnect_removes_a_file_that_was_only_ever_ours(pi_cfg):
    """An empty {} left behind is litter Pi parses on every start."""
    path, entry = pi_cfg
    _connect(entry)
    A._disconnect_pi(entry)
    assert not path.exists() or path.read_text(encoding="utf-8").strip() == "{}"


def test_disconnect_is_safe_when_we_were_never_connected(pi_cfg):
    _path, entry = pi_cfg
    out = A._disconnect_pi(entry)
    assert out["ok"] and out["reverted_path"] is None


def test_both_writers_are_registered():
    assert A._AUTOFIXERS["pi"] is A._autofix_pi
    assert A._DISCONNECTERS["pi"] is A._disconnect_pi


# --------------------------------------------------------------------------- #
# Agent Zero: honest about what it cannot do
# --------------------------------------------------------------------------- #

def test_agent_zero_has_no_autofix():
    """Its endpoint is set in its own Settings UI, and a Docker install keeps
    that inside the container. A Connect button here would do nothing."""
    assert _entry("agent-zero")["autofix"] is None


def test_agent_zero_says_exactly_where_to_put_the_values():
    hint = _entry("agent-zero")["hint"]
    assert "Settings" in hint and "Base URL" in hint


def test_agent_zero_is_marked_manual():
    assert _entry("agent-zero")["default_method"] == "manual"


def test_the_bearer_is_not_sent_twice(pi_cfg):
    """Pi documents authHeader for a provider that "expects Authorization:
    Bearer but doesn't use a standard API". We ARE the standard API, so the
    openai-completions client already sends it from apiKey."""
    path, entry = pi_cfg
    _connect(entry)
    prov = json.loads(path.read_text(encoding="utf-8"))["providers"]["free-llm-hub"]
    assert "authHeader" not in prov


def test_both_have_an_install_link():
    """They are the two entries most likely to show as not-installed, so the
    "install" link in that list is the whole card for now."""
    with open("templates/index.html", encoding="utf-8") as f:
        html = f.read()
    i = html.index("var CLI_SITES = {")
    block = html[i:i + 900]
    assert "'pi': 'https://pi.dev'" in block
    assert "'agent-zero': 'https://github.com/agent0ai/agent-zero'" in block
