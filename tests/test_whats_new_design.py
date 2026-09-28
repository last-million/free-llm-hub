r"""The "What changed" window: plain-language notes, grouped, legible in both themes.

REQUESTED 2026-09-28: "edit the pop up of changes, I want it better in colors
css even in dark mode, and list new fixes". Commit subjects are written for
developers ("Provider status: no_key reason, dead-key memory, echoed-decimal
junk strike"); the window now shows a plain sentence per change, grouped as
New / Fixed / Improved / Other with counts, in the theme's own colour
families. MEASURED before the redesign: its dates (--text-faint) were 3.57:1
(dark) and 3.29:1 (light) -- under WCAG AA -- and the light theme's green text
on a green tint was 4.39:1. Every pair is measured below from the template's
own tokens, in both themes.
"""
import json
import os
import re

import pytest

import app as A

ROOT = os.path.dirname(os.path.abspath(A.__file__))
SRC = open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8").read()


# --------------------------------------------------------------------------- #
# Plain-language notes
# --------------------------------------------------------------------------- #

def test_an_override_names_kind_scope_and_title():
    o = {"abc1234": {"kind": "fix", "scope": "Agent sessions", "title": "Plain words"}}
    v = A._release_note_view("abc1234ffff", "Agent outages: jargon", "", o)
    assert v == {"kind": "fix", "title": "Plain words", "scope": "Agent sessions"}


def test_a_trailer_carries_the_note_of_a_new_commit():
    v = A._release_note_view("f00", "Popup: redesign", "improved: Easier to read in both themes", {})
    assert v == {"kind": "improved", "title": "Easier to read in both themes", "scope": "Popup"}
    v = A._release_note_view("f00", "Popup: redesign", "Just a sentence", {})
    assert v["title"] == "Just a sentence" and v["kind"] == "change"
    # git without trailer support prints the placeholder: ignored
    v = A._release_note_view("f00", "Popup: redesign", "%(trailers:key=Release-note)", {})
    assert v["title"] == "Redesign"


def test_without_either_the_subject_is_split_and_docs_are_recognised():
    v = A._release_note_view("f00", "Settings: one-click health check", "", {})
    assert v == {"kind": "change", "title": "One-click health check", "scope": "Settings"}
    assert A._release_note_view("f00", "a subject with no scope", "", {})["scope"] == ""
    assert A._release_note_view("f00", "README: route count 145", "", {})["kind"] == "docs"
    assert A._release_note_view("f00", "fix: a|b, c;d", "", {})["title"] == "A|b, c;d"


def test_the_endpoint_reads_all_three_and_hides_docs(monkeypatch):
    A._RELEASE_NOTES_CACHE["rows"] = None
    monkeypatch.setattr(A, "_is_git_repo", lambda: True)
    monkeypatch.setattr(A, "_release_note_overrides",
                        lambda: {"aaa1111": {"kind": "new", "title": "Skills in Settings"}})
    monkeypatch.setattr(A, "_git", lambda *a, **k: (0, (
        "aaa1111\x1f2026-09-28\x1fSettings: Skills -- jargon\x1faaa1111ffff\x1f\x1e"
        "bbb2222\x1f2026-09-28\x1fPopup: redesign\x1fbbb2222ffff\x1ffix: Clearer window\x1e"
        "ccc3333\x1f2026-09-27\x1fREADME: counts\x1fccc3333ffff\x1f\x1e"), ""))
    with A.app.test_request_context("/api/release-notes"):
        rows = A.api_release_notes().get_json()["notes"]
    assert [(r["hash"], r["kind"], r["title"]) for r in rows] == [
        ("aaa1111", "new", "Skills in Settings"), ("bbb2222", "fix", "Clearer window")]
    assert rows[1]["subject"] == "Popup: redesign" and rows[1]["scope"] == "Popup"


