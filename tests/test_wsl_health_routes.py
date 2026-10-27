"""The hub's WSL-health routes and the brief warning, hermetic (no real wsl.exe).

GET /api/wsl-health detects; POST /api/wsl-health/fix repairs under confirm. The agent brief for
a WSL project carries the warning so an agent never again guesses OneDrive sync or a quoting bug
(the live 2026-10-27 failure) for an unreachable /mnt/c.
"""
import json

import pytest

import app as A
import agent_servers
import wslfix


@pytest.fixture(autouse=True)
def _no_real_wsl(monkeypatch):
    monkeypatch.setattr(A, "_wsl_distros", lambda: ["Ubuntu"])
    agent_servers._WSL_HEALTH_CACHE.clear()
    yield
    agent_servers._WSL_HEALTH_CACHE.clear()


BROKEN = {"distro": "Ubuntu", "healthy": False, "reason": "automount_disabled",
          "mount_type": None, "automount_enabled": False, "interop_enabled": False}
HEALTHY = {"distro": "Ubuntu", "healthy": True, "reason": "ok", "mount_type": "9p",
           "automount_enabled": None, "interop_enabled": None}


def _headers():
    import config
    tok = config.get_control_token()
    h = {"X-Free-LLM-Hub": "dashboard"}
    if tok:
        h["X-Free-LLM-Hub-Token"] = tok
    return h


class _Client:
    def __init__(self):
        self.c = A.app.test_client()

    def get(self, path):
        return self.c.get(path, headers=_headers())

    def post(self, path, json=None):
        return self.c.post(path, json=json, headers=_headers())


def _client():
    return _Client()


# --- GET /api/wsl-health -----------------------------------------------------------------------

def test_get_reports_a_broken_distro_and_whether_it_is_fixable(monkeypatch):
    monkeypatch.setattr(wslfix, "diagnose", lambda d, **k: dict(BROKEN))
    monkeypatch.setattr(wslfix, "can_use_root", lambda d, **k: True)
    r = _client().get("/api/wsl-health")
    assert r.status_code == 200
    body = r.get_json()
    assert body["available"] is True
    row = body["distros"][0]
    assert row["healthy"] is False and row["reason"] == "automount_disabled"
    assert row["fixable"] is True


def test_get_a_healthy_distro_has_no_fixable_key(monkeypatch):
    monkeypatch.setattr(wslfix, "diagnose", lambda d, **k: dict(HEALTHY))
    row = _client().get("/api/wsl-health").get_json()["distros"][0]
    assert row["healthy"] is True and "fixable" not in row


def test_get_is_read_only(monkeypatch):
    # diagnose is the only wslfix call a GET may make; apply/can_use_root-as-fix must not run
    seen = []
    monkeypatch.setattr(wslfix, "diagnose", lambda d, **k: (seen.append("diagnose"), dict(HEALTHY))[1])
    monkeypatch.setattr(wslfix, "apply", lambda *a, **k: seen.append("apply") or {})
    _client().get("/api/wsl-health")
    assert "apply" not in seen


# --- POST /api/wsl-health/fix ------------------------------------------------------------------

def test_fix_needs_confirm(monkeypatch):
    called = []
    monkeypatch.setattr(wslfix, "apply", lambda *a, **k: called.append(1) or {"ok": True})
    r = _client().post("/api/wsl-health/fix", json={"distro": "Ubuntu"})
    assert r.status_code == 400 and r.get_json()["code"] == "confirm_required"
    assert not called                       # apply never ran


def test_fix_unknown_distro(monkeypatch):
    r = _client().post("/api/wsl-health/fix", json={"distro": "Nope", "confirm": True})
    assert r.status_code == 400 and r.get_json()["error"] == "unknown distro"


def test_fix_runs_apply_with_confirm_and_returns_the_result(monkeypatch):
    seen = {}

    def fake_apply(distro, confirm=False, **k):
        seen["distro"], seen["confirm"] = distro, confirm
        return {"ok": True, "before": BROKEN, "after": HEALTHY}
    monkeypatch.setattr(wslfix, "apply", fake_apply)
    r = _client().post("/api/wsl-health/fix", json={"distro": "Ubuntu", "confirm": True})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert seen == {"distro": "Ubuntu", "confirm": True}


def test_fix_without_root_is_200_with_a_paste_command(monkeypatch):
    monkeypatch.setattr(wslfix, "apply", lambda *a, **k: {
        "ok": False, "needs_manual": True, "code": "no_root",
        "command": "sudo sed -i -E \"s/^(\\s*enabled\\s*=\\s*)false/\\1true/\" /etc/wsl.conf && exit"})
    r = _client().post("/api/wsl-health/fix", json={"distro": "Ubuntu", "confirm": True})
    assert r.status_code == 200
    body = r.get_json()
    assert body["needs_manual"] is True and "/etc/wsl.conf" in body["command"]


def test_fix_hard_failure_is_500(monkeypatch):
    monkeypatch.setattr(wslfix, "apply", lambda *a, **k: {"ok": False, "code": "write_failed"})
    r = _client().post("/api/wsl-health/fix", json={"distro": "Ubuntu", "confirm": True})
    assert r.status_code == 500


# --- the brief warning -------------------------------------------------------------------------

def test_the_wsl_brief_carries_the_health_warning(monkeypatch):
    monkeypatch.setattr("wslpath.parse", lambda p: ("Ubuntu", "/home/hamza/proj"))
    monkeypatch.setattr(wslfix, "diagnose", lambda d, **k: dict(BROKEN))
    sec = agent_servers.wsl_section(r"\\wsl.localhost\Ubuntu\home\hamza\proj")
    assert "NOT the live Windows" in sec
    assert "sync" in sec.lower()                      # it tells the agent what it is NOT
    assert "## This project lives in WSL" in sec       # the normal brief still follows


