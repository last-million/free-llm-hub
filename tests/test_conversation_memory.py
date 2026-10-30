"""Durable conversation memory (convmem.py + app `_cmem_*` + the MCP tools).

OWNER DECISION 2026-10-24: "keep the context reach at max 400K and then
auto-compact, no matter which CLI, with perfect long unlimited memory, smartly,
like a DATABASE the agent can use, to avoid losing tokens and context window".
The hub clears old tool output and drops old turns to keep requests small; this
file pins the archive that keeps the originals, the two capability-based MCP
tools that read it, the stub / note that point at it, and the append-only
union of standing rules that no recap rewrite can erode.

Hermetic: temp SQLite files, a fake clock, fake providers, no network, no real
config. tests/conftest.py turns the feature OFF for every other test file and
points the default archive at a per-test temp file; this file turns it back on.
"""
import base64
import json
import logging
import os
import re
import sqlite3
import threading

import pytest

import app as A
import convmem
import ctxwin
import hub_mcp
from test_old_tool_result_clearing import (     # noqa: F401  (shared helpers)
    TOOLS, _Lcg, _Resp, _code_file, _history, _mini, _tool_call)
from test_context_window_management import (    # noqa: F401  (imported fixture + helpers)
    PINNED, _Resp as _CwmResp, _answer, _sse_chunks, quiet)

CONV = "hdr:sess-1"
FLAG_NAMES = ("conversation_memory", "conversation_memory_note")


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #

class _Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def arch(tmp_path):
    a = convmem.Archive(lambda: str(tmp_path / "a.db"))
    yield a
    a.close()


@pytest.fixture
def req():
    """A request context whose conversation key is CONV, as _ctx_begin sets it."""
    with A.app.test_request_context("/v1/chat/completions"):
        A._ctx_set("_ctx_conv", CONV)
        yield CONV


def _item(text, kind="tool_result", tool="", turn=1):
    return {"text": text, "kind": kind, "tool": tool, "turn": turn}


def _text_of(rpc_out):
    return rpc_out["result"]["content"][0]["text"]


def _rpc(name, **arguments):
    out, _status = hub_mcp.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                       "params": {"name": name, "arguments": arguments}})
    return out


def _full(archive, key, item_id):
    """An archived item, every page joined."""
    text, off = "", 0
    while True:
        r = archive.get(key, item_id, off)
        if r is None:
            return None
        text += r["text"]
        if r["next_offset"] is None:
            return text
        off = r["next_offset"]


def _flags_off(monkeypatch, *names):
    real = A.config.get_flag
    monkeypatch.setattr(A.config, "get_flag",
                        lambda k, d=False: False if k in names else real(k, d))


def _system_texts(msgs):
    return [m["content"] for m in msgs if m.get("role") == "system"]


def _note_of(msgs):
    notes = [t for t in _system_texts(msgs) if t.startswith("Hub memory:")]
    return notes[0] if notes else None


def _key_in(note):
    m = re.search(r'memory_key="(mk_[A-Za-z0-9_-]+)"', note or "")
    return m.group(1) if m else None


# =========================================================================== #
# convmem: the archive
# =========================================================================== #

def test_round_trip_search_and_get(arch):
    text = "def handleRequest(req):\n    return parse_config(req)\n" * 20
    key = arch.memory_key(CONV)
    ids = arch.put_many(CONV, [_item(text, tool="read: src/server.py", turn=3)])
    assert ids == {convmem.content_id(text)}
    hits = arch.search(key, "handleRequest")
    assert [h["id"] for h in hits] == [convmem.content_id(text)]
    assert hits[0]["tool"] == "read: src/server.py" and hits[0]["turn"] == 3
    got = arch.get(key, hits[0]["id"])
    assert got["text"] == text and got["next_offset"] is None and got["kind"] == "tool_result"
    assert arch.has_items(CONV) and not arch.has_items("other")


def test_the_id_is_the_content_hash_the_stub_uses():
    for t in ("abc", "x" * 5000, "ünï\ncode ☃"):
        assert ctxwin.mem_id(t) == convmem.content_id(t)
        assert len(convmem.content_id(t)) == convmem.ID_LEN == 12


@pytest.mark.parametrize("fts, expect_mode", [(None, None), ("unicode61", "unicode61"),
                                              (False, "none")])
def test_every_search_mode_finds_what_a_model_types(tmp_path, fts, expect_mode):
    a = convmem.Archive(lambda: str(tmp_path / ("m%s.db" % fts)), fts=fts)
    try:
        if expect_mode:
            assert a.mode == expect_mode
        key = a.memory_key(CONV)
        a.put_many(CONV, [
            _item("src/mod_12.py has 3 handlers\nraise ValueError('retry missing')",
                  tool="read: src/mod_12.py"),
            _item("pytest -q\n12 passed in 3.2s", tool="bash: pytest -q"),
            _item("something unrelated about databases")])
        for query, expect in (("pytest passed", "bash: pytest -q"),
                              ("mod_12.py", "read: src/mod_12.py"),
                              ("retry missing", "read: src/mod_12.py"),
                              ("foo-bar mod_12", "read: src/mod_12.py")):    # OR retry
            hits = a.search(key, query)
            assert hits and hits[0]["tool"] == expect, (a.mode, query, hits)
        assert a.search(key, "zzzzqqqq") == []
    finally:
        a.close()


def test_trigram_finds_a_piece_of_an_identifier(tmp_path):
    a = convmem.Archive(lambda: str(tmp_path / "t.db"), fts="trigram")
    try:
        if a.mode != "trigram":
            pytest.skip("this SQLite build has no trigram tokenizer")
        key = a.memory_key(CONV)
        a.put_many(CONV, [_item("export function handleRequestTimeout(ctx) {}")])
        assert a.search(key, "RequestTime")        # a substring, case-insensitive
    finally:
        a.close()


def test_a_broken_fts_query_falls_back_to_like(arch, monkeypatch):
    key = arch.memory_key(CONV)
    arch.put_many(CONV, [_item("the quick brown fox jumps over the lazy dog")])

    def boom(*a, **k):
        raise sqlite3.OperationalError("fts5: syntax error near ...")
    monkeypatch.setattr(arch, "_fts_rows", boom)
    hits = arch.search(key, "brown fox")
    assert hits and "brown fox" in hits[0]["excerpt"]


@pytest.mark.parametrize("q", ['foo"bar', "AND OR NOT", "((", "*", "a:b", "NEAR(x y)", '"',
                               "", "   ", "\\", "%", "_", "col:val*", "-x", "x -y", "'; DROP",
                               "☃", "a" * 5000])
def test_a_hostile_query_never_raises(arch, q):
    key = arch.memory_key(CONV)
    arch.put_many(CONV, [_item("some archived text with foo bar and x y")])
    out = arch.search(key, q)
    assert out is None or isinstance(out, list)
    assert isinstance(arch.search_text(key, q), str)


def test_the_same_text_is_stored_once(tmp_path):
    p = str(tmp_path / "d.db")
    a, b = convmem.Archive(lambda: p), convmem.Archive(lambda: p)
    try:
        text = "repeated tool output " * 100
        a.put_many(CONV, [_item(text)])
        a.put_many(CONV, [_item(text), _item(text)])
        b.put_many(CONV, [_item(text)])               # another process, same file
        assert a.stats()["items"] == 1 and a.stats()["bytes"] == b.stats()["bytes"]
        key = a.memory_key(CONV)
        assert len(a.search(key, "repeated tool output")) == 1
    finally:
        a.close()
        b.close()


def test_memory_survives_a_restart(tmp_path):
    """OWNER 2026-10-24, persistence #2+#3: reopening a conversation later and
    surviving a hub restart / auto-update. The archive is a file on disk; a
    FRESH Archive on the same path (a new hub process after a restart) still has
    every item and rule and resolves the SAME memory_key -- so a conversation
    whose old tool output was cleared before the restart can still fetch its
    originals back, and the model continues with its context intact."""
    p = str(tmp_path / "persist.db")
    before = convmem.Archive(lambda: p)
    try:
        before.put_many(CONV, [_item(
            "the deploy command was: npm run build && node server.js")])
        before.add_rules(CONV, ["Always deploy from the project root.",
                                "PROJECT = billtrack"])
        key = before.memory_key(CONV)
        assert before.stats()["items"] == 1
    finally:
        before.close()                           # the hub shuts down / auto-updates
    after = convmem.Archive(lambda: p)            # a fresh hub process, same file
    try:
        assert after.memory_key(CONV) == key                     # same capability key
        assert after.stats()["items"] == 1                       # the item survived
        hits = after.search(key, "deploy command")
        assert hits and any("npm run build" in h["excerpt"] for h in hits)
        assert "node server.js" in after.get_text(key, hits[0]["id"])
        rules = after.rules(CONV)
        assert any("project root" in r for r in rules)
        assert any("billtrack" in r for r in rules)
    finally:
        after.close()


