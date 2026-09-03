"""The hub sees Antigravity, and says honestly how far it can connect it.

ASKED 2026-09-03: "why he dont detect antigravity to connect inside of it?" then
"i want my hub to be able with simple connect button to do it u understand? or
if really need kilocode so should write it in the card of what should be done"
and finally "but be sure if you can connect it without kilocode in antigravity".

So the requirement is conditional, and the first job was to establish WHICH
branch is true. What was measured on this machine, not assumed:

  antigravity-ide.cmd --help   -> only the VS Code launcher flags (--diff,
                                  --merge, --goto, --new-window). No exec.
  extensions/antigravity/
    package.json contributes   -> five settings, none of them a provider,
                                  baseUrl or apiKey
  bundle grep SERVER_URL|
    BASE_URL|API_URL|ENDPOINT  -> nothing. Codeium-derived, Google-authenticated
  product.json extensionsGallery -> https://open-vsx.org/vscode/gallery
  --install-extension          -> supported

=> the built-in agent has no seam to point at the hub (impossible, not just
   unimplemented), and an OpenAI-compatible agent extension inside the IDE does.
   The install of that extension is genuinely one click; its provider config is
   not, because it lives in the IDE's state.vscdb rather than settings.json.

These tests pin the honest half: that detection is real, that the ids are the
ones Open VSX actually serves, and that the card neither promises a Connect the
hub cannot deliver nor hides the manual step.
"""
import json
import os
import sys
from unittest import mock

import antigravity


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

def test_detect_answers_every_question_the_card_asks():
    d = antigravity.detect()
    for key in ("installed", "path", "user_dir", "extensions_dir", "agents",
                "agent_ready", "default_extension", "builtin_agent_connectable"):
        assert key in d, key


def test_the_launcher_is_found_off_path():
    """The generic CLI detector is shutil.which(), and Antigravity's launcher is
    NOT on PATH -- which is the whole reason the hub never saw it."""
    cands = antigravity._bin_candidates()
    assert cands, "no candidate paths for this platform"
    assert all(os.path.isabs(c) for c in cands)
    # and the fallback to PATH is still there for a non-standard install
    with mock.patch.object(antigravity.os.path, "isfile", return_value=False), \
         mock.patch.object(antigravity.shutil, "which",
                           side_effect=lambda n: "/opt/ag" if n == "antigravity-ide" else None):
        assert antigravity.find_binary() == "/opt/ag"


def test_a_missing_install_is_reported_as_missing_not_guessed():
    with mock.patch.object(antigravity, "find_binary", return_value=None):
        d = antigravity.detect()
    assert d["installed"] is False
    assert d["path"] is None
    assert d["agent_ready"] is False


def test_the_builtin_agent_is_never_advertised_as_connectable():
    """The one claim that would be a lie. It is a constant, not a probe, because
    it is a property of Antigravity's own extension, not of this machine."""
    assert antigravity.detect()["builtin_agent_connectable"] is False


# --------------------------------------------------------------------------- #
# The extension ids
# --------------------------------------------------------------------------- #

def test_the_ids_are_the_ones_open_vsx_serves():
    """Antigravity's gallery IS Open VSX (product.json), so a Marketplace-only
    id would 404 at install time. These three were resolved against the live
    Open VSX API before being written down."""
    ids = [e["id"] for e in antigravity.AGENT_EXTENSIONS]
    assert ids == ["kilocode.kilo-code", "saoudrizwan.claude-dev",
                   "RooVeterinaryInc.roo-cline"]


def test_exactly_one_extension_is_recommended():
    rec = [e for e in antigravity.AGENT_EXTENSIONS if e["recommended"]]
    assert len(rec) == 1 and rec[0]["id"] == antigravity._DEFAULT_EXTENSION


def test_every_extension_says_where_its_provider_screen_is():
    """That pointer is the whole value of the card's second step."""
    for e in antigravity.AGENT_EXTENSIONS:
        assert "OpenAI Compatible" in e["settings_path"]


# --------------------------------------------------------------------------- #
# Reading what is installed
# --------------------------------------------------------------------------- #

