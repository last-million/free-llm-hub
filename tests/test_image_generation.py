import base64

import pytest

import app
import config
import providers as prov


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    path = tmp_path / "state" / "config.json"
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", str(path))
    app._runtime_active[0] = 0
    app._runtime_shutdown_thread[0] = None
    app._runtime_server[0] = None
    return path


# ---------------------------------------------------------------------------
# _image_model_rows / _image_candidates / _resolve_image_model
# ---------------------------------------------------------------------------

def test_image_model_rows_returns_registry_rows():
    rows = app._image_model_rows("cloudflare")
    ids = [r["id"] for r in rows]
    assert "@cf/black-forest-labs/flux-1-schnell" in ids
    assert all(isinstance(r, dict) and r.get("id") for r in rows)


def test_image_model_rows_empty_for_unknown_provider():
    assert app._image_model_rows("does-not-exist") == []


def test_image_candidates_manual_priority_first(monkeypatch):
    monkeypatch.setattr(app, "_available_providers", lambda: ["pollinations", "cloudflare"])
    monkeypatch.setattr(config, "get_images_state", lambda: {
        "priority_mode": "manual",
        "manual_priority": ["cloudflare/@cf/black-forest-labs/flux-1-schnell"],
    })
    candidates = app._image_candidates()
    assert candidates[0] == ("cloudflare", "@cf/black-forest-labs/flux-1-schnell")
    # the auto tail still carries every other available pair
    assert ("pollinations", "flux") in candidates
    assert ("pollinations", "turbo") in candidates


def test_resolve_image_model_pinned():
    pid, model = app._resolve_image_model(
        "cloudflare/@cf/black-forest-labs/flux-1-schnell")
    assert (pid, model) == ("cloudflare", "@cf/black-forest-labs/flux-1-schnell")


def test_resolve_image_model_auto_picks_top_candidate(monkeypatch):
    monkeypatch.setattr(app, "_image_candidates",
                        lambda: [("pollinations", "flux"), ("cloudflare", "x")])
    assert app._resolve_image_model("auto") == ("pollinations", "flux")
    assert app._resolve_image_model("") == ("pollinations", "flux")


def test_resolve_image_model_auto_errors_when_none_available(monkeypatch):
    monkeypatch.setattr(app, "_image_candidates", lambda: [])
    pid, err = app._resolve_image_model("auto")
    assert pid is None
    assert isinstance(err, str) and err


def test_resolve_image_model_unknown_pinned_id():
    pid, err = app._resolve_image_model("cloudflare/does-not-exist")
    assert pid is None
    assert "Unknown image model" in err


# ---------------------------------------------------------------------------
# /api/images
# ---------------------------------------------------------------------------

def test_images_api_get_shape(isolated_config):
    client = app.app.test_client()
    response = client.get("/api/images")
    assert response.status_code == 200
    body = response.get_json()
    assert "state" in body and "models" in body and "effective_priority" in body
    assert body["state"]["priority_mode"] == "auto"
    ids = [m["id"] for m in body["models"]]
    assert "pollinations/flux" in ids


def test_images_api_uses_revision_cas(isolated_config, monkeypatch):
    monkeypatch.setattr(app, "_image_candidates", lambda: [])
    client = app.app.test_client()
    response = client.post("/api/images", json={
        "revision": 0, "priority_mode": "manual",
        "manual_priority": ["pollinations/flux"],
    }, headers={"X-Free-LLM-Hub": "dashboard"})
    assert response.status_code == 200
    assert response.get_json()["state"]["revision"] == 1
    stale = client.post("/api/images", json={
        "revision": 0, "priority_mode": "auto", "manual_priority": [],
    }, headers={"X-Free-LLM-Hub": "dashboard"})
    assert stale.status_code == 409
    stale_body = stale.get_json()
    assert "error" in stale_body
    assert stale_body["current_revision"] == 1
    assert "state" in stale_body


