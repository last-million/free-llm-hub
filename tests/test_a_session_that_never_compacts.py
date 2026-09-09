r"""A session that fills up and can only be escaped by quitting opencode.

REPORTED 2026-09-09: "sometimes in opencode and other CLI's it's like the
session gets full and I should go out from conversation and reopen it again to
continue".

NOT THE HUB. Measured first, against the live hub, with an opencode-shaped
history that grows the way a coding session grows (tool call + fat tool result,
over and over), tools attached, streaming:

    turns  ~tokens   http   secs   outcome
    2       11580    200    14.7   tool_call
    20     108072    200    19.7   tool_call
    70     376122    200     9.9   tool_call
    160    858657    200    19.2   tool_call

858K tokens and still answering with a real tool call. The hub compacts per hop
and routes around anything too small, so it never refuses a big session.

IT IS OPENCODE, AND THE HUB TOLD IT TO. Decompiled from the shipped binary
(opencode 1.18.29):

    function Dl(e){                                  // should we auto-compact?
      if(e.cfg.compaction?.auto===!1) return !1;
      if(e.model.limit.context===0) return !1;       // <-- silently never
      return (e.tokens.total||...) >= Is(e);
    }
    function Is(e){ let o=e.model.limit.context; if(o===0) return 0; ... }

and the model schema those read from:

    limit: D.optional(D.Struct({context: D.Finite,
                                input: D.optional(D.Finite),
                                output: D.Finite}))

`limit` is OPTIONAL, and the hub's provider block declared each model as
`{"name": "..."}` and nothing else. So limit.context is 0, so auto-compaction
is disabled -- not failing, not warning, just off. The history grows forever
until the turn stops working, and the only recovery a user has is to quit the
session and start a new one. Which is exactly the report.

The number is the one codex is already given (_CODEX_CONTEXT_WINDOW): the hub
has one assumption about how much context it offers and it is written down
once. Codex was told in July (model_context_window +
model_auto_compact_token_limit, because its metadata lookup misses on routing
verbs like "auto"); opencode was never told at all, and it is the CLI the user
actually lives in.
"""
import json

import agentic_chat as AC


MODELS = AC._opencode_hub_models()


# --------------------------------------------------------------------------- #
# The bug: limit.context == 0
# --------------------------------------------------------------------------- #

def test_every_hub_model_declares_a_context_window():
    """`if(e.model.limit.context===0) return !1` -- one missing key per model
    and the CLI never compacts anything, ever."""
    for mid, spec in MODELS.items():
        assert spec.get("limit", {}).get("context", 0) > 0, mid


def test_every_hub_model_declares_an_output_limit():
    """`output` is required INSIDE the struct -- a limit object missing it is
    not a valid model entry."""
    for mid, spec in MODELS.items():
        assert spec["limit"].get("output", 0) > 0, mid


def test_the_output_reserve_leaves_most_of_the_window_usable():
    """The compaction threshold is context - output. An output reserve close to
    the window would compact almost immediately, which is the same session
    unusable from the other direction."""
    for mid, spec in MODELS.items():
        lim = spec["limit"]
        assert lim["output"] < lim["context"] // 2, mid


def test_the_window_is_the_same_one_codex_is_told():
    """One assumption about how much context this hub offers, in one place.
    Codex has been told since July; opencode was never told at all."""
    for spec in MODELS.values():
        assert spec["limit"]["context"] == AC._CODEX_CONTEXT_WINDOW


def test_each_model_gets_its_own_limit_object():
    """Sharing one dict across ten entries means one later edit silently
    rewrites all of them -- and json.dump would not show the aliasing."""
    limits = [id(spec["limit"]) for spec in MODELS.values()]
    assert len(set(limits)) == len(limits)


# --------------------------------------------------------------------------- #
# The rest of what a CLI needs to know
# --------------------------------------------------------------------------- #

def test_the_models_advertise_that_they_can_call_tools():
    """`toolcall:Z.tool_call??!0` -- it defaults true, but a coding agent
    deciding whether to attach schemas should be told, not left to a default."""
    for mid, spec in MODELS.items():
        assert spec.get("tool_call") is True, mid


def test_the_vision_mode_accepts_attachments():
    """Every other mode routes text. Declaring attachment on all of them would
    invite opencode to send an image into a chain that cannot take one."""
    if "vision" in MODELS:
        assert MODELS["vision"].get("attachment") is True
        others = [m for m in MODELS if m != "vision"]
        assert all(MODELS[m].get("attachment") is False for m in others)


def test_the_labels_are_still_there():
    """The picker still has to read like a picker."""
    for mid, spec in MODELS.items():
        assert spec.get("name"), mid
    assert MODELS["auto"]["name"].startswith("effort:")
    assert MODELS["uncensored"]["name"].startswith("mode:")


def test_the_entry_is_valid_json():
    json.dumps(MODELS)


# --------------------------------------------------------------------------- #
# An install that already has the old, limitless entries
# --------------------------------------------------------------------------- #

