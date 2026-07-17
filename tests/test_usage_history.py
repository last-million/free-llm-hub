import time

import pytest

import app
import usage_history


@pytest.fixture
def isolated_usage(tmp_path, monkeypatch):
    path = tmp_path / "state" / "config.json"
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", str(path))
    return path


def test_record_and_get_day_round_trip(isolated_usage):
    usage_history.record("groq", "llama-3.3-70b-versatile", 100, 50, estimated=False)
    usage_history.record("groq", "llama-3.3-70b-versatile", 40, 10, estimated=True)
    usage_history.record("cerebras", "gpt-oss-120b", 200, 0, estimated=False)

    day = usage_history.get_day()
    assert day["date"] == time.strftime("%Y-%m-%d", time.gmtime())
    assert day["total_tokens"] == (100 + 50) + (40 + 10) + 200

    by_id = {m["id"]: m for m in day["models"]}
    groq_row = by_id["groq/llama-3.3-70b-versatile"]
    assert groq_row["provider"] == "groq"
    assert groq_row["model"] == "llama-3.3-70b-versatile"
    assert groq_row["prompt_tokens"] == 140
    assert groq_row["completion_tokens"] == 60
    assert groq_row["total_tokens"] == 200
    assert groq_row["requests"] == 2
    assert groq_row["estimated_requests"] == 1

    cerebras_row = by_id["cerebras/gpt-oss-120b"]
    assert cerebras_row["total_tokens"] == 200
    assert cerebras_row["requests"] == 1
    assert cerebras_row["estimated_requests"] == 0

    # sorted descending by total_tokens
    assert day["models"][0]["id"] == "groq/llama-3.3-70b-versatile"


def test_get_day_empty_when_no_data(isolated_usage):
    day = usage_history.get_day("2020-01-01")
    assert day == {"date": "2020-01-01", "total_tokens": 0, "models": []}


def test_record_ignores_missing_pid_or_model(isolated_usage):
    usage_history.record(None, "model-x", 10, 10)
    usage_history.record("pid-x", None, 10, 10)
    usage_history.record("", "model-x", 10, 10)
    day = usage_history.get_day()
    assert day["models"] == []


def test_record_never_raises_on_bad_input(isolated_usage):
    # A non-numeric token count must not raise -- caller's request must never
    # be broken by a usage-tracking bug (record() swallows the error, at the
    # cost of that one call not being persisted -- still better than a crash).
    usage_history.record("groq", "m1", prompt_tokens=-5, completion_tokens="oops")
    day = usage_history.get_day()  # must not raise either
    assert isinstance(day["models"], list)

    # A negative-but-numeric count IS recorded, clamped to zero.
    usage_history.record("groq", "m1", prompt_tokens=-5, completion_tokens=0)
    row = usage_history.get_day()["models"][0]
    assert row["prompt_tokens"] == 0
    assert row["requests"] == 1


def test_recent_days_sorted_newest_first(isolated_usage):
    data = {
        "2026-07-01": {"groq/m1": {"prompt_tokens": 1, "completion_tokens": 1,
                                   "requests": 1, "estimated_requests": 0}},
        "2026-07-15": {"groq/m1": {"prompt_tokens": 1, "completion_tokens": 1,
                                   "requests": 1, "estimated_requests": 0}},
        "2026-07-10": {"groq/m1": {"prompt_tokens": 1, "completion_tokens": 1,
                                   "requests": 1, "estimated_requests": 0}},
    }
    usage_history._save(data)
    assert usage_history.recent_days() == ["2026-07-15", "2026-07-10", "2026-07-01"]
    assert usage_history.recent_days(limit=2) == ["2026-07-15", "2026-07-10"]


def test_prune_drops_entries_older_than_retention(isolated_usage):
    old_day = time.strftime("%Y-%m-%d", time.gmtime(time.time() - (usage_history.RETENTION_DAYS + 5) * 86400))
    recent_day = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400))
    data = {
        old_day: {"groq/m1": {"prompt_tokens": 1, "completion_tokens": 1,
                              "requests": 1, "estimated_requests": 0}},
        recent_day: {"groq/m1": {"prompt_tokens": 1, "completion_tokens": 1,
                                 "requests": 1, "estimated_requests": 0}},
    }
    usage_history._save(data)
    # record() prunes on every write -- trigger one for a 3rd, unrelated day.
    usage_history.record("groq", "m2", 1, 1)
    on_disk = usage_history._load()
    assert old_day not in on_disk
    assert recent_day in on_disk


def test_prune_drops_malformed_day_keys(isolated_usage):
    data = {"not-a-date": {"groq/m1": {"prompt_tokens": 1, "completion_tokens": 1,
                                       "requests": 1, "estimated_requests": 0}}}
    usage_history._save(data)
    usage_history.record("groq", "m2", 1, 1)
    on_disk = usage_history._load()
    assert "not-a-date" not in on_disk


# ---------------------------------------------------------------------------
# /api/usage -- the thin Flask route wrapping get_day()/recent_days()
# ---------------------------------------------------------------------------

def test_api_usage_default_day_shape(isolated_usage):
    usage_history.record("groq", "llama-3.3-70b-versatile", 100, 50)
    client = app.app.test_client()
    resp = client.get("/api/usage")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["date"] == time.strftime("%Y-%m-%d", time.gmtime())
    assert body["total_tokens"] == 150
    assert body["models"][0]["id"] == "groq/llama-3.3-70b-versatile"
    assert body["available_days"] == usage_history.recent_days()


def test_api_usage_explicit_date_param(isolated_usage):
    client = app.app.test_client()
    resp = client.get("/api/usage?date=2020-01-01")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["date"] == "2020-01-01"
    assert body["total_tokens"] == 0
    assert body["models"] == []


def test_api_usage_rejects_malformed_date(isolated_usage):
    client = app.app.test_client()
    resp = client.get("/api/usage?date=07-16-2026")
    assert resp.status_code == 400
    assert "error" in resp.get_json()