def test_images_api_rejects_unknown_model(isolated_config, monkeypatch):
    monkeypatch.setattr(app, "_image_candidates", lambda: [])
    client = app.app.test_client()
    response = client.post("/api/images", json={
        "revision": 0, "priority_mode": "manual",
        "manual_priority": ["cloudflare/not-a-real-model"],
    }, headers={"X-Free-LLM-Hub": "dashboard"})
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# POST /v1/images/generations
# ---------------------------------------------------------------------------

def test_images_generations_success(isolated_config, monkeypatch):
    monkeypatch.setattr(app, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(app, "_image_candidates",
                        lambda: [("cloudflare", "@cf/black-forest-labs/flux-1-schnell")])
    monkeypatch.setitem(app._IMAGE_GENERATORS, "cloudflare",
                        lambda pcfg, model, prompt, size=1024, steps=4:
                        (200, "ZmFrZS1pbWFnZQ==", None))
    client = app.app.test_client()
    response = client.post("/v1/images/generations", json={
        "prompt": "a fox", "model": "cloudflare/@cf/black-forest-labs/flux-1-schnell",
    })
    assert response.status_code == 200
    body = response.get_json()
    assert body["data"] == [{"b64_json": "ZmFrZS1pbWFnZQ=="}]
    assert body["model"] == "cloudflare/@cf/black-forest-labs/flux-1-schnell"


def test_images_generations_falls_back_to_next_provider(isolated_config, monkeypatch):
    monkeypatch.setattr(app, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(app, "_image_candidates", lambda: [
        ("cloudflare", "@cf/black-forest-labs/flux-1-schnell"),
        ("pollinations", "flux"),
    ])
    monkeypatch.setitem(app._IMAGE_GENERATORS, "cloudflare",
                        lambda pcfg, model, prompt, size=1024, steps=4:
                        (400, None, "some error"))
    monkeypatch.setitem(app._IMAGE_GENERATORS, "pollinations",
                        lambda pcfg, model, prompt, size=1024, steps=4:
                        (200, "c2Vjb25kLWhvcA==", None))
    client = app.app.test_client()
    response = client.post("/v1/images/generations", json={
        "prompt": "a fox", "model": "cloudflare/@cf/black-forest-labs/flux-1-schnell",
    })
    assert response.status_code == 200
    body = response.get_json()
    assert body["data"] == [{"b64_json": "c2Vjb25kLWhvcA=="}]
    assert body["model"] == "pollinations/flux"


def test_images_generations_all_providers_fail(isolated_config, monkeypatch):
    monkeypatch.setattr(app, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(app, "_image_candidates", lambda: [
        ("cloudflare", "@cf/black-forest-labs/flux-1-schnell"),
        ("pollinations", "flux"),
    ])
    monkeypatch.setitem(app._IMAGE_GENERATORS, "cloudflare",
                        lambda pcfg, model, prompt, size=1024, steps=4:
                        (400, None, "cf broke"))
    monkeypatch.setitem(app._IMAGE_GENERATORS, "pollinations",
                        lambda pcfg, model, prompt, size=1024, steps=4:
                        (502, None, "pollinations broke"))
    client = app.app.test_client()
    response = client.post("/v1/images/generations", json={
        "prompt": "a fox", "model": "cloudflare/@cf/black-forest-labs/flux-1-schnell",
    })
    assert response.status_code == 502
    body = response.get_json()
    assert "error" in body
    assert "message" in body["error"]
    assert "All image providers failed" in body["error"]["message"]


def test_images_generations_missing_prompt(isolated_config):
    client = app.app.test_client()
    response = client.post("/v1/images/generations", json={
        "model": "cloudflare/@cf/black-forest-labs/flux-1-schnell",
    })
    assert response.status_code == 400


def test_images_generations_n_param_repeats_same_generator(isolated_config, monkeypatch):
    calls = {"count": 0}

    def generator(pcfg, model, prompt, size=1024, steps=4):
        calls["count"] += 1
        return 200, "cmVwZWF0", None

    monkeypatch.setattr(app, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(app, "_image_candidates",
                        lambda: [("cloudflare", "@cf/black-forest-labs/flux-1-schnell")])
    monkeypatch.setitem(app._IMAGE_GENERATORS, "cloudflare", generator)
    client = app.app.test_client()
    response = client.post("/v1/images/generations", json={
        "prompt": "a fox", "model": "cloudflare/@cf/black-forest-labs/flux-1-schnell",
        "n": 2,
    })
    assert response.status_code == 200
    body = response.get_json()
    assert len(body["data"]) == 2
    assert calls["count"] == 2


# ---------------------------------------------------------------------------
# Provider-shape unit tests (no network)
# ---------------------------------------------------------------------------

def test_cf_generate_image_json_response(monkeypatch):
    monkeypatch.setattr(app, "_cf_account_id", lambda api_key: "acct123")

    class Resp:
        status_code = 200
        headers = {"Content-Type": "application/json"}

        def json(self):
            return {"result": {"image": "b64img"}, "success": True}

    monkeypatch.setattr(app.requests, "post", lambda *a, **kw: Resp())
    status, b64, err = app._cf_generate_image(
        {"api_keys": ["key"]}, "@cf/black-forest-labs/flux-1-schnell", "a fox")
    assert (status, b64, err) == (200, "b64img", None)


def test_cf_generate_image_raw_bytes_response(monkeypatch):
    monkeypatch.setattr(app, "_cf_account_id", lambda api_key: "acct123")
    raw = b"\x89PNG-fake-bytes"

    class Resp:
        status_code = 200
        headers = {"Content-Type": "image/png"}
        content = raw

    monkeypatch.setattr(app.requests, "post", lambda *a, **kw: Resp())
    status, b64, err = app._cf_generate_image(
        {"api_keys": ["key"]}, "@cf/bytedance/stable-diffusion-xl-lightning", "a fox")
    assert status == 200
    assert err is None
    assert base64.b64decode(b64) == raw


def test_modelscope_generate_image_polls_then_downloads(monkeypatch):
    monkeypatch.setattr(app.time, "sleep", lambda *_: None)
    # The download step now runs the image URL through _is_safe_external_url,
    # which does a real DNS lookup -- stub it to a public IP so this test
    # doesn't depend on network access and isn't exercising DNS, just the
    # scheme/private-IP logic.
    monkeypatch.setattr(app.socket, "getaddrinfo",
                        lambda *a, **kw: [(2, 1, 6, "", ("93.184.216.34", 0))])

    def fake_post(url, headers=None, json=None, timeout=None):
        assert "/v1/images/generations" in url

        class R:
            status_code = 200

            def json(self):
                return {"task_id": "abc"}
        return R()

    def fake_get(url, headers=None, timeout=None):
        if "/v1/tasks/" in url:
            class R:
                status_code = 200

                def json(self):
                    return {"task_status": "SUCCEED",
                             "output_images": ["https://x/y.png"]}
            return R()

        class R2:
            status_code = 200
            content = b"downloaded-bytes"
        return R2()

    monkeypatch.setattr(app.requests, "post", fake_post)
    monkeypatch.setattr(app.requests, "get", fake_get)
    status, b64, err = app._modelscope_generate_image(
        {"api_keys": ["key"]}, "Qwen/Qwen-Image", "a fox")
    assert status == 200
    assert err is None
    assert base64.b64decode(b64) == b"downloaded-bytes"


def test_pollinations_generate_image_no_key_required(monkeypatch):
    class Resp:
        status_code = 200
        content = b"pollinations-bytes"
        text = ""

    monkeypatch.setattr(app.requests, "get", lambda *a, **kw: Resp())
    status, b64, err = app._pollinations_generate_image(
        {"api_keys": []}, "flux", "a fox")
    assert status == 200
    assert err is None
    assert base64.b64decode(b64) == b"pollinations-bytes"


# ---------------------------------------------------------------------------
# Adversarial-review fixups: soft-400 simplification, steps validation,
# Cloudflare custom base URL, ModelScope SSRF guard, size width/height.
# ---------------------------------------------------------------------------

def test_classify_soft_400_ignores_unrelated_large_numbers():
    """A stray large number elsewhere in the body (e.g. a request id) must
    not matter anymore -- the fix that tried to learn a 'required token
    count' from it was removed entirely after review found it could inflate
    to billions and wrongly disqualify every remaining fallback hop."""
    class Resp:
        def json(self):
            return {"error": {"message": "Please reduce the length of the messages.",
                              "code": "context_length_exceeded"},
                    "request_id": "req_9284710385"}
    assert app._classify_soft_400(Resp()) is True


def test_classify_soft_400_thought_signature():
    class Resp:
        def json(self):
            return {"error": {"message": "missing thought_signature in functionCall parts"}}
    assert app._classify_soft_400(Resp()) is True


def test_classify_soft_400_false_for_unrelated_400():
    class Resp:
        def json(self):
            return {"error": {"message": "invalid JSON body"}}
    assert app._classify_soft_400(Resp()) is False


def test_images_generations_rejects_bad_steps_gracefully(isolated_config, monkeypatch):
    """A non-numeric `steps` must not crash the whole request -- it silently
    falls back to the default, same as an invalid `n`."""
    monkeypatch.setattr(app, "_resolve_image_model",
                        lambda m: ("cloudflare", "@cf/black-forest-labs/flux-1-schnell"))
    monkeypatch.setattr(app, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(app, "_image_candidates", lambda: [])
    captured = {}

    def fake_cf(pcfg, model, prompt, size=1024, steps=4):
        captured["steps"] = steps
        return 200, "b64", None

    monkeypatch.setattr(app, "_IMAGE_GENERATORS", {"cloudflare": fake_cf})
    client = app.app.test_client()
    response = client.post("/v1/images/generations",
                           json={"prompt": "a cat", "steps": "fast"})
    assert response.status_code == 200
    assert captured["steps"] == 4


def test_cf_generate_image_honors_custom_base_url(monkeypatch):
    """A user-pasted account-scoped base URL must resolve the account id
    directly instead of re-calling _cf_account_id (which would fail again for
    a token too narrowly scoped to list accounts -- exactly the case the
    custom-base-URL field exists to work around)."""
    monkeypatch.setattr(app, "_cf_account_id", lambda api_key: (_ for _ in ()).throw(
        AssertionError("should not be called when a custom base_url is set")))
    captured = {}

    class Resp:
        status_code = 200
        headers = {"Content-Type": "application/json"}

        def json(self):
            return {"result": {"image": "b64img"}, "success": True}

    def fake_post(url, **kw):
        captured["url"] = url
        return Resp()

    monkeypatch.setattr(app.requests, "post", fake_post)
    status, b64, err = app._cf_generate_image(
        {"api_keys": ["key"],
         "base_url": "https://api.cloudflare.com/client/v4/accounts/manual-acct/ai/v1"},
        "@cf/black-forest-labs/flux-1-schnell", "a fox")
    assert (status, b64, err) == (200, "b64img", None)
    assert "accounts/manual-acct/ai/run/" in captured["url"]


def test_modelscope_generate_image_rejects_unsafe_image_url(monkeypatch):
    """A ModelScope task result pointing at a private/loopback address must
    be refused rather than fetched."""
    monkeypatch.setattr(app.time, "sleep", lambda *_: None)

    def fake_post(url, headers=None, json=None, timeout=None):
        class R:
            status_code = 200

            def json(self):
                return {"task_id": "abc"}
        return R()

    def fake_get(url, headers=None, timeout=None):
        class R:
            status_code = 200

            def json(self):
                return {"task_status": "SUCCEED",
                        "output_images": ["http://169.254.169.254/latest/meta-data/"]}
        return R()

    monkeypatch.setattr(app.requests, "post", fake_post)
    monkeypatch.setattr(app.requests, "get", fake_get)
    status, b64, err = app._modelscope_generate_image(
        {"api_keys": ["key"]}, "Qwen/Qwen-Image", "a fox")
    assert status != 200
    assert b64 is None
    assert "unsafe" in (err or "").lower()


def test_is_safe_external_url_blocks_private_and_non_https(monkeypatch):
    monkeypatch.setattr(app.socket, "getaddrinfo",
                        lambda *a, **kw: [(2, 1, 6, "", ("10.0.0.5", 0))])
    assert app._is_safe_external_url("https://internal.example/x") is False
    assert app._is_safe_external_url("http://example.com/x") is False
    assert app._is_safe_external_url("https://user:pass@example.com/x") is False


def test_is_safe_external_url_allows_public_https(monkeypatch):
    monkeypatch.setattr(app.socket, "getaddrinfo",
                        lambda *a, **kw: [(2, 1, 6, "", ("93.184.216.34", 0))])
    assert app._is_safe_external_url("https://example.com/x.png") is True


def test_parse_wh_keeps_width_and_height_independent():
    """The prior bug squared everything (one scalar used for both width and
    height) -- 'portrait' silently became a square image."""
    assert app._parse_wh("768x1024") == (768, 1024)
    assert app._parse_wh("1024x768") == (1024, 768)
    assert app._parse_wh("not-a-size") == (1024, 1024)
    assert app._parse_wh(1024) == (1024, 1024)


def test_images_generations_marks_throttled_on_429(isolated_config, monkeypatch):
    monkeypatch.setattr(app, "_resolve_image_model",
                        lambda m: ("cloudflare", "@cf/black-forest-labs/flux-1-schnell"))
    monkeypatch.setattr(app, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(app, "_image_candidates", lambda: [])
    throttled = {}
    monkeypatch.setattr(app.quota, "mark_throttled",
                        lambda pid, seconds=None: throttled.setdefault(pid, seconds))
    monkeypatch.setattr(app, "_IMAGE_GENERATORS",
                        {"cloudflare": lambda pcfg, model, prompt, size=1024, steps=4:
                         (429, None, "rate limited")})
    client = app.app.test_client()
    response = client.post("/v1/images/generations", json={"prompt": "a cat"})
    assert response.status_code != 200
    assert throttled.get("cloudflare") == 60


# ---------------------------------------------------------------------------
# PAID image providers -- OpenAI / Google / OpenRouter / Higgsfield.
#
# This is the single highest-stakes correctness property in this whole batch
# of image-gen work: a PAID image model must NEVER be auto/manual-rotated --
# reachable ONLY via an explicit "<provider>/<model>" pin, exactly like this
# hub's existing paid CHAT providers (deepseek/kimi/minimax). It is enforced
# per-MODEL (row["free"] is False), not per-provider, in _image_candidates().
# ---------------------------------------------------------------------------

_PAID_IMAGE_PAIRS = [
    ("openai", "gpt-image-1.5"),
    ("openai", "gpt-image-2"),
    ("google", "gemini-3.1-flash-image"),
    ("google", "gemini-3-pro-image-preview"),
    ("openrouter", "bytedance-seed/seedream-4.5"),
    ("higgsfield", "higgsfield/text2image/soul"),
    ("higgsfield", "flux-pro/kontext/max/text-to-image"),
    ("higgsfield", "bytedance/seedream/v4/text-to-image"),
    ("higgsfield", "higgsfield/nano-banana-pro"),
]


def test_paid_image_provider_rows_all_registered_as_not_free():
    """Registry-level guard: every image_models row belonging to a provider
    marked paid=True must carry free=False -- a data-entry regression here
    (a new paid image model added without the flag) would silently smuggle a
    billed model into the free auto/manual rotation."""
    for pid, pcfg in prov.PROVIDERS.items():
        if not pcfg.get("paid"):
            continue
        for row in pcfg.get("image_models") or []:
            assert row.get("free") is False, (
                "%s/%s is on a paid provider but not marked free:False" % (pid, row.get("id")))


def test_free_image_provider_rows_still_free():
    """Regression guard the other direction: the pre-existing genuinely-free
    image providers must not have been flipped to non-free by this change."""
    for pid in ("cloudflare", "modelscope", "pollinations"):
        rows = app._image_model_rows(pid)
        assert rows, "%s should still expose free image models" % pid
        assert all(r.get("free", True) is True for r in rows)


def test_all_expected_paid_image_pairs_are_registered():
    for pid, model in _PAID_IMAGE_PAIRS:
        rows = app._image_model_rows(pid)
        match = next((r for r in rows if r["id"] == model), None)
        assert match is not None, "%s/%s missing from registry" % (pid, model)
        assert match.get("free") is False


def test_image_candidates_never_includes_paid_providers_even_when_available(monkeypatch):
    """The paid providers being enabled+keyed (i.e. present in
    _available_providers()) must make ZERO difference -- _image_candidates()
    filters per-MODEL, not per-provider, so a paid model never enters the
    auto/manual rotation regardless of whether its provider is otherwise
    usable."""
    monkeypatch.setattr(app, "_available_providers",
                        lambda: ["cloudflare", "openai", "google", "openrouter", "higgsfield"])
    monkeypatch.setattr(config, "get_images_state", lambda: {"priority_mode": "auto"})
    candidates = app._image_candidates()
    pids_in_candidates = {pid for pid, _ in candidates}
    assert "openai" not in pids_in_candidates
    assert "google" not in pids_in_candidates
    assert "higgsfield" not in pids_in_candidates
    # openrouter's only image_models row is the paid Seedream one -- it has no
    # free image row of its own, so it must not appear as a candidate either.
    assert "openrouter" not in pids_in_candidates
    # the one genuinely-free provider in the mix is still present
    assert any(pid == "cloudflare" for pid, _ in candidates)


@pytest.mark.parametrize("pid,model", _PAID_IMAGE_PAIRS)
def test_resolve_image_model_pins_paid_provider_explicitly(pid, model):
    """An explicit '<provider>/<model>' pin is the ONLY way to reach a paid
    image model -- confirm _resolve_image_model honors it for every paid
    model in the registry."""
    resolved_pid, resolved_model = app._resolve_image_model(pid + "/" + model)
    assert (resolved_pid, resolved_model) == (pid, model)


def test_images_generations_explicit_paid_pin_succeeds(isolated_config, monkeypatch):
    monkeypatch.setattr(app, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(app, "_image_candidates", lambda: [])  # no free providers configured
    monkeypatch.setitem(app._IMAGE_GENERATORS, "openai",
                        lambda pcfg, model, prompt, size="1024x1024", steps=4:
                        (200, "cGFpZC1pbWFnZQ==", None))
    client = app.app.test_client()
    response = client.post("/v1/images/generations", json={
        "prompt": "a fox", "model": "openai/gpt-image-1.5",
    })
    assert response.status_code == 200
    body = response.get_json()
    assert body["model"] == "openai/gpt-image-1.5"
    assert body["data"] == [{"b64_json": "cGFpZC1pbWFnZQ=="}]


def test_images_generations_paid_pin_never_falls_back_to_other_paid_providers(isolated_config, monkeypatch):
    """If the explicitly pinned paid provider fails, the fallback chain must
    come only from _image_candidates() (free-only) -- a second paid provider
    must NEVER be silently tried next, even if it's enabled+keyed too."""
    monkeypatch.setattr(app, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(app, "_image_candidates",
                        lambda: [("cloudflare", "@cf/black-forest-labs/flux-1-schnell")])
    called = {"google": False}

    def fake_google(pcfg, model, prompt, size="1024x1024", steps=4):
        called["google"] = True
        return 200, "should-not-be-used", None

    monkeypatch.setitem(app._IMAGE_GENERATORS, "openai",
                        lambda pcfg, model, prompt, size="1024x1024", steps=4:
                        (400, None, "openai broke"))
    monkeypatch.setitem(app._IMAGE_GENERATORS, "google", fake_google)
    monkeypatch.setitem(app._IMAGE_GENERATORS, "cloudflare",
                        lambda pcfg, model, prompt, size=1024, steps=4:
                        (200, "ZnJlZS1mYWxsYmFjaw==", None))
    client = app.app.test_client()
    response = client.post("/v1/images/generations", json={
        "prompt": "a fox", "model": "openai/gpt-image-1.5",
    })
    assert response.status_code == 200
    body = response.get_json()
    assert body["model"] == "cloudflare/@cf/black-forest-labs/flux-1-schnell"
    assert called["google"] is False


# ---------------------------------------------------------------------------
# Paid generator shape tests (no network) -- OpenAI / Google / OpenRouter
# ---------------------------------------------------------------------------

def test_openai_generate_image_success_b64(monkeypatch):
    class Resp:
        status_code = 200

        def json(self):
            return {"data": [{"b64_json": "b64img"}]}

    monkeypatch.setattr(app.requests, "post", lambda *a, **kw: Resp())
    status, b64, err = app._openai_generate_image({"api_keys": ["sk-x"]}, "gpt-image-1.5", "a fox")
    assert (status, b64, err) == (200, "b64img", None)


def test_openai_generate_image_falls_back_to_url_download(monkeypatch):
    class PostResp:
        status_code = 200

        def json(self):
            return {"data": [{"url": "https://example.com/img.png"}]}

    class GetResp:
        status_code = 200
        content = b"downloaded-bytes"

    monkeypatch.setattr(app.requests, "post", lambda *a, **kw: PostResp())
    monkeypatch.setattr(app.requests, "get", lambda *a, **kw: GetResp())
    monkeypatch.setattr(app, "_is_safe_external_url", lambda url: True)
    status, b64, err = app._openai_generate_image({"api_keys": ["sk-x"]}, "gpt-image-1.5", "a fox")
    assert status == 200
    assert err is None
    assert base64.b64decode(b64) == b"downloaded-bytes"


def test_openai_generate_image_no_api_key():
    status, b64, err = app._openai_generate_image({"api_keys": []}, "gpt-image-1.5", "a fox")
    assert status == 400
    assert b64 is None
    assert "no api key" in err


def test_openai_generate_image_upstream_error(monkeypatch):
    class Resp:
        status_code = 400

        def json(self):
            return {"error": {"message": "invalid prompt"}}

    monkeypatch.setattr(app.requests, "post", lambda *a, **kw: Resp())
    status, b64, err = app._openai_generate_image({"api_keys": ["sk-x"]}, "gpt-image-1.5", "a fox")
    assert status == 400
    assert b64 is None
    assert "invalid prompt" in err


def test_google_generate_image_success(monkeypatch):
    class Resp:
        status_code = 200

        def json(self):
            return {"candidates": [{"content": {"parts": [{"inlineData": {"data": "b64img"}}]}}]}

    monkeypatch.setattr(app.requests, "post", lambda *a, **kw: Resp())
    status, b64, err = app._google_generate_image(
        {"api_keys": ["key"]}, "gemini-3.1-flash-image", "a fox")
    assert (status, b64, err) == (200, "b64img", None)


def test_google_generate_image_no_image_data(monkeypatch):
    class Resp:
        status_code = 200

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": "sorry, no image"}]}}]}

    monkeypatch.setattr(app.requests, "post", lambda *a, **kw: Resp())
    status, b64, err = app._google_generate_image(
        {"api_keys": ["key"]}, "gemini-3.1-flash-image", "a fox")
    assert status == 502
    assert b64 is None
    assert "no image data" in err.lower()


def test_google_generate_image_no_api_key():
    status, b64, err = app._google_generate_image({"api_keys": []}, "gemini-3.1-flash-image", "a fox")
    assert status == 400
    assert "no api key" in err


def test_openrouter_generate_image_data_uri(monkeypatch):
    class Resp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"images": [
                {"image_url": {"url": "data:image/png;base64,ZGF0YQ=="}}]}}]}

    monkeypatch.setattr(app.requests, "post", lambda *a, **kw: Resp())
    status, b64, err = app._openrouter_generate_image(
        {"api_keys": ["sk-or-x"]}, "bytedance-seed/seedream-4.5", "a fox")
    assert (status, b64, err) == (200, "ZGF0YQ==", None)


def test_openrouter_generate_image_fetchable_url(monkeypatch):
    class PostResp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"images": [
                {"image_url": {"url": "https://example.com/x.png"}}]}}]}

    class GetResp:
        status_code = 200
        content = b"or-bytes"

    monkeypatch.setattr(app.requests, "post", lambda *a, **kw: PostResp())
    monkeypatch.setattr(app.requests, "get", lambda *a, **kw: GetResp())
    monkeypatch.setattr(app, "_is_safe_external_url", lambda url: True)
    status, b64, err = app._openrouter_generate_image(
        {"api_keys": ["sk-or-x"]}, "bytedance-seed/seedream-4.5", "a fox")
    assert status == 200
    assert base64.b64decode(b64) == b"or-bytes"


