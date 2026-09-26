"""Answer-quality gate: is a 200's text a real answer, or an answer with junk
glued on (or only junk)?

MEASURED LIVE (llm7/GLM-5.3-Flash): the model answered correctly, then kept
generating until max_tokens, and the hub served the whole thing as a success:

    "OK出具证明的，原试题解析 做题如有雷同，纯属巧合…"           (prompt had no CJK)
    "6510</arg_value></tool_call>6510</arg_value></tool_call>The user asked"

Every existing check (empty 200, relay error page, refusal, typed tool call)
looked for a BAD answer; none looked for a GOOD answer followed by garbage, so
both counted as clean deliveries and kept that model winning hop 1.

Leaf module on purpose (no app imports): pure text in, verdict out, trivially
testable and cheap enough to run on every non-streamed answer and once at the
end of every stream.

CONSERVATIVE by design. A false positive either trims a real answer (salvage)
or costs a hop, so every detector demands a strong, specific signal:
  * code (fenced blocks, inline `spans`) is masked out before any check --
    repeated lines and odd markup are normal inside code;
  * the script-switch check only fires when the PROMPT never used that script
    and never names its language / asks for a translation, AND the switch is
    either glued straight onto the answer ("OK出具…") or the reply ran to the
    token cap -- a list of greetings after "Here are some:" is neither;
  * markup leaks are only judged when no tools were offered (with tools, the
    typed-tool-call / tool_rescue path owns that shape) and the prompt itself
    does not mention the tag;
  * truncation alone (finish_reason "length") is reported but never fails an
    answer: a plain truncation is common and legitimate (see
    app._chat_json_starved's notes).
"""
import re

__all__ = ["inspect"]

# Anything longer is judged on its head (script switch) and tail (loops and
# leaks live at the END of a runaway generation); keeps the check O(cap).
_MAX_SCAN = 24000
_RATIO_WINDOW = 1200            # script-share is judged on this much of the rest

_FENCE_RE = re.compile(r"```.*?(?:```|\Z)", re.S)
_INLINE_CODE_RE = re.compile(r"`[^`\n]{1,300}`")


def _blank(m):
    # per LINE, not per char: a 20 KB code block is ~700 lines, not 20k subs
    return "\n".join(" " * len(part) for part in m.group(0).split("\n"))


def _mask_code(text):
    """Same-length copy of `text` with code blanked to spaces (newlines kept),
    so every index found on the mask is valid on the original."""
    if "`" not in text:
        return text
    return _INLINE_CODE_RE.sub(_blank, _FENCE_RE.sub(_blank, text))


# --------------------------------------------------------------------------- #
# (a) runaway continuation: the reply switches to a script the prompt never used
# --------------------------------------------------------------------------- #
# Greek is deliberately absent: alpha/beta/pi show up in ordinary maths answers.
_SCRIPTS = {
    "cjk": ("぀-ヿ㐀-䶿一-鿿가-힯豈-﫿"
            "　-〿＀-￯",
            ("chinese", "mandarin", "cantonese", "japanese", "korean", "kanji",
             "hanzi", "hiragana", "katakana", "hangul", "pinyin", "chinois",
             "japonais", "coreen", "coréen", "chino", "japon", "cjk")),
    "cyrillic": ("Ѐ-ӿ",
                 ("russian", "ukrainian", "cyrillic", "bulgarian", "serbian",
                  "russe", "kazakh", "belarus", "mongolian", "macedonian")),
    "arabic": ("؀-ۿݐ-ݿ",
               ("arabic", "arabe", "persian", "farsi", "urdu", "pashto",
                "quran", "coran")),
    "hebrew": ("֐-׿", ("hebrew", "hébreu", "hebreu", "yiddish")),
    "thai": ("฀-๿", ("thai", "thaï")),
    "devanagari": ("ऀ-ॿ", ("hindi", "sanskrit", "marathi", "nepali",
                                     "devanagari")),
}
_SCRIPT_RES = {k: re.compile("[" + rng + "]") for k, (rng, _names) in _SCRIPTS.items()}
# A prompt using any of these asked for other-language output on purpose.
_TRANSLATE_HINTS = ("translat", "tradu", "übersetz", "multilingual",
                    "multilingue", "unicode", "transliterat")
