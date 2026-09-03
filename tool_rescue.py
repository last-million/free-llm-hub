"""Turn a tool call the model TYPED into the tool call it should have emitted.

Plenty of free models know perfectly well which tool to call and with what
arguments, and then write it into the message content instead of emitting it in
the `tool_calls` field -- because they were fine-tuned on a different dialect, or
because the provider's own adapter dropped it. To the client this is prose, so
the CLI executes nothing and the build stops.

The hub already SPOTTED this (_looks_like_text_tool_call) and reacted by marking
the model dead for the TTL and retrying the whole turn on another model. That is
right when the text is unusable. It is wasteful when the text contains a
complete, correct call: a turn is thrown away, a working model is sidelined, and
the user waits through a second inference for an answer we were already holding.

So: parse first, discard only on failure.

SAFETY: a rescued call is only ever emitted when its name is one the CLIENT
offered in this request. A model that invents a tool name has NOT produced a
usable call -- handing it back would make the agent loop fail on an unknown
tool, which is worse than retrying elsewhere -- so those still fall through to
the old path.

Dialects handled, all observed in the wild:

    <tool_call>{"name": "read", "arguments": {"path": "a.txt"}}</tool_call>
    <tool_call>read<arg_key>path</arg_key><arg_value>a.txt</arg_value></tool_call>
    ```json  {"name": "read", "arguments": {...}}  ```
    {"tool_calls": [{"function": {"name": "read", "arguments": "{...}"}}]}
    {"function_call": {"name": "read", "arguments": "{...}"}}
    <function=read>{"path": "a.txt"}</function>

Pure: no I/O, no globals.
"""
import json
import re

_TOOL_CALL_BLOCK = re.compile(r"<tool_call>(.*?)</tool_call>", re.I | re.S)
# An UNCLOSED block still gets a chance: models truncated by max_tokens open the
# tag, write a complete JSON object, and never close it.
_TOOL_CALL_OPEN = re.compile(r"<tool_call>(.*)$", re.I | re.S)
_ARG_PAIR = re.compile(r"<arg_key>(.*?)</arg_key>\s*<arg_value>(.*?)</arg_value>",
                       re.I | re.S)
_FUNCTION_TAG = re.compile(r"<function=([A-Za-z0-9_.-]+)\s*>(.*?)(?:</function>|$)",
                           re.I | re.S)
_FENCE = re.compile(r"```(?:json|tool_code|python|tool_call)?\s*(.*?)```", re.I | re.S)

_NAME_KEYS = ("name", "tool", "tool_name", "function", "command", "recipient_name")
_ARG_KEYS = ("arguments", "args", "parameters", "params", "input", "tool_input")


# --------------------------------------------------------------------------- #
# Finding JSON inside prose
# --------------------------------------------------------------------------- #

def _json_objects(text):
    """Yield every balanced {...} in `text`, outermost first.

    A regex cannot do this: tool arguments nest, and they contain braces inside
    strings. Scanning with a depth counter that knows about strings and escapes
    can, and it is the difference between rescuing a nested argument object and
    truncating it at the first inner brace."""
    i, n = 0, len(text or "")
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth, j, in_str, esc = 0, i, False, False
        while j < n:
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if j < n and depth == 0:
            chunk = text[i:j + 1]
            try:
                yield json.loads(chunk), i, j + 1
            except ValueError:
                pass
            i = j + 1
        else:
            i += 1                     # unbalanced: keep looking past this brace


def _as_arg_string(value):
    """OpenAI tool arguments are a JSON STRING. Models write an object about as
    often as a string, and a client handed the wrong one sees a broken call."""
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return "{}"
        try:
            json.loads(s)
            return s                    # already JSON text
        except ValueError:
            return json.dumps({"input": value})
    if value is None:
        return "{}"
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return "{}"


def _call(name, args):
    return {"name": str(name), "arguments": _as_arg_string(args)}


# --------------------------------------------------------------------------- #
# The dialects
# --------------------------------------------------------------------------- #

