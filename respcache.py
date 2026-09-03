"""An opt-in cache of completions, so a repeated request costs no free quota.

WHY THIS AND NOT A GENERIC HTTP CACHE: the scarce resource here is not latency,
it is other people's free tiers. Every request the hub can answer from memory is
a request that does not come off a daily allowance -- and the things that repeat
are exactly the expensive ones: a /build turn re-run after a stream dropped, an
agent retrying after a tool error, the same prompt tried against auto/best/swarm,
a dashboard page reloaded mid-generation.

OFF BY DEFAULT, and that is not timidity. A cache turns "ask again" into "get the
same answer", which is right for a retry and wrong for someone hitting regenerate
hoping for something better. Since the hub cannot tell those apart from the
request alone, the choice belongs to the person running it.

WHAT IS NEVER CACHED, decided here rather than at the call site so no surface can
forget:

  * anything with a `tools` array. An agent loop that repeats a request byte for
    byte is already stuck, and replaying the identical tool call would make the
    hub a participant in the loop rather than a witness to it.
  * `n` > 1, or a `seed`: both are explicit requests about sampling.
  * a swarm/crew/team model. Those cost the most, so caching them is tempting,
    but they are a PIPELINE whose value is that it ran -- and their results are
    already recorded elsewhere.
  * an empty or error answer. Only a real completion is worth remembering.

Memory only. A restart clears it, which is the correct behaviour for something
whose whole purpose is to be free to rebuild.
"""
import hashlib
import json
import threading
import time

DEFAULT_TTL = 900          # 15 minutes: long enough for a retry, short enough
                           # that a model or catalog change is not sticky
DEFAULT_MAX = 256          # entries; completions are small, this is a few MB

_LOCK = threading.Lock()
_ENTRIES = {}              # key -> (stored_at, data)
_HITS = [0]
_MISSES = [0]

# Model ids whose answer is a pipeline, not a completion.
_UNCACHEABLE_MODELS = ("swarm", "crew", "team", "plan")


def _model_of(body):
    return str((body or {}).get("model") or "").strip().lower()


def cacheable(body):
    """Whether this request may be served from, or stored in, the cache."""
    if not isinstance(body, dict):
        return False
    if body.get("tools"):
        return False
    if body.get("seed") is not None:
        return False
    try:
        if int(body.get("n") or 1) != 1:
            return False
    except (TypeError, ValueError):
        return False
    model = _model_of(body)
    if any(model == m or model.startswith(m + ":") for m in _UNCACHEABLE_MODELS):
        return False
    if not body.get("messages"):
        return False
    return True


def key_for(body):
    """A stable fingerprint of everything that can change the answer.

    Deliberately NOT the whole body: stream is excluded because a streamed and a
    buffered request for the same thing deserve the same answer, and the
    per-request routing hints (exclusions, quality) are excluded because they
    change WHICH model answers, not what was asked. Model IS included -- 'auto'
    and a pinned id are different questions."""
    src = {
        "model": _model_of(body),
        "messages": body.get("messages"),
        "temperature": body.get("temperature"),
        "top_p": body.get("top_p"),
        "max_tokens": body.get("max_tokens"),
        "stop": body.get("stop"),
        "response_format": body.get("response_format"),
    }
    try:
        blob = json.dumps(src, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()


def _usable(data):
    """Only a real completion is worth remembering."""
    if not isinstance(data, dict) or data.get("error"):
        return False
    try:
        msg = (data.get("choices") or [{}])[0].get("message") or {}
    except (AttributeError, IndexError, TypeError):
        return False
    if msg.get("tool_calls"):
        return False           # see the module docstring: never replay these
    return bool((msg.get("content") or "").strip())


def get(body, ttl=DEFAULT_TTL):
    """The stored completion for this request, or None."""
    if not cacheable(body):
        return None
    key = key_for(body)
    if not key:
        return None
    now = time.time()
    with _LOCK:
        hit = _ENTRIES.get(key)
        if not hit:
            _MISSES[0] += 1
            return None
        stored_at, data = hit
        if now - stored_at >= ttl:   # ttl=0 means expired, not 'expires soon'
            _ENTRIES.pop(key, None)
            _MISSES[0] += 1
            return None
        # Refresh recency so a hot entry survives eviction.
        _ENTRIES[key] = (stored_at, data)
        _HITS[0] += 1
        return json.loads(json.dumps(data))     # a copy: callers mutate it


def put(body, data, max_entries=DEFAULT_MAX):
    """Remember a completion. Returns True when it was stored."""
    if not cacheable(body) or not _usable(data):
        return False
    key = key_for(body)
    if not key:
        return False
    with _LOCK:
        if len(_ENTRIES) >= max_entries and key not in _ENTRIES:
            # Drop the oldest. dicts keep insertion order, and `get` does not
            # reinsert, so this is oldest-stored rather than true LRU -- which
            # is the right axis anyway for entries that expire by age.
            oldest = next(iter(_ENTRIES), None)
            if oldest is not None:
                _ENTRIES.pop(oldest, None)
        _ENTRIES[key] = (time.time(), json.loads(json.dumps(data)))
    return True


def stats():
    with _LOCK:
        return {"entries": len(_ENTRIES), "hits": _HITS[0], "misses": _MISSES[0]}


def clear():
    with _LOCK:
        _ENTRIES.clear()
        _HITS[0] = 0
        _MISSES[0] = 0