def test_the_log_asks_git_for_the_release_note_trailer(monkeypatch):
    A._RELEASE_NOTES_CACHE["rows"] = None
    seen = {}
    monkeypatch.setattr(A, "_is_git_repo", lambda: True)

    def fake(*args, **k):
        seen["args"] = args
        return (0, "", "")
    monkeypatch.setattr(A, "_git", fake)
    with A.app.test_request_context("/api/release-notes"):
        A.api_release_notes()
    fmt = [a for a in seen["args"] if a.startswith("--pretty=")][0]
    assert "%(trailers:key=Release-note" in fmt and "%H" in fmt


def test_the_shipped_notes_file_is_valid():
    with open(os.path.join(ROOT, "release_notes.json"), encoding="utf-8") as fh:
        notes = json.load(fh)["notes"]
    assert len(notes) >= 10
    for key, n in notes.items():
        assert re.fullmatch(r"[0-9a-f]{7,40}", key), key
        assert n["kind"] in A._RELEASE_KINDS, key
        assert n["title"].strip(), key
        assert len(n.get("scope", "")) <= 32, key
    assert A._release_note_overrides()          # the real file loads


# --------------------------------------------------------------------------- #
# The window
# --------------------------------------------------------------------------- #

def _css_block(start, end):
    i = SRC.index(start)
    return SRC[i:SRC.index(end, i)]


def test_every_colour_in_the_window_is_a_theme_token():
    css = _css_block('/* ---------- "what changed" ----------', "@media (prefers-reduced-motion:reduce){\n    .wn-list")
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", css), "hard-coded colour in the popup CSS"
    for kind in ("new", "fix", "improved", "change"):
        assert ".wn-k-%s{" % kind in css


def test_it_groups_counts_and_escapes():
    js = SRC[SRC.index("function showWhatsNew"):SRC.index("/* ---------- multi swarm windows")]
    for needle in ("WN_KINDS", "wn-group", "wn-count", "wn-scope", "wn-fresh-tag",
                   "esc(n.title)", "esc(n.subject)", "esc(n.scope)", "esc(n.date)"):
        assert needle in js or needle in SRC, needle
    assert "['fix', 'Fixed', 'fixed']" in SRC


def test_settings_can_open_it_any_time():
    assert 'id="settings-whats-new"' in SRC
    assert "initWhatsNewButton();" in SRC
    body = SRC[SRC.index("function initWhatsNewButton"):SRC.index("var WN_KINDS")]
    assert "/api/release-notes" in body and "showWhatsNew(" in body


# --------------------------------------------------------------------------- #
# Contrast, measured from the template's own tokens, both themes
# --------------------------------------------------------------------------- #

def _theme(name):
    def parse(block):
        return dict(re.findall(r"--([\w-]+):\s*([^;]+);", block))
    dark = parse(re.search(r"\n  :root\{(.*?)\n  \}", SRC, re.S).group(1))
    if name == "dark":
        return dark
    light = dict(dark)
    light.update(parse(re.search(r':root\[data-theme="light"\]\{(.*?)\n  \}', SRC, re.S).group(1)))
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


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_every_text_in_the_window_meets_aa(theme):
    t = _theme(theme)
    T = lambda k: _rgba(t[k])                                   # noqa: E731
    surf, s2, s3 = T("surface"), T("surface-2"), T("surface-3")
    pairs = {
        "new chip": (T("new-text"), _over(T("new-soft"), surf)),
        "fixed chip": (T("ok-text"), _over(T("accent-soft"), surf)),
        "improved chip": (T("info-text"), _over(T("info-soft"), surf)),
        "other chip": (T("text-dim"), s2),
        "new heading": (T("new-text"), surf),
        "fixed heading": (T("ok-text"), surf),
        "improved heading": (T("info-text"), surf),
        "item title": (T("text"), s2),
        "item date": (T("text-dim"), s2),
        "scope chip": (T("text-dim"), s3),
        "since-last-visit tag": (T("ok-text"), _over(T("accent-soft"), s2)),
        "star button": (T("surface"), T("text")),
    }
    low = {k: round(_ratio(*v), 2) for k, v in pairs.items() if _ratio(*v) < 4.5}
    assert not low, "%s theme under 4.5:1: %s" % (theme, low)
