"""LMArena text leaderboard, fetched once a day, free and keyless.

Source: LMArena's own public Hugging Face dataset
(huggingface.co/datasets/lmarena-ai/leaderboard-dataset), read through the
Hugging Face datasets-server rows API. Config ``text_style_control`` is the
board arena.ai shows by default (VERIFIED 2026-09-30: claude-opus-5.5-high
1508.6, kimi-k3-max 1488.0, gpt-5.5-high 1481.4, glm-5.3-max 1479.6 -- the
website's numbers; the plain ``text`` config differs by ~10 points).

What the hub does with it (app.py): a model the hub's own tables do not know
-- a new release, a stealth model -- gets a score from its Arena rating
(``hub_score``) instead of the unknown-family 10, CAPPED under the owner's
top band so a newcomer lands among the strong models but never overrides a
ranking the owner chose. Known models are untouched. /api/tracking shows the
rating and rank next to every model.

Pure except ``fetch_overall`` (network) and the cache helpers (disk).
"""
import functools
import json
import os
import re
import tempfile
import time

ROWS_URL = "https://datasets-server.huggingface.co/rows"
DATASET = "lmarena-ai/leaderboard-dataset"
CONFIG = "text_style_control"
PAGE = 100                      # the rows API's maximum page size
MAX_PAGES = 12                  # ~400 models on the overall board; hard stop
REFRESH_SECONDS = 24 * 3600
CACHE_NAME = "arena_scores.json"

# Arena rating -> hub score. The hub's strong band is 133-138, and every model
# there is placed by the owner or by two boards (see AGENTS.md "Static ranking
# rebench"). An Arena-scored newcomer is capped at HUB_CAP -- among the strong
# free models (glm-5.3-flash 133.6, deepseek-v4 134), under every owner floor
# above them -- and scales linearly below it. Anchors from the 2026-09-25 board:
# deepseek-v4.1-flash 1477 / glm-5.3-flash 1474 (hub 133.6-134), minimax-m3
# 1440 (hub 133 by owner floor, ~120 by strength), claude-haiku-4.5 1414.
HUB_CAP = 134.5
TOP_RATING = 1480.0             # at or above: HUB_CAP
FLOOR_RATING = 1300.0           # at or below: HUB_FLOOR
HUB_FLOOR = 60.0
MIN_VOTES = 500                 # fewer votes than this is too noisy to route on

# Variant words the board appends to a model's name: effort levels, thinking
# modes, dates. The hub serves the model, not a board-specific effort setting.
_VARIANT_RE = re.compile(
    r"(?:[-_ ](?:max|xhigh|high|medium|low|minimal|thinking|reasoning|nothinking|"
    r"non-thinking|instruct|chat|latest|preview|exp|experimental|beta|"
    r"\d+k|20\d{6}|\d{4}))+$")
_PREFIX_RE = re.compile(r"^(?:[a-z0-9_.-]+/)+")          # vendor/ or models/


@functools.lru_cache(maxsize=8192)
def normalize(name):
    """A comparable identity for a board name or a hub model id:
    'moonshotai/Kimi-K3-Max' and 'kimi-k3-max' -> 'kimi-k3';
    'srv_x:models/gemini-3.8-flash:free' -> 'gemini-3.8-flash'."""
    s = str(name or "").strip().lower()
    if not s:
        return ""
    if ":" in s:                                         # relay prefix / :free
        parts = [p for p in s.split(":") if p and p not in ("free", "beta", "extended")]
        s = max(parts, key=len) if parts else ""
    s = _PREFIX_RE.sub("", s)
    s = s.replace("_", "-").replace(" ", "-")
    s = re.sub(r"(?<=\d)\.(?=\d)", "-", s)              # 5.1 and 5-1 are one version
    prev = None
    while prev != s:
        prev = s
        s = _VARIANT_RE.sub("", s)
    return s.strip("-.")


def hub_score(rating):
    """Map an Arena rating onto the hub's scale (see HUB_CAP)."""
    try:
        r = float(rating)
    except (TypeError, ValueError):
        return None
    if r >= TOP_RATING:
        return HUB_CAP
    if r <= FLOOR_RATING:
        return HUB_FLOOR
    return round(HUB_FLOOR + (r - FLOOR_RATING) * (HUB_CAP - HUB_FLOOR)
                 / (TOP_RATING - FLOOR_RATING), 2)


def parse_rows(rows):
    """{identity: {rating, rank, votes, name, published}} from rows of the
    overall board -- the best-rated variant per identity (a model's strongest
    published setting), skipping thin-vote rows."""
    out = {}
    for row in rows or ():
        r = row.get("row", row) if isinstance(row, dict) else None
        if not isinstance(r, dict) or r.get("category") != "overall":
            continue
        try:
            rating = float(r.get("rating"))
            votes = float(r.get("vote_count") or 0)
        except (TypeError, ValueError):
            continue
        if votes < MIN_VOTES:
            continue
        ident = normalize(r.get("model_name"))
        if not ident:
            continue
        cur = out.get(ident)
        if cur is None or rating > cur["rating"]:
            out[ident] = {"rating": round(rating, 1), "rank": int(r.get("rank") or 0),
                          "votes": int(votes), "name": r.get("model_name"),
                          "published": r.get("leaderboard_publish_date")}
    return out


def fetch_overall(get, timeout=30):
    """Every row of the latest overall board. `get` is requests.get (injected,
    so tests never touch the network). Stops at the first page that is empty
    or leaves the overall category. Raises on a transport error."""
    rows = []
    for page in range(MAX_PAGES):
        resp = get(ROWS_URL, params={"dataset": DATASET, "config": CONFIG,
                                     "split": "latest", "offset": page * PAGE,
                                     "length": PAGE}, timeout=timeout)
        resp.raise_for_status()
        batch = (resp.json() or {}).get("rows") or []
        overall = [b for b in batch if (b.get("row") or {}).get("category") == "overall"]
        rows.extend(overall)
        if len(batch) < PAGE or len(overall) < len(batch):
            break
    return rows


class Board:
    """The day's board, kept in memory and in ``<state dir>/arena_scores.json``."""

    def __init__(self, path):
        self.path = path
        self.models = {}
        self.fetched_at = 0.0
        self.published = None
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and isinstance(data.get("models"), dict):
                self.models = data["models"]
                self.fetched_at = float(data.get("fetched_at") or 0.0)
                self.published = data.get("published")
        except (OSError, ValueError, TypeError):
            pass

    def save(self):
        parent = os.path.dirname(self.path) or "."
        os.makedirs(parent, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".arena-", suffix=".tmp", dir=parent)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"fetched_at": self.fetched_at, "published": self.published,
                       "models": self.models}, fh, ensure_ascii=False)
        os.replace(tmp, self.path)

    def stale(self, now=None):
        return ((now or time.time()) - self.fetched_at) >= REFRESH_SECONDS

    def refresh(self, get, now=None):
        """Fetch and keep the board; returns how many models it has. A failed or
        empty fetch keeps yesterday's board (never a blank one)."""
        models = parse_rows(fetch_overall(get))
        if not models:
            return 0
        self.models = models
        self.fetched_at = now or time.time()
        dates = sorted({m.get("published") for m in models.values() if m.get("published")})
        self.published = dates[-1] if dates else None
        self.save()
        return len(models)

    def lookup(self, model_id):
        """The board entry for a hub model id, or None. Exact identity match
        only: a wrong match would route on a confident, wrong number."""
        return self.models.get(normalize(model_id)) if self.models else None
