"""A section at the top of the Providers page lists every dead API key and its provider.

Owner, 2026-10-27: "I want the hub to show that APIs are dead, and show on the provider page on top a
section declaring the dead API keys and in which provider."

The data already existed: every /api/providers row carries keys[] with `dead`, `dead_why`, `dead_until`
and a masked key (quota.mark_key_dead: a failed provider Test or two credential-shaped 401/403 in live
traffic). It was only visible inside each provider's card. Page-only change: `deadKeyRows` (pure) builds
the list, `renderDeadKeys` draws it with textContent only, the section stays hidden when nothing is dead.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8").read()


def _function(name):
    m = re.search(r"\n  function %s\(" % re.escape(name), HTML)
    assert m, name
    i = HTML.index("{", m.start())
    depth = 0
    for j in range(i, len(HTML)):
        depth += (HTML[j] == "{") - (HTML[j] == "}")
        if depth == 0:
            return HTML[m.start():j + 1]
    raise AssertionError(name)


def test_the_section_sits_above_the_provider_grid():
    sec = HTML.index('id="dead-keys"')
    assert sec < HTML.index('id="grid"')
    assert re.search(r'<section[^>]*id="dead-keys"[^>]*hidden', HTML), "hidden until something is dead"
    assert 'aria-labelledby="dead-keys-title"' in HTML


def test_it_is_redrawn_with_the_grid():
    assert "renderDeadKeys()" in _function("renderGrid")


def test_backend_strings_never_go_through_innerHTML():
    body = _function("renderDeadKeys")
    assert "innerHTML" not in body.replace("list.innerHTML = ''", "")
    assert "textContent" in body


def test_css_uses_theme_tokens_only():
    m = re.search(r"/\* dead-keys-css:start \*/(.*?)/\* dead-keys-css:end \*/", HTML, re.S)
    assert m
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", m.group(1)), "no raw colours"


NODE = shutil.which("node")


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_dead_key_rows_lists_each_dead_key_with_its_provider(tmp_path):
    providers = [
        {"id": "g4f", "name": "G4F", "keys": [
            {"index": 0, "masked": "g4f_...84bb", "dead": True, "dead_why": "Test: HTTP 401", "dead_until": 2000},
            {"index": 1, "masked": "g4f_...8aa9", "dead": True, "dead_why": "Test: HTTP 401", "dead_until": None}]},
        {"id": "groq", "name": "Groq", "keys": [
            {"index": 0, "masked": "gsk_...wDEK", "dead": False},
            {"index": 1, "masked": "gsk_...kikU", "dead": True, "dead_why": "401 twice", "dead_until": 5000}]},
        {"id": "nvidia", "name": "NVIDIA", "keys": [{"index": 0, "masked": "nvap", "dead": False}]},
        {"id": "nokeys", "name": "No keys"},
    ]
    script = _function("deadKeyRows") + "\nconsole.log(JSON.stringify(deadKeyRows(%s, 1000)));" % json.dumps(providers)
    f = tmp_path / "t.js"
    f.write_text(script, encoding="utf-8")
    out = json.loads(subprocess.run([NODE, str(f)], capture_output=True, text=True, check=True).stdout)
    assert [(r["pid"], r["index"]) for r in out] == [("g4f", 0), ("g4f", 1), ("groq", 1)]
    g = out[0]
    assert g["name"] == "G4F" and g["masked"] == "g4f_...84bb" and g["why"] == "Test: HTTP 401"
    assert g["all_dead"] is True and g["total"] == 2 and g["in_secs"] == 1000
    assert out[1]["in_secs"] is None
    assert out[2]["all_dead"] is False and out[2]["total"] == 2


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_nothing_dead_gives_an_empty_list(tmp_path):
    script = _function("deadKeyRows") + "\nconsole.log(JSON.stringify(deadKeyRows([{id:'a',keys:[{dead:false}]}, null, {}], 0)));"
    f = tmp_path / "t.js"
    f.write_text(script, encoding="utf-8")
    assert subprocess.run([NODE, str(f)], capture_output=True, text=True, check=True).stdout.strip() == "[]"


def test_the_page_renders_and_its_script_parses():
    import app as A
    r = A.app.test_client().get("/")
    assert r.status_code == 200 and b'id="dead-keys"' in r.data
    if not NODE:
        return
    for i, s in enumerate(re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", r.data.decode("utf-8"), re.S)):
        if not s.strip():
            continue
        p = os.path.join(os.environ.get("TMP", "."), "dk_check_%d_%d.js" % (os.getpid(), i))
        try:
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(s)
            res = subprocess.run([NODE, "--check", p], capture_output=True, text=True)
            assert res.returncode == 0, res.stderr[:400]
        finally:
            os.remove(p)
