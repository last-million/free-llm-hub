r"""A popup that said the same thing forever, on a schedule nobody asked for.

REPORTED 2026-09-09: "the pop up should be perfect UI UX and show the new
things well presented and pro and 0 AI SLOP and show the pop up only one time
after each 4 houres only please car ca derange".

TWO FAULTS.

IT WAS STALE BY CONSTRUCTION. The notes were a hardcoded paragraph inside the
template, so it described whatever was new on the day someone last edited that
paragraph -- months later it was still announcing an August provider change
("g4f.space also ended anonymous access on 2026-08-06") as news, to people
installing in September. The hub updates itself by `git pull`, which means the
commit log IS the release note and is never stale by definition.

IT INTERRUPTED. It fired whenever the running version differed from the last
one seen, and the hub pulls every five hours -- so it reappeared on a cadence
the reader never chose, mid-work. Now the version check is joined by a
four-hour floor.

The timestamp is written when the modal OPENS, not only when "Got it" is
clicked: a modal dismissed with Escape or the X is still a modal that was
shown, and stamping only on the button meant a dismissed popup came straight
back on the next page load.
"""
import json
import re

import pytest

import app as A


SRC = open("templates/index.html", encoding="utf-8").read()


@pytest.fixture()
def client():
    A.app.config["TESTING"] = True
    return A.app.test_client()


# --------------------------------------------------------------------------- #
# The notes are real
# --------------------------------------------------------------------------- #

def test_the_endpoint_reports_the_running_version(client):
    d = client.get("/api/release-notes",
                   headers={"X-Free-LLM-Hub-Token": A.config.get_setting("control_token") or ""}
                   ).get_json()
    assert d["version"] == A._HUB_VERSION
    assert d["release"] == A.HUB_RELEASE


def test_each_note_carries_a_date_a_hash_and_a_subject(monkeypatch):
    A._RELEASE_NOTES_CACHE["rows"] = None
    monkeypatch.setattr(A, "_is_git_repo", lambda: True)
    monkeypatch.setattr(A, "_git", lambda *a, **k: (
        0, "abc1234\x1f2026-09-09\x1fa real subject\x1e"
           "def5678\x1f2026-09-08\x1fanother one\x1e", ""))
    with A.app.test_request_context("/api/release-notes"):
        rows = A.api_release_notes().get_json()["notes"]
    assert rows == [{"hash": "abc1234", "date": "2026-09-09", "subject": "a real subject"},
                    {"hash": "def5678", "date": "2026-09-08", "subject": "another one"}]


def test_a_subject_containing_the_delimiter_is_not_a_problem(monkeypatch):
    """Commit subjects are written by people and can contain anything. The
    separators are the ASCII unit/record characters precisely because every
    printable delimiter is something someone will eventually type."""
    A._RELEASE_NOTES_CACHE["rows"] = None
    monkeypatch.setattr(A, "_is_git_repo", lambda: True)
    monkeypatch.setattr(A, "_git", lambda *a, **k: (
        0, "abc1234\x1f2026-09-09\x1ffix: a|b, c;d -- and \"quotes\"\x1e", ""))
    with A.app.test_request_context("/api/release-notes"):
        rows = A.api_release_notes().get_json()["notes"]
    assert rows[0]["subject"] == 'fix: a|b, c;d -- and "quotes"'


def test_a_zip_install_gets_an_empty_list_not_an_error(monkeypatch):
    """No git checkout means no log. The popup degrades to just the version
    rather than breaking."""
    A._RELEASE_NOTES_CACHE["rows"] = None
    monkeypatch.setattr(A, "_is_git_repo", lambda: False)
    with A.app.test_request_context("/api/release-notes"):
        d = A.api_release_notes().get_json()
    assert d["notes"] == [] and d["version"]


def test_a_failing_git_is_not_an_error_either(monkeypatch):
    A._RELEASE_NOTES_CACHE["rows"] = None
    monkeypatch.setattr(A, "_is_git_repo", lambda: True)
    monkeypatch.setattr(A, "_git", lambda *a, **k: (128, "", "not a repository"))
    with A.app.test_request_context("/api/release-notes"):
        assert A.api_release_notes().get_json()["notes"] == []


