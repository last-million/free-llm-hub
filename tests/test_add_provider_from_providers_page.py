"""Add a provider from the Providers page itself (2026-10-27).

The backend (POST /api/custom-providers + the ordinary per-provider key route)
already exists; this is the page's half. A button in the Providers toolbar
opens a native <dialog> with the form (name, base URL, key, and an "Optional
details" fold with models URL, where-to-get-a-key, key hint, notes). Submit:

  1. POST /api/custom-providers {six trimmed fields, NO id}  (the server slugs
     the name, so a blind re-POST after a later failure would mint "name-2")
  2. with a key: POST /api/providers/<id from the 201>/keys {api_key}
  3. reload the cards, test the key, reveal the new card.

Hermetic: the page logic is extracted from templates/index.html and run in
node against a fake DOM and a fake `api` (same shape as
tests/test_custom_providers.py / tests/test_dashboard_feature_rendering.py;
skipped without node); the markup and CSS get static checks. Nothing here
touches a hub, a network or ~/.free-llm-hub.
"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

import app

_HTML_PATH = Path(app.__file__).resolve().parent / "templates" / "index.html"
_needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

_XSS = "<img src=x onerror=alert(1)>"


def _html():
    return _HTML_PATH.read_text(encoding="utf-8")


def _fn(html, name):
    """Source of a top-level (2-space indented) `function name(...)`."""
    hits = list(re.finditer(r"(\n  function %s\(.*?\n  \}\n)" % re.escape(name), html, re.S))
    assert hits, "could not extract %s from the template" % name
    return hits[0].group(1)


def _block(html, start, end):
    i = html.index(start)
    return html[i:html.index(end, i)]


# --------------------------------------------------------------------------- #
# Static: the control, the dialog, the labels, the ids
# --------------------------------------------------------------------------- #
def _providers_section(html):
    i = html.index('aria-labelledby="h-providers"')
    return html[i:html.index("<section", i + 10)]


def test_the_providers_toolbar_has_an_add_provider_button():
    sec = _providers_section(_html())
    toolbar = sec[sec.index('<div class="toolbar">'):sec.index('<div class="guided"')]
    m = re.search(r'<button[^>]*id="pa-open"[^>]*>(.*?)</button>', toolbar, re.S)
    assert m, "no #pa-open button in the Providers toolbar"
    tag = m.group(0)
    assert 'type="button"' in tag
    assert 'aria-haspopup="dialog"' in tag
    assert 'aria-controls="add-provider-dialog"' in tag
    assert "Add provider" in m.group(1)
    # it is a real <button> (keyboard + a name), never a div with a click handler
    assert re.sub(r"<svg.*?</svg>", "", m.group(1), flags=re.S).strip() == "Add provider"


def test_the_dialog_is_a_native_labelled_modal_directly_under_body():
    html = _html()
    m = re.search(r'<dialog\b[^>]*id="add-provider-dialog"[^>]*>', html)
    assert m, "no <dialog id=add-provider-dialog>"
    tag = m.group(0)
    assert 'aria-labelledby="pa-title"' in tag
    assert "publish-dialog" in tag and "add-prov-dialog" in tag     # reuses the dialog layout
    assert re.search(r'<h2[^>]*id="pa-title"[^>]*tabindex="-1"', html)
    # a direct child of <body>: after the toasts div, before the main page script
    toasts = html.index('<div id="toasts"')
    assert toasts < m.start() < html.index('<script nonce="{{ csp_nonce }}">', toasts)
    close = re.search(r'<button[^>]*id="pa-close"[^>]*>', html).group(0)
    assert 'aria-label="Close"' in close and 'type="button"' in close


_FIELDS = ["pa-name", "pa-base-url", "pa-key", "pa-models-url", "pa-signup-url", "pa-key-hint", "pa-notes"]


def test_every_field_has_a_real_label_and_the_key_is_a_password_input():
    html = _html()
    dlg = html[html.index('id="add-provider-dialog"'):html.index("</dialog>", html.index('id="add-provider-dialog"'))]
    for fid in _FIELDS:
        assert re.search(r'<label[^>]*for="%s"' % fid, dlg), "no <label for=%s>" % fid
        assert re.search(r'<(input|textarea)[^>]*id="%s"' % fid, dlg), "no control #%s" % fid
    key = re.search(r'<input[^>]*id="pa-key"[^>]*>', dlg).group(0)
    assert 'type="password"' in key
    assert 'autocomplete="off"' in key
    assert 'spellcheck="false"' in key
    # required fields are named as required in their label
    assert re.search(r'<label[^>]*for="pa-name"[^>]*>.*?required.*?</label>', dlg, re.I | re.S)
    assert re.search(r'<label[^>]*for="pa-base-url"[^>]*>.*?required.*?</label>', dlg, re.I | re.S)
    # the error line is announced; the form is submitted by a real submit button
    assert re.search(r'id="pa-err"[^>]*role="alert"', dlg) or re.search(r'role="alert"[^>]*id="pa-err"', dlg)
    submit = re.search(r'<button[^>]*id="pa-submit"[^>]*>', dlg).group(0)
    assert 'type="submit"' in submit and 'form="pa-form"' in submit
    assert re.search(r'<form[^>]*id="pa-form"[^>]*novalidate', dlg)


def test_new_ids_are_unique_and_never_collide_with_the_settings_form():
    html = _html()
    ids = re.findall(r'\bid="(pa-[a-z0-9-]+|add-provider-dialog)"', html)
    assert len(ids) >= 12
    assert len(ids) == len(set(ids)), "duplicate id among %r" % ids
    # Settings' handlers use $('#cp-...') where the first match wins: nothing new may start with cp-
    dlg = html[html.index('id="add-provider-dialog"'):html.index("</dialog>", html.index('id="add-provider-dialog"'))]
    assert not re.findall(r'\bid="cp-', dlg)
    # ...and the Settings group is untouched
    for keep in ("cp-group", "cp-form", "cp-list", "cp-name", "cp-base-url"):
        assert html.count('id="%s"' % keep) == 1


def test_no_new_sd_wide_group_and_no_jinja_syntax_in_the_new_markup():
    html = _html()
    assert html.count('class="sd-group sd-wide"') == 5          # tests/test_the_whats_new_popup.py
    dlg = html[html.index('id="add-provider-dialog"'):html.index("</dialog>", html.index('id="add-provider-dialog"'))]
    for bad in ("{{", "{%", "{#"):
        assert bad not in dlg


# --------------------------------------------------------------------------- #
# Static: the CSS block (tokens only, 44 rendered px, phone font size)
# --------------------------------------------------------------------------- #
def test_the_css_block_uses_theme_tokens_only_and_44_rendered_px_targets():
    css = _block(_html(), "/* add-prov-css:start */", "/* add-prov-css:end */")
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css), "raw hex colour in the add-provider CSS"
    assert not re.search(r"\b(rgb|rgba|hsl|hsla)\(", css)
    # inputs are as tall as the dialog's tap size; the toolbar button grows to 44 rendered px
    assert "min-height:var(--pub-tap)" in css
    assert re.search(r"\(pointer:coarse\),\s*\(max-width:640px\)", css)
    assert "calc(44px / var(--ui-scale, .8))" in css
    # iOS does not zoom a focused field at 16 rendered px
    assert "calc(16px / var(--ui-scale, .8))" in css
    # focus is visible
    assert ":focus-visible" in css
    # the `[hidden]` attribute still wins over the button's display (global rule)
    assert "[hidden]" in _html()


def test_the_new_blocks_do_not_sit_inside_other_features_marker_blocks():
    html = _html()
    css_at = html.index("/* add-prov-css:start */")
    assert "/* add-prov-js:start */" in html and "/* add-prov-js:end */" in html
    js_at = html.index("/* add-prov-js:start */")
    for a, b, at in (("/* publish-css:start */", "/* publish-css:end */", css_at),
                     ("/* github-css:start */", "/* github-css:end */", css_at),
                     ("/* publish-js:start */", "/* publish-js:end */", js_at),
                     ("/* github-js:start */", "/* github-js:end */", js_at)):
        assert html.index(a) < html.index(b)
        assert not (html.index(a) < at < html.index(b)), "new block sits inside %s" % a
    js = _block(html, "/* add-prov-js:start */", "/* add-prov-js:end */")
    for lbl in ("publish-js:", "github-js:", "publish-css:", "github-css:"):
        assert lbl not in js


def test_the_flag_reaches_the_button_through_load_custom_providers():
    html = _html()
    load = _fn(html, "loadCustomProviders")
    assert "paSetEnabled(" in load
    # the existing escape test extracts renderCustomProviders alone: it must stay free of new helpers
    assert "paSetEnabled" not in _fn(html, "renderCustomProviders")
    # the Providers page learns the flag at startup, not only when Settings opens
    assert re.search(r"initAddProvider\(\);", html)
    assert "loadCustomProviders(true)" in _fn(html, "initAddProvider")


def test_the_dialog_and_the_settings_form_share_one_body_builder():
    """One definition of the six fields (trim + the two required ones), used by
    both forms; the dialog must not carry a second copy."""
    html = _html()
    js = _block(html, "/* add-prov-js:start */", "/* add-prov-js:end */")
    settings = _fn(html, "initCustomProviders")
    assert "cpBuildBody(" in settings and "cpBuildBody(" in js
    assert "function cpBuildBody(" in html and "function cpBuildBody(" not in js
    assert "function paBuildBody" not in html
    # both talk to the same create route and neither sends an id on create
    assert "'/api/custom-providers', { method: 'POST', body: body }" in settings
    assert "'/api/custom-providers', { method: 'POST', body: body }" in js
    assert "id:" not in _fn(html, "cpBuildBody")


def test_no_backend_string_is_written_through_innerhtml_in_the_new_code():
    html = _html()
    js = _block(html, "/* add-prov-js:start */", "/* add-prov-js:end */")
    assert ".innerHTML" not in js
    assert "insertAdjacentHTML" not in js


def test_the_dashboard_page_still_renders_and_its_inline_js_parses():
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    r = app.app.test_client().get("/")
    assert r.status_code == 200
    page = r.get_data(as_text=True)
    assert 'id="add-provider-dialog"' in page and 'id="pa-open"' in page
    scripts = re.findall(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', page, re.S)
    assert scripts and not any("{{" in s for s in scripts), "unrendered Jinja leaked into the page"
    tmp = Path(tempfile.mkdtemp()) / "dashboard.js"
    tmp.write_text("\n;\n".join(scripts), encoding="utf-8")
    out = subprocess.run(["node", "--check", str(tmp)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


# --------------------------------------------------------------------------- #
# node: the logic, against a fake DOM and a fake api
# --------------------------------------------------------------------------- #
_PRELUDE = r"""
function mkEl(id){
  var e = { id: id, hidden: false, textContent: '', value: '', open: false, focused: false,
            readOnly: false, disabled: false, attrs: {}, scrolled: false, children: [] };
  e.setAttribute = function(k, v){ this.attrs[k] = String(v); };
  e.removeAttribute = function(k){ delete this.attrs[k]; };
  e.getAttribute = function(k){ return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; };
  e.focus = function(){ this.focused = true; };
  e.scrollIntoView = function(){ this.scrolled = true; };
  e.closest = function(sel){ return sel === 'details' ? (this.parentDetails || null) : null; };
  Object.defineProperty(e, 'innerHTML', { get: function(){ return ''; },
    set: function(){ throw new Error('innerHTML must never be written (' + id + ')'); } });
  return e;
}
var els = {};
function $(sel){ var id = String(sel).replace(/^#/, ''); return els[id] || (els[id] = mkEl(id)); }
function esc(s){ return String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
function mkApi(script){
  var calls = [];
  function api(path, opts){
    opts = opts || {};
    calls.push({ path: path, method: opts.method || 'GET', body: opts.body });
    var r = script(path, opts, calls.length);
    if (r instanceof Error) return Promise.reject(r);
    return Promise.resolve(r);
  }
  api.calls = calls;
  return api;
}
function fail(msg, code, status){ var e = new Error(msg); e.body = code ? { error: msg, code: code } : { error: msg }; e.status = status || 400; return e; }
"""


def _run(body_js, funcs):
    html = _html()
    src = "\n".join([_PRELUDE] + [_fn(html, f) for f in funcs] + [body_js])
    tmp = Path(tempfile.mkdtemp())
    js = tmp / "t.js"
    js.write_text(src, encoding="utf-8")
    out = subprocess.run(["node", str(js)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr + out.stdout
    return json.loads(out.stdout.strip().splitlines()[-1])


@_needs_node
def test_build_body_trims_sends_exactly_six_fields_and_no_id():
    res = _run("""
      var full = cpBuildBody({ name: '  My LLM ', base_url: ' https://api.example.com/v1 ', models_url: ' ',
                               signup_url: ' ask admin ', key_hint: 'sk-', notes: ' n ' });
      var noName = cpBuildBody({ name: '   ', base_url: 'https://x' });
      var noBase = cpBuildBody({ name: 'x', base_url: '  ' });
      var weird = cpBuildBody({ name: 5, base_url: null });
      console.log(JSON.stringify({ full: full, noName: noName, noBase: noBase, weird: weird }));
    """, ["cpBuildBody"])
    body = res["full"]["body"]
    assert sorted(body) == ["base_url", "key_hint", "models_url", "name", "notes", "signup_url"]
    assert "id" not in body
    assert body["name"] == "My LLM" and body["base_url"] == "https://api.example.com/v1"
    assert body["models_url"] == "" and body["signup_url"] == "ask admin" and body["notes"] == "n"
    # the field is the API's own key; the dialog maps it to its input id
    assert res["noName"]["field"] == "name" and "name" in res["noName"]["error"].lower()
    assert res["noBase"]["field"] == "base_url" and "base" in res["noBase"]["error"].lower()
    assert "body" not in res["noName"] and "body" not in res["noBase"]
    assert res["weird"]["field"] == "base_url"          # non-strings never throw; name 5 -> "5" is a name


@_needs_node
def test_error_field_maps_the_servers_messages_to_the_input_to_focus():
    res = _run("""
      var m = ['name is required and must be a non-empty string',
               'base_url: custom base URL must use http:// or https://',
               'models_url: non-loopback custom base URLs must use https://',
               "a custom provider 'x' already exists", '', null, 'namespace oops'];
      console.log(JSON.stringify(m.map(paErrorField)));
    """, ["paErrorField"])
    assert res == ["pa-name", "pa-base-url", "pa-models-url", "", "", "", ""]


@_needs_node
def test_submit_creates_then_saves_the_key_for_the_id_the_server_returned():
    res = _run("""
      var api = mkApi(function(path){
        if (path === '/api/custom-providers') return { id: 'my-llm', name: 'My LLM', custom: true };
        return { ok: true };
      });
      var body = cpBuildBody({ name: 'My LLM', base_url: 'https://api.example.com/v1' }).body;
      paSubmit(body, 'sk-secret', api).then(function(r){
        console.log(JSON.stringify({ r: r, calls: api.calls }));
      });
    """, ["cpBuildBody", "paErrorField", "paSubmit"])
    assert [c["path"] for c in res["calls"]] == ["/api/custom-providers", "/api/providers/my-llm/keys"]
    assert [c["method"] for c in res["calls"]] == ["POST", "POST"]
    assert "id" not in res["calls"][0]["body"]
    assert res["calls"][1]["body"] == {"api_key": "sk-secret"}
    r = res["r"]
    assert r["ok"] is True and r["pid"] == "my-llm"
    assert r["info"] == {"name": "My LLM", "keyGiven": True, "keySaved": True}


@_needs_node
def test_submit_without_a_key_makes_no_keys_call_and_url_encodes_the_id():
    res = _run("""
      var api = mkApi(function(path){ return { id: 'a b', name: 'A' }; });
      paSubmit({ name: 'A', base_url: 'https://x.example.com' }, '', api).then(function(r){
        var api2 = mkApi(function(path){ return { id: 'a b', name: 'A' }; });
        paSubmit({ name: 'A', base_url: 'https://x.example.com' }, 'k', api2).then(function(r2){
          console.log(JSON.stringify({ n1: api.calls.length, r: r, p2: api2.calls[1].path }));
        });
      });
    """, ["paErrorField", "paSubmit"])
    assert res["n1"] == 1
    assert res["r"]["ok"] is True and res["r"]["info"]["keyGiven"] is False
    assert res["p2"] == "/api/providers/a%20b/keys"


@_needs_node
def test_a_create_failure_makes_no_keys_call_and_names_the_field():
    res = _run("""
      var api = mkApi(function(){ return fail('base_url: custom base URL must use http:// or https://'); });
      paSubmit({ name: 'A', base_url: 'ftp://x' }, 'sk-1', api).then(function(r){
        var api2 = mkApi(function(){ return fail('Custom providers are switched off.', 'disabled', 403); });
        paSubmit({ name: 'A', base_url: 'https://x' }, 'sk-1', api2).then(function(r2){
          console.log(JSON.stringify({ r: r, n: api.calls.length, r2: r2, n2: api2.calls.length }));
        });
      });
    """, ["paErrorField", "paSubmit"])
    assert res["n"] == 1 and res["n2"] == 1
    assert res["r"]["ok"] is False and res["r"]["field"] == "pa-base-url"
    assert "http" in res["r"]["error"] and res["r"]["disabled"] is False
    assert res["r2"]["ok"] is False and res["r2"]["disabled"] is True
    assert res["r2"]["error"] == "Custom providers are switched off."


@_needs_node
def test_a_key_failure_after_create_keeps_the_provider_and_the_retry_never_creates_again():
    res = _run("""
      var attempt = 0;
      var api = mkApi(function(path){
        if (path === '/api/custom-providers') return { id: 'my-llm', name: 'My LLM' };
        attempt++;
        if (attempt === 1) return fail('That key was refused', '', 400);
        return { ok: true };
      });
      var body = { name: 'My LLM', base_url: 'https://x.example.com' };
      paSubmit(body, 'bad', api).then(function(first){
        paSubmit(body, 'good', api, first.pid).then(function(second){
          console.log(JSON.stringify({ first: first, second: second, calls: api.calls }));
        });
      });
    """, ["paErrorField", "paSubmit"])
    first, second = res["first"], res["second"]
    assert first["ok"] is True and first["pid"] == "my-llm"
    assert first["info"]["keyGiven"] is True and first["info"]["keySaved"] is False
    assert first["info"]["keyError"] == "That key was refused"
    assert second["ok"] is True and second["info"]["keySaved"] is True
    paths = [c["path"] for c in res["calls"]]
    assert paths.count("/api/custom-providers") == 1                  # created once
    assert paths == ["/api/custom-providers", "/api/providers/my-llm/keys", "/api/providers/my-llm/keys"]


@_needs_node
def test_a_create_reply_without_an_id_is_an_error_not_a_silent_success():
    res = _run("""
      var api = mkApi(function(){ return {}; });
      paSubmit({ name: 'A', base_url: 'https://x.example.com' }, 'k', api).then(function(r){
        console.log(JSON.stringify({ r: r, n: api.calls.length }));
      });
    """, ["paErrorField", "paSubmit"])
    assert res["r"]["ok"] is False and res["r"]["error"] and res["n"] == 1


@_needs_node
def test_backend_error_text_goes_in_through_textcontent_only():
    """The fake element THROWS if innerHTML is ever written, so a payload coming
    back in an error message (or a provider name in a toast) cannot be markup."""
    res = _run("""
      paShowError(%s, 'pa-models-url');
      var box = $('#pa-err');
      console.log(JSON.stringify({ text: box.textContent, hidden: box.hidden,
        invalid: $('#pa-models-url').getAttribute('aria-invalid'), focused: $('#pa-models-url').focused,
        more: $('#pa-more').open, otherInvalid: $('#pa-name').getAttribute('aria-invalid') }));
    """ % json.dumps(_XSS), ["paShowError"])
    assert res["text"] == _XSS                       # literal text, never parsed
    assert res["hidden"] is False and res["invalid"] == "true" and res["focused"] is True
    assert res["more"] is True                       # the optional fold opens onto the bad field
    assert res["otherInvalid"] is None


@_needs_node
def test_show_error_clears_the_previous_mark_and_an_empty_message_hides_the_line():
    res = _run("""
      paShowError('name is required', 'pa-name');
      paShowError('base_url: bad', 'pa-base-url');
      var a = { name: $('#pa-name').getAttribute('aria-invalid'), base: $('#pa-base-url').getAttribute('aria-invalid') };
      paShowError('', '');
      console.log(JSON.stringify({ a: a, hidden: $('#pa-err').hidden, text: $('#pa-err').textContent,
        name: $('#pa-name').getAttribute('aria-invalid'), base: $('#pa-base-url').getAttribute('aria-invalid') }));
    """, ["paShowError"])
    assert res["a"] == {"name": None, "base": "true"}
    assert res["hidden"] is True and res["text"] == "" and res["name"] is None and res["base"] is None


@_needs_node
def test_the_confirmation_text_covers_every_outcome_and_stays_plain_text():
    res = _run("""
      var n = %s;
      var out = {
        nokey: paNotice({ name: n, keyGiven: false, keySaved: false }, null),
        keyfail: paNotice({ name: n, keyGiven: true, keySaved: false, keyError: 'refused' }, null),
        ok: paNotice({ name: n, keyGiven: true, keySaved: true }, { state: 'ok', detail: '12 models' }),
        okbare: paNotice({ name: n, keyGiven: true, keySaved: true }, { state: 'ok' }),
        bad: paNotice({ name: n, keyGiven: true, keySaved: true }, { state: 'bad', detail: 'HTTP 401' }),
        badbare: paNotice({ name: n, keyGiven: true, keySaved: true }, { state: 'bad' }),
        unknown: paNotice({ name: n, keyGiven: true, keySaved: true }, { state: 'unknown', detail: 'Key test timed out.' }),
        none: paNotice({ name: n, keyGiven: true, keySaved: true }, undefined),
        empty: paNotice(null, null)
      };
      console.log(JSON.stringify(out));
    """ % json.dumps(_XSS), ["paNotice"])
    assert res["nokey"]["err"] is False and "key" in res["nokey"]["text"].lower()
    assert res["keyfail"]["err"] is True and "refused" in res["keyfail"]["text"]
    assert res["ok"]["err"] is False and "12 models" in res["ok"]["text"]
    assert res["okbare"]["err"] is False
    assert res["bad"]["err"] is True and "HTTP 401" in res["bad"]["text"]
    assert res["badbare"]["err"] is True
    assert res["unknown"]["err"] is True and "timed out" in res["unknown"]["text"]
    assert res["none"]["err"] is True
    assert res["empty"]["text"]
    for k in ("nokey", "keyfail", "ok", "bad", "unknown"):
        assert _XSS in res[k]["text"]                # the name is a plain string, escaped by toast's textContent
    for k, v in res.items():
        assert "  " not in v["text"] and ".." not in v["text"], (k, v["text"])


@_needs_node
def test_the_flag_hides_and_shows_the_button_and_fails_open():
    res = _run("""
      var out = [];
      paSetEnabled({ enabled: false }); out.push($('#pa-open').hidden);
      paSetEnabled({ enabled: true });  out.push($('#pa-open').hidden);
      // no reply at all (the GET failed) says nothing: the button stays as it was
      paSetEnabled({ enabled: false }); paSetEnabled(null); out.push($('#pa-open').hidden);
      paSetEnabled({ enabled: false }); paSetEnabled(undefined); out.push($('#pa-open').hidden);
      // a reply without the field means "on" (the flag defaults ON), same as Settings
      paSetEnabled({}); out.push($('#pa-open').hidden);
      console.log(JSON.stringify(out));
    """, ["paSetEnabled"])
    assert res == [True, False, True, True, False]


@_needs_node
def test_reveal_opens_the_collapsed_group_scrolls_to_the_card_and_reports_a_miss():
    res = _run("""
      var card = mkEl('card'); var group = mkEl('group'); group.open = false; card.parentDetails = group;
      var keyInput = mkEl('key'); card.querySelector = function(sel){ return sel === '.key-input' ? keyInput : null; };
      var cards = { 'my-llm': card };
      function cardForPid(pid){ return cards[pid] || null; }
      var window = { matchMedia: function(){ return { matches: true }; } };
      var hit = paRevealCard('my-llm', true), miss = paRevealCard('nope', true);
      console.log(JSON.stringify({ hit: hit, miss: miss, open: group.open, scrolled: card.scrolled, keyFocus: keyInput.focused }));
    """, ["paRevealCard"])
    assert res == {"hit": True, "miss": False, "open": True, "scrolled": True, "keyFocus": True}


# --------------------------------------------------------------------------- #
# node: the whole dialog (initAddProvider) against a fake DOM and a fake api
# --------------------------------------------------------------------------- #
_DIALOG_HARNESS = r"""
var api, toasts = [], keyHealth = {};
var document = { body: {} };
var window = { matchMedia: function(){ return { matches: true }; } };
var _paSeq = 0, _paPid = '', _paBusy = false;
function toast(m, err){ toasts.push({ m: m, err: !!err }); }
function loadProviders(){ return Promise.resolve(); }
function loadCustomProviders(){ return Promise.resolve(); }
function loadStatus(){ return Promise.resolve(); }
function loadModels(){ return Promise.resolve(); }
function cardForPid(){ return null; }
function testKeyHealth(pid){ keyHealth[pid] = { state: 'ok', detail: '12 models' }; return Promise.resolve(); }
function settle(){ return new Promise(function(r){ setTimeout(r, 20); }); }
function mkEl2(id){
  var e = mkEl(id); e.handlers = {}; e.parentNode = null;
  e.addEventListener = function(t, f){ (this.handlers[t] = this.handlers[t] || []).push(f); };
  e.fire = function(t, ev){
    ev = ev || {}; ev.target = ev.target || this; ev.preventDefault = ev.preventDefault || function(){};
    (this.handlers[t] || []).forEach(function(f){ f(ev); });
  };
  return e;
}
function setup(script){
  els = {}; _paSeq = 0; _paPid = ''; _paBusy = false; toasts.length = 0; keyHealth = {};
  ['add-provider-dialog', 'pa-open', 'pa-close', 'pa-cancel', 'pa-form'].forEach(function(id){ els[id] = mkEl2(id); });
  var dlg = els['add-provider-dialog'];
  dlg.parentNode = document.body;
  dlg.showModal = function(){ this.open = true; };
  dlg.close = function(){ if (!this.open) return; this.open = false; this.fire('close'); };
  api = mkApi(script);
  initAddProvider();
  return dlg;
}
function fill(v){ Object.keys(v).forEach(function(k){ $('#' + k).value = v[k]; }); }
function submit(){ $('#pa-form').fire('submit'); }
function paths(){ return api.calls.map(function(c){ return c.method + ' ' + c.path; }); }
var CREATE = 'POST /api/custom-providers', KEYS = 'POST /api/providers/my-llm/keys';
var OK = { id: 'my-llm', name: 'My LLM' };
"""

_DIALOG_FUNCS = ["cpBuildBody", "paErrorField", "paSubmit", "paNotice", "paShowError",
                 "paSetEnabled", "paRevealCard", "initAddProvider"]


@_needs_node
def test_the_dialog_creates_saves_the_key_closes_and_says_it_worked():
    res = _run(_DIALOG_HARNESS + """
      (async function(){
        var dlg = setup(function(path){ return path === '/api/custom-providers' ? OK : { ok: true }; });
        $('#pa-open').fire('click');
        var opened = dlg.open;
        fill({ 'pa-name': ' My LLM ', 'pa-base-url': 'https://x.example.com/v1', 'pa-key': 'sk-1' });
        submit(); await settle();
        console.log(JSON.stringify({ opened: opened, open: dlg.open, paths: paths(), body: api.calls[0].body,
          keyBody: api.calls[1].body, toasts: toasts, keyLeft: $('#pa-key').value,
          errHidden: $('#pa-err').hidden }));
      })();
    """, _DIALOG_FUNCS)
    assert res["opened"] is True and res["open"] is False
    assert res["paths"] == ["POST /api/custom-providers", "POST /api/providers/my-llm/keys"]
    assert res["body"]["name"] == "My LLM" and "id" not in res["body"]
    assert res["keyBody"] == {"api_key": "sk-1"}
    assert res["toasts"] == [{"m": "My LLM is ready. The key works. 12 models.", "err": False}]
    assert res["keyLeft"] == "" and res["errHidden"] is True      # no key left sitting in a closed form


@_needs_node
def test_a_key_that_did_not_save_keeps_the_dialog_open_and_the_retry_never_creates_again():
    res = _run(_DIALOG_HARNESS + """
      (async function(){
        var n = 0;
        var dlg = setup(function(path){
          if (path === '/api/custom-providers') return OK;
          n++; return n === 1 ? fail('That key was refused') : { ok: true };
        });
        $('#pa-open').fire('click');
        fill({ 'pa-name': 'My LLM', 'pa-base-url': 'https://x.example.com/v1', 'pa-key': 'bad' });
        submit(); await settle();
        var mid = { open: dlg.open, label: $('#pa-submit').textContent, locked: $('#pa-name').readOnly,
                    lockedBase: $('#pa-base-url').readOnly, err: $('#pa-err').textContent, errHidden: $('#pa-err').hidden,
                    invalid: $('#pa-key').getAttribute('aria-invalid'), keyLeft: $('#pa-key').value,
                    disabled: $('#pa-submit').disabled };
        submit(); await settle();                          /* no key typed: asks for it, calls nothing */
        var noKey = { n: api.calls.length, err: $('#pa-err').textContent };
        $('#pa-key').value = 'good'; submit(); await settle();
        console.log(JSON.stringify({ mid: mid, noKey: noKey, paths: paths(), open: dlg.open, toasts: toasts }));
      })();
    """, _DIALOG_FUNCS)
    mid = res["mid"]
    assert mid["open"] is True and mid["label"] == "Save key" and mid["disabled"] is False
    assert mid["locked"] is True and mid["lockedBase"] is True
    assert "was added, but its key was not saved" in mid["err"] and "That key was refused" in mid["err"]
    assert mid["errHidden"] is False and mid["invalid"] == "true" and mid["keyLeft"] == ""
    assert res["noKey"]["n"] == 2 and "key" in res["noKey"]["err"].lower()
    assert res["paths"] == ["POST /api/custom-providers", "POST /api/providers/my-llm/keys", "POST /api/providers/my-llm/keys"]
    assert res["open"] is False
    assert len(res["toasts"]) == 1 and res["toasts"][0]["err"] is False and "ready" in res["toasts"][0]["m"]


@_needs_node
def test_missing_fields_call_nothing_and_point_at_the_input():
    res = _run(_DIALOG_HARNESS + """
      (async function(){
        var dlg = setup(function(){ return OK; });
        $('#pa-open').fire('click');
        fill({ 'pa-name': '   ', 'pa-base-url': 'https://x.example.com/v1' });
        submit(); await settle();
        var a = { n: api.calls.length, err: $('#pa-err').textContent, invalid: $('#pa-name').getAttribute('aria-invalid'),
                  focused: $('#pa-name').focused };
        fill({ 'pa-name': 'x', 'pa-base-url': '' });
        submit(); await settle();
        var b = { n: api.calls.length, invalid: $('#pa-base-url').getAttribute('aria-invalid'),
                  nameCleared: $('#pa-name').getAttribute('aria-invalid'), open: dlg.open };
        console.log(JSON.stringify({ a: a, b: b }));
      })();
    """, _DIALOG_FUNCS)
    assert res["a"]["n"] == 0 and "name" in res["a"]["err"].lower()
    assert res["a"]["invalid"] == "true" and res["a"]["focused"] is True
    assert res["b"] == {"n": 0, "invalid": "true", "nameCleared": None, "open": True}


@_needs_node
def test_a_server_refusal_is_shown_in_plain_words_and_a_disabled_flag_removes_the_button():
    res = _run(_DIALOG_HARNESS + """
      (async function(){
        var dlg = setup(function(){ return fail('base_url: custom base URL must use http:// or https://'); });
        $('#pa-open').fire('click');
        fill({ 'pa-name': 'x', 'pa-base-url': 'ftp://x' });
        submit(); await settle();
        var a = { open: dlg.open, err: $('#pa-err').textContent, invalid: $('#pa-base-url').getAttribute('aria-invalid'),
                  label: $('#pa-submit').textContent, disabled: $('#pa-submit').disabled, btnHidden: $('#pa-open').hidden,
                  toasts: toasts.length };
        var dlg2 = setup(function(){ return fail('Custom providers are switched off.', 'disabled', 403); });
        $('#pa-open').fire('click');
        fill({ 'pa-name': 'x', 'pa-base-url': 'https://x.example.com' });
        submit(); await settle();
        var b = { open: dlg2.open, err: $('#pa-err').textContent, btnHidden: $('#pa-open').hidden };
        // with the button gone the dialog cannot be reopened
        dlg2.close(); $('#pa-open').fire('click');
        b.reopened = dlg2.open;
        console.log(JSON.stringify({ a: a, b: b }));
      })();
    """, _DIALOG_FUNCS)
    a, b = res["a"], res["b"]
    assert a["open"] is True and "http" in a["err"] and a["invalid"] == "true"
    assert a["label"] == "Add provider" and a["disabled"] is False and a["btnHidden"] is False and a["toasts"] == 0
    assert b["open"] is True and b["err"] == "Custom providers are switched off." and b["btnHidden"] is True
    assert b["reopened"] is False


@_needs_node
def test_closing_the_dialog_while_the_request_is_in_flight_only_toasts_the_result():
    res = _run(_DIALOG_HARNESS + """
      (async function(){
        var release;
        var gate = new Promise(function(r){ release = r; });
        var dlg = setup(function(path){ return gate.then(function(){ return OK; }); });
        $('#pa-open').fire('click');
        fill({ 'pa-name': 'My LLM', 'pa-base-url': 'https://x.example.com/v1' });
        submit();
        dlg.close();                                        /* the person leaves before the answer */
        release(); await settle();
        console.log(JSON.stringify({ open: dlg.open, paths: paths(), toasts: toasts, errHidden: $('#pa-err').hidden }));
      })();
    """, _DIALOG_FUNCS)
    assert res["open"] is False and res["paths"] == ["POST /api/custom-providers"]
    assert len(res["toasts"]) == 1 and "My LLM was added" in res["toasts"][0]["m"] and res["toasts"][0]["err"] is False
    assert res["errHidden"] is True


@_needs_node
def test_a_double_submit_creates_one_provider():
    res = _run(_DIALOG_HARNESS + """
      (async function(){
        var release;
        var gate = new Promise(function(r){ release = r; });
        var dlg = setup(function(path){ return gate.then(function(){ return OK; }); });
        $('#pa-open').fire('click');
        fill({ 'pa-name': 'My LLM', 'pa-base-url': 'https://x.example.com/v1' });
        submit(); submit(); submit();
        var busy = { label: $('#pa-submit').textContent, disabled: $('#pa-submit').disabled };
        release(); await settle();
        console.log(JSON.stringify({ busy: busy, paths: paths() }));
      })();
    """, _DIALOG_FUNCS)
    # (the label ends in an ellipsis; the console's code page is not this test's business)
    assert res["busy"]["label"].startswith("Adding") and res["busy"]["disabled"] is True
    assert res["paths"] == ["POST /api/custom-providers"]
