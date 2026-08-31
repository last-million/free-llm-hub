"""The image question was a guaranteed stop on every website build.

REPORTED 2026-08-31, for the third time: "why in last projects i worked in
/agent and inside cli he dont finsih till the end and he sotp and i need
anlways to say continue WTF man".

The previous fixes were all real -- ACT on every turn, the phased plan in every
mode, tool calls typed out as prose, announcements with no tool call. This one
is different, and it is the one that was actually firing: the hub was TELLING
the agent to stop.

Read from the session at 03:20, after every one of those fixes was live:

    user   "build an engaging wbesite landing page for IPTV"
    agent  "Before I start building, I need one decision from you -- the image
            source. ... 1. FREE STOCK ... 2. GENERATED ... 3. BOTH"
    user   "both gp"

That is not a model failing to continue. That is craft.IMAGES, verbatim:

    "The moment you know the build needs pictures, STOP and offer exactly three
     options, then wait for the answer"

Every website needs pictures, so that fired on every single build -- a
guaranteed halt on turn one, every time, no matter which model or mode ran it.

The brief already answered its own question: option 3 is described as "usually
the right answer for a real site". So it now takes that default, says which it
took and why, and KEEPS BUILDING. Anyone who wants different says so -- and
since 2026-08-31 they can also Restore & rerun that message.

The generator itself, the WebP rule, the image-SEO rules and the ANTI list are
untouched. Only the halt is gone.
"""
import craft


def _brief():
    msg = craft.system_message("build me a restaurant website", tools=True)
    return msg["content"] if msg else ""


# --------------------------------------------------------------------------- #
# The halt is gone
# --------------------------------------------------------------------------- #

def test_it_no_longer_tells_the_agent_to_stop_and_wait():
    body = craft.IMAGES
    assert "STOP and offer" not in body
    assert "wait for the answer" not in body


def test_it_takes_the_default_and_carries_on():
    low = craft.IMAGES.lower()
    assert "both" in low
    assert "do not stop" in low or "keep building" in low or "without stopping" in low


def test_the_default_is_still_announced_not_hidden():
    """Proceeding silently would be its own problem -- the user has to know
    which source was used so they can redirect it."""
    low = craft.IMAGES.lower()
    assert "say in one line which source" in low


def test_an_explicit_instruction_still_wins():
    """If the user already said "generated only", that choice is theirs."""
    low = craft.IMAGES.lower()
    assert "already" in low


# --------------------------------------------------------------------------- #
# Everything else about the brief survives
# --------------------------------------------------------------------------- #

def test_the_generator_is_still_there():
    body = craft.IMAGES
    assert "/v1/images/generations" in body
    assert "127.0.0.1:8787" in body


def test_the_three_options_are_still_described():
    body = craft.IMAGES
    for opt in ("FREE STOCK", "GENERATED", "BOTH"):
        assert opt in body, opt


def test_the_image_seo_rules_survive():
    body = craft.IMAGES
    for rule in ("fetchpriority", "loading=\"lazy\"", "srcset", "alt", "WebP"):
        assert rule in body, rule


def test_the_anti_list_survives():
    assert "ANTI:" in craft.IMAGES
    assert "source.unsplash.com/random" in craft.IMAGES


def test_it_still_refuses_the_no_image_tool_excuse():
    """The original reason this brief exists: models claimed they had no image
    tool and silently fell back to Unsplash."""
    assert "Never claim otherwise" in craft.IMAGES


# --------------------------------------------------------------------------- #
# ...and it still ships
# --------------------------------------------------------------------------- #

def test_the_brief_still_fires_on_a_website_build():
    assert "IMAGES" in _brief()


def test_the_budget_still_holds():
    worst = max(len(craft.system_message(t)["content"]) for t in
                ("build an online store and deploy it",
                 "create a landing page for my saas",
                 "build me a restaurant website"))
    assert worst / 4 < 32768 * 0.125, "briefs cost ~%d tokens" % (worst // 4)
