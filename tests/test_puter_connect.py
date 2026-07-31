"""Tests for the zero-manual Puter connect flow (browser-side "path A").

The dashboard puter card's "Connect with Puter" button replicates puter.js
v2's own popup sign-in contract (verified live 2026-07-31 against
https://js.puter.com/v2/): the user logs in on puter.com itself, the Puter GUI
postMessages {msg:"puter.token", msg_id, token, success:true} to the opener,
and the dashboard saves that token through the EXISTING provider-keys
endpoint (POST /api/providers/puter/keys). There is deliberately NO new
backend endpoint and no server-side Puter call to monkeypatch — the hub never
sees a username or password, so "no password in any returned payload" holds
by construction and is asserted below for the token itself.

Covered here: the endpoint the flow relies on (guard behavior, key saved,
auto-enable, dedupe, no secret echoed), the frontend contract (button hook,
origin check, message shape), and node --check over every inline script
block of templates/index.html.
"""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

import app
import config

_DASH = {"X-Free-LLM-Hub": "dashboard"}
_TOKEN = "puter-test-jwt.abc123.def456"

INDEX_HTML = Path(app.__file__).resolve().parent / "templates" / "index.html"


@pytest.fixture
def isolated_config(monkeypatch):
    # tempfile.mkdtemp, NOT pytest's tmp_path: this machine's pytest temp root
    # (Temp/pytest-of-hamza) is permission-broken (known environmental issue,
    # ~238 suite errors) while plain mkdtemp works fine.
    path = Path(tempfile.mkdtemp()) / "state" / "config.json"
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", str(path))
    return path


def _auth_headers():
    return dict(_DASH, **{"X-Free-LLM-Hub-Token": config.ensure_control_token()})


# --------------------------------------------------------------------------- #
# Backend: the endpoint the connect flow POSTs the captured token to
# --------------------------------------------------------------------------- #

def test_keys_endpoint_rejects_missing_dashboard_header(isolated_config):
    client = app.app.test_client()
    resp = client.post("/api/providers/puter/keys", json={"api_key": _TOKEN})
    assert resp.status_code == 403


def test_keys_endpoint_rejects_missing_control_token(isolated_config):
    config.ensure_control_token()   # guard only engages once a token exists
    client = app.app.test_client()
    resp = client.post("/api/providers/puter/keys", json={"api_key": _TOKEN},
                       headers=_DASH)
    assert resp.status_code == 401
    assert resp.get_json().get("code") == "token_required"


def test_keys_endpoint_rejects_wrong_control_token(isolated_config):
    config.ensure_control_token()
    client = app.app.test_client()
    headers = dict(_DASH, **{"X-Free-LLM-Hub-Token": "not-the-token"})
    resp = client.post("/api/providers/puter/keys", json={"api_key": _TOKEN},
                       headers=headers)
    assert resp.status_code == 401


def test_connect_saves_token_and_auto_enables(isolated_config):
    client = app.app.test_client()
    resp = client.post("/api/providers/puter/keys", json={"api_key": _TOKEN},
                       headers=_auth_headers())
    assert resp.status_code == 200
    # The captured token lands in the puter key pool via the standard machinery…
    assert config.list_provider_keys("puter") == [_TOKEN]
    # …and the provider is auto-enabled, exactly like a manual key save.
    assert config.get_provider_config("puter").get("enabled") is True
    body = resp.get_json()
    assert body["id"] == "puter"
    assert body["has_key"] is True
    assert body["key_count"] == 1


def test_connect_dedupes_repeated_token(isolated_config):
    # Clicking Connect twice with the same account must not grow the pool.
    client = app.app.test_client()
    headers = _auth_headers()
    for _ in range(2):
        resp = client.post("/api/providers/puter/keys", json={"api_key": _TOKEN},
                           headers=headers)
        assert resp.status_code == 200
    assert config.list_provider_keys("puter") == [_TOKEN]
    assert resp.get_json()["key_count"] == 1


def test_keys_endpoint_rejects_empty_token(isolated_config):
    client = app.app.test_client()
    resp = client.post("/api/providers/puter/keys", json={"api_key": "   "},
                       headers=_auth_headers())
    assert resp.status_code == 400


def test_token_never_echoed_in_response(isolated_config):
    # The saved token is a session credential: responses may mask it but must
    # never contain it verbatim.
    client = app.app.test_client()
    resp = client.post("/api/providers/puter/keys", json={"api_key": _TOKEN},
                       headers=_auth_headers())
    assert resp.status_code == 200
    assert _TOKEN not in resp.get_data(as_text=True)
    listed = client.get("/api/providers", headers=_auth_headers())
    assert listed.status_code == 200
    assert _TOKEN not in listed.get_data(as_text=True)


