"""Context-window helpers for the gateway -- the pure half.

Everything here is side-effect free apart from the recap store, and imports
nothing from app.py, so it can be tested without Flask or a provider in sight.
app.py owns the glue (which window a model has, when to compact, what to tell
the client); this module owns the parts that are just text and arithmetic:

  * token estimation for text that is NOT English prose (CJK, Arabic, ...) and
    for images whose size can be read from their own header bytes;
  * which user message is a real instruction, as opposed to the wrapper blocks
    CLIs inject (Claude Code's <system-reminder>, Codex's AGENTS.md and
    <environment_context>);
  * the conversation id a terminal CLI carries on /v1 (opencode, Codex, Claude
    Code) -- the key the rolling compaction recap is filed under;
  * the persistent, bounded recap store itself;
  * telling an OUTPUT cap ("max_tokens must be <= 8192") apart from an INPUT
    window, so the first is never learned as the second;
  * each protocol's native "context too long" error body;
  * rewriting `usage` on an OpenAI chat SSE stream so a client sees the size of
    the request IT sent, not of the compacted one the hub forwarded.
"""
import base64
import binascii
import hashlib
import json
import os
import re
import struct
import threading
import time
from collections import OrderedDict

# --------------------------------------------------------------------------- #
# Token estimation for non-Latin text
# --------------------------------------------------------------------------- #

# chars/4 is calibrated on English. A CJK character, a kana or a hangul syllable
# is roughly ONE token on every current tokenizer, so chars/4 under-counts that
# text about four times -- and an under-estimate is exactly the error that turns
# into a 413 (the estimate is used to keep requests UNDER windows). Arabic,
# Hebrew, Devanagari, Cyrillic and Greek sit in between (~0.4-0.7 tok/char).
_DENSE_RE = re.compile(
    "[฀-๿຀-໿က-႟ᄀ-ᇿក-៿"
    "⺀-⿟　-〿぀-ヿ㄀-ㄯ㄰-㆏"
    "ㆠ-ㇿ㐀-䶿一-鿿ꥠ-꥿가-퟿"
    "豈-﫿＀-￯\U00020000-\U0003134f\U0001f300-\U0001faff]")
_MID_RE = re.compile(
    "[Ͱ-ϿЀ-ԯ԰-֏֐-׿؀-ۿ"
    "܀-ࣿऀ-෿Ⴀ-ჿሀ-፿יִ-﷿"
    "ﹰ-﻿]")
DENSE_TOKENS_PER_CHAR = 1.0
MID_TOKENS_PER_CHAR = 0.5
_BASE_TOKENS_PER_CHAR = 0.25


def nonlatin_extra_tokens(text):
    """Tokens a string costs BEYOND the chars/4 every character is already
    charged. 0.0 for ASCII (the common case, checked in C)."""
    if not text or not isinstance(text, str) or text.isascii():
        return 0.0
    n = len(text)
    dense = n - len(_DENSE_RE.sub("", text))
    mid = n - len(_MID_RE.sub("", text))
    return (dense * (DENSE_TOKENS_PER_CHAR - _BASE_TOKENS_PER_CHAR)
            + mid * (MID_TOKENS_PER_CHAR - _BASE_TOKENS_PER_CHAR))


# --------------------------------------------------------------------------- #
# Image tokens
# --------------------------------------------------------------------------- #

IMAGE_TOKENS_DEFAULT = 1000        # the old flat allowance, for unreadable sizes
IMAGE_TOKENS_LOW = 85              # OpenAI "detail: low" is a fixed 85
_IMAGE_TOKENS_MIN, _IMAGE_TOKENS_MAX = 85, 1600
_IMAGE_LONG_EDGE = 1568            # providers downscale past this before counting
_B64_PEEK = 131072                 # base64 chars decoded to find a JPEG's SOF


