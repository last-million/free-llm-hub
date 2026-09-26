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

...and the model-NATIVE tool-call syntaxes, which a provider's adapter is meant
to turn into tool_calls and sometimes hands over as text instead (MEASURED
2026-09: DeepSeek V4 on /v1/responses and /v1/messages, non-stream, the reply
text opening "<｜DSML｜" -- fullwidth bars, U+FF5C):

    <｜DSML｜function_calls>
    <｜DSML｜invoke name="add">
    <｜DSML｜parameter name="a" string="false">17</｜DSML｜parameter>
    </｜DSML｜invoke>
    </｜DSML｜function_calls>
    <｜tool▁calls▁begin｜><｜tool▁call▁begin｜>function<｜tool▁sep｜>add
    ```json {"a": 17} ```<｜tool▁call▁end｜><｜tool▁calls▁end｜>   (V3 / R1)
    <｜tool▁call▁begin｜>add<｜tool▁sep｜>{"a": 17}<｜tool▁call▁end｜>   (V3.1)
    <|tool_call_begin|>functions.add:0<|tool_call_argument_begin|>{..}<|tool_call_end|>

ASCII bars and "_" for the "▁" are accepted too. See rescue_stream for the
streaming half.

Pure: no I/O, no globals.
"""
import json
import re
import uuid

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


# --------------------------------------------------------------------------- #
# Model-native markup: DeepSeek DSML, DeepSeek/Kimi special-token calls
# --------------------------------------------------------------------------- #

_BAR = "[｜|]"                # fullwidth vertical line, or the ASCII one
_SP = "[▁_ ]"                # sentencepiece "▁", "_" or a plain space
_DSML_OPEN = r"<\s*" + _BAR + r"\s*DSML\s*" + _BAR + r"\s*"
_DSML_CLOSE = r"<\s*(?:/\s*" + _BAR + r"|" + _BAR + r"\s*/)\s*DSML\s*" + _BAR + r"\s*"


def _special(*words):
    """A <｜tool▁call▁begin｜>-style token, any bar / space spelling."""
    return r"<\s*" + _BAR + r"\s*" + _SP.join(words) + r"\s*" + _BAR + r"\s*>"


_TC_BEGIN = _special("tool", "call", "begin")
_TC_END = _special("tool", "call", "end")
_TCS_END = _special("tool", "calls", "end")
_TC_SEP = "(?:" + _special("tool", "sep") + "|" + _special("tool", "call", "argument", "begin") + ")"

_DSML_INVOKE_RE = re.compile(
    _DSML_OPEN + r"invoke\b([^>]*)>(.*?)(?=" + _DSML_CLOSE + r"invoke\s*>|" + _DSML_OPEN
    + r"invoke\b|" + _DSML_CLOSE + r"function_calls\s*>|\Z)", re.I | re.S)
_DSML_PARAM_RE = re.compile(
    _DSML_OPEN + r"parameter\b([^>]*)>(.*?)(?:" + _DSML_CLOSE + r"parameter\s*>|(?="
    + _DSML_OPEN + r"parameter\b)|\Z)", re.I | re.S)
_ATTR_RE = re.compile(r"([\w-]+)\s*=\s*([\"'])(.*?)\2", re.S)
_SPECIAL_CALL_RE = re.compile(
    _TC_BEGIN + r"(.*?)" + _TC_SEP + r"(.*?)(?=" + _TC_END + "|" + _TC_BEGIN + "|" + _TCS_END
    + r"|\Z)", re.I | re.S)
# Where model-native markup STARTS. Deliberately only the openers a model uses
# for a call: a stray "<|im_end|>" is a template leak (answer_check's job).
_MARKUP_START_RE = re.compile(
    r"<\s*/?\s*" + _BAR + r"\s*/?\s*DSML\s*" + _BAR
    + "|" + _special("tool", "calls", "begin") + "|" + _TC_BEGIN
    + "|" + _special("tool", "calls", "section", "begin"), re.I)
# Every tag of that markup, to find where it ENDS.
_MARKUP_TAG_RE = re.compile(
    r"<\s*/?\s*" + _BAR + r"\s*/?\s*DSML\s*" + _BAR + r"[^>]*>"
    r"|<\s*" + _BAR + r"[^<>\n]{0,48}?" + _BAR + r"\s*>", re.I)
# The openers, normalised (see _norm_marker), for the streaming prefix hold.
_MARKERS = ("<|dsml|", "<|tool_calls_begin|>", "<|tool_call_begin|>",
            "<|tool_calls_section_begin|>")


def has_model_markup(text):
    """True when `text` carries a model-native tool-call opener."""
    return bool(isinstance(text, str) and text and _MARKUP_START_RE.search(text))


def _attrs(blob):
    return {k.lower(): v for k, _q, v in _ATTR_RE.findall(blob or "")}


def _schema_type(schemas, name, param):
    try:
        t = (((schemas or {}).get(name) or {}).get("properties") or {}).get(param, {}).get("type")
    except AttributeError:
        return None
    return t[0] if isinstance(t, list) and t else t


def _dsml_value(raw, attrs, declared):
    """One DSML parameter value. string="true" is verbatim text; "false" is
    JSON. Without the hint the tool's schema decides, then "is it a JSON
    literal"."""
    flag = (attrs.get("string") or "").strip().lower()
    s = raw.strip()
    if flag == "true" or (not flag and declared == "string"):
        return raw if flag == "true" else raw.strip("\r\n")
    if flag == "false" or declared in ("integer", "number", "boolean", "object", "array"):
        try:
            return json.loads(s)
        except ValueError:
            return s
    if s[:1] in '{["-0123456789' or s in ("true", "false", "null"):
        try:
            return json.loads(s)
        except ValueError:
            pass
    return raw.strip("\r\n")


