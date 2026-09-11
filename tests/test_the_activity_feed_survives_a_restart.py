r"""The one page that answers "is anything actually working" was blank after
every restart.

The activity feed is a 40-entry ring in memory. The hub restarts itself every
few hours to git pull, and the feed started empty each time -- so the page you
open to check whether models are answering showed nothing, on a hub where
everything was fine.

REPORTED: "I did not see models working in /activity", on a hub that had
restarted minutes earlier. The rows were real and the models were answering;
the evidence had simply been thrown away.

It rides in the state file the quota/dead-model bridge already writes. Safe to
put on disk: a row is request METADATA -- cli, protocol, provider, model,
status, timings, the project's folder name -- with no prompt, no response and
no key in it. Checked against a live row before this was written.
"""
import time

import pytest

import app as A


def _row(**kw):
    row = {"id": 1, "protocol": "openai", "cli": "OpenCode", "source": "cli",
           "project": None, "model_req": "auto", "provider": "groq",
           "model": "qwen/qwen3.8-27b", "status": "ok", "http": 200,
           "stream": True, "started": time.time(), "finished": time.time()}
    row.update(kw)
    return row


@pytest.fixture(autouse=True)
def _clean():
    with A._activity_lock:
        A._activity.clear()
        before = A._activity_seq[0]
    yield
    with A._activity_lock:
        A._activity.clear()
        A._activity_seq[0] = before


# --------------------------------------------------------------------------- #
# It is written down
# --------------------------------------------------------------------------- #

def test_the_feed_is_in_the_state_blob():
    with A._activity_lock:
        A._activity.appendleft(_row())
    blob = A._dead_state_dump()
    assert [r["model"] for r in blob["activity"]] == ["qwen/qwen3.8-27b"]


def test_the_counter_rides_along():
    with A._activity_lock:
        A._activity_seq[0] = 412
    assert A._dead_state_dump()["activity_seq"] == 412


def test_an_empty_feed_writes_an_empty_list():
    assert A._dead_state_dump()["activity"] == []


# --------------------------------------------------------------------------- #
# And read back
# --------------------------------------------------------------------------- #

def test_it_comes_back():
    A._dead_state_load({"activity": [_row(id=7)], "activity_seq": 7})
    with A._activity_lock:
        assert [r["id"] for r in A._activity] == [7]


def test_a_full_round_trip_keeps_the_order():
    with A._activity_lock:
        for i in (1, 2, 3):
            A._activity.appendleft(_row(id=i))
    blob = A._dead_state_dump()
    A._dead_state_load(blob)
    with A._activity_lock:
        assert [r["id"] for r in A._activity] == [3, 2, 1]


def test_a_request_that_was_running_is_not_left_spinning():
    """It is never going to finish -- the process that was serving it is gone.
    Left as in_progress it shows a timer that climbs forever."""
    A._dead_state_load({"activity": [_row(id=9, status="in_progress",
                                          finished=None)]})
    with A._activity_lock:
        row = list(A._activity)[0]
    assert row["status"] == "stalled"
    assert row["finished"]


def test_ids_keep_climbing_after_a_restart():
    """The frontend tells an already-drawn row from a new one by its id, so a
    counter that restarts makes fresh requests look like old ones."""
    A._dead_state_load({"activity": [_row(id=93)], "activity_seq": 93})
    assert A._activity_seq[0] >= 93


def test_the_counter_is_believed_even_if_the_rows_disagree():
    A._dead_state_load({"activity": [_row(id=2)], "activity_seq": 500})
    assert A._activity_seq[0] == 500


def test_it_never_loads_more_than_the_ring_holds():
    A._dead_state_load({"activity": [_row(id=i) for i in range(A._ACTIVITY_MAX + 25)]})
    with A._activity_lock:
        assert len(A._activity) <= A._ACTIVITY_MAX


def test_a_state_file_from_an_older_hub_is_fine():
    """No "activity" key at all: every field in this blob is optional, and a
    missing one must leave the feed alone rather than raise."""
    with A._activity_lock:
        A._activity.appendleft(_row(id=5))
    A._dead_state_load({})
    with A._activity_lock:
        assert [r["id"] for r in A._activity] == [5]


def test_rubbish_in_the_file_is_skipped_not_fatal():
    A._dead_state_load({"activity": ["not a row", None, 7, _row(id=11)]})
    with A._activity_lock:
        assert [r["id"] for r in A._activity] == [11]


def test_a_corrupt_counter_does_not_raise():
    A._dead_state_load({"activity": [_row(id=3)], "activity_seq": "not a number"})
    assert isinstance(A._activity_seq[0], int)


# --------------------------------------------------------------------------- #
# What must NOT be on disk
# --------------------------------------------------------------------------- #

def test_a_row_carries_no_prompt_or_key():
    """The reason this is safe to persist at all. If a field ever starts
    carrying request CONTENT, this test is where that has to be noticed."""
    with A._activity_lock:
        A._activity.appendleft(_row())
    row = A._dead_state_dump()["activity"][0]
    for field in ("messages", "prompt", "input", "content", "text",
                  "api_key", "key", "authorization"):
        assert field not in row, field
