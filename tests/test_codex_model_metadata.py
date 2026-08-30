"""Tell codex how big the hub's context is instead of letting it guess.

REPORTED 2026-08-30, on every hub-backed codex turn:

    Model metadata for `swarm` not found. Defaulting to fallback metadata;
    this can degrade performance and cause issues.

Codex looks its model up in a built-in metadata table to learn the context
window and max output. The hub's model ids ("auto", "best", "swarm") are the
hub's own routing verbs, not real model names, so the lookup misses every time
and codex falls back to a built-in default -- it is GUESSING how much context
it may use. Pre-existing, not new: the same warning appeared for "auto" long
before the quality modes existed (see the fixture in test_codex_agentic.py,
which captured 'Model metadata for `auto` not found' from a live run in July).

codex config.toml takes model_context_window and model_auto_compact_token_limit
as explicit overrides, so the hub states the numbers instead of leaving codex to
default. 128000 is the same context size the hub already tells every other CLI
it has (see the Kimi Code setup text in app.py) -- one number, one assumption.

The warning LINE is left alone on purpose. Silencing it needs model_catalog_json,
an undocumented internal schema -- a probe against codex 0.146.0 got a catalog
accepted only after it named slug, display_name, context_window,
max_output_tokens, auto_compact_token_limit, supported_reasoning_levels,
shell_type, with more behind those. Codex REFUSES TO START when that file misses
a field it wants, so shipping one would break every user's codex the next time
OpenAI adds a field. A cosmetic warning is the cheaper of the two.

Written the same way as every other line the hub adds to config.toml: tagged
with _CODEX_HUB_MARKER, so a later real sign-in removes exactly these lines and
nothing codex or the user wrote.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agentic_chat as ac


def _text(existing=""):
    return ac._codex_hub_fallback_text(existing)


def test_the_context_window_is_stated():
    text = _text()
    assert "model_context_window = " in text
    assert str(ac._CODEX_CONTEXT_WINDOW) in text


def test_the_compaction_threshold_is_stated():
    """codex derives this from the context window, so a guessed window means a
    badly-timed compaction as well."""
    assert "model_auto_compact_token_limit = " in _text()
    assert str(ac._CODEX_COMPACT_LIMIT) in _text()


def test_only_documented_keys_are_written():
    """`model_max_output_tokens` was written here first and is NOT a real codex
    config key -- it appears nowhere in the config reference. An unrecognised
    key is exactly what --strict-config exists to reject, so the hub must not
    put one in anyone's config."""
    assert "model_max_output_tokens" not in _text()


def test_it_matches_what_the_hub_tells_every_other_cli():
    """One assumption, not one per CLI."""
    assert ac._CODEX_CONTEXT_WINDOW == 128000


def test_both_lines_carry_the_removal_marker():
    """A later real sign-in strips exactly the hub's own lines. An untagged
    line would survive forever and quietly cap a real ChatGPT subscription."""
    for line in _text().splitlines():
        if line.startswith(("model_context_window", "model_auto_compact_token_limit")):
            assert ac._CODEX_HUB_MARKER in line, line


def test_a_sign_in_removes_them_again():
    text = _text()
    reverted = ac._revert_codex_hub_fallback_text(text)
    assert "model_context_window" not in reverted
    assert "model_auto_compact_token_limit" not in reverted
    assert ac._CODEX_HUB_MARKER not in reverted


def test_the_users_own_lines_survive_a_revert():
    mine = 'model_context_window = 999999\n\n[projects."c:/x"]\ntrust_level = "trusted"\n'
    reverted = ac._revert_codex_hub_fallback_text(_text(mine))
    assert "999999" in reverted, reverted
    assert 'trust_level = "trusted"' in reverted


def test_re_applying_does_not_pile_up_copies():
    """Measured once before on this file: a re-applied block accumulated one
    copy per turn. Every write here runs on every single turn."""
    once = _text()
    assert _text(once) == once
    assert once.count("model_context_window") == 1


def test_codexs_own_entries_are_still_untouched():
    existing = '[projects."c:/users/x/proj"]\ntrust_level = "trusted"\n'
    text = _text(existing)
    assert 'trust_level = "trusted"' in text
    assert "[model_providers.freehub]" in text
    assert 'model_provider = "freehub"' in text


def test_the_written_file_is_still_valid_toml():
    text = _text('[projects."c:/x"]\ntrust_level = "trusted"\n')
    try:
        import tomllib
    except ImportError:                                   # pragma: no cover
        import tomli as tomllib
    parsed = tomllib.loads(text)
    assert parsed["model_context_window"] == ac._CODEX_CONTEXT_WINDOW
    assert parsed["model_auto_compact_token_limit"] == ac._CODEX_COMPACT_LIMIT
    assert parsed["model_provider"] == "freehub"
    assert parsed["model_providers"]["freehub"]["wire_api"] == "responses"


def test_they_are_numbers_not_strings():
    """TOML: a quoted value is a string, and codex wants an integer here."""
    for line in _text().splitlines():
        if line.startswith(("model_context_window", "model_auto_compact_token_limit")):
            value = line.split("=", 1)[1].split("#")[0].strip()
            assert value.isdigit(), line


def test_the_whole_file_survives_a_round_trip_on_disk():
    with tempfile.TemporaryDirectory() as home:
        ac._apply_codex_hub_fallback(home)
        path = os.path.join(home, "config.toml")
        assert os.path.isfile(path)
        with open(path, encoding="utf-8") as f:
            written = f.read()
    assert "model_context_window" in written
    assert "model_auto_compact_token_limit" in written


def test_a_key_the_hub_no_longer_writes_is_cleaned_up():
    """FOUND on a live config during this work. An earlier version of this file
    wrote `model_max_output_tokens`, which turned out not to be a real codex
    key at all. Removing it from the code was not enough: the line stayed in the
    config forever, because nothing revisits a marker-tagged key the hub has
    stopped emitting -- and --strict-config, the very flag that would catch a
    bogus key, rejects the whole file over it. The marker means "the hub owns
    this line", so the hub cleans up after itself."""
    stale = ('model_max_output_tokens = 32000  %s\n'
             'model_provider = "freehub"  %s\n' % (ac._CODEX_HUB_MARKER,
                                                   ac._CODEX_HUB_MARKER))
    text = _text(stale)
    assert "model_max_output_tokens" not in text, text
    assert 'model_provider = "freehub"' in text


def test_a_line_the_user_wrote_is_never_cleaned_up():
    """Only OUR lines. An identically-named key without the marker is theirs."""
    mine = "model_max_output_tokens = 4242\n"
    assert "4242" in _text(mine)
