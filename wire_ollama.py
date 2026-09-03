"""Ollama's wire format, translated to and from OpenAI's.

WHY: a whole class of tools speaks Ollama and ONLY Ollama -- Open WebUI, Enchanted,
Obsidian's local-LLM plugins, Continue's ollama provider, Raycast, LM Studio
importers, half the "local AI" mobile apps. They have no base-URL-plus-key screen
to point at an OpenAI endpoint; they ask for an Ollama host and then call
/api/tags to see what is there. Serving these five routes is the difference
between "not supported" and "works" for all of them at once, and costs no new
routing logic: every request lands on the same _chat_completions seam.

The format is close to OpenAI's but not the same, and the differences are the
whole job:

  * responses are NDJSON, one object per line, not SSE `data:` frames;
  * a chat delta is message.content, a generate delta is response;
  * the terminating object carries done:true plus the token counts, rather than
    a [DONE] sentinel;
  * tool-call arguments are a JSON OBJECT, where OpenAI sends a STRING;
  * sampling lives under options{} with Ollama's own names (num_predict, not
    max_tokens);
  * durations are nanoseconds.

Everything here is pure translation with no I/O, so it is tested directly.
"""
import json
import time

# Ollama clients version-gate their features on this. 0.1.x lacks /api/chat
# tool support in several clients, so report something recent enough that they
# take the modern path, and stable enough that none of them probe for a
# feature that is genuinely absent.
OLLAMA_VERSION = "0.5.7"

_NS = 1_000_000_000


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #

def tag_name(model_id):
    """Ollama names are "family:tag" and clients SPLIT ON THE COLON to show a
    tag badge. Our ids are "provider/model", which contain slashes and often
    colons of their own, so pass the id through untouched and only append the
    ":latest" that clients expect when there is no colon at all -- inventing a
    prettier name would break the round trip, because whatever we list here is
    exactly what comes back in the next request's model field."""
    mid = str(model_id or "")
    return mid if ":" in mid else mid + ":latest"


def tags_payload(models):
    """GET /api/tags -- the list every Ollama client calls first."""
    out = []
    for m in models or []:
        mid = m.get("id") if isinstance(m, dict) else str(m)
        if not mid:
            continue
        out.append({
            "name": tag_name(mid),
            "model": tag_name(mid),
            "modified_at": _now_iso(),
            # Real Ollama reports the on-disk blob size. Nothing is on disk
            # here; 0 is honest and clients render it as unknown rather than
            # inventing a bogus gigabyte figure.
            "size": 0,
            "digest": "",
            "details": {
                "parent_model": "",
                "format": "gguf",
                "family": (m.get("provider") if isinstance(m, dict) else "") or "free-llm-hub",
                "families": None,
                "parameter_size": "",
                "quantization_level": "",
            },
        })
    return {"models": out}


def show_payload(model_id, capabilities=None):
    """POST /api/show -- clients call this to decide whether to offer tools or
    vision, so the capabilities list is the part that actually matters."""
    caps = capabilities or ["completion", "tools"]
    return {
        "license": "",
        "modelfile": "",
        "parameters": "",
        "template": "",
        "details": {
            "parent_model": "",
            "format": "gguf",
            "family": "free-llm-hub",
            "families": None,
            "parameter_size": "",
            "quantization_level": "",
        },
        "model_info": {"general.architecture": "free-llm-hub"},
        "capabilities": caps,
        "modified_at": _now_iso(),
    }


# --------------------------------------------------------------------------- #
# Request in
# --------------------------------------------------------------------------- #

def _sampling(body, openai_body):
    """options{} -> OpenAI's top-level sampling fields, under Ollama's names."""
    opts = body.get("options")
    if not isinstance(opts, dict):
        opts = {}
    if isinstance(opts.get("temperature"), (int, float)):
        openai_body["temperature"] = opts["temperature"]
    if isinstance(opts.get("top_p"), (int, float)):
        openai_body["top_p"] = opts["top_p"]
    if isinstance(opts.get("num_predict"), int) and opts["num_predict"] > 0:
        openai_body["max_tokens"] = opts["num_predict"]   # NOT max_tokens upstream
    if isinstance(opts.get("seed"), int):
        openai_body["seed"] = opts["seed"]
    stop = opts.get("stop")
    if isinstance(stop, list) and stop:
        openai_body["stop"] = stop
    elif isinstance(stop, str) and stop:
        openai_body["stop"] = [stop]
    # `format` is Ollama's structured-output switch and is top-level, not in
    # options. "json" is the only value every client sends.
    if body.get("format") == "json":
        openai_body["response_format"] = {"type": "json_object"}
    elif isinstance(body.get("format"), dict):
        openai_body["response_format"] = {"type": "json_schema",
                                          "json_schema": {"name": "response",
                                                          "schema": body["format"]}}
    return openai_body


def _content_with_images(msg):
    """Ollama puts base64 images in a sibling `images` array rather than inline
    parts, so a vision request from an Ollama client is unrecognisable to the
    OpenAI path until it is folded in."""
    text = msg.get("content")
    images = msg.get("images")
    if not isinstance(images, list) or not images:
        return text if isinstance(text, str) else ""
    parts = []
    if isinstance(text, str) and text:
        parts.append({"type": "text", "text": text})
    for b64 in images:
        if not isinstance(b64, str) or not b64:
            continue
        url = b64 if b64.startswith("data:") else "data:image/png;base64," + b64
        parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts


