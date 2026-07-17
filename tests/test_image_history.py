import pytest

import app
import image_history


@pytest.fixture
def isolated_history(tmp_path, monkeypatch):
    path = tmp_path / "state" / "config.json"
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", str(path))
    app._runtime_active[0] = 0
    app._runtime_shutdown_thread[0] = None
    app._runtime_server[0] = None
    return path


# ---------------------------------------------------------------------------
# Flask routes: /api/images/history[/<id>]
# ---------------------------------------------------------------------------

def test_history_route_list_empty(isolated_history):
    client = app.app.test_client()
    response = client.get("/api/images/history")
    assert response.status_code == 200
    assert response.get_json() == {"images": []}


def test_history_route_serves_and_deletes(isolated_history):
    image_id = image_history.save(b"\x89PNG-fake", "a fox", "cloudflare", "flux-1-schnell")
    client = app.app.test_client()

    listed = client.get("/api/images/history").get_json()["images"]
    assert len(listed) == 1 and listed[0]["id"] == image_id

    served = client.get("/api/images/history/" + image_id)
    assert served.status_code == 200
    assert served.data == b"\x89PNG-fake"
    assert served.mimetype == "image/png"

    deleted = client.delete("/api/images/history/" + image_id,
                           headers={"X-Free-LLM-Hub": "dashboard"})
    assert deleted.status_code == 200
    assert client.get("/api/images/history/" + image_id).status_code == 404


def test_history_route_delete_requires_dashboard_header(isolated_history):
    image_id = image_history.save(b"x", "p", "cloudflare", "m")
    client = app.app.test_client()
    response = client.delete("/api/images/history/" + image_id)
    assert response.status_code == 403  # missing local control header
    assert client.get("/api/images/history/" + image_id).status_code == 200


def test_history_route_unknown_id_404s(isolated_history):
    client = app.app.test_client()
    assert client.get("/api/images/history/does-not-exist").status_code == 404
    assert client.delete("/api/images/history/does-not-exist",
                         headers={"X-Free-LLM-Hub": "dashboard"}).status_code == 404


def test_save_then_list_and_get(isolated_history):
    image_id = image_history.save(b"fake-png-bytes", "a fox", "cloudflare",
                                  "@cf/black-forest-labs/flux-1-schnell")
    assert image_id

    entries = image_history.list_entries()
    assert len(entries) == 1
    assert entries[0]["id"] == image_id
    assert entries[0]["prompt"] == "a fox"
    assert entries[0]["provider"] == "cloudflare"
    assert "filename" not in entries[0]  # metadata-only, no internal path leak

    raw, mime = image_history.get_file(image_id)
    assert raw == b"fake-png-bytes"
    assert mime == "image/png"


def test_get_file_missing_id_returns_none(isolated_history):
    raw, mime = image_history.get_file("does-not-exist")
    assert raw is None and mime is None


def test_delete_removes_entry_and_file(isolated_history):
    image_id = image_history.save(b"bytes", "p", "pollinations", "flux")
    assert image_history.delete(image_id) is True
    assert image_history.list_entries() == []
    assert image_history.get_file(image_id) == (None, None)


def test_delete_unknown_id_returns_false(isolated_history):
    assert image_history.delete("nope") is False


def test_newest_first_order(isolated_history):
    first = image_history.save(b"1", "p1", "cloudflare", "m1")
    second = image_history.save(b"2", "p2", "cloudflare", "m1")
    entries = image_history.list_entries()
    assert [e["id"] for e in entries] == [second, first]


def test_max_entries_prunes_oldest(isolated_history, monkeypatch):
    monkeypatch.setattr(image_history, "MAX_ENTRIES", 3)
    ids = [image_history.save(("img%d" % i).encode(), "p", "cloudflare", "m1")
           for i in range(5)]
    entries = image_history.list_entries(limit=100)
    assert len(entries) == 3
    # the 3 most recently saved survive; the oldest 2 are pruned (file + index)
    assert [e["id"] for e in entries] == list(reversed(ids[-3:]))
    assert image_history.get_file(ids[0]) == (None, None)


def test_save_never_raises_on_bad_input(isolated_history):
    # None prompt/provider/model must not crash the caller -- a history bug
    # must never break a real image-generation response.
    image_id = image_history.save(b"x", None, None, None)
    assert image_id  # still saves with sanitized/empty metadata
