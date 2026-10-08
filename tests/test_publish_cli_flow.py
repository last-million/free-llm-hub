"""Publishing from the CLIs and /agent: the agent-facing workflow (2026-10-08).

The agent finishes a web app, gives the LOCAL url, ASKS whether to also publish
it through a free Cloudflare tunnel, and only on a yes calls the hub tool
publish_start; it tells the user the url and when it expires; publish_renew
gives a new link; expiry closes it. This file covers the hub's half:

  * the four MCP tools (tools/list wording, tools/call glue, error codes);
  * the brief line craft.PUBLISH_ASK (only web/UI + tools, byte-identical
    otherwise, off with the `agent_publish` flag, never for a Multi helper,
    present in the Build brief file);
  * the log line (no url) and the kill switch.

The engine (publish.py) is another module: every test here talks to a FAKE one
placed in sys.modules that follows the documented contract. No tunnel is ever
started and nothing contacts Cloudflare.
"""
import json
import logging
import os
import sys
import types

import pytest

import agentic_chat
import app as A
import config
import craft
import hub_mcp
import swarm_windows

URL = "https://quiet-river-123.trycloudflare.com"
WEB_ASK = "build me a landing page for my bakery"
CODE_ASK = "refactor the auth module and add unit tests"


# --------------------------------------------------------------------------- #
# A fake engine that follows the contract
# --------------------------------------------------------------------------- #

class FakePublishError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class FakeEngine:
    def __init__(self):
        self.calls = []
        self.tunnels = []
        self.fail = {}                  # method -> (code, message)
        self.start_state = "live"       # state of the tunnel start() returns
        self.live_on_poll = True        # a "starting" tunnel goes live when polled
        self.cloudflared = {"available": True, "installable": True}
        self.n = 0

    def _maybe(self, method):
        if method in self.fail:
            code, msg = self.fail[method]
            raise FakePublishError(code, msg)

    def _tunnel(self, project_dir, port, state, source="agent", ttl=60):
        self.n += 1
        return {"id": "t%d" % self.n, "project_dir": project_dir, "port": port,
                "url": URL if state == "live" else None, "state": state,
                "error": None, "source": source, "started_at": 1000,
                "expires_at": "2026-10-08T12:00:00Z", "ttl_seconds": ttl * 60,
                "remaining_seconds": ttl * 60 - 30}

    def status(self, project_dir=None):
        self.calls.append(("status", project_dir))
        self._maybe("status")
        if self.live_on_poll:
            for t in self.tunnels:
                if t["state"] == "starting":
                    t.update(state="live", url=URL)
        return {"server_time": 1000,
                "cloudflared": dict(self.cloudflared),
                "tunnels": [dict(t) for t in self.tunnels
                            if project_dir is None or t["project_dir"] == project_dir],
                "limits": {"default_ttl_minutes": 60, "ttl_choices": [30, 60, 120],
                           "max_tunnels": 3}}

    def start(self, project_dir, port=None, ttl_minutes=None, source="agent"):
        self.calls.append(("start", project_dir, port, ttl_minutes, source))
        self._maybe("start")
        t = self._tunnel(project_dir, port, self.start_state, source,
                         ttl=ttl_minutes or 60)
        self.tunnels.append(t)
        return dict(t)

    def stop(self, tunnel_id):
        self.calls.append(("stop", tunnel_id))
        self._maybe("stop")
        for t in self.tunnels:
            if t["id"] == tunnel_id:
                t["state"] = "stopped"
                return dict(t)
        raise FakePublishError("not_found", "No such tunnel.")

    def renew(self, tunnel_id, ttl_minutes=None):
        self.calls.append(("renew", tunnel_id, ttl_minutes))
        self._maybe("renew")
        for t in self.tunnels:
            if t["id"] == tunnel_id:
                t.update(state="live", url=URL, ttl_seconds=(ttl_minutes or 60) * 60,
                         remaining_seconds=(ttl_minutes or 60) * 60 - 5)
                return dict(t)
        raise FakePublishError("not_found", "No such tunnel.")


