r"""The Build page's Publish button opens "Publish online": a native modal dialog
that puts the running preview on the internet through a free Cloudflare quick
tunnel, with a countdown until the link closes.

REQUESTED 2026-10-08 (the button and its countdown) and 2026-10-10: "the
Cloudflare thing should open in a POPUP, responsive, clean CSS -- it looks tight
in that place". The backend is a separate piece; this file pins the page's half
of the contract (five routes, ten error codes) and the things that are easy to
get wrong in a UI like this:

  * it is a real <dialog> opened with showModal(): the top layer is never
    clipped by the preview column, Esc and the focus trap are the browser's
    own, a press on the backdrop closes it and focus goes back to the button;
  * it is moved under <body> once, so no hidden ancestor can leave an invisible
    modal over an inert page; while it is open it speaks through its own live
    region (everything behind a modal is inert, live regions included);
  * the warning and the tick box that gates "Publish" are always on screen;
  * the countdown is computed from the server's remaining seconds and a LOCAL
    monotonic clock (never Date.now() against the server's expires_at), is exact
    to the second, keeps ticking on the button with the dialog closed, and is
    not announced every second -- only at 5:00 and 1:00 left;
  * polling runs only while something is settling, and every timer is cleared
    when the project changes;
  * every backend string goes in as text, and a link is only ever https://;
  * a centred window on a desktop, a full-width bottom sheet on a phone, with
    44 px targets; screen-relative sizes are percentages because the page root
    is zoomed (viewport units would shrink with it);
  * the colours are theme tokens and every text pair measures >= 4.5:1 in
    both themes.

The page is one big template, so (like the other UI tests) most checks read it
as text. The pure helpers and the whole state machine are ALSO run under node
against a tiny fake DOM, because "the ids exist" proves nothing about a timer;
the fake DOM's parent links are held to the real markup below.
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


JS = _between("/* publish-js:start */", "/* publish-js:end */")
PURE = _between("/* publish-pure:start */", "/* publish-pure:end */")
CSS = _between("/* publish-css:start */", "/* publish-css:end */")
DIALOG = _between('<dialog class="publish-dialog"', "</dialog>")
BAR = _between('<div class="preview-bar" id="preview-bar">', 'id="preview-state"')

CONTRACT_ROUTES = {
    "/api/publish", "/api/publish/start", "/api/publish/stop",
    "/api/publish/renew", "/api/publish/install",
}
ERROR_CODES = ["no_cloudflared", "no_preview", "forbidden_port", "not_http", "too_many",
               "bad_ttl", "install_failed", "not_found", "already_published", "disabled"]

# Where the controls the script hides sit, for the fake DOM's closest('[hidden]')
# (the focus rule). test_the_fake_dom_parents_match_the_markup holds it to the
# real template, so the fake cannot drift from the page.
PARENT = {
    "publish-start": "publish-foot-form",
    "publish-stop": "publish-foot-live",
    "publish-renew": "publish-foot-live",
    "publish-copy": "publish-foot-live",
    "publish-renew-yes": "publish-renew-confirm",
    "publish-renew-no": "publish-renew-confirm",
    "publish-expired-renew": "publish-foot-expired",
    "publish-failed-retry": "publish-foot-failed",
    "publish-ttl": "publish-form",
    "publish-consent": "publish-form",
    "publish-install-btn": "publish-install",
    "publish-url": "publish-v-live",
    "publish-v-live": "publish-live",
}


def _node():
    return shutil.which("node")


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


# --------------------------------------------------------------------------- #
# The button, the dialog, and how it opens and closes
# --------------------------------------------------------------------------- #

def test_the_button_sits_in_the_preview_bar_next_to_run_and_stop():
    assert BAR.index('id="preview-run"') < BAR.index('id="preview-stop"') < BAR.index('id="preview-publish"')
    btn = _between('<button class="btn sm ghost preview-publish"', "</button>", BAR)
    for needle in ('type="button"', 'aria-disabled="true"', 'aria-haspopup="dialog"',
                   'aria-controls="publish-dialog"', 'aria-describedby="preview-publish-why"',
                   'title="Start the preview first"'):
        assert needle in btn, needle
    assert "aria-expanded" not in btn, "a modal dialog makes the button inert while open"
    assert 'id="preview-publish-why">Start the preview first<' in BAR


def test_the_live_badge_lives_on_the_button_and_does_not_rename_it_every_second():
    assert 'id="preview-publish-label"' in BAR and 'id="preview-publish-time"' in BAR
    time_el = _between('<span id="preview-publish-time"', ">", BAR)
    assert "aria-hidden" in time_el and "hidden" in time_el


def test_two_polite_status_regions_one_outside_one_inside_the_dialog():
    outside = _between('id="publish-announce"', ">")
    assert 'role="status"' in outside and 'aria-live="polite"' in outside
    assert 'id="publish-announce"' not in DIALOG, "it must speak with the dialog closed"
    # ...and outside #preview-bar, which the Files tab hides.
    between = HTML[HTML.index('id="preview-reload"'):HTML.index('id="publish-announce"')]
    assert "</div>" in between, "the status region must sit after the bar closes"
    inside = _between('id="publish-say"', ">", DIALOG)
    assert 'role="status"' in inside and 'aria-live="polite"' in inside
    assert "publish-say" not in _between('<div class="pub-dlg-body"', '<div class="pub-dlg-foot"', DIALOG), \
        "never inside a section the script hides"
    assert "var el = isOpen ? sayIn : sayOut;" in JS


def test_the_dialog_ids_exist():
    for i in ("publish-title", "publish-close", "publish-warning", "publish-warning-text",
              "publish-body", "publish-install", "publish-install-btn", "publish-install-status",
              "publish-install-cmd", "publish-install-copy", "publish-auto", "publish-form",
              "publish-ttl", "publish-consent", "publish-live", "publish-v-starting",
              "publish-v-live", "publish-v-expired", "publish-v-failed", "publish-url",
              "publish-countdown", "publish-soon", "publish-failed-msg", "publish-foot",
              "publish-error", "publish-foot-form", "publish-start", "publish-start-why",
              "publish-foot-live", "publish-stop", "publish-renew", "publish-copy",
              "publish-renew-confirm", "publish-renew-yes", "publish-renew-no",
              "publish-foot-expired", "publish-expired-renew", "publish-foot-failed",
              "publish-failed-retry", "publish-say"):
        assert 'id="%s"' % i in DIALOG, i


def test_it_is_a_native_modal_dialog_named_and_described():
    head = _between('<dialog class="publish-dialog"', ">")
    assert 'id="publish-dialog"' in head
    assert 'aria-labelledby="publish-title"' in head and 'aria-describedby="publish-warning-text"' in head
    assert "hidden" not in head and "role=" not in head, "the element is the dialog; closed = not [open]"
    title = _between('<h2 class="publish-title"', "</h2>", DIALOG)
    assert 'id="publish-title"' in title and 'tabindex="-1"' in title and ">Publish online" in title
    x = _between('<button class="btn ghost publish-x" id="publish-close"', "</button>", DIALOG)
    assert 'aria-label="Close"' in x and "<svg" in x and 'aria-hidden="true"' in x
    # header, a body that scrolls, a footer -- as divs: the page styles <header>/<footer>
    assert DIALOG.index('class="pub-dlg-head"') < DIALOG.index('class="pub-dlg-body"') \
        < DIALOG.index('class="pub-dlg-foot"')
    assert "<header" not in DIALOG and "<footer" not in DIALOG
    # opened modally, and moved where no hidden ancestor can swallow it
    assert "dlg.showModal()" in JS and "dlg.close()" in JS
    assert "if (dlg.parentNode !== document.body) document.body.appendChild(dlg);" in JS
    # the old in-flow panel is gone, markup, style and script
    assert "publish-panel" not in HTML and 'aria-modal="false"' not in DIALOG


def test_esc_backdrop_and_the_x_close_it_and_focus_goes_back():
    assert "closeBtn.addEventListener('click', function(){ setOpen(false, true); });" in JS
    assert "dlg.addEventListener('cancel'" in JS and "e.preventDefault(); keepLink();" in JS
    assert "dlg.addEventListener('close'" in JS and "btn.focus()" in JS
    assert "downOnBackdrop = e.target === dlg" in JS
    assert "var outside = e.target === dlg && downOnBackdrop;" in JS
    assert "titleEl.focus()" in JS                         # where focus lands on open


def test_the_warning_is_always_visible_and_says_the_three_things():
    warn = _between('id="publish-warning"', "</div>", DIALOG)
    assert ("Anyone with the link can open this app. Don't publish apps that show private data. "
            "The link closes by itself when the timer reaches 0:00.") in warn
    assert "hidden" not in warn.split(">")[0], "the warning must not be toggled away"
    assert 'id="publish-warning-text"' in warn


def test_the_tick_box_gates_the_publish_button():
    assert "I understand anyone with the link can open this app" in DIALOG
    box = _between('<input type="checkbox" id="publish-consent"', ">", DIALOG)
    assert "required" in box
    assert '<label class="publish-consent" for="publish-consent">' in DIALOG
    start = _between('<button class="btn primary" id="publish-start"', ">", DIALOG)
    assert " disabled" in start, "closed until the box is ticked"
    # ...and the script keeps it closed for the same reason.
    assert "!consent.checked ? 'Tick the box above to continue.'" in JS
    assert "startBtn.disabled = !!reason || busy" in JS
    assert "if (startBtn.disabled || !dir) return;" in JS
    assert "consent.addEventListener('change', render)" in JS
    # a fresh yes for every publication
    assert "consent.checked = false;" in JS


def test_install_offer_is_an_explicit_click_that_says_what_it_does():
    assert "downloads the official release from Cloudflare and verifies its checksum" in DIALOG
    assert 'id="publish-install-btn" type="button">Install cloudflared<' in DIALOG
    assert "installBtn.addEventListener('click', doInstall)" in JS       # never automatic
    assert "winget install --id Cloudflare.cloudflared" in JS and "brew install cloudflared" in JS
    assert "Install failed: " in JS and "cf.install_error" in JS and "cf.installing" in JS


def test_the_ttl_select_and_labelled_choices():
    sel = _between('<label for="publish-ttl">', "</select>", DIALOG)
    assert "Keep the link open for" in sel
    assert "ttlChoices()" in JS and "default_ttl_minutes" in JS and "ttl_choices" in JS


def test_live_view_and_footer_rows():
    live = _between('id="publish-v-live"', 'id="publish-v-expired"', DIALOG)
    anchor = _between('<a class="publish-url" id="publish-url"', ">", live)
    assert 'target="_blank"' in anchor and 'rel="noopener noreferrer"' in anchor
    timer = _between('<span class="publish-countdown"', ">", live)
    assert 'role="timer"' in timer and 'aria-live="off"' in timer and 'aria-labelledby=' in timer
    assert "Closing soon" in live
    # the footer: the primary action is the last (right-most) button of its row
    row = _between('id="publish-foot-live"', "</div>", DIALOG)
    assert row.index(">Stop publishing<") < row.index(">New link<") < row.index(">Copy link<")
    assert '<button class="btn primary" id="publish-copy"' in row and "publish-lead" in row
    form = _between('id="publish-foot-form"', "</div>", DIALOG)
    assert form.index('id="publish-start-why"') < form.index('id="publish-start"')
    ask = _between('id="publish-renew-confirm"', 'id="publish-foot-expired"', DIALOG)
    assert "Make a new link? The old link stops working right away." in ask
    assert ask.index(">Keep this link<") < ask.index(">Make new link<")
    assert "Link expired" in DIALOG and ">Generate new link<" in DIALOG
    assert ">Try again<" in DIALOG
    # Stop publishing also cancels a tunnel that is still starting
    assert "setHidden(fLive, !(live || v === 'starting') || !confirmBox.hidden);" in JS


def test_the_countdown_words_are_not_colour_alone():
    # "closing soon" is text in the dialog AND on the button label.
    assert "'Closing soon'" in JS and "setHidden(soonTag, !soon)" in JS
    assert "countdown.classList.toggle('is-soon', soon)" in JS


def test_the_fake_dom_parents_match_the_markup():
    up = _ancestors()
    for child, parent in PARENT.items():
        assert parent in up[child], (child, parent, up.get(child))
    # everything the script hides sits inside the dialog, under its body or footer
    for i in PARENT:
        assert "publish-dialog" in up[i], i
    assert up["publish-say"][0] == "publish-dialog"


# --------------------------------------------------------------------------- #
# Only the contract is called; strings go in as text
# --------------------------------------------------------------------------- #

def test_the_contract_routes_are_the_only_ones_called():
    called = set(re.findall(r"""['"](/api/[^'"?]*)""", JS))
    assert called == CONTRACT_ROUTES, called
    assert "fetch(" not in JS.replace("/* publish-js:start */", ""), \
        "go through the dashboard's api() helper: it adds the token and the dashboard header"
    assert "api('/api/publish?project_dir=' + encodeURIComponent(d))" in JS