def test_the_repeating_history_costs_no_queries(arch, monkeypatch):
    """A CLI resends the whole history every turn; the second look at a text
    already filed must not touch the database."""
    arch.put_many(CONV, [_item("a result " * 50)])
    arch._conn()
    real = arch._db
    calls = []

    class Spy:
        def execute(self, *a, **k):
            calls.append(a[0][:30])
            return real.execute(*a, **k)

        def __getattr__(self, n):
            return getattr(real, n)
    monkeypatch.setattr(arch, "_db", Spy())
    assert arch.put_many(CONV, [_item("a result " * 50)]) == {convmem.content_id("a result " * 50)}
    assert calls == []


def test_large_items_come_back_in_bounded_pages(arch):
    big = "".join("line %05d some archived output\n" % i for i in range(1200))     # ~37 KB
    key = arch.memory_key(CONV)
    (cid,) = arch.put_many(CONV, [_item(big)])
    first = arch.get(key, cid)
    assert len(first["text"]) <= convmem.RESULT_MAX_CHARS and first["next_offset"] == len(first["text"])
    assert _full(arch, key, cid) == big
    assert len(arch.get_text(key, cid)) <= convmem.RESULT_MAX_CHARS + 600
    assert "offset=%d" % first["next_offset"] in arch.get_text(key, cid)
    many = arch.put_many(CONV, [_item("needle line %d\n" % i + "x" * 900) for i in range(40)])
    assert len(many) == 40
    assert len(arch.search_text(key, "needle")) <= convmem.RESULT_MAX_CHARS + 50


def test_an_oversized_item_keeps_head_and_tail(arch):
    n = convmem.MAX_ITEM_CHARS * 2
    text = "HEAD" + "m" * n + "TAIL"
    key = arch.memory_key(CONV)
    (cid,) = arch.put_many(CONV, [_item(text)])
    out = _full(arch, key, cid)
    assert out.startswith("HEAD") and out.endswith("TAIL") and "omitted by the hub archive" in out
    assert len(out) < convmem.MAX_ITEM_CHARS + 200


# ---- bounds ---------------------------------------------------------------- #

def test_an_oversized_conversation_loses_its_oldest_items_first(tmp_path):
    clock = _Clock()
    a = convmem.Archive(lambda: str(tmp_path / "c.db"), clock=clock, max_conv_bytes=6000)
    try:
        key = a.memory_key(CONV)
        ids = []
        for i in range(12):
            clock.t += 1
            ids += list(a.put_many(CONV, [_item("item %02d " % i + "y" * 990)]))
        a.prune()
        assert a.get(key, ids[0]) is None and a.get(key, ids[1]) is None
        assert a.get(key, ids[-1]) is not None
        assert a.stats()["bytes"] <= 6000
        # an evicted text can be filed again (the "already seen" memory forgot it)
        clock.t += 1
        assert a.put_many(CONV, [_item("item 00 " + "y" * 990)])
        assert a.get(key, ids[0]) is not None
    finally:
        a.close()


def test_the_total_cap_evicts_the_least_recently_used_conversation(tmp_path):
    clock = _Clock()
    a = convmem.Archive(lambda: str(tmp_path / "g.db"), clock=clock, max_total_bytes=7000)
    try:
        clock.t += 1
        ka = a.memory_key("old")
        a.put_many("old", [_item("O" * 2400)])
        clock.t += 100
        kb = a.memory_key("new")
        a.put_many("new", [_item("N" * 2400)])
        clock.t += 100
        a.put_many("new", [_item("M" * 2400)])
        a.prune()
        assert a.search(ka, "OOOO") == []                    # emptied, but...
        assert a.memory_key("old") == ka                     # ...its key still stands
        assert a.search(kb, "NNNN") and a.search(kb, "MMMM")
    finally:
        a.close()


def test_a_conversation_idle_past_the_ttl_is_forgotten_with_its_key(tmp_path):
    clock = _Clock()
    a = convmem.Archive(lambda: str(tmp_path / "t.db"), clock=clock, ttl=1000)
    try:
        key = a.memory_key(CONV)
        a.put_many(CONV, [_item("old output " * 40)])
        a.add_rules(CONV, ['user said: "Always use tabs."'])
        clock.t += 1001
        a.prune()
        assert a.search(key, "old output") is None           # the key is gone
        assert a.rules(CONV) == [] and not a.has_items(CONV)
        assert a.memory_key(CONV) != key                     # a fresh conversation
    finally:
        a.close()


def test_use_keeps_a_conversation_alive(tmp_path):
    clock = _Clock()
    a = convmem.Archive(lambda: str(tmp_path / "u.db"), clock=clock, ttl=1000)
    try:
        key = a.memory_key(CONV)
        a.put_many(CONV, [_item("kept output " * 40)])
        for _ in range(4):
            clock.t += 600
            assert a.search(key, "kept output")
            a.prune()
        assert a.search(key, "kept output")
    finally:
        a.close()


def test_eviction_keeps_the_full_text_index_in_step(tmp_path):
    """The delete triggers must really remove what they evict from the FTS index
    (a silent failure there would make pruning raise and the file grow forever)."""
    clock = _Clock()
    for tok in ("trigram", "unicode61"):
        a = convmem.Archive(lambda tok=tok: str(tmp_path / ("i-%s.db" % tok)), clock=clock,
                            fts=tok, max_conv_bytes=8000)
        try:
            if a.mode != tok:
                continue
            key = a.memory_key(CONV)
            for i in range(30):
                clock.t += 1
                a.put_many(CONV, [_item("evictable output %02d " % i + "w" * 700)])
            a.prune()
            assert a.stats()["bytes"] <= 8000 and a.stats()["items"] < 30
            db = a._conn()
            db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('integrity-check')")
            n_idx = db.execute("SELECT count(*) FROM chunks_fts_docsize").fetchone()[0]
            n_rows = db.execute("SELECT count(*) FROM chunks").fetchone()[0]
            assert n_idx == n_rows > 0, (tok, n_idx, n_rows)
            assert a.search(key, "evictable output 29")
        finally:
            a.close()


def test_one_prune_pass_is_time_bounded_and_resumes(tmp_path, monkeypatch):
    clock = _Clock()
    a = convmem.Archive(lambda: str(tmp_path / "b.db"), clock=clock, max_conv_bytes=3000)
    try:
        key = a.memory_key(CONV)
        for i in range(40):
            clock.t += 1
            a.put_many(CONV, [_item("bounded %02d " % i + "v" * 500)])
        monkeypatch.setattr(convmem, "PRUNE_BUDGET_SECONDS", 0.0)       # no time at all
        a.prune()
        assert a.stats()["bytes"] > 3000                                # nothing done yet, nothing broken
        monkeypatch.setattr(convmem, "PRUNE_BUDGET_SECONDS", 5.0)
        a.prune()
        assert a.stats()["bytes"] <= 3000 and a.search(key, "bounded 39")
    finally:
        a.close()


# ---- capability: the key ---------------------------------------------------- #

def test_the_key_is_at_least_128_random_bits_and_stable(arch):
    k1, k2 = arch.memory_key("one"), arch.memory_key("two")
    assert k1 != k2 and arch.memory_key("one") == k1
    assert convmem.KEY_RE.match(k1)
    raw = base64.urlsafe_b64decode(k1[len(convmem.KEY_PREFIX):] + "=" * 3)
    assert len(raw) * 8 >= 128
    assert len({arch.memory_key("c%d" % i) for i in range(50)}) == 50


