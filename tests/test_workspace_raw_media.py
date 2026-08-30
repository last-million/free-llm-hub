"""Images and videos display in the file preview instead of "Binary file".

/api/workspace/file returns JSON: it decodes utf-8 and refuses anything with a
NUL byte, so every .png/.mp4 came back as {"binary": true} and the preview
printed "Binary file - not shown." There was no route anywhere that could serve
raw bytes, so an <img> had nothing to point at.

/api/workspace/raw serves the bytes, under exactly the guards every other
workspace route has -- and with a hard rule about WHICH types get a real media
type, because this is same-origin: a project's own .html or .svg rendered here
would run inside the dashboard's origin.
"""
import io
import os

import app
import config
import workspace


def _client():
    app.app.config["TESTING"] = True
    return app.app.test_client()


def _url(proj, path, token=True):
    q = "project_dir=%s&path=%s" % (proj.replace("\\", "/"), path)
    if token:
        q += "&token=" + config.ensure_control_token()
    return "/api/workspace/raw?" + q


def _proj(tmp_path):
    (tmp_path / "img").mkdir()
    # A real PNG header -- it carries a NUL, which is exactly what the JSON
    # endpoint rejects.
    (tmp_path / "img" / "a.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    (tmp_path / "clip.mp4").write_bytes(b"\x00\x00\x00 ftypmp42" + b"\x00" * 32)
    (tmp_path / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
    (tmp_path / "icon.svg").write_text("<svg onload='x()'/>", encoding="utf-8")
    return str(tmp_path)


def test_an_image_comes_back_as_an_image(tmp_path):
    p = _proj(tmp_path)
    r = _client().get(_url(p, "img/a.png"), headers={"X-Free-LLM-Hub": "dashboard"})
    assert r.status_code == 200
    assert r.headers["Content-Type"].startswith("image/png")
    assert r.data.startswith(b"\x89PNG")


def test_a_video_is_served_with_a_video_type_and_supports_range(tmp_path):
    """conditional=True matters: without Range support a browser cannot seek.

    Asserted by actually making a Range request rather than by looking for an
    Accept-Ranges header -- werkzeug does not always emit that header, so
    checking it tests the framework's advertising rather than the behaviour the
    video player depends on."""
    p = _proj(tmp_path)
    c = _client()
    r = c.get(_url(p, "clip.mp4"), headers={"X-Free-LLM-Hub": "dashboard"})
    assert r.status_code == 200
    assert r.headers["Content-Type"].startswith("video/mp4")
    part = c.get(_url(p, "clip.mp4"),
                 headers={"X-Free-LLM-Hub": "dashboard", "Range": "bytes=0-7"})
    assert part.status_code == 206, "a Range request must return partial content"
    assert len(part.data) == 8


def test_html_is_never_rendered_inline(tmp_path):
    """Same-origin: a project's own page must not run as the dashboard."""
    p = _proj(tmp_path)
    r = _client().get(_url(p, "index.html"), headers={"X-Free-LLM-Hub": "dashboard"})
    assert r.headers["Content-Type"].startswith("application/octet-stream")
    assert "attachment" in (r.headers.get("Content-Disposition") or "")


def test_svg_is_not_on_the_inline_allowlist(tmp_path):
    """SVG is XML that can carry script. It stays a download here, and stays
    source code in the text view."""
    assert ".svg" not in app._RAW_MEDIA_TYPES
    p = _proj(tmp_path)
    r = _client().get(_url(p, "icon.svg"), headers={"X-Free-LLM-Hub": "dashboard"})
    assert r.headers["Content-Type"].startswith("application/octet-stream")


def test_nosniff_is_set(tmp_path):
    p = _proj(tmp_path)
    r = _client().get(_url(p, "img/a.png"), headers={"X-Free-LLM-Hub": "dashboard"})
    assert r.headers.get("X-Content-Type-Options") == "nosniff"


def test_path_traversal_is_refused(tmp_path):
    p = _proj(tmp_path)
    for bad in ("../../../etc/passwd", "..\\..\\windows\\win.ini", "../outside.txt"):
        r = _client().get(_url(p, bad), headers={"X-Free-LLM-Hub": "dashboard"})
        assert r.status_code in (400, 404), (bad, r.status_code)


def test_a_missing_file_is_404_not_500(tmp_path):
    p = _proj(tmp_path)
    r = _client().get(_url(p, "img/nope.png"), headers={"X-Free-LLM-Hub": "dashboard"})
    assert r.status_code == 404


def test_the_csp_allows_same_origin_media():
    """<video> falls back to default-src 'none' without media-src, so it would
    be blocked before it ever loaded."""
    src = io.open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "app.py"), encoding="utf-8").read()
    assert "media-src 'self'" in src
    assert "img-src 'self' data:" in src


def test_the_preview_uses_the_raw_route_for_media():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    html = io.open(os.path.join(root, "templates", "index.html"),
                   encoding="utf-8").read()
    assert "/api/workspace/raw?" in html
    assert "function showMedia(rel)" in html
    # the JSON path is still used for text, and is only reached after media
    i, j = html.find("function openFile(rel)"), html.find("if (showMedia(rel)) return;")
    assert i != -1 and j > i