def test_post_bodies_match_the_contract():
    assert "{ project_dir: dir, ttl_minutes: Number(ttlSel.value) || undefined, confirm: true }" in JS
    assert "body.port = previewPort" in JS
    assert "api('/api/publish/start', { method: 'POST', body: body })" in JS
    assert "api('/api/publish/stop', { method: 'POST', body: { id: tun.id } })" in JS
    assert "var body = { id: tun.id };" in JS and "body.ttl_minutes = m" in JS
    assert "api('/api/publish/renew', { method: 'POST', body: body })" in JS
    assert "api('/api/publish/install', { method: 'POST', body: { confirm: true } })" in JS


def test_every_contract_error_code_has_plain_words():
    for code in ERROR_CODES:
        assert re.search(r"\b%s: " % code, PURE), code
    assert "pubErrorText(code, err && err.message)" in JS


def test_backend_text_is_text_not_markup_and_links_are_https_only():
    assert "innerHTML" not in JS and "insertAdjacentHTML" not in JS
    assert "pubSafeUrl(tun.url)" in JS and r"/^https:\/\/[^\s<>" in PURE
    assert "urlA.setAttribute('href', u)" in JS


def test_the_link_is_never_written_to_browser_storage():
    assert not re.search(r"localStorage|sessionStorage|indexedDB|document\.cookie", JS)


