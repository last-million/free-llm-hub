"""Three more wire formats, so the tools that cannot speak OpenAI can reach the hub.

ASKED 2026-09-03: compare this hub against tashfeenahmed/freellmapi and fill the
gaps. Its README's second feature bullet is the one that mattered most --
"Native Gemini + Ollama surfaces" -- and an audit of this repo confirmed all
three were genuinely absent, not merely named differently:

    /v1/completions             no route
    /v1beta/*:generateContent   Gemini used OUTBOUND only (app.py, image gen);
                                nothing SERVED the protocol
    /api/tags | /api/chat       absent ("/api/chat/history" is our own feature)

That absence is not cosmetic. Gemini CLI, google-genai, Open WebUI, Enchanted,
Continue's ollama provider and editor ghost-text autocomplete have no
base-URL-plus-OpenAI-key screen at all; they ask for a Gemini endpoint or an
Ollama host. Until now the hub did not exist for any of them.

The translation is where the bugs live, so it is pure and tested directly:

  * Ollama streams by DEFAULT (absent "stream" means true) -- the opposite of
    OpenAI. Backwards, and every client hangs on first use.
  * Ollama tool arguments are an OBJECT; OpenAI's are a JSON STRING. A client
    handed the string silently sees zero tool calls.
  * Ollama images are a sibling `images` array, not inline content parts.
  * Gemini roles are user/model, never assistant.
  * Gemini tool RESULTS arrive as functionResponse parts inside a USER turn; as
    a user message the model answers the JSON instead of continuing the loop.
  * Gemini streams a JSON ARRAY unless ?alt=sse is given.
"""
import json

import wire_gemini as G
import wire_ollama as O


# --------------------------------------------------------------------------- #
# Ollama: catalog
# --------------------------------------------------------------------------- #

def test_tags_lists_models_clients_can_send_straight_back():
    """Whatever is listed here is what comes back as `model` on the next
    request, so a prettified name would break the round trip."""
    p = O.tags_payload([{"id": "auto", "provider": "hub"},
                        {"id": "chutes/glm-5.3", "provider": "chutes"}])
    names = [m["name"] for m in p["models"]]
    assert names == ["auto:latest", "chutes/glm-5.3:latest"]


def test_an_id_that_already_has_a_colon_is_left_alone():
    assert O.tag_name("ollama/qwen3:8b") == "ollama/qwen3:8b"


def test_show_reports_tool_capability():
    """Clients read this to decide whether to offer tool calling at all."""
    assert "tools" in O.show_payload("auto")["capabilities"]


# --------------------------------------------------------------------------- #
# Ollama: request in
# --------------------------------------------------------------------------- #

def test_options_map_onto_openai_sampling_names():
    b = O.chat_to_openai({"model": "auto", "messages": [],
                          "options": {"temperature": 0.4, "num_predict": 256,
                                      "top_p": 0.8, "stop": "END"}})
    assert b["temperature"] == 0.4
    assert b["max_tokens"] == 256           # num_predict, not max_tokens
    assert b["top_p"] == 0.8
    assert b["stop"] == ["END"]


def test_format_json_becomes_a_response_format():
    b = O.chat_to_openai({"messages": [], "format": "json"})
    assert b["response_format"] == {"type": "json_object"}


def test_a_format_schema_becomes_a_json_schema():
    b = O.chat_to_openai({"messages": [], "format": {"type": "object"}})
    assert b["response_format"]["type"] == "json_schema"


def test_images_move_from_the_sibling_array_into_content_parts():
    """Ollama's vision requests are invisible to the OpenAI path until folded in."""
    b = O.chat_to_openai({"messages": [{"role": "user", "content": "what is this",
                                        "images": ["QUJD"]}]})
    parts = b["messages"][0]["content"]
    assert parts[0] == {"type": "text", "text": "what is this"}
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,QUJD")


def test_a_data_url_is_not_double_prefixed():
    b = O.chat_to_openai({"messages": [{"role": "user", "content": "",
                                        "images": ["data:image/jpeg;base64,QUJD"]}]})
    assert b["messages"][0]["content"][0]["image_url"]["url"] == "data:image/jpeg;base64,QUJD"


