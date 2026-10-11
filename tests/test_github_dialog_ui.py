r"""The Build page's GitHub dialog: one-click connect, an existing repository,
visibility and unlink.

REQUESTED 2026-10-11 on top of the first GitHub push UI (paste a token, create a
repo, Push / Sync). The backend routes are a separate piece; this file pins the
page's half of the contract and the things that are easy to get wrong:

  * "Connect with GitHub" runs GitHub's device login: the one-time code is shown
    large (and spoken), "Open GitHub" only ever points at GitHub's own device
    page, the state is polled every 3 s ONLY while a login is pending (or the gh
    tool is installing) AND the dialog is open, and a countdown follows the
    server's expires_in;
  * the pasted token stays, collapsed under "Or paste a token instead"; the
    Client ID field appears only once device/start says no_client_id;
  * a public repository -- created or switched -- needs the tick "Anyone on the
    internet can see this code"; linking a repository that has files needs the
    overwrite tick;
  * Unlink is a confirm step inside the dialog (never window.confirm), and Esc
    first backs out of an open question;
  * every backend string goes in as text (innerHTML is only ever cleared), the
    server's own error sentence is shown first, and the CSS is theme tokens only.

Like tests/test_publish_ui.py, the markup is read as text and the whole state
machine is ALSO run under node against a tiny fake DOM (fake timers, fake api()).
The fake's parent links are computed from the real markup, so they cannot drift.
"""
import html.parser
import io
import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

HTML = io.open("templates/index.html", encoding="utf-8").read()


def _between(start, end, src=HTML):
    i = src.index(start)
    return src[i:src.index(end, i)]


JS = _between("/* github-js:start */", "/* github-js:end */")
CSS = _between("/* github-css:start */", "/* github-css:end */")
DIALOG = _between('<dialog class="publish-dialog github-dialog"', "</dialog>")

CONTRACT_ROUTES = {
    "/api/github", "/api/github/token", "/api/github/token/delete", "/api/github/preview",
    "/api/github/gitignore", "/api/github/create", "/api/github/push", "/api/github/sync",
    "/api/github/client-id", "/api/github/device/start", "/api/github/device",
    "/api/github/device/cancel", "/api/github/repos", "/api/github/link",
    "/api/github/visibility", "/api/github/unlink",
}
NEW_CODES = ["bad_client_id", "no_client_id", "disabled", "github_error", "needs_overwrite_ok",
             "repo_not_owned", "no_push_permission", "already_linked", "not_found",
             "public_ok_required"]


