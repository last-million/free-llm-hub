"""Rendering an agent-produced page without wrecking the dashboard console.

An artifact used to go into a sandboxed iframe via `srcdoc`. Safe, but a srcdoc
iframe INHERITS the embedder's Content-Security-Policy — and the dashboard runs
`default-src 'none'`. So every webfont, stylesheet and image a generated page
referenced was refused: one page produced dozens of

    Refused to load the font '<URL>' ... "default-src 'none'". Note that
    'font-src' was not explicitly set, so 'default-src' is used as a fallback.

plus hundreds of failed image requests, all in the PARENT's console.

Served from its own URL it is a separate document with its own policy, so it
renders as a browser really would — while the dashboard keeps its strict CSP.
"""
import pytest

import app


@pytest.fixture
def client():
    app.app.config["TESTING"] = True
    with app.app.test_client() as c:
        yield c


def _auth():
    import config
    return {"X-Free-LLM-Hub": "dashboard",
            "X-Free-LLM-Hub-Token": config.ensure_control_token()}


PAGE = ('<!doctype html><html><head>'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Mulish">'
        '</head><body><img src="https://via.placeholder.com/300x200"></body></html>')


def _put(client):
    r = client.post("/api/artifact", json={"html": PAGE}, headers=_auth())
    if r.status_code == 403:
        pytest.skip("agentic chat master flag is off on this machine")
    assert r.status_code == 200
    return r.get_json()["url"]


def test_the_dashboard_keeps_its_strict_policy(client):
    csp = client.get("/health").headers.get("Content-Security-Policy", "")
    assert "default-src 'none'" in csp, "the dashboard must stay locked down"


def test_an_artifact_gets_its_own_policy(client):
    url = _put(client)
    csp = client.get(url).headers.get("Content-Security-Policy", "")
    assert "default-src 'none'" not in csp
    assert "font-src" in csp, "webfonts were the thing being refused"
    assert "img-src" in csp


def test_an_artifact_is_still_framed_only_by_us(client):
    """Permissive about what it may LOAD, strict about what it may reach."""
    csp = client.get(_put(client)).headers.get("Content-Security-Policy", "")
    assert "frame-ancestors 'self'" in csp
    assert "form-action 'none'" in csp


def test_the_artifact_route_is_embeddable(client):
    """X-Frame-Options: DENY from the global handler would stop the preview
    panel embedding it at all."""
    h = client.get(_put(client)).headers
    assert h.get("X-Frame-Options") != "DENY"
    assert h.get("X-Content-Type-Options") == "nosniff"


def test_the_page_is_served_verbatim(client):
    body = client.get(_put(client)).get_data(as_text=True)
    assert "fonts.googleapis.com" in body


def test_an_unknown_artifact_404s(client):
    assert client.get("/artifact/deadbeef").status_code == 404


def test_html_is_required(client):
    r = client.post("/api/artifact", json={}, headers=_auth())
    assert r.status_code in (400, 403)


def test_only_a_few_artifacts_are_kept(client):
    """A preview panel, not a store."""
    if client.post("/api/artifact", json={"html": "<p>x</p>"},
                   headers=_auth()).status_code == 403:
        pytest.skip("agentic chat master flag is off on this machine")
    for _ in range(app._ARTIFACT_KEEP + 6):
        client.post("/api/artifact", json={"html": "<p>x</p>"}, headers=_auth())
    assert len(app._ARTIFACTS) <= app._ARTIFACT_KEEP + 1


def test_dashboard_is_never_served_from_the_browser_cache(client):
    """The dashboard ships its own JavaScript inline, so a cached copy of the
    page is a cached copy of the CODE. That produced console errors from a
    code path deleted hours earlier -- the file on disk said one thing, the
    running tab did another. Restarting the hub has to mean the next load runs
    the current build."""
    cc = client.get("/").headers.get("Cache-Control", "")
    assert "no-store" in cc, "dashboard is cacheable: a stale tab runs stale JS"