_LATIN_LETTER_RE = re.compile(r"[A-Za-zÀ-ɏ]")
_MIN_FOREIGN_CHARS = 8          # a word or two of foreign script is never junk
_FOREIGN_SHARE = 0.6            # ...and the rest of the reply must be MOSTLY it


def _script_switch(text, masked, prompt_text, finish_reason):
    if prompt_text is None:
        return None             # nothing to compare against -> never guess
    low = prompt_text.lower()
    if any(h in low for h in _TRANSLATE_HINTS):
        return None
    best = None
    for key, rx in _SCRIPT_RES.items():
        if rx.search(prompt_text) or any(n in low for n in _SCRIPTS[key][1]):
            continue            # the prompt itself used / asked for this script
        m = rx.search(masked)
        if not m:
            continue
        pos = m.start()
        prefix = text[:pos]
        if not re.search(r"\w", prefix):
            continue            # the reply STARTED in that script: not a switch
        rest = masked[pos:pos + _RATIO_WINDOW]
        n_foreign = len(rx.findall(rest))
        if n_foreign < _MIN_FOREIGN_CHARS:
            continue
        n_latin = len(_LATIN_LETTER_RE.findall(rest))
        if n_foreign < _FOREIGN_SHARE * (n_foreign + n_latin):
            continue
        glued = prefix[-1].isascii() and prefix[-1].isalnum()
        if not (glued or finish_reason == "length"):
            continue
        if best is None or pos < best:
            best = pos
    return best


# --------------------------------------------------------------------------- #
# (b) repetition loops
# --------------------------------------------------------------------------- #
_LINE_MIN_LEN = 12
_LINE_MIN_RUN = 3
# A looping unit is 8..160 chars: shorter is "ha ha ha" / "0x00, 0x00" land,
# longer is a paragraph (caught per line by _line_loop).
_UNIT_MIN = 8
_UNIT_MAX = 160
_PRECHECK_IGNORE = frozenset(" \t\r\n|")


def _unit_is_meaningful(unit):
    stripped = unit.strip()
    if not stripped or stripped.startswith("|"):
        return False            # markdown table rows legitimately repeat
    distinct = {c for c in stripped if not c.isspace()}
    letters = sum(1 for c in stripped if c.isalpha())
    return len(distinct) >= 5 and letters >= 3


def _line_is_meaningful(norm):
    # short lines ("---", "}", "Yes.") and table rows repeat legitimately
    return (len(norm) >= _LINE_MIN_LEN and not norm.startswith("|")
            and any(c.isalnum() for c in norm))


def _line_loop(masked, prompt_text):
    """Offset just past the first copy of a line repeated >=3x in a row."""
    prev = None
    run = 0
    first_end = 0
    pos = 0
    for line in masked.split("\n"):
        start = pos
        pos += len(line) + 1
        norm = line.strip()
        if not norm:
            continue            # blank lines between copies do not break a run
        if norm == prev:
            run += 1
            if run >= _LINE_MIN_RUN and _line_is_meaningful(norm):
                if prompt_text and (norm + "\n" + norm) in prompt_text:
                    return None
                return first_end
        else:
            prev = norm
            run = 1
            first_end = start + len(line)
    return None


