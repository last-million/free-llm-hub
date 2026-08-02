"""claude and codex, isolated AND running on the hub's own free models --
no subscription needed, exactly like opencode already works.

"remember that in /agent all cli's should work isolated and with our local
hub llm of course man" -- the isolated copies used to be dead weight without
a real subscription login. Now an isolated copy with no login runs against
THIS hub instead, the same shape opencode already had (_seed_opencode_config).
Signing in (the button built the previous turn) remains available for anyone
who wants their own subscription's stronger models instead of the free tier
-- once real credentials appear, the hub fallback gets out of the way on its
own.

Verified live, not just unit-tested: a genuinely unauthenticated isolated
claude AND codex both wrote a real file through a full agentic turn, routed
through this hub, with zero login -- codex took ~5 minutes (free-tier model
latency on a tool-use turn, not a hang; polled to completion and confirmed).
"""
import os
import shutil
import tempfile

import pytest

import agentic_chat as ac


@pytest.fixture
def cfg_home():
    d = tempfile.mkdtemp(prefix="hubfallback-")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------- #
# claude -- env vars, conditional on NOT being signed in
# --------------------------------------------------------------------------- #

def test_claude_falls_back_to_the_hub_when_not_signed_in(monkeypatch):
    monkeypatch.setattr(ac, "_isolated_signed_in", lambda cli: False)
    monkeypatch.setattr(ac, "_port", lambda: 8787)
    monkeypatch.setattr(ac.config, "get_local_api_key", lambda: "my-local-key")
    env = {}
    ac._apply_claude_hub_fallback(env, "/unused")
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8787"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "my-local-key"
    assert env["ANTHROPIC_MODEL"] == "auto"


def test_claude_never_overrides_a_real_sign_in(monkeypatch):
    """ANTHROPIC_BASE_URL/AUTH_TOKEN override stored credentials whenever
    Claude Code sees them -- setting them unconditionally would make a real
    sign-in silently useless forever."""
    monkeypatch.setattr(ac, "_isolated_signed_in", lambda cli: True)
    env = {"PATH": "/usr/bin"}
    ac._apply_claude_hub_fallback(env, "/unused")
    assert "ANTHROPIC_BASE_URL" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env


def test_claude_gets_a_usable_token_even_with_no_local_key_configured(monkeypatch):
    monkeypatch.setattr(ac, "_isolated_signed_in", lambda cli: False)
    monkeypatch.setattr(ac.config, "get_local_api_key", lambda: None)
    env = {}
    ac._apply_claude_hub_fallback(env, "/unused")
    assert env["ANTHROPIC_AUTH_TOKEN"]                      # non-empty, hub accepts it open


# --------------------------------------------------------------------------- #
# codex -- config.toml, additive, must survive codex's own bookkeeping
# --------------------------------------------------------------------------- #

# The exact shape codex writes on its own, unprompted, the first time it runs
# in any directory -- confirmed live. Nothing here is provider configuration.
_REAL_CODEX_BOOKKEEPING = (
    "[projects.'c:\\users\\hamza\\calvoun-projects\\demo']\n"
    'trust_level = "trusted"\n'
)


def test_codex_activates_even_though_its_own_config_already_exists(cfg_home, monkeypatch):
    """The actual bug, found live: config.toml is essentially NEVER empty by
    the time this runs (codex writes trust bookkeeping on first use in any
    directory), and treating "the file exists" as "do not touch it" meant the
    fallback silently never activated after the very first codex invocation
    on the machine."""
    monkeypatch.setattr(ac, "_isolated_signed_in", lambda cli: False)
    monkeypatch.setattr(ac, "_hub_base_url", lambda: "http://127.0.0.1:8787")
    monkeypatch.setattr(ac.config, "get_local_api_key", lambda: None)
    path = os.path.join(cfg_home, "config.toml")
    open(path, "w").write(_REAL_CODEX_BOOKKEEPING)

    ac._apply_codex_hub_fallback(cfg_home)

    text = open(path, encoding="utf-8").read()
    assert 'model_provider = "freehub"' in text
    assert "[model_providers.freehub]" in text
    assert "[projects.'c:\\users\\hamza\\calvoun-projects\\demo']" in text, (
        "codex's own trust entry must survive")
    assert 'trust_level = "trusted"' in text


def test_codex_never_overwrites_a_real_provider_choice(cfg_home, monkeypatch):
    monkeypatch.setattr(ac, "_isolated_signed_in", lambda cli: False)
    real = 'model_provider = "openai"\nmodel = "gpt-5"\n'
    path = os.path.join(cfg_home, "config.toml")
    open(path, "w").write(real)

    ac._apply_codex_hub_fallback(cfg_home)

    assert open(path, encoding="utf-8").read() == real


def test_codex_fallback_is_idempotent(cfg_home, monkeypatch):
    """_agentic_env runs this on EVERY turn while not signed in -- repeated
    application must not accumulate duplicate lines. Measured: it did, once,
    before the marker placement was fixed."""
    monkeypatch.setattr(ac, "_isolated_signed_in", lambda cli: False)
    monkeypatch.setattr(ac, "_hub_base_url", lambda: "http://127.0.0.1:8787")
    path = os.path.join(cfg_home, "config.toml")
    open(path, "w").write(_REAL_CODEX_BOOKKEEPING)

    ac._apply_codex_hub_fallback(cfg_home)
    first = open(path, encoding="utf-8").read()
    ac._apply_codex_hub_fallback(cfg_home)
    second = open(path, encoding="utf-8").read()

    assert first == second
    assert first.count("[model_providers.freehub]") == 1