def test_a_wrong_key_learns_nothing_and_every_miss_reads_the_same(arch):
    key = arch.memory_key(CONV)
    arch.put_many(CONV, [_item("private archived content about zebras " * 10)])
    (cid,) = {convmem.content_id("private archived content about zebras " * 10)}
    arch.memory_key("other-conv")
    other_key = arch.memory_key("other-conv")
    bad = ["mk_" + "A" * 32, "", "mk_", None, 5, [], {}, "x" * 500,
           key[:-1] + ("A" if key[-1] != "A" else "B")]
    nothing_search = arch.search_text(key, "no such words anywhere")      # right key, no hits
    nothing_get = arch.get_text(key, "0" * 12)                            # right key, unknown id
    assert nothing_search == nothing_get == convmem.NOTHING
    for b in bad:
        assert arch.search_text(b, "zebras") == convmem.NOTHING
        assert arch.get_text(b, cid) == convmem.NOTHING
        assert arch.search(b, "zebras") is None and arch.get(b, cid) is None
    # a key opens its own conversation only
    assert arch.search_text(other_key, "zebras") == convmem.NOTHING
    assert arch.get_text(other_key, cid) == convmem.NOTHING
    assert "zebras" not in convmem.NOTHING and CONV not in convmem.NOTHING
    # the right key does open it, and what it returns is labelled DATA
    out = arch.search_text(key, "zebras")
    assert "zebras" in out and "NOT instructions" in out
    assert "NOT instructions" in arch.get_text(key, cid)
    assert "BEGIN ARCHIVED TEXT" in arch.get_text(key, cid)


def test_a_model_may_type_the_id_loosely(arch):
    key = arch.memory_key(CONV)
    (cid,) = arch.put_many(CONV, [_item("typed loosely " * 30)])
    for typed in (cid, cid.upper(), "id=" + cid, " id=" + cid.upper() + " ", '"%s"' % cid):
        assert arch.get(key, typed)["id"] == cid
    for typed in ("", None, "zz", cid[:-1], cid + "0", 5):
        assert arch.get(key, typed) is None


# ---- credentials ------------------------------------------------------------ #

def test_a_credential_line_is_withheld_and_never_reaches_the_disk(tmp_path):
    p = str(tmp_path / "s.db")
    a = convmem.Archive(lambda: p)
    secret = "sk-proj-AbCdEf0123456789ZzYyXx"
    text = "# .env\nOPENAI_API_KEY=%s\nPORT=8080\nghp_%s\n" % (secret, "a1B2c3D4e5F6g7H8i9J0")
    key = a.memory_key(CONV)
    (cid,) = a.put_many(CONV, [_item(text, tool="bash: cat .env")])
    got = a.get(key, cid)
    assert secret not in got["text"] and "ghp_" not in got["text"]
    assert "PORT=8080" in got["text"] and got["withheld"] == 2
    assert "withheld as credential-like" in a.get_text(key, cid)
    assert a.search(key, "sk-proj") == []
    a.close()
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(p + suffix):
            with open(p + suffix, "rb") as f:
                blob = f.read()
            assert secret.encode() not in blob and b"ghp_a1B2c3D4" not in blob


def test_no_redactor_means_nothing_is_stored(tmp_path, monkeypatch):
    def boom(_t):
        raise ImportError("memory module unavailable")
    a = convmem.Archive(lambda: str(tmp_path / "f.db"), redact=boom)
    try:
        assert a.put_many(CONV, [_item("harmless text " * 20)]) == set()
        assert a.stats()["items"] == 0
    finally:
        a.close()
    monkeypatch.setattr(convmem, "_secret_re", None)
    monkeypatch.setattr(convmem, "_load_secret_re",
                        lambda: (_ for _ in ()).throw(ImportError("gone")))
    b = convmem.Archive(lambda: str(tmp_path / "f2.db"))
    try:
        assert b.put_many(CONV, [_item("harmless text " * 20)]) == set()
        assert b.stats()["items"] == 0
    finally:
        b.close()


# ---- robustness ------------------------------------------------------------- #

def test_malformed_input_never_raises(arch):
    junk = [None, 5, "text", [], {}, {"text": 5}, {"text": ""}, {"text": "   "},
            {"text": b"bytes"}, {"text": "ok " * 50, "turn": "NaN", "kind": 5, "tool": 7},
            {"text": "lone \ud800 surrogate " * 20}, {"text": "x" * 20, "kind": None}]
    assert isinstance(arch.put_many(CONV, junk), set)
    assert isinstance(arch.put_many(None, junk), set) and isinstance(arch.put_many("", None), set)
    assert arch.put_many(CONV, "not a list") == set()
    key = arch.memory_key(CONV)
    assert arch.memory_key(None) is None and arch.memory_key("") is None
    for q in (None, 5, [], {}):
        assert isinstance(arch.search_text(key, q), str)
    for i in (None, 5, [], {}, "id=", "../../etc/passwd"):
        assert arch.get_text(key, i) == convmem.NOTHING
    assert arch.get(key, "0" * 12, offset="x", limit="y") is None
    assert arch.add_rules(CONV, [None, 5, "", "   ", ["x"]]) == 0
    assert arch.rules(None) == [] and arch.add_rules(None, ["abc def ghi"]) == 0
    # and the archive still works afterwards (no transaction was left open)
    assert arch.put_many(CONV, [_item("still works " * 20)])


@pytest.mark.parametrize("path_fn", [lambda: None, lambda: "", None])
def test_an_unavailable_archive_answers_empty_and_never_raises(tmp_path, path_fn):
    a = convmem.Archive(path_fn if path_fn is not None else (lambda: 1 / 0))
    assert a.put_many(CONV, [_item("x" * 300)]) == set()
    assert a.memory_key(CONV) is None and not a.has_items(CONV)
    assert a.search("mk_" + "A" * 32, "x") is None and a.get_text("mk_" + "A" * 32, "0" * 12) \
        == convmem.NOTHING
    assert a.rules(CONV) == [] and a.add_rules(CONV, ["abcd efgh"]) == 0
    assert a.stats() == {"available": False}
    a.prune()


def test_a_path_that_is_a_directory_is_survived(tmp_path):
    a = convmem.Archive(lambda: str(tmp_path))
    assert a.put_many(CONV, [_item("x" * 300)]) == set() and a.memory_key(CONV) is None


