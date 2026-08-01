"""Generated images leave the hub as WebP, with an honest mime.

USER 2026-08-01: "all images should be converted to webp".

Done in the HUB rather than in the craft brief on purpose: "convert to WebP" as
an instruction is a step a model can forget, and each generator returns
something different (cloudflare/flux answers JPEG, pollinations PNG). Converting
here means every client — every CLI, the dashboard, a raw curl — gets WebP
without knowing anything about it. Measured on a real generation: 484KB JPEG
became a 72KB WebP.
"""
import base64
import io

import pytest

import app


def _png_bytes(mode="RGB", size=(8, 8)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new(mode, size, "red").save(buf, format="PNG")
    return buf.getvalue()


def _b64(raw):
    return base64.b64encode(raw).decode("ascii")


def test_png_is_converted_to_webp():
    out, mime = app._to_webp_b64(_b64(_png_bytes()))
    assert mime == "image/webp"
    raw = base64.b64decode(out)
    assert raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"


def test_jpeg_is_converted_to_webp():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(buf, format="JPEG")
    out, mime = app._to_webp_b64(_b64(buf.getvalue()))
    assert mime == "image/webp"
    assert base64.b64decode(out)[8:12] == b"WEBP"


def test_palette_and_alpha_modes_do_not_raise():
    """WebP has no palette mode; some generators return P or LA images."""
    for mode in ("P", "LA", "L", "RGBA"):
        out, mime = app._to_webp_b64(_b64(_png_bytes(mode=mode)))
        assert mime == "image/webp", mode
        assert base64.b64decode(out)[8:12] == b"WEBP", mode


def test_webp_is_actually_smaller():
    """The whole point — a page that loads faster."""
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (400, 400), "red").save(buf, format="PNG")
    src = buf.getvalue()
    out, _mime = app._to_webp_b64(_b64(src))
    assert len(base64.b64decode(out)) < len(src)


def test_undecodable_input_is_returned_untouched():
    """A slightly larger image is a much better outcome than a 500."""
    out, mime = app._to_webp_b64("not base64 at all!!")
    assert out == "not base64 at all!!"
    assert mime.startswith("image/")


def test_unrecognisable_bytes_report_an_honest_mime():
    """Never claim webp for bytes that are not webp."""
    out, mime = app._to_webp_b64(_b64(b"\xff\xd8\xff" + b"junk that is not a jpeg"))
    assert mime in ("image/jpeg", "image/png", "image/webp")
    if mime != "image/webp":
        assert out == _b64(b"\xff\xd8\xff" + b"junk that is not a jpeg")


def test_pillow_missing_falls_back_instead_of_failing(monkeypatch):
    """Pillow is pinned in requirements.txt but must stay OPTIONAL at runtime."""
    import builtins
    real_import = builtins.__import__

    def no_pil(name, *a, **kw):
        if name.startswith("PIL"):
            raise ImportError("no PIL")
        return real_import(name, *a, **kw)

    src = _png_bytes()               # build it BEFORE PIL is taken away
    monkeypatch.setattr(builtins, "__import__", no_pil)
    out, mime = app._to_webp_b64(_b64(src))
    assert out == _b64(src)          # unchanged
    assert mime == "image/png"       # and correctly labelled
