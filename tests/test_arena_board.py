"""LMArena's text board, once a day, free (arena.py).

Asked 2026-09-30: "is there a free API for LM Arena that updates every 24 hours
of benchmarks? if yes integrate it". VERIFIED that day: LMArena publishes its
leaderboard as a public Hugging Face dataset; config text_style_control is
the board arena.ai shows (kimi-k3-max 1488.0, glm-5.3-max 1479.6).
"""
import pytest

import arena


@pytest.mark.parametrize("name,ident", [
    ("kimi-k3-max", "kimi-k3"),
    ("moonshotai/kimi-k3", "kimi-k3"),
    ("glm-5.3-max", "glm-5-3"),
    ("z-ai/glm-5.3", "glm-5-3"),
    ("gpt-5.6-sol-xhigh", "gpt-5-6-sol"),
    ("claude-fable-5.1-max", "claude-fable-5-1"),
    ("OpenCode:claude-fable-5-1", "claude-fable-5-1"),
    ("srv_x:models/gemini-3.8-flash:free", "gemini-3-8-flash"),
    ("gemini-3.8-flash-high", "gemini-3-8-flash"),
    ("deepseek-v4-pro-high-20260813", "deepseek-v4-pro"),
    ("claude-opus-4-5-20251101-high-32k", "claude-opus-4-5"),
    ("", ""),
])
def test_board_names_and_hub_ids_meet(name, ident):
    assert arena.normalize(name) == ident


def test_the_scale_caps_under_the_owner_top_band():
    assert arena.hub_score(1509) == arena.HUB_CAP
    assert arena.hub_score(1480) == arena.HUB_CAP
    assert arena.HUB_CAP < 137.7            # under space-bunny / glm / kimi floors
    assert arena.hub_score(1300) == arena.HUB_FLOOR
    assert arena.hub_score(1200) == arena.HUB_FLOOR
    mid = arena.hub_score(1440)
    assert arena.HUB_FLOOR < mid < arena.HUB_CAP
    assert arena.hub_score(1450) > mid       # monotonic
    assert arena.hub_score("nan?") is None


def _row(name, rating, votes=5000, rank=1, cat="overall", date="2026-09-25"):
    return {"row": {"model_name": name, "rating": rating, "vote_count": votes,
                    "rank": rank, "category": cat, "leaderboard_publish_date": date}}


def test_the_best_variant_counts_and_thin_or_other_rows_do_not():
    models = arena.parse_rows([
        _row("kimi-k3-max", 1488.0, rank=16), _row("kimi-k3-low", 1440.0, rank=90),
        _row("glm-5.3-max", 1479.6, rank=24),
        _row("brand-new", 1500.0, votes=100),                  # too few votes
        _row("coding-only", 1500.0, cat="coding"),
    ])
    assert set(models) == {"kimi-k3", "glm-5-3"}
    assert models["kimi-k3"]["rating"] == 1488.0 and models["kimi-k3"]["rank"] == 16


class _Resp:
    def __init__(self, rows):
        self._rows = rows

    def raise_for_status(self):
        pass

    def json(self):
        return {"rows": self._rows}


def test_fetch_pages_until_the_overall_board_ends():
    pages = [[_row("m%d" % i, 1400.0) for i in range(100)],
             [_row("n%d" % i, 1400.0) for i in range(40)] + [_row("x", 1.0, cat="coding")]]
    calls = []

    def get(url, params=None, timeout=None):
        calls.append(params["offset"])
        return _Resp(pages[len(calls) - 1])
    rows = arena.fetch_overall(get)
    assert calls == [0, 100] and len(rows) == 140
    assert all(r["row"]["category"] == "overall" for r in rows)


def test_the_board_survives_a_restart_and_a_failed_day(tmp_path):
    path = str(tmp_path / "arena_scores.json")
    b = arena.Board(path)
    assert b.stale() and b.lookup("kimi-k3") is None
    n = b.refresh(lambda url, params=None, timeout=None:
                  _Resp([_row("kimi-k3-max", 1488.0, rank=16)]), now=1000.0)
    assert n == 1 and b.published == "2026-09-25"
    again = arena.Board(path)
    assert again.lookup("moonshotai/kimi-k3")["rating"] == 1488.0
    assert again.fetched_at == 1000.0 and again.stale(now=1000.0 + arena.REFRESH_SECONDS)
    assert again.refresh(lambda url, params=None, timeout=None: _Resp([])) == 0
    assert again.lookup("kimi-k3") is not None                 # yesterday's board kept


# --------------------------------------------------------------------------- #
# The hub side (app.py)
# --------------------------------------------------------------------------- #
import app as A


def _board_says(monkeypatch, table):
    monkeypatch.setattr(A, "_arena_entry", lambda model_id: table.get(model_id))


def test_an_unknown_model_scores_from_the_board_and_stays_under_the_cap(monkeypatch):
    before = A._benchmark_score("groq", "acme/nova-9-70b-instruct")
    _board_says(monkeypatch, {"acme/nova-9-70b-instruct": {"rating": 1500.0}})
    after = A._benchmark_score("groq", "acme/nova-9-70b-instruct")
    assert before <= 20 < 100 < after <= arena.HUB_CAP        # size/instruct/provider bonuses capped
    assert after < A._PREF_FLOORS[12] < A._PREF_FLOORS[1]      # never over space-bunny / kimi-k3


def test_a_known_model_ignores_the_board(monkeypatch):
    base = A._benchmark_score("nvidia", "moonshotai/kimi-k3")
    _board_says(monkeypatch, {"moonshotai/kimi-k3": {"rating": 1300.0}})
    assert A._benchmark_score("nvidia", "moonshotai/kimi-k3") == base


def test_an_unknown_model_the_board_lacks_is_unchanged(monkeypatch):
    before = A._benchmark_score("p", "acme/obscure-1")
    _board_says(monkeypatch, {})
    assert A._benchmark_score("p", "acme/obscure-1") == before


def test_the_board_route_and_the_tracking_rows(tmp_path, monkeypatch):
    b = arena.Board(str(tmp_path / "arena_scores.json"))
    b.models = {"kimi-k3": {"rating": 1488.0, "rank": 16, "name": "kimi-k3-max"},
                "glm-5-3": {"rating": 1479.6, "rank": 24, "name": "glm-5.3-max"}}
    b.published, b.fetched_at = "2026-09-25", 1000.0
    monkeypatch.setattr(A, "_arena_board", lambda: b)
    with A.app.test_request_context("/api/arena"):
        d = A.api_arena().get_json()
    assert d["published"] == "2026-09-25" and d["models"] == 2
    assert [m["name"] for m in d["top"]] == ["kimi-k3-max", "glm-5.3-max"]
    src = open("app.py", encoding="utf-8").read()
    assert '"arena_rating": _ar.get("rating") if _ar else None' in src
    assert "_start_arena_refresh()" in src[src.index("    _start_aa_refresh()\n"):][:200]