def test_hop_threads_can_file_the_same_history_at_once(tmp_path):
    a = convmem.Archive(lambda: str(tmp_path / "th.db"))
    errors = []
    texts = ["shared tool result %03d " % i + "z" * 400 for i in range(60)]

    def worker(shift):
        try:
            for i in range(60):
                a.put_many(CONV, [_item(texts[(i + shift) % 60])])
        except Exception as exc:                                  # noqa: BLE001
            errors.append(exc)
    ts = [threading.Thread(target=worker, args=(s * 7,)) for s in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    try:
        assert not errors and a.stats()["items"] == 60
        assert all(convmem.content_id(t) in a.put_many(CONV, [_item(t)]) for t in texts)
    finally:
        a.close()


# ---- the standing-rules ledger ---------------------------------------------- #

def test_rules_are_an_append_only_union_with_exact_keys(arch):
    assert arch.add_rules(CONV, ['user said: "Always use tabs."', 'user said: "Never use tabs."']) == 2
    assert arch.add_rules(CONV, ['user said: "Always use tabs."']) == 0        # exact repeat
    # near-duplicates are DIFFERENT rules: no fuzzy resolution may drop one
    assert arch.add_rules(CONV, ['user said: "Always use tabs"']) == 1
    # an explicit `NAME = value` is one setting: the newest value wins
    arch.add_rules(CONV, ["Decision: port = 8080", "Decision: db = sqlite"])
    arch.add_rules(CONV, ["Decision: port = 9090"])
    rules = arch.rules(CONV)
    assert "Decision: port = 9090" in rules and "Decision: port = 8080" not in rules
    assert "Decision: db = sqlite" in rules
    assert rules.index('user said: "Always use tabs."') < rules.index('user said: "Never use tabs."')
    assert convmem.rule_key("Decision: port = 1") == convmem.rule_key("port=2") == "kv:port"
    assert convmem.rule_key("use  TABS") == convmem.rule_key("use tabs")
    assert convmem.rule_key("use tabs") != convmem.rule_key("use tab")


def test_the_ledger_is_bounded(arch):
    for i in range(convmem.MAX_RULES + 40):
        arch.add_rules(CONV, ["Rule number %04d must hold." % i])
    rules = arch.rules(CONV)
    assert len(rules) == convmem.MAX_RULES
    assert rules[-1] == "Rule number %04d must hold." % (convmem.MAX_RULES + 39)
    assert "Rule number 0000 must hold." not in rules


# =========================================================================== #
# ctxwin: the stub and the hook
# =========================================================================== #

def test_the_default_stub_is_byte_for_byte_what_it_always_was():
    body = "abc" * 1000
    expect = ("[tool output cleared by the hub to save context (was ~3000 chars): %s. "
              "Re-run the command if you need it.]" % ("abc" * 53 + "a"))
    assert ctxwin.cleared_stub(body) == expect


def test_a_stub_with_an_archive_id_says_how_to_get_it_back():
    body = "line one\n\n  line   two\t" + "x" * 3000
    cid = ctxwin.mem_id(body)
    stub = ctxwin.cleared_stub(body, archived_id=cid)
    assert stub.startswith(ctxwin.CLEARED_RESULT_PREFIX) and "\n" not in stub
    assert ("Fetch it with the hub tool memory_get id=%s if available, or re-run the command.]"
            % cid) in stub
    assert stub == ctxwin.cleared_stub(body, archived_id=cid)          # a function of the text
    assert stub != ctxwin.cleared_stub(body + "!", archived_id=ctxwin.mem_id(body + "!"))
    assert len(stub) < len(body)


def test_clearing_without_a_stub_hook_is_unchanged():
    msgs = _mini(14, size=2500)
    a, sa = ctxwin.clear_old_tool_results(msgs, min_tokens=0)
    b, sb = ctxwin.clear_old_tool_results(msgs, min_tokens=0, stub_fn=None)
    assert a == b and sa == sb and sa["cleared"] > 0


def test_the_hook_is_asked_only_for_results_that_are_cleared():
    msgs = _mini(14, size=2500, fail=(3,))
    asked = []

    def hook(text, msg, slot):
        asked.append(msg["tool_call_id"])
        return ctxwin.cleared_stub(text, archived_id=ctxwin.mem_id(text))
    out, st = ctxwin.clear_old_tool_results(msgs, min_tokens=0, stub_fn=hook)
    assert len(asked) == st["cleared"] > 0
    cleared = [m["tool_call_id"] for m, o in zip(out, msgs) if m is not o]
    assert sorted(asked) == sorted(cleared)
    assert "call_03" not in asked                                    # a failing step is kept
    assert "call_13" not in asked                                    # so are the newest units


def test_a_hook_with_prepare_is_handed_every_result_once_before_any_stub():
    msgs = _mini(14, size=2500, fail=(3,))
    order = []

    class Hook:
        def prepare(self, entries):
            order.append(("prepare", [m["tool_call_id"] for _t, m, _s in entries]))

        def __call__(self, text, msg, slot):
            order.append(("stub", msg["tool_call_id"]))
            return None
    out, st = ctxwin.clear_old_tool_results(msgs, min_tokens=0, stub_fn=Hook())
    assert order[0][0] == "prepare" and len(order[0][1]) == st["cleared"]
    assert [o[1] for o in order[1:]] == order[0][1]                  # then one stub each, same order
    assert sum(o[0] == "prepare" for o in order) == 1
    plain, _ = ctxwin.clear_old_tool_results(msgs, min_tokens=0)
    assert out == plain                                              # the hook said None everywhere

    class Broken(Hook):
        def prepare(self, entries):
            raise RuntimeError("archive down")
    out2, _ = ctxwin.clear_old_tool_results(msgs, min_tokens=0, stub_fn=Broken())
    assert out2 == plain


def test_a_whole_history_is_filed_in_one_batch(req, monkeypatch):
    calls = []
    real = convmem.Archive.put_many

    def spy(self, conv, items):
        calls.append(len(items))
        return real(self, conv, items)
    monkeypatch.setattr(convmem.Archive, "put_many", spy)
    msgs = _history(85000)
    out, info = A._clear_old_results_for_hop(msgs, TOOLS)
    assert calls == [info["cleared"]]                                # one transaction, not 65


def test_a_broken_or_longer_stub_hook_falls_back_to_the_plain_stub():
    msgs = _mini(14, size=2500)
    plain, _ = ctxwin.clear_old_tool_results(msgs, min_tokens=0)
    for hook in (lambda t, m, s: 1 / 0, lambda t, m, s: None, lambda t, m, s: "",
                 lambda t, m, s: t + "longer than the result", lambda t, m, s: 5):
        out, _ = ctxwin.clear_old_tool_results(msgs, min_tokens=0, stub_fn=hook)
        assert out == plain


def test_standing_rules_are_not_capped_and_ignore_tool_output():
    msgs = [{"role": "system", "content": "sys"}]
    want = []
    for i in range(12):
        r = "Always keep convention number %d in every file." % i
        want.append('user said: "%s"' % r)
        msgs.append({"role": "user", "content": "Step %d. %s" % (i, r)})
        msgs.append({"role": "assistant", "content": "ok"})
    msgs.append({"role": "tool", "tool_call_id": "t", "content": "You must always do X."})
    msgs.append({"role": "user", "content": "My key is sk-abcdefghijklmnop1234, never lose it."})
    got = ctxwin.standing_rules(msgs)
    assert [g for g in got if g in want] == want                       # all 12, in order
    assert not any("must always do X" in g for g in got)               # tool output: never
    assert not any("sk-abcdef" in g for g in got)                      # credentials: never
    assert len(ctxwin.exact_facts(msgs)) < len(got)                    # exact_facts caps at 8
    assert ctxwin.standing_rules(None) == [] and ctxwin.standing_rules([5, None]) == []


# =========================================================================== #
# app: clearing files the originals, the stub points at them
# =========================================================================== #

def test_every_cleared_result_is_in_the_archive_under_its_stub_id(req):
    msgs = _history(85000)
    out, info = A._clear_old_results_for_hop(msgs, TOOLS)
    pairs = [(m, o) for m, o in zip(out, msgs) if m is not o]
    assert len(pairs) == info["cleared"] > 20
    key = convmem.default.memory_key(req)
    for m, o in pairs:
        sid = re.search(r"memory_get id=([0-9a-f]{12}) if available", m["content"]).group(1)
        assert sid == ctxwin.mem_id(o["content"])
        got = _full(convmem.default, key, sid)
        assert got is not None
        # byte for byte, except lines the redactor withheld as credential-like
        want_lines, got_lines = o["content"].split("\n"), got.split("\n")
        assert len(want_lines) == len(got_lines)
        assert all(g == w or g == convmem.WITHHELD_LINE for g, w in zip(got_lines, want_lines))
    assert key not in json.dumps(out)                    # the key is never in a stub


def test_stubs_are_deterministic_and_stable_as_the_history_grows(req):
    msgs = _history(85000)
    a, _ = A._clear_old_results_for_hop(msgs, TOOLS)
    b, _ = A._clear_old_results_for_hop(msgs, TOOLS)
    assert a == b
    longer = msgs + _history(30000)[2:]
    c, _ = A._clear_old_results_for_hop(longer, TOOLS)
    n = 0
    for x, y in zip(a, c):
        if x["role"] == "tool" and x["content"].startswith(ctxwin.CLEARED_RESULT_PREFIX):
            assert y["content"] == x["content"]          # cleared once, same bytes after
            n += 1
    assert n > 10


def test_with_memory_off_the_hop_is_byte_identical_and_writes_nothing(monkeypatch, req):
    msgs = _history(85000)
    ref, ref_info = ctxwin.clear_old_tool_results(msgs, est_tokens=A._est_tokens(msgs, TOOLS))

    def check(how):
        out, info = A._clear_old_results_for_hop(msgs, TOOLS)
        assert out == ref and info["cleared"] == ref_info["cleared"], how
        assert "memory_get" not in json.dumps(out)
        assert A._cmem_apply_note(out, TOOLS) is out
        assert A._cmem_archive_cb() is None and A._cmem_archive_kwargs() == {}
        assert not os.path.exists(convmem.PATH), how
        assert A._cmem_mcp_search("mk_" + "A" * 32, "x") == convmem.NOTHING

    with monkeypatch.context() as mp:                      # the flag, as an owner sets it
        real = A.config.get_flag
        mp.setattr(A.config, "get_flag",
                   lambda k, d=False: False if k in FLAG_NAMES else real(k, d))
        check("flag")
    with monkeypatch.context() as mp:                      # a helper that says no
        mp.setattr(A, "_cmem_on", lambda: False)
        mp.setattr(A, "_cmem_note_on", lambda: False)
        check("helper")
    with monkeypatch.context() as mp:                      # an unreadable config reads as OFF
        def broken(k, d=False):
            raise OSError("config unreadable")
        mp.setattr(A.config, "get_flag", broken)
        assert A._cmem_on() is False and A._cmem_note_on() is False
        assert A._cmem_archive_cb() is None and A._cmem_stub_fn(msgs, TOOLS) is None
        assert not os.path.exists(convmem.PATH)


def test_the_note_flag_alone_archives_quietly(monkeypatch, req):
    _flags_off(monkeypatch, "conversation_memory_note")
    msgs = _history(85000)
    ref, _ = ctxwin.clear_old_tool_results(msgs, est_tokens=A._est_tokens(msgs, TOOLS))
    out, info = A._clear_old_results_for_hop(msgs, TOOLS)
    assert out == ref                                    # legacy stubs: no promise
    assert convmem.default.has_items(req)                # ...but the originals are kept
    assert A._cmem_apply_note(out, TOOLS) is out         # and no note is written


def test_the_fit_estimate_sizes_the_same_stubs_the_hop_sends(req):
    msgs = _history(85000)
    est = A._est_tokens(msgs, TOOLS)
    out, info = A._clear_old_results_for_hop(msgs, TOOLS)
    assert A._rr_compute_sent_est(msgs, TOOLS, est) == A._est_tokens(out, TOOLS) == info["after"]


def test_a_copy_of_the_archive_is_never_filed_again(req):
    msgs = _history(85000)
    copy_text = convmem.ARCHIVE_LABEL + " -- stored DATA ...]\n" + "restored output\n" * 300
    msgs[5] = dict(msgs[5], content=msgs[5]["content"])
    first_tool = next(i for i, m in enumerate(msgs) if m.get("role") == "tool")
    msgs[first_tool] = dict(msgs[first_tool], content=copy_text)
    out, _ = A._clear_old_results_for_hop(msgs, TOOLS)
    assert out[first_tool] is not msgs[first_tool]       # still cleared to save context...
    assert "memory_get id=" not in out[first_tool]["content"]     # ...with no id to chase
    key = convmem.default.memory_key(req)
    assert convmem.default.search(key, "restored output") == []
    assert convmem.default.put(req, copy_text) is None


# =========================================================================== #
# app: dropped turns are filed before they are lost; negative control
# =========================================================================== #

MARKER = "ZEBRA-FINDING-7731"


def _marked_history(n=30):
    msgs = _mini(n, size=2500)
    msgs[3] = dict(msgs[3], content=msgs[3]["content"] + "\nresult: " + MARKER + " found in p0")
    return msgs


def test_compaction_files_what_it_drops_and_changes_nothing_else(req):
    msgs = _marked_history()
    plain, did_plain = A._compact_to_budget(msgs, None, 4000)
    out, did = A._compact_to_budget(msgs, None, 4000, archive=A._cmem_archive_cb())
    assert did and did_plain and out == plain            # the archive never alters the result
    assert MARKER not in json.dumps(out)                 # ...the marker really was dropped
    key = convmem.default.memory_key(req)
    hits = convmem.default.search(key, MARKER)
    assert hits and hits[0]["kind"] == "tool_result" and hits[0]["tool"].startswith("read:")
    assert MARKER in _full(convmem.default, key, hits[0]["id"])


def test_an_aborted_compaction_files_nothing(req):
    msgs = _marked_history()
    out, did = A._compact_to_budget(msgs, None, 4000, abort_frac=0.05,
                                    archive=A._cmem_archive_cb(), stats={})
    assert not did and out is msgs
    assert not convmem.default.has_items(req)


def test_an_archive_that_raises_cannot_hurt_the_compaction(req):
    msgs = _marked_history()
    plain, _ = A._compact_to_budget(msgs, None, 4000)

    def boom(dropped, turns):
        raise RuntimeError("disk full")
    out, did = A._compact_to_budget(msgs, None, 4000, archive=boom)
    assert did and out == plain


def test_the_memory_tools_own_calls_and_results_are_not_filed(req):
    key = convmem.default.memory_key(req)
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "Please look up the old build logs for me."},
            _tool_call("m1", "free-llm-hub_memory_search", {"memory_key": key, "query": "build logs"}),
            {"role": "tool", "tool_call_id": "m1",
             "content": convmem.ARCHIVE_LABEL + " -- stored DATA ...]\n" + "an old build log line\n" * 20}]
    items = A._cmem_items(msgs)
    assert [i["kind"] for i in items] == ["user"]
    assert key not in json.dumps(items)