def _dsml_calls(text, schemas=None):
    out = []
    for attr_blob, body in _DSML_INVOKE_RE.findall(text):
        name = (_attrs(attr_blob).get("name") or "").strip()
        if not name:
            continue
        args = {}
        for p_blob, raw in _DSML_PARAM_RE.findall(body):
            pa = _attrs(p_blob)
            key = (pa.get("name") or "").strip()
            if key:
                args[key] = _dsml_value(raw, pa, _schema_type(schemas, name, key))
        out.append(_call(name, args))
    return out


def _special_calls(text):
    out = []
    for head, body in _SPECIAL_CALL_RE.findall(text):
        head = head.strip()
        if head.lower() in ("", "function"):
            # V3/R1: <｜tool▁call▁begin｜>function<｜tool▁sep｜>NAME\n```json {...}```
            name, _nl, rest = body.strip().partition("\n")
            if not _nl and "{" in name:
                name, rest = name[:name.index("{")], name[name.index("{"):]
        else:
            name, rest = head, body       # V3.1 / Kimi: NAME<sep>{...}
        # Kimi spells the name "functions.add:0"
        name = re.sub(r":\d+$", "", name.strip().strip("`\"'"))
        if name.startswith("functions."):
            name = name[len("functions."):]
        if not name or re.search(r"\s", name):
            continue
        args = next((obj for obj, _s, _e in _json_objects(rest)), None)
        leftover = _FENCE_MARKS.sub("", rest).strip()
        if args is None and leftover:
            continue                      # arguments that are not JSON: unusable
        out.append(_call(name, args if args is not None else {}))
    return out


_FENCE_MARKS = re.compile(r"```(?:json)?", re.I)


def model_markup_calls(text, schemas=None):
    """Calls in model-native markup (see the module docstring), unfiltered."""
    if not has_model_markup(text):
        return []
    return _dsml_calls(text, schemas) or _special_calls(text)


