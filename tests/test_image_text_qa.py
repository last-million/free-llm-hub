"""User 2026-08-05: "for image generation with AI I want that he verify with
vision each image to see if there is TEXT issue in the image so then ask to
regenerate it to fix the text cause sometimes image generators do mistakes
in text."

_image_text_qa_flagged (app.py) asks a vision-capable model whether a
just-generated image has garbled/wrong text. The main /v1/images/generations
loop treats a flagged image as a soft failure: fall through to the next hop
(a real regeneration attempt on a different provider/model) rather than
accepting it, but fail OPEN -- both when no vision model is reachable at all,
and as a last resort when every hop's image gets flagged, since a real image
beats a 502 over a soft quality heuristic.

Uses tempfile.mkdtemp (not pytest's tmp_path -- broken by a pre-existing
Windows permission issue in this environment, unrelated to this feature).
"""
import base64
import os
import shutil
import tempfile

import pytest

import app


@pytest.fixture
def state_dir():
    d = tempfile.mkdtemp(prefix="hub-pytest-imgqa-")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def isolated_config(state_dir, monkeypatch):
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", os.path.join(state_dir, "state", "config.json"))


class _Resp:
    def __init__(self, status, content):
        self.status_code = status
        self._content = content

    def json(self):
        return {"choices": [{"message": {"role": "assistant", "content": self._content}}]}


_FAKE_B64 = base64.b64encode(b"fake-image-bytes").decode()


# --------------------------------------------------------------------------- #
# _image_text_qa_flagged
# --------------------------------------------------------------------------- #

def test_no_vision_candidates_fails_open_to_none(monkeypatch):
    monkeypatch.setattr(app, "_vision_candidates", lambda: [])
    assert app._image_text_qa_flagged(_FAKE_B64) is None


def test_vision_model_says_yes_returns_true(monkeypatch):
    monkeypatch.setattr(app, "_vision_candidates", lambda: [("google", "gemini-vision")])
    monkeypatch.setattr(app, "_dispatch_chat",
                        lambda pid, payload, stream: _Resp(200, "YES the sign reads 'Wlecome'."))
    assert app._image_text_qa_flagged(_FAKE_B64) is True


def test_vision_model_says_no_returns_false(monkeypatch):
    monkeypatch.setattr(app, "_vision_candidates", lambda: [("google", "gemini-vision")])
    monkeypatch.setattr(app, "_dispatch_chat",
                        lambda pid, payload, stream: _Resp(200, "NO the text looks correct."))
    assert app._image_text_qa_flagged(_FAKE_B64) is False


def test_first_candidate_erroring_falls_through_to_second(monkeypatch):
    monkeypatch.setattr(app, "_vision_candidates",
                        lambda: [("dead-provider", "m1"), ("google", "gemini-vision")])
    calls = []

    def fake_dispatch(pid, payload, stream):
        calls.append(pid)
        if pid == "dead-provider":
            raise app.requests.exceptions.ConnectionError("down")
        return _Resp(200, "NO")
    monkeypatch.setattr(app, "_dispatch_chat", fake_dispatch)
    assert app._image_text_qa_flagged(_FAKE_B64) is False
    assert calls == ["dead-provider", "google"]


def test_all_candidates_failing_fails_open_to_none(monkeypatch):
    monkeypatch.setattr(app, "_vision_candidates",
                        lambda: [("a", "m1"), ("b", "m2")])
    monkeypatch.setattr(app, "_dispatch_chat",
                        lambda pid, payload, stream: _Resp(500, "irrelevant"))
    assert app._image_text_qa_flagged(_FAKE_B64) is None


def test_never_raises_on_a_broken_vision_candidates_lookup(monkeypatch):
    def boom():
        raise RuntimeError("registry broke")
    monkeypatch.setattr(app, "_vision_candidates", boom)
    assert app._image_text_qa_flagged(_FAKE_B64) is None


# --------------------------------------------------------------------------- #
# End-to-end: /v1/images/generations
# --------------------------------------------------------------------------- #

def test_flagged_image_falls_through_to_the_next_hop(isolated_config, monkeypatch):
    monkeypatch.setattr(app, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(app, "_image_candidates", lambda: [
        ("cloudflare", "@cf/black-forest-labs/flux-1-schnell"),
        ("pollinations", "flux"),
    ])
    monkeypatch.setitem(app._IMAGE_GENERATORS, "cloudflare",
                        lambda pcfg, model, prompt, size=1024, steps=4:
                        (200, _FAKE_B64, None))
    monkeypatch.setitem(app._IMAGE_GENERATORS, "pollinations",
                        lambda pcfg, model, prompt, size=1024, steps=4:
                        (200, _FAKE_B64, None))
    # Flag the FIRST hop's image, clear the second.
    flagged_seq = [True, False]
    monkeypatch.setattr(app, "_image_text_qa_flagged", lambda b64: flagged_seq.pop(0))
    client = app.app.test_client()
    response = client.post("/v1/images/generations", json={
        "prompt": "a shop sign", "model": "cloudflare/@cf/black-forest-labs/flux-1-schnell",
    })
    assert response.status_code == 200
    assert response.get_json()["model"] == "pollinations/flux"


def test_every_hop_flagged_still_returns_the_image_not_a_502(isolated_config, monkeypatch):
    monkeypatch.setattr(app, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(app, "_image_candidates", lambda: [
        ("cloudflare", "@cf/black-forest-labs/flux-1-schnell"),
    ])
    monkeypatch.setitem(app._IMAGE_GENERATORS, "cloudflare",
                        lambda pcfg, model, prompt, size=1024, steps=4:
                        (200, _FAKE_B64, None))
    monkeypatch.setattr(app, "_image_text_qa_flagged", lambda b64: True)
    client = app.app.test_client()
    response = client.post("/v1/images/generations", json={
        "prompt": "a shop sign", "model": "cloudflare/@cf/black-forest-labs/flux-1-schnell",
    })
    assert response.status_code == 200, \
        "a real (if text-flagged) image must beat a 502 over a soft quality heuristic"
    assert response.get_json()["data"]


def test_no_vision_model_reachable_accepts_the_first_clean_result(isolated_config, monkeypatch):
    """None (fail-open) must behave exactly like a clean/False result -- the
    first hop is accepted immediately, no fallthrough."""
    monkeypatch.setattr(app, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(app, "_image_candidates", lambda: [
        ("cloudflare", "@cf/black-forest-labs/flux-1-schnell"),
        ("pollinations", "flux"),
    ])
    monkeypatch.setitem(app._IMAGE_GENERATORS, "cloudflare",
                        lambda pcfg, model, prompt, size=1024, steps=4:
                        (200, _FAKE_B64, None))
    monkeypatch.setattr(app, "_image_text_qa_flagged", lambda b64: None)
    client = app.app.test_client()
    response = client.post("/v1/images/generations", json={
        "prompt": "a fox", "model": "cloudflare/@cf/black-forest-labs/flux-1-schnell",
    })
    assert response.status_code == 200
    assert response.get_json()["model"] == "cloudflare/@cf/black-forest-labs/flux-1-schnell"