def test_installed_extensions_come_from_the_ide_manifest(tmp_path):
    (tmp_path / "extensions.json").write_text(json.dumps([
        {"identifier": {"id": "kilocode.kilo-code"}, "version": "7.5.9"},
        {"identifier": {"id": "ms-python.python"}, "version": "2026.4.0"},
    ]), encoding="utf-8")
    with mock.patch.object(antigravity, "extensions_dir", return_value=str(tmp_path)):
        assert antigravity.installed_extension_ids() == ["kilocode.kilo-code",
                                                         "ms-python.python"]


def test_folder_names_are_the_fallback_when_the_manifest_is_unreadable(tmp_path):
    (tmp_path / "extensions.json").write_text("{ not json", encoding="utf-8")
    for name in ("kilocode.kilo-code-7.5.9-universal",
                 "ms-python.python-2026.4.0-universal"):
        (tmp_path / name).mkdir()
    with mock.patch.object(antigravity, "extensions_dir", return_value=str(tmp_path)):
        got = sorted(antigravity.installed_extension_ids())
    assert got == ["kilocode.kilo-code", "ms-python.python"]


def test_a_hyphenated_extension_name_survives_the_version_strip(tmp_path):
    """"kilo-code" has a hyphen in it, so a naive rsplit('-') would mangle it."""
    (tmp_path / "kilocode.kilo-code-7.5.9-universal").mkdir()
    with mock.patch.object(antigravity, "extensions_dir", return_value=str(tmp_path)):
        assert antigravity.installed_extension_ids() == ["kilocode.kilo-code"]


def test_the_match_is_case_insensitive():
    """The manifest says RooVeterinaryInc, the folder on disk is lowercased."""
    assert antigravity._match("RooVeterinaryInc.roo-cline",
                              ["rooveterinaryinc.roo-cline"])
    assert not antigravity._match("RooVeterinaryInc.roo-cline", ["ms-python.python"])


def test_no_extensions_dir_is_not_a_crash():
    with mock.patch.object(antigravity, "extensions_dir", return_value=None):
        assert antigravity.installed_extension_ids() == []


# --------------------------------------------------------------------------- #
# The install, which is the part that really is one click
# --------------------------------------------------------------------------- #

def test_install_shells_out_to_the_ide_launcher():
    with mock.patch.object(antigravity, "find_binary", return_value="/x/ag.cmd"), \
         mock.patch.object(antigravity.subprocess, "run") as run:
        run.return_value = mock.Mock(returncode=0, stdout="successfully installed", stderr="")
        ok, msg = antigravity.install_extension("kilocode.kilo-code")
    assert ok and "successfully installed" in msg
    argv = run.call_args[0][0]
    assert argv[:3] == ["/x/ag.cmd", "--install-extension", "kilocode.kilo-code"]


def test_a_failed_install_reports_the_ides_own_words():
    """Not "failed": the reason, so it is actionable."""
    with mock.patch.object(antigravity, "find_binary", return_value="/x/ag.cmd"), \
         mock.patch.object(antigravity.subprocess, "run") as run:
        run.return_value = mock.Mock(returncode=1, stdout="",
                                     stderr="Extension 'x.y' not found.")
        ok, msg = antigravity.install_extension("kilocode.kilo-code")
    assert ok is False and "not found" in msg


def test_an_unknown_extension_is_refused_before_anything_runs():
    with mock.patch.object(antigravity.subprocess, "run") as run:
        ok, msg = antigravity.install_extension("evil.publisher")
    assert ok is False and "Unknown extension" in msg
    assert not run.called


def test_install_without_antigravity_says_so():
    with mock.patch.object(antigravity, "find_binary", return_value=None):
        ok, msg = antigravity.install_extension()
    assert ok is False and "not found" in msg.lower()


def test_a_hanging_install_times_out_instead_of_wedging_the_route():
    with mock.patch.object(antigravity, "find_binary", return_value="/x/ag.cmd"), \
         mock.patch.object(antigravity.subprocess, "run",
                           side_effect=antigravity.subprocess.TimeoutExpired("ag", 180)):
        ok, msg = antigravity.install_extension()
    assert ok is False and "timed out" in msg


def test_the_install_never_raises():
    with mock.patch.object(antigravity, "find_binary", return_value="/x/ag.cmd"), \
         mock.patch.object(antigravity.subprocess, "run", side_effect=OSError("boom")):
        ok, msg = antigravity.install_extension()
    assert ok is False and "boom" in msg