def test_openrouter_generate_image_no_images_in_response(monkeypatch):
    class Resp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"images": []}}]}

    monkeypatch.setattr(app.requests, "post", lambda *a, **kw: Resp())
    status, b64, err = app._openrouter_generate_image(
        {"api_keys": ["sk-or-x"]}, "bytedance-seed/seedream-4.5", "a fox")
    assert status == 502
    assert "no image data" in err.lower()


def test_openrouter_generate_image_no_api_key():
    status, b64, err = app._openrouter_generate_image(
        {"api_keys": []}, "bytedance-seed/seedream-4.5", "a fox")
    assert status == 400
    assert "no api key" in err


# ---------------------------------------------------------------------------
# Higgsfield generator -- async submit-then-poll, composite KEY_ID:KEY_SECRET
# credential. Mirrors the ModelScope poll test's mocking pattern: time.sleep
# is stubbed to a no-op AND the first poll response already reports a
# terminal status, so the unmocked wall-clock deadline (90s) never matters.
# ---------------------------------------------------------------------------

def test_higgsfield_generate_image_success(monkeypatch):
    monkeypatch.setattr(app.time, "sleep", lambda *_: None)
    monkeypatch.setattr(app, "_is_safe_external_url", lambda url: True)

    def fake_post(url, headers=None, json=None, timeout=None):
        assert "/v1/text2image/soul" in url

        class R:
            status_code = 200
            content = b'{"request_id": "req-1"}'

            def json(self):
                return {"request_id": "req-1"}
        return R()

    def fake_get(url, headers=None, timeout=None):
        if "/requests/req-1/status" in url:
            class R:
                status_code = 200

                def json(self):
                    return {"status": "completed", "images": [{"url": "https://x/y.png"}]}
            return R()

        class R2:
            status_code = 200
            content = b"higgs-bytes"
        return R2()

    monkeypatch.setattr(app.requests, "post", fake_post)
    monkeypatch.setattr(app.requests, "get", fake_get)
    status, b64, err = app._higgsfield_generate_image(
        {"api_keys": ["kid:ksecret"]}, "higgsfield/text2image/soul", "a fox")
    assert status == 200
    assert err is None
    assert base64.b64decode(b64) == b"higgs-bytes"


