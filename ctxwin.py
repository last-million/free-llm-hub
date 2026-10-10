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
#
# Spellings verified in each CLI's source (2026-10-04): opencode
# packages/core/src/session/compaction.ts buildPrompt ("Create a new anchored
# summary from the conversation history ..." / the update path's "Construct a
# new summary that combines both"), kimi-cli prompts/compact.md ("compact this
# conversation context"), gemini-cli / qwen-code compression system prompt
# ("distilling chat history into a structured XML <state_snapshot>").
# MEASURED 2026-10-03: opencode's prompt matched none of the older spellings,
# so on `coding-multi` every compaction ran the whole Multi pipeline (plan,
# phases "Extract anchor state" / "Render anchored Markdown summary", review,
# synthesis): 300-600 s per summary, and opencode looked stuck on "compaction".
_COMPACTION_REQUEST_RE = re.compile(
    r"CONTEXT CHECKPOINT COMPACTION|"
    r"create a detailed summary of the conversation|"
    r"(?:summary|prompt) for continuing (?:our|the|this) conversation|"
    r"summari[sz]e (?:the|this|our) (?:entire |whole )?conversation|"
    r"handoff summary|"
    r"create a new anchored summary|"
    r"construct a new summary that combines both|"
    r"compact this conversation context|"
    r"distilling chat history into a structured", re.I)


def is_compaction_request(messages):
    """True when the latest user text is a CLI's own compaction prompt."""
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get("role") == "user":
            t = message_text(m)
            if t.strip():
                if _COMPACTION_REQUEST_RE.search(t[-6000:]):
                    return True
                break
    # gemini-cli / qwen-code ask for it in the SYSTEM prompt of the call.
    for m in messages or []:
        if isinstance(m, dict) and m.get("role") == "system":
            if _COMPACTION_REQUEST_RE.search(message_text(m)[:6000]):
                return True
    return False


# --------------------------------------------------------------------------- #
# Message units, and old tool results cleared on big turns
# --------------------------------------------------------------------------- #
#
# MEASURED 2026-10-08 (hub.log, OpenCode `coding-swarm`): ~85K-token tool turns
# on free models timed out (`CHAT-DEADLINE est=85203 ... _HopBudgetExceeded no
# answer within 106s`). Only a handful of models hold 85K and they are slow at
# that size; on a slow uplink an 85K-token body (~340 KB) also costs ~17 s of
# UPLOAD per hop. Most of those tokens are OLD tool outputs (file reads, command
# logs) the model no longer needs verbatim -- it can re-run the command.
#
# `clear_old_tool_results` replaces the CONTENT of an older tool result with one
# line (what it was, its first characters, how to get it back). No model call,
# no message removed, no id touched: a tool call keeps its matching result, so a
# strict provider never sees an orphan. It is a pure function of the history, and
# the stub of a result depends on THAT result's text alone, so the same result is
# cleared to the same bytes on every later turn (provider prompt caches stay
# valid up to the one unit that crosses the keep window each turn).

# Requests below this estimated size are never touched.
OLD_RESULT_CLEAR_FROM_TOKENS = 60000
OLD_RESULT_KEEP_RECENT = 8         # newest message units left verbatim
OLD_RESULT_MIN_CHARS = 1500        # only results LONGER than this are cleared
OLD_RESULT_KEEP_FAILING = 3        # the last N failing steps keep their output
OLD_RESULT_EXCERPT_CHARS = 160
# Every stub starts with this (exact_facts and the idempotence check key on it).
CLEARED_RESULT_PREFIX = "[tool output cleared by the hub to save context"

_TEXT_PART_TYPES = (None, "text", "input_text", "output_text")
_IMAGE_PART_TYPES = ("image", "image_url", "input_image")


def _block_ids(content, kind, key):
    return {b.get(key) for b in content
            if isinstance(b, dict) and b.get("type") == kind and b.get(key)}


