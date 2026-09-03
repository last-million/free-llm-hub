"""/v1/embeddings -- the surface the hub could not serve at all.

Fourth gap from the freellmapi comparison. The hub was not merely missing a
route: it could not have written one, because embedding models are stripped out
of the catalog before anything sees them. providers.filter_models drops every
non-chat id (whisper/tts/embed/guard) so that routing can never pick Whisper to
write code -- correct for chat, and it also made embeddings invisible.

They are a different SURFACE, not junk. /v1/embeddings is what codebase
indexing, RAG and every vector-store integration call, and without it this hub
is chat-only for tools like Continue and Open WebUI.

So discovery now keeps the embedding ids it was already holding, in the same
pass, rather than paying for a second round trip to re-fetch what it just threw
away.

The one design point worth stating: vectors from two different models are NOT
comparable. A silent fallback halfway through indexing a corpus would poison the
index in a way that surfaces much later as unexplained retrieval nonsense, so
the model that actually served is always reported back.
"""
from unittest import mock

import pytest

import app as A
import providers as prov


@pytest.fixture
def client():
    return A.app.test_client()


ROWS = [{"id": "alpha/bge-m3", "provider": "alpha", "model": "bge-m3"},
        {"id": "beta/bge-m3", "provider": "beta", "model": "bge-m3"}]


def _vectors(n=1, dim=3):
    return {"object": "list", "model": "upstream-name",
            "data": [{"object": "embedding", "index": i, "embedding": [0.1] * dim}
                     for i in range(n)],
            "usage": {"prompt_tokens": 4, "total_tokens": 4}}


def _resp(status=200, payload=None):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = payload if payload is not None else _vectors()
    r.headers = {}
    return r


@pytest.fixture
def catalog():
    with mock.patch.object(A, "embedding_models", return_value=list(ROWS)):
        yield


# --------------------------------------------------------------------------- #
# Recognising an embedding model
# --------------------------------------------------------------------------- #

def test_the_usual_families_are_recognised():
    for mid in ("text-embedding-3-small", "bge-m3", "nomic-embed-text",
                "gte-large", "e5-mistral-7b", "all-minilm-l6-v2"):
        assert prov.is_embedding_model(mid), mid


def test_chat_models_are_not():
    for mid in ("gpt-4o", "glm-5.3", "qwen3-32b", "claude-opus"):
        assert not prov.is_embedding_model(mid), mid


def test_rerankers_are_excluded():
    """They match the same name families but return a SCORE, not a vector, so a
    client that sent one to /v1/embeddings gets a shape it cannot use."""
    for mid in ("bge-reranker-v2-m3", "jina-reranker-v2"):
        assert not prov.is_embedding_model(mid), mid


def test_they_are_still_kept_out_of_the_chat_catalog():
    """The reason they were invisible in the first place must not regress:
    routing picking an embedding model to generate text is a hard failure."""
    assert prov.filter_models(["bge-m3", "glm-5.3"]) == ["glm-5.3"]


# --------------------------------------------------------------------------- #
# The chain
# --------------------------------------------------------------------------- #

def test_auto_fans_out_across_every_provider(catalog):
    assert A._embedding_chain("auto") == [("alpha", "bge-m3"), ("beta", "bge-m3")]


def test_a_pinned_id_uses_only_that_provider(catalog):
    assert A._embedding_chain("beta/bge-m3") == [("beta", "bge-m3")]


def test_a_bare_model_name_matches_every_provider_serving_it(catalog):
    assert A._embedding_chain("bge-m3") == [("alpha", "bge-m3"), ("beta", "bge-m3")]


def test_an_unknown_model_matches_nothing(catalog):
    assert A._embedding_chain("nope/not-real") == []


# --------------------------------------------------------------------------- #
# The route
# --------------------------------------------------------------------------- #

def test_a_string_input_returns_vectors(client, catalog):
    with mock.patch.object(A, "_upstream_post", return_value=_resp()):
        r = client.post("/v1/embeddings", json={"model": "auto", "input": "hello"})
    assert r.status_code == 200
    assert r.get_json()["data"][0]["embedding"] == [0.1, 0.1, 0.1]


def test_an_array_input_is_passed_through_as_a_batch(client, catalog):
    with mock.patch.object(A, "_upstream_post", return_value=_resp(payload=_vectors(2))) as up:
        r = client.post("/v1/embeddings", json={"input": ["a", "b"]})
    assert len(r.get_json()["data"]) == 2
    assert up.call_args[0][2]["input"] == ["a", "b"]