def _from_mapping(obj):
    """A dict that might BE a call, or might carry one."""
    if not isinstance(obj, dict):
        return []

    # {"tool_calls": [...]} -- the whole OpenAI field, written into content
    if isinstance(obj.get("tool_calls"), list):
        out = []
        for c in obj["tool_calls"]:
            if not isinstance(c, dict):
                continue
            fn = c.get("function") if isinstance(c.get("function"), dict) else c
            name = fn.get("name")
            if name:
                out.append(_call(name, _first_present(fn, _ARG_KEYS)))
        if out:
            return out

    # {"function_call": {...}} -- the legacy singular field
    fc = obj.get("function_call")
    if isinstance(fc, dict) and fc.get("name"):
        return [_call(fc["name"], _first_present(fc, _ARG_KEYS))]

    # a bare call object
    name = _first_present(obj, _NAME_KEYS)
    if isinstance(name, dict):            # {"function": {"name": ...}}
        inner = name
        if inner.get("name"):
            return [_call(inner["name"], _first_present(inner, _ARG_KEYS))]
        return []
    if isinstance(name, str) and name.strip():
        args = _first_present(obj, _ARG_KEYS)
        if args is None:
            # Some models put the arguments at the top level next to the name.
            args = {k: v for k, v in obj.items()
                    if k not in _NAME_KEYS and k not in _ARG_KEYS}
        return [_call(name.strip(), args)]
    return []


def _first_present(obj, keys):
    for k in keys:
        if k in obj:
            return obj[k]
    return None


def _from_tool_call_block(inner):
    """Whatever is between <tool_call> and </tool_call>."""
    pairs = _ARG_PAIR.findall(inner or "")
    if pairs:
        # the arg_key/arg_value dialect: the name is the leading bare text
        name = _ARG_PAIR.split(inner)[0].strip().strip('"').strip()
        name = name.splitlines()[0].strip() if name else ""
        if name:
            return [_call(name, {k.strip(): v.strip() for k, v in pairs})]
        return []
    for obj, _s, _e in _json_objects(inner or ""):
        calls = _from_mapping(obj)
        if calls:
            return calls
    return []


def parse(text, allowed_names=None):
    """Every rescuable call in `text`, as OpenAI function dicts.

    `allowed_names`: the tool names the client offered. When given, a call to
    anything else is dropped -- an invented name is not a usable call."""
    if not text or not isinstance(text, str):
        return []
    found = []

    for block in _TOOL_CALL_BLOCK.findall(text):
        found.extend(_from_tool_call_block(block))
    if not found:
        m = _TOOL_CALL_OPEN.search(text)
        if m and "</tool_call>" not in text.lower():
            found.extend(_from_tool_call_block(m.group(1)))

    for name, body in _FUNCTION_TAG.findall(text):
        args = None
        for obj, _s, _e in _json_objects(body):
            args = obj
            break
        found.append(_call(name, args if args is not None else body.strip()))

    if not found:
        for fenced in _FENCE.findall(text):
            for obj, _s, _e in _json_objects(fenced):
                found.extend(_from_mapping(obj))
            if found:
                break

    if not found:
        for obj, _s, _e in _json_objects(text):
            found.extend(_from_mapping(obj))

    # de-duplicate: the same call often matches two dialects at once
    seen, unique = set(), []
    for c in found:
        key = (c["name"], c["arguments"])
        if key not in seen:
            seen.add(key)
            unique.append(c)

    if allowed_names is not None:
        allowed = {str(n) for n in allowed_names}
        unique = [c for c in unique if c["name"] in allowed]
    return unique


# --------------------------------------------------------------------------- #
# Applying it
# --------------------------------------------------------------------------- #

def tool_names(tools):
    """The names a client offered, from an OpenAI `tools` array."""
    names = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        if fn.get("name"):
            names.append(str(fn["name"]))
    return names


def _strip_calls(text):
    """Remove the typed call from the prose so the client is not shown raw XML
    next to the real call it now has."""
    out = _TOOL_CALL_BLOCK.sub("", text or "")
    out = _FUNCTION_TAG.sub("", out)
    if "<tool_call>" in out.lower():
        out = _TOOL_CALL_OPEN.sub("", out)
    out = _FENCE.sub("", out)
    return out.strip()


def rescue(data, tools):
    """Promote a typed call in `data` into real tool_calls, in place.

    Returns True when something was rescued. Only ever acts on a response that
    has no tool_calls of its own and a request that actually offered tools."""
    names = tool_names(tools)
    if not names:
        return False
    try:
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
    except (AttributeError, IndexError, TypeError):
        return False
    if not isinstance(msg, dict) or msg.get("tool_calls"):
        return False

    content = msg.get("content")
    if isinstance(content, list):
        content = "".join((p.get("text") or "") for p in content if isinstance(p, dict))
    calls = parse(content, allowed_names=names)
    if not calls:
        return False

    msg["tool_calls"] = [{"id": "call_rescued_%d" % i, "type": "function",
                          "function": c} for i, c in enumerate(calls)]
    left = _strip_calls(content)
    # A model that typed a call and nothing else leaves no prose behind; content
    # must then be None, not "", or strict clients reject the message.
    msg["content"] = left or None
    choice["finish_reason"] = "tool_calls"
    return True