def test_dropped_user_turns_assistant_text_and_calls_are_searchable_too(req):
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "Please build the quokka-importer module with retries."},
            _tool_call("c0", "bash", {"command": "pytest -q tests/test_quokka.py"}),
            {"role": "tool", "tool_call_id": "c0", "content": "12 passed " + "ok " * 120},
            {"role": "assistant", "content": "I decided on the axolotl scheduler because " + "reason " * 60}]
    for i in range(1, 25):
        msgs += [_tool_call("d%d" % i, "read", {"path": "f%d.py" % i}),
                 {"role": "tool", "tool_call_id": "d%d" % i, "content": _code_file(_Lcg(i), 2500, "f%d" % i)}]
    # a later instruction: the LATEST one is pinned and never dropped, the first one is not
    msgs.append({"role": "user", "content": "Now please also tidy the logging."})
    out, did = A._compact_to_budget(msgs, None, 3000, archive=A._cmem_archive_cb())
    assert did
    key = convmem.default.memory_key(req)
    kinds = {}
    for q in ("quokka-importer", "pytest tests/test_quokka.py", "axolotl scheduler"):
        hits = convmem.default.search(key, q)
        assert hits, q
        kinds[q] = hits[0]["kind"]
    assert kinds == {"quokka-importer": "user", "pytest tests/test_quokka.py": "tool_call",
                     "axolotl scheduler": "assistant"}


# ---- end to end through the hop ----------------------------------------------- #

def _hop(monkeypatch, messages, tools=TOOLS, budget=None, no_craft=False, headers=None,
         signal=True):
    """One _upstream_chat hop against a fake provider -> the posted body."""
    posted = []
    monkeypatch.setattr(A.requests, "post",
                        lambda *a, **kw: posted.append(kw.get("json")) or _Resp())
    monkeypatch.setattr(A, "_post_with_header_deadline",
                        lambda deadline, post, **kw: posted.append(kw.get("json")) or _Resp())
    monkeypatch.setattr(A, "_summarize_dropped", lambda dropped: None)
    monkeypatch.setattr(A, "_apply_craft_brief", lambda m, agentic=False, tools=None: m)
    monkeypatch.setattr(A, "_with_model_guide", lambda m, pid, model, tools: m)
    monkeypatch.setattr(A, "_apply_goal_note", lambda m: m)
    if budget:
        monkeypatch.setattr(A, "_model_ctx_info", lambda pid, model: (budget, "catalog"))
    payload = {"model": "open", "messages": messages}
    if tools:
        payload["tools"] = tools
    if no_craft:
        payload["_no_craft"] = True
    hdrs = headers if headers is not None else {"X-Session-Id": "sess-e2e"}
    with A.app.test_request_context("/v1/chat/completions", headers=hdrs):
        A._ctx_begin({"model": "auto"}, messages, A._est_tokens(messages, tools), signal=signal)
        A._upstream_chat("uncloseai", payload, False)
    assert posted, "the hop never posted"
    return posted[-1]