def is_tool_call_msg(m):
    """An assistant turn that calls tools, in any of the three wire shapes:
    OpenAI `tool_calls`, an Anthropic assistant turn with `tool_use` blocks, a
    Responses `function_call` item."""
    if not isinstance(m, dict):
        return False
    if m.get("type") == "function_call":
        return True
    if m.get("role") != "assistant":
        return False
    if isinstance(m.get("tool_calls"), list):
        return True
    c = m.get("content")
    return isinstance(c, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_use" for b in c)


def _call_ids(m):
    ids = set()
    if m.get("type") == "function_call":
        i = m.get("call_id") or m.get("id")
        if i:
            ids.add(i)
    tcs = m.get("tool_calls")
    for tc in (tcs if isinstance(tcs, list) else []):
        if isinstance(tc, dict) and tc.get("id"):
            ids.add(tc["id"])
    if isinstance(m.get("content"), list):
        ids |= _block_ids(m["content"], "tool_use", "id")
    return ids


def _result_ids(m):
    """None when `m` is not a tool-result message, else the call ids it
    answers (possibly none): an OpenAI role "tool" message, a Responses
    `function_call_output` item, or an Anthropic user turn of `tool_result`
    blocks."""
    if m.get("role") == "tool":
        return {m["tool_call_id"]} if m.get("tool_call_id") else set()
    if m.get("type") == "function_call_output":
        i = m.get("call_id") or m.get("id")
        return {i} if i else set()
    c = m.get("content")
    if m.get("role") == "user" and isinstance(c, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
        return _block_ids(c, "tool_result", "tool_use_id")
    return None


def _answers(m, ids):
    """True when result message `m` belongs to a call run whose ids are `ids`."""
    if m.get("role") == "tool":
        return (not ids) or m.get("tool_call_id") in ids
    rids = _result_ids(m)
    return rids is not None and ((not ids) or (not rids) or bool(rids & ids))


def unit_spans(rest):
    """The history as UNITS, as (start, end) index pairs into `rest`: an
    assistant message with tool calls together with the tool results that
    immediately answer it, or a single other message. Compaction and clearing
    keep or drop a unit whole, so a call is never separated from its result
    (which _sanitize_tool_messages would then delete as an orphan).

    A RUN of tool-calling assistant messages is one unit with every result
    that follows it: /v1/responses turns Codex's parallel calls into one
    assistant message PER function_call (asst a, asst b, tool a, tool b), and
    unit-per-assistant let compaction keep tool a while dropping asst a."""
    spans, i, n = [], 0, len(rest)
    while i < n:
        if is_tool_call_msg(rest[i]):
            j = i + 1
            while j < n and is_tool_call_msg(rest[j]):
                j += 1
            ids = set()
            for a in rest[i:j]:
                ids |= _call_ids(a)
            while j < n and isinstance(rest[j], dict) and _answers(rest[j], ids):
                j += 1
            spans.append((i, j))
            i = j
        else:
            spans.append((i, i + 1))
            i += 1
    return spans


def message_units(rest):
    """`unit_spans` as lists of the messages themselves (the same objects)."""
    return [list(rest[a:b]) for a, b in unit_spans(rest)]


def _plain_text(c):
    """The text of a tool result's content when it is ONLY text (a string, or a
    list of text parts); None for anything else -- an image, a file, nothing."""
    if isinstance(c, str):
        return c
    if isinstance(c, list) and c:
        parts = []
        for p in c:
            if isinstance(p, str):
                parts.append(p)
            elif (isinstance(p, dict) and p.get("type") in _TEXT_PART_TYPES
                  and isinstance(p.get("text"), str)):
                parts.append(p["text"])
            else:
                return None
        return "\n".join(parts)
    return None


def _result_slots(m):
    """[(slot, text, is_error)] for every tool result message `m` carries; the
    slot says where the text lives so it can be replaced in place."""
    out = []
    if m.get("role") == "tool":
        out.append((("msg", 0), _plain_text(m.get("content")), False))
    elif m.get("type") == "function_call_output":
        out.append((("item", 0), _plain_text(m.get("output")), False))
    elif m.get("role") == "user" and isinstance(m.get("content"), list):
        for i, b in enumerate(m["content"]):
            if isinstance(b, dict) and b.get("type") == "tool_result":
                out.append((("block", i), _plain_text(b.get("content")),
                            bool(b.get("is_error"))))
    return out


def _has_image(m):
    c = m.get("content")
    return isinstance(c, list) and any(
        isinstance(b, dict) and b.get("type") in _IMAGE_PART_TYPES for b in c)


# A step that FAILED keeps its output: the model is probably mid-diagnosis and
# the error text is exactly what it needs verbatim. Deliberately liberal -- a
# false positive only protects one more unit, and only the last
# OLD_RESULT_KEEP_FAILING units are protected at all.
_FAILED_CS_RE = re.compile(
    r"Traceback \(most recent call last\)|\bnpm ERR!|\bFAILED\b|\bFAIL\b|"
    r"\bpanicked at\b|\b[A-Za-z]*(?:SyntaxError|ModuleNotFoundError|ImportError|"
    r"AssertionError|TypeError|ReferenceError)\b|"
    r"^\s*(?:Error|ERROR|Fatal|FATAL|fatal)\b|\berror(?:\[\w+\]|\s+TS\d+)?:|"
    r"\b(?:Unhandled|Uncaught)\b.{0,20}\b(?:rejection|exception|error)\b", re.M)
_FAILED_CI_RE = re.compile(
    r"\bexit(?:ed)?(?:\s+(?:with|status|code|value))*\s*[:=]?\s*-?[1-9]\d*\b|"
    r"\b(?:return ?code|exit_?code)\b\s*[:=]\s*-?[1-9]\d*|"
    r"\b[1-9]\d*\s+(?:failed|failing|errors?)\b|"
    r"\bcommand not found\b|no such file or directory|permission denied|"
    r"segmentation fault|cannot find module|command failed", re.I)


def looks_failed(text):
    """True when a tool result reads like a failing step (non-zero exit,
    traceback, failing tests, a compiler/shell error). Looks at the head and
    the tail only: that is where a command prints its verdict, and a file read
    that merely CONTAINS the word "error" in its middle is not a failure."""
    if not isinstance(text, str) or not text:
        return False
    probe = text if len(text) <= 1600 else text[:800] + "\n" + text[-800:]
    return bool(_FAILED_CS_RE.search(probe) or _FAILED_CI_RE.search(probe))


def cleared_stub(text):
    """The one line that replaces a cleared result. A function of `text` alone
    (no counters, no clock) so the same result gets the same bytes every turn."""
    head = re.sub(r"\s+", " ", text[:OLD_RESULT_EXCERPT_CHARS * 4]).strip()
    head = head[:OLD_RESULT_EXCERPT_CHARS] or "(no text)"
    return ("%s (was ~%d chars): %s. Re-run the command if you need it.]"
            % (CLEARED_RESULT_PREFIX, len(text), head))


_IMAGE_KEYS = frozenset(("image_url", "source", "data", "url"))


def rough_tokens(obj, _depth=0):
    """chars/4 over every string in a request (image payload keys skipped).
    The fallback for a caller with no estimate of its own; app.py passes its
    tools-aware `_est_tokens` instead."""
    if _depth > 8:
        return 0
    if isinstance(obj, str):
        return len(obj) // 4
    if isinstance(obj, dict):
        return sum(rough_tokens(v, _depth + 1) for k, v in obj.items()
                   if k not in _IMAGE_KEYS)
    if isinstance(obj, (list, tuple)):
        return sum(rough_tokens(v, _depth + 1) for v in obj)
    return 0


def _like(orig, stub):
    """`stub` in the shape of the content it replaces (text parts stay parts)."""
    if isinstance(orig, list) and orig and isinstance(orig[0], dict):
        return [{"type": orig[0].get("type") or "text", "text": stub}]
    return stub


def _with_stubs(m, stubs):
    """A copy of message `m` with the results in `stubs` ({slot: stub}) replaced."""
    new = dict(m)
    for (kind, idx), stub in stubs.items():
        if kind == "msg":
            new["content"] = _like(m.get("content"), stub)
        elif kind == "item":
            new["output"] = _like(m.get("output"), stub)
        else:
            blocks = list(new["content"])
            blocks[idx] = dict(blocks[idx], content=_like(blocks[idx].get("content"), stub))
            new["content"] = blocks
    return new


def clear_old_tool_results(messages, keep_recent=OLD_RESULT_KEEP_RECENT,
                           min_chars=OLD_RESULT_MIN_CHARS, *, est_tokens=None,
                           min_tokens=OLD_RESULT_CLEAR_FROM_TOKENS,
                           keep_failing=OLD_RESULT_KEEP_FAILING):
    """-> (messages, stats). Old tool results longer than `min_chars` are
    replaced by one `cleared_stub` line; everything else is returned as it was.

    Works on all three wire shapes (OpenAI role "tool" messages, Anthropic
    `tool_result` blocks, Responses `function_call_output` items). Never removes
    or reorders a message and never touches an id, so call/result pairing is
    intact by construction; the input list is not mutated, a changed message
    is a copy, and when nothing changes the SAME list comes back.

    Left alone, always: the newest `keep_recent` message units, the leading
    system messages, the latest real user instruction, the results of the last
    `keep_failing` failing steps, a message carrying an image, a CLI's own
    compaction request, a request under `min_tokens` (`est_tokens`, else
    `rough_tokens`; 0 disables the size gate), a result already cleared, and a
    result the stub would not make shorter.

    Monotonic in time: a result that is cleared at turn T is cleared, to the
    same bytes, at every later turn (the keep window only moves forward and a
    failing step can only fall out of the last `keep_failing`)."""
    stats = {"cleared": 0, "chars_before": 0, "chars_after": 0, "units": 0,
             "kept_failing": 0, "skipped": None}
    try:
        return _clear_old_tool_results(messages, keep_recent, min_chars, est_tokens,
                                       min_tokens, keep_failing, stats)
    except Exception:                                            # noqa: BLE001
        stats.update(cleared=0, chars_before=0, chars_after=0, skipped="error")
        return messages, stats


def _clear_old_tool_results(messages, keep_recent, min_chars, est_tokens, min_tokens,
                            keep_failing, stats):
    if not isinstance(messages, list) or not messages:
        stats["skipped"] = "empty"
        return messages, stats
    if min_tokens:
        est = est_tokens if est_tokens is not None else rough_tokens(messages)
        if est < min_tokens:
            stats["skipped"] = "small"
            return messages, stats
    if is_compaction_request(messages):
        stats["skipped"] = "compaction"
        return messages, stats
    start = 0
    while (start < len(messages) and isinstance(messages[start], dict)
           and messages[start].get("role") in ("system", "developer")):
        start += 1
    spans = [(start + a, start + b) for a, b in unit_spans(messages[start:])]
    stats["units"] = len(spans)
    older = len(spans) - max(0, int(keep_recent))
    if older <= 0:
        stats["skipped"] = "short"
        return messages, stats
    # The last `keep_failing` units that hold a failing result, counted from the
    # END of the whole history (so a step only ever falls OUT of this set).
    protected = set()
    if keep_failing and keep_failing > 0:
        for u in range(len(spans) - 1, -1, -1):
            a, b = spans[u]
            if any((err or looks_failed(text)) for k in range(a, b)
                   if isinstance(messages[k], dict)
                   for _slot, text, err in _result_slots(messages[k])
                   if text is None or not text.startswith(CLEARED_RESULT_PREFIX)):
                protected.add(u)
                if len(protected) >= keep_failing:
                    break
    pinned = None                  # the latest real user instruction
    for k in range(len(messages) - 1, start - 1, -1):
        if is_real_instruction(messages[k]):
            pinned = k
            break
    replaced = {}
    for u in range(older):
        pending = {}               # message index -> {slot: (stub, chars before)}
        for k in range(*spans[u]):
            m = messages[k]
            if not isinstance(m, dict) or k == pinned or _has_image(m):
                continue
            for slot, text, _err in _result_slots(m):
                if (text is None or len(text) <= min_chars
                        or text.startswith(CLEARED_RESULT_PREFIX)):
                    continue
                stub = cleared_stub(text)
                if len(stub) < len(text):
                    pending.setdefault(k, {})[slot] = (stub, len(text))
        if not pending:
            continue
        if u in protected:
            stats["kept_failing"] += 1
            continue
        for k, slots in pending.items():
            replaced[k] = _with_stubs(messages[k], {s: v[0] for s, v in slots.items()})
            for stub, n in slots.values():
                stats["cleared"] += 1
                stats["chars_before"] += n
                stats["chars_after"] += len(stub)
    if not replaced:
        stats["skipped"] = "nothing"
        return messages, stats
    out = list(messages)
    for k, nm in replaced.items():
        out[k] = nm
    return out, stats


# --------------------------------------------------------------------------- #
# Exact facts: what a summary must never paraphrase
# --------------------------------------------------------------------------- #
#
# MEASURED 2026-09-27: codex with a 12K compaction limit (3 compactions in 7
# turns) kept a stated preference but answered a file's last line with ANOTHER
# file's value -- a model-written summary had merged two files' contents. A
# summary paraphrases; a value copied mechanically does not. So the facts a
# later turn is likely to ask for exactly -- what each file/command printed,
# what was written to which file, the user's stated values and rules, recorded
# decisions -- are extracted here by pattern, verbatim, with no model call, and
# attached next to (never inside) the summary. Bounded: the newest facts win,
# the user's own statements have their own share so a burst of file reads
# cannot push them out.

EXACT_FACTS_MARKER = "EXACT FACTS (verbatim"
EXACT_FACTS_HEADER = (
    "[EXACT FACTS (verbatim, extracted by the hub -- not a summary). Quote these "
    "values exactly; a value belongs ONLY to the file / command it is listed "
    "under. Later lines supersede earlier ones for the same file.]")
EXACT_FACTS_MAX_CHARS = 4000
EXACT_FACTS_MAX_FILE = 24          # newest file / command facts kept
EXACT_FACTS_MAX_USER = 16          # newest user statements / decisions kept
_FACT_VALUE_CHARS = 160
_FACT_CMD_CHARS = 80

# A file path: something with a letter-led extension, optionally with dirs.
# Not preceded by "/" or ":" so a URL's host ("https://x.com/a") is not a file.
_FACT_PATH_RE = re.compile(
    r"(?<![\w/:.\\-])((?:[A-Za-z]:[\\/])?(?:[\w.-]+[\\/])*[\w-][\w.-]*"
    r"\.[A-Za-z][A-Za-z0-9]{0,7})(?![\w\\/-])")
_FACT_SECTION_RE = re.compile(r"^==> (.+?) <==$", re.M)
_FACT_META_LINE_RE = re.compile(r"^[A-Z][\w ]{1,40}:")
_FACT_HEREDOC_RES = (
    re.compile(r"cat\s+>\s*(['\"]?)([^\s'\"<>|;&]+)\1\s*<<-?\s*(['\"]?)(\w+)\3[^\n]*\n(.*?)\n\s*\4\b",
               re.S),
    re.compile(r"cat\s+<<-?\s*(['\"]?)(\w+)\1\s*>\s*(['\"]?)([^\s'\"<>|;&]+)\3[^\n]*\n(.*?)\n\s*\2\b",
               re.S),
)
_FACT_ECHO_RE = re.compile(
    r"(?:echo|printf)\s+(?:-[a-zA-Z]+\s+)*(['\"])(.*?)(?<!\\)\1\s*(>>?)\s*(['\"]?)([^\s'\"<>|;&]+)\4")
_FACT_WRITE_REDIRECT_RE = re.compile(r"(?<![<>&0-9])>{1,2}(?!&)")
_FACT_PATCH_ADD_RE = re.compile(r"^\*\*\* Add File: (.+)$", re.M)
_FACT_PATH_KEYS = ("path", "file_path", "filePath", "filename", "file", "target_file")
_FACT_CONTENT_KEYS = ("content", "contents", "file_text", "text")
_FACT_CMD_KEYS = ("command", "cmd", "script", "input")
# A user sentence worth keeping verbatim: a quoted literal, a stated value
# ("is 42", "= 8787", "port 5173", "ORCHID-9", "300 ms"), or a standing rule /
# preference / name. A bare digit ("step 3") is not a fact.
_USER_FACT_RE = re.compile(
    r"[\"'`][^\"'`\n]{1,120}[\"'`]"
    r"|(?:\b(?:is|are|was|equals|to|of)\b|[=:])\s*[^\s,;]*\d"
    r"|\b[A-Za-z]+[-_]?\d+[\w.-]*\b|\b\d[\d.,]*\s*(?:ms|s|sec|seconds?|minutes?|px|%|"
    r"kb|mb|gb|k|tokens?|usd|eur)\b"
    r"|\b(?:prefer|always|never|must|remember|call me|my name|named?|codename|"
    r"instead of|do not|don't|deadline|port|version|favou?rite|preferred)\b"
    # a personal fact with no digit and no "remember": "My favourite colour is
    # teal" was dropped (nothing above matched a plain "my X is <word>").
    r"|\bmy\s+(?:[\w'’-]+\s+){0,2}?(?:colou?r|name|email|city|country|timezone|time\s+zone"
    r"|birthday|pet|dog|cat|username|handle|nickname|pronouns?|language|editor|shell)"
    r"\s+(?:is|are|was)\b", re.I)
_USER_FACT_MAX_SENTENCE = 300      # longer is a pasted blob, not a statement
# A STANDING rule / preference / name: kept in its own share, so a long run of
# per-step values ("step 12: use value=12") cannot push "I prefer tabs" out.
_USER_RULE_RE = re.compile(
    r"\b(?:prefer|always|never|must|remember|call me|my name|codename|"
    r"instead of|do not|don't|favou?rite|preferred)\b", re.I)
EXACT_FACTS_MAX_RULES = 8
_DECISION_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:decision|decided|constraint|agreed)\b\s*[:\-]", re.I | re.M)
# A CLI's own compaction SUMMARY carried into the history (codex: "Another
# language model started to solve this problem and produced a summary of its
# thinking process..."). Its prose is a paraphrase -- never mined for facts --
# but an EXACT FACTS block inside it is carried forward as is.
_SUMMARY_BRIDGE_RE = re.compile(
    r"another language model started|produced a summary of|summary of (?:the|our) "
    r"(?:earlier |previous )?conversation", re.I)


def _fact_clip(value, n=_FACT_VALUE_CHARS):
    value = str(value or "").replace("\r", "").strip()
    return value if len(value) <= n else value[:n] + "...[cut]"


def _fact_tool_output(text):
    """The payload of a tool result: codex's JSON {"output": ...} and its
    "Exit code: / Wall time: / Output:" header are unwrapped."""
    t = text or ""
    s = t.lstrip()
    if s.startswith("{"):
        try:
            d = json.loads(s)
            if isinstance(d, dict) and isinstance(d.get("output"), str):
                t = d["output"]
        except ValueError:
            pass
    lines = t.split("\n")
    for i in range(min(10, len(lines))):
        if lines[i].strip() == "Output:" and all(
                (not ln.strip()) or _FACT_META_LINE_RE.match(ln) for ln in lines[:i]):
            return "\n".join(lines[i + 1:])
    return t


def _fact_command(args):
    """(command text, parsed args dict or None) of a tool call's arguments."""
    raw = args if isinstance(args, str) else json.dumps(args or {})
    try:
        d = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        d = None
    if isinstance(d, dict):
        for k in _FACT_CMD_KEYS:
            v = d.get(k)
            if isinstance(v, list) and v and all(isinstance(x, str) for x in v):
                # ["bash", "-lc", "cat a.txt"] -> the script; else the argv.
                return (v[-1] if len(v) >= 3 and v[-2] in ("-c", "-lc") else " ".join(v)), d
            if isinstance(v, str) and v.strip():
                return v, d
        return "", d
    return raw or "", None


def _fact_lines_of(text):
    return [ln.rstrip() for ln in (text or "").replace("\r", "").split("\n") if ln.strip()]


def _fact_describe(path, label, body_lines):
    if not body_lines:
        return None
    if len(body_lines) == 1:
        return '%s%s: "%s"' % (path, label, _fact_clip(body_lines[0]))
    return '%s%s: %d lines; first "%s"; last "%s"' % (
        path, label, len(body_lines), _fact_clip(body_lines[0]), _fact_clip(body_lines[-1]))


def _fact_writes(cmd, d):
    """[(path, kind, fact)] for content a tool call WROTE: a write tool's
    path + content, an apply_patch "Add File", a heredoc or echo redirect."""
    out = []
    if isinstance(d, dict):
        path = next((d.get(k) for k in _FACT_PATH_KEYS if isinstance(d.get(k), str)), None)
        content = next((d.get(k) for k in _FACT_CONTENT_KEYS
                        if isinstance(d.get(k), str)), None)
        if path and content is not None:
            f = _fact_describe(path, " (written)", _fact_lines_of(content))
            if f:
                out.append((path, "write", f))
    text = cmd or ""
    for m in _FACT_PATCH_ADD_RE.finditer(text):
        path = m.group(1).strip()
        rest = text[m.end():]
        stop = re.search(r"^\*\*\* ", rest, re.M)
        body = rest[:stop.start()] if stop else rest
        added = [ln[1:] for ln in body.split("\n") if ln.startswith("+")]
        f = _fact_describe(path, " (written)", [ln for ln in added if ln.strip()])
        if f:
            out.append((path, "write", f))
    for rx in _FACT_HEREDOC_RES:
        for m in rx.finditer(text):
            g = m.groups()
            path, body = (g[1], g[4]) if rx is _FACT_HEREDOC_RES[0] else (g[3], g[4])
            f = _fact_describe(path, " (written)", _fact_lines_of(body))
            if f:
                out.append((path, "write", f))
    for m in _FACT_ECHO_RE.finditer(text):
        value, op, path = m.group(2), m.group(3), m.group(5)
        kind = "append" if op == ">>" else "write"
        f = '%s (%s): "%s"' % (path, "appended" if kind == "append" else "written",
                                _fact_clip(value))
        out.append((path, kind, f))
    return out


_FACT_LINENO_RE = re.compile(r"^\s*\d+(?:→|\t)", re.M)


def _fact_reads(cmd, output, read_path=None):
    """[(path, fact)] for what a tool call PRINTED about a file: per section of
    a multi-file `==> path <==` listing, else attributed to the command's ONE
    path, else to a read tool's own `path` argument (Claude Code Read: its
    "   12->" line numbers are dropped). Two or more paths and no section
    headers -> nothing: guessing which file a line came from is exactly the
    mistake this exists to prevent."""
    out = []
    text = _fact_tool_output(output).strip("\n")
    if not text.strip():
        return out
    if not cmd and read_path:
        f = _fact_describe(read_path, " (read)",
                           _fact_lines_of(_FACT_LINENO_RE.sub("", text)))
        return [(read_path, f)] if f else out
    via = " (via `%s`)" % _fact_clip(re.sub(r"\s+", " ", cmd), _FACT_CMD_CHARS) if cmd else ""
    heads = list(_FACT_SECTION_RE.finditer(text))
    if heads:
        for i, m in enumerate(heads):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            f = _fact_describe(m.group(1).strip(), via, _fact_lines_of(text[m.end():end]))
            if f:
                out.append((m.group(1).strip(), f))
        return out
    if not cmd or _FACT_WRITE_REDIRECT_RE.search(cmd):
        return out
    paths = list(dict.fromkeys(_FACT_PATH_RE.findall(cmd)))
    if len(paths) != 1:
        return out
    f = _fact_describe(paths[0], via, _fact_lines_of(text))
    if f:
        out.append((paths[0], f))
    return out


def _carried_facts(text):
    """Bullet lines of an EXACT FACTS block already present in `text`."""
    i = text.find(EXACT_FACTS_MARKER)
    if i < 0:
        return []
    out = []
    for ln in text[i:].split("\n")[1:]:
        s = ln.strip()
        if not s:
            if out:
                break
            continue
        if not s.startswith("- "):
            break
        out.append(s[2:])
    return out


def _sentences(text):
    for ln in (text or "").split("\n"):
        for s in re.split(r"(?<=[.!?])\s+(?=[A-Z\"'`])", ln):
            s = s.strip(" \t-*")
            if s:
                yield s


def exact_facts(messages, max_chars=EXACT_FACTS_MAX_CHARS):
    """The verbatim facts of `messages` (OpenAI chat shape), oldest first, as
    bullet-less lines. Pure, bounded, never raises."""
    try:
        facts = _exact_facts(messages or [], max_chars)
    except Exception:                                            # noqa: BLE001
        return []
    # NEVER a credential: these lines are persisted in the recap store and
    # handed to crew/swarm workers and /agent context -- the same rule as
    # memory.py's ("a candidate that carries a credential is dropped whole").
    return _drop_secrets(facts)


def _drop_secrets(facts):
    """`facts` minus every line that looks like it carries a credential
    (memory._looks_secret). Fails CLOSED: no filter, no verbatim facts."""
    try:
        from memory import _looks_secret
        return [f for f in facts if not _looks_secret(str(f))]
    except Exception:                                            # noqa: BLE001
        return []


def _exact_facts(messages, max_chars):
    files = OrderedDict()       # key -> (path, fact)
    users = OrderedDict()       # key -> fact
    calls = {}                  # tool_call_id -> command text

    def _put_file(key, path, fact):
        files.pop(key, None)
        files[key] = (path, fact)

    def _drop_path(path):
        for k in [k for k, v in files.items() if v[0] == path]:
            files.pop(k, None)

    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        text = message_text(m)
        for carried in _carried_facts(text):
            if carried.startswith("user said:") or _DECISION_LINE_RE.match(carried):
                users.pop(carried.lower(), None)
                users[carried.lower()] = carried
                continue
            p = carried.split(" ", 1)[0].rstrip(":")
            _put_file(("carried", carried), p, carried)
        if role == "assistant":
            for tc in m.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                cmd, d = _fact_command((tc.get("function") or {}).get("arguments"))
                read_path = None
                if isinstance(d, dict) and not any(isinstance(d.get(k), str)
                                                   for k in _FACT_CONTENT_KEYS + ("new_string",)):
                    read_path = next((d.get(k) for k in _FACT_PATH_KEYS
                                      if isinstance(d.get(k), str) and d.get(k)), None)
                if tc.get("id"):
                    calls[tc["id"]] = (cmd, read_path)
                for path, kind, fact in _fact_writes(cmd, d):
                    if kind == "write":
                        _drop_path(path)
                    _put_file((path, kind, fact), path, fact)
        elif role == "tool":
            if text.startswith(CLEARED_RESULT_PREFIX):
                continue        # a cleared stub is not what the file printed
            cmd, read_path = calls.get(m.get("tool_call_id")) or ("", None)
            for path, fact in _fact_reads(cmd, text, read_path):
                _put_file((path, cmd or "read"), path, fact)
        elif role == "user" and not _COMPACTION_REQUEST_RE.search(text[-6000:]) \
                and not _SUMMARY_BRIDGE_RE.search(text[:2000]):
            said = instruction_text(text)
            for s in _sentences(said):
                if 4 <= len(s) <= _USER_FACT_MAX_SENTENCE and _USER_FACT_RE.search(s):
                    line = "user said: \"%s\"" % _fact_clip(s, 200)
                    users.pop(line.lower(), None)
                    users[line.lower()] = line
        if role in ("user", "assistant") and not _SUMMARY_BRIDGE_RE.search(text[:2000]):
            for dm in _DECISION_LINE_RE.finditer(text):
                line = text[dm.start():].split("\n", 1)[0].strip(" \t-*")
                users.pop(line.lower(), None)
                users[line.lower()] = _fact_clip(line, 200)
    file_facts = [v[1] for v in files.values()][-EXACT_FACTS_MAX_FILE:]
    said = list(users.values())
    is_rule = [bool(_USER_RULE_RE.search(v) or _DECISION_LINE_RE.match(v)) for v in said]
    rule_idx = [i for i, r in enumerate(is_rule) if r][-EXACT_FACTS_MAX_RULES:]
    room = max(0, EXACT_FACTS_MAX_USER - len(rule_idx))
    other_idx = [i for i, r in enumerate(is_rule) if not r][-room:] if room else []
    rules = [said[i] for i in rule_idx]
    others = [said[i] for i in other_idx]
    order = {v: i for i, v in enumerate(said)}

    def _size():
        return sum(len(x) + 3 for x in rules + others + file_facts)
    # Over the char cap: the oldest file facts go first, then the oldest
    # per-step values, the standing rules last.
    while file_facts and _size() > max_chars:
        file_facts.pop(0)
    while others and _size() > max_chars:
        others.pop(0)
    while rules and _size() > max_chars:
        rules.pop(0)
    return sorted(rules + others, key=order.get) + file_facts


def format_exact_facts(facts):
    """The block that carries `facts`, or "" when there are none. A stored
    entry written before the secret filter existed is filtered here too."""
    facts = _drop_secrets(facts or [])
    if not facts:
        return ""
    return EXACT_FACTS_HEADER + "\n" + "\n".join("- " + f for f in facts)


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

def _overflow_numbers(requested, window):
    """(requested, window) that always state a request LARGER than its limit.
    The signal also fires when a hop would drop most of the history well
    under its raw window, and the window can be a table figure; printed as
    given that read "90000 tokens > 100000 maximum"."""
    requested, window = int(requested or 0), int(window or 0)
    if window and requested <= window:
        requested = window + 1
    return requested, window


def openai_overflow_body(requested, window, hint=""):
    """OpenAI's own shape and code -- what opencode, the OpenAI SDKs and codex
    (non-stream) recognise as 'compact and retry'. `hint` (empty by default,
    so the body is byte-identical when nothing is passed) is appended to the
    message to make it actionable WITHOUT changing the shape or code."""
    requested, window = _overflow_numbers(requested, window)
    if window:
        msg = ("This model's maximum context length is %d tokens. However, your "
               "messages resulted in %d tokens. Please reduce the length of the "
               "messages." % (window, requested))
    else:
        msg = ("Your input exceeds the context window of every available model "
               "(%d tokens). Please reduce the length of the messages." % requested)
    if hint:
        msg += hint
    return {"error": {"message": msg, "type": "invalid_request_error",
                      "param": "messages", "code": "context_length_exceeded"}}


def anthropic_overflow_body(requested, window, hint=""):
    """Anthropic's shape. Claude Code keys its reactive compaction on the
    message starting 'prompt is too long', so `hint` is appended AFTER that
    prefix (empty by default = byte-identical)."""
    requested, window = _overflow_numbers(requested, window)
    msg = "prompt is too long: %d tokens > %d maximum" % (requested, window or max(1, requested - 1))
    if hint:
        msg += hint
    return {"type": "error", "error": {"type": "invalid_request_error", "message": msg}}


def responses_overflow_events(requested, window, model="auto", hint=""):
    """(response.created, response.failed) payloads for a streamed Responses
    request. Codex maps a response.failed whose error.code is
    context_length_exceeded to its ContextWindowExceeded error, marks the window
    full and auto-compacts; a plain HTTP 400 only surfaces as an error. `hint`
    (empty by default) rides the error message, not the code."""
    rid = "resp_" + hashlib.sha1(str(time.time()).encode()).hexdigest()[:24]
    base = {"id": rid, "object": "response", "created_at": int(time.time()),
            "model": model, "output": []}
    err = openai_overflow_body(requested, window, hint=hint)["error"]
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