def test_the_script_follows_the_project_and_the_pane():
    pv = _between("var preview = (function(){", "/* publish-js:start */")
    assert "publish.previewState(running, st.port)" in pv
    assert "publish.attach(d)" in pv and "publish.detach()" in pv
    assert "publish.paneVisible(!files)" in pv
    for call in ("previewState", "attach", "detach", "paneVisible"):
        assert call + ": function" in JS, call


def test_timers_stop_when_things_settle_or_the_page_goes():
    assert "function stopTimers()" in JS and JS.count("stopTimers()") >= 3
    assert "window.addEventListener('pagehide', stopTimers)" in JS
    assert "document.addEventListener('visibilitychange'" in JS      # a slept computer re-syncs
    assert "setInterval(function(){ if (!document.hidden) load(true); }, 1500)" in JS
    assert "performance.now()" in JS and "Date.now()" not in JS, \
        "the countdown must not trust this computer's wall clock"


# --------------------------------------------------------------------------- #
# CSS: tokens only, a window on a desktop, a sheet on a phone, reduced motion
# --------------------------------------------------------------------------- #

def _rule(selector, src=CSS):
    i = src.index(selector + "{")
    return src[i:src.index("}", i)]


def test_every_colour_in_the_publish_css_is_a_theme_token():
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", CSS), "hard-coded colour"
    for token in ("var(--surface)", "var(--scrim)", "var(--ok-text)", "var(--warn-text)",
                  "var(--danger-text)", "var(--info-text)"):
        assert token in CSS, token


def test_a_closed_dialog_stays_closed():
    """display:flex on the bare class would beat the browser's
    dialog:not([open]){display:none} and show a closed dialog."""
    assert "display:" not in _rule(".publish-dialog")
    assert ".publish-dialog[open]{ display:flex; flex-direction:column;" in CSS


def test_desktop_is_a_centred_window_sized_in_percentages():
    box = _rule(".publish-dialog")
    assert "width:min(560px, calc(100% - 2 * var(--pub-gutter)))" in box
    assert "max-height:85%" in box and "margin:auto" in box and "position:fixed" in box
    assert "--pub-tap:calc(44px / var(--ui-scale, .8))" in box          # 44 RENDERED px
    assert "--pub-gutter:calc(16px / var(--ui-scale, .8))" in box
    # vw / vh / dvh resolve unzoomed and then shrink with html{zoom:.8}
    assert not re.search(r"\d(?:[dsl])?v[hw]\b", CSS)
    body = _rule(".pub-dlg-body")
    assert "overflow-y:auto" in body and "min-height:0" in body and "overscroll-behavior:contain" in body
    assert "flex:none" in _rule(".pub-dlg-foot") and "flex:none" in _rule(".pub-dlg-head")
    assert "justify-content:flex-end" in _rule(".publish-row")


def test_a_phone_gets_a_full_width_bottom_sheet_with_44_px_targets():
    sheet = _between("@media (max-width:640px){", "@media (prefers-reduced-motion", CSS)
    assert ".publish-dialog{ width:100%; max-height:90%; margin:auto 0 0;" in sheet
    assert "border-radius:16px 16px 0 0" in sheet
    assert "env(safe-area-inset-bottom, 0px) / var(--ui-scale, .8)" in sheet
    assert ".publish-dialog select{ font-size:calc(16px / var(--ui-scale, .8)); }" in sheet  # no iOS zoom
    assert ".publish-row .btn{ flex:1 1 auto; }" in sheet
    assert ".publish-dialog .btn{ min-height:var(--pub-tap)" in CSS
    assert "width:var(--pub-tap)" in _rule(".publish-dialog .publish-x")
    assert "min-height:var(--pub-tap)" in _rule(".publish-consent")
    assert "min-height:var(--pub-tap)" in _rule(".publish-url")
    coarse = _between("@media (pointer:coarse), (max-width:640px){", "}", CSS)
    assert ".preview-publish{ min-height:calc(44px / var(--ui-scale, .8))" in coarse
    # long text wraps instead of pushing the sheet sideways
    for sel in (".publish-url", ".publish-cmd code", ".publish-err", ".publish-title"):
        assert "overflow-wrap:anywhere" in _rule(sel), sel
    assert "width:100%; min-width:0; max-width:100%" in _rule(".publish-dialog select")


def test_motion_is_short_and_off_on_request_and_the_focus_ring_stays():
    m = re.search(r"animation:pub-in \.(\d+)s ease-out", CSS)
    assert m and 150 <= int(m.group(1)) * 10 ** (3 - len(m.group(1))) <= 200
    assert "@keyframes pub-in{ from{ opacity:0; transform:translateY(" in CSS
    rm = CSS[CSS.index("@media (prefers-reduced-motion:reduce){"):]
    assert ".publish-dialog[open], .publish-dialog[open]::backdrop{ animation:none; }" in rm
    assert "transition:none" in rm
    assert ":focus-visible{outline:2px solid var(--accent)" in HTML      # global ring; nothing here removes it
    assert not re.search(r"outline\s*:\s*(none|0)", CSS)


# --------------------------------------------------------------------------- #
# Contrast, measured from the template's own tokens, both themes
# --------------------------------------------------------------------------- #

def _theme(name):
    def parse(block):
        return dict(re.findall(r"--([\w-]+):\s*([^;]+);", block))
    dark = parse(re.search(r"\n  :root\{(.*?)\n  \}", HTML, re.S).group(1))
    if name == "dark":
        return dark
    light = dict(dark)
    light.update(parse(re.search(r':root\[data-theme="light"\]\{(.*?)\n  \}', HTML, re.S).group(1)))
    return light


def _rgba(v):
    v = v.strip()
    if v.startswith("#"):
        return tuple(int(v[i:i + 2], 16) for i in (1, 3, 5)) + (1.0,)
    m = re.match(r"rgba\(([\d.]+),([\d.]+),([\d.]+),([\d.]+)\)", v.replace(" ", ""))
    return tuple(float(x) for x in m.groups()[:3]) + (float(m.group(4)),)


def _over(fg, bg):
    return tuple(fg[i] * fg[3] + bg[i] * (1 - fg[3]) for i in range(3)) + (1.0,)


