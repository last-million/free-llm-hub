"""Qwen 4 (and its preview) is used when the hub finds it, with no board row yet.

ASKED 2026-11-12: "if our hub finds the new Qwen 4 or Qwen 4 preview, use it of course
because it is good, but we don't have a real benchmark for it".

MEASURED (sandbox) BEFORE the change: every Qwen 4 id scored 134.10 for chat (the qwen
floor), but on tool turns it had no agentic bonus (134.10 against GLM 5.3's 138.85 and
DeepSeek V4.1 Flash's 141.07) and was not allowed to open a tool turn, while the small
qwen4-27b (it inherits qwen3.8-27b's row) was. Neither OpenRouter's public catalog nor the
hub's live catalog listed a Qwen 4 that day: this only takes effect when one appears.
"""
import app


def chat(pid, mid):
    return app._benchmark_score(pid, mid)


def tool(pid, mid):
    return app._agentic_score((chat(pid, mid), pid, mid))


RELEASES = ("qwen4-max", "qwen4", "qwen4-plus", "qwen/qwen4-max", "Qwen/Qwen4-Max",
            "qwen/qwen4-235b-a22b", "qwen4-coder", "qwen-4-max", "qwen/qwen4-max:free")
PREVIEWS = ("qwen4-max-preview", "qwen/qwen4-preview", "qwen-4-preview",
            "Qwen/Qwen4-Max-Preview", "qwen/qwen4-preview:free")


def test_every_full_size_spelling_gets_the_owner_place():
    for mid in RELEASES:
        assert app._owner_rank_key(mid)[0] == "qwen4", mid
    for mid in PREVIEWS:
        assert app._owner_rank_key(mid)[0] == "qwen4_preview", mid


def test_qwen4_shares_the_top_of_the_free_chinese_models_on_tool_turns():
    q = tool("nvidia", "qwen/qwen4-max")
    ds = tool("nvidia", "deepseek-v4.1-flash")
    glm = tool("nvidia", "z-ai/glm-5.3")
    assert q > ds > glm
    assert q - ds < app._AUTO_TOP_BAND          # level with DeepSeek Flash, not a leap
    assert q - glm >= app._AUTO_TOP_BAND        # clearly ahead of GLM 5.3


def test_it_stays_under_the_models_with_a_record_or_a_higher_owner_place():
    q = tool("nvidia", "qwen/qwen4-max")
    for pid, mid in (("nvidia", "tencent/hy4"), ("nvidia", "claude-opus-5.5"),
                     ("nvidia", "moonshotai/kimi-k3.1-pro")):
        assert tool(pid, mid) > q, mid


def test_qwen4_may_open_a_tool_turn_and_the_older_flagship_is_unchanged():
    for mid in RELEASES + PREVIEWS:
        assert app._may_lead_agentic(chat("nvidia", mid), mid), mid
    assert not app._may_lead_agentic(chat("dashscope", "qwen3.8-max"), "qwen3.8-max")


def test_a_preview_sits_one_step_under_its_release_and_above_glm():
    rel = tool("nvidia", "qwen/qwen4-max")
    pre = tool("nvidia", "qwen/qwen4-max-preview")
    assert rel > pre > tool("nvidia", "z-ai/glm-5.3")
    assert rel - pre < 0.5
    # chat is not lowered for a preview: a newer model never ranks under an older one
    assert chat("nvidia", "qwen/qwen4-max-preview") == chat("nvidia", "qwen/qwen4-max")
    assert chat("nvidia", "qwen/qwen4-max-preview") > chat("nvidia", "qwen/qwen3.9-max")


def test_chat_is_left_on_the_qwen_floor():
    assert abs(chat("nvidia", "qwen/qwen4-max") - 134.10) < 1e-6
    assert chat("nvidia", "qwen/qwen4-max") > chat("nvidia", "z-ai/glm-5.3")
    assert chat("nvidia", "qwen/qwen4-max") < chat("nvidia", "claude-opus-5.5")


def test_speed_and_small_size_cuts_keep_their_own_place():
    for mid in ("qwen4-flash", "qwen4-mini", "qwen4-27b", "qwen/qwen4-8b",
                "qwen4-max-flash", "qwen4-flash-preview"):
        assert app._owner_rank_key(mid)[0] is None, mid
        assert tool("nvidia", mid) < tool("nvidia", "qwen/qwen4-max"), mid


def test_a_newer_release_still_ranks_above_an_older_one():
    assert tool("nvidia", "qwen/qwen4.1-max") > tool("nvidia", "qwen/qwen4-max")
    assert tool("nvidia", "qwen/qwen5-max") > tool("nvidia", "qwen/qwen4.1-max")
    assert chat("nvidia", "qwen/qwen4.1-max") > chat("nvidia", "qwen/qwen4-max")


def test_older_qwen_ids_are_unchanged():
    assert app._owner_rank_key("qwen3.8-max")[0] is None
    assert abs(chat("dashscope", "qwen3.8-max") - 134.08) < 1e-6
    assert abs(tool("dashscope", "qwen3.8-max") - 134.08) < 1e-6
    assert abs(chat("groq", "qwen/qwen3.8-27b") - 133.08) < 1e-6


def test_a_relay_copy_pays_the_relay_discount():
    first = tool("nvidia", "qwen/qwen4-max")
    relay = tool("g4f", "qwen4-max")
    assert first - relay >= 3.9


def test_a_measured_failure_still_demotes_it(monkeypatch):
    base = tool("nvidia", "qwen/qwen4-max")
    monkeypatch.setattr(app, "_reliability_penalty",
                        lambda pid, mid: 10.0 if "qwen4" in mid else 0.0)
    assert tool("nvidia", "qwen/qwen4-max") < base - 9
    assert tool("nvidia", "qwen/qwen4-max") < tool("nvidia", "z-ai/glm-5.3")
