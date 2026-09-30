"""A work request in another language is not "simple" (owner, 2026-09-30:
"it should work in any language"). MEASURED: a French fix request classified
simple and went to a small model."""
import pytest

import app as A


def _d(text):
    return A._classify_difficulty([{"role": "user", "content": text}])


@pytest.mark.parametrize("text", [
    "les contours sont pas parfaits :( peut tu focus aevc tes meilleur models et utlrra thinking pour corriger ca ?",
    "corrige le bug du zoom",
    "¿puedes arreglar el menú de la página?",
    "améliore la page d'accueil du site",
])
def test_work_in_other_languages_is_not_simple(text):
    assert _d(text) != "simple", text


@pytest.mark.parametrize("text", ["merci", "hi", "what is 2+2", "ok"])
def test_short_chat_stays_simple(text):
    assert _d(text) == "simple", text


def test_english_is_judged_as_before():
    assert _d("fix this bug in my code") != "simple"
    assert _d("thanks") == "simple"