class _Ancestry(html.parser.HTMLParser):
    """id -> the ids of its ancestors, nearest first."""
    VOID = {"input", "br", "img", "meta", "link", "hr", "source", "wbr", "col", "area", "base",
            "embed", "track", "param"}

    def __init__(self):
        super().__init__()
        self.stack, self.up = [], {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            self.up[a["id"]] = [x for x in reversed(self.stack) if x]
        if tag not in self.VOID:
            self.stack.append(a.get("id") or "")

    def handle_startendtag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            self.up[a["id"]] = [x for x in reversed(self.stack) if x]

    def handle_endtag(self, tag):
        if tag not in self.VOID and self.stack:
            self.stack.pop()


def _ancestors():
    p = _Ancestry()
    p.feed(DIALOG + "</dialog>")
    return p.up


ANC = _ancestors()
# nearest id'd ancestor of every id in the dialog: the fake DOM's parent links
PARENT = {k: v[0] for k, v in ANC.items() if v}


def _section(anid):
    """The markup of one element with an id (to its matching close tag)."""
    i = DIALOG.index('id="%s"' % anid)
    start = DIALOG.rindex("<", 0, i)
    tag = re.match(r"<(\w+)", DIALOG[start:]).group(1)
    depth, pos = 0, start
    pat = re.compile(r"<(/?)%s\b[^>]*>" % tag)
    for m in pat.finditer(DIALOG, start):
        depth += -1 if m.group(1) else 1
        if depth == 0:
            return DIALOG[start:m.end()]
    raise AssertionError("unclosed " + anid)


# --------------------------------------------------------------------------- #
# Markup
# --------------------------------------------------------------------------- #

def test_the_new_ids_exist_and_every_id_the_script_reads_is_in_the_markup():
    for anid in ("github-connect-btn", "github-device", "github-device-code", "github-device-copy",
                 "github-device-open", "github-device-wait", "github-device-time",
                 "github-device-installing", "github-device-progress", "github-device-msg",
                 "github-device-cancel", "github-device-retry", "github-token-details",
                 "github-clientid-box", "github-client-id", "github-client-save",
                 "github-mode-new", "github-mode-existing", "github-desc", "github-repo-list",
                 "github-repo-rows", "github-repo-filter", "github-repos-status",
                 "github-repos-retry", "github-overwrite-row", "github-overwrite-ok",
                 "github-link-btn", "github-make-private", "github-make-public",
                 "github-public-confirm", "github-make-public-ok", "github-make-public-yes",
                 "github-make-public-no", "github-unlink", "github-unlink-confirm",
                 "github-unlink-text", "github-unlink-yes", "github-unlink-no",
                 "github-foot-err", "github-manage-err", "github-token-err", "github-client-err"):
        assert ('id="%s"' % anid) in DIALOG, anid
    used = set(re.findall(r"\$\('#([\w-]+)'\)", JS))
    assert used, "the script reads its controls by id"
    for anid in used:
        assert ('id="%s"' % anid) in HTML, anid


def test_the_code_is_large_spoken_and_the_countdown_is_a_quiet_timer():
    code = _section("github-device-code")
    assert 'aria-live="polite"' in code
    assert re.search(r"\.gh-code\{[^}]*font-family:var\(--mono\)[^}]*letter-spacing:", CSS)
    assert re.search(r"\.gh-code\{[^}]*font-size:2\dpx", CSS)
    timer = _section("github-device-time")
    assert 'role="timer"' in timer and 'aria-live="off"' in timer
    assert "Waiting for you to approve on GitHub…" in _section("github-device-pending")


def test_open_github_is_a_new_tab_link_without_opener_or_referrer():
    a = _section("github-device-open")
    assert a.startswith("<a ")
    assert 'target="_blank"' in a and 'rel="noopener noreferrer"' in a
    assert 'href="#"' in a, "the real address is set only after the allow-check"
    assert "Open GitHub" in a


def test_the_device_link_allow_check_is_exact():
    assert "var GH_DEVICE_URL = 'https://github.com/login/device';" in JS
    assert re.search(r"function ghDeviceUrl\(u\)\{ return u === GH_DEVICE_URL \? u : ''; \}", JS)
    # every place that sets the link's address goes through the check
    for m in re.finditer(r"devOpen\.setAttribute\('href',\s*([^)]+)\)", JS):
        assert m.group(1).strip() in ("u || '#'", "'#'"), m.group(0)


def test_the_token_form_lives_in_a_collapsed_details_with_the_client_id_box():
    det = _section("github-token-details")
    assert det.startswith("<details") and " open" not in det.split(">", 1)[0]
    assert "<summary" in det and "Or paste a token instead" in det
    assert re.search(r'<input type="password" id="github-token"', det)
    assert 'id="github-save"' in det
    assert "https://github.com/settings/personal-access-tokens/new" in det
    box = _section("github-clientid-box")
    assert " hidden" in box.split(">", 1)[0], "the Client ID field starts hidden"
    assert "One-click connect needs a GitHub app Client ID." in box
    assert 'for="github-client-id"' in box


def test_installing_the_gh_tool_is_said_in_words():
    box = _section("github-device-installing")
    assert "Installing GitHub's official tool (gh)… this happens once." in box
    assert 'id="github-device-progress"' in box


def test_the_connect_button_and_the_device_controls_sit_in_the_footer():
    foot = _section("github-foot-connect")
    for anid in ("github-connect-btn", "github-device-cancel", "github-device-retry"):
        assert ('id="%s"' % anid) in foot
    assert ">Connect with GitHub<" in foot and ">Try again<" in foot and ">Cancel<" in foot


def test_new_or_existing_is_a_keyboard_radio_group():
    mode = _section("github-mode")
    assert mode.startswith("<fieldset") and "<legend>" in mode
    assert mode.count('type="radio" name="github-mode"') == 2
    assert "New repository" in mode and "Existing repository" in mode
    assert re.search(r'id="github-mode-new" value="new" checked', mode)


def test_public_needs_the_tick_when_created_and_when_switched():
    tick = "Anyone on the internet can see this code"
    assert tick in _section("github-public-row")
    assert tick in _section("github-public-confirm")
    assert 'type="checkbox" id="github-make-public-ok"' in _section("github-public-confirm")
    # the script refuses to send public without the tick, in both places
    assert "var needTick = visPublic.checked && !publicOk.checked;" in JS
    assert "if (!makePublicOk.checked) return;" in JS
    assert "makePublicYes.disabled = b || !makePublicOk.checked;" in JS


def test_linking_a_repository_with_files_needs_the_overwrite_tick():
    row = _section("github-overwrite-row")
    assert ("This repository already has files. Pushing will make its latest version match this "
            "project (older versions stay in the history).") in row
    assert 'type="checkbox" id="github-overwrite-ok"' in row
    assert "linkBtn.disabled = b || !r || (needOw && !overwriteOk.checked);" in JS
    assert "if (code === 'needs_overwrite_ok')" in JS


def test_unlink_is_a_confirm_step_in_the_dialog():
    box = _section("github-unlink-confirm")
    assert 'id="github-unlink-yes"' in box and 'id="github-unlink-no"' in box
    assert PARENT["github-unlink-confirm"] == "github-foot"
    assert "'Forget the link to ' + String(repo.full_name || 'this repository') +" in JS
    assert "'? Nothing is deleted on GitHub or in your project.'" in JS
    assert not re.search(r"\b(window\.)?confirm\(", JS), "no browser confirm box"


def test_the_linked_state_keeps_push_sync_and_disconnect_and_adds_manage():
    linked = _section("github-linked")
    for anid in ("github-repo-url", "github-visibility", "github-pushed", "github-counts",
                 "github-make-private", "github-make-public", "github-unlink", "github-files"):
        assert ('id="%s"' % anid) in linked, anid
    assert 'id="github-disconnect"' in DIALOG
    assert 'id="github-push"' in _section("github-foot-linked")
    assert 'id="github-sync"' in _section("github-foot-linked")


def test_errors_sit_next_to_their_action():
    assert PARENT["github-foot-err"] == "github-foot"
    assert "github-token-details" in ANC["github-token-err"]
    assert "github-token-details" in ANC["github-save"]
    assert "github-clientid-box" in ANC["github-client-err"]
    assert "github-linked" in ANC["github-manage-err"]
    for anid in ("github-foot-err", "github-token-err", "github-client-err", "github-manage-err"):
        assert 'role="alert"' in _section(anid), anid


# --------------------------------------------------------------------------- #
# Script, read as text
# --------------------------------------------------------------------------- #

def test_only_the_contract_routes_are_called():
    paths = set()
    for m in re.finditer(r"api\('(/api/[^'?]*)", JS):
        paths.add(m.group(1))
    assert paths and paths <= CONTRACT_ROUTES, sorted(paths - CONTRACT_ROUTES)
    for p in ("/api/github/device/start", "/api/github/device", "/api/github/device/cancel",
              "/api/github/client-id", "/api/github/repos", "/api/github/link",
              "/api/github/visibility", "/api/github/unlink"):
        assert p in paths, p


def test_post_bodies_carry_what_the_contract_needs():
    assert "{ project_dir: dir, name: r.name, confirm: true }" in JS
    assert "body.overwrite_ok = true" in JS
    assert "{ project_dir: dir, private: !!makePrivate }" in JS and "body.public_ok = true" in JS
    assert "{ project_dir: dir, confirm: true } })" in JS          # unlink
    assert "body:{ client_id: id }" in JS
    assert "if (desc) body.description = desc;" in JS


def test_polling_runs_only_while_pending_or_installing_and_open():
    assert "function devWanted(){ return isOpen && devActive(); }" in JS
    assert "function devActive(){ return device.state === 'pending' || device.state === 'installing'; }" in JS
    assert "setInterval(pollDevice, 3000)" in JS
    assert JS.count("setInterval(") == 2, "one poll timer, one countdown tick"
    # closing (X, Esc, backdrop, Files tab) re-evaluates the timers
    close = _between("dlg.addEventListener('close'", "});", JS)
    assert "syncDevTimers()" in close


def test_backend_text_is_text_and_innerhtml_is_only_cleared():
    for m in re.finditer(r"\.innerHTML\s*=\s*([^;]+);", JS):
        assert m.group(1).strip() in ("''", '""'), m.group(0)
    assert "insertAdjacentHTML" not in JS and "outerHTML" not in JS
    assert "document.write" not in JS
    assert "nm.textContent = r.name" in JS and "ds.textContent = String(r.description)" in JS


def test_every_new_code_has_plain_words_and_the_server_sentence_comes_first():
    for code in NEW_CODES:
        assert re.search(r"\b%s: '" % code, JS), code
    body = _between("function errText(err, fallback){", "\n      }", JS)
    assert body.index("b.error") < body.index("GH_ERRORS[code]")


def test_the_new_css_is_theme_tokens_only():
    css = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css)
    assert not re.search(r"\b(rgba?|hsla?|oklch|lab|lch|color-mix)\(", css)
    assert not re.search(r"\b\d+(\.\d+)?(vw|vh|dvh|svh|lvh)\b", css), "the root is zoomed: no viewport units"
    for name in ("white", "black", "red", "green", "blue", "gray", "grey", "orange", "purple"):
        assert not re.search(r":\s*%s\b" % name, css), name


