"""The new dashboard features, as the page itself renders them.

Found by opening every one of them in a real headless browser (2026-09-27)
with faked API answers, including values carrying <img src=x onerror=...>:

- a no-free-tier provider (morph: documented free limit 0) wore the red
  "out of quota" cross and the exhausted styling while its own line said
  "No free tier", and /api/status kept the "Some providers are out of free
  quota - Morph resets in 3d" banner up forever;
- junk-benched models vanished from a card once a reason (parked,
  rate-limited) took over `detail`;
- a saved subscription model missing from `model_choices` showed the first
  option instead of what the CLI is really told to use;
- the Multi-run phase `needs` list went into innerHTML unescaped;
- a protected env var (`removable: false`) was offered a Remove button the
  route always refuses.

The pure render helpers are extracted from the template and run in node
(skipped without node); the rest are contract checks on the template.
"""
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

import app
import config
import quota

HTML_PATH = Path(app.__file__).resolve().parent / "templates" / "index.html"
XSS = "<img src=x onerror=alert(1)>"


def _html():
    return HTML_PATH.read_text(encoding="utf-8")


def _fn(html, name, last=False):
    """Source of a top-level (2-space indented) `function name(...)`."""
    hits = list(re.finditer(r"(\n  function %s\(.*?\n  \}\n)" % re.escape(name), html, re.S))
    assert hits, "could not extract %s from the template" % name
    return (hits[-1] if last else hits[0]).group(1)


def _node(body):
    html = _html()
    m = re.search(r"(\n  var OUT_REASONS = \{.*?\};\n)", html, re.S)
    assert m, "OUT_REASONS not found"
    src = "".join([
        _fn(html, "esc"),
        # two fmtCountdown declarations share one scope: the LAST one wins
        _fn(html, "fmtCountdown", last=True),
        m.group(1),
        _fn(html, "providerNeverFree"), _fn(html, "providerExhausted"),
        _fn(html, "backInText"), _fn(html, "outReasonLine"),
        _fn(html, "usedByLine"), _fn(html, "benchedLine"),
        "var _subManager = null;\n", _fn(html, "subModelRowHTML"),
        body,
    ])
    tmp = tempfile.mkdtemp(prefix="hub-pytest-")
    try:
        js = os.path.join(tmp, "render.js")
        with io.open(js, "w", encoding="utf-8") as fh:
            fh.write(src)
        out = subprocess.run(["node", js], capture_output=True, text=True, timeout=30)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


@needs_node
def test_no_free_tier_is_not_an_out_of_quota_card():
    r = _node("""
      var morph = {status_reason: 'no_free_tier', quota: {limit_known: true, limit: 0, exhausted: true}};
      var quotaOnly = {status_reason: 'ok', quota: {limit_known: true, limit: 0, exhausted: true}};
      var spent = {status_reason: 'exhausted', quota: {limit_known: true, limit: 50, exhausted: true}};
      var parked = {status_reason: 'parked', until: Date.now() / 1000 + 720};
      console.log(JSON.stringify([providerExhausted(morph), providerExhausted(quotaOnly),
                                  providerExhausted(spent), providerExhausted(parked)]));
    """)
    assert r == [False, False, True, True]


@needs_node
def test_benched_models_stay_visible_when_a_reason_takes_over_detail():
    r = _node("""
      var b = [{model: %s, detail: 'x benched 6 h', until: 0, count: 3}];
      console.log(JSON.stringify([
        benchedLine({status_reason: 'parked', benched: b}),
        benchedLine({status_reason: 'ok', benched: b}),
        benchedLine({status_reason: 'throttled', benched: []})]));
    """ % json.dumps(XSS))
    parked, ok, empty = r
    assert "benched for junk answers" in parked
    assert "&lt;img src=x" in parked and "<img" not in parked
    assert ok == "" and empty == ""       # healthy: the backend detail already leads with them


@needs_node
def test_reason_countdown_and_used_by_are_escaped():
    r = _node("""
      var p = {status_reason: 'parked', until: Date.now() / 1000 + 750, detail: %s,
               used_by: [{source: %s, count: 9}, {source: 'a', count: 1}, {source: 'b', count: 1},
                         {source: 'c', count: 1}, {source: 'd', count: 1}, {source: 'e', count: 1}]};
      console.log(JSON.stringify([outReasonLine(p), usedByLine(p)]));
    """ % (json.dumps(XSS), json.dumps(XSS)))
    reason, used = r
    assert "Parked" in reason and "back in 12m" in reason
    assert "+1 more" in used
    for s in (reason, used):
        assert "<img" not in s and "&lt;img" in s


@needs_node
def test_a_saved_model_outside_the_choices_is_still_the_selection():
    r = _node("""
      console.log(JSON.stringify(subModelRowHTML({id: 'sub-gemini', name: 'Gemini',
        model_choices: ['gemini-3-pro'], selected_model: 'gemini-custom-9',
        default_model: 'gemini-3-pro'})));
    """)
    assert '<option value="gemini-custom-9" selected>' in r
    assert '<option value="gemini-3-pro" selected>' not in r


def test_no_free_tier_card_claims_no_free_badge_or_free_models():
    html = _html()
    assert "var noFreeTier = providerNeverFree(p);" in html
    assert "var hasFree = !noFreeTier && (" in html
    assert "(noFreeTier ? ' billed' : ' free')" in html


def test_multi_run_phase_needs_are_escaped():
    html = _html()
    assert "esc(a.needs.join(', '))" in html
    assert "'after ' + a.needs.join" not in html


def test_protected_env_vars_get_no_remove_button():
    html = _html()
    assert "k.scope === 'user' && k.removable !== false" in html
    assert "removable: v.removable" in html


def test_provider_cards_stack_on_a_phone():
    html = _html()
    assert re.search(r"@media \(max-width:640px\)\{ \.prov-subgrid\{grid-template-columns:1fr", html)


def test_tracking_rows_show_the_detected_window():
    html = _html()
    assert "r.ctx_source !== 'default'" in html and "Number(r.ctx_window)" in html


# --------------------------------------------------------------------------- #
# /api/status: a researched zero is not "out of free quota"
# --------------------------------------------------------------------------- #

@pytest.fixture
def isolated(monkeypatch):
    path = Path(tempfile.mkdtemp()) / "state" / "config.json"
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", str(path))
    monkeypatch.setattr(quota, "_PERSIST_PATH", None)
    monkeypatch.setattr(quota, "_STATE", {})
    monkeypatch.setattr(quota, "_MODEL_STATE", {})
    monkeypatch.setattr(quota, "_MODEL_THROTTLE", {})
    monkeypatch.setattr(quota, "_DYNAMIC", {})
    monkeypatch.setattr(quota, "_SOURCE_STATE", {})
    yield


def test_status_banner_skips_a_provider_with_no_free_tier(isolated, monkeypatch):
    # precondition: the registry really documents morph's free limit as 0
    st = quota.status("morph")
    assert st.get("limit_known") and st.get("limit") == 0 and st.get("exhausted")
    monkeypatch.setattr(app, "_enabled_keyed", lambda: ["morph", "groq"])
    headers = {"X-Free-LLM-Hub-Token": config.ensure_control_token(),
               "X-Free-LLM-Hub": "dashboard"}
    j = app.app.test_client().get("/api/status", headers=headers).get_json()
    assert "morph" not in j["quota"]
    assert "groq" in j["quota"]
    assert j["any_exhausted"] is False and j["all_exhausted"] is False