def chat_to_openai(body):
    """POST /api/chat -> an OpenAI chat-completions body."""
    messages = []
    for m in body.get("messages") or []:
        if not isinstance(m, dict):
            continue
        out = {"role": m.get("role") or "user", "content": _content_with_images(m)}
        # Ollama echoes tool results with role "tool" and a name, same as OpenAI
        # except that tool_call_id is optional there and required here.
        if m.get("role") == "tool":
            out["tool_call_id"] = m.get("tool_call_id") or m.get("name") or "call_0"
        if m.get("tool_calls"):
            out["tool_calls"] = _tool_calls_to_openai(m["tool_calls"])
        messages.append(out)
    openai_body = {"model": body.get("model") or "auto", "messages": messages}
    if isinstance(body.get("tools"), list) and body["tools"]:
        openai_body["tools"] = body["tools"]        # already the OpenAI schema
    return _sampling(body, openai_body)


def generate_to_openai(body):
    """POST /api/generate -> the same, with prompt/system folded into messages."""
    messages = []
    if isinstance(body.get("system"), str) and body["system"]:
        messages.append({"role": "system", "content": body["system"]})
    messages.append({"role": "user",
                     "content": _content_with_images(
                         {"content": body.get("prompt") or "",
                          "images": body.get("images")})})
    openai_body = {"model": body.get("model") or "auto", "messages": messages}
    return _sampling(body, openai_body)


def _tool_calls_to_openai(calls):
    """Ollama arguments are an object; OpenAI's are a JSON string."""
    out = []
    for i, c in enumerate(calls or []):
        if not isinstance(c, dict):
            continue
        fn = c.get("function") or {}
        args = fn.get("arguments")
        if not isinstance(args, str):
            args = json.dumps(args if args is not None else {})
        out.append({"id": c.get("id") or ("call_%d" % i), "type": "function",
                    "function": {"name": fn.get("name") or "", "arguments": args}})
    return out


# --------------------------------------------------------------------------- #
# Response out
# --------------------------------------------------------------------------- #

def _tool_calls_from_openai(calls):
    """...and back: a STRING of JSON becomes an object again. A client that gets
    a string here silently sees zero tool calls."""
    out = []
    for c in calls or []:
        if not isinstance(c, dict):
            continue
        fn = c.get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except ValueError:
                args = {"_raw": args}       # never drop it on the floor
        out.append({"function": {"name": fn.get("name") or "",
                                 "arguments": args if isinstance(args, dict) else {}}})
    return out


_FINISH = {"stop": "stop", "length": "length", "tool_calls": "stop",
           "content_filter": "stop"}


def _usage(data, elapsed_ns):
    u = (data or {}).get("usage") or {}
    return {
        "total_duration": elapsed_ns,
        "load_duration": 0,
        "prompt_eval_count": u.get("prompt_tokens") or 0,
        "prompt_eval_duration": 0,
        "eval_count": u.get("completion_tokens") or 0,
        "eval_duration": elapsed_ns,
    }


def chat_response(data, model, elapsed_ns=0):
    """A whole OpenAI completion -> one Ollama /api/chat object."""
    choice = ((data or {}).get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    out_msg = {"role": "assistant", "content": msg.get("content") or ""}
    if msg.get("tool_calls"):
        out_msg["tool_calls"] = _tool_calls_from_openai(msg["tool_calls"])
    if msg.get("reasoning_content"):
        out_msg["thinking"] = msg["reasoning_content"]
    body = {"model": tag_name(model), "created_at": _now_iso(),
            "message": out_msg, "done": True,
            "done_reason": _FINISH.get(choice.get("finish_reason"), "stop")}
    body.update(_usage(data, elapsed_ns))
    return body


def generate_response(data, model, elapsed_ns=0):
    choice = ((data or {}).get("choices") or [{}])[0]
    body = {"model": tag_name(model), "created_at": _now_iso(),
            "response": (choice.get("message") or {}).get("content") or "",
            "done": True,
            "done_reason": _FINISH.get(choice.get("finish_reason"), "stop"),
            "context": []}
    body.update(_usage(data, elapsed_ns))
    return body


def chat_chunk(model, text, tool_calls=None):
    msg = {"role": "assistant", "content": text or ""}
    if tool_calls:
        msg["tool_calls"] = _tool_calls_from_openai(tool_calls)
    return {"model": tag_name(model), "created_at": _now_iso(),
            "message": msg, "done": False}


def generate_chunk(model, text):
    return {"model": tag_name(model), "created_at": _now_iso(),
            "response": text or "", "done": False}


def final_chunk(model, kind="chat", elapsed_ns=0, usage=None):
    """The done:true line. Clients wait for this to stop spinning, so a stream
    that ends without it hangs their UI even though the text all arrived."""
    body = {"model": tag_name(model), "created_at": _now_iso(),
            "done": True, "done_reason": "stop"}
    body["message" if kind == "chat" else "response"] = (
        {"role": "assistant", "content": ""} if kind == "chat" else "")
    body.update(_usage({"usage": usage or {}}, elapsed_ns))
    return body


def ndjson(obj):
    return json.dumps(obj, ensure_ascii=False) + "\n"


def error_payload(message):
    return {"error": str(message)}