def _ratio(a, b):
    def lum(c):
        def ch(x):
            x /= 255
            return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4
        return 0.2126 * ch(c[0]) + 0.7152 * ch(c[1]) + 0.0722 * ch(c[2])
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_the_primary_buttons_use_the_green_that_carries_their_text():
    """The dashboard's own light primary (white on --accent-dim) is 3.3:1; the
    dialog's primary buttons take the darker green in the light theme."""
    assert ".btn.primary{background:var(--accent-dim);border-color:var(--accent-dim);color:var(--on-accent)}" in HTML
    assert ':root[data-theme="light"] .publish-dialog .btn.primary{ background:var(--accent-strong);' in CSS
    assert ':root[data-theme="light"] .publish-dialog .btn.primary:hover{ background:var(--ok-text);' in CSS


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_every_publish_text_pair_meets_aa(theme):
    t = _theme(theme)
    T = lambda k: _rgba(t[k])                                   # noqa: E731
    surf, s2, s3, inset = T("surface"), T("surface-2"), T("surface-3"), T("bg-inset")
    primary = T("accent-strong") if theme == "light" else T("accent-dim")
    primary_hover = T("ok-text") if theme == "light" else T("accent")
    pairs = {
        "dialog text": (T("text"), surf),
        "dialog hint text": (T("text-dim"), surf),
        "install card text": (T("text-dim"), s2),
        "install card link": (T("info-text"), s2),
        "warning text": (T("text"), _over(T("warn-soft"), surf)),
        "warning label": (T("warn-text"), _over(T("warn-soft"), surf)),
        "countdown": (T("text"), surf),
        "countdown, last minutes": (T("warn-text"), surf),
        "closing soon tag": (T("warn-text"), _over(T("warn-soft"), surf)),
        "public link": (T("info-text"), inset),
        "command": (T("code-text"), inset),
        "error text": (T("danger-text"), _over(T("danger-soft"), surf)),
        "stop publishing": (T("danger-text"), surf),
        "plain button": (T("text"), s2),
        "close button, hover": (T("text"), s3),
        "select": (T("text"), s2),
        "primary button": (T("on-accent"), primary),
        "primary button, hover": (T("on-accent"), primary_hover),
        "button badge, live": (T("ok-text"), _over(T("accent-soft"), surf)),
        "button badge, closing soon": (T("warn-text"), _over(T("warn-soft"), surf)),
    }
    low = {k: round(_ratio(*v), 2) for k, v in pairs.items() if _ratio(*v) < 4.5}
    assert not low, "%s theme under 4.5:1: %s" % (theme, low)


# --------------------------------------------------------------------------- #
# The pure helpers, run for real
# --------------------------------------------------------------------------- #