def _image_dims(raw):
    """(width, height) from an image's own header bytes, or None."""
    try:
        if raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) >= 24:
            return struct.unpack(">II", raw[16:24])
        if raw[:6] in (b"GIF87a", b"GIF89a") and len(raw) >= 10:
            return struct.unpack("<HH", raw[6:10])
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP" and len(raw) >= 30:
            kind = raw[12:16]
            if kind == b"VP8X":
                w = 1 + int.from_bytes(raw[24:27], "little")
                h = 1 + int.from_bytes(raw[27:30], "little")
                return w, h
            if kind == b"VP8 ":
                w, h = struct.unpack("<HH", raw[26:30])
                return w & 0x3FFF, h & 0x3FFF
            if kind == b"VP8L":
                b = raw[21:25]
                w = 1 + (((b[1] & 0x3F) << 8) | b[0])
                h = 1 + (((b[3] & 0x0F) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
                return w, h
        if raw[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(raw):
                if raw[i] != 0xFF:
                    i += 1
                    continue
                marker = raw[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                seg = struct.unpack(">H", raw[i + 2:i + 4])[0]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                              0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", raw[i + 5:i + 9])
                    return w, h
                i += 2 + seg
    except (struct.error, IndexError, ValueError):
        return None
    return None


def _decode_b64_head(data):
    data = (data or "").strip()[:_B64_PEEK]
    data = data[: len(data) - (len(data) % 4)]
    try:
        return base64.b64decode(data, validate=False)
    except (binascii.Error, ValueError):
        return b""


def image_tokens(url=None, detail=None, b64=None):
    """Estimated tokens for one image. Reads the real dimensions from a data
    URL / raw base64 payload when it can (PNG, GIF, WEBP, JPEG); falls back to
    the old flat allowance for a remote URL or anything unreadable."""
    if isinstance(detail, str) and detail.lower() == "low":
        return IMAGE_TOKENS_LOW
    raw = b""
    if isinstance(b64, str) and b64:
        raw = _decode_b64_head(b64)
    elif isinstance(url, str) and url.startswith("data:") and "," in url:
        head, _, payload = url.partition(",")
        if ";base64" in head:
            raw = _decode_b64_head(payload)
    dims = _image_dims(raw) if raw else None
    if not dims or dims[0] <= 0 or dims[1] <= 0:
        return IMAGE_TOKENS_DEFAULT
    w, h = dims
    long_edge = max(w, h)
    if long_edge > _IMAGE_LONG_EDGE:
        scale = _IMAGE_LONG_EDGE / float(long_edge)
        w, h = max(1, int(w * scale)), max(1, int(h * scale))
    return max(_IMAGE_TOKENS_MIN, min(_IMAGE_TOKENS_MAX, -(-(w * h) // 750)))


# --------------------------------------------------------------------------- #
# Real instructions vs CLI wrapper blocks
# --------------------------------------------------------------------------- #

# Blocks a CLI injects into a USER message that are not the user speaking:
# Claude Code's <system-reminder>, Codex's <environment_context> and its
# AGENTS.md <INSTRUCTIONS> / <user_instructions>, slash-command echoes.
_WRAPPER_BLOCK_RE = re.compile(
    r"<(system-reminder|environment_context|user_instructions|INSTRUCTIONS|"
    r"user-prompt-submit-hook|command-name|command-message|command-args|"
    r"local-command-stdout|local-command-stderr|turn_aborted)>.*?</\1>",
    re.S | re.I)
_WRAPPER_LINE_RE = re.compile(
    r"^\s*#\s*AGENTS\.md instructions for .*$|"
    r"^\s*Caveat: The messages below were generated by the user while running "
    r"local commands.*$", re.M | re.I)


def message_text(msg):
    """Plain text of an OpenAI-shaped message (string or content parts)."""
    if not isinstance(msg, dict):
        return ""
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join((p.get("text") or "") for p in c
                        if isinstance(p, dict) and isinstance(p.get("text"), str))
    return ""


def instruction_text(text):
    """What is left of a user message once CLI wrapper blocks are removed."""
    if not isinstance(text, str) or not text:
        return ""
    out = _WRAPPER_BLOCK_RE.sub("", text)
    out = _WRAPPER_LINE_RE.sub("", out)
    return out.strip()


def is_real_instruction(msg):
    """True for a user message that carries something the USER said -- not a
    tool result (role "tool" in the OpenAI shape), not a pure reminder or
    environment block."""
    if not isinstance(msg, dict) or msg.get("role") != "user":
        return False
    return bool(instruction_text(message_text(msg)))


# A CLI asking for its OWN compaction. Such a request must always be served
# (compacted by the hub if need be), never answered with "context too long":
# that error is the CLI's cue to compact, and the compaction request itself
# failing with it would leave the CLI no way out.
_COMPACTION_REQUEST_RE = re.compile(
    r"CONTEXT CHECKPOINT COMPACTION|"
    r"create a detailed summary of the conversation|"
    r"(?:summary|prompt) for continuing (?:our|the|this) conversation|"
    r"summari[sz]e (?:the|this|our) (?:entire |whole )?conversation|"
    r"handoff summary", re.I)


def is_compaction_request(messages):
    """True when the latest user text is a CLI's own compaction prompt."""
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get("role") == "user":
            t = message_text(m)
            if t.strip():
                return bool(_COMPACTION_REQUEST_RE.search(t[-6000:]))
    for m in messages or []:
        if isinstance(m, dict) and m.get("role") == "system":
            if _COMPACTION_REQUEST_RE.search(message_text(m)[:6000]):
                return True
    return False


# --------------------------------------------------------------------------- #
# Conversation identity
# --------------------------------------------------------------------------- #

# Header names carrying a CLI's own session / conversation id. Dashes only:
# werkzeug's dev server (what the hub runs on) DROPS any header whose name
# contains an underscore, so Codex's `session_id` / `conversation_id` headers
# never reach Flask -- Codex is identified by its body's prompt_cache_key.
SESSION_HEADERS = (
    "X-Claude-Code-Session-Id",      # Claude Code
    "X-Session-Id",                  # OpenCode (and several SDKs)
    "x-session-affinity",            # OpenCode
    "x-opencode-session",
    "Session-Id", "Conversation-Id", "X-Conversation-Id",
    "X-Codex-Session-Id", "X-Codex-Conversation-Id",
)
_SESSION_IN_USER_ID_RE = re.compile(
    r"session[_-]([0-9a-fA-F][0-9a-fA-F-]{7,63})")
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._:-]")


def _clean_id(value):
    v = _SAFE_ID_RE.sub("", str(value or "").strip())[:120]
    return v or None


def _session_from_metadata(meta):
    """Claude Code's metadata.user_id: 'user_<hash>_account_<uuid>_session_<uuid>'
    (older) or a JSON string with a session_id (newer)."""
    if not isinstance(meta, dict):
        return None
    for k in ("session_id", "conversation_id"):
        if meta.get(k):
            return _clean_id(meta.get(k))
    uid = meta.get("user_id")
    if isinstance(uid, str) and uid:
        if uid.lstrip().startswith("{"):
            try:
                obj = json.loads(uid)
                if isinstance(obj, dict) and obj.get("session_id"):
                    return _clean_id(obj["session_id"])
            except ValueError:
                pass
        m = _SESSION_IN_USER_ID_RE.search(uid)
        if m:
            return _clean_id(m.group(1))
    return None


def fallback_conversation_key(messages):
    """A stable id for a conversation that carries none: its system prompt head
    plus its first REAL instruction (Codex's AGENTS.md / environment blocks are
    identical for every session in a folder, so they alone would collide)."""
    sys_head, first = "", ""
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "system" and not sys_head:
            sys_head = message_text(m)[:2000]
        elif is_real_instruction(m):
            first = instruction_text(message_text(m))[:2000]
            break
    if not first and not sys_head:
        return None
    h = hashlib.sha256((sys_head + "\x00" + first).encode("utf-8", "replace"))
    return "h:" + h.hexdigest()[:32]


def conversation_key(headers=None, body=None, agent_sid=None, messages=None):
    """The conversation this request belongs to, in order of reliability: the
    hub's own agent session, a CLI session header, the body's own id (Codex
    prompt_cache_key, Claude Code metadata), then a content hash."""
    if agent_sid:
        return "agent:" + str(agent_sid)[:120]
    if headers is not None:
        for name in SESSION_HEADERS:
            try:
                val = headers.get(name)
            except Exception:                                    # noqa: BLE001
                val = None
            cid = _clean_id(val) if val else None
            if cid:
                return "hdr:" + cid
    if isinstance(body, dict):
        pck = body.get("prompt_cache_key")
        if isinstance(pck, str) and _clean_id(pck):
            return "pck:" + _clean_id(pck)
        for k in ("conversation_id", "session_id"):
            if isinstance(body.get(k), str) and _clean_id(body.get(k)):
                return "body:" + _clean_id(body.get(k))
        sid = _session_from_metadata(body.get("metadata"))
        if sid:
            return "meta:" + sid
    return fallback_conversation_key(messages)


def message_hash(msg):
    """Short identity of one message, stable across turns (CLIs resend the
    same history each turn)."""
    if not isinstance(msg, dict):
        return ""
    parts = [str(msg.get("role") or ""), message_text(msg)[:4000],
             str(msg.get("tool_call_id") or "")]
    for tc in msg.get("tool_calls") or []:
        if isinstance(tc, dict):
            fn = tc.get("function") or {}
            parts.append(str(tc.get("id") or ""))
            parts.append(str(fn.get("name") or ""))
            parts.append(str(fn.get("arguments") or "")[:1000])
    return hashlib.sha1("\x1f".join(parts).encode("utf-8", "replace")).hexdigest()[:16]


def head_hash(messages, n=3):
    return hashlib.sha1("|".join(message_hash(m) for m in (messages or [])[:n])
                        .encode("ascii")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Persistent, bounded recap store
# --------------------------------------------------------------------------- #

RECAP_MAX_CONVERSATIONS = 500
RECAP_TTL_SECONDS = 30 * 86400
RECAP_MAX_CHARS = 6000


class RecapStore:
    """{conversation key: recap entry}, LRU-bounded, TTL-expired, written to one
    JSON file so a /v1 client's recap survives a hub restart (the hub restarts
    itself every few hours to auto-update). Every method is best-effort and
    never raises."""

    def __init__(self, path_fn, max_items=RECAP_MAX_CONVERSATIONS,
                 ttl=RECAP_TTL_SECONDS):
        self._path_fn = path_fn
        self._max = max_items
        self._ttl = ttl
        self._lock = threading.RLock()
        self._data = OrderedDict()
        self._loaded_from = None

    def _path(self):
        try:
            return self._path_fn()
        except Exception:                                        # noqa: BLE001
            return None

    def _ensure_loaded(self):
        path = self._path()
        if path == self._loaded_from:
            return
        self._data = OrderedDict()
        self._loaded_from = path
        if not path or not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                blob = json.load(f)
        except (OSError, ValueError):
            return
        now = time.time()
        rows = blob.get("recaps") if isinstance(blob, dict) else None
        if not isinstance(rows, list):
            return
        for row in rows:
            if not (isinstance(row, list) and len(row) == 2
                    and isinstance(row[0], str) and isinstance(row[1], dict)):
                continue
            entry = row[1]
            ts = entry.get("ts")
            if not isinstance(ts, (int, float)) or now - ts > self._ttl:
                continue
            if not isinstance(entry.get("recap"), str) or not entry["recap"].strip():
                continue
            self._data[row[0]] = entry
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    def _save(self):
        path = self._path()
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"recaps": [[k, v] for k, v in self._data.items()]}, f)
            os.replace(tmp, path)
        except OSError:
            pass

    def get(self, key):
        if not key:
            return None
        with self._lock:
            self._ensure_loaded()
            entry = self._data.get(key)
            if entry is None:
                return None
            if time.time() - float(entry.get("ts") or 0) > self._ttl:
                self._data.pop(key, None)
                return None
            self._data.move_to_end(key)
            return dict(entry)

    def put(self, key, entry):
        if not key or not isinstance(entry, dict):
            return
        recap = entry.get("recap")
        if not isinstance(recap, str) or not recap.strip():
            return
        entry = dict(entry)
        entry["recap"] = recap[:RECAP_MAX_CHARS]
        entry["ts"] = time.time()
        with self._lock:
            self._ensure_loaded()
            self._data[key] = entry
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)
            self._save()

    def delete(self, key):
        """Forget one conversation's recap (it was deleted). True if it had one."""
        if not key:
            return False
        with self._lock:
            self._ensure_loaded()
            if self._data.pop(key, None) is None:
                return False
            self._save()
            return True

    def __len__(self):
        with self._lock:
            self._ensure_loaded()
            return len(self._data)


def quick_chat_key(chat_id):
    """The conversation key the dashboard quick chat's requests resolve to:
    it sends X-Conversation-Id: quick-<chat id> (see conversation_key)."""
    cid = _clean_id("quick-" + str(chat_id or "")) if chat_id else None
    return ("hdr:" + cid) if cid else None


# --------------------------------------------------------------------------- #
# Output caps are not input windows
# --------------------------------------------------------------------------- #

_Q = r"[`'\"]?"
_OUTPUT_CAP_PATS = (
    # groq: "`max_tokens` must be less than or equal to `8192`, the maximum
    # value for `max_tokens` is less than the `context_window` for this model"
    re.compile(r"max_(?:completion_|output_|new_)?tokens" + _Q +
               r"[^0-9\n]{0,30}?(?:less than or equal to|<=|at most|"
               r"no (?:more|greater) than|not exceed)\s*" + _Q + r"(\d{3,7})", re.I),
    # anthropic: "max_tokens: 100000 > 64000, which is the maximum allowed
    # number of output tokens for ..."
    re.compile(r"max_(?:completion_|output_)?tokens" + _Q +
               r"\W{0,3}\s*\d{3,9}\s*>\s*(\d{3,7})", re.I),
    re.compile(r"(\d{3,7})" + _Q + r",? which is the maximum (?:allowed )?"
               r"(?:number of )?(?:output|completion) tokens", re.I),
    # "max_tokens (32000) exceeds the maximum allowed (8192)"
    re.compile(r"max_(?:completion_|output_|new_)?tokens" + _Q +
               r"[^\n]{0,40}?(?:exceeds?|greater than|larger than|above)"
               r"[^0-9\n]{0,40}?(?:maximum|max|limit|allowed)[^0-9\n]{0,20}?"
               + _Q + r"\(?(\d{3,7})", re.I),
    # "maximum output tokens is 4096" / "maximum number of completion tokens: 8192"
    re.compile(r"maximum (?:number of )?(?:allowed )?(?:output|completion|generated|new)"
               r" tokens(?: allowed)?(?: is| of|:)?\s*" + _Q + r"(\d{3,7})", re.I),
)


def output_cap_from_error(text):
    """The OUTPUT-token ceiling an error names, or None. Deliberately narrow:
    every pattern is anchored on max_tokens / output / completion wording, so a
    context-window error ("maximum context length is 32768 tokens. However, you
    requested 64 output tokens") never matches."""
    if not isinstance(text, str) or not text:
        return None
    for pat in _OUTPUT_CAP_PATS:
        m = pat.search(text)
        if m:
            try:
                v = int(m.group(1))
            except (TypeError, ValueError):
                continue
            if 16 <= v <= 2_000_000:
                return v
    return None


# --------------------------------------------------------------------------- #
# Native "context too long" error bodies, one per protocol
# --------------------------------------------------------------------------- #

def openai_overflow_body(requested, window):
    """OpenAI's own shape and code -- what opencode, the OpenAI SDKs and codex
    (non-stream) recognise as 'compact and retry'."""
    requested, window = int(requested or 0), int(window or 0)
    if window:
        msg = ("This model's maximum context length is %d tokens. However, your "
               "messages resulted in %d tokens. Please reduce the length of the "
               "messages." % (window, requested))
    else:
        msg = ("Your input exceeds the context window of every available model "
               "(%d tokens). Please reduce the length of the messages." % requested)
    return {"error": {"message": msg, "type": "invalid_request_error",
                      "param": "messages", "code": "context_length_exceeded"}}


def anthropic_overflow_body(requested, window):
    """Anthropic's shape. Claude Code keys its reactive compaction on the
    message starting 'prompt is too long'."""
    requested, window = int(requested or 0), int(window or 0)
    msg = "prompt is too long: %d tokens > %d maximum" % (requested, window or max(1, requested - 1))
    return {"type": "error", "error": {"type": "invalid_request_error", "message": msg}}


def responses_overflow_events(requested, window, model="auto"):
    """(response.created, response.failed) payloads for a streamed Responses
    request. Codex maps a response.failed whose error.code is
    context_length_exceeded to its ContextWindowExceeded error, marks the window
    full and auto-compacts; a plain HTTP 400 only surfaces as an error."""
    rid = "resp_" + hashlib.sha1(str(time.time()).encode()).hexdigest()[:24]
    base = {"id": rid, "object": "response", "created_at": int(time.time()),
            "model": model, "output": []}
    err = openai_overflow_body(requested, window)["error"]
    created = dict(base, status="in_progress")
    failed = dict(base, status="failed",
                  error={"code": "context_length_exceeded", "message": err["message"]})
    return created, failed


# --------------------------------------------------------------------------- #
# Usage on an OpenAI chat SSE stream
# --------------------------------------------------------------------------- #

_DONE_FRAME_RE = re.compile(rb"\s*data:\s*\[DONE\]\s*$")
_FRAME_END_RE = re.compile(rb"\r?\n\r?\n")
_MAX_PENDING_FRAME = 65536


def fix_chat_sse_usage(chunks, prompt_fn, inject_if_missing=False, model=None):
    """Relay an OpenAI chat-completions SSE byte stream, rewriting each usage
    frame's prompt_tokens through `prompt_fn(upstream_prompt_tokens)`, and --
    when `inject_if_missing` -- adding a usage frame before [DONE] if upstream
    sent none (the client asked stream_options.include_usage and must get it).

    Frame-buffered on the blank line that ends every SSE event, so a frame split
    across two network reads is still rewritten whole; everything that is not a
    usage frame passes byte-for-byte."""
    buf = b""
    seen_usage = False
    content_chars = 0
    last_id = None

    def _handle(frame):
        nonlocal seen_usage, content_chars, last_id
        if b"data:" not in frame:
            return frame, False
        if _DONE_FRAME_RE.match(frame):
            return frame, True
        if b'"usage"' not in frame and b'"content"' not in frame and b'"arguments"' not in frame:
            return frame, False
        lines = frame.split(b"\n")
        changed = False
        for i, line in enumerate(lines):
            s = line.strip()
            if not s.startswith(b"data:"):
                continue
            try:
                obj = json.loads(s[5:].strip().decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(obj, dict):
                continue
            last_id = obj.get("id") or last_id
            for ch in obj.get("choices") or []:
                d = (ch or {}).get("delta") or {}
                if isinstance(d.get("content"), str):
                    content_chars += len(d["content"])
                for tc in d.get("tool_calls") or []:
                    a = ((tc or {}).get("function") or {}).get("arguments")
                    if isinstance(a, str):
                        content_chars += len(a)
            u = obj.get("usage")
            if isinstance(u, dict) and (u.get("prompt_tokens") is not None
                                        or u.get("completion_tokens") is not None):
                seen_usage = True
                try:
                    pt = int(prompt_fn(int(u.get("prompt_tokens") or 0)))
                except Exception:                                # noqa: BLE001
                    continue
                if pt != u.get("prompt_tokens"):
                    u = dict(u)
                    u["prompt_tokens"] = pt
                    u["total_tokens"] = pt + int(u.get("completion_tokens") or 0)
                    obj["usage"] = u
                    lines[i] = b"data: " + json.dumps(obj).encode("utf-8")
                    changed = True
        return (b"\n".join(lines) if changed else frame), False

    def _usage_frame():
        pt = int(prompt_fn(0))
        ct = max(0, content_chars // 4)
        obj = {"id": last_id or "chatcmpl-usage", "object": "chat.completion.chunk",
               "created": int(time.time()), "model": model or "auto", "choices": [],
               "usage": {"prompt_tokens": pt, "completion_tokens": ct,
                         "total_tokens": pt + ct}}
        return b"data: " + json.dumps(obj).encode("utf-8") + b"\n\n"

    try:
        for chunk in chunks:
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            if not chunk:
                continue
            buf += chunk
            out = []
            while True:
                m = _FRAME_END_RE.search(buf)
                if not m:
                    break
                frame, buf = buf[:m.end()], buf[m.end():]
                frame, done = _handle(frame)
                if done and inject_if_missing and not seen_usage:
                    out.append(_usage_frame())
                    seen_usage = True
                out.append(frame)
            if len(buf) > _MAX_PENDING_FRAME:
                # A non-compliant stream with no blank-line separators: never
                # hold the client's answer back waiting for one.
                out.append(buf)
                buf = b""
            if out:
                yield b"".join(out)
        if buf:
            frame, done = _handle(buf)
            if done and inject_if_missing and not seen_usage:
                yield _usage_frame()
                seen_usage = True
            yield frame
    finally:
        # A client that disconnects closes THIS generator; the relay under it
        # (which owns the upstream socket) must be closed too, not left to GC.
        close = getattr(chunks, "close", None)
        if callable(close):
            try:
                close()
            except Exception:                                    # noqa: BLE001
                pass