def strip_model_markup(text):
    """`text` minus the model-native markup: from its first opener to the end of
    its last tag. Prose on either side survives."""
    m = _MARKUP_START_RE.search(text or "")
    if not m:
        return text or ""
    end = m.end()
    for t in _MARKUP_TAG_RE.finditer(text, m.start()):
        end = max(end, t.end())
    return (text[:m.start()] + text[end:]).strip()


def tool_schemas(tools):
    """{name: parameters-schema} from an OpenAI (or Anthropic) tools array."""
    out = {}
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        params = fn.get("parameters") or fn.get("input_schema")
        if fn.get("name") and isinstance(params, dict):
            out[str(fn["name"])] = params
    return out


def parse(text, allowed_names=None, schemas=None):
    """Every rescuable call in `text`, as OpenAI function dicts.

    `allowed_names`: the tool names the client offered. When given, a call to
    anything else is dropped -- an invented name is not a usable call.
    `schemas` ({name: parameters}) only refines DSML value typing."""
    if not text or not isinstance(text, str):
        return []
    found = []

    if has_model_markup(text):
        # Model-native markup owns the text: the generic JSON fallback below
        # would read an ARGUMENTS object such as {"name": "x"} as a call to x.
        found = model_markup_calls(text, schemas)
        if allowed_names is not None:
            allowed = {str(n) for n in allowed_names}
            found = [c for c in found if c["name"] in allowed]
        return found

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
    if has_model_markup(text):
        return strip_model_markup(text)
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
    calls = parse(content, allowed_names=names, schemas=tool_schemas(tools))
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


# --------------------------------------------------------------------------- #
# The streaming half
# --------------------------------------------------------------------------- #
#
# A streamed reply that types model-native markup reaches the client as text
# deltas, and nothing downstream can take them back. So the markup is caught
# on the UPSTREAM chat SSE, before the hub's first-content peek: text flows
# through untouched until an opener (or a prefix of one) shows up, from there
# the text is held, and when the upstream finishes the held markup becomes
# tool_calls deltas + finish_reason "tool_calls" -- the shape every protocol
# translator already turns into function_call items / tool_use blocks.
# Unparseable markup becomes an in-stream error frame instead: before the peek
# commits, that is the peek's "error" status (next hop); after, the
# translators end the turn on it. Never the raw markup as text.

_FRAME_CAP = 1 << 20
_STREAM_ERROR = {"error": {"message": "the model wrote a tool call as model-native markup "
                                      "the hub could not parse",
                           "type": "upstream_error", "code": "unparseable_tool_markup"}}


def _norm_marker(s):
    return re.sub(r"\s+", "", s.lower().replace("｜", "|").replace("▁", "_"))


def _may_open_marker(text):
    """True when `text` ENDS with what could still become an opener."""
    i = text.rfind("<", max(0, len(text) - 48))
    if i < 0:
        return False
    tail = _norm_marker(text[i:])
    return any(m.startswith(tail) for m in _MARKERS)


def _units(items, framing):
    if framing == "lines":
        yield from items
        return
    buf = b""
    for chunk in items:
        if not chunk:
            continue
        if not isinstance(chunk, (bytes, bytearray)):
            chunk = str(chunk).encode("utf-8", "ignore")
        buf += chunk
        while True:
            cuts = [c for c in (buf.find(b"\n\n"), buf.find(b"\r\n\r\n")) if c >= 0]
            if not cuts:
                break
            cut = min(cuts)
            sep = 4 if buf[cut:cut + 4] == b"\r\n\r\n" else 2
            frame, buf = buf[:cut + sep], buf[cut + sep:]
            yield frame
        if len(buf) > _FRAME_CAP:
            yield buf
            buf = b""
    if buf:
        yield buf


