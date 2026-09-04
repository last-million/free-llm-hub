"""Picking Max or Swarm has to reach the hub, or it is just a label.

MEASURED 2026-08-30, from the user's own live session and the hub's own
activity feed -- a codex session whose stored quality was "swarm":

    {"cli": "Codex", "model_req": "auto", "project": "project-20260830-024030",
     "routed": "nvidia/moonshotai/kimi-k3", "source": "build"}

model_req is "auto". Not "swarm". The mode was saved, shown, persisted -- and
then dropped on the floor at the one place it had to survive: the argv of the
CLI subprocess. Reported as "he dont use the mode man".

Three separate holes, one per CLI:

  claude    argv carried a hardcoded `--model opus` on EVERY turn. The env var
            _apply_claude_hub_fallback sets was the only carrier of the mode,
            and an explicit CLI flag is not something an env var wins against.
  codex     model came from config.toml, hardcoded `model = "auto"`; quality
            was never even passed to the function that writes it.
  opencode  same, hardcoded "free-llm-hub/auto", and written ONCE (early-return
            if the file exists), so it could never change afterwards.

The fix is a per-invocation `--model` on all three -- verified against the
installed binaries, not assumed:
    codex exec --help    ->  -m, --model <MODEL>
    opencode run --help  ->  -m, --model  (provider/model)
    claude --help        ->  --model (already used)
A flag on the command line, unlike a shared config file, also cannot be raced
by a second session turning at the same moment.

Normal is deliberately left byte-identical to what shipped before: only Max and
Swarm change argv at all, so the default path carries none of this risk.
"""
import os
import sys
import tempfile
import types
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agentic_chat as ac


def _sess(cli, quality="normal", native=None):
    return types.SimpleNamespace(cli_id=cli, native_session_id=native,
                                 project_dir=tempfile.gettempdir(),
                                 quality=quality)


def _argv(cli, quality, native=None, signed_in=False):
    """argv for one turn, with the hub standing in as the CLI's backend."""
    with mock.patch.object(ac, "_launcher", return_value=["BIN"]), \
            mock.patch.object(ac, "_isolated_signed_in", return_value=signed_in), \
            mock.patch.object(ac, "write_task_brief", return_value=False), \
            mock.patch.object(ac, "_system_prompt_addition", return_value=""):
        return ac._build_argv(_sess(cli, quality, native), "BIN", "do the thing")


def _model_in(argv):
    for i, a in enumerate(argv):
        if a in ("--model", "-m"):
            return argv[i + 1]
    return None


# --------------------------------------------------------------------------- #
# The mapping itself
# --------------------------------------------------------------------------- #

def test_each_mode_maps_to_the_id_the_hub_acts_on():
    """'best' is what app._is_orchestrate turns into quality_mode=True;
    'swarm' is what app._is_swarm_model dispatches to the fan-out. 'auto' is
    ordinary routing. These three strings are the entire contract."""
    assert ac._hub_model_for("max") == "best"
    assert ac._hub_model_for("swarm") == "swarm"
    assert ac._hub_model_for("normal") == "auto"
    assert ac._hub_model_for(None) == "auto"
    assert ac._hub_model_for("nonsense") == "auto"


def test_the_hub_side_really_understands_those_ids():
    """Guards the two modules against drifting apart -- the mapping above is
    worthless if app.py stops recognising what it produces."""
    import app
    assert app._is_orchestrate("best") is True
    assert app._is_swarm_model("swarm") is True
    assert app._is_orchestrate("auto") is True


# --------------------------------------------------------------------------- #
# claude
# --------------------------------------------------------------------------- #

def test_claude_normal_is_unchanged():
    assert _model_in(_argv("claude", "normal")) == ac._MODEL_ALIAS


def test_claude_max_asks_for_best():
    assert _model_in(_argv("claude", "max")) == "best"


def test_claude_swarm_asks_for_swarm():
    assert _model_in(_argv("claude", "swarm")) == "swarm"


def test_claude_on_a_real_subscription_keeps_opus():
    """Signed in means the child talks to Anthropic, not to this hub. 'swarm'
    is not an Anthropic model -- sending it there would fail the turn outright,
    which is worse than not applying the mode."""
    assert _model_in(_argv("claude", "swarm", signed_in=True)) == ac._MODEL_ALIAS
    assert _model_in(_argv("claude", "max", signed_in=True)) == ac._MODEL_ALIAS


def test_claude_carries_the_mode_on_resumed_turns_too():
    """--model does not persist across --resume (the reason _MODEL_ALIAS was
    already re-sent every turn), so the mode has to ride along every time."""
    assert _model_in(_argv("claude", "swarm", native="thread-1")) == "swarm"