def _tail_loop(masked, prompt_text):
    """Offset just past the FIRST copy of a unit that repeats, back to back and
    at least 3 times, up to the very END of the answer.

    End-anchored on purpose: a degenerate generation loops until the token cap,
    so the loop IS the tail. That turns the search into ~150 C-speed slice
    compares (one per candidate period) instead of a backtracking regex over
    the whole text -- MEASURED: `(.{8,160}?)\\1{2,}` cost ~40 ms on 4 KB of
    plain prose, twenty times the budget. Mid-text line loops are
    _line_loop's job."""
    s = masked.rstrip()
    n = len(s)
    for p in range(_UNIT_MIN, _UNIT_MAX + 1):
        if 3 * p > n:
            break
        # s[i] == s[i+p] across the last 3p chars <=> 3 copies of period p
        if s[n - 3 * p:n - p] != s[n - 2 * p:n]:
            continue
        chars = set(s[n - p:n]) - _PRECHECK_IGNORE
        if len(chars) < 5:
            continue            # rotation-invariant, C-speed: skip "aaaa…" walks
        start = n - 3 * p
        while start - p >= 0 and s[start - p:start] == s[start:start + p]:
            start -= p          # whole earlier copies
        while start > 0 and s[start - 1] == s[start - 1 + p]:
            start -= 1          # ...and the partial one the loop began with
        unit = s[start:start + p]
        if not _unit_is_meaningful(unit):
            continue
        if prompt_text and unit * 2 in prompt_text:
            continue            # the user asked for this repetition
        return start + p
    return None


# --------------------------------------------------------------------------- #
# (c) leaked tool-call markup in a plain-chat answer
# --------------------------------------------------------------------------- #
_LEAK_RE = re.compile(r"</?tool_call\b[^>]{0,40}>|</?arg_(?:key|value)>", re.I)
_LEAK_PROMPT_RE = re.compile(r"tool_call|arg_value|arg_key", re.I)


def _markup_leak(masked, prompt_text, tools_offered):
    if tools_offered:
        return None             # typed-tool-call / tool_rescue own this shape
    if prompt_text and _LEAK_PROMPT_RE.search(prompt_text):
        return None             # the user is asking ABOUT this markup
    m = _LEAK_RE.search(masked)
    return m.start() if m else None


def _clip(text):
    if len(text) <= _MAX_SCAN:
        return text
    return text[-_MAX_SCAN:]


def inspect(text, *, prompt_text=None, tools_offered=False, finish_reason=None):
    """Judge one answer's text.

    Returns {"ok": bool, "reasons": [...], "salvage": str|None}. `reasons` may
    hold "truncated" even when ok is True (informational). `salvage` is the
    clean answer before the junk started, or None when the junk is not just a
    tail (nothing meaningful precedes it). Never raises: any internal error
    reports ok=True -- the gate must never be the thing that loses an answer."""
    result = {"ok": True, "reasons": [], "salvage": None}
    try:
        if not isinstance(text, str) or not text.strip():
            return result
        if prompt_text is not None and not isinstance(prompt_text, str):
            prompt_text = str(prompt_text)
        if finish_reason == "length":
            result["reasons"].append("truncated")
        # Long answers: judge the tail only. A loop or leak that ran to the cap
        # is AT the tail, and indices below are shifted back onto `text`.
        offset = max(0, len(text) - _MAX_SCAN)
        body = _clip(text)
        masked = _mask_code(body)
        cuts = []
        checks = (
            ("runaway_script", lambda: _script_switch(body, masked, prompt_text,
                                                      finish_reason)),
            ("repetition", lambda: _line_loop(masked, prompt_text)),
            ("repetition", lambda: _tail_loop(masked, prompt_text)),
            ("tool_markup", lambda: _markup_leak(masked, prompt_text, tools_offered)),
        )
        for reason, fn in checks:
            cut = fn()
            if cut is None:
                continue
            if reason not in result["reasons"]:
                result["reasons"].append(reason)
            cuts.append(offset + cut)
        if not cuts:
            return result
        result["ok"] = False
        clean = text[:min(cuts)].rstrip()
        if re.search(r"\w", clean):
            result["salvage"] = clean
        return result
    except Exception:                                            # noqa: BLE001
        return {"ok": True, "reasons": [], "salvage": None}