@pytest.fixture
def route(monkeypatch, quiet):
    """The real /v1/chat/completions handler, chain, _dispatch_chat and
    _upstream_chat, with only the provider's HTTP call faked."""
    sent = []

    def post(url=None, json=None, **kw):
        sent.append(json)
        return _CwmResp(200, _answer("Done."), chunks=_sse_chunks())
    monkeypatch.setattr(A.config, "get_provider_config", lambda pid: {"api_keys": ["k"]})
    monkeypatch.setattr(A, "_resolve_base_url", lambda pid, pcfg: "https://example.invalid/v1")
    monkeypatch.setattr(A.requests, "post", post)
    monkeypatch.setattr(A, "_post_with_header_deadline",
                        lambda deadline, p, **kw: post(**kw))
    monkeypatch.setattr(A, "_model_ctx_info", lambda pid, model: (400000, "catalog"))
    monkeypatch.setattr(A, "_summarize_dropped", lambda dropped: None)
    monkeypatch.setattr(A, "_apply_craft_brief", lambda m, agentic=False, tools=None: m)
    monkeypatch.setattr(A, "_with_model_guide", lambda m, pid, model, tools: m)
    monkeypatch.setattr(A, "_apply_goal_note", lambda m: m)
    return sent


@pytest.mark.parametrize("stream", [False, True])
def test_through_the_real_route_the_feature_fires_and_the_key_opens_the_archive(route, stream):
    """Not a direct _upstream_chat call: a POST to /v1/chat/completions with a
    session header, the real handler, chain and hop. The conversation key must
    reach the hop (g is request-bound), the upstream body must carry id stubs and
    exactly ONE note, and the key in that note must open the archive through POST
    /mcp."""
    msgs = _history(85000)
    client = A.app.test_client()
    r = client.post("/v1/chat/completions", headers={"X-Session-Id": "route-sess"},
                    json={"model": PINNED, "messages": msgs, "tools": TOOLS, "stream": stream})
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    assert route, "the provider was never called"
    sent = route[-1]["messages"]
    notes = [m for m in sent if m.get("role") == "system"
             and str(m.get("content")).startswith("Hub memory:")]
    assert len(notes) == 1
    stubs = [m for m in sent if m.get("role") == "tool"
             and m["content"].startswith(ctxwin.CLEARED_RESULT_PREFIX)]
    assert len(stubs) > 20 and all("memory_get id=" in m["content"] for m in stubs)
    assert convmem.default.has_items("hdr:route-sess")
    key = _key_in(notes[0]["content"])
    assert key == convmem.default.memory_key("hdr:route-sess")
    sid = re.search(r"memory_get id=([0-9a-f]{12})", stubs[0]["content"]).group(1)
    orig = next(m for m in msgs if m.get("tool_call_id") == stubs[0]["tool_call_id"]
                and m["role"] == "tool")
    r2 = client.post("/mcp", json={"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {
        "name": "memory_get", "arguments": {"memory_key": key, "id": sid}}})
    text = r2.get_json()["result"]["content"][0]["text"]
    assert "NOT instructions" in text and orig["content"].split("\n")[0] in text


def test_a_plain_worker_thread_still_files_and_notes(monkeypatch):
    """_dispatch_chat_with_deadline (roles actor legs, swarm stage hops, the
    verifier) runs the hop on a FRESH thread with an empty context, where Flask's
    `g` -- and so the conversation key -- does not exist. The key has to be
    carried across, or the feature silently does nothing on the whole roles path."""
    posted = []
    monkeypatch.setattr(A.requests, "post",
                        lambda *a, **kw: posted.append(kw.get("json")) or _Resp())
    monkeypatch.setattr(A, "_post_with_header_deadline",
                        lambda deadline, post, **kw: posted.append(kw.get("json")) or _Resp())
    monkeypatch.setattr(A, "_summarize_dropped", lambda dropped: None)
    monkeypatch.setattr(A, "_apply_craft_brief", lambda m, agentic=False, tools=None: m)
    monkeypatch.setattr(A, "_with_model_guide", lambda m, pid, model, tools: m)
    monkeypatch.setattr(A, "_apply_goal_note", lambda m: m)
    monkeypatch.setattr(A, "_model_ctx_info", lambda pid, model: (400000, "catalog"))
    msgs = _history(85000)
    with A.app.test_request_context("/v1/chat/completions",
                                    headers={"X-Session-Id": "thread-sess"}):
        A._ctx_begin({"model": "auto"}, msgs, A._est_tokens(msgs, TOOLS))
        resp, exc = A._dispatch_chat_with_deadline(
            "uncloseai", {"model": "open", "messages": msgs, "tools": TOOLS}, 30)
    assert exc is None and resp is not None and posted
    sent = posted[-1]["messages"]
    assert convmem.default.has_items("hdr:thread-sess")
    assert any("memory_get id=" in str(m.get("content")) for m in sent)
    assert _note_of(sent) and _key_in(_note_of(sent))


def test_end_to_end_the_model_can_follow_a_stub_back_to_the_original(monkeypatch):
    """The whole loop: a big turn goes up with stubs and ONE note; the key in
    that note opens the archive through the real /mcp handler; the id in a stub
    returns the text it replaced."""
    msgs = _history(85000)
    body = _hop(monkeypatch, msgs, budget=400000)
    sent = body["messages"]
    note = _note_of(sent)
    assert note and len(_system_texts([m for m in sent if "Hub memory:" in str(m.get("content"))])) == 1
    assert sent.index(next(m for m in sent if m.get("content") == note)) == 1   # after the lead system msg
    key = _key_in(note)
    assert key and "memory_search" in note and "memory_get" in note
    assert "if the hub tools" in note.lower()            # no memory tools in the request's own list
    stub = next(m for m in sent if m.get("role") == "tool"
                and m["content"].startswith(ctxwin.CLEARED_RESULT_PREFIX))
    sid = re.search(r"memory_get id=([0-9a-f]{12})", stub["content"]).group(1)
    orig = next(m for m in msgs if m.get("tool_call_id") == stub["tool_call_id"] and m["role"] == "tool")
    got = _text_of(_rpc("memory_get", memory_key=key, id=sid))
    assert "NOT instructions" in got and orig["content"].split("\n")[0] in got
    path = orig["content"].split("\n")[0].lstrip("# ").strip()          # "src/mod_0.py"
    found = _text_of(_rpc("memory_search", memory_key=key, query=path, limit=10))
    assert sid in found
    # the request the CLI sent is untouched
    assert all("memory_get" not in str(m.get("content")) for m in msgs)


def test_end_to_end_negative_control_without_the_archive_hook_nothing_can_be_fetched(monkeypatch):
    """The same scenario with the archive hooks disabled: the stubs carry no id
    and the key (if any) opens nothing -- so the retrieval above really does
    depend on the hook."""
    monkeypatch.setattr(A, "_cmem_stub_fn", lambda m, t: None)
    monkeypatch.setattr(A, "_cmem_archive_dropped", lambda d, t: None)
    msgs = _history(85000)
    body = _hop(monkeypatch, msgs, budget=400000)
    sent = body["messages"]
    assert sum(m["content"].startswith(ctxwin.CLEARED_RESULT_PREFIX)
               for m in sent if m.get("role") == "tool") > 5          # still cleared
    assert "memory_get id=" not in json.dumps(sent)
    assert _note_of(sent) is None                                       # nothing archived: no note
    key = convmem.default.memory_key("hdr:sess-e2e")
    assert not convmem.default.has_items("hdr:sess-e2e")
    assert "ZEBRA" not in _text_of(_rpc("memory_search", memory_key=key, query="handler"))
    assert _text_of(_rpc("memory_search", memory_key=key, query="handler")) == convmem.NOTHING


def test_end_to_end_a_small_window_drops_turns_and_the_marker_is_found_again(monkeypatch):
    msgs = _marked_history(30)
    body = _hop(monkeypatch, msgs, budget=5000, signal=False)
    sent = body["messages"]
    assert MARKER not in json.dumps(sent)
    note = _note_of(sent)
    key = _key_in(note)
    assert note and key
    assert MARKER in _text_of(_rpc("memory_search", memory_key=key, query=MARKER))


def test_the_key_never_reaches_a_log_or_a_stub(monkeypatch, caplog):
    msgs = _history(85000)
    with caplog.at_level(logging.DEBUG):
        body = _hop(monkeypatch, msgs, budget=400000)
        key = _key_in(_note_of(body["messages"]))
        _rpc("memory_search", memory_key=key, query="handler")
        _rpc("memory_search", memory_key="mk_" + "Z" * 30, query="handler")
        convmem.default.search_text(key, "handler")
    assert key and key not in caplog.text
    assert all(key not in str(m["content"]) for m in body["messages"] if m.get("role") == "tool")


