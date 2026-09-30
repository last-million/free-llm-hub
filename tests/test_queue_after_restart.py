"""Queued messages that waited through a reload / hub restart are shown and
released by the owner, never sent on their own (owner, 2026-09-30: "see the
queued messages and select if I want to use them to continue").
"""
HTML = open("templates/index.html", encoding="utf-8").read()


def _fn(name):
    body = HTML[HTML.index("function %s(" % name):]
    return body[:body.index("\n    function ", 10)]


def test_a_restored_queue_starts_paused():
    assert "_queueRestored = _queuePaused = _queue.length > 0;" in _fn("queueLoad")


def test_a_finished_turn_does_not_send_a_restored_queue():
    assert "if (failed || stopped || _queueRestored){" in _fn("queueAfterTurn")


def test_the_owner_can_send_them_all_or_clear_them():
    render = _fn("queueRender")
    assert "'Send them, in order'" in render and "'Clear all'" in render
    assert "queueForget(sessionId)" in render


def test_the_interrupted_bar_mentions_them():
    assert "queued message' + (_queue.length === 1" in HTML
