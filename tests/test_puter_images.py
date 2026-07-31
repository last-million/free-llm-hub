"""Puter text-to-image: result-shape handling and the registry rows.

Puter's image driver answers in TWO shapes depending on which upstream served
the request (MEASURED live 2026-07-31):
  * OpenAI / Gemini backed models -> "data:image/png;base64,<payload>"
  * Replicate / Together backed models -> a bare https URL to a .webp/.jpg
The second was originally treated as base64 and decoded to 31 bytes of garbage,
so both paths are pinned here. No network: requests.get is monkeypatched.
"""
import base64

import pytest

import app
import providers as prov

PNG = b"\x89PNG\r\n\x1a\n" + b"fake"


def test_data_uri_result_is_unwrapped():
    b64, err = app._puter_image_b64("data:image/png;base64,QUJD")
    assert (b64, err) == ("QUJD", None)


def test_dict_wrapped_result_is_unwrapped():
    b64, err = app._puter_image_b64({"url": "data:image/png;base64,QUJD"})
    assert (b64, err) == ("QUJD", None)


def test_https_result_is_downloaded_not_treated_as_base64(monkeypatch):
    """REGRESSION: replicate/together return a URL. Decoding it as base64 gave
    31 bytes of nonsense that still looked like a 'successful' generation."""
    class R:
        status_code = 200
        content = PNG
    seen = {}

    def fake_get(url, **kw):
        seen["url"] = url
        return R()
    monkeypatch.setattr(app.requests, "get", fake_get)
    b64, err = app._puter_image_b64("https://replicate.delivery/x/out-0.webp")
    assert err is None
    assert base64.b64decode(b64) == PNG
    assert seen["url"].startswith("https://replicate.delivery/")


def test_an_unsafe_url_is_refused(monkeypatch):
    monkeypatch.setattr(app, "_is_safe_external_url", lambda u: False)
    monkeypatch.setattr(app.requests, "get", lambda *a, **k:
                        pytest.fail("must not fetch an unsafe URL"))
    b64, err = app._puter_image_b64("https://169.254.169.254/latest/meta-data/")
    assert b64 is None and "safety check" in err


def test_a_failed_download_reports_instead_of_returning_junk(monkeypatch):
    class R:
        status_code = 503
        content = b""
    monkeypatch.setattr(app.requests, "get", lambda *a, **k: R())
    b64, err = app._puter_image_b64("https://replicate.delivery/x/out-0.webp")
    assert b64 is None and "download failed" in err


@pytest.mark.parametrize("result", [None, "", "   ", 42, {}])
def test_empty_results_report_an_error(result):
    b64, err = app._puter_image_b64(result)
    assert b64 is None and err


# --------------------------------------------------------------------------- #
# Registry rows
# --------------------------------------------------------------------------- #

def _rows():
    return prov.get_provider("puter")["image_models"]


def test_every_puter_image_model_is_marked_paid():
    """All 59 models in Puter's image catalog carry a cost — there is no free
    image tier. Marking any of them free would let an Auto generation quietly
    spend the account balance."""
    rows = _rows()
    assert rows, "puter lost its image models"
    for r in rows:
        assert r.get("free") is False, "%s is not marked paid" % r["id"]


def test_puter_is_not_in_the_free_image_rotation():
    assert "puter" not in app._IMAGE_PROVIDER_ORDER


def test_registered_ids_are_the_ones_verified_to_generate():
    """A catalog listing is not proof: togetherai:lykon/dreamshaper is listed
    but 400s "Unable to access model" upstream, so it is deliberately absent."""
    ids = {r["id"] for r in _rows()}
    for verified in ("gpt-image-1-mini", "gpt-image-1", "gemini-2.5-flash-image",
                     "black-forest-labs/flux-schnell",
                     "togetherai:black-forest-labs/flux.1-schnell", "ai-image"):
        assert verified in ids, "%s missing" % verified
    assert "togetherai:lykon/dreamshaper" not in ids, "unreachable model re-added"


def test_the_sentinel_still_means_send_no_model():
    import inspect
    src = inspect.getsource(app._puter_generate_image)
    assert 'if model and model != _PUTER_IMAGE_DEFAULT_ID:' in src
    assert app._PUTER_IMAGE_DEFAULT_ID == "ai-image"


def test_rows_carry_a_cost_note_so_the_picker_can_show_it():
    for r in _rows():
        assert r.get("notes"), "%s has no notes" % r["id"]
        assert r.get("label"), "%s has no label" % r["id"]