def test_no_console_window_pops_up_on_windows():
    """A .cmd launched without this flashes a black window over whatever the
    user is doing."""
    if sys.platform == "win32":
        assert antigravity._NO_WINDOW == antigravity.subprocess.CREATE_NO_WINDOW


# --------------------------------------------------------------------------- #
# The routes
# --------------------------------------------------------------------------- #

def _app_source():
    with open("app.py", encoding="utf-8") as f:
        return f.read()


def test_the_hub_exposes_detection_and_install():
    src = _app_source()
    assert '@app.route("/api/antigravity", methods=["GET"])' in src
    assert '@app.route("/api/antigravity/install", methods=["POST"])' in src


def test_the_route_hands_over_the_three_values():
    src = _app_source()
    body = src.split("def api_antigravity():", 1)[1].split("@app.route", 1)[0]
    assert '"base_url"' in body and '"api_key"' in body and '"model"' in body
    assert '/v1" % PORT' in body                 # the live port, not a constant
    assert '"models": ["auto", "best", "swarm"]' in body


def test_antigravity_is_not_smuggled_into_the_cli_registry():
    """Every CLI_REGISTRY card renders Connect / Disconnect / Test. Antigravity
    can honour none of the three, so a row there would be three broken buttons."""
    src = _app_source()
    reg = src.split("CLI_REGISTRY = [", 1)[1].split("\n]", 1)[0]
    assert "antigravity" not in reg.lower()


def test_the_install_route_refuses_an_arbitrary_extension():
    """The id comes off the wire, so it must be checked against the allowlist
    rather than passed to a subprocess."""
    src = _app_source()
    body = src.split("def api_antigravity_install():", 1)[1][:600]
    assert "antigravity.install_extension(body.get(\"extension\"))" in body
    # ...and install_extension is the thing that validates:
    ok, _ = antigravity.install_extension("../../etc/passwd")
    assert ok is False


# --------------------------------------------------------------------------- #
# The card
# --------------------------------------------------------------------------- #

def _template():
    with open("templates/index.html", encoding="utf-8") as f:
        return f.read()


def _card_js():
    html = _template()
    i = html.index("function renderAntigravity(")
    return html[i:html.index("var CHECK_SVG", i)]


def test_the_card_is_rendered_and_refreshed():
    html = _template()
    assert 'id="antigravity-card"' in html
    assert "function loadAntigravity()" in html
    # the refresh line for the Connect view, matched by its PARTS: a later card
    # added to the same line must not fail this test, only a dropped loader
    refresh = [l for l in html.splitlines() if "'sec-lifecycle': function()" in l][0]
    assert "loadAntigravity()" in refresh and "loadClis()" in refresh


def test_the_card_hides_itself_when_antigravity_is_absent():
    """It is one machine-specific tool; an empty "not installed" card on every
    other machine is noise."""
    body = _card_js()
    assert "if (!d || !d.installed){ host.innerHTML = ''; return; }" in body


def test_the_card_states_the_builtin_agent_cannot_be_routed():
    """The user asked to be told plainly rather than left guessing."""
    body = _card_js()
    assert "built-in agent cannot be routed through the hub" in body


def test_the_card_offers_the_install_that_really_is_one_click():
    body = _card_js()
    assert "ag-install" in body
    assert "/api/antigravity/install" in body


def test_the_card_shows_all_three_values_with_copy_buttons():
    body = _card_js()
    assert "'Base URL'" in body and "'API key'" in body and "'Model'" in body
    assert "ag-copy" in body and "copyText(" in body


def test_the_card_names_the_manual_step_and_why():
    """Not hidden, not dressed up as a button that does nothing."""
    body = _card_js()
    assert "settings_path" in body
    assert "state database" in body


def test_the_card_mentions_the_other_model_aliases():
    body = _card_js()
    assert "best" in body and "swarm" in body


def test_installing_refreshes_the_card_from_the_response():
    """The install response already carries a fresh detect(), so the card must
    not need a page reload to show the extension as ready."""
    body = _card_js()
    assert "renderAntigravity(r);" in body
