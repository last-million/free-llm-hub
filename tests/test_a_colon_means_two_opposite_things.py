r"""34 unrelated models were sharing one identity, and routing believed it.

FOUND 2026-09-09 while building the identity-grouped model picker: grouping the
live fleet produced an identity called "latest" with 34 members --

    srv_mqjxnj9i:codestral:latest      -> latest
    srv_mqjxnj9i:gemma4:latest         -> latest
    srv_msg68ooo:gpt-oss:latest        -> latest
    srv_mqjxnj9i:Qwen2:latest          -> latest

-- and another with 7 called "8b". Ollama-style `name:tag` ids, every one of
them, collapsed onto the tag.

NOT COSMETIC. _normalize_model_identity is the hub's "same model" key and is
load-bearing in ROUTING:

  * _model_identity_min_penalty shares one scarcity penalty across every
    candidate with the same identity -- so a 50/day model was lending its
    penalty to 33 strangers, and borrowing theirs;
  * same-host alternation treats same-identity candidates as interchangeable;
  * the retry veto ("not that model again") vetoed all 34 when one failed.

THE ACTUAL PROBLEM is that a colon in a model id means one of two OPPOSITE
things:

    'Airforce:claude-opus-5'    HOST prefix -- the model is on the RIGHT
    'codestral:latest'          Ollama TAG  -- the model is on the LEFT

The old rule (keep the last segment) was right for hosts and wrong for tags.
The first fix attempted here -- keep the FIRST segment -- was measured too, and
was wrong in the mirror image: 17 models under "githubcopilot", 14 under
"airforce". Both were measured against this machine's 504 live model ids
before either was kept.

A tag comes from a small recognisable vocabulary (a version, a size, a
quantisation). A host is an arbitrary word. So tags are matched BY SHAPE and
stripped from the right; whatever colons remain are hosts and are stripped from
the left.

MEASURED, same 504 ids, before -> after:
    identities              260  ->  279
    "latest" members         34  ->    0
    "8b" members              7  ->    0
    gpt-oss-120b members      2  ->    2   (intended merge kept)
    kimi-k3 members          11  ->   11   (intended merge kept)
    glm-5.3 members           8  ->    8   (and now 'GLM:GLM-5.3' joins it)
"""
import pytest

import app as A


# --------------------------------------------------------------------------- #
# Tags: the model is on the LEFT
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("mid,want", [
    ("codestral:latest", "codestral"),
    ("gemma4:latest", "gemma4"),
    ("Qwen2:latest", "qwen2"),
    ("llama3:8b", "llama3"),
    ("llama3:70b-instruct-q4_0", "llama3"),
    ("mistral:7b", "mistral"),
    ("phi:2.7b", "phi"),
    ("qwen:v1.5", "qwen"),
    ("something:q4_K_M", "something"),
    ("something:fp16", "something"),
    ("cyberwald/llama-3.1-sauerkrautlm-8b-instruct:latest",
     "llama-3.1-sauerkrautlm-8b-instruct"),
])
def test_an_ollama_tag_is_stripped_from_the_right(mid, want):
    assert A._normalize_model_identity(mid) == want


def test_every_tagged_model_keeps_its_own_identity():
    """The bug in one assertion: 34 of these used to be the same model."""
    tagged = ["codestral:latest", "gemma4:latest", "gpt-oss:latest",
              "Qwen2:latest", "devstral-small-2:latest", "gemma3:latest"]
    idents = {A._normalize_model_identity(m) for m in tagged}
    assert len(idents) == len(tagged), idents


# --------------------------------------------------------------------------- #
# Hosts: the model is on the RIGHT
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("mid,want", [
    ("Airforce:claude-opus-5", "claude-opus-5"),
    ("Airforce:glm-5.3", "glm-5.3"),
    ("GLM:GLM-5.3", "glm-5.3"),
    ("Claude:claude-sonnet-4-20250514", "claude-sonnet-4-20250514"),
    ("Groq:openai/gpt-oss-120b", "gpt-oss-120b"),
    ("nvidia:moonshotai/kimi-k3", "kimi-k3"),
    ("srv_mkom688d57c76d8a3542:openai/gpt-oss-120b", "gpt-oss-120b"),
    ("pa:657cce02:auto", "auto"),
    ("G4FSpace:srv_mrdypihj16e8b1776409:z-ai/glm-5.3-flash", "glm-5.3-flash"),
])
def test_a_relay_host_is_stripped_from_the_left(mid, want):
    assert A._normalize_model_identity(mid) == want


def test_hosts_do_not_become_an_identity():
    """The mirror-image bug, which the first attempt at this fix shipped: 14
    unrelated models under 'airforce'."""
    hosted = ["Airforce:claude-opus-5", "Airforce:claude-fable-5.1",
              "Airforce:glm-5.3", "Airforce:gpt-6-astra"]
    idents = {A._normalize_model_identity(m) for m in hosted}
    assert "airforce" not in idents
    assert len(idents) == len(hosted)


def test_a_host_and_a_tag_together():
    """Both rules, one id, in the right order."""
    assert A._normalize_model_identity("srv_x1:codestral:latest") == "codestral"


# --------------------------------------------------------------------------- #
# The merges this function EXISTS to make must survive
# --------------------------------------------------------------------------- #

def test_the_same_model_on_different_hosts_is_still_one_identity():
    """The whole reason the function exists: hosts rename the namespace and the
    suffix, never the model."""
    same = ["groq/openai/gpt-oss-120b", "cerebras/gpt-oss-120b",
            "openai/gpt-oss-120b:free", "@cf/openai/gpt-oss-120b",
            "srv_abc123:openai/gpt-oss-120b"]
    assert len({A._normalize_model_identity(m) for m in same}) == 1


def test_the_free_tier_spellings_still_collapse():
    assert (A._normalize_model_identity("z-ai/glm-5.3-free")
            == A._normalize_model_identity("z-ai/glm-5.3")
            == A._normalize_model_identity("GLM:GLM-5.3"))


@pytest.mark.parametrize("mid", [
    "openai/gpt-oss-120b:beta", "openai/gpt-oss-120b:nitro",
    "openai/gpt-oss-120b:extended", "openai/gpt-oss-120b:online",
])
def test_the_provider_suffixes_are_still_removed(mid):
    assert A._normalize_model_identity(mid) == "gpt-oss-120b"


# --------------------------------------------------------------------------- #
# Nothing here may raise
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("mid", ["", None, ":", "::", "a:", ":b", "/", "a/"])
def test_a_degenerate_id_does_not_raise(mid):
    A._normalize_model_identity(mid)


def test_a_model_whose_name_ends_in_a_version_is_not_eaten():
    """'free-willy-2' must not lose its tail to the size/version tag rule --
    the tag has to be after a COLON, not a hyphen."""
    assert A._normalize_model_identity("stabilityai/free-willy-2") == "free-willy-2"
    assert A._normalize_model_identity("qwen3.8-27b") == "qwen3.8-27b"


def test_it_stays_stable_under_repetition():
    """Routing calls this on ids it has already normalised."""
    once = A._normalize_model_identity("srv_x:codestral:latest")
    assert A._normalize_model_identity(once) == once