# --------------------------------------------------------------------------- #
# Frontend: the popup sign-in contract in templates/index.html
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def html():
    return INDEX_HTML.read_text(encoding="utf-8")


def _script_blocks(html_text):
    """Inline <script> bodies with Jinja expressions neutralized to `null`
    (the only in-JS Jinja is {{ control_token | tojson }}; nonce lives on the
    tag, not in the body)."""
    blocks = re.findall(r"<script[^>]*>(.*?)</script>", html_text, re.S)
    return [re.sub(r"\{\{.*?\}\}", "null", b, flags=re.S) for b in blocks]


def test_puter_card_has_connect_button_and_wiring(html):
    # The button exists in the card markup, gated to the puter card only…
    assert "puter-connect-btn" in html
    assert "Connect with Puter" in html
    assert "p.id === 'puter'" in html
    # …and is wired in wireCard (no dangling hook).
    assert re.search(r"\$\('\.puter-connect-btn',\s*card\)", html)
    assert "connectPuter(card, p, puterBtn)" in html
    assert "function connectPuter(card, p, btn)" in html


def test_puter_connect_uses_puterjs_popup_contract(html):
    # The exact contract verified live against https://js.puter.com/v2/ on
    # 2026-07-31 — if puter.com changes it, this test is the tripwire.
    assert "PUTER_GUI_ORIGIN = 'https://puter.com'" in html
    assert "/action/sign-in?embedded_in_popup=true&msg_id=" in html
    assert "e.origin !== PUTER_GUI_ORIGIN" in html
    assert "'puter.token'" in html
    assert "d.msg_id != msgId" in html
    assert "d.token" in html


def test_puter_connect_answers_the_guis_requestOrigin_handshake(html):
    """REGRESSION (blank popup, 2026-07-31). The Puter GUI's initgui() needs
    the opener's origin before it renders anything. It reads document.referrer
    first — always EMPTY here, because the hub sends `Referrer-Policy:
    no-referrer` on every page — then falls back to postMessaging
    {msg:"requestOrigin"} to window.opener and waiting 5s for a reply; with no
    reply it throws "No referrer found" and the popup stays blank. puter.js
    answers that from an always-on top-level listener, so we must too."""
    assert "requestOrigin" in html
    assert "originResponse" in html
    # Registered at load, NOT inside connectPuter() — the request can arrive
    # before/after any single attempt's own listener.
    connect_fn = html.split("function connectPuter", 1)[1]
    assert "requestOrigin" not in connect_fn, \
        "the requestOrigin reply must live in the top-level listener, not per-click"
    # Origin-checked exactly like the token message, and it replies to the
    # message's own source rather than to a remembered window.
    assert re.search(
        r"window\.addEventListener\('message', function\(e\)\{\s*"
        r"if \(e\.origin !== PUTER_GUI_ORIGIN\) return;\s*"
        r"if \(!e\.data \|\| e\.data\.msg !== 'requestOrigin' \|\| !e\.source\) return;\s*"
        r"try\{ e\.source\.postMessage\(\{ msg:'originResponse' \}, '\*'\); \}",
        html), "top-level requestOrigin handler missing or reshaped"


def test_hub_still_sends_no_referrer_so_the_handshake_stays_load_bearing(isolated_config):
    """The reply above is only needed while the hub strips the referrer. If
    this header ever changes, revisit that comment rather than silently
    keeping a workaround for a problem that no longer exists."""
    client = app.app.test_client()
    resp = client.get("/", headers=_DASH)
    assert resp.headers.get("Referrer-Policy") == "no-referrer"


def test_puter_connect_saves_via_existing_keys_endpoint(html):
    # The captured token goes through the same endpoint as a manual paste —
    # no bespoke backend route for this flow.
    assert re.search(
        r"api\('/api/providers/' \+ encodeURIComponent\(p\.id\) \+ '/keys', "
        r"\{ method:'POST', body:\{ api_key: String\(d\.token\) \} \}\)", html)
    # No credentials fields anywhere in the connect flow.
    connect_fn = html.split("function connectPuter", 1)[1].split("\n  function ", 1)[0]
    assert "password" not in connect_fn
    assert "username" not in connect_fn


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_all_inline_script_blocks_pass_node_check(html):
    blocks = _script_blocks(html)
    assert len(blocks) >= 3, "expected the theme boot + 2 app script blocks"
    tmp = Path(tempfile.mkdtemp())   # not tmp_path — see isolated_config
    for i, body in enumerate(blocks):
        f = tmp / ("block%d.js" % i)
        f.write_text(body, encoding="utf-8")
        proc = subprocess.run(["node", "--check", str(f)],
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, "script block %d failed node --check:\n%s" % (i, proc.stderr)