@pytest.fixture
def engine(monkeypatch):
    eng = FakeEngine()
    mod = types.ModuleType("publish")
    mod.PublishError = FakePublishError
    mod.default = eng
    monkeypatch.setitem(sys.modules, "publish", mod)
    monkeypatch.setattr(A, "_PUBLISH_POLL_SECONDS", 0)
    monkeypatch.setattr(A, "_PUBLISH_WAIT_SECONDS", 1)
    return eng


def _flags(monkeypatch, **flags):
    real = config.get_flag
    monkeypatch.setattr(config, "get_flag",
                        lambda name, default=False: flags.get(name, real(name, default)))


@pytest.fixture
def proj(tmp_path):
    p = tmp_path / "bakery-site"
    p.mkdir()
    return str(p)


def rpc(name, **args):
    out, _status = hub_mcp.handle_rpc({
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": name, "arguments": args}})
    return out


def answer(name, **args):
    """(parsed tool text, isError) of a tools/call that returned a tool result."""
    out = rpc(name, **args)
    assert "error" not in out, out
    res = out["result"]
    return json.loads(res["content"][0]["text"]), bool(res.get("isError"))


# --------------------------------------------------------------------------- #
# 1. tools/list
# --------------------------------------------------------------------------- #

def _tools():
    out, _ = hub_mcp.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    return {t["name"]: t for t in out["result"]["tools"]}


def test_tools_list_has_the_four_publish_tools_with_the_consent_wording():
    tools = _tools()
    for name in ("publish_start", "publish_status", "publish_stop", "publish_renew"):
        assert name in tools, name
    for name in ("publish_start", "publish_renew"):
        d = tools[name]["description"]
        assert "CALL ONLY AFTER THE USER SAID YES" in d
        assert "Anyone with the link can open the app" in d
        assert "expires" in d
    assert tools["publish_start"]["inputSchema"]["required"] == ["port"]
    assert set(tools["publish_start"]["inputSchema"]["properties"]) == {
        "port", "project_dir", "ttl_minutes"}
    assert tools["publish_stop"]["inputSchema"]["required"] == ["id"]
    assert tools["publish_renew"]["inputSchema"]["required"] == ["id"]
    assert "cloudflared" in tools["publish_start"]["description"]


def test_publish_tools_are_hidden_when_not_wired(monkeypatch):
    monkeypatch.setattr(hub_mcp, "_PUBLISH", None)
    assert not [n for n in _tools() if n.startswith("publish_")]
    assert rpc("publish_start", port=3000)["error"]["code"] == -32603


def test_install_hub_registers_only_the_url_so_new_tools_arrive_by_themselves(monkeypatch):
    """Nothing enumerates tools in the entry the CLIs get: it is the /mcp URL, and
    tools/list is what a CLI reads. So the four tools reach every CLI that
    already has the hub entry, with no re-install."""
    seen = []
    monkeypatch.setattr(A.mcp_manager, "add_server",
                        lambda cli, name, spec, isolated=False:
                        seen.append((cli, name, dict(spec), isolated)) or (True, "ok"))
    A.app.config["TESTING"] = True
    h = {"X-Free-LLM-Hub-Token": config.get_setting("control_token") or "",
         "X-Free-LLM-Hub": "dashboard"}
    r = A.app.test_client().post("/api/mcp/install-hub", json={"cli": "codex"}, headers=h)
    assert r.status_code == 200
    assert seen and all(s[1] == "free-llm-hub" and s[2] == {"url": A._hub_mcp_url()}
                        for s in seen)
    assert "publish_start" in _tools()


def test_the_real_route_serves_the_tools(engine):
    A.app.config["TESTING"] = True
    r = A.app.test_client().post("/mcp", json={
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "publish_status", "arguments": {}}})
    assert r.status_code == 200
    body = json.loads(r.get_json()["result"]["content"][0]["text"])
    assert body["cloudflared_available"] is True and body["tunnels"] == []


# --------------------------------------------------------------------------- #
# 2. publish_start
# --------------------------------------------------------------------------- #

def test_start_publishes_with_source_agent_and_returns_url_and_expiry(engine, proj):
    out, is_err = answer("publish_start", port=3000, project_dir=proj, ttl_minutes=30)
    assert not is_err
    assert out["url"] == URL and out["state"] == "live" and out["port"] == 3000
    assert out["id"] == "t1"
    assert out["expires_at"] == "2026-10-08T12:00:00Z"   # passed through untouched
    assert out["expires_in"] == "about 29 minutes"
    assert out["project"] == "bakery-site"
    assert "Tell the user" in out["note"] and "Anyone with the link" in out["note"]
    assert "error" not in out
    assert engine.calls[0] == ("start", os.path.abspath(proj), 3000, 30, "agent")


def test_a_quoted_port_is_accepted(engine, proj):
    out, is_err = answer("publish_start", port="3000", project_dir=proj)
    assert not is_err and engine.calls[0][2] == 3000


def test_start_waits_for_a_starting_tunnel_to_go_live(engine, proj):
    engine.start_state = "starting"
    out, is_err = answer("publish_start", port=5173, project_dir=proj)
    assert not is_err and out["state"] == "live" and out["url"] == URL
    assert ("status", os.path.abspath(proj)) in engine.calls


def test_a_tunnel_that_is_still_starting_gives_no_link(engine, proj, monkeypatch):
    engine.start_state = "starting"
    engine.live_on_poll = False
    monkeypatch.setattr(A, "_PUBLISH_WAIT_SECONDS", 0)
    out, is_err = answer("publish_start", port=5173, project_dir=proj)
    assert not is_err and out["state"] == "starting"
    assert "url" not in out and "publish_status" in out["note"]


def test_a_failed_tunnel_is_an_error_with_no_link(engine, proj):
    engine.start_state = "starting"
    engine.live_on_poll = False
    orig = engine.status

    def failing_status(project_dir=None):
        for t in engine.tunnels:
            t.update(state="failed", error="cloudflared exited")
        return orig(project_dir)
    engine.status = failing_status
    out, is_err = answer("publish_start", port=5173, project_dir=proj)
    assert is_err and out["code"] == "failed" and "cloudflared exited" in out["error"]
    assert "url" not in out


def test_start_without_a_folder_asks_for_one(engine, monkeypatch):
    monkeypatch.setattr(A.workspace, "running", lambda: [])
    out, is_err = answer("publish_start", port=3000)
    assert is_err and out["code"] == "no_project" and "project_dir" in out["error"]
    assert not engine.calls


@pytest.mark.parametrize("folder", ["relative/dir", ".", "C:/definitely/not/a/dir/xyz"])
def test_start_refuses_a_folder_that_is_not_a_real_absolute_one(engine, folder):
    out, is_err = answer("publish_start", port=3000, project_dir=folder)
    assert is_err and out["code"] == "no_project"
    assert not engine.calls


def test_the_folder_is_guessed_from_the_preview_on_that_port(engine, proj, monkeypatch):
    monkeypatch.setattr(A.workspace, "running",
                        lambda: [{"project_dir": proj, "port": 4321}])
    out, is_err = answer("publish_start", port=4321)
    assert not is_err and out["url"] == URL
    assert engine.calls[0][1] == os.path.abspath(proj)


@pytest.mark.parametrize("code", [
    "no_cloudflared", "no_preview", "forbidden_port", "not_http", "too_many",
    "bad_ttl", "install_failed", "not_found", "already_published", "disabled"])
def test_every_engine_refusal_comes_back_as_error_and_code(engine, proj, code):
    engine.fail["start"] = (code, "Plain engine message for %s." % code)
    out, is_err = answer("publish_start", port=3000, project_dir=proj)
    assert is_err and out["code"] == code and out["error"]
    assert "url" not in out
    if code == "no_cloudflared":
        assert "Publish panel" in out["error"]      # tells the user where to install
    elif code == "disabled":
        assert out["error"] == "Plain engine message for disabled."   # passes through
    else:
        assert out["error"] == "Plain engine message for %s." % code


def test_already_published_names_the_running_tunnel(engine, proj):
    engine.start(os.path.abspath(proj), 3000)          # a live tunnel exists
    engine.fail["start"] = ("already_published", "Already published.")
    out, is_err = answer("publish_start", port=3000, project_dir=proj)
    assert is_err and out["code"] == "already_published"
    assert out["tunnel"]["url"] == URL and "publish_renew" in out["hint"]


def test_an_unexpected_engine_crash_is_a_plain_failure(engine, proj):
    def boom(*a, **k):
        raise RuntimeError("secret internals")
    engine.start = boom
    out, is_err = answer("publish_start", port=3000, project_dir=proj)
    assert is_err and out["code"] == "failed" and "secret internals" not in out["error"]


@pytest.mark.parametrize("args", [
    {}, {"port": 0}, {"port": 70000}, {"port": True}, {"port": "abc"},
    {"port": 3000, "ttl_minutes": "soon"}, {"port": 3000, "project_dir": 5}])
def test_bad_arguments_are_protocol_errors_and_touch_nothing(engine, args):
    assert rpc("publish_start", **args)["error"]["code"] == -32602
    assert not engine.calls


# --------------------------------------------------------------------------- #
# 3. status / stop / renew
# --------------------------------------------------------------------------- #

def test_status_lists_tunnels_and_cloudflared(engine, proj):
    engine.start(os.path.abspath(proj), 3000)
    out, is_err = answer("publish_status")
    assert not is_err and "error" not in out
    assert out["tunnels"][0]["url"] == URL and out["tunnels"][0]["expires_in"]
    assert out["cloudflared_available"] is True and out["max_tunnels"] == 3
    assert out["default_ttl_minutes"] == 60 and "note" not in out


def test_status_says_how_to_get_cloudflared_when_missing(engine):
    engine.cloudflared = {"available": False, "installable": True}
    out, _ = answer("publish_status")
    assert out["cloudflared_available"] is False and "Publish panel" in out["note"]


def test_status_filters_by_an_absolute_folder_only(engine, proj):
    answer("publish_status", project_dir=proj)
    answer("publish_status", project_dir="not/absolute")
    assert engine.calls == [("status", os.path.abspath(proj)), ("status", None)]


def test_a_failed_tunnel_in_status_reports_a_reason_not_an_error_key(engine, proj):
    engine.start(os.path.abspath(proj), 3000)
    engine.tunnels[0].update(state="failed", error="cloudflared exited")
    out, is_err = answer("publish_status")
    assert not is_err and out["tunnels"][0]["reason"] == "cloudflared exited"
    assert "error" not in out["tunnels"][0]


def test_stop_and_its_errors(engine, proj):
    engine.start(os.path.abspath(proj), 3000)
    out, is_err = answer("publish_stop", id="t1")
    assert not is_err and out == {"ok": True, "id": "t1", "state": "stopped"}
    out, is_err = answer("publish_stop", id="nope")
    assert is_err and out["code"] == "not_found"
    assert rpc("publish_stop")["error"]["code"] == -32602


def test_renew_returns_the_new_link_and_expiry(engine, proj):
    engine.start(os.path.abspath(proj), 3000)
    engine.tunnels[0].update(state="expired", url=None, remaining_seconds=0)
    out, is_err = answer("publish_renew", id="t1", ttl_minutes=120)
    assert not is_err and out["url"] == URL and out["state"] == "live"
    assert out["expires_in"] == "about 1 hour 59 min"
    assert "Tell the user" in out["note"]
    assert ("renew", "t1", 120) in engine.calls


def test_renew_errors(engine, proj):
    out, is_err = answer("publish_renew", id="ghost")
    assert is_err and out["code"] == "not_found"
    engine.fail["renew"] = ("bad_ttl", "That lifetime is not allowed.")
    out, is_err = answer("publish_renew", id="t1", ttl_minutes=99999)
    assert is_err and out["code"] == "bad_ttl"
    assert rpc("publish_renew")["error"]["code"] == -32602


# --------------------------------------------------------------------------- #
# 4. kill switch + missing engine
# --------------------------------------------------------------------------- #

def test_agent_publish_off_answers_disabled_and_touches_nothing(engine, proj, monkeypatch):
    _flags(monkeypatch, agent_publish=False)
    for name, args in (("publish_start", {"port": 3000, "project_dir": proj}),
                       ("publish_status", {}), ("publish_stop", {"id": "t1"}),
                       ("publish_renew", {"id": "t1"})):
        out, is_err = answer(name, **args)
        assert is_err and out["code"] == "disabled", name
    assert not engine.calls


def test_a_hub_without_the_engine_says_disabled(monkeypatch, proj):
    monkeypatch.setitem(sys.modules, "publish", None)        # import raises
    out, is_err = answer("publish_start", port=3000, project_dir=proj)
    assert is_err and out["code"] == "disabled"


# --------------------------------------------------------------------------- #
# 5. the hub.log trace: every agent start, never the url
# --------------------------------------------------------------------------- #

def test_the_log_line_names_the_project_and_port_but_never_the_url(engine, proj, caplog):
    with caplog.at_level(logging.INFO, logger="free-llm-hub"):
        answer("publish_start", port=3000, project_dir=proj)
        engine.fail["start"] = ("too_many", "Too many tunnels.")
        answer("publish_start", port=3001, project_dir=proj)
        engine.fail.clear()
        answer("publish_renew", id="t1")
        answer("publish_stop", id="t1")
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "[publish] agent started a tunnel for bakery-site port 3000" in text
    assert "[publish] agent start refused (too_many) for bakery-site port 3001" in text
    assert "trycloudflare" not in text and "quiet-river" not in text
    assert "https://" not in text


# --------------------------------------------------------------------------- #
# 6. the brief line
# --------------------------------------------------------------------------- #

def _expected_without_publish(text):
    """The brief as it was before PUBLISH_ASK existed (tool-carrying)."""
    hits = craft.match(text)
    tail = [craft.PLAN_PHASES] + ([craft.DESIGN_FIRST] if any(
        n == "web_design" for n, _ in hits) else []) + [craft.ACT_RUN, craft.VERIFY_RUN]
    return "\n\n".join([b for _n, b in hits] + tail)


def test_the_line_asks_first_and_names_the_tool():
    ask = craft.PUBLISH_ASK
    assert "Publish it online through a free Cloudflare tunnel" in ask
    assert "yes/no" in ask and "Nothing until yes" in ask
    assert "publish_start" in ask and "Build page" in ask
    assert len(ask) / 4 <= 70, "the line is ~%d tokens" % (len(ask) // 4)
    assert "publish_start" in _tools()          # the tool the line names exists


def test_the_line_ships_for_web_ui_with_tools():
    body = craft.system_message(WEB_ASK)["content"]
    assert craft.PUBLISH_ASK in body
    # the loop is still last (its back-references), and nothing else moved
    assert body.endswith(craft.VERIFY_RUN)
    assert body.replace(craft.PUBLISH_ASK + "\n\n", "") == _expected_without_publish(WEB_ASK)


def test_a_web_ask_with_no_domain_brief_still_gets_it():
    text = "build a todo web app with flask"
    assert craft.is_web_ui(text) and not craft.match(text)
    body = craft.system_message(text)["content"]
    assert body == "\n\n".join([craft.PUBLISH_ASK, craft.PLAN_PHASES, craft.ACT_RUN,
                                craft.VERIFY_RUN])


def test_everything_else_is_byte_identical():
    assert craft.PUBLISH_ASK not in craft.system_message(CODE_ASK)["content"]
    assert craft.system_message(CODE_ASK)["content"] == _expected_without_publish(CODE_ASK)
    # tool-less callers cannot call anything: no line, same as before
    web_tool_less = craft.system_message(WEB_ASK, tools=False)["content"]
    assert craft.PUBLISH_ASK not in web_tool_less
    assert craft.system_message("what is a closure?", tools=True)["content"] == \
        "\n\n".join([craft.PLAN_PHASES, craft.ACT_RUN, craft.VERIFY_RUN])
    assert craft.system_message("what is a closure?", tools=False) is None


def test_the_flag_takes_the_line_out_of_every_path(monkeypatch, tmp_path):
    _flags(monkeypatch, agent_publish=False)
    assert craft.PUBLISH_ASK not in craft.system_message(WEB_ASK)["content"]
    msgs = A._apply_craft_brief([{"role": "user", "content": WEB_ASK}], agentic=True)
    assert not any(craft.PUBLISH_ASK in str(m.get("content")) for m in msgs)
    name = agentic_chat.write_task_brief(str(tmp_path), WEB_ASK, session_id="s1")
    assert name and craft.PUBLISH_ASK not in (tmp_path / name).read_text(encoding="utf-8")
    # ...and the brief is otherwise the same as the flag-on one minus that line
    assert craft.system_message(WEB_ASK)["content"] == _expected_without_publish(WEB_ASK)


def test_cli_opening_turn_carries_it_only_for_web_with_tools():
    web = A._apply_craft_brief([{"role": "user", "content": WEB_ASK}], agentic=True)
    assert sum(craft.PUBLISH_ASK in str(m.get("content")) for m in web) == 1
    no_tools = A._apply_craft_brief([{"role": "user", "content": WEB_ASK}], agentic=False)
    assert not any(craft.PUBLISH_ASK in str(m.get("content")) for m in no_tools)
    code = A._apply_craft_brief([{"role": "user", "content": CODE_ASK}], agentic=True)
    assert not any(craft.PUBLISH_ASK in str(m.get("content")) for m in code)


def test_the_yes_turn_still_carries_the_instruction():
    """The user answers "yes" -- a NEW instruction with no domain of its own. The
    brief falls back to the instruction that opened the project, so the model
    that has to call publish_start sees the line again on that very turn."""
    msgs = [{"role": "user", "content": WEB_ASK},
            {"role": "assistant", "content": "Done: http://127.0.0.1:5173. Publish it "
                                             "online through a free Cloudflare tunnel? yes/no"},
            {"role": "user", "content": "yes"}]
    out = A._apply_craft_brief(msgs, agentic=True)
    assert sum(craft.PUBLISH_ASK in str(m.get("content")) for m in out) == 1


def test_a_multi_helper_never_asks_the_user(monkeypatch):
    monkeypatch.setattr(swarm_windows, "worker_info",
                        lambda sid: {"run_id": "r1"} if sid == "helper-1" else None)
    assert craft.PUBLISH_ASK not in craft.system_message(
        WEB_ASK, session_id="helper-1")["content"]
    assert craft.PUBLISH_ASK in craft.system_message(
        WEB_ASK, session_id="main-chat")["content"]


def test_the_build_brief_file_carries_it(tmp_path):
    name = agentic_chat.write_task_brief(str(tmp_path), WEB_ASK, session_id="s1")
    assert craft.PUBLISH_ASK in (tmp_path / name).read_text(encoding="utf-8")
    other = tmp_path / "other"
    other.mkdir()
    name = agentic_chat.write_task_brief(str(other), CODE_ASK, session_id="s1")
    assert craft.PUBLISH_ASK not in (other / name).read_text(encoding="utf-8")


def test_the_heaviest_request_with_the_line_stays_under_the_unchanged_ceiling():
    """test_craft_briefs.test_worst_case_brief_cost, spelled out for this feature:
    the saas landing page (the heaviest request) now carries the line and still
    sits under 32768 * 0.135 tokens. The ceiling did not move for it."""
    body = craft.system_message("create a landing page for my saas")["content"]
    assert craft.PUBLISH_ASK in body
    assert len(body) / 4 < 32768 * 0.135, "briefs cost ~%d tokens" % (len(body) // 4)
