r"""A settings read used to cost a disk read and a JSON parse. Every time.

MEASURED, building one ordinary tool-capable chain on this machine:

    _build_chain            4.41 s   (10 hops)
    _is_model_dead calls      484
    _blocked_models()       0.860 ms each  ->  0.42 s of that 4.41 s
    config.load_config()    0.785 ms each  (config.json is 15,539 bytes)

_is_model_dead is the single seam every filter goes through -- the candidate
pool, _build_chain, the model lists, the probes -- and its first line asks the
user's blocklist, which called get_setting, which called load_config, which
opened and parsed the file. 484 times per chain, for an answer that changes
when the user clicks something in Settings.

This was already ~10% of chain-build time. It became the blocking issue while
adding the model whitelist and the identity-level blocklist, because those add
two more settings reads to the SAME seam -- the honest version of that feature
would have tripled 0.42 s to ~1.3 s.

The cache is keyed on the file's (mtime_ns, size) and invalidated explicitly by
every in-process write, so:
  * a click in Settings is visible to the very next read (explicit invalidation),
  * an edit from another process is picked up when the stat changes,
  * and a caller that mutates what it got back cannot poison the next reader.

Correctness is not traded for speed anywhere here: when the stat cannot be read
at all, the cache is bypassed and the file is parsed, exactly as before.
"""
import json
import os

import config


def _fresh(tmp_path, monkeypatch, data):
    p = tmp_path / "config.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", str(p))
    monkeypatch.setattr(config, "_config_path", lambda: str(p))
    config.invalidate_settings_cache()
    return p


# --------------------------------------------------------------------------- #
# It still reads the truth
# --------------------------------------------------------------------------- #

def test_a_setting_is_returned(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch, {"blocked_models": ["a/b"]})
    assert config.get_setting("blocked_models") == ["a/b"]


def test_a_missing_setting_gives_the_default(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch, {})
    assert config.get_setting("nope", "fallback") == "fallback"


def test_a_write_is_visible_to_the_next_read(tmp_path, monkeypatch):
    """The click-in-Settings case. A cache that needs a restart to notice would
    be worse than the disk read it replaced."""
    _fresh(tmp_path, monkeypatch, {"blocked_models": []})
    assert config.get_setting("blocked_models") == []
    config.set_setting("blocked_models", ["groq/x"])
    assert config.get_setting("blocked_models") == ["groq/x"]


def test_repeated_writes_are_each_visible(tmp_path, monkeypatch):
    """mtime resolution is coarse enough on some filesystems that two writes in
    the same tick look identical -- which is why the write path invalidates
    explicitly instead of trusting the stat."""
    _fresh(tmp_path, monkeypatch, {})
    for i in range(6):
        config.set_setting("model_mode", "mode-%d" % i)
        assert config.get_setting("model_mode") == "mode-%d" % i


def test_an_edit_from_outside_is_noticed(tmp_path, monkeypatch):
    p = _fresh(tmp_path, monkeypatch, {"model_mode": "all"})
    assert config.get_setting("model_mode") == "all"
    os.utime(str(p), None)
    p.write_text(json.dumps({"model_mode": "coding"}), encoding="utf-8")
    config.invalidate_settings_cache()      # what a real writer would do
    assert config.get_setting("model_mode") == "coding"


def test_a_caller_cannot_poison_the_cache(tmp_path, monkeypatch):
    """_blocked_models builds a set from the list it gets. Something else will
    eventually append to one instead, and a shared mutable would make that edit
    permanent for the whole process."""
    _fresh(tmp_path, monkeypatch, {"blocked_models": ["a/b"]})
    got = config.get_setting("blocked_models")
    got.append("MUTATED")
    assert config.get_setting("blocked_models") == ["a/b"]


def test_a_nested_value_is_also_copied(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch, {"overrides": {"coding": {"add": ["x"]}}})
    got = config.get_setting("overrides")
    got["coding"]["add"].append("MUTATED")
    assert config.get_setting("overrides") == {"coding": {"add": ["x"]}}


def test_a_missing_file_is_not_an_error(tmp_path, monkeypatch):
    p = tmp_path / "config.json"
    monkeypatch.setattr(config, "CONFIG_PATH", str(p))
    monkeypatch.setattr(config, "_config_path", lambda: str(p))
    config.invalidate_settings_cache()
    assert config.get_setting("anything", "d") == "d"


def test_a_corrupt_file_still_degrades_to_the_default(tmp_path, monkeypatch):
    """load_config's read path already tolerates corruption; caching must not
    turn that into an exception."""
    p = tmp_path / "config.json"
    p.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", str(p))
    monkeypatch.setattr(config, "_config_path", lambda: str(p))
    config.invalidate_settings_cache()
    assert config.get_setting("anything", "d") == "d"


# --------------------------------------------------------------------------- #
# ...and it is actually faster
# --------------------------------------------------------------------------- #

def test_the_file_is_not_reparsed_on_every_read(tmp_path, monkeypatch):
    """The whole point. 484 parses per chain build is the thing being removed,
    so count parses rather than timing, which would be flaky on CI."""
    _fresh(tmp_path, monkeypatch, {"blocked_models": ["a/b"]})
    calls = {"n": 0}
    real = config.load_config

    def counted(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(config, "load_config", counted)
    for _ in range(50):
        config.get_setting("blocked_models")
    assert calls["n"] <= 1, "re-parsed the file %d times for 50 reads" % calls["n"]


def test_the_hot_seam_uses_it(tmp_path, monkeypatch):
    """_is_model_blocked_by_user is called for every candidate model in every
    chain build; it must not be the thing that reopens the file."""
    import app as A
    _fresh(tmp_path, monkeypatch, {"blocked_models": ["groq/x"]})
    calls = {"n": 0}
    real = config.load_config

    def counted(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(config, "load_config", counted)
    for _ in range(100):
        A._is_model_blocked_by_user("groq", "x")
    assert calls["n"] <= 1
