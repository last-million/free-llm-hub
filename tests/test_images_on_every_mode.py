"""Images work on every mode and in every CLI (2026-10-24).

Owner: "the hub should know which models support images -- which can see and
which can't -- and if a model can't do a task the hub should switch to another
one." The reported symptom (opencode `coding-swarm`): "I can't read image files
-- this model doesn't support image input". That message was opencode's OWN,
printed because the hub declared `attachment: false` on every mode but vision,
so the screenshot was stripped client-side and never reached the hub.

Two halves, both here:
  1. A pipeline tier (swarm/multi/crews) or a non-vision category mode has its
     images DESCRIBED by a vision model up front (app._predescribe_pipeline_images),
     so the answering model gets words it can read. auto/best/vision/pin keep the
     native path (a model that can see gets the real image).
  2. A short reply that OPENS by refusing the task for a missing capability
     ("I can't see images") is a non-answer, so the chain switches
     (answer_check.is_cannot_do_capability).
"""
import app as A
import answer_check as AC


def _img_turn(text="look at this", url="data:image/png;base64,AAAA"):
    return [{"role": "user", "content": [
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": url}}]}]


# --------------------------------------------------------------------------- #
# 1. Which turns bypass the native vision route (and so get a described image)
# --------------------------------------------------------------------------- #

def test_pipeline_tiers_and_nonvision_modes_bypass_the_vision_route():
    # A compound like "coding-swarm" is rewritten to model="swarm" + mode=coding
    # by _apply_category_effort BEFORE this helper is consulted, so the id that
    # actually reaches it is the bare tier/mode below.
    for mid in ("swarm", "multi", "crew-code", "coding", "reasoning"):
        assert A._turn_bypasses_vision_route(mid) is True, mid


def test_auto_best_vision_and_pins_keep_the_native_route():
    for mid in ("auto", "best", "max", "vision", "vision-max", "", None,
                "nvidia/some-model"):
        assert A._turn_bypasses_vision_route(mid) is False, mid


# --------------------------------------------------------------------------- #
# 2. The describe step
# --------------------------------------------------------------------------- #

def test_a_swarm_turn_gets_its_image_described(monkeypatch):
    monkeypatch.setattr(A, "_describe_image", lambda url, est=0: "a login form")
    msgs = _img_turn()
    out, n = A._predescribe_pipeline_images("swarm", msgs, est=0)
    assert n == 1
    # the image part is now text, and no image_url survives
    assert not A._message_images(out)
    flat = out[0]["content"][1]
    assert flat["type"] == "text" and "a login form" in flat["text"]


def test_auto_leaves_the_image_for_the_native_vision_route(monkeypatch):
    monkeypatch.setattr(A, "_describe_image", lambda url, est=0: "should not run")
    msgs = _img_turn()
    out, n = A._predescribe_pipeline_images("auto", msgs, est=0)
    assert n == 0 and out is msgs and A._message_images(out)      # untouched


def test_vision_mode_leaves_the_image(monkeypatch):
    monkeypatch.setattr(A, "_describe_image", lambda url, est=0: "nope")
    out, n = A._predescribe_pipeline_images("vision", _img_turn(), est=0)
    assert n == 0


def test_a_text_only_turn_is_never_touched():
    msgs = [{"role": "user", "content": "just text"}]
    out, n = A._predescribe_pipeline_images("swarm", msgs, est=0)
    assert n == 0 and out is msgs


def test_fails_open_when_nothing_can_see(monkeypatch):
    # no vision model on the fleet -> _describe_image returns "" -> turn untouched
    monkeypatch.setattr(A, "_describe_image", lambda url, est=0: "")
    out, n = A._predescribe_pipeline_images("swarm", _img_turn(), est=0)
    assert n == 0 and A._message_images(out)      # the image rides through


def test_never_raises_on_garbage():
    assert A._predescribe_pipeline_images("swarm", None, 0) == (None, 0)
    assert A._predescribe_pipeline_images(None, "nonsense", 0) == ("nonsense", 0)


# --------------------------------------------------------------------------- #
# 3. "I can't see images" is a non-answer -> the chain switches
# --------------------------------------------------------------------------- #

def test_cannot_see_images_is_a_non_answer():
    for reply in (
        "I can't read image files, so I can't inspect that screenshot.",
        "I'm unable to view images in this conversation.",
        "This model doesn't support image input, so I cannot see it.",
        "Sorry, I cannot see the screenshot you attached.",
        "Image input is not supported by this model.",
        "I don't have the ability to see images.",
    ):
        assert AC.is_cannot_do_capability(reply, "look at this screenshot and fix the bug"), reply


def test_a_real_answer_is_not_a_capability_refusal():
    for reply in (
        "The login button is misaligned; move it 8px left and it lines up.",
        "Here is the fix: set `display:flex` on the container.",
        "I read the screenshot: the error is a 404 on /api/users.",
    ):
        assert not AC.is_cannot_do_capability(reply, "look at this screenshot")


def test_a_file_access_refusal_is_a_real_report_not_a_capability_gap():
    # A sandbox/tool refusal the model is correctly REPORTING is a served answer,
    # not a missing capability -- it must NOT be junked (test_no_tools_claim).
    for reply in (
        "I can't access files outside the project; the read was refused.",
        "I'm unable to open that file -- permission denied.",
        "I cannot read /etc/hosts; it is outside the workspace.",
    ):
        assert not AC.is_cannot_do_capability(reply, "read /etc/hosts"), reply


def test_asking_about_image_support_gets_an_honest_no():
    # the user's question IS about capability -> "no" is the real answer, not a failed hop
    assert not AC.is_cannot_do_capability(
        "No, this model does not support image input.",
        "does this model support image input?")


def test_capability_refusal_only_at_the_opening():
    # a long answer that merely MENTIONS image support late is not cut
    reply = ("The component renders fine. " * 40
             + "Note: this model doesn't support image input for other tasks.")
    assert not AC.is_cannot_do_capability(reply, "review this code")


def test_the_gate_is_relay_or_tool_turns():
    # wired into inspect() only for relay hops / tool turns (like idle_greeting)
    bad = "I can't see images, so I can't help with that screenshot."
    direct_chat = AC.inspect(bad, prompt_text="fix my screenshot bug",
                             tools_offered=False, relay=False)
    assert direct_chat["ok"] is True                       # not gated here
    tool_turn = AC.inspect(bad, prompt_text="fix my screenshot bug",
                           tools_offered=True, relay=False)
    assert tool_turn["ok"] is False and "cannot_do" in tool_turn["reasons"]
    relay_hop = AC.inspect(bad, prompt_text="fix my screenshot bug",
                           tools_offered=False, relay=True)
    assert relay_hop["ok"] is False and "cannot_do" in relay_hop["reasons"]