def test_generate_folds_system_and_prompt_into_messages():
    b = O.generate_to_openai({"model": "auto", "system": "be terse", "prompt": "hi"})
    assert [m["role"] for m in b["messages"]] == ["system", "user"]
    assert b["messages"][1]["content"] == "hi"


def test_tool_results_keep_a_call_id():
    """OpenAI requires tool_call_id; Ollama does not send one."""
    b = O.chat_to_openai({"messages": [{"role": "tool", "name": "get_time",
                                        "content": "12:00"}]})
    assert b["messages"][0]["tool_call_id"] == "get_time"


# --------------------------------------------------------------------------- #
# Ollama: response out
# --------------------------------------------------------------------------- #

def _completion(content="hello", tool_calls=None, finish="stop"):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"choices": [{"message": msg, "finish_reason": finish}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 5}}


def test_a_completion_becomes_an_ollama_chat_object():
    out = O.chat_response(_completion(), "auto", 1_000_000)
    assert out["message"] == {"role": "assistant", "content": "hello"}
    assert out["done"] is True and out["done_reason"] == "stop"
    assert out["prompt_eval_count"] == 3 and out["eval_count"] == 5


def test_tool_call_arguments_become_an_object_again():
    """THE difference that silently breaks tool use: OpenAI sends a JSON string,
    Ollama clients parse an object and see nothing if handed the string."""
    out = O.chat_response(_completion("", [{
        "function": {"name": "search", "arguments": '{"q": "cats"}'}}]), "auto")
    assert out["message"]["tool_calls"][0]["function"]["arguments"] == {"q": "cats"}


def test_unparseable_arguments_are_kept_rather_than_dropped():
    out = O.chat_response(_completion("", [{
        "function": {"name": "f", "arguments": "{not json"}}]), "auto")
    assert out["message"]["tool_calls"][0]["function"]["arguments"] == {"_raw": "{not json"}


def test_generate_returns_response_not_message():
    out = O.generate_response(_completion("hi"), "auto")
    assert out["response"] == "hi" and "message" not in out


def test_the_final_chunk_says_done():
    """Clients spin forever on a stream that ends without it, even though all
    the text arrived."""
    fin = O.final_chunk("auto", "chat", 5, {"completion_tokens": 2})
    assert fin["done"] is True and fin["eval_count"] == 2


def test_lines_are_ndjson_not_sse():
    line = O.ndjson(O.chat_chunk("auto", "hi"))
    assert line.endswith("\n") and not line.startswith("data:")
    assert json.loads(line)["message"]["content"] == "hi"


# --------------------------------------------------------------------------- #
# Gemini: request in
# --------------------------------------------------------------------------- #

def test_system_instruction_becomes_a_system_message():
    msgs = G.contents_to_messages({
        "systemInstruction": {"parts": [{"text": "be terse"}]},
        "contents": [{"role": "user", "parts": [{"text": "hi"}]}]})
    assert msgs[0] == {"role": "system", "content": "be terse"}


def test_the_model_role_becomes_assistant():
    """"model" is Gemini's word for it and OpenAI rejects the request outright."""
    msgs = G.contents_to_messages({"contents": [
        {"role": "user", "parts": [{"text": "hi"}]},
        {"role": "model", "parts": [{"text": "hello"}]}]})
    assert [m["role"] for m in msgs] == ["user", "assistant"]


def test_inline_data_becomes_a_data_url():
    msgs = G.contents_to_messages({"contents": [{"role": "user", "parts": [
        {"text": "what"}, {"inlineData": {"mimeType": "image/jpeg", "data": "QUJD"}}]}]})
    assert msgs[0]["content"][1]["image_url"]["url"] == "data:image/jpeg;base64,QUJD"


def test_a_function_response_becomes_a_tool_message_not_a_user_one():
    """As a user message the model answers the JSON instead of continuing the
    tool loop -- the single most damaging way to get this wrong."""
    msgs = G.contents_to_messages({"contents": [{"role": "user", "parts": [
        {"functionResponse": {"name": "get_time", "response": {"t": "12:00"}}}]}]})
    assert msgs[0]["role"] == "tool"
    assert msgs[0]["tool_call_id"] == "get_time"
    assert json.loads(msgs[0]["content"]) == {"t": "12:00"}


