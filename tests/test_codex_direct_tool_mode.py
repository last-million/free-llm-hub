"""The hub's codex /model entries use tool_mode "direct" (2026-10-30).

codex 0.165 reads `tool_mode` from the model catalog. The hub clones its entries from
codex's bundled one, whose value is "code_mode_only": codex then offers ONE tool,
`exec` (raw JavaScript, `await tools.exec_command(...)`), plus `wait`, so a free model
has to write a program to run a command. MEASURED with the real codex (isolated
CODEX_HOME, fake Responses server): "direct" offers exec_command / write_stdin /
apply_patch as ordinary tools. The owner chose direct for the hub's own entries.
"""
import copy

import pytest

import app as A


def _entry(slug, **extra):
    e = {"slug": slug, "display_name": slug, "description": "d", "visibility": "list",
         "priority": 5, "shell_type": "unified_exec", "tool_mode": "code_mode_only",
         "context_window": 100000, "max_context_window": 100000,
         "supported_reasoning_levels": [], "default_reasoning_level": "low"}
    e.update(extra)
    return e


BUILTIN = _entry("gpt-6-sol")


def _hub_entries(dump):
    out = A._codex_catalog_models(dump)
    hub = {m["slug"] for m in out} - {BUILTIN["slug"]}
    return [m for m in out if m["slug"] in hub], out


def _flag(monkeypatch, on):
    real = A.config.get_flag
    monkeypatch.setattr(A.config, "get_flag",
                        lambda k, d=None: on if k == "codex_direct_tools" else real(k, d))


def test_hub_entries_use_direct_and_builtins_keep_their_own_mode():
    hub, out = _hub_entries({"models": [copy.deepcopy(BUILTIN)]})
    assert hub and all(m["tool_mode"] == "direct" for m in hub)
    builtin = [m for m in out if m["slug"] == "gpt-6-sol"]
    assert len(builtin) == 1 and builtin[0]["tool_mode"] == "code_mode_only"


def test_every_other_field_is_still_cloned_from_the_template():
    hub, _ = _hub_entries({"models": [copy.deepcopy(BUILTIN)]})
    coding = [m for m in hub if m["slug"] == "coding"][0]
    assert coding["shell_type"] == "unified_exec"
    assert coding["visibility"] == "list"


def test_a_template_without_the_field_gets_none_added():
    tpl = copy.deepcopy(BUILTIN)
    tpl.pop("tool_mode")
    hub, _ = _hub_entries({"models": [tpl]})
    assert hub and all("tool_mode" not in m for m in hub)


def test_the_flag_off_keeps_the_bundled_value(monkeypatch):
    _flag(monkeypatch, False)
    hub, _ = _hub_entries({"models": [copy.deepcopy(BUILTIN)]})
    assert hub and all(m["tool_mode"] == "code_mode_only" for m in hub)


def test_a_flag_read_that_raises_counts_as_off(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("config unreadable")
    monkeypatch.setattr(A.config, "get_flag", boom)
    assert A._codex_direct_tools_on() is False


def test_the_template_dump_itself_is_not_changed():
    dump = {"models": [copy.deepcopy(BUILTIN)]}
    A._codex_catalog_models(dump)
    assert dump["models"][0]["tool_mode"] == "code_mode_only"