# --------------------------------------------------------------------------- #
# The state machine, run against a fake DOM, fake timers and a fake api()
# --------------------------------------------------------------------------- #

HARNESS = r"""
const fs = require('fs');
const code = fs.readFileSync(process.argv[2], 'utf8');
const PARENT = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const flush = () => new Promise(r => setImmediate(r));
const settle = async () => { for (let i = 0; i < 16; i++) await flush(); };
const GH = 'https://github.com/login/device';

class El {
  constructor(id, env, tag){
    this.id = id; this.env = env; this.tag = tag || ''; this.hidden = false; this.textContent = '';
    this.title = ''; this.value = ''; this.checked = false; this.disabled = false; this.open = false;
    this.attrs = {}; this._cls = new Set(); this.l = {}; this.children = []; this.parentNode = null;
    this.className = '';
    const self = this;
    this.classList = {
      toggle(n, on){ if (on === undefined) on = !self._cls.has(n); if (on) self._cls.add(n); else self._cls.delete(n); },
      add(n){ self._cls.add(n); }, remove(n){ self._cls.delete(n); }, contains(n){ return self._cls.has(n); },
    };
  }
  set innerHTML(v){ if (v !== '') throw new Error('innerHTML with content: ' + v); this.children = []; }
  setAttribute(k, v){ this.attrs[k] = String(v); }
  getAttribute(k){ return k in this.attrs ? this.attrs[k] : null; }
  addEventListener(t, fn){ (this.l[t] = this.l[t] || []).push(fn); }
  fire(t, e){ (this.l[t] || []).forEach(f => f(e || {})); }
  click(){
    const ev = {target: this, defaultPrevented: false, preventDefault(){ this.defaultPrevented = true; }};
    if (this.disabled) return ev;            // a disabled button fires nothing
    this.fire('click', ev); return ev;
  }
  focus(){ this.env.focused = this; }
  appendChild(c){ this.children.push(c); c.parentNode = this; return c; }
  has(c){ return this._cls.has(c); }
  showModal(){ if (this.open) throw new Error('InvalidStateError'); this.open = true; this.env.modals++; }
  close(){ if (!this.open) return; this.open = false; setImmediate(() => this.fire('close', {})); }
  contains(x){ for (let n = x; n; n = this.env.up(n)) if (n === this) return true; return false; }
  closest(sel){
    if (sel !== '[hidden]') throw new Error('unsupported selector ' + sel);
    for (let n = this; n; n = this.env.up(n)) if (n.hidden) return n;
    return null;
  }
}

function makeEnv(){
  let now = 0, nextId = 1, timers = [];
  const env = { calls: [], copies: [], opens: [], els: {}, focused: null, handler: null, modals: 0,
                unexpected: [] };
  const setI = (fn, ms) => { const t = {id: nextId++, at: now + ms, every: ms, fn}; timers.push(t); return t.id; };
  const setT = (fn, ms) => { const t = {id: nextId++, at: now + (ms || 0), every: 0, fn}; timers.push(t); return t.id; };
  const clr = id => { timers = timers.filter(t => t.id !== id); };
  env.now = () => now;
  env.intervals = () => timers.filter(t => t.every).length;
  env.advance = async function(ms){
    const end = now + ms;
    for (;;){
      const due = timers.filter(t => t.at <= end).sort((a, b) => a.at - b.at)[0];
      if (!due) break;
      now = due.at;
      if (due.every) due.at += due.every; else timers = timers.filter(t => t !== due);
      due.fn();
      await settle();
    }
    now = end;
    await settle();
  };
  env.el = id => env.els[id] || (env.els[id] = new El(id, env));
  env.up = n => n.parentNode || (PARENT[n.id] ? env.el(PARENT[n.id]) : null);
  const $ = sel => env.el(sel.replace(/^#/, ''));
  const body = new El('body', env);
  const doc = { body: body, createElement: tag => new El('', env, tag),
                get activeElement(){ return env.focused || body; } };
  env.body = body;
  env.focusId = () => env.focused ? (env.focused.id || '(dynamic)') : null;
  env.esc = async function(){
    const d = env.el('github-dialog'), ev = {defaultPrevented: false, preventDefault(){ this.defaultPrevented = true; }};
    d.fire('cancel', ev);
    if (!ev.defaultPrevented) d.close();
    await settle();
  };
  const win = { open(u, t, f){ env.opens.push([u, t, f]); return null; } };
  const api = (path, opts) => {
    env.calls.push({path: path, method: opts && opts.method, body: opts && opts.body});
    try { return Promise.resolve(env.handler(path, opts)); } catch (e) { return Promise.reject(e); }
  };
  const copyText = (t, b) => { env.copies.push(t); b.textContent = 'Copied!'; };
  const fn = new Function('$', 'api', 'toast', 'copyText', 'document', 'window', 'performance',
                          'setInterval', 'clearInterval', 'setTimeout', 'clearTimeout',
                          code + '\nreturn github;');
  env.github = fn($, api, () => {}, copyText, doc, win, {now: () => now}, setI, clr, setT, clr);
  env.posts = p => env.calls.filter(c => c.method === 'POST' && (!p || c.path === p));
  env.devGets = () => env.calls.filter(c => c.path === '/api/github/device' && !c.method).length;
  return env;
}
const err = (code, status, msg) => Object.assign(new Error(msg || 'server said no'),
  {body: msg ? {code: code, error: msg} : {code: code}, status: status || 400});
const txt = (e, id) => e.el(id).textContent;
const shown = (e, id) => !e.el(id).hidden;
const PROJ = {dir: '/x/myproj', is_repo: false, has_remote: false, repo: null};
const sConnect = oauth => ({connected: false, login: null, masked_hint: null, oauth_available: oauth, project: PROJ});
const sCreate = () => ({connected: true, login: 'octo', masked_hint: 'ghp_…abcd', oauth_available: true, project: PROJ});
const sLinked = priv => ({connected: true, login: 'octo', masked_hint: 'ghp_…abcd', oauth_available: true,
  project: {dir: '/x/myproj', is_repo: true, has_remote: true,
            repo: {full_name: 'octo/myproj', html_url: 'https://github.com/octo/myproj', private: priv},
            last_pushed: 1000, ahead: 0, behind: 0}});
const PREVIEW = {total_files: 1, files: ['index.html'], findings: []};
const clock = s => { s = Math.max(0, Math.ceil(s)); return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); };
function route(e, table){
  e.handler = (p, o) => {
    const key = (o && o.method ? 'POST ' : '') + p.split('?')[0];
    if (key in table){ const v = table[key]; return typeof v === 'function' ? v(o, p) : v; }
    e.unexpected.push(key); throw new Error('unexpected ' + key);
  };
}
async function opened(e, dir){
  e.github.attach(dir || '/x/myproj'); await settle();
  e.el('preview-github').click(); await settle();
}

(async () => {
  const R = {};

  // A: Connect with GitHub -> code, copy, open, countdown, poll only while open, connected -> create
  {
    const e = makeEnv(); let dev = {state: 'idle'}, startAt = 0, connected = false;
    const pending = () => ({state: 'pending', user_code: 'WDJB-MJHT', verification_uri: GH,
                            expires_in: 900 - (e.now() - startAt) / 1000, interval: 5});
    route(e, {
      '/api/github': () => connected ? sCreate() : sConnect(true),
      '/api/github/device': () => dev.state === 'pending' ? pending() : dev,
      'POST /api/github/device/start': () => { startAt = e.now(); dev = {state: 'pending'}; return pending(); },
    });
    await opened(e);
    const a = R.A = {};
    a.open = !!e.el('github-dialog').open; a.inBody = e.el('github-dialog').parentNode === e.body;
    a.focusOpen = e.focusId(); a.devGetsOnOpen = e.devGets();
    a.connectShown = shown(e, 'github-connect-btn'); a.footShown = shown(e, 'github-foot-connect');
    a.detailsOpen = e.el('github-token-details').open; a.clientShown = shown(e, 'github-clientid-box');
    a.deviceShown = shown(e, 'github-device'); a.intervals0 = e.intervals();
    e.el('github-connect-btn').click(); await settle();
    a.startPost = e.posts('/api/github/device/start').map(c => c.body);
    a.code = txt(e, 'github-device-code'); a.openShown = shown(e, 'github-device-open');
    a.href = e.el('github-device-open').getAttribute('href'); a.focus = e.focusId();
    a.time = txt(e, 'github-device-time'); a.intervals1 = e.intervals();
    a.cancelShown = shown(e, 'github-device-cancel'); a.connectHidden = e.el('github-connect-btn').hidden;
    a.wait = shown(e, 'github-device-wait');
    let g = e.devGets(); await e.advance(9000); a.polls9 = e.devGets() - g;
    a.time9 = txt(e, 'github-device-time'); a.time9want = clock(900 - e.now() / 1000);
    e.el('github-device-copy').click(); await e.advance(300);
    a.copies = e.copies; a.sayCopy = txt(e, 'github-say');
    const ev = e.el('github-device-open').click();
    a.opens = e.opens; a.prevented = ev.defaultPrevented;
    e.el('github-close').click(); await settle();
    a.closedIntervals = e.intervals(); a.focusClosed = e.focusId();
    g = e.devGets(); await e.advance(30000); a.pollsClosed = e.devGets() - g;
    e.el('preview-github').click(); await settle();
    a.reopenIntervals = e.intervals(); a.reopenCode = txt(e, 'github-device-code');
    a.reopenTime = txt(e, 'github-device-time'); a.reopenWant = clock(900 - e.now() / 1000);
    dev = {state: 'connected', login: 'octo'}; connected = true;
    await e.advance(3000); await e.advance(100);
    a.after = {connectBox: e.el('github-connect').hidden, create: shown(e, 'github-create'),
               intervals: e.intervals(), say: txt(e, 'github-say'), account: shown(e, 'github-account')};
    g = e.devGets(); await e.advance(12000); a.pollsAfter = e.devGets() - g;
    a.unexpected = e.unexpected;
  }

  // B: a verification_uri that is not exactly GitHub's device page never becomes a link
  {
    const out = R.B = [];
    for (const uri of ['https://github.com.evil.example/login/device', 'http://github.com/login/device',
                       'https://github.com/login/device?next=x', 'javascript:alert(1)', GH]){
      const e = makeEnv();
      route(e, {
        '/api/github': sConnect(true), '/api/github/device': {state: 'idle'},
        'POST /api/github/device/start': {user_code: 'AAAA-BBBB', verification_uri: uri, expires_in: 900, interval: 5},
      });
      await opened(e);
      e.el('github-connect-btn').click(); await settle();
      const ev = e.el('github-device-open').click();
      out.push({uri: uri, shown: shown(e, 'github-device-open'), href: e.el('github-device-open').getAttribute('href'),
                opens: e.opens.length, prevented: ev.defaultPrevented, code: txt(e, 'github-device-code')});
    }
  }

  // C: installing gh -> words + progress, polls, then a code; Cancel stops everything
  {
    const e = makeEnv(); let dev = {state: 'idle'};
    route(e, {
      '/api/github': sConnect(true), '/api/github/device': () => dev,
      'POST /api/github/device/start': () => (dev = {state: 'installing', progress: 0.42}),
      'POST /api/github/device/cancel': () => { dev = {state: 'idle'}; return {ok: true}; },
    });
    await opened(e);
    e.el('github-connect-btn').click(); await settle();
    const c = R.C = {};
    c.installing = shown(e, 'github-device-installing'); c.progress = txt(e, 'github-device-progress');
    c.cancel = shown(e, 'github-device-cancel'); c.connectHidden = e.el('github-connect-btn').hidden;
    c.intervals = e.intervals(); c.focus = e.focusId();
    await e.advance(100); c.say = txt(e, 'github-say');
    dev = {state: 'installing', progress: 'Unpacking gh'};
    let g = e.devGets(); await e.advance(3000); c.polls = e.devGets() - g; c.progress2 = txt(e, 'github-device-progress');
    dev = {state: 'installing'};
    await e.advance(3000); c.progress3 = txt(e, 'github-device-progress');
    dev = {state: 'pending', user_code: 'ABCD-1234', verification_uri: GH, expires_in: 900};
    await e.advance(3000);
    c.code = txt(e, 'github-device-code'); c.intervals2 = e.intervals(); c.installingHidden = e.el('github-device-installing').hidden;
    e.el('github-device-cancel').click(); await settle();
    c.cancelPosts = e.posts('/api/github/device/cancel').length;
    c.after = {connect: shown(e, 'github-connect-btn'), device: shown(e, 'github-device'), intervals: e.intervals(),
               focus: e.focusId()};
    g = e.devGets(); await e.advance(9000); c.pollsAfter = e.devGets() - g;
  }

  // D: expired (by the clock), denied and error -> plain words + Try again, polling stops
  {
    const d = R.D = {};
    {
      const e = makeEnv(); let startAt = 0;
      route(e, {
        '/api/github': sConnect(true),
        '/api/github/device': () => ({state: 'pending', user_code: 'EXPI-RING', verification_uri: GH,
                                      expires_in: 5 - (e.now() - startAt) / 1000}),
        'POST /api/github/device/start': () => { startAt = e.now(); return {user_code: 'EXPI-RING', verification_uri: GH, expires_in: 5, interval: 5}; },
      });
      await opened(e);
      e.el('github-connect-btn').click(); await settle();
      await e.advance(6000);
      d.expired = {msg: txt(e, 'github-device-msg'), retry: shown(e, 'github-device-retry'), intervals: e.intervals(),
                   focus: e.focusId(), pending: shown(e, 'github-device-pending'), cancel: shown(e, 'github-device-cancel')};
      e.el('github-device-retry').click(); await settle();
      d.retryStarts = e.posts('/api/github/device/start').length; d.retryCode = txt(e, 'github-device-code');
    }
    for (const [name, st] of [['denied', {state: 'denied'}], ['error', {state: 'error', error: 'GitHub said <b>no</b>'}],
                              ['errorPlain', {state: 'error'}]]){
      const e = makeEnv(); let dev = {state: 'idle'};
      route(e, {
        '/api/github': sConnect(true), '/api/github/device': () => dev,
        'POST /api/github/device/start': () => (dev = {state: 'pending', user_code: 'XXXX-YYYY', verification_uri: GH, expires_in: 900}),
      });
      await opened(e);
      e.el('github-connect-btn').click(); await settle();
      dev = st; await e.advance(3000);
      d[name] = {msg: txt(e, 'github-device-msg'), retry: shown(e, 'github-device-retry'), intervals: e.intervals()};
    }
  }

  // E: no_client_id -> the Client ID box (and only then); save it; a bad one shows the server's words
  {
    const e = makeEnv(); let oauth = true;
    route(e, {
      '/api/github': () => sConnect(oauth), '/api/github/device': {state: 'idle'},
      'POST /api/github/device/start': () => Promise.reject(err('no_client_id', 400)),
      'POST /api/github/client-id': () => ({ok: true, oauth_available: true}),
    });
    await opened(e);
    const x = R.E = {};
    x.clientBefore = shown(e, 'github-clientid-box');
    e.el('github-connect-btn').click(); await settle();
    x.footErr = txt(e, 'github-foot-err'); x.clientShown = shown(e, 'github-clientid-box');
    x.detailsOpen = e.el('github-token-details').open; x.focus = e.focusId();
    x.saveDisabled = e.el('github-client-save').disabled;
    e.el('github-client-id').value = '  Ov23liABC  '; e.el('github-client-id').fire('input');
    e.el('github-client-save').click(); await settle();
    x.post = e.posts('/api/github/client-id').map(c => c.body);
    x.clientAfter = shown(e, 'github-clientid-box'); x.focusAfter = e.focusId();
    x.clientValue = e.el('github-client-id').value;
    const e2 = makeEnv();
    route(e2, {
      '/api/github': sConnect(true), '/api/github/device': {state: 'idle'},
      'POST /api/github/device/start': () => Promise.reject(err('no_client_id', 400, 'Set a GitHub app Client ID first.')),
      'POST /api/github/client-id': () => Promise.reject(err('bad_client_id', 400, 'Client IDs look like Iv1.<hex> or Ov23li<...>')),
    });
    await opened(e2);
    e2.el('github-connect-btn').click(); await settle();
    x.footErrServer = txt(e2, 'github-foot-err');
    e2.el('github-client-id').value = 'nope'; e2.el('github-client-id').fire('input');
    e2.el('github-client-save').click(); await settle();
    x.badText = txt(e2, 'github-client-err'); x.badShown = shown(e2, 'github-client-err');
    x.stillShown = shown(e2, 'github-clientid-box');
  }

  // F: no one-click connect available -> no Connect button; the token form opens; it still works
  {
    const e = makeEnv(); let connected = false;
    route(e, {
      '/api/github': () => connected ? sCreate() : sConnect(false), '/api/github/device': {state: 'idle'},
      'POST /api/github/token': () => { connected = true; return {login: 'octo', masked_hint: 'ghp_…abcd'}; },
    });
    await opened(e);
    const f = R.F = {};
    f.connectBtn = shown(e, 'github-connect-btn'); f.foot = shown(e, 'github-foot-connect');
    f.detailsOpen = e.el('github-token-details').open; f.client = shown(e, 'github-clientid-box');
    f.saveDisabled = e.el('github-save').disabled;
    e.el('github-token').value = 'ghp_secret'; e.el('github-token').fire('input');
    e.el('github-save').click(); await settle();
    f.post = e.posts('/api/github/token').map(c => c.body);
    f.create = shown(e, 'github-create'); f.tokenCleared = e.el('github-token').value;
  }

  // G: a new repository -> name prefilled, description, public only with the tick
  {
    const e = makeEnv(); let linked = false;
    route(e, {
      '/api/github': () => linked ? sLinked(false) : sCreate(), '/api/github/preview': PREVIEW,
      'POST /api/github/create': () => { linked = true; return sLinked(false); },
    });
    await opened(e);
    const g = R.G = {};
    g.name = e.el('github-name').value; g.create = shown(e, 'github-create-btn'); g.link = shown(e, 'github-link-btn');
    g.newPanel = shown(e, 'github-new'); g.existingPanel = shown(e, 'github-existing');
    g.publicRow0 = shown(e, 'github-public-row');
    e.el('github-vis-private').checked = false; e.el('github-vis-public').checked = true; e.el('github-vis-public').fire('change');
    g.publicRow = shown(e, 'github-public-row'); g.disabled = e.el('github-create-btn').disabled; g.why = txt(e, 'github-create-why');
    e.el('github-create-btn').click(); await settle(); g.postsWithoutTick = e.posts('/api/github/create').length;
    e.el('github-public-ok').checked = true; e.el('github-public-ok').fire('change');
    g.enabled = !e.el('github-create-btn').disabled;
    e.el('github-desc').value = '  A demo  ';
    e.el('github-create-btn').click(); await settle();
    g.body = e.posts('/api/github/create')[0].body; g.linked = shown(e, 'github-linked');
  }

  // H: an existing repository -> list, chips, filter, overwrite tick, link
  {
    const many = [];
    for (let i = 0; i < 10; i++) many.push({name: 'r' + i, private: true, empty: false, updated_at: '', html_url: 'https://github.com/octo/r' + i, description: ''});
    many.push({name: 'alpha-site', private: false, empty: false, html_url: 'https://github.com/octo/alpha-site', description: '<img src=x onerror=alert(1)>'});
    many.push({name: 'alpha-empty', private: true, empty: true, html_url: 'https://github.com/octo/alpha-empty', description: null});
    const rows = e => e.el('github-repo-rows').children;
    const pick = (e, name) => {
      const lab = rows(e).find(l => l.children[0].value === name);
      lab.children[0].checked = true; lab.children[0].fire('change');
    };
    const h = R.H = {};
    {
      const e = makeEnv(); let linked = false;
      route(e, {
        '/api/github': () => linked ? sLinked(true) : sCreate(), '/api/github/preview': PREVIEW,
        '/api/github/repos': {repos: many},
        'POST /api/github/link': () => { linked = true; return {ok: true}; },
      });
      await opened(e);
      h.reposBefore = e.calls.filter(c => c.path === '/api/github/repos').length;
      e.el('github-mode-existing').checked = true; e.el('github-mode-existing').fire('change'); await settle();
      h.reposGets = e.calls.filter(c => c.path === '/api/github/repos').length;
      h.rows = rows(e).length; h.filter = shown(e, 'github-repo-filter-row');
      h.linkShown = shown(e, 'github-link-btn'); h.createHidden = e.el('github-create-btn').hidden;
      h.linkDisabled = e.el('github-link-btn').disabled; h.why = txt(e, 'github-create-why');
      const site = rows(e).find(l => l.children[0].value === 'alpha-site').children[1];
      h.siteParts = site.children.map(c => [c.className, c.textContent]);
      const emp = rows(e).find(l => l.children[0].value === 'alpha-empty').children[1];
      h.emptyParts = emp.children.map(c => [c.className, c.textContent]);
      e.el('github-repo-filter').value = 'alpha'; e.el('github-repo-filter').fire('input');
      h.filtered = rows(e).map(l => l.children[0].value);
      pick(e, 'alpha-site');
      h.ow = shown(e, 'github-overwrite-row'); h.owDisabled = e.el('github-link-btn').disabled; h.owWhy = txt(e, 'github-create-why');
      e.el('github-link-btn').click(); await settle(); h.postsWithoutTick = e.posts('/api/github/link').length;
      pick(e, 'alpha-empty');
      h.owEmpty = shown(e, 'github-overwrite-row'); h.emptyEnabled = !e.el('github-link-btn').disabled;
      pick(e, 'alpha-site');
      h.owTickReset = e.el('github-overwrite-ok').checked;
      e.el('github-overwrite-ok').checked = true; e.el('github-overwrite-ok').fire('change');
      h.enabled = !e.el('github-link-btn').disabled;
      e.el('github-link-btn').click(); await settle();
      h.body = e.posts('/api/github/link')[0].body; h.linked = shown(e, 'github-linked');
      h.statusReads = e.calls.filter(c => c.path.indexOf('/api/github?') === 0).length;
    }
    {
      const e = makeEnv();
      route(e, {
        '/api/github': sCreate(), '/api/github/repos': {repos: many.slice(10)},
        'POST /api/github/link': () => Promise.reject(err('needs_overwrite_ok', 409)),
      });
      await opened(e);
      e.el('github-mode-existing').checked = true; e.el('github-mode-existing').fire('change'); await settle();
      h.smallFilter = shown(e, 'github-repo-filter-row');
      pick(e, 'alpha-empty');
      h.emptyOw = shown(e, 'github-overwrite-row');
      e.el('github-link-btn').click(); await settle();
      h.firstBody = e.posts('/api/github/link')[0].body;
      h.needs = {ow: shown(e, 'github-overwrite-row'), focus: e.focusId(), err: txt(e, 'github-foot-err'),
                 disabled: e.el('github-link-btn').disabled};
      e.el('github-mode-new').checked = true; e.el('github-mode-new').fire('change');
      h.backToNew = {create: shown(e, 'github-create-btn'), link: shown(e, 'github-link-btn'), panel: shown(e, 'github-new')};
    }
    {
      const e = makeEnv();
      route(e, {'/api/github': sCreate(), '/api/github/repos': {repos: []}});
      await opened(e);
      e.el('github-mode-existing').checked = true; e.el('github-mode-existing').fire('change'); await settle();
      h.empty = {status: txt(e, 'github-repos-status'), list: shown(e, 'github-repo-list'), retry: shown(e, 'github-repos-retry')};
    }
    {
      const e = makeEnv(); let fails = true;
      route(e, {'/api/github': sCreate(),
                '/api/github/repos': () => fails ? Promise.reject(err('github_error', 502, 'GitHub is down')) : {repos: many.slice(10)}});
      await opened(e);
      e.el('github-mode-existing').checked = true; e.el('github-mode-existing').fire('change'); await settle();
      h.error = {status: txt(e, 'github-repos-status'), retry: shown(e, 'github-repos-retry')};
      fails = false; e.el('github-repos-retry').click(); await settle();
      h.retried = {gets: e.calls.filter(c => c.path === '/api/github/repos').length, rows: rows(e).length,
                   retry: shown(e, 'github-repos-retry')};
    }
  }

  // I: linked -> make public (tick, Esc backs out), make private, unlink (confirm step, Esc backs out)
  {
    const e = makeEnv(); let linked = true;
    route(e, {
      '/api/github': () => linked ? sLinked(true) : sCreate(), '/api/github/preview': PREVIEW,
      'POST /api/github/visibility': o => ({ok: true, private: o.body.private}),
      'POST /api/github/unlink': () => { linked = false; return {ok: true}; },
    });
    await opened(e);
    const i = R.I = {};
    i.chip = txt(e, 'github-visibility'); i.pub = shown(e, 'github-make-public'); i.priv = shown(e, 'github-make-private');
    i.repoHref = e.el('github-repo-url').getAttribute('href');
    e.el('github-make-public').click();
    i.ask = {confirm: shown(e, 'github-public-confirm'), focus: e.focusId(), yesDisabled: e.el('github-make-public-yes').disabled,
             pubHidden: e.el('github-make-public').hidden};
    await e.esc();
    i.esc = {confirm: shown(e, 'github-public-confirm'), open: !!e.el('github-dialog').open, focus: e.focusId()};
    e.el('github-make-public').click(); e.el('github-make-public-yes').click(); await settle();
    i.postsWithoutTick = e.posts('/api/github/visibility').length;
    e.el('github-make-public-ok').checked = true; e.el('github-make-public-ok').fire('change');
    e.el('github-make-public-yes').click(); await settle(); await e.advance(100);
    i.pubBody = e.posts('/api/github/visibility')[0].body;
    i.afterPub = {chip: txt(e, 'github-visibility'), priv: shown(e, 'github-make-private'), confirm: shown(e, 'github-public-confirm'),
                  focus: e.focusId(), say: txt(e, 'github-say'), isPublic: e.el('github-visibility').has('is-public')};
    e.el('github-make-private').click(); await settle();
    i.privBody = e.posts('/api/github/visibility')[1].body;
    i.afterPriv = {chip: txt(e, 'github-visibility'), pub: shown(e, 'github-make-public')};
    e.el('github-unlink').click();
    i.unlinkAsk = {confirm: shown(e, 'github-unlink-confirm'), text: txt(e, 'github-unlink-text'), focus: e.focusId(),
                   footLinked: shown(e, 'github-foot-linked')};
    await e.esc();
    i.unlinkEsc = {confirm: shown(e, 'github-unlink-confirm'), open: !!e.el('github-dialog').open, focus: e.focusId(),
                   footLinked: shown(e, 'github-foot-linked')};
    e.el('github-unlink').click(); e.el('github-unlink-no').click();
    i.unlinkNo = {confirm: shown(e, 'github-unlink-confirm'), focus: e.focusId()};
    i.postsBeforeYes = e.posts('/api/github/unlink').length;
    e.el('github-unlink').click(); e.el('github-unlink-yes').click(); await settle();
    i.unlinkBody = e.posts('/api/github/unlink').map(c => c.body);
    i.afterUnlink = {create: shown(e, 'github-create'), linked: shown(e, 'github-linked'), confirm: shown(e, 'github-unlink-confirm'),
                     name: e.el('github-name').value};
  }

  // J: errors -> the server's sentence first, the plain words for a bare code, next to the action
  {
    const e = makeEnv(); let mode = 'msg';
    route(e, {
      '/api/github': sLinked(true), '/api/github/preview': PREVIEW,
      'POST /api/github/push': () => Promise.reject(mode === 'msg' ? err('behind_remote', 409, 'GitHub is ahead, press Sync') : err('behind_remote', 409)),
      'POST /api/github/visibility': () => Promise.reject(err('public_ok_required', 400)),
    });
    await opened(e);
    const j = R.J = {};
    e.el('github-push').click(); await settle(); j.server = txt(e, 'github-foot-err');
    mode = 'bare'; e.el('github-push').click(); await settle(); j.plain = txt(e, 'github-foot-err');
    j.shown = shown(e, 'github-foot-err');
    e.el('github-make-public').click(); e.el('github-make-public-ok').checked = true; e.el('github-make-public-ok').fire('change');
    e.el('github-make-public-yes').click(); await settle();
    j.vis = {err: txt(e, 'github-manage-err'), confirm: shown(e, 'github-public-confirm'), tick: e.el('github-make-public-ok').checked};
  }

  // K: busy -> the clicked button says what it is doing and nothing else can be pressed
  {
    const e = makeEnv(); let release;
    route(e, {'/api/github': sLinked(true), '/api/github/preview': PREVIEW,
              'POST /api/github/push': () => new Promise(r => { release = r; })});
    await opened(e);
    e.el('github-push').click(); await settle();
    R.K = {push: [txt(e, 'github-push'), e.el('github-push').disabled], sync: e.el('github-sync').disabled,
           unlink: e.el('github-unlink').disabled, pub: e.el('github-make-public').disabled,
           disconnect: e.el('github-disconnect').disabled};
    release(sLinked(true)); await settle();
    R.K.after = [txt(e, 'github-push'), e.el('github-push').disabled];
  }

  // L: leaving the project stops the device timers and closes the dialog
  {
    const e = makeEnv();
    route(e, {'/api/github': sConnect(true), '/api/github/device': {state: 'pending', user_code: 'Q', verification_uri: GH, expires_in: 900},
              'POST /api/github/device/start': {user_code: 'Q', verification_uri: GH, expires_in: 900}});
    await opened(e);
    e.el('github-connect-btn').click(); await settle();
    const before = e.intervals();
    e.github.paneVisible(false); await settle();
    const pane = {open: !!e.el('github-dialog').open, intervals: e.intervals()};
    e.github.detach(); await settle();
    R.L = {before: before, pane: pane, detached: e.intervals(), open: !!e.el('github-dialog').open};
  }

  console.log(JSON.stringify(R));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


def _node():
    return shutil.which("node")


@pytest.fixture(scope="module")
def run():
    if not _node():
        pytest.skip("node is not installed")
    d = tempfile.mkdtemp()
    code_path = os.path.join(d, "github.js")
    io.open(code_path, "w", encoding="utf-8").write(JS)
    parent_path = os.path.join(d, "parent.json")
    io.open(parent_path, "w", encoding="utf-8").write(json.dumps(PARENT))
    harness = os.path.join(d, "harness.js")
    io.open(harness, "w", encoding="utf-8").write(HARNESS)
    out = subprocess.run([_node(), harness, code_path, parent_path],
                         capture_output=True, text=True, encoding="utf-8", timeout=180)
    assert out.returncode == 0, (out.stdout + out.stderr)[-3000:]
    return json.loads(out.stdout)


def test_connect_shows_the_code_and_polls_only_while_pending_and_open(run):
    a = run["A"]
    assert a["open"] and a["inBody"] and a["focusOpen"] == "github-title"
    assert a["devGetsOnOpen"] == 1, "one read on open picks a pending login back up"
    assert a["connectShown"] and a["footShown"] and not a["detailsOpen"]
    assert not a["clientShown"] and not a["deviceShown"] and a["intervals0"] == 0
    assert a["startPost"] == [{}]
    assert a["code"] == "WDJB-MJHT" and a["openShown"] and a["href"] == "https://github.com/login/device"
    assert a["focus"] == "github-device-copy"
    assert a["time"] == "15:00" and a["intervals1"] == 2
    assert a["cancelShown"] and a["connectHidden"] and a["wait"]
    assert a["polls9"] == 3, "every 3 s while pending"
    assert a["time9"] == a["time9want"] == "14:51"
    assert a["copies"] == ["WDJB-MJHT"] and a["sayCopy"] == "Copied"
    assert a["opens"] == [["https://github.com/login/device", "_blank", "noopener,noreferrer"]]
    assert a["prevented"] is True
    assert a["closedIntervals"] == 0 and a["focusClosed"] == "preview-github"
    assert a["pollsClosed"] == 0, "no polling with the dialog closed"
    assert a["reopenIntervals"] == 2 and a["reopenCode"] == "WDJB-MJHT"
    assert a["reopenTime"] == a["reopenWant"]
    after = a["after"]
    assert after["connectBox"] and after["create"] and after["account"] and after["intervals"] == 0
    assert after["say"] == "GitHub connected as octo."
    assert a["pollsAfter"] == 0
    assert a["unexpected"] == []


def test_only_githubs_own_device_page_becomes_a_link(run):
    by = {r["uri"]: r for r in run["B"]}
    for uri, r in by.items():
        assert r["code"] == "AAAA-BBBB", "the code is shown whatever the link"
        if uri == "https://github.com/login/device":
            assert r["shown"] and r["href"] == uri and r["opens"] == 1
        else:
            assert not r["shown"] and r["href"] == "#" and r["opens"] == 0 and r["prevented"], uri


def test_installing_gh_shows_progress_keeps_polling_and_cancel_works(run):
    c = run["C"]
    assert c["installing"] and c["cancel"] and c["connectHidden"]
    assert c["progress"] == "Downloading… 42%"
    assert c["intervals"] == 1, "a poll, no countdown while installing"
    assert c["focus"] == "github-device-cancel"
    assert c["say"] == "Installing GitHub's official tool. This happens once."
    assert c["polls"] == 1 and c["progress2"] == "Unpacking gh"
    assert re.match(r"^Working… \d+:\d\d so far$", c["progress3"])
    assert c["code"] == "ABCD-1234" and c["intervals2"] == 2 and c["installingHidden"]
    assert c["cancelPosts"] == 1
    assert c["after"] == {"connect": True, "device": False, "intervals": 0, "focus": "github-connect-btn"}
    assert c["pollsAfter"] == 0


def test_expired_denied_and_error_say_so_and_offer_try_again(run):
    d = run["D"]
    ex = d["expired"]
    assert ex["msg"] == "The code expired before it was approved on GitHub."
    assert ex["retry"] and ex["intervals"] == 0 and not ex["pending"] and not ex["cancel"]
    assert ex["focus"] == "github-device-retry"
    assert d["retryStarts"] == 2 and d["retryCode"] == "EXPI-RING"
    assert d["denied"] == {"msg": "The sign-in was declined on GitHub.", "retry": True, "intervals": 0}
    assert d["error"] == {"msg": "GitHub said <b>no</b>", "retry": True, "intervals": 0}
    assert d["errorPlain"]["msg"] == "Something went wrong while connecting to GitHub."


def test_the_client_id_box_appears_only_after_no_client_id(run):
    x = run["E"]
    assert x["clientBefore"] is False
    assert x["footErr"].startswith("One-click connect needs a GitHub app Client ID.")
    assert x["clientShown"] and x["detailsOpen"] and x["focus"] == "github-client-id"
    assert x["saveDisabled"] is True
    assert x["post"] == [{"client_id": "Ov23liABC"}]
    assert x["clientAfter"] is False and x["focusAfter"] == "github-connect-btn" and x["clientValue"] == ""
    assert x["footErrServer"] == "Set a GitHub app Client ID first."
    assert x["badText"] == "Client IDs look like Iv1.<hex> or Ov23li<...>" and x["badShown"] and x["stillShown"]


def test_without_one_click_connect_the_token_form_opens_and_works(run):
    f = run["F"]
    assert f["connectBtn"] is False and f["foot"] is False
    assert f["detailsOpen"] is True and f["client"] is False
    assert f["saveDisabled"] is True
    assert f["post"] == [{"token": "ghp_secret"}]
    assert f["create"] is True and f["tokenCleared"] == ""


def test_a_new_repository_is_private_unless_the_public_tick_is_given(run):
    g = run["G"]
    assert g["name"] == "myproj" and g["create"] and not g["link"]
    assert g["newPanel"] and not g["existingPanel"] and not g["publicRow0"]
    assert g["publicRow"] and g["disabled"] and g["why"] == "Tick the box to make it public."
    assert g["postsWithoutTick"] == 0
    assert g["enabled"]
    assert g["body"] == {"project_dir": "/x/myproj", "name": "myproj", "private": False,
                         "confirm": True, "description": "A demo", "public_ok": True}
    assert g["linked"]


def test_an_existing_repository_is_listed_filtered_and_linked_with_the_overwrite_tick(run):
    h = run["H"]
    assert h["reposBefore"] == 0 and h["reposGets"] == 1, "loaded when chosen, not before"
    assert h["rows"] == 12 and h["filter"] is True
    assert h["linkShown"] and h["createHidden"] and h["linkDisabled"] and h["why"] == "Pick a repository."
    assert h["siteParts"] == [["gh-repo-name", "alpha-site"], ["gh-chip is-public", "Public"],
                              ["gh-repo-desc", "<img src=x onerror=alert(1)>"]]
    assert h["emptyParts"] == [["gh-repo-name", "alpha-empty"], ["gh-chip is-private", "Private"],
                               ["gh-chip is-empty", "empty"]]
    assert h["filtered"] == ["alpha-site", "alpha-empty"]
    assert h["ow"] and h["owDisabled"] and h["owWhy"] == "Tick the box to replace what is there."
    assert h["postsWithoutTick"] == 0
    assert h["owEmpty"] is False and h["emptyEnabled"] is True
    assert h["owTickReset"] is False
    assert h["enabled"]
    assert h["body"] == {"project_dir": "/x/myproj", "name": "alpha-site", "confirm": True, "overwrite_ok": True}
    assert h["linked"], "an answer without the status is followed by a status read"
    assert h["smallFilter"] is False, "no filter box for ten or fewer"
    assert h["emptyOw"] is False
    assert h["firstBody"] == {"project_dir": "/x/myproj", "name": "alpha-empty", "confirm": True}
    n = h["needs"]
    assert n["ow"] and n["focus"] == "github-overwrite-ok" and n["disabled"]
    assert n["err"].startswith("This repository already has files.")
    assert h["backToNew"] == {"create": True, "link": False, "panel": True}
    assert h["empty"] == {"status": "No repositories on this account yet. Choose New repository instead.",
                          "list": False, "retry": False}
    assert h["error"] == {"status": "GitHub is down", "retry": True}
    assert h["retried"] == {"gets": 2, "rows": 2, "retry": False}


def test_visibility_and_unlink_ask_first_and_esc_backs_out(run):
    i = run["I"]
    assert i["chip"] == "Private" and i["pub"] and not i["priv"]
    assert i["repoHref"] == "https://github.com/octo/myproj"
    assert i["ask"] == {"confirm": True, "focus": "github-make-public-ok", "yesDisabled": True, "pubHidden": True}
    assert i["esc"] == {"confirm": False, "open": True, "focus": "github-make-public"}
    assert i["postsWithoutTick"] == 0
    assert i["pubBody"] == {"project_dir": "/x/myproj", "private": False, "public_ok": True}
    ap = i["afterPub"]
    assert ap["chip"] == "Public" and ap["priv"] and not ap["confirm"] and ap["isPublic"]
    assert ap["focus"] == "github-make-private" and ap["say"] == "The repository is public now."
    assert i["privBody"] == {"project_dir": "/x/myproj", "private": True}
    assert i["afterPriv"] == {"chip": "Private", "pub": True}
    ua = i["unlinkAsk"]
    assert ua["confirm"] and not ua["footLinked"] and ua["focus"] == "github-unlink-no"
    assert ua["text"] == "Forget the link to octo/myproj? Nothing is deleted on GitHub or in your project."
    assert i["unlinkEsc"] == {"confirm": False, "open": True, "focus": "github-unlink", "footLinked": True}
    assert i["unlinkNo"] == {"confirm": False, "focus": "github-unlink"}
    assert i["postsBeforeYes"] == 0
    assert i["unlinkBody"] == [{"project_dir": "/x/myproj", "confirm": True}]
    assert i["afterUnlink"] == {"create": True, "linked": False, "confirm": False, "name": "myproj"}


def test_errors_show_the_server_sentence_then_plain_words(run):
    j = run["J"]
    assert j["server"] == "GitHub is ahead, press Sync"
    assert j["plain"] == "GitHub has changes this copy does not. Press Sync first, then push again."
    assert j["shown"]
    assert j["vis"]["err"] == "Tick the box to confirm anyone on the internet can see this code."
    assert j["vis"]["confirm"] is True and j["vis"]["tick"] is False


def test_busy_disables_the_clicked_button_and_says_what_it_does(run):
    k = run["K"]
    assert k["push"] == ["Pushing…", True]
    assert k["sync"] and k["unlink"] and k["pub"] and k["disconnect"]
    assert k["after"] == ["Push", False]


def test_leaving_the_pane_or_the_project_stops_the_timers(run):
    l = run["L"]
    assert l["before"] == 2
    assert l["pane"] == {"open": False, "intervals": 0}
    assert l["detached"] == 0 and l["open"] is False