def test_a_function_call_becomes_an_assistant_tool_call():
    msgs = G.contents_to_messages({"contents": [{"role": "model", "parts": [
        {"functionCall": {"name": "search", "args": {"q": "cats"}}}]}]})
    assert msgs[0]["role"] == "assistant"
    assert msgs[0]["tool_calls"][0]["function"]["arguments"] == '{"q": "cats"}'


def test_function_declarations_become_openai_tools():
    tools = G.tools_to_openai([{"functionDeclarations": [
        {"name": "f", "description": "d", "parameters": {"type": "object"}}]}])
    assert tools[0]["type"] == "function" and tools[0]["function"]["name"] == "f"


def test_generation_config_maps_onto_sampling():
    b = G.to_openai({"contents": [], "generationConfig": {
        "temperature": 0.2, "maxOutputTokens": 99, "stopSequences": ["X"]}}, "models/auto")
    assert b["temperature"] == 0.2 and b["max_tokens"] == 99 and b["stop"] == ["X"]
    assert b["model"] == "auto"                      # the models/ prefix is gone


def test_a_response_schema_becomes_structured_output():
    b = G.to_openai({"contents": [], "generationConfig": {
        "responseMimeType": "application/json",
        "responseSchema": {"type": "object"}}}, "auto")
    assert b["response_format"]["type"] == "json_schema"


def test_tool_config_any_means_required():
    b = G.to_openai({"contents": [],
                     "tools": [{"functionDeclarations": [{"name": "f"}]}],
                     "toolConfig": {"functionCallingConfig": {"mode": "ANY"}}}, "auto")
    assert b["tool_choice"] == "required"


def test_a_provider_pinned_id_survives_the_prefix_strip():
    """Our ids contain slashes, so a greedy split would eat the provider."""
    assert G.strip_model_prefix("models/chutes/glm-5.3") == "chutes/glm-5.3"


# --------------------------------------------------------------------------- #
# Gemini: response out
# --------------------------------------------------------------------------- #

def test_a_completion_becomes_candidates():
    out = G.from_openai(_completion("hello"), "auto")
    cand = out["candidates"][0]
    assert cand["content"]["role"] == "model"
    assert cand["content"]["parts"][0]["text"] == "hello"
    assert cand["finishReason"] == "STOP"
    assert out["usageMetadata"]["totalTokenCount"] == 8


def test_length_maps_to_max_tokens():
    out = G.from_openai(_completion("x", finish="length"), "auto")
    assert out["candidates"][0]["finishReason"] == "MAX_TOKENS"


def test_tool_calls_become_function_call_parts():
    out = G.from_openai(_completion("", [{
        "function": {"name": "search", "arguments": '{"q":"cats"}'}}]), "auto")
    parts = out["candidates"][0]["content"]["parts"]
    fc = [p for p in parts if "functionCall" in p][0]
    assert fc["functionCall"] == {"name": "search", "args": {"q": "cats"}}


def test_parts_is_never_empty():
    """candidates[].content.parts is required; an empty list makes SDKs throw."""
    out = G.from_openai({"choices": [{"message": {"content": ""}}]}, "auto")
    assert out["candidates"][0]["content"]["parts"] == [{"text": ""}]


def test_errors_use_googles_envelope():
    """Clients read .error.message; a bare string shows as "undefined"."""
    e = G.error_payload("nope", 429, "RESOURCE_EXHAUSTED")
    assert e["error"]["message"] == "nope" and e["error"]["status"] == "RESOURCE_EXHAUSTED"


def test_count_tokens_shape():
    assert G.count_tokens_payload(42) == {"totalTokens": 42}


def test_models_advertise_the_methods_clients_check_for():
    p = G.models_payload([{"id": "auto", "context_window": 200000}])
    m = p["models"][0]
    assert m["name"] == "models/auto"
    assert "streamGenerateContent" in m["supportedGenerationMethods"]
    assert m["inputTokenLimit"] == 200000


def test_a_model_without_a_known_window_still_advertises_a_real_floor():
    """A zero here makes some clients refuse the model outright."""
    assert G.models_payload([{"id": "x"}])["models"][0]["inputTokenLimit"] > 0