def test_codex_fallback_reverts_cleanly_once_signed_in(cfg_home, monkeypatch):
    """A real sign-in must take over -- and codex's own bookkeeping must
    survive the revert, not just the initial apply."""
    monkeypatch.setattr(ac, "_isolated_signed_in", lambda cli: False)
    monkeypatch.setattr(ac, "_hub_base_url", lambda: "http://127.0.0.1:8787")
    path = os.path.join(cfg_home, "config.toml")
    open(path, "w").write(_REAL_CODEX_BOOKKEEPING)
    ac._apply_codex_hub_fallback(cfg_home)
    assert "freehub" in open(path, encoding="utf-8").read()

    monkeypatch.setattr(ac, "_isolated_signed_in", lambda cli: True)
    ac._apply_codex_hub_fallback(cfg_home)

    text = open(path, encoding="utf-8").read()
    assert "freehub" not in text
    assert ac._CODEX_HUB_MARKER not in text
    assert "[projects.'c:\\users\\hamza\\calvoun-projects\\demo']" in text
    assert 'trust_level = "trusted"' in text


def test_reverting_when_we_never_applied_anything_is_a_no_op(cfg_home, monkeypatch):
    monkeypatch.setattr(ac, "_isolated_signed_in", lambda cli: True)
    path = os.path.join(cfg_home, "config.toml")
    open(path, "w").write(_REAL_CODEX_BOOKKEEPING)
    ac._apply_codex_hub_fallback(cfg_home)
    assert open(path, encoding="utf-8").read() == _REAL_CODEX_BOOKKEEPING


def test_bearer_token_is_embedded_when_the_hub_has_a_local_key(cfg_home, monkeypatch):
    monkeypatch.setattr(ac, "_isolated_signed_in", lambda cli: False)
    monkeypatch.setattr(ac, "_hub_base_url", lambda: "http://127.0.0.1:8787")
    monkeypatch.setattr(ac.config, "get_local_api_key", lambda: "shh-secret")
    ac._apply_codex_hub_fallback(cfg_home)
    text = open(os.path.join(cfg_home, "config.toml"), encoding="utf-8").read()
    assert 'experimental_bearer_token = "shh-secret"' in text


def test_no_bearer_token_written_when_the_hub_is_open(cfg_home, monkeypatch):
    monkeypatch.setattr(ac, "_isolated_signed_in", lambda cli: False)
    monkeypatch.setattr(ac, "_hub_base_url", lambda: "http://127.0.0.1:8787")
    monkeypatch.setattr(ac.config, "get_local_api_key", lambda: None)
    ac._apply_codex_hub_fallback(cfg_home)
    text = open(os.path.join(cfg_home, "config.toml"), encoding="utf-8").read()
    assert "experimental_bearer_token" not in text


def test_a_brand_new_empty_config_home_still_works(cfg_home, monkeypatch):
    """No config.toml at all yet (a truly fresh isolated install)."""
    monkeypatch.setattr(ac, "_isolated_signed_in", lambda cli: False)
    monkeypatch.setattr(ac, "_hub_base_url", lambda: "http://127.0.0.1:8787")
    ac._apply_codex_hub_fallback(cfg_home)
    text = open(os.path.join(cfg_home, "config.toml"), encoding="utf-8").read()
    assert 'model_provider = "freehub"' in text


# --------------------------------------------------------------------------- #
# Wired into the actual env-building path
# --------------------------------------------------------------------------- #

def test_agentic_env_applies_the_claude_fallback(monkeypatch):
    monkeypatch.setattr(ac, "_isolated_bin", lambda cli: "/hub/copy/" + cli)
    monkeypatch.setattr(ac, "_isolated_signed_in", lambda cli: False)
    monkeypatch.setattr(ac, "_hub_base_url", lambda: "http://127.0.0.1:8787")
    env = ac._agentic_env("claude", project_dir="/some/project")
    assert env.get("ANTHROPIC_BASE_URL") == "http://127.0.0.1:8787"


def test_agentic_env_applies_the_codex_fallback(monkeypatch, cfg_home):
    monkeypatch.setattr(ac, "_isolated_bin", lambda cli: "/hub/copy/" + cli)
    monkeypatch.setattr(ac, "_isolated_config_dir", lambda cli: cfg_home)
    monkeypatch.setattr(ac, "_isolated_signed_in", lambda cli: False)
    monkeypatch.setattr(ac, "_hub_base_url", lambda: "http://127.0.0.1:8787")
    ac._agentic_env("codex", project_dir="/some/project")
    text = open(os.path.join(cfg_home, "config.toml"), encoding="utf-8").read()
    assert "freehub" in text


def test_opencode_still_uses_its_own_seed_not_the_new_ones(monkeypatch, cfg_home):
    """The three CLIs must not cross-wire -- opencode keeps its existing,
    already-working seed path."""
    monkeypatch.setattr(ac, "_isolated_bin", lambda cli: "/hub/copy/" + cli)
    monkeypatch.setattr(ac, "_isolated_config_dir", lambda cli: cfg_home)
    seeded = []
    monkeypatch.setattr(ac, "_seed_opencode_config", lambda p: seeded.append(p))
    ac._agentic_env("opencode", project_dir="/some/project")
    assert seeded == [cfg_home]
