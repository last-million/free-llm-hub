"""Google's generateContent wire format, translated to and from OpenAI's.

WHY: Gemini CLI, the google-genai SDKs, Android/Firebase AI clients and a
growing pile of "point this at a Gemini endpoint" tools speak only this. They
send `contents`, not `messages`; they expect `candidates`, not `choices`. None
of them can be configured to talk OpenAI, so without these routes the hub is
invisible to all of them.

The hub already CONSUMES Gemini as an upstream provider. That is the opposite
direction and shares no code with this: here the hub is the server.

Differences that actually bite, all handled below:

  * roles are user/model, never assistant, and a stray "assistant" makes the
    API reject the whole request;
  * system prompts are a separate systemInstruction object, not a message;
  * images are inlineData{mimeType,data}, not a data: URL;
  * tool calls are functionCall{name,args} with args as an OBJECT, and results
    come back as functionResponse parts inside a USER turn, not a tool role;
  * sampling lives in generationConfig under different names (maxOutputTokens,
    stopSequences);
  * streaming is SSE only when the caller adds ?alt=sse -- otherwise it is a
    JSON array delivered incrementally, which is what Gemini CLI actually uses.

Pure translation, no I/O.
"""
import json

# What we advertise per model in /v1beta/models. Gemini clients read these to
# size their own context management, and a missing/zero value makes some of them
# refuse the model outright, so publish a real floor rather than nothing.
DEFAULT_INPUT_LIMIT = 128000
DEFAULT_OUTPUT_LIMIT = 8192

_METHODS = ["generateContent", "streamGenerateContent", "countTokens"]

_FINISH = {"stop": "STOP", "length": "MAX_TOKENS", "tool_calls": "STOP",
           "content_filter": "SAFETY", "function_call": "STOP"}


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #

def model_entry(model_id, context_window=None, display=None):
    return {
        "name": "models/" + str(model_id),
        "baseModelId": str(model_id),
        "version": "001",
        "displayName": display or str(model_id),
        "description": "Served by free-llm-hub",
        "inputTokenLimit": int(context_window or DEFAULT_INPUT_LIMIT),
        "outputTokenLimit": DEFAULT_OUTPUT_LIMIT,
        "supportedGenerationMethods": list(_METHODS),
        "temperature": 1.0,
        "topP": 0.95,
        "topK": 64,
    }


def models_payload(models):
    out = []
    for m in models or []:
        if isinstance(m, dict):
            out.append(model_entry(m.get("id"), m.get("context_window")))
        elif m:
            out.append(model_entry(m))
    return {"models": out}


def strip_model_prefix(name):
    """Clients address models as "models/<id>", and Gemini CLI sometimes sends
    the bare id. Our ids are "provider/model", so the prefix must be stripped
    exactly once and never with a greedy split."""
    n = str(name or "").strip()
    if n.startswith("models/"):
        n = n[len("models/"):]
    return n


# --------------------------------------------------------------------------- #
# Request in
# --------------------------------------------------------------------------- #

def _part_to_openai(part, text_acc, parts_acc):
    """One Gemini part -> either accumulated text or an OpenAI content part."""
    if not isinstance(part, dict):
        return
    if isinstance(part.get("text"), str):
        text_acc.append(part["text"])
        parts_acc.append({"type": "text", "text": part["text"]})
        return
    inline = part.get("inlineData") or part.get("inline_data")
    if isinstance(inline, dict) and inline.get("data"):
        mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
        parts_acc.append({"type": "image_url",
                          "image_url": {"url": "data:%s;base64,%s" % (mime, inline["data"])}})
        return
    fd = part.get("fileData") or part.get("file_data")
    if isinstance(fd, dict) and fd.get("fileUri"):
        parts_acc.append({"type": "image_url", "image_url": {"url": fd["fileUri"]}})


def _tool_calls_from_parts(parts):
    """functionCall parts -> OpenAI tool_calls (args object -> JSON string)."""
    calls = []
    for i, p in enumerate(parts or []):
        if not isinstance(p, dict):
            continue
        fc = p.get("functionCall") or p.get("function_call")
        if not isinstance(fc, dict):
            continue
        calls.append({"id": "call_%d" % i, "type": "function",
                      "function": {"name": fc.get("name") or "",
                                   "arguments": json.dumps(fc.get("args") or {})}})
    return calls


def contents_to_messages(body):
    """contents[] + systemInstruction -> OpenAI messages[]."""
    messages = []
    sysi = body.get("systemInstruction") or body.get("system_instruction")
    if isinstance(sysi, dict):
        text = "".join(p.get("text") or "" for p in (sysi.get("parts") or [])
                       if isinstance(p, dict))
        if text:
            messages.append({"role": "system", "content": text})
    elif isinstance(sysi, str) and sysi:
        messages.append({"role": "system", "content": sysi})

    for turn in body.get("contents") or []:
        if not isinstance(turn, dict):
            continue
        parts = turn.get("parts") or []
        role = turn.get("role") or "user"

        # A tool RESULT arrives as a functionResponse part inside a user turn.
        # Emitting it as a user message would make the model answer the JSON
        # instead of continuing the tool loop.
        responses = [p for p in parts if isinstance(p, dict)
                     and (p.get("functionResponse") or p.get("function_response"))]
        if responses:
            for p in responses:
                fr = p.get("functionResponse") or p.get("function_response")
                messages.append({
                    "role": "tool",
                    "tool_call_id": fr.get("name") or "call_0",
                    "content": json.dumps(fr.get("response") or {}),
                })
            continue

        calls = _tool_calls_from_parts(parts)
        if calls:
            messages.append({"role": "assistant", "content": None, "tool_calls": calls})
            continue

        text_acc, parts_acc = [], []
        for p in parts:
            _part_to_openai(p, text_acc, parts_acc)
        has_image = any(x.get("type") == "image_url" for x in parts_acc)
        content = parts_acc if has_image else "".join(text_acc)
        messages.append({"role": "assistant" if role == "model" else "user",
                         "content": content})
    return messages