def test_pipeline_internal_calls_get_neither_stubs_with_ids_nor_a_note(monkeypatch):
    msgs = _history(85000)
    body = _hop(monkeypatch, msgs, budget=400000, no_craft=True)
    assert _note_of(body["messages"]) is None
    assert "memory_get id=" not in json.dumps(body["messages"])
    assert not convmem.default.has_items("hdr:sess-e2e")


def test_a_request_without_tools_gets_no_promise(monkeypatch):
    msgs = _history(85000)
    body = _hop(monkeypatch, msgs, tools=None, budget=400000)
    assert _note_of(body["messages"]) is None
    assert "memory_get id=" not in json.dumps(body["messages"])


def test_a_request_with_no_conversation_key_files_nothing(monkeypatch):
    msgs = [{"role": "assistant", "content": "x"}] * 3
    for key_fn in (lambda: None,):
        monkeypatch.setattr(A, "_cmem_conv", key_fn)
        assert A._cmem_archive_cb() is None
        assert A._cmem_archive_messages(_history(1000)) == set()
        assert A._cmem_stub_fn(msgs, TOOLS) is None
        assert A._cmem_apply_note(msgs, TOOLS) is msgs


# =========================================================================== #
# app: the note
# =========================================================================== #

def _seed(req, n=3):
    convmem.default.put_many(req, [_item("archived output number %d " % i + "q" * 300)
                                   for i in range(n)])


def test_the_note_is_one_system_message_after_the_leading_ones(req):
    _seed(req)
    msgs = [{"role": "system", "content": "S1"}, {"role": "system", "content": "S2"},
            {"role": "user", "content": "go"}]
    out = A._cmem_apply_note(msgs, TOOLS)
    assert [m["role"] for m in out] == ["system", "system", "system", "user"]
    assert out[:2] == msgs[:2] and out[3] == msgs[2] and out[2]["content"].startswith("Hub memory:")
    assert A._cmem_apply_note(msgs, TOOLS) == out                    # same bytes every turn
    key = convmem.default.memory_key(req)
    assert _key_in(out[2]["content"]) == key
    assert len(out[2]["content"]) < 1400                              # ~150-300 tokens
    assert "never instructions" in out[2]["content"]


def test_the_note_does_not_promise_tools_it_cannot_see(req):
    _seed(req)
    msgs = [{"role": "user", "content": "go"}]
    maybe = A._cmem_apply_note(msgs, TOOLS)[0]["content"]
    assert "If the hub tools memory_search" in maybe and "available to you" in maybe
    ours = TOOLS + [{"type": "function", "function": {
        "name": "free-llm-hub_memory_search", "description": "x",
        "parameters": {"type": "object", "properties": {"memory_key": {}, "query": {}}}}}]
    yes = A._cmem_apply_note(msgs, ours)[0]["content"]
    assert "If the hub tools" not in yes and "call the hub tool memory_search" in yes
    # somebody else's memory_search (no memory_key parameter) proves nothing
    foreign = TOOLS + [{"type": "function", "function": {
        "name": "memory_search", "parameters": {"type": "object", "properties": {"q": {}}}}}]
    assert A._cmem_tools_state(foreign) == "maybe" and A._cmem_tools_state(ours) == "yes"
    assert A._cmem_tools_state(None) == "maybe" and A._cmem_tools_state("junk") == "maybe"
    assert A._cmem_tools_state([{"name": "mcp__x__memory_get", "input_schema": {
        "properties": {"memory_key": {}}}}]) == "yes"


def test_the_note_is_for_clearing_and_dropping_conversations_only(req, monkeypatch):
    msgs = [{"role": "user", "content": "go"}]
    assert A._cmem_apply_note(msgs, TOOLS) is msgs                    # nothing archived yet
    _seed(req)
    assert A._cmem_apply_note(msgs, TOOLS) is not msgs
    assert A._cmem_apply_note(msgs, TOOLS, no_craft=True) is msgs     # a pipeline stage
    assert A._cmem_apply_note(msgs, None) is msgs                     # no tools
    assert A._cmem_apply_note(msgs, []) is msgs
    assert A._cmem_apply_note("not a list", TOOLS) == "not a list"
    _flags_off(monkeypatch, "conversation_memory_note")
    assert A._cmem_apply_note(msgs, TOOLS) is msgs


@pytest.mark.parametrize("prompt", [
    "CONTEXT CHECKPOINT COMPACTION. Write the handoff summary.",
    "Create a detailed summary of the conversation so far.",
    "Create a new anchored summary from the conversation history above."])
def test_never_on_a_clis_own_compaction_request(req, prompt):
    _seed(req)
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": prompt}]
    assert A._cmem_apply_note(msgs, TOOLS) is msgs


def test_a_clis_compaction_request_is_the_moment_the_history_is_filed(monkeypatch):
    """The CLI is about to replace its whole history with one summary. Results
    under the clearing threshold were never filed; this is their last chance --
    and the standing rules it holds must reach the exact-facts block the request
    carries."""
    short = "ok: 14 files changed, tests green, marker KIWI-4412 seen. " * 3       # < 1500 chars
    msgs = [{"role": "system", "content": "You are a coding agent."},
            {"role": "user", "content": "Build the importer. Always answer in French."}]
    for i in range(8):
        msgs += [_tool_call("c%d" % i, "bash", {"command": "pytest -k part%d" % i}),
                 {"role": "tool", "tool_call_id": "c%d" % i, "content": short + str(i)},
                 {"role": "assistant", "content": "step %d done" % i}]
    msgs += [{"role": "user", "content": "Never touch the legacy folder."},
             {"role": "assistant", "content": "Understood."},
             {"role": "user", "content": "Create a detailed summary of the conversation so far."}]
    body = _hop(monkeypatch, msgs, budget=400000)
    sent = body["messages"]
    assert _note_of(sent) is None                                      # never the note here
    assert not any("detailed summary" in h["excerpt"] for h in convmem.default.search(
        convmem.default.memory_key("hdr:sess-e2e"), "detailed summary", limit=10))   # the prompt itself is not filed
    assert not any("detailed summary" in i["text"] for i in A._cmem_items(msgs))
    last_user = [m for m in sent if m.get("role") == "user"][-1]["content"]
    assert ctxwin.EXACT_FACTS_MARKER in last_user                      # the request carries the facts
    assert "Always answer in French" in last_user and "Never touch the legacy folder" in last_user
    conv = "hdr:sess-e2e"
    key = convmem.default.memory_key(conv)
    assert convmem.default.search(key, "KIWI-4412")                    # the short results were filed
    assert convmem.default.search(key, "pytest -k part3")              # and the calls
    rules = convmem.default.rules(conv)
    assert any("Always answer in French" in r for r in rules)
    assert any("Never touch the legacy folder" in r for r in rules)


# =========================================================================== #
# Standing rules never erode
# =========================================================================== #

RULES = ["Always use tabs for indentation.", "Never commit the .env file.",
         "Prefer pytest over unittest.", "The API port must stay 8787.",
         "Always write docstrings in French.", "Never rename the public API.",
         "Remember that staging is called orchid.", "Do not add new dependencies.",
         "Always run the linter before finishing.", "Never use global state.",
         "Prefer small commits with clear messages.", "The deadline is Friday evening.",
         "Always keep the changelog current.", "Never log user email addresses."]


def _chain(updates, drop_budget=900, lossy=True):
    """Drive `updates` compactions of a conversation whose early messages the
    window keeps losing, with a recap rewritten (lossily) each time. Returns
    (rules fed so far, the window that reaches the hub at the end)."""
    history = [{"role": "system", "content": "sys"}]
    window = history
    for i in range(updates):
        history.append({"role": "user", "content": "Update %d. %s" % (i, RULES[i % len(RULES)])})
        history.append({"role": "assistant", "content": "Working on it. " + "details " * 150})
        window = [history[0]] + history[1:][-6:]               # the early messages fall away
        out, _ = A._compact_to_budget(
            window, None, drop_budget,
            summarizer=(lambda d: "Recap: the user is building something.") if lossy else None,
            archive=A._cmem_archive_cb())
        # the model-written recap is REWRITTEN on every update and keeps only
        # the facts of what this request still carries (what _summarize_worker does)
        A._recap_store.put(CONV, {"recap": "Recap v%d: building something." % i,
                                  "head": "h", "last": "l", "n": i,
                                  "exact": ctxwin.exact_facts(window)})
    return window


