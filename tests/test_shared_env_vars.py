"""A shared environment variable is not a CLI's connection.

REPORTED 2026-09-27: after Disconnect on OpenCode the dashboard said
"Disconnected in config — but an env var still points OpenCode at the hub".
The user's persistent environment (HKCU\\Environment) held OPENAI_BASE_URL and
LLM_BASE_URL = http://127.0.0.1:8787 -- read by EVERY OpenAI-shaped tool --
while OpenCode's free-llm-hub provider block (the only thing that shows the
hub's models/efforts/modes in OpenCode) had been removed correctly.

Now:
  * a config-wired CLI is connected only by its own config file; a shared var
    is a note naming the tools that share it (env-only CLIs such as aider keep
    the var as their connection);
  * the hub records which connectors rely on each persistent var (seeded
    conservatively for vars written before tracking existed);
  * Disconnect removes a var only when no other still-connected tool relies on
    it, and names who does otherwise;
  * /api/env/remove removes ONE hub-pointing var on explicit request, previewing
    the tools it reaches first, and refuses anything not pointing at the hub.

The persistent environment is the in-memory userenv.MemoryBackend installed by
the root conftest.py for every test -- the real registry is never touched.
"""
import os
import tempfile

import pytest

import app
import config
import userenv

HUB_V1 = "http://127.0.0.1:%d/v1" % app.PORT
VARS = ("OPENAI_BASE_URL", "OPENAI_API_BASE", "ANTHROPIC_BASE_URL", "LLM_BASE_URL")


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Fake persistent env + a fresh hub config + control over which CLIs are
    'installed' and what their config files say."""
    backend = userenv.backend()
    assert isinstance(backend, userenv.MemoryBackend), "tests must never see the real registry"
    for v in VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", str(tmp_path / "hubcfg" / "config.json"))
    installed = set()
    monkeypatch.setattr(app, "_cli_installed",
                        lambda e: (e.get("id") in installed, "/bin/" + str(e.get("id"))))
    app._installed_memo.clear()
    # Every registry CLI gets an empty private config file: nothing is wired
    # through config unless a test says so.
    cfg = {}
    for e in app.CLI_REGISTRY:
        p = tmp_path / ("%s.cfg" % e["id"])
        p.write_text("{}\n", encoding="utf-8")
        monkeypatch.setitem(e, "config_paths", [str(p)])
        cfg[e["id"]] = p
    monkeypatch.setattr(app, "_mark_hub_mode_unmanaged", lambda: None)
    monkeypatch.setattr(app, "_hub_mcp_kept", lambda cid: None)

    class Env:
        pass
    E = Env()
    E.backend, E.installed, E.cfg, E.mp = backend, installed, cfg, monkeypatch

    def wire(cid):
        cfg[cid].write_text('{"provider": {"free-llm-hub": {"options": {"baseURL": "%s"}}}}\n'
                            % HUB_V1, encoding="utf-8")

    def unwire(cid):
        cfg[cid].write_text("{}\n", encoding="utf-8")

    def fake_reverter(cid):
        def revert(entry):
            unwire(cid)
            return {"wrote_path": str(cfg[cid]), "restored_from_backup": False}
        return revert

    E.wire, E.unwire = wire, unwire
    for strategy in ("opencode", "aider", "qwen", "claude"):
        monkeypatch.setitem(app._DISCONNECTERS, strategy, fake_reverter(strategy))
    return E


def _client():
    return app.app.test_client()


def _headers():
    return {"X-Free-LLM-Hub": "dashboard",
            "X-Free-LLM-Hub-Token": config.ensure_control_token()}


def _entry(cid):
    return app._CLI_BY_ID[cid]


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #

def test_opencode_is_not_connected_by_a_shared_var_and_says_why(env):
    env.backend.set("OPENAI_BASE_URL", HUB_V1)
    env.installed.update({"opencode", "aider"})
    row = app._cli_row(_entry("opencode"))
    assert row["connected"] is False
    assert [v["name"] for v in row["env_vars"]] == ["OPENAI_BASE_URL"]
    assert row["env_vars"][0]["scope"] == "user"
    assert row["env_vars"][0]["shared_by"] == ["Aider"]
    note = row["env_note"]
    assert "OPENAI_BASE_URL in your user environment still points at the hub" in note
    assert "shared by Aider" in note
    assert "OpenCode's built-in OpenAI provider would also use it" in note


def test_opencode_config_is_still_the_connection(env):
    env.backend.set("OPENAI_BASE_URL", HUB_V1)
    env.installed.add("opencode")
    env.wire("opencode")
    connected, method, _detail = app._cli_connected(_entry("opencode"))
    assert (connected, method) == (True, "config")
    assert "env_note" not in app._cli_row(_entry("opencode"))


def test_env_only_cli_is_still_connected_by_the_var(env):
    env.backend.set("OPENAI_BASE_URL", HUB_V1)
    connected, method, detail = app._cli_connected(_entry("aider"))
    assert (connected, method) == (True, "env")
    assert "OPENAI_BASE_URL" in detail


def test_env_only_cli_session_var_still_counts(env):
    env.mp.setenv("OPENAI_BASE_URL", HUB_V1)       # set in the hub's own env only
    assert app._cli_connected(_entry("llm"))[:2] == (True, "env")
    assert app._env_scope("OPENAI_BASE_URL")[0] == "session"


def test_a_var_pointing_elsewhere_is_nothing(env):
    env.backend.set("OPENAI_BASE_URL", "https://api.openai.com/v1")
    env.installed.add("opencode")
    assert app._cli_connected(_entry("aider"))[0] is False
    assert "env_vars" not in app._cli_row(_entry("opencode"))


# --------------------------------------------------------------------------- #
# Ownership
# --------------------------------------------------------------------------- #

def test_a_pre_existing_var_is_seeded_with_every_reader(env):
    owners = app._env_owners("OPENAI_BASE_URL")
    readers = {e["id"] for e in app.CLI_REGISTRY if "OPENAI_BASE_URL" in e.get("env_check", [])}
    assert set(owners) == readers
    assert {"opencode", "aider", "qwen", "llm", "hermes"} <= readers
    rec = config.get_json(app._ENV_OWNERS_KEY)["OPENAI_BASE_URL"]
    assert rec["source"] == "inferred"


def test_handing_out_setx_records_the_owner(env):
    config.set_json(app._ENV_OWNERS_KEY, {"OPENAI_BASE_URL": {"owners": ["opencode"],
                                                              "source": "recorded"}})
    r = _client().get("/api/clis/aider/instructions", headers=_headers())
    assert r.status_code == 200
    assert "setx OPENAI_BASE_URL" in r.get_json()["commands"]["windows"]
    led = config.get_json(app._ENV_OWNERS_KEY)
    assert set(led["OPENAI_BASE_URL"]["owners"]) == {"opencode", "aider"}
    assert "aider" in led["OPENAI_API_BASE"]["owners"]


# --------------------------------------------------------------------------- #
# Disconnect
# --------------------------------------------------------------------------- #

def test_disconnect_keeps_a_var_another_connected_cli_uses(env):
    env.backend.set("OPENAI_BASE_URL", HUB_V1)
    env.installed.update({"opencode", "aider"})
    env.wire("opencode")
    r = _client().post("/api/clis/opencode/disconnect", headers=_headers())
    body = r.get_json()
    assert body["ok"] is True
    assert body["connected"] is False and "still_connected" not in body
    assert env.backend.get("OPENAI_BASE_URL") == HUB_V1          # kept
    kept = body["env"]["kept"]
    assert [k["name"] for k in kept] == ["OPENAI_BASE_URL"]
    assert kept[0]["used_by"] == ["Aider"]
    assert body["env"]["removed"] == []
    assert "Terminals that are already open keep the old value" in body["env_note"]
    assert "OpenCode's built-in OpenAI provider would also use it" in body["env_note"]
    owners = config.get_json(app._ENV_OWNERS_KEY)["OPENAI_BASE_URL"]["owners"]
    assert "opencode" not in owners and "aider" in owners


def test_disconnect_of_the_last_owner_removes_the_var(env):
    env.backend.set("OPENAI_BASE_URL", HUB_V1)
    env.mp.setenv("OPENAI_BASE_URL", HUB_V1)        # the hub process inherited it too
    env.installed.update({"opencode", "aider"})
    env.wire("opencode")
    _client().post("/api/clis/opencode/disconnect", headers=_headers())
    assert env.backend.get("OPENAI_BASE_URL") == HUB_V1          # aider still relies on it
    body = _client().post("/api/clis/aider/disconnect", headers=_headers()).get_json()
    assert body["env"]["removed"] == ["OPENAI_BASE_URL"]
    assert env.backend.get("OPENAI_BASE_URL") is None
    assert "OPENAI_BASE_URL" in env.backend.deleted
    assert "OPENAI_BASE_URL" not in os.environ               # status right without a restart
    assert body["connected"] is False
    assert "Terminals that are already open keep the old value" in body["note"]
    assert "OPENAI_BASE_URL" not in (config.get_json(app._ENV_OWNERS_KEY) or {})


def test_disconnect_never_removes_a_var_pointing_elsewhere(env):
    env.backend.set("OPENAI_BASE_URL", "https://api.openai.com/v1")
    env.installed.add("opencode")
    env.wire("opencode")
    body = _client().post("/api/clis/opencode/disconnect", headers=_headers()).get_json()
    assert "env" not in body
    assert env.backend.get("OPENAI_BASE_URL") == "https://api.openai.com/v1"
    assert env.backend.deleted == []


def test_disconnect_with_no_shared_var_is_a_plain_success(env):
    env.installed.add("opencode")
    env.wire("opencode")
    body = _client().post("/api/clis/opencode/disconnect", headers=_headers()).get_json()
    assert body["ok"] is True and body["connected"] is False
    assert "env" not in body and "still_connected" not in body


# --------------------------------------------------------------------------- #
# Explicit removal endpoint
# --------------------------------------------------------------------------- #

def test_remove_endpoint_previews_then_removes(env):
    env.backend.set("OPENAI_BASE_URL", HUB_V1)
    env.installed.update({"opencode", "aider"})
    r = _client().post("/api/env/remove", json={"name": "OPENAI_BASE_URL"}, headers=_headers())
    body = r.get_json()
    assert r.status_code == 200 and body["dry_run"] is True and body["removed"] is False
    assert {a["name"] for a in body["affects"]} == {"OpenCode", "Aider"}
    assert env.backend.get("OPENAI_BASE_URL") == HUB_V1          # preview removes nothing
    r = _client().post("/api/env/remove", json={"name": "OPENAI_BASE_URL", "confirm": True},
                       headers=_headers())
    body = r.get_json()
    assert body["ok"] is True and body["removed"] is True
    assert env.backend.get("OPENAI_BASE_URL") is None
    assert "keep the old value" in body["note"]


def test_remove_endpoint_handles_a_var_no_cli_reads(env):
    env.backend.set("LLM_BASE_URL", "http://127.0.0.1:%d" % app.PORT)
    listing = _client().get("/api/env/hub-vars", headers=_headers()).get_json()
    assert "LLM_BASE_URL" in [v["name"] for v in listing["vars"]]
    r = _client().post("/api/env/remove", json={"name": "LLM_BASE_URL", "confirm": True},
                       headers=_headers())
    assert r.get_json()["removed"] is True
    assert env.backend.get("LLM_BASE_URL") is None


def test_remove_endpoint_refuses_a_value_not_pointing_at_the_hub(env):
    env.backend.set("OPENAI_BASE_URL", "https://api.openai.com/v1")
    r = _client().post("/api/env/remove", json={"name": "OPENAI_BASE_URL", "confirm": True},
                       headers=_headers())
    assert r.status_code == 409
    assert env.backend.get("OPENAI_BASE_URL") == "https://api.openai.com/v1"
    assert env.backend.deleted == []


def test_remove_endpoint_refuses_protected_invalid_and_missing_names(env):
    env.backend.set("PATH", "C:\\x;http://127.0.0.1:%d" % app.PORT)
    for name, code in (("PATH", 400), ("BAD NAME", 400), ("", 400), ("NOT_SET_VAR", 404)):
        r = _client().post("/api/env/remove", json={"name": name, "confirm": True},
                           headers=_headers())
        assert r.status_code == code, name
    assert env.backend.get("PATH") is not None
    assert env.backend.deleted == []


def test_remove_endpoint_is_control_token_gated(env):
    env.backend.set("OPENAI_BASE_URL", HUB_V1)
    config.ensure_control_token()
    r = _client().post("/api/env/remove", json={"name": "OPENAI_BASE_URL", "confirm": True},
                       headers={"X-Free-LLM-Hub": "dashboard"})
    assert r.status_code == 401
    r = _client().get("/api/env/hub-vars")
    assert r.status_code == 401
    assert env.backend.get("OPENAI_BASE_URL") == HUB_V1


def test_dashboard_shows_the_env_modal_not_the_misleading_toast():
    html = open(os.path.join(os.path.dirname(app.__file__), "templates", "index.html"),
                encoding="utf-8").read()
    assert "an env var still points ' + name + ' at the hub" not in html
    assert "function openEnvModal(" in html
    assert "'Remove ' + name + ' from my user environment'" in html
    # The preview (tools it reaches) comes back before the confirmed removal.
    i = html.index("function envRemoveControl(")
    body = html[i:html.index("function envKeptBlock(")]
    assert body.index("body: { name: name } }") < body.index("confirm: true")
    assert "esc(a.name)" in body and "esc(name)" in body


def test_userenv_memory_backend_is_case_insensitive_like_windows():
    b = userenv.MemoryBackend({"Openai_Base_Url": "x"})
    assert b.get("OPENAI_BASE_URL") == "x"
    b.delete("openai_base_url")
    assert b.get("OPENAI_BASE_URL") is None
    assert not userenv.valid_name("A B") and userenv.is_protected("path")