def _payload(unit):
    """("json", dict) | ("done", None) | ("other", None) for one SSE unit."""
    raw = unit if isinstance(unit, (bytes, bytearray)) else str(unit).encode("utf-8", "ignore")
    datas = [ln.strip()[5:].strip() for ln in raw.splitlines() if ln.strip().startswith(b"data:")]
    if len(datas) != 1:
        return "other", None
    if datas[0] == b"[DONE]":
        return "done", None
    try:
        obj = json.loads(datas[0].decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return "other", None
    return ("json", obj) if isinstance(obj, dict) else ("other", None)


def _choice0(chunk):
    ch = (chunk or {}).get("choices") if isinstance(chunk, dict) else None
    if isinstance(ch, list) and ch and isinstance(ch[0], dict):
        return ch[0]
    return {}


def rescue_stream(items, tools, framing="lines"):
    """Wrap an upstream OpenAI chat SSE iterator; see the note above.

    `framing`: "lines" for resp.iter_lines() items (one SSE line each), "frames"
    for resp.iter_content() chunks (re-framed on the blank line). Anything that
    is not markup is re-emitted as its original bytes. No-op without tools."""
    names = tool_names(tools)
    if not names:
        yield from items
        return
    schemas = tool_schemas(tools)
    end = b"\n\n" if framing == "frames" else b""

    def emit(obj):
        return b"data: " + json.dumps(obj).encode("utf-8") + end

    template = {}

    def chunk(delta, finish=None, usage=None):
        c = {"id": template.get("id") or "chatcmpl-rescued",
             "object": "chat.completion.chunk",
             "created": template.get("created") or 0,
             "model": template.get("model") or "",
             "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        if usage:
            c["usage"] = usage
        return emit(c)

    mode = "pass"          # pass | hold (maybe an opener) | markup | done
    held, pending, markup, tail = [], "", "", []
    real_calls = False

    def finalize(fin_obj):
        calls = parse(markup, allowed_names=names, schemas=schemas)
        usage = (fin_obj or {}).get("usage") if isinstance(fin_obj, dict) else None
        if real_calls:
            # It emitted real calls as well: the markup is only noise now.
            yield from tail
            yield chunk({}, "tool_calls", usage)
            return
        if not calls:
            yield emit(_STREAM_ERROR)
            return
        rest = strip_model_markup(markup)
        if rest:
            yield chunk({"content": rest})
        for i, c in enumerate(calls):
            yield chunk({"tool_calls": [{"index": i, "id": "call_" + uuid.uuid4().hex[:24],
                                         "type": "function", "function": c}]})
        yield from tail
        yield chunk({}, "tool_calls", usage)

    for unit in _units(items, framing):
        kind, obj = _payload(unit)
        ch = _choice0(obj) if kind == "json" else {}
        delta = ch.get("delta") if isinstance(ch.get("delta"), dict) else {}
        text = delta.get("content") if isinstance(delta.get("content"), str) else ""
        fin = ch.get("finish_reason")
        if kind == "json" and ch:
            template = obj
        if mode == "done":
            yield unit
            continue
        if mode == "markup":
            markup += text
            if delta.get("tool_calls"):
                real_calls = True
                tail.append(unit)
            elif not text and not fin and kind != "done":
                tail.append(unit)
            if fin or kind == "done":
                yield from finalize(obj if fin else None)
                mode = "done"
                if kind == "done":
                    yield unit
            continue
        if mode == "hold":
            if not text:
                # Nothing completes an opener across a non-text unit.
                yield from held
                held, pending, mode = [], "", "pass"
                yield unit
                continue
            held.append(unit)
            pending += text
            text, held_text = pending, True
        else:
            held_text = False
        if not text:
            yield unit
            continue
        m = _MARKUP_START_RE.search(text)
        if m:
            if text[:m.start()]:
                yield chunk({"content": text[:m.start()]})
            markup, held, pending, mode = text[m.start():], [], "", "markup"
            if fin:
                yield from finalize(obj)
                mode = "done"
            continue
        if not fin and _may_open_marker(text):
            if not held_text:
                held, pending = [unit], text
            mode = "hold"
            continue
        if held_text:
            yield from held
            held, pending = [], ""
        else:
            yield unit
        mode = "pass"
    if mode == "hold":
        yield from held
    elif mode == "markup":
        yield from finalize(None)