def test_the_log_is_not_shelled_out_to_on_every_open(monkeypatch):
    """The hub pulls every five hours; reading the log per popup is waste."""
    A._RELEASE_NOTES_CACHE["rows"] = None
    calls = {"n": 0}
    monkeypatch.setattr(A, "_is_git_repo", lambda: True)

    def counted(*a, **k):
        calls["n"] += 1
        return (0, "h\x1f2026-01-01\x1fs\x1e", "")

    monkeypatch.setattr(A, "_git", counted)
    with A.app.test_request_context("/api/release-notes"):
        A.api_release_notes()
        A.api_release_notes()
        A.api_release_notes()
    assert calls["n"] == 1


# --------------------------------------------------------------------------- #
# It does not interrupt
# --------------------------------------------------------------------------- #

def test_there_is_a_four_hour_floor():
    m = re.search(r"WHATS_NEW_MIN_GAP_MS\s*=\s*([^;]+);", SRC)
    assert m, "no minimum gap defined"
    assert "4 * 60 * 60 * 1000" in m.group(1)


def test_the_gap_is_checked_before_anything_is_shown():
    i = SRC.index("function checkWhatsNew(")
    body = SRC[i:SRC.index("function showWhatsNew(")]
    assert "flh.whatsNewAt" in body
    assert "WHATS_NEW_MIN_GAP_MS" in body
    assert body.index("WHATS_NEW_MIN_GAP_MS") < body.index("showWhatsNew(")


def test_the_version_check_still_applies():
    """The floor is IN ADDITION to "only when something changed" -- a timer on
    its own would pop up every four hours forever."""
    i = SRC.index("function checkWhatsNew(")
    body = SRC[i:SRC.index("function showWhatsNew(")]
    assert "seen === v" in body


def test_opening_it_counts_as_showing_it():
    """Escape and the X close the modal without touching Got it. Stamping only
    on the button brought it straight back on the next load."""
    i = SRC.index("function showWhatsNew(")
    body = SRC[i:i + 3500]
    assert body.count("flh.whatsNewAt") >= 2, "stamped on dismiss only"


def test_the_first_run_is_still_silent():
    """It must not stack on top of the welcome modal."""
    i = SRC.index("function checkWhatsNew(")
    body = SRC[i:SRC.index("function showWhatsNew(")]
    assert "cx_welcome_seen_v1" in body


# --------------------------------------------------------------------------- #
# ...and it reads like a release note
# --------------------------------------------------------------------------- #

def test_the_notes_are_rendered_as_a_list_with_dates():
    assert "wn-list" in SRC and "wn-date" in SRC and "wn-sub" in SRC


def test_the_hardcoded_paragraph_is_gone():
    """The specific thing that was still being announced months later. Scoped
    to the popup: the same provider note legitimately appears elsewhere as a
    code comment explaining why g4f needs a key."""
    i = SRC.index("function showWhatsNew(")
    body = SRC[i:i + 3000]
    assert "No cake credits" not in body
    assert "g4f.dev/members.html" not in body
    assert "pollinations, aihorde, uncloseai" not in body


def test_the_subjects_are_escaped():
    """A commit subject is text from the repo, not markup."""
    i = SRC.index("function showWhatsNew(")
    body = SRC[i:i + 3000]
    assert "esc(n.subject)" in body
    assert "esc(n.date)" in body


def test_there_is_an_empty_state():
    assert "wn-empty" in SRC


def test_it_uses_classes_not_inline_styles():
    """The old body was built from style="margin:0 0 10px" strings, which is
    why it could not be themed and did not match anything around it."""
    i = SRC.index("function showWhatsNew(")
    body = SRC[i:i + 3000]
    assert 'style="margin' not in body


# --------------------------------------------------------------------------- #
# Settings layout
# --------------------------------------------------------------------------- #

def test_settings_groups_are_cards():
    i = SRC.index(".sd-group{")
    rule = SRC[i:SRC.index("}", i)]
    assert "border-radius" in rule and "background:var(--surface)" in rule


def test_settings_is_a_responsive_grid():
    assert ".sd-grid{" in SRC
    assert "@media (min-width:900px)" in SRC
    assert 'class="sd-grid"' in SRC


def test_the_data_heavy_groups_span_the_full_width():
    assert ".sd-wide{grid-column:1 / -1}" in SRC.replace(" > ", " > ") or \
           "sd-wide{grid-column:1 / -1}" in SRC
    assert SRC.count('class="sd-group sd-wide"') == 2


def test_there_is_a_small_screen_layout():
    assert "@media (max-width:600px)" in SRC