def _run_node(script_body, prelude):
    d = tempfile.mkdtemp()
    path = os.path.join(d, "t.js")
    io.open(path, "w", encoding="utf-8").write(prelude + "\n" + script_body)
    out = subprocess.run([_node(), path], capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert out.returncode == 0, (out.stdout + out.stderr)[-1500:]
    return json.loads(out.stdout)


PURE_DRIVER = r"""
const out = {};
out.clock = [0, 5, 59.2, 60, 299.01, 2530, 3599, 3600, 3661, -3, NaN].map(pubFmtClock);
const T = 1700000000;
out.remaining = [
  pubRemainingAt({remaining_seconds: 100}, 5),
  pubRemainingAt({expires_at: T + 300}, T),
  pubRemainingAt({expires_at: '2026-10-08T12:05:00Z'}, '2026-10-08T12:00:00Z'),
  pubRemainingAt({expires_at: (T + 300) * 1000}, T * 1000),
  pubRemainingAt({started_at: 1000, ttl_seconds: 600}, 1100),
  pubRemainingAt({expires_at: T - 5}, T),
  pubRemainingAt({}, T),
  pubRemainingAt(null, T),
];
out.announce = [];
let f = {};
for (const s of [400, 299, 298, 61, 59, 30, 0]) {
  const a = pubAnnounce(s, f); f = a.fired; out.announce.push([s, a.msg]);
}
out.lateOpen = pubAnnounce(40, {});
out.soon = [301, 300, 1, 0, NaN].map(pubIsSoon);
out.ttl = [[15, false], [60, true], [240, false], [720, false], [1440, false], [90, false], [1, false]].map(a => pubTtlLabel(a[0], a[1]));
out.url = ['https://quiet-fox.trycloudflare.com', 'http://x.example', 'javascript:alert(1)', 'https://x y', 'https://a"onload=1', null, ''].map(pubSafeUrl);
out.install = ['darwin', 'Darwin', 'macOS', 'win32', 'Windows', 'linux', '', undefined].map(pubInstallCommand);
const mk = (id, state, started) => ({id: id, state: state, started_at: started, url: 'https://' + id + '.trycloudflare.com'});
const list = [mk('a', 'expired', 1), mk('b', 'live', 2), mk('c', 'failed', 3), mk('d', 'stopped', 9)];
out.pick = [
  pubPickTunnel(list).id,
  pubPickTunnel(list, {[pubKey(list[1])]: true}).id,
  pubPickTunnel([mk('d', 'stopped', 9)]),
  pubPickTunnel([mk('x', 'starting', 1), mk('y', 'starting', 5)]).id,
  pubPickTunnel(undefined),
];
out.errors = Object.keys(PUB_ERRORS);
out.errText = [pubErrorText('too_many'), pubErrorText('nope', 'Boom'), pubErrorText(undefined, ''), pubErrorText('__proto__', 'x')];
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def pure():
    if not _node():
        pytest.skip("node is not installed")
    return _run_node(PURE_DRIVER, PURE)


def test_clock_format_is_m_ss_then_h_mm_ss(pure):
    assert pure["clock"] == ["0:00", "0:05", "1:00", "1:00", "5:00", "42:10", "59:59",
                             "1:00:00", "1:01:01", "0:00", "0:00"]


def test_remaining_time_uses_only_server_clock_numbers(pure):
    assert pure["remaining"] == [100, 300, 300, 300, 500, 0, None, None]


def test_the_two_spoken_warnings_fire_once_each(pure):
    assert pure["announce"] == [
        [400, ""], [299, "Your public link closes in less than 5 minutes."], [298, ""],
        [61, ""], [59, "Your public link closes in less than 1 minute."], [30, ""], [0, ""]]
    # opened at 0:40: the one-minute line, and no stale five-minute one afterwards
    assert pure["lateOpen"]["msg"].endswith("less than 1 minute.")
    assert pure["lateOpen"]["fired"] == {"m5": True, "m1": True}
    assert pure["soon"] == [False, True, True, False, False]


def test_labels_urls_commands_and_picks(pure):
    assert pure["ttl"] == ["15 minutes", "1 hour (default)", "4 hours", "12 hours", "24 hours",
                           "1 h 30 min", "1 minute"]
    assert pure["url"] == ["https://quiet-fox.trycloudflare.com", "", "", "", "", "", ""]
    inst = pure["install"]
    assert inst[0] == inst[1] == inst[2] == "brew install cloudflared"      # "darwin" holds "win"
    assert inst[3] == inst[4] == "winget install --id Cloudflare.cloudflared"
    assert "dpkg" in inst[5] and inst[6] == "" and inst[7] == ""
    assert pure["pick"] == ["b", "a", None, "y", None]
    assert sorted(pure["errors"]) == sorted(ERROR_CODES)
    assert pure["errText"][0].startswith("Too many")
    assert pure["errText"][1] == "Boom" and pure["errText"][2].startswith("Something went wrong")
    assert pure["errText"][3] == "x"                  # a prototype key is not an error code


# --------------------------------------------------------------------------- #
# The state machine, run against a fake DOM, fake timers and a fake api()
# --------------------------------------------------------------------------- #

HARNESS = r"""
const fs = require('fs');
const code = fs.readFileSync(process.argv[2], 'utf8');
const PARENT = JSON.parse(process.argv[3]);
const flush = () => new Promise(r => setImmediate(r));

class El {
  constructor(id, env){
    this.id = id; this.env = env; this.hidden = false; this.textContent = ''; this.title = '';
    this.value = ''; this.checked = false; this.disabled = false; this.attrs = {}; this._cls = new Set();
    this.l = {}; this.children = [];
    const self = this;
    this.classList = {
      toggle(n, on){ if (on === undefined) on = !self._cls.has(n); if (on) self._cls.add(n); else self._cls.delete(n); },
      add(n){ self._cls.add(n); }, remove(n){ self._cls.delete(n); }, contains(n){ return self._cls.has(n); },
    };
  }
  setAttribute(k, v){ this.attrs[k] = String(v); }
  getAttribute(k){ return k in this.attrs ? this.attrs[k] : null; }
  addEventListener(t, fn){ (this.l[t] = this.l[t] || []).push(fn); }
  fire(t, e){ (this.l[t] || []).forEach(f => f(e || {})); }
  click(){ this.fire('click', {target: this}); }
  focus(){ this.env.focused = this.id; }
  appendChild(c){ this.children.push(c); c.parentNode = this; return c; }
  removeChild(c){ this.children = this.children.filter(x => x !== c); }
  get firstChild(){ return this.children[0] || null; }
  has(c){ return this._cls.has(c); }
  // <dialog>: like a browser, the close event comes a task later.
  showModal(){ if (this.open) throw new Error('InvalidStateError'); this.open = true; this.env.modals++; }
  close(){ if (!this.open) return; this.open = false; setImmediate(() => this.fire('close', {})); }
  // only the dialog holds anything; its controls are the publish-* ids (the outside region excepted)
  contains(x){ return this.id === 'publish-dialog' && !!x && x.id.indexOf('publish-') === 0 && x.id !== 'publish-announce'; }
  closest(sel){
    if (sel !== '[hidden]') throw new Error('unsupported selector ' + sel);
    for (let n = this; n; n = PARENT[n.id] ? this.env.el(PARENT[n.id]) : null) if (n.hidden) return n;
    return null;
  }
}

function makeEnv(){
  let now = 0, nextId = 1, timers = [];
  const env = { calls: [], toasts: [], copies: [], els: {}, focused: null, docL: {}, winL: {}, handler: null,
                modals: 0 };
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
      await flush();
    }
    now = end;
    await flush();
  };
  env.el = id => env.els[id] || (env.els[id] = new El(id, env));
  const $ = sel => env.el(sel.replace(/^#/, ''));
  const body = new El('body', env);
  const doc = { hidden: false, body: body, createElement: () => new El('opt', env),
                addEventListener(t, fn){ env.docL[t] = fn; },
                get activeElement(){ return env.focused ? env.el(env.focused) : body; } };
  env.body = body;
  // Esc: the browser fires a cancelable "cancel" and closes unless it was prevented.
  env.esc = async function(){
    const d = env.el('publish-dialog'), ev = {defaultPrevented: false, preventDefault(){ this.defaultPrevented = true; }};
    d.fire('cancel', ev);
    if (!ev.defaultPrevented) d.close();
    await flush();
  };
  // A press: pointerdown on `down`, release (click) on `up` -- the click goes to their common ancestor.
  env.press = async function(down, up){
    const d = env.el('publish-dialog');
    d.fire('pointerdown', {target: env.el(down)});
    d.fire('click', {target: env.el(up)});
    await flush();
  };
  const win = { addEventListener(t, fn){ env.winL[t] = fn; } };
  const api = (path, opts) => {
    env.calls.push({path: path, method: opts && opts.method, body: opts && opts.body});
    try { return Promise.resolve(env.handler(path, opts)); } catch (e) { return Promise.reject(e); }
  };
  const toast = (m, err) => env.toasts.push(m);
  const copyText = (t, b) => { env.copies.push(t); b.textContent = 'Copied!'; };
  const fn = new Function('$', 'api', 'toast', 'copyText', 'document', 'window', 'performance',
                          'setInterval', 'clearInterval', 'setTimeout', code + '\nreturn publish;');
  env.publish = fn($, api, toast, copyText, doc, win, {now: () => now}, setI, clr, setT);
  env.gets = () => env.calls.filter(c => c.path.indexOf('/api/publish?') === 0).length;
  return env;
}
const err = (code, status) => Object.assign(new Error('server said no'), {body: {code: code}, status: status || 400});
const CF = {available: true, path: '/bin/cloudflared', version: '2026.1.0', platform: 'Windows',
            installable: true, installing: false, install_error: null};
const LIMITS = {default_ttl_minutes: 60, ttl_choices: [15, 60, 240, 720, 1440], max_tunnels: 3};
const status = (tunnels, cf) => ({server_time: 1000000, cloudflared: cf || CF, tunnels: tunnels || [], limits: LIMITS});
const tunnel = o => Object.assign({id: 't1', project_dir: '/p', port: 5801, url: 'https://quiet-fox.trycloudflare.com',
  state: 'live', error: null, source: 'build', started_at: 999000, expires_at: 1003600, ttl_seconds: 3600,
  remaining_seconds: 2530}, o || {});
const txt = (e, id) => e.el(id).textContent;

(async () => {
  const R = {};

  // A: reload with a live tunnel -> badge, exact countdown, spoken warnings, expiry, cleanup
  {
    const e = makeEnv();
    e.handler = () => {
      const left = Math.max(0, 2530 - e.now() / 1000);
      return status([tunnel({remaining_seconds: left, state: left > 0 ? 'live' : 'expired'})]);
    };
    e.publish.attach('/p'); await flush();
    const a = R.A = {};
    a.label = txt(e, 'preview-publish-label'); a.time = txt(e, 'preview-publish-time');
    a.timeHidden = e.el('preview-publish-time').hidden;
    a.aria = e.el('preview-publish').getAttribute('aria-disabled');
    a.countdown = txt(e, 'publish-countdown');
    a.href = e.el('publish-url').getAttribute('href');
    a.options = e.el('publish-ttl').children.map(o => o.value + ':' + o.textContent);
    a.ttlValue = e.el('publish-ttl').value;
    a.intervals = e.intervals();
    await e.advance(1000); a.t1 = txt(e, 'publish-countdown');
    await e.advance(2229000);                       // now 2230 s -> 300 s left
    await e.advance(100);
    a.soonLabel = txt(e, 'preview-publish-label'); a.soonBtn = e.el('preview-publish').has('is-soon');
    a.soonTagHidden = e.el('publish-soon').hidden; a.soonSay = txt(e, 'publish-announce');
    a.soonTime = txt(e, 'preview-publish-time');
    await e.advance(240000);                        // now 2470 s -> 60 s left
    await e.advance(100); a.lastSay = txt(e, 'publish-announce');
    await e.advance(61000);                         // past 0:00
    a.endLabel = txt(e, 'preview-publish-label'); a.expiredShown = !e.el('publish-v-expired').hidden;
    a.liveHidden = e.el('publish-v-live').hidden; a.endSay = txt(e, 'publish-announce');
    a.endIntervals = e.intervals();
    a.neverOpened = e.modals === 0 && !e.el('publish-dialog').open;
    a.inBody = e.el('publish-dialog').parentNode === e.body;
    e.publish.detach();
    a.afterDetach = {label: txt(e, 'preview-publish-label'), aria: e.el('preview-publish').getAttribute('aria-disabled'),
                     intervals: e.intervals(), dialogOpen: !!e.el('publish-dialog').open};
  }

  // B: publish flow -> consent gate, request body, starting poll, live, polling stops
  {
    const e = makeEnv();
    e.handler = (p, o) => {
      if (p.indexOf('/api/publish/start') === 0)
        return {ok: true, tunnel: tunnel({id: 't2', state: 'starting', url: null, remaining_seconds: 3600})};
      if (e.started && e.now() < 3000) return status([tunnel({id: 't2', state: 'starting', url: null})]);
      if (e.started) return status([tunnel({id: 't2', remaining_seconds: 3600 - e.now() / 1000})]);
      return status([]);
    };
    e.publish.attach('/p'); e.publish.previewState(true, 5801); await flush();
    const b = R.B = {};
    b.aria = e.el('preview-publish').getAttribute('aria-disabled');
    b.formShown = !e.el('publish-form').hidden;
    e.el('preview-publish').click(); await flush();
    b.dialogOpen = !!e.el('publish-dialog').open; b.modals = e.modals;
    b.focus = e.focused;
    b.disabled0 = e.el('publish-start').disabled; b.why0 = txt(e, 'publish-start-why');
    b.footForm = !e.el('publish-foot-form').hidden && e.el('publish-foot-live').hidden;
    e.el('publish-consent').checked = true; e.el('publish-consent').fire('change');
    b.disabled1 = e.el('publish-start').disabled;
    e.started = true;
    e.el('publish-start').focus();                   // a keyboard user presses it
    e.el('publish-start').click(); await flush();
    const post = e.calls.filter(c => c.method === 'POST')[0];
    b.post = post; b.consentAfter = e.el('publish-consent').checked;
    b.startingShown = !e.el('publish-v-starting').hidden; b.btnLabel = txt(e, 'preview-publish-label');
    b.focusAfterStart = e.focused;                   // its row went away: focus stays in the dialog
    b.startingFoot = {form: e.el('publish-foot-form').hidden, live: !e.el('publish-foot-live').hidden,
                      stop: !e.el('publish-stop').hidden, copy: e.el('publish-copy').hidden,
                      renew: e.el('publish-renew').hidden};
    b.pollOn = e.intervals();
    await e.advance(1500); b.stillStarting = !e.el('publish-v-starting').hidden;
    await e.advance(1500); b.liveShown = !e.el('publish-v-live').hidden;
    b.countdown = txt(e, 'publish-countdown');
    await e.advance(100); b.say = txt(e, 'publish-say'); b.sayOutside = txt(e, 'publish-announce');
    b.liveFoot = !e.el('publish-copy').hidden && !e.el('publish-renew').hidden && !e.el('publish-foot-live').hidden;
    b.intervalsLive = e.intervals();
    const g0 = e.gets(); await e.advance(10000); b.extraGets = e.gets() - g0;
  }

  // C: no preview -> disabled, reason on hover/screen reader, a click explains instead of opening
  {
    const e = makeEnv();
    e.handler = () => status([]);
    e.publish.attach('/p'); await flush();
    e.publish.previewState(false, null);
    const c = R.C = {};
    c.aria = e.el('preview-publish').getAttribute('aria-disabled'); c.title = e.el('preview-publish').title;
    c.why = txt(e, 'preview-publish-why');
    e.el('preview-publish').click(); await flush();
    c.toasts = e.toasts; c.dialogOpen = !!e.el('publish-dialog').open;
    e.publish.previewState(true, 5801);
    c.ariaOn = e.el('preview-publish').getAttribute('aria-disabled');
  }

  // D: cloudflared missing -> install offer, manual command, explicit install, polls until done
  {
    const e = makeEnv();
    let phase = 0;
    e.handler = (p, o) => {
      if (p.indexOf('/api/publish/install') === 0){ phase = 1; return {ok: true}; }
      if (phase === 0) return status([], Object.assign({}, CF, {available: false}));
      if (e.now() < 3000) return status([], Object.assign({}, CF, {available: false, installing: true}));
      return status([]);
    };
    e.publish.attach('/p'); e.publish.previewState(true, 5801); await flush();
    e.el('preview-publish').click(); await flush();
    const d = R.D = {};
    d.installShown = !e.el('publish-install').hidden; d.cmd = txt(e, 'publish-install-cmd');
    d.startDisabled = e.el('publish-start').disabled; d.why = txt(e, 'publish-start-why');
    d.noAuto = e.calls.filter(c => c.method === 'POST').length;
    e.el('publish-install-btn').click(); await flush();
    d.body = e.calls.filter(c => c.method === 'POST')[0].body;
    d.btnText = txt(e, 'publish-install-btn'); d.btnDisabled = e.el('publish-install-btn').disabled;
    d.status = txt(e, 'publish-install-status'); d.polling = e.intervals();
    await e.advance(3000);
    d.doneShown = e.el('publish-install').hidden; d.pollAfter = e.intervals();
    d.startWhyAfter = txt(e, 'publish-start-why');
  }

  // E: errors in plain words
  {
    const e = makeEnv();
    e.handler = (p, o) => { if (p.indexOf('/start') > 0) return Promise.reject(err(e.code)); return status([]); };
    e.publish.attach('/p'); e.publish.previewState(true, 5801); await flush();
    e.el('preview-publish').click(); await flush();
    e.el('publish-consent').checked = true; e.el('publish-consent').fire('change');
    const x = R.E = {};
    e.code = 'too_many'; e.el('publish-start').click(); await flush();
    x.tooMany = txt(e, 'publish-error'); x.shown = !e.el('publish-error').hidden;
    e.code = 'forbidden_port'; e.el('publish-consent').checked = true; e.el('publish-consent').fire('change');
    e.el('publish-start').click(); await flush(); x.port = txt(e, 'publish-error');
    e.code = undefined; e.el('publish-consent').checked = true; e.el('publish-consent').fire('change');
    e.el('publish-start').click(); await flush(); x.plain = txt(e, 'publish-error');
  }

  // F: New link asks first; yes renews with the same length; the old link is gone at once
  {
    const e = makeEnv();
    e.handler = (p, o) => {
      if (p.indexOf('/api/publish/renew') === 0)
        return {ok: true, tunnel: tunnel({id: 't1', state: 'starting', url: null, remaining_seconds: 3600})};
      return status([tunnel()]);
    };
    e.publish.attach('/p'); await flush();
    e.el('preview-publish').click(); await flush();
    const f = R.F = {};
    e.el('publish-renew').click(); f.confirmShown = !e.el('publish-renew-confirm').hidden; f.focusYes = e.focused;
    f.liveRowWhileAsking = e.el('publish-foot-live').hidden;
    e.el('publish-renew-no').click(); f.confirmHidden = e.el('publish-renew-confirm').hidden; f.focusBack = e.focused;
    e.el('publish-renew').click(); await e.esc();          // Esc backs out of the question first
    f.escAnswer = {asking: !e.el('publish-renew-confirm').hidden, open: !!e.el('publish-dialog').open,
                   focus: e.focused, liveRow: !e.el('publish-foot-live').hidden};
    f.noCall = e.calls.filter(c => c.method === 'POST').length;
    e.el('publish-renew').click(); e.el('publish-renew-yes').focus(); e.el('publish-renew-yes').click(); await flush();
    f.post = e.calls.filter(c => c.method === 'POST')[0];
    f.startingShown = !e.el('publish-v-starting').hidden; f.liveHidden = e.el('publish-v-live').hidden;
    f.hrefStillOld = e.el('publish-url').getAttribute('href');
    f.focusAfter = e.focused;
    await e.advance(100); f.say = txt(e, 'publish-say');
  }

  // G: a late answer for the project we left changes nothing
  {
    const e = makeEnv();
    const pending = {};
    e.handler = p => new Promise(res => { pending[p] = res; });
    e.publish.attach('/a'); await flush();
    e.publish.attach('/b'); await flush();
    pending['/api/publish?project_dir=%2Fb'](status([]));
    await flush();
    pending['/api/publish?project_dir=%2Fa'](status([tunnel()]));
    await flush();
    R.G = {label: txt(e, 'preview-publish-label'), intervals: e.intervals(), liveHidden: e.el('publish-v-live').hidden};
  }

  // H: open, the Files tab, Esc, the X, the backdrop, and a drag out of the content
  {
    const e = makeEnv();
    e.handler = () => status([]);
    e.publish.attach('/p'); e.publish.previewState(true, 5801); await flush();
    const d = e.el('publish-dialog');
    e.el('preview-publish').click(); await flush();
    const h = R.H = {open: !!d.open, focus: e.focused, modals: e.modals};
    e.publish.paneVisible(false); await flush();
    h.filesClosed = !d.open; h.filesFocus = e.focused;
    e.publish.paneVisible(true); h.notReopened = !d.open;
    e.el('preview-publish').click(); await flush();
    await e.esc();
    h.escClosed = !d.open; h.escFocus = e.focused;
    e.el('preview-publish').click(); await flush();
    e.el('publish-close').click(); await flush();
    h.xClosed = !d.open; h.xFocus = e.focused;
    e.el('preview-publish').click(); await flush();
    await e.press('publish-url', 'publish-dialog');        // a drag from the link text out to the backdrop
    h.dragKept = !!d.open;
    await e.press('publish-dialog', 'publish-dialog');     // a press on the backdrop
    h.backdropClosed = !d.open; h.backdropFocus = e.focused;
    e.el('preview-publish').click(); await flush();
    h.reopened = !!d.open; h.reopenFocus = e.focused; h.modalsAfter = e.modals;
  }

  // I: a failed tunnel -> plain error, Try again returns to the form (and asks for the tick again)
  {
    const e = makeEnv();
    e.handler = () => status([tunnel({state: 'failed', url: null, error: '<b>boom</b>'})]);
    e.publish.attach('/p'); e.publish.previewState(true, 5801); await flush();
    e.el('preview-publish').click(); await flush();
    const i = R.I = {};
    i.failedShown = !e.el('publish-v-failed').hidden; i.msg = txt(e, 'publish-failed-msg');
    e.el('publish-failed-retry').click();
    i.formShown = !e.el('publish-form').hidden; i.failedHidden = e.el('publish-v-failed').hidden;
    i.startDisabled = e.el('publish-start').disabled; i.focus = e.focused;
    await e.advance(31000);      // a later status poll must not bring the dismissed failure back
  }

  // J: a hostile url never becomes a link; a missing backend turns the button off in plain words
  {
    const e = makeEnv();
    e.handler = () => status([tunnel({url: 'javascript:alert(1)'})]);
    e.publish.attach('/p'); await flush();
    R.J = {href: e.el('publish-url').getAttribute('href'), starting: !e.el('publish-v-starting').hidden};
    const e2 = makeEnv();
    e2.handler = () => Promise.reject(err(undefined, 404));
    e2.publish.attach('/p'); e2.publish.previewState(true, 5801); await flush();
    R.J.off = {aria: e2.el('preview-publish').getAttribute('aria-disabled'), title: e2.el('preview-publish').title};
  }
  // K: an answer with no tunnel object is followed by a status read, never an empty form
  {
    const e = makeEnv();
    let renewed = false;
    e.handler = (p, o) => {
      if (p.indexOf('/api/publish/renew') === 0){ renewed = true; return {ok: true}; }
      if (renewed) return status([tunnel({id: 't9', state: 'starting', url: null})]);
      return status([tunnel()]);
    };
    e.publish.attach('/p'); await flush();
    e.el('preview-publish').click(); await flush();
    e.el('publish-renew').click(); e.el('publish-renew-yes').click(); await flush();
    const k = R.K = {renew: {startingShown: !e.el('publish-v-starting').hidden,
                             formHidden: e.el('publish-form').hidden, polling: e.intervals()}};
    const e2 = makeEnv();
    let started = false;
    e2.handler = (p, o) => {
      if (p.indexOf('/api/publish/start') === 0){ started = true; return {ok: true}; }
      return started ? status([tunnel({id: 't8', state: 'starting', url: null})]) : status([]);
    };
    e2.publish.attach('/p'); e2.publish.previewState(true, 5801); await flush();
    e2.el('preview-publish').click(); await flush();
    e2.el('publish-consent').checked = true; e2.el('publish-consent').fire('change');
    e2.el('publish-start').click(); await flush();
    k.start = {startingShown: !e2.el('publish-v-starting').hidden,
               formHidden: e2.el('publish-form').hidden, polling: e2.intervals()};
  }

  // L: the badge ticks with the dialog closed; each announcement goes where it can be heard
  {
    const e = makeEnv();
    e.handler = () => {
      const left = Math.max(0, 301 - e.now() / 1000);
      return status([tunnel({remaining_seconds: left, state: left > 0 ? 'live' : 'expired'})]);
    };
    e.publish.attach('/p'); await flush();
    e.el('preview-publish').click(); await flush();
    await e.advance(2000); await e.advance(100);           // 5:00 passes with the dialog open
    const l = R.L = {inside5: txt(e, 'publish-say'), outside5: txt(e, 'publish-announce')};
    e.el('publish-close').click(); await flush();
    const t0 = txt(e, 'preview-publish-time');
    await e.advance(3000);
    l.ticking = [t0, txt(e, 'preview-publish-time')];
    await e.advance(240000); await e.advance(100);         // 1:00 passes with it closed
    l.outside1 = txt(e, 'publish-announce');
  }

  console.log(JSON.stringify(R));
})().catch(e => { console.error(e); process.exit(1); });
"""


@pytest.fixture(scope="module")
def run():
    if not _node():
        pytest.skip("node is not installed")
    d = tempfile.mkdtemp()
    code_path = os.path.join(d, "publish.js")
    io.open(code_path, "w", encoding="utf-8").write(JS)
    harness = os.path.join(d, "harness.js")
    io.open(harness, "w", encoding="utf-8").write(HARNESS)
    out = subprocess.run([_node(), harness, code_path, json.dumps(PARENT)],
                         capture_output=True, text=True, encoding="utf-8", timeout=180)
    assert out.returncode == 0, (out.stdout + out.stderr)[-2000:]
    return json.loads(out.stdout)


def test_reload_shows_the_live_badge_and_an_exact_countdown(run):
    a = run["A"]
    assert a["aria"] == "false", "a live tunnel keeps the button usable even if the preview stopped"
    assert (a["label"], a["time"], a["timeHidden"]) == ("Published", " · 42:10", False)
    assert a["countdown"] == "42:10" and a["t1"] == "42:09"
    assert a["href"] == "https://quiet-fox.trycloudflare.com"
    assert a["options"] == ["15:15 minutes", "60:1 hour (default)", "240:4 hours", "720:12 hours", "1440:24 hours"]
    assert a["ttlValue"] == "60"
    assert a["intervals"] == 2, "one tick, one slow re-sync; no 1.5 s poll while live"


def test_under_five_minutes_says_so_in_words_and_speaks_once(run):
    a = run["A"]
    assert a["soonLabel"] == "Closing soon" and a["soonBtn"] is True and a["soonTagHidden"] is False
    assert a["soonTime"] == " · 5:00"
    assert a["soonSay"] == "Your public link closes in less than 5 minutes."
    assert a["lastSay"] == "Your public link closes in less than 1 minute."


def test_at_zero_it_expires_and_every_timer_stops(run):
    a = run["A"]
    assert a["endLabel"] == "Link expired" and a["expiredShown"] is True and a["liveHidden"] is True
    assert a["endSay"] == "Your public link has expired."
    assert a["endIntervals"] == 0, "nothing keeps ticking once the link is closed"


def test_leaving_the_project_clears_the_timers_and_the_badge(run):
    d = run["A"]["afterDetach"]
    assert d == {"label": "Publish", "aria": "true", "intervals": 0, "dialogOpen": False}


def test_the_badge_and_its_timers_need_no_open_dialog(run):
    a = run["A"]
    assert a["neverOpened"] is True, "scenario A ran start to expiry with the dialog shut"
    assert a["inBody"] is True, "the dialog is moved under <body> when the module starts"


def test_publish_flow_is_gated_then_polls_until_settled(run):
    b = run["B"]
    assert b["aria"] == "false" and b["formShown"] is True
    assert b["dialogOpen"] is True and b["modals"] == 1 and b["focus"] == "publish-title"
    assert b["disabled0"] is True and b["why0"] == "Tick the box above to continue."
    assert b["footForm"] is True
    assert b["disabled1"] is False
    assert b["post"]["path"] == "/api/publish/start"
    assert b["post"]["body"] == {"project_dir": "/p", "ttl_minutes": 60, "confirm": True, "port": 5801}
    assert b["consentAfter"] is False
    assert b["startingShown"] is True and b["btnLabel"] == "Starting…"
    assert b["focusAfterStart"] == "publish-title", "the pressed button's row went away; focus stays inside"
    # while it starts, the footer offers only Stop publishing
    assert b["startingFoot"] == {"form": True, "live": True, "stop": True, "copy": True, "renew": True}
    assert b["pollOn"] == 1
    assert b["stillStarting"] is True and b["liveShown"] is True
    assert b["countdown"] == "59:57"       # 3600 s minus the 3 s the fake clock ran before the answer
    assert b["say"] == "Your app is published. The link is ready."
    assert b["sayOutside"] == "", "with the dialog open, the inert page behind it is not where it speaks"
    assert b["liveFoot"] is True
    assert b["intervalsLive"] == 2, "the 1.5 s poll stops the moment the tunnel is live"
    assert b["extraGets"] == 0


def test_without_a_preview_the_button_explains_instead_of_opening(run):
    c = run["C"]
    assert c["aria"] == "true" and c["title"] == "Start the preview first"
    assert c["why"] == "Start the preview first"
    assert c["toasts"] == ["Start the preview first"] and c["dialogOpen"] is False
    assert c["ariaOn"] == "false"


def test_missing_cloudflared_offers_install_only_on_a_click(run):
    d = run["D"]
    assert d["installShown"] is True and d["cmd"] == "winget install --id Cloudflare.cloudflared"
    assert d["startDisabled"] is True and d["why"] == "Install cloudflared first."
    assert d["noAuto"] == 0
    assert d["body"] == {"confirm": True}
    assert d["btnText"] == "Installing…" and d["btnDisabled"] is True
    assert d["status"].startswith("Downloading cloudflared and checking its checksum")
    assert d["polling"] == 1
    assert d["doneShown"] is True and d["pollAfter"] == 0
    assert d["startWhyAfter"] == "Tick the box above to continue."


def test_errors_are_said_in_plain_words(run):
    e = run["E"]
    assert e["tooMany"] == "Too many public links are open already. Stop one first." and e["shown"] is True
    assert e["port"] == "This app runs on a port that cannot be published."
    assert e["plain"] == "server said no"


def test_new_link_asks_first_and_keeps_the_length(run):
    f = run["F"]
    assert f["confirmShown"] is True and f["focusYes"] == "publish-renew-no"      # safe choice has focus
    assert f["liveRowWhileAsking"] is True, "the question replaces the action row"
    assert f["confirmHidden"] is True and f["focusBack"] == "publish-renew" and f["noCall"] == 0
    assert f["escAnswer"] == {"asking": False, "open": True, "focus": "publish-renew", "liveRow": True}
    assert f["post"]["path"] == "/api/publish/renew"
    assert f["post"]["body"] == {"id": "t1", "ttl_minutes": 60}
    assert f["startingShown"] is True and f["liveHidden"] is True
    assert f["focusAfter"] == "publish-title"
    assert f["say"] == "A new link is being made. The old link no longer works."


def test_a_late_answer_for_the_old_project_is_ignored(run):
    assert run["G"] == {"label": "Publish", "intervals": 0, "liveHidden": True}


def test_open_close_and_where_focus_goes(run):
    h = run["H"]
    assert h["open"] is True and h["modals"] == 1 and h["focus"] == "publish-title"
    # the Files tab hides the button: the dialog closes without sending focus to it
    assert h["filesClosed"] is True and h["filesFocus"] != "preview-publish" and h["notReopened"] is True
    assert h["escClosed"] is True and h["escFocus"] == "preview-publish"
    assert h["xClosed"] is True and h["xFocus"] == "preview-publish"
    assert h["dragKept"] is True, "a drag that ends on the backdrop is not a press on it"
    assert h["backdropClosed"] is True and h["backdropFocus"] == "preview-publish"
    assert h["reopened"] is True and h["reopenFocus"] == "publish-title" and h["modalsAfter"] == 5


def test_the_badge_ticks_closed_and_each_warning_is_heard(run):
    l = run["L"]
    assert l["inside5"] == "Your public link closes in less than 5 minutes."
    assert l["outside5"] == ""
    assert l["ticking"] == [" · 4:59", " · 4:56"]
    assert l["outside1"] == "Your public link closes in less than 1 minute."


def test_a_failed_tunnel_shows_text_and_try_again_asks_for_the_tick_again(run):
    i = run["I"]
    assert i["failedShown"] is True
    assert i["msg"] == "The tunnel could not start. <b>boom</b>"          # text, never markup
    assert i["formShown"] is True and i["failedHidden"] is True
    assert i["startDisabled"] is True and i["focus"] == "publish-consent"


def test_an_answer_without_a_tunnel_is_followed_by_a_status_read(run):
    """renew's body is not pinned by the contract; the page must not fall back to
    an empty form (and stop polling) while a public tunnel is being made."""
    for which in ("renew", "start"):
        k = run["K"][which]
        assert k == {"startingShown": True, "formHidden": True, "polling": 1}, (which, k)


def test_hostile_urls_never_become_links_and_a_missing_backend_is_plain(run):
    j = run["J"]
    assert j["href"] is None and j["starting"] is True
    assert j["off"] == {"aria": "true",
                        "title": "Publishing is not available in this version of the hub."}