def test_twelve_recap_updates_never_lose_an_early_rule(req, monkeypatch, tmp_path):
    monkeypatch.setattr(A, "_RECAP_STORE_PATH", str(tmp_path / "recaps.json"))
    monkeypatch.setattr(A, "_recap_store", ctxwin.RecapStore(A._recap_store_path))
    window = _chain(len(RULES))                                          # 14 updates
    # What the hub hands the model at the next compaction: every rule, verbatim,
    # although the window itself now holds only the last few messages.
    block = A._exact_facts_block(window, 20000)
    for r in RULES:
        assert r in block, "rule lost: " + r
    recap = A._conversation_recap(CONV)
    for r in RULES:
        assert r in recap, "rule lost from the recap path: " + r
    # ...and in the system messages of an actual compaction output
    out, did = A._compact_to_budget(window, None, 900, archive=A._cmem_archive_cb())
    facts_msgs = [t for t in _system_texts(out) if ctxwin.EXACT_FACTS_MARKER in t]
    assert did and facts_msgs
    # A tiny window's facts block is capped at 5% of its target (600 chars); the
    # rules it cannot hold ride in the memory note, so none is lost to the model.
    noted = A._cmem_apply_note(out, TOOLS)
    seen_by_the_model = "\n".join(ctxwin.message_text(m) for m in noted)
    assert _note_of(noted)
    for r in RULES:
        assert r in seen_by_the_model, "rule never reaches the model: " + r


def test_the_same_chain_without_the_ledger_does_lose_rules(req, monkeypatch, tmp_path):
    """Control: with conversation memory off, the same 14 updates leave the
    early rules out of the block -- so the test above pins a real difference."""
    monkeypatch.setattr(A, "_RECAP_STORE_PATH", str(tmp_path / "recaps.json"))
    monkeypatch.setattr(A, "_recap_store", ctxwin.RecapStore(A._recap_store_path))
    monkeypatch.setattr(A, "_cmem_on", lambda: False)
    window = _chain(len(RULES))
    block = A._exact_facts_block(window, 20000)
    lost = [r for r in RULES if r not in block]
    assert len(lost) >= 4 and RULES[0] in lost


def test_a_rule_changed_by_name_is_replaced_not_duplicated(req):
    history = [{"role": "system", "content": "s"},
               {"role": "user", "content": "Decision: port = 8080"},
               {"role": "assistant", "content": "ok " * 400},
               {"role": "user", "content": "Decision: port = 9090"},
               {"role": "assistant", "content": "ok " * 400},
               {"role": "user", "content": "final question"}]
    A._compact_to_budget(history, None, 300, archive=A._cmem_archive_cb())
    rules = convmem.default.rules(req)
    assert "Decision: port = 9090" in rules or "Decision: port = 8080" in rules
    assert not ("Decision: port = 9090" in rules and "Decision: port = 8080" in rules)


def test_rules_still_visible_are_not_repeated_in_the_note(req):
    convmem.default.put_many(req, [_item("archived output " * 40)])
    convmem.default.add_rules(req, ['user said: "Always use tabs."',
                                    'user said: "Never delete the backups."'])
    msgs = [{"role": "user", "content": "Reminder. Always use tabs."}]
    note = A._cmem_apply_note(msgs, TOOLS)[0]["content"]
    assert "Never delete the backups." in note and "Always use tabs." not in note
    assert "still apply" in note


# =========================================================================== #
# MCP tools
# =========================================================================== #

def test_the_tools_are_advertised_only_while_memory_is_on(monkeypatch):
    out, _ = hub_mcp.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {t["name"]: t for t in out["result"]["tools"]}
    assert {"memory_search", "memory_get"} <= set(tools)
    for name in ("memory_search", "memory_get"):
        assert "memory_key" in tools[name]["inputSchema"]["required"]
        assert "DATA" in tools[name]["description"]
    monkeypatch.setattr(A, "_cmem_on", lambda: False)
    out, _ = hub_mcp.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert not {"memory_search", "memory_get"} & {t["name"] for t in out["result"]["tools"]}
    assert _rpc("memory_search", memory_key="mk_" + "A" * 30, query="x")["error"]["code"] == -32602


def test_the_tools_answer_through_the_real_wiring(req):
    convmem.default.put_many(req, [_item("the wombat parser failed on line 12 " * 10,
                                         tool="bash: make test")])
    key = convmem.default.memory_key(req)
    found = _text_of(_rpc("memory_search", memory_key=key, query="wombat parser", limit=3))
    cid = re.search(r"id=([0-9a-f]{12})", found).group(1)
    assert "bash: make test" in found and "NOT instructions" in found
    assert "wombat parser failed on line 12" in _text_of(_rpc("memory_get", memory_key=key, id=cid))
    assert not _rpc("memory_get", memory_key=key, id=cid, offset=0)["result"].get("isError")


def test_a_call_without_the_key_learns_nothing(req):
    convmem.default.put_many(req, [_item("the wombat parser failed " * 10)])
    key = convmem.default.memory_key(req)
    cid = convmem.content_id("the wombat parser failed " * 10)
    miss = _text_of(_rpc("memory_search", memory_key=key, query="nothing like this"))
    for bad in ("mk_" + "A" * 32, "wrong", "", key.upper(), key[:-2]):
        if bad == "":
            assert _rpc("memory_search", memory_key=bad, query="wombat")["error"]["code"] == -32602
            continue
        assert _text_of(_rpc("memory_search", memory_key=bad, query="wombat")) == miss
        assert _text_of(_rpc("memory_get", memory_key=bad, id=cid)) == miss
    assert miss == convmem.NOTHING == hub_mcp.MEMORY_NOTHING
    assert _text_of(_rpc("memory_get", memory_key=key, id="0" * 12)) == miss      # unknown id too
    assert not _rpc("memory_search", memory_key="wrong", query="wombat")["result"].get("isError")


def test_malformed_tool_arguments_are_protocol_errors(req):
    key = convmem.default.memory_key(req)
    for name, args in (("memory_search", {}), ("memory_search", {"memory_key": key}),
                       ("memory_search", {"memory_key": 5, "query": "x"}),
                       ("memory_search", {"memory_key": key, "query": ""}),
                       ("memory_search", {"memory_key": key, "query": "x", "limit": 0}),
                       ("memory_search", {"memory_key": key, "query": "x", "limit": 99}),
                       ("memory_search", {"memory_key": key, "query": "x", "limit": "many"}),
                       ("memory_get", {"memory_key": key}),
                       ("memory_get", {"memory_key": key, "id": 5}),
                       ("memory_get", {"memory_key": key, "id": "a" * 12, "offset": -1})):
        out = _rpc(name, **args)
        assert out["error"]["code"] == -32602, (name, args)


def test_a_failure_inside_the_tools_reads_like_an_empty_archive(req, monkeypatch):
    key = convmem.default.memory_key(req)

    def boom(*a, **k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(convmem.Archive, "search_text", boom)
    monkeypatch.setattr(convmem.Archive, "get_text", boom)
    assert _text_of(_rpc("memory_search", memory_key=key, query="x")) == convmem.NOTHING
    assert _text_of(_rpc("memory_get", memory_key=key, id="a" * 12)) == convmem.NOTHING

# ---- rules never erode: fixes 2026-10-30 ------------------------------------ #

def test_a_reasserted_kv_value_is_not_swallowed(arch):
    # newest-wins must hold even when an earlier value comes back in the same process
    assert arch.add_rules(CONV, ["Decision: port = 1"]) == 1
    assert arch.add_rules(CONV, ["Decision: port = 2"]) == 1
    assert arch.add_rules(CONV, ["Decision: port = 1"]) == 1     # applied, not cache-skipped
    rules = arch.rules(CONV)
    assert "Decision: port = 1" in rules and "Decision: port = 2" not in rules


def test_many_updates_to_one_rule_do_not_erode_a_standing_rule(arch):
    arch.add_rules(CONV, ["Keep this standing rule forever."])
    for i in range(convmem.MAX_RULES + 20):
        arch.add_rules(CONV, ["Decision: counter = %d" % i])
    rules = arch.rules(CONV)
    assert "Keep this standing rule forever." in rules          # only 2 distinct rules exist
    assert len([r for r in rules if r.startswith("Decision: counter")]) == 1