def test_higgsfield_generate_image_requires_composite_credential():
    status, b64, err = app._higgsfield_generate_image(
        {"api_keys": ["no-colon-here"]}, "higgsfield/text2image/soul", "a fox")
    assert status == 400
    assert b64 is None
    assert "KEY_ID:KEY_SECRET" in err


def test_higgsfield_generate_image_no_api_key():
    status, b64, err = app._higgsfield_generate_image(
        {"api_keys": []}, "higgsfield/text2image/soul", "a fox")
    assert status == 400
    assert b64 is None


def test_higgsfield_generate_image_task_failed(monkeypatch):
    monkeypatch.setattr(app.time, "sleep", lambda *_: None)

    def fake_post(url, headers=None, json=None, timeout=None):
        class R:
            status_code = 200
            content = b'{"request_id": "req-1"}'

            def json(self):
                return {"request_id": "req-1"}
        return R()

    def fake_get(url, headers=None, timeout=None):
        class R:
            status_code = 200

            def json(self):
                return {"status": "failed"}
        return R()

    monkeypatch.setattr(app.requests, "post", fake_post)
    monkeypatch.setattr(app.requests, "get", fake_get)
    status, b64, err = app._higgsfield_generate_image(
        {"api_keys": ["kid:ksecret"]}, "higgsfield/text2image/soul", "a fox")
    assert status == 502
    assert b64 is None
    assert "failed" in err.lower()
