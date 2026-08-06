"""Guided key setup: an ORDERED LIST OF LINKS for the user to click through.

USER 2026-08-06 asked whether the hub could instead auto-create the accounts
with Playwright and harvest the keys. Rejected on the merits, and that
rejection is what this endpoint's shape encodes: automating signups breaks
essentially every provider's terms, gets keys revoked, breaks on CAPTCHA /
phone / KYC anyway, and risks the Google account driving it. So the hub does
the one honest thing it can -- put the best providers in the best order, link
straight to their own signup pages, and be upfront about which ones cost real
human effort.

The endpoint therefore only ever RETURNS DATA. It performs no outbound request
to any provider, and there is no code path here that could.
"""
import os
import shutil
import tempfile

import pytest

import app
import config


@pytest.fixture
def state_dir():
    d = tempfile.mkdtemp(prefix="hub-pytest-onboarding-")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def isolated_config(state_dir, monkeypatch):
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", os.path.join(state_dir, "state", "config.json"))


def _get(client):
    token = config.get_control_token()
    headers = {"X-Free-LLM-Hub-Token": token} if token else {}
    return client.get("/api/onboarding", headers=headers).get_json()


def test_lists_unconnected_providers_best_first(isolated_config):
    d = _get(app.app.test_client())
    steps = d["steps"]
    assert steps and d["remaining"] == len(steps)
    frictionless = [s for s in steps if not s["friction"]]
    assert len(frictionless) >= 2
    # Best-first WITHIN the frictionless group (the whole point of an order).
    scores = [s["quality_score"] for s in frictionless]
    assert scores == sorted(scores, reverse=True)


def test_friction_providers_are_sorted_last_and_explained(isolated_config):
    steps = _get(app.app.test_client())["steps"]
    idx = [i for i, s in enumerate(steps) if s["friction"]]
    clean = [i for i, s in enumerate(steps) if not s["friction"]]
    assert idx and clean
    assert min(idx) > max(clean), "phone/KYC/Telegram signups must come last"
    for s in steps:
        if s["friction"]:
            assert len(s["friction"]) > 10, "a friction flag must say WHAT the friction is"


def test_telegram_provider_is_flagged_not_silently_dropped(isolated_config):
    """The user specifically called this case out. It must still be offered --
    just labelled -- rather than hidden, so the choice stays theirs."""
    steps = _get(app.app.test_client())["steps"]
    nara = next((s for s in steps if s["id"] == "nararouter"), None)
    assert nara is not None
    assert "Telegram" in nara["friction"]


def test_every_step_has_a_real_signup_url(isolated_config):
    for s in _get(app.app.test_client())["steps"]:
        assert s["signup_url"].startswith("http"), s["id"]


def test_keyless_providers_are_not_listed(isolated_config):
    """Nothing to set up -- listing them would be busywork."""
    ids = {s["id"] for s in _get(app.app.test_client())["steps"]}
    import providers as prov
    for p in prov.list_providers():
        if p.get("no_key"):
            assert p["id"] not in ids


def test_a_connected_provider_drops_off_the_list(isolated_config):
    client = app.app.test_client()
    before = {s["id"] for s in _get(client)["steps"]}
    target = "groq" if "groq" in before else sorted(before)[0]
    config.add_provider_key(target, "test-key-not-real")
    after = {s["id"] for s in _get(client)["steps"]}
    assert target not in after
    assert len(after) == len(before) - 1