def test_the_serving_model_is_reported_back(client, catalog):
    """Vectors from two models are not comparable; a caller indexing a corpus
    has to be able to see that a hop changed underneath it."""
    with mock.patch.object(A, "_upstream_post", return_value=_resp()):
        r = client.post("/v1/embeddings", json={"input": "x"})
    assert r.get_json()["model"] == "alpha/bge-m3"
    assert r.headers["X-Free-LLM-Hub-Model"] == "alpha/bge-m3"


def test_it_falls_through_to_the_next_provider(client, catalog):
    with mock.patch.object(A, "_upstream_post",
                           side_effect=[_resp(500), _resp()]) as up:
        r = client.post("/v1/embeddings", json={"input": "x"})
    assert r.status_code == 200
    assert r.get_json()["model"] == "beta/bge-m3"
    assert up.call_count == 2


def test_a_provider_that_returns_no_vectors_is_treated_as_a_failure(client, catalog):
    """A 200 carrying an empty data array is not an answer."""
    empty = _resp(payload={"object": "list", "data": []})
    with mock.patch.object(A, "_upstream_post", side_effect=[empty, _resp()]):
        r = client.post("/v1/embeddings", json={"input": "x"})
    assert r.get_json()["model"] == "beta/bge-m3"


def test_every_provider_failing_is_a_502_that_says_why(client, catalog):
    with mock.patch.object(A, "_upstream_post", return_value=_resp(500)):
        r = client.post("/v1/embeddings", json={"input": "x"})
    assert r.status_code == 502
    assert "500" in r.get_json()["error"]["message"]


def test_the_fallback_is_bounded(client):
    """Embeddings are called in tight indexing loops, so an unbounded chain
    turns one bad provider into thousands of slow requests instead of a fast
    error."""
    many = [{"id": "p%d/bge-m3" % i, "provider": "p%d" % i, "model": "bge-m3"}
            for i in range(20)]
    with mock.patch.object(A, "embedding_models", return_value=many), \
         mock.patch.object(A, "_upstream_post", return_value=_resp(500)) as up:
        client.post("/v1/embeddings", json={"input": "x"})
    assert up.call_count == A._EMBED_MAX_HOPS


def test_no_embedding_model_says_what_to_do(client):
    with mock.patch.object(A, "embedding_models", return_value=[]):
        r = client.post("/v1/embeddings", json={"input": "x"})
    assert r.status_code == 503
    assert "Enable a provider" in r.get_json()["error"]["message"]


def test_a_missing_input_is_refused(client, catalog):
    for body in ({}, {"input": ""}, {"input": []}):
        assert client.post("/v1/embeddings", json=body).status_code == 400


def test_dimensions_and_encoding_format_reach_the_provider(client, catalog):
    with mock.patch.object(A, "_upstream_post", return_value=_resp()) as up:
        client.post("/v1/embeddings", json={"input": "x", "dimensions": 256,
                                            "encoding_format": "float"})
    sent = up.call_args[0][2]
    assert sent["dimensions"] == 256 and sent["encoding_format"] == "float"


# --------------------------------------------------------------------------- #
# Ollama's two shapes
# --------------------------------------------------------------------------- #

@pytest.fixture
def ollama_on():
    with mock.patch.object(A.config, "get_flag",
                           side_effect=lambda k, d=None: True if k == "ollama_api" else d):
        yield


def test_api_embed_returns_the_current_shape(client, catalog, ollama_on):
    with mock.patch.object(A, "_upstream_post", return_value=_resp(payload=_vectors(2))):
        body = client.post("/api/embed", json={"model": "auto",
                                               "input": ["a", "b"]}).get_json()
    assert len(body["embeddings"]) == 2
    assert body["embeddings"][0] == [0.1, 0.1, 0.1]


def test_the_deprecated_endpoint_returns_the_old_singular_shape(client, catalog, ollama_on):
    """/api/embeddings takes `prompt` and returns `embedding`, not `embeddings`.
    Clients in the wild still use both."""
    with mock.patch.object(A, "_upstream_post", return_value=_resp()):
        body = client.post("/api/embeddings", json={"prompt": "a"}).get_json()
    assert body["embedding"] == [0.1, 0.1, 0.1]
    assert "embeddings" not in body


def test_the_ollama_embedding_paths_are_off_by_default(client, catalog):
    assert client.post("/api/embed", json={"input": "x"}).status_code == 404


def test_a_missing_prompt_is_refused(client, catalog, ollama_on):
    r = client.post("/api/embeddings", json={})
    assert r.status_code == 400
    assert "prompt" in r.get_json()["error"]