def test_a_healthy_wsl_project_brief_has_no_warning(monkeypatch):
    monkeypatch.setattr("wslpath.parse", lambda p: ("Ubuntu", "/home/hamza/proj"))
    monkeypatch.setattr(wslfix, "diagnose", lambda d, **k: dict(HEALTHY))
    sec = agent_servers.wsl_section(r"\\wsl.localhost\Ubuntu\home\hamza\proj")
    assert "WSL MOUNT WARNING" not in sec and "## This project lives in WSL" in sec


def test_the_health_note_is_cached(monkeypatch):
    monkeypatch.setattr("wslpath.parse", lambda p: ("Ubuntu", "/home/hamza/proj"))
    calls = []
    monkeypatch.setattr(wslfix, "diagnose", lambda d, **k: calls.append(d) or dict(BROKEN))
    for _ in range(3):
        agent_servers.wsl_section(r"\\wsl.localhost\Ubuntu\home\hamza\proj")
    assert len(calls) == 1                             # TTL cache: diagnose ran once, not 3x


def test_the_brief_never_breaks_if_diagnose_raises(monkeypatch):
    monkeypatch.setattr("wslpath.parse", lambda p: ("Ubuntu", "/home/hamza/proj"))
    monkeypatch.setattr(wslfix, "diagnose", lambda d, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    sec = agent_servers.wsl_section(r"\\wsl.localhost\Ubuntu\home\hamza\proj")
    assert "## This project lives in WSL" in sec       # fails open: the brief is still produced


# --- the dashboard wiring (static) -------------------------------------------------------------

def _template():
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "templates", "index.html"), encoding="utf-8") as f:
        return f.read()


def test_the_settings_page_has_the_wsl_health_group():
    html = _template()
    assert 'id="wsl-health-group"' in html
    assert 'id="wsl-health-status"' in html and 'id="wsl-health-list"' in html
    # the group starts hidden and is only shown when a distro is broken
    assert 'id="wsl-health-group" hidden' in html
    # detect is loaded with the rest of the settings view; fix posts confirm:true
    assert "loadWslHealth(silent)" in html
    assert "'/api/wsl-health/fix'" in html and "confirm: true" in html
    # backend strings never go through innerHTML in this block
    block = html[html.index("function renderWslHealth"):html.index("/* ---------- Heartbeats")]
    assert ".innerHTML" not in block


def _fn(html, name):
    import re
    hits = list(re.finditer(r"(\n  function %s\(.*?\n  \}\n)" % re.escape(name), html, re.S))
    assert hits, "could not extract %s from the template" % name
    return hits[0].group(1)


def _render_in_node(distros):
    """Run the template's own renderWslHealth against a tiny fake DOM in node; return a summary
    of what it produced. Proves the function parses AND behaves, the way the repo's other
    dashboard-render tests do (not a raw node --check, which trips on template syntax)."""
    import json
    import os
    import shutil
    import subprocess
    import tempfile
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    html = _template()
    harness = """
    var buttons = [], textParts = [];
    function El(){ this.children = []; this.className=''; this._text='';
      Object.defineProperty(this, 'textContent', {get:function(){return this._text;},
        set:function(v){ this._text=v; if(v) textParts.push(v); }}); }
    El.prototype.appendChild = function(c){ this.children.push(c); };
    El.prototype.addEventListener = function(){ };
    var els = { 'wsl-health-group': new El(), 'wsl-health-list': new El(),
                'wsl-health-status': new El() };
    els['wsl-health-group'].hidden = true;
    global.$ = function(sel){ return els[sel.replace('#','')] || null; };
    global.document = { createElement: function(t){ var e = new El();
      if (t === 'button') buttons.push(e); if (t === 'button') e._isbtn = true; return e; },
      createTextNode: function(t){ if(t) textParts.push(t); return {textContent: t, _text: t}; } };
    global.toast = function(){ };
    global.api = function(){ return { then: function(){ return { catch: function(){} }; } }; };
    %s
    renderWslHealth({ distros: DISTROS });
    console.log(JSON.stringify({
      hidden: els['wsl-health-group'].hidden,
      status: els['wsl-health-status']._text,
      buttons: buttons.map(function(b){ return b._text; }),
      text: textParts.join(' | ')
    }));
    """ % _fn(html, "renderWslHealth")
    src = "var DISTROS = %s;\n%s" % (json.dumps(distros), harness)
    tmp = tempfile.mkdtemp(prefix="hub-pytest-")
    try:
        js = os.path.join(tmp, "r.js")
        with open(js, "w", encoding="utf-8") as fh:
            fh.write(src)
        out = subprocess.run([node, js], capture_output=True, text=True, timeout=30)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_render_shows_a_fix_button_for_a_fixable_distro():
    r = _render_in_node([dict(BROKEN, fixable=True)])
    assert r["hidden"] is False
    assert r["buttons"] == ["Fix it"]
    assert "Ubuntu" in r["text"]


def test_the_render_shows_no_button_when_not_fixable():
    r = _render_in_node([dict(BROKEN, fixable=False)])
    assert r["hidden"] is False and r["buttons"] == []
    assert "root is not reachable" in r["text"]


def test_the_render_hides_the_group_when_all_healthy():
    r = _render_in_node([dict(HEALTHY)])
    assert r["hidden"] is True
