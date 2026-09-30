"""The Multi tier splits WORK written in French too.

MEASURED 2026-09-30, session 47a25faa (Multi sessions): "les contours sont
pas parfaits :( peut tu focus avec tes meilleur models et ultra thinking pour
corriger ca ?" was answered directly by one session -- _MULTI_WORK_WORDS only
knew English verbs, the word regex split accented words, and the classifier
calls a short non-English ask "simple".
"""
import pytest

import app as A

_REAL_INTENT = A._multi_intent_by_model      # before conftest stubs it


@pytest.mark.parametrize("text", [
    "les contours sont pas parfaits :( peut tu focus aevc tes meilleur models "
    "et utlrra thinking pour corriger ca ?",
    "corrige le header",
    "ajoute un footer en bleu",
    "améliore la page d'accueil",
    "réécris le module de paiement",
    "continue",
])
def test_french_work_starts_a_run(text):
    assert A._multi_wants_a_swarm(text) is True, text


@pytest.mark.parametrize("text", [
    "merci !",
    "c'est bon ?",
    "pourquoi tu as changé le header ?",
    "comment tu corriges ça ?",
    "quel modèle a corrigé le bug ?",
])
def test_french_questions_and_chat_are_answered_directly(text):
    assert A._multi_wants_a_swarm(text) is False, text


@pytest.mark.parametrize("text,verdict,want", [
    ("peux-tu corriger les contours ?", "work", True),        # question-shaped work
    ("¿puedes arreglar el menú?", "work", True),
    ("هل يمكنك إصلاح القائمة؟", "work", True),
    ("merci beaucoup pour tout ça, c'est parfait", "chat", False),
])
def test_the_model_verdict_decides_in_any_language(monkeypatch, text, verdict, want):
    monkeypatch.setattr(A, "_multi_intent_by_model", lambda t: verdict)
    assert A._multi_wants_a_swarm(text) is want, text


def test_a_long_message_needs_no_verdict(monkeypatch):
    def boom(t):
        raise AssertionError("no model call for a long message")
    monkeypatch.setattr(A, "_multi_intent_by_model", boom)
    assert A._multi_wants_a_swarm("x " * 100) is True


def _fake_hops(monkeypatch, *texts):
    class R:
        status_code = 200
        def __init__(self, text): self._t = text
        def json(self): return {"choices": [{"message": {"content": self._t}}]}
        def close(self): pass
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("p", "m", "medium"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [("p", "m"), ("q", "n"), ("r", "o")])
    monkeypatch.setattr(A, "_is_sub", lambda pid: False)
    replies = iter([R(t) for t in texts])
    sent = []
    def dispatch(pid, payload, deadline):
        sent.append(pid)
        return next(replies), None
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", dispatch)
    return sent


def test_the_verdict_reads_one_word(monkeypatch):
    _fake_hops(monkeypatch, "Hmm, maybe", "work.")
    assert _REAL_INTENT("corrige les contours") == "work"


def test_the_verdict_fails_open_after_two_hops(monkeypatch):
    sent = _fake_hops(monkeypatch, "?", "no idea", "CHAT")
    assert _REAL_INTENT("hmm") is None
    assert sent == ["p", "q"]                       # never more than two hops


@pytest.mark.parametrize("text,want", [
    ("so all ok ?", False),
    ("what did you change?", False),
    ("fix the zoom", True),
    ("make it blue", True),
])
def test_english_behaviour_is_unchanged(text, want):
    assert A._multi_wants_a_swarm(text) is want, text