def _write(tmp_path, models):
    p = tmp_path / "opencode.json"
    p.write_text(json.dumps({
        "$schema": "https://opencode.ai/config.json",
        "provider": {"free-llm-hub": {"npm": "@ai-sdk/openai-compatible",
                                      "models": models}},
        "model": "free-llm-hub/auto"}), encoding="utf-8")
    return p


def test_an_existing_entry_gains_the_missing_limit(tmp_path):
    """Every install that ran before this shipped has ten limitless entries.
    Only topping up MISSING IDS would leave all ten of them broken forever --
    which is the shape the previous upgrade had."""
    p = _write(tmp_path, {k: {"name": "old label"} for k in MODELS})
    AC._upgrade_opencode_seed(str(p))
    got = json.loads(p.read_text(encoding="utf-8"))
    for mid in MODELS:
        assert got["provider"]["free-llm-hub"]["models"][mid]["limit"]["context"] > 0


def test_a_label_the_user_renamed_is_kept(tmp_path):
    """Adding a field is a repair; overwriting a name is taking their config."""
    p = _write(tmp_path, {"auto": {"name": "MY OWN LABEL"}})
    AC._upgrade_opencode_seed(str(p))
    got = json.loads(p.read_text(encoding="utf-8"))
    assert got["provider"]["free-llm-hub"]["models"]["auto"]["name"] == "MY OWN LABEL"


def test_a_limit_the_user_set_is_kept(tmp_path):
    """Someone who knows their fleet can raise it. Only an ABSENT field is
    filled in."""
    p = _write(tmp_path, {"auto": {"name": "x", "limit": {"context": 999999,
                                                          "output": 4096}}})
    AC._upgrade_opencode_seed(str(p))
    got = json.loads(p.read_text(encoding="utf-8"))
    assert got["provider"]["free-llm-hub"]["models"]["auto"]["limit"]["context"] == 999999


def test_missing_ids_are_still_added(tmp_path):
    """The behaviour that was already there does not regress."""
    p = _write(tmp_path, {"auto": {"name": "x"}})
    AC._upgrade_opencode_seed(str(p))
    got = json.loads(p.read_text(encoding="utf-8"))["provider"]["free-llm-hub"]["models"]
    assert set(got) == set(MODELS)


def test_a_config_that_is_not_ours_is_never_touched(tmp_path):
    p = tmp_path / "opencode.json"
    original = json.dumps({"provider": {"anthropic": {"models": {"x": {}}}}})
    p.write_text(original, encoding="utf-8")
    AC._upgrade_opencode_seed(str(p))
    assert p.read_text(encoding="utf-8") == original


def test_an_already_correct_config_is_not_rewritten(tmp_path):
    p = _write(tmp_path, {k: dict(v) for k, v in MODELS.items()})
    before = p.read_text(encoding="utf-8")
    AC._upgrade_opencode_seed(str(p))
    assert p.read_text(encoding="utf-8") == before


def test_a_corrupt_config_does_not_raise(tmp_path):
    p = tmp_path / "opencode.json"
    p.write_text("{ not json", encoding="utf-8")
    AC._upgrade_opencode_seed(str(p))     # must not raise


# --------------------------------------------------------------------------- #
# Both writers, one definition
# --------------------------------------------------------------------------- #

def test_the_dashboard_connect_writes_the_same_entries():
    src = open("app.py", encoding="utf-8").read()
    body = src.split("def _autofix_opencode(", 1)[1]
    body = body[:body.index("\ndef ")]
    assert "agentic_chat._opencode_hub_models()" in body


def test_the_users_own_config_is_repaired_too():
    """The isolated /agent copy self-healed; the config the user actually runs
    opencode against was only ever written by Connect, so without this every
    existing install stays limitless until someone clicks it again."""
    src = open("app.py", encoding="utf-8").read()
    body = src.split("def _repair_opencode_config(", 1)[1]
    body = body[:body.index("\ndef ")]
    assert "_p_opencode()" in body
    assert "_upgrade_opencode_seed(" in body
    assert "_backup_once(" in body, "this is the user's own file"
    # ...and it has to actually run at startup, or it repairs nobody.
    assert "_repair_opencode_config()" in src.split("def _repair_opencode_config(", 1)[1]


# --------------------------------------------------------------------------- #
# ...and every OTHER CLI is told the same number
# --------------------------------------------------------------------------- #

def test_one_context_window_for_every_cli():
    """A CLI compacts, or refuses, against whatever it was told. openclaw was
    handed a hand-written 200000 that agreed with nothing else here -- so it
    would let a session grow half again past the size the hub states everywhere
    else before summarising it."""
    import app as A
    assert A.HUB_CONTEXT_WINDOW == AC._CODEX_CONTEXT_WINDOW
    assert A._PI_CTX == A.HUB_CONTEXT_WINDOW
    src = open("app.py", encoding="utf-8").read()
    body = src.split("def _autofix_openclaw(", 1)[1]
    body = body[:body.index("\ndef ")]
    assert "HUB_CONTEXT_WINDOW" in body
    assert "200000" not in body


def test_the_reply_reserve_agrees_too():
    import app as A
    assert A.HUB_MAX_TOKENS == A._PI_MAX_TOKENS