def tools_to_openai(tools):
    """[{functionDeclarations:[...]}] -> [{type:function, function:{...}}]."""
    out = []
    for group in tools or []:
        if not isinstance(group, dict):
            continue
        decls = group.get("functionDeclarations") or group.get("function_declarations") or []
        for d in decls:
            if not isinstance(d, dict) or not d.get("name"):
                continue
            out.append({"type": "function", "function": {
                "name": d["name"],
                "description": d.get("description") or "",
                "parameters": d.get("parameters") or {"type": "object", "properties": {}},
            }})
    return out


def _tool_choice(body):
    cfg = ((body.get("toolConfig") or body.get("tool_config") or {})
           .get("functionCallingConfig")
           or (body.get("toolConfig") or {}).get("function_calling_config") or {})
    mode = str(cfg.get("mode") or "").upper()
    if mode == "NONE":
        return "none"
    if mode == "ANY":
        return "required"
    return None                                  # AUTO / unset: leave it alone


def to_openai(body, model):
    """A whole generateContent request -> an OpenAI chat-completions body."""
    out = {"model": strip_model_prefix(model) or "auto",
           "messages": contents_to_messages(body)}
    cfg = body.get("generationConfig") or body.get("generation_config") or {}
    if isinstance(cfg, dict):
        if isinstance(cfg.get("temperature"), (int, float)):
            out["temperature"] = cfg["temperature"]
        if isinstance(cfg.get("topP"), (int, float)):
            out["top_p"] = cfg["topP"]
        for k in ("maxOutputTokens", "max_output_tokens"):
            if isinstance(cfg.get(k), int) and cfg[k] > 0:
                out["max_tokens"] = cfg[k]
                break
        stops = cfg.get("stopSequences") or cfg.get("stop_sequences")
        if isinstance(stops, list) and stops:
            out["stop"] = stops
        mime = cfg.get("responseMimeType") or cfg.get("response_mime_type")
        schema = cfg.get("responseSchema") or cfg.get("response_schema")
        if schema:
            out["response_format"] = {"type": "json_schema",
                                      "json_schema": {"name": "response", "schema": schema}}
        elif mime == "application/json":
            out["response_format"] = {"type": "json_object"}
    tools = tools_to_openai(body.get("tools"))
    if tools:
        out["tools"] = tools
        choice = _tool_choice(body)
        if choice:
            out["tool_choice"] = choice
    return out


# --------------------------------------------------------------------------- #
# Response out
# --------------------------------------------------------------------------- #

def _parts_from_message(msg):
    parts = []
    if msg.get("content"):
        parts.append({"text": msg["content"]})
    for c in msg.get("tool_calls") or []:
        if not isinstance(c, dict):
            continue
        fn = c.get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except ValueError:
                args = {"_raw": args}
        parts.append({"functionCall": {"name": fn.get("name") or "",
                                       "args": args if isinstance(args, dict) else {}}})
    if not parts:
        parts.append({"text": ""})               # candidates[].content.parts is required
    return parts


def _usage(data):
    u = (data or {}).get("usage") or {}
    p = u.get("prompt_tokens") or 0
    c = u.get("completion_tokens") or 0
    return {"promptTokenCount": p, "candidatesTokenCount": c,
            "totalTokenCount": u.get("total_tokens") or (p + c)}


def from_openai(data, model):
    """A whole OpenAI completion -> one generateContent response."""
    choice = ((data or {}).get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    return {
        "candidates": [{
            "content": {"parts": _parts_from_message(msg), "role": "model"},
            "finishReason": _FINISH.get(choice.get("finish_reason"), "STOP"),
            "index": 0,
            "safetyRatings": [],
        }],
        "usageMetadata": _usage(data),
        "modelVersion": strip_model_prefix(model),
    }


def stream_chunk(text, model, finish=None, tool_calls=None, usage=None):
    """One incremental candidate. Gemini streams whole response objects, not
    deltas of a different shape, so this is the same envelope with one part."""
    parts = [{"text": text or ""}] if not tool_calls else _parts_from_message(
        {"content": text or "", "tool_calls": tool_calls})
    cand = {"content": {"parts": parts, "role": "model"}, "index": 0}
    if finish:
        cand["finishReason"] = _FINISH.get(finish, "STOP")
    out = {"candidates": [cand], "modelVersion": strip_model_prefix(model)}
    if usage:
        out["usageMetadata"] = _usage({"usage": usage})
    return out


def count_tokens_payload(total):
    return {"totalTokens": int(total or 0)}


def error_payload(message, status=400, reason="INVALID_ARGUMENT"):
    """Google's error envelope. Clients parse .error.message and show it, so a
    plain string here would surface as "undefined" in Gemini CLI."""
    return {"error": {"code": int(status), "message": str(message), "status": reason}}