# --------------------------------------------------------------------------- #
# codex
# --------------------------------------------------------------------------- #

def test_codex_normal_sends_no_model_flag():
    """Unchanged from what shipped: config.toml decides."""
    assert _model_in(_argv("codex", "normal")) is None


def test_codex_max_and_swarm_reach_the_hub():
    assert _model_in(_argv("codex", "max")) == "best"
    assert _model_in(_argv("codex", "swarm")) == "swarm"


def test_codex_on_a_real_subscription_is_left_alone():
    assert _model_in(_argv("codex", "swarm", signed_in=True)) is None


def test_codex_keeps_its_verified_argv_shape():
    """The flag is added, nothing else moves -- exec/resume/--json and the
    positional prompt LAST are all live-verified against codex-cli 0.144.5."""
    argv = _argv("codex", "swarm")
    assert argv[:2] == ["BIN", "exec"]
    assert "--json" in argv and "--dangerously-bypass-approvals-and-sandbox" in argv
    assert argv[-1] == "do the thing"


def test_codex_resume_keeps_the_thread_id_next_to_resume():
    argv = _argv("codex", "swarm", native="th-9")
    assert argv[argv.index("resume") + 1] == "th-9"
    assert argv[-1] == "do the thing"


# --------------------------------------------------------------------------- #
# opencode
# --------------------------------------------------------------------------- #

def test_opencode_normal_sends_no_model_flag():
    assert _model_in(_argv("opencode", "normal")) is None


def test_opencode_max_and_swarm_name_the_hub_provider():
    """opencode wants provider/model, not a bare id."""
    assert _model_in(_argv("opencode", "max")) == "free-llm-hub/best"
    assert _model_in(_argv("opencode", "swarm")) == "free-llm-hub/swarm"


def test_opencode_keeps_its_verified_argv_shape():
    argv = _argv("opencode", "swarm", native="s-3")
    assert argv[:5] == ["BIN", "run", "--auto", "--format", "json"]
    assert argv[argv.index("--session") + 1] == "s-3"
    assert argv[-1] == "do the thing"


def test_opencode_config_declares_the_modes_it_can_be_asked_for():
    """An openai-compatible provider lists its models; asking for one that is
    not listed is how you get 'model not found' instead of a turn."""
    with tempfile.TemporaryDirectory() as home:
        ac._seed_opencode_config(home)
        import json
        with open(os.path.join(home, "opencode", "opencode.json"), encoding="utf-8") as f:
            cfg = json.load(f)
    models = cfg["provider"]["free-llm-hub"]["models"]
    assert set(models) >= {"auto", "best", "swarm"}, models
    assert cfg["model"] == "free-llm-hub/auto"      # the default is still auto


def test_an_existing_hub_seed_gains_the_new_modes():
    """Anyone who already ran opencode through the hub has the OLD seed on
    disk, listing only 'auto' -- and the seeder used to return early whenever
    the file existed, so it would never have been fixed."""
    import json
    with tempfile.TemporaryDirectory() as home:
        target = os.path.join(home, "opencode", "opencode.json")
        os.makedirs(os.path.dirname(target))
        old = {"$schema": "https://opencode.ai/config.json",
               "provider": {"free-llm-hub": {"npm": "@ai-sdk/openai-compatible",
                                             "name": "Calvoun Free LLM Hub",
                                             "options": {"baseURL": "http://127.0.0.1:8787/v1",
                                                         "apiKey": "k"},
                                             "models": {"auto": {"name": "auto"}}}},
               "model": "free-llm-hub/auto"}
        with open(target, "w", encoding="utf-8") as f:
            json.dump(old, f)
        ac._seed_opencode_config(home)
        with open(target, encoding="utf-8") as f:
            cfg = json.load(f)
    assert set(cfg["provider"]["free-llm-hub"]["models"]) >= {"auto", "best", "swarm"}


def test_a_config_that_is_not_ours_is_never_touched():
    """The seeder's existing promise: a file the user wrote is left alone."""
    import json
    with tempfile.TemporaryDirectory() as home:
        target = os.path.join(home, "opencode", "opencode.json")
        os.makedirs(os.path.dirname(target))
        mine = {"model": "anthropic/claude-opus-4", "provider": {"anthropic": {}}}
        with open(target, "w", encoding="utf-8") as f:
            json.dump(mine, f)
        ac._seed_opencode_config(home)
        with open(target, encoding="utf-8") as f:
            assert json.load(f) == mine
