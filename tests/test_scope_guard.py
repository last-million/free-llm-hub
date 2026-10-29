"""The PROGRAM SCOPE CARD and the SCOPE GUARD (scopecard.py + app.py's `_scope_*`).

Hermetic: synthetic programs on example.com / example.org / example.net hosts, a
temp project folder per test, a temp scopes.json (app._scope_store is replaced),
no network, no real CLI, never the real ~/.free-llm-hub. The upstream is a fake
object handed out by a patched _dispatch_chat / requests.post.

What is pinned:
  * scopecard reads scope sections, tables, inline owner-declared scope and
    explicit markers -- never a host that is merely mentioned in prose;
  * in_scope: True / False (explicitly OUT, wins a tie) / None (not listed);
  * the note is deterministic, <= 800 chars, on EVERY hop, and survives the
    CLI's own compaction (it is rebuilt from the stored card);
  * the guard turns a tool call aimed at an OUT-of-scope host into a plain
    message on the non-stream, streamed and roles paths of all three
    protocols, and never touches a call to a host that is not listed;
  * flags `scope_card` / `scope_guard` off = the request is untouched.
"""
import json
import logging
import os

import pytest

import app as A
import config
import scopecard as S

PROGRAM = """# Example Corp public program

## In scope
- *.example.com
- api.example.org
| Asset | Type |
|---|---|
| shop.example.net | Domain |
| 10.20.0.0/16 | CIDR |

## Out of scope
- payments.example.com
- legacy.example.com (third party)
- Self-XSS
- Missing security headers

## Rules
- Max 5 requests per second.
- No automated scanning.
- Do not test login.example.com with brute force.
"""

BASH = {"type": "function", "function": {
    "name": "bash", "description": "run a shell command",
    "parameters": {"type": "object", "properties": {"command": {"type": "string"}},
                   "required": ["command"]}}}


def _call(command, name="bash", cid="call_1"):
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": json.dumps({"command": command})}}


# --------------------------------------------------------------------------- #
# scopecard: reading a scope
# --------------------------------------------------------------------------- #

def test_program_sections_tables_and_rules():
    c = S.extract(PROGRAM, src="SCOPE.md")
    assert [e["p"] for e in c["in"]] == ["*.example.com", "api.example.org",
                                        "shop.example.net", "10.20.0.0/16"]
    assert [e["p"] for e in c["out"]] == ["payments.example.com", "legacy.example.com"]
    rules = [r["t"] for r in c["rules"]]
    assert "Max 5 requests per second" in rules
    assert "No automated scanning" in rules
    assert "Out of scope: Self-XSS" in rules            # issue types stay rules, never hosts
    assert all(e["kind"] == "program" and e["src"] == "SCOPE.md" for e in c["in"] + c["out"])


def test_a_do_not_test_with_a_qualifier_is_a_rule_not_an_exclusion():
    c = S.extract(PROGRAM)
    assert "login.example.com" not in [e["p"] for e in c["out"]]
    assert any("brute force" in r["t"] for r in c["rules"])
    bare = S.extract("## Rules\n- do not test admin.example.com\n")
    assert [e["p"] for e in bare["out"]] == ["admin.example.com"]


def test_owner_declared_inline_scope():
    c = S.extract("my app is app.example.com, api.example.com; out of scope: payments.example.com")
    assert [e["p"] for e in c["in"]] == ["app.example.com", "api.example.com"]
    assert [e["p"] for e in c["out"]] == ["payments.example.com"]
    assert {e["kind"] for e in c["in"] + c["out"]} == {"owner-declared"}


@pytest.mark.parametrize("text,ins,outs", [
    ("Test app.example.com and api.example.com, but payments.example.com is out of scope.",
     [], ["payments.example.com"]),
    ("everything under *.example.com is in scope except payments.example.com and "
     "billing.example.com", ["*.example.com"], ["payments.example.com", "billing.example.com"]),
    ("In scope: app.example.org, 192.0.2.10\nOut of scope: 192.0.2.0/24 range is excluded",
     ["app.example.org", "192.0.2.10"], ["192.0.2.0/24"]),
    ("Targets: shop.example.net", ["shop.example.net"], []),
    ("Do not test admin.example.org or ops.example.org.", [],
     ["admin.example.org", "ops.example.org"]),
])
def test_inline_markers(text, ins, outs):
    c = S.extract(text)
    assert [e["p"] for e in c["in"]] == ins
    assert [e["p"] for e in c["out"]] == outs


@pytest.mark.parametrize("text", [
    "that refactor is out of scope for this PR",
    "Please look at https://app.example.com/api/users/1 and www.example.org later.",
    "send the report to security@example.com",
    "see SCOPE.md and package.json for details",
    "",
    None,
])
def test_prose_is_never_scope(text):
    assert S.is_empty(S.extract(text))


def test_a_list_after_a_blank_line_stays_in_its_section_but_prose_ends_it():
    c = S.extract("Out of scope:\n- payments.example.com\n\n- legacy.example.com\n\n"
                  "We will also look at docs.example.org next week.")
    assert [e["p"] for e in c["out"]] == ["payments.example.com", "legacy.example.com"]
    assert not c["in"]


def test_detect_kind():
    assert S.detect_kind("my saas is app.example.com") == "owner-declared"
    assert S.detect_kind("the HackerOne program scope") == "program"
    assert S.detect_kind("our own bug bounty program: in scope *.example.com") == "program"


def test_merge_and_drop_source_are_bounded_and_ordered():
    a = S.extract(PROGRAM, src="SCOPE.md")
    b = S.extract("out of scope: billing.example.com")
    m = S.merge(a, b)
    assert [e["p"] for e in m["out"]][-1] == "billing.example.com"
    assert S.merge(m, b) == m                            # idempotent
    assert [e["p"] for e in S.drop_source(m, "SCOPE.md")["out"]] == ["billing.example.com"]
    many = {"in": [], "out": [{"p": "h%d.example.com" % i} for i in range(500)], "rules": []}
    assert len(S.normalize(many)["out"]) == S.MAX_HOSTS
    assert S.normalize("garbage") == S.empty()


# --------------------------------------------------------------------------- #
# scopecard: deciding
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("host,expected", [
    ("app.example.com", True),
    ("payments.example.com", False),
    ("PAYMENTS.example.com.", False),
    ("legacy.example.com:443", False),
    ("example.com", None),                  # a wildcard never covers the apex
    ("shop.example.net", True),
    ("www.example.org", None),
    ("10.20.3.4", True),
    ("10.21.0.1", None),
    ("registry.npmjs.org", None),
    ("not a host", None),
])
def test_in_scope(host, expected):
    assert S.in_scope(host, S.extract(PROGRAM)) is expected


def test_out_wins_a_tie_and_exact_beats_wildcard():
    c = {"in": [{"p": "a.example.com"}], "out": [{"p": "a.example.com"}, {"p": "*.example.org"}],
         "rules": []}
    assert S.in_scope("a.example.com", c) is False
    c2 = {"in": [{"p": "keep.example.org"}], "out": [{"p": "*.example.org"}], "rules": []}
    assert S.in_scope("keep.example.org", c2) is True
    assert S.in_scope("other.example.org", c2) is False


def test_a_scan_range_that_touches_an_out_host_is_out():
    c = {"in": [{"p": "10.0.0.0/8"}], "out": [{"p": "10.0.0.5"}], "rules": []}
    assert S.in_scope("10.0.0.0/24", c) is False
    assert S.in_scope("10.1.0.0/16", c) is True


# --------------------------------------------------------------------------- #
# scopecard: the note
# --------------------------------------------------------------------------- #

def test_render_is_deterministic_bounded_and_out_first():
    c = S.extract(PROGRAM, src="SCOPE.md")
    a, b = S.render(c), S.render(S.normalize(json.loads(json.dumps(c))))
    assert a == b and len(a) <= 800
    assert a.startswith("PROGRAM SCOPE")
    assert a.index("OUT of scope") < a.index("In scope")
    assert "not in the listed scope" in a.lower() or "NOT in the listed scope" in a
    assert S.render(S.empty()) == ""


def test_render_keeps_the_out_list_when_the_card_is_huge():
    c = {"in": [{"p": "in%03d.example.com" % i} for i in range(150)],
         "out": [{"p": "out%03d.example.org" % i} for i in range(150)],
         "rules": [{"t": "rule number %d about rate limits" % i} for i in range(40)]}
    text = S.render(c, 800)
    assert len(text) <= 800
    assert "out000.example.org" in text and "more)" in text


def test_owner_declared_is_named_in_the_note():
    text = S.render(S.extract("my app is app.example.com; out of scope: payments.example.com"))
    assert "owner-declared" in text and "payments.example.com" in text


# --------------------------------------------------------------------------- #
# scopecard: what a tool call would hit
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name,args,hosts", [
    ("bash", {"command": "curl -s https://payments.example.com/api | jq ."},
     ["payments.example.com"]),
    ("bash", {"command": "nmap -sV 10.20.0.0/24 payments.example.com"},
     ["10.20.0.0/24", "payments.example.com"]),
    ("bash", {"command": "python -c \"import urllib.request as u; u.urlopen('https://payments.example.com/x')\""},
     ["payments.example.com"]),
    ("bash", {"command": "wsl -d Ubuntu -e bash -lic 'cd ~/p && ffuf -u https://payments.example.com/FUZZ -w w.txt'"},
     ["payments.example.com"]),
    ("shell", {"command": ["bash", "-lc", "curl -I http://payments.example.com"]},
     ["payments.example.com"]),
    ("webfetch", {"url": "https://payments.example.com/robots.txt"}, ["payments.example.com"]),
    ("bash", {"command": "curl -H 'Host: payments.example.com' https://app.example.com/"},
     ["payments.example.com", "app.example.com"]),
    ("bash", {"command": "curl 'https://app.example.com/?next=payments.example.com'"},
     ["app.example.com"]),
    # not a request: written, echoed, cloned or installed
    ("bash", {"command": "echo 'out of scope: payments.example.com' >> notes.md"}, []),
    ("bash", {"command": "echo \"curl https://payments.example.com\" >> notes.md"}, []),
    ("write", {"filePath": "notes.md", "content": "curl https://payments.example.com"}, []),
    ("bash", {"command": "git clone https://payments.example.com/repo.git"}, []),
    ("bash", {"command": "npm install && npm test"}, []),
])
def test_targets_in_call(name, args, hosts):
    assert S.targets_in_call(name, json.dumps(args)) == hosts


def test_unparseable_arguments_are_read_as_a_command():
    assert S.targets_in_call("bash", "curl https://payments.example.com {") == [
        "payments.example.com"]
    assert S.targets_in_call("bash", None) == []


def test_check_calls_blocks_only_an_explicit_out_host():
    c = S.extract(PROGRAM, src="SCOPE.md")
    hit = S.check_calls([_call("curl https://payments.example.com/")], c)
    assert hit and hit[0] == "payments.example.com"
    msg = S.block_message(*hit)
    assert msg.startswith("The hub blocked a call to payments.example.com: it is out of "
                          "scope for this program (program: SCOPE.md)")
    for cmd in ("curl https://app.example.com/", "curl https://registry.npmjs.org/x",
                "curl https://docs.example.org/"):
        assert S.check_calls([_call(cmd)], c) is None, cmd
    assert S.check_calls([_call("curl https://payments.example.com/")],
                         {"in": [{"p": "*.example.com"}], "out": [], "rules": []}) is None


# --------------------------------------------------------------------------- #
# scopecard: the streamed guard
# --------------------------------------------------------------------------- #

def _sse(obj, end=b"\n\n"):
    return b"data: " + json.dumps(obj).encode() + end


def _frames(command, end=b"\n\n", text=True, usage=False):
    out = []
    if text:
        out.append(_sse({"id": "x", "choices": [{"index": 0, "delta": {
            "role": "assistant", "content": "Checking."}, "finish_reason": None}]}, end))
    args = json.dumps({"command": command})
    half = len(args) // 2
    out.append(_sse({"id": "x", "choices": [{"index": 0, "delta": {"tool_calls": [{
        "index": 0, "id": "c1", "type": "function",
        "function": {"name": "bash", "arguments": ""}}]}, "finish_reason": None}]}, end))
    for part in (args[:half], args[half:]):
        out.append(_sse({"id": "x", "choices": [{"index": 0, "delta": {"tool_calls": [{
            "index": 0, "function": {"arguments": part}}]}, "finish_reason": None}]}, end))
    out.append(_sse({"id": "x", "choices": [{"index": 0, "delta": {},
                                             "finish_reason": "tool_calls"}]}, end))
    if usage:
        out.append(_sse({"id": "x", "choices": [], "usage": {"prompt_tokens": 3}}, end))
    out.append(b"data: [DONE]" + end)
    return out


def _check(card):
    def check(calls):
        hit = S.check_calls(calls, card)
        return S.block_message(*hit) if hit else None
    return check


def test_guard_stream_passes_an_allowed_call_byte_for_byte():
    card = S.extract(PROGRAM)
    frames = _frames("curl https://app.example.com/")
    assert b"".join(S.guard_stream(iter(frames), "frames", _check(card))) == b"".join(frames)
    lines = _frames("curl https://app.example.com/", end=b"")
    assert list(S.guard_stream(iter(lines), "lines", _check(card))) == lines


def test_guard_stream_blocks_an_out_of_scope_call():
    card = S.extract(PROGRAM, src="SCOPE.md")
    events = []
    out = list(S.guard_stream(iter(_frames("curl https://payments.example.com/", usage=True)),
                              "frames", _check(card),
                              on_event=lambda k, d: events.append(k)))
    body = b"".join(out)
    assert b"tool_calls" not in body
    assert b"The hub blocked a call to payments.example.com" in body
    assert b'"finish_reason": "stop"' in body
    assert b'"usage"' in body and body.endswith(b"data: [DONE]\n\n")
    assert out[0] == _frames("x")[0]                     # text before the call went out as-is
    assert events == ["blocked"]


def test_guard_stream_lines_mode_blocks_too():
    card = S.extract(PROGRAM)
    out = list(S.guard_stream(iter(_frames("nmap payments.example.com", end=b"", text=False)),
                              "lines", _check(card)))
    assert all(not u.endswith(b"\n\n") for u in out)
    assert any(b"The hub blocked" in u for u in out) and not any(b"tool_calls" in u for u in out)


def test_guard_stream_gives_up_on_a_huge_hold_and_reports_what_it_missed():
    card = S.extract(PROGRAM)
    events = []
    frames = _frames("curl https://payments.example.com/")
    out = list(S.guard_stream(iter(frames), "frames", _check(card),
                              on_event=lambda k, d: events.append(k), hold_max_bytes=50))
    assert b"".join(out) == b"".join(frames)             # released unchecked, fail-open
    assert events == ["gave_up", "missed"]


def test_guard_stream_without_tool_calls_is_untouched():
    frames = [_sse({"id": "x", "choices": [{"index": 0, "delta": {"content": "hi"},
                                            "finish_reason": None}]}), b"data: [DONE]\n\n"]
    assert list(S.guard_stream(iter(frames), "frames", lambda c: "never")) == frames


# --------------------------------------------------------------------------- #
# scopecard: storage
# --------------------------------------------------------------------------- #

def test_store_round_trip_bounded_and_fail_open(tmp_path, monkeypatch):
    p = tmp_path / "state" / "scopes.json"
    st = S.Store(str(p))
    assert st.get("k") is None
    card = S.extract(PROGRAM)
    assert st.put("k", card, {"SCOPE.md": [1, 2]}, now=1.0)
    again = S.Store(str(p)).get("k")
    assert again["card"] == S.normalize(card) and again["files"] == {"SCOPE.md": [1, 2]}
    monkeypatch.setattr(S, "MAX_FOLDERS", 3)
    for i in range(6):
        st.put("f%d" % i, card, now=10.0 + i)
    assert set(json.loads(p.read_text("utf-8"))["folders"]) == {"f3", "f4", "f5"}
    p.write_text("{not json", "utf-8")
    assert S.Store(str(p)).get("f5") is None
    assert not any(n.endswith(".tmp") for n in os.listdir(p.parent))


# --------------------------------------------------------------------------- #
# app: the card, the note, the files
# --------------------------------------------------------------------------- #

@pytest.fixture
def scope(tmp_path, monkeypatch):
    """A fresh card store in tmp, fresh in-memory tables, a project folder."""
    store = S.Store(str(tmp_path / "state" / "scopes.json"))
    monkeypatch.setattr(A, "_scope_store", lambda: store)
    monkeypatch.setattr(A, "_SCOPE_MEM", {})
    monkeypatch.setattr(A, "_SCOPE_SEEN", {})
    monkeypatch.setattr(A, "_SCOPE_MISSED", {})
    proj = tmp_path / "proj"
    proj.mkdir()
    return {"store": store, "proj": proj, "path": tmp_path / "state" / "scopes.json"}


def _env(folder):
    return {"role": "system", "content": "You are an agent CLI.\n<env>\n  Working directory: %s\n"
                                         "  Platform: win32\n</env>" % folder}


def _flags(monkeypatch, **vals):
    real = config.get_flag
    monkeypatch.setattr(config, "get_flag",
                        lambda name, default=False: vals[name] if name in vals else real(name, default))


def test_a_scope_file_puts_the_note_on_every_hop(scope):
    (scope["proj"] / "SCOPE.md").write_text(PROGRAM, "utf-8")
    msgs = [_env(scope["proj"]), {"role": "user", "content": "start recon"}]
    out = A._scope_apply_note(msgs)
    assert len(out) == 3 and out[0] is msgs[0] and out[2] is msgs[1]
    note = out[1]
    assert note["role"] == "system" and note["content"].startswith("PROGRAM SCOPE")
    assert "payments.example.com" in note["content"] and len(note["content"]) <= 800
    # deterministic, every hop, and a later turn deep in a tool loop gets it too
    later = msgs + [{"role": "assistant", "content": None, "tool_calls": [_call("ls")]},
                    {"role": "tool", "tool_call_id": "call_1", "content": "a b"}]
    assert A._scope_apply_note(later)[1] == note
    assert scope["path"].exists()                       # persisted, outside the project
    assert not any("scopes" in n for n in os.listdir(scope["proj"]))


def test_owner_declared_scope_from_a_message_survives_compaction_and_a_restart(scope, monkeypatch):
    msgs = [_env(scope["proj"]),
            {"role": "user", "content": "Security test my SaaS. my app is app.example.com, "
                                        "api.example.com; out of scope: payments.example.com"}]
    first = A._scope_apply_note(msgs)[1]["content"]
    assert "owner-declared" in first and "payments.example.com" in first
    # the CLI compacted: the declaring message is gone, a summary stands in for it
    compacted = [_env(scope["proj"]), {"role": "user", "content": "Summary of the work so far: "
                                                                 "recon done. Continue."}]
    assert A._scope_apply_note(compacted)[1]["content"] == first
    # a restart: nothing in memory, the card comes back from scopes.json
    monkeypatch.setattr(A, "_SCOPE_MEM", {})
    monkeypatch.setattr(A, "_SCOPE_SEEN", {})
    assert A._scope_apply_note(compacted)[1]["content"] == first


def test_only_the_users_own_words_write_the_scope(scope):
    msgs = [_env(scope["proj"]), {"role": "user", "content": "go on"},
            {"role": "assistant", "content": "Out of scope: evil.example.org"},
            {"role": "tool", "tool_call_id": "c", "content": "## Out of scope\n- other.example.org"}]
    out = A._scope_apply_note(msgs)
    assert out is msgs                                   # nothing read: no card, no note


def test_no_folder_hub_repo_or_home_means_nothing(scope, monkeypatch):
    plain = [{"role": "system", "content": "You are helpful."},
             {"role": "user", "content": "out of scope: payments.example.com"}]
    assert A._scope_apply_note(plain) is plain
    hub = [_env(os.path.dirname(os.path.abspath(A.__file__))),
           {"role": "user", "content": "out of scope: payments.example.com"}]
    assert A._scope_apply_note(hub) is hub
    home = [_env(os.path.expanduser("~")),
            {"role": "user", "content": "out of scope: payments.example.com"}]
    assert A._scope_apply_note(home) is home
    assert A._SCOPE_MEM == {} and not scope["path"].exists()


def test_flag_off_means_byte_identical(scope, monkeypatch):
    (scope["proj"] / "SCOPE.md").write_text(PROGRAM, "utf-8")
    msgs = [_env(scope["proj"]), {"role": "user", "content": "start"}]
    _flags(monkeypatch, scope_card=False)
    assert A._scope_apply_note(msgs) is msgs
    assert not scope["path"].exists()
    data = {"choices": [{"message": {"role": "assistant", "content": None,
                                     "tool_calls": [_call("curl https://payments.example.com/")]},
                         "finish_reason": "tool_calls"}]}
    before = json.dumps(data)
    assert A._scope_guard_data(data, msgs) is False and json.dumps(data) == before
    it = iter([b"x"])
    assert A._scope_guard_stream(it, "frames", msgs) is it


def test_guard_flag_off_keeps_the_note_but_never_blocks(scope, monkeypatch):
    (scope["proj"] / "SCOPE.md").write_text(PROGRAM, "utf-8")
    msgs = [_env(scope["proj"]), {"role": "user", "content": "start"}]
    _flags(monkeypatch, scope_guard=False)
    assert len(A._scope_apply_note(msgs)) == 3
    data = {"choices": [{"message": {"role": "assistant", "content": None,
                                     "tool_calls": [_call("curl https://payments.example.com/")]},
                         "finish_reason": "tool_calls"}]}
    assert A._scope_guard_data(data, msgs) is False
    assert data["choices"][0]["message"]["tool_calls"]


def test_files_are_found_one_level_deep_bounded_and_never_through_a_link(scope, monkeypatch):
    proj = scope["proj"]
    (proj / "docs").mkdir()
    (proj / "docs" / "scope.md").write_text("Out of scope: one.example.org\n", "utf-8")
    (proj / "docs" / "deeper").mkdir()
    (proj / "docs" / "deeper" / "scope.md").write_text("Out of scope: two.example.org\n", "utf-8")
    (proj / "node_modules").mkdir()
    (proj / "node_modules" / "scope.md").write_text("Out of scope: three.example.org\n", "utf-8")
    (proj / "big-scope.txt").write_text("Out of scope: four.example.org\n" + "x" * 70000, "utf-8")
    (proj / "notes.md").write_text("Out of scope: five.example.org\n", "utf-8")
    outside = proj.parent / "outside"
    outside.mkdir()
    (outside / "scope.md").write_text("Out of scope: six.example.org\n", "utf-8")
    try:
        os.symlink(str(outside / "scope.md"), str(proj / "program-link.md"))
    except (OSError, NotImplementedError):
        pass                                             # no symlink right here: still checked below
    card = A._scope_refresh(str(proj), [_env(proj)])
    outs = [e["p"] for e in card["out"]]
    assert outs == ["one.example.org"]
    assert card["out"][0]["src"] == "docs/scope.md"


def test_a_changed_or_removed_file_replaces_its_part_of_the_card(scope, monkeypatch):
    f = scope["proj"] / "SCOPE.md"
    f.write_text("Out of scope: old.example.org\n", "utf-8")
    msgs = [_env(scope["proj"])]
    assert [e["p"] for e in A._scope_refresh(str(scope["proj"]), msgs)["out"]] == ["old.example.org"]
    f.write_text("Out of scope: new.example.org, newer.example.org\n", "utf-8")
    os.utime(f, (1_900_000_000, 1_900_000_000))
    monkeypatch.setattr(A, "_SCOPE_FILE_RESCAN", 0.0)
    assert [e["p"] for e in A._scope_refresh(str(scope["proj"]), msgs)["out"]] == [
        "new.example.org", "newer.example.org"]
    f.unlink()
    assert A._scope_refresh(str(scope["proj"]), msgs)["out"] == []


def test_the_note_rides_every_hop_of_upstream_chat_and_never_a_pipeline_stage(scope, monkeypatch):
    (scope["proj"] / "SCOPE.md").write_text(PROGRAM, "utf-8")
    posted = []

    class _R:
        status_code, text, headers = 200, "", {}

        def json(self):
            return {"choices": [{"finish_reason": "stop",
                                 "message": {"role": "assistant", "content": "OK"}}]}

        def close(self):
            pass

    monkeypatch.setattr(A.requests, "post", lambda *a, **kw: posted.append(kw.get("json")) or _R())
    monkeypatch.setattr(A, "_post_with_header_deadline",
                        lambda deadline, post, **kw: posted.append(kw.get("json")) or _R())
    monkeypatch.setattr(A, "_summarize_dropped", lambda dropped: None)
    monkeypatch.setattr(A, "_apply_craft_brief", lambda m, agentic=False, tools=None: m)
    monkeypatch.setattr(A, "_with_model_guide", lambda m, pid, model, tools: m)
    msgs = [_env(scope["proj"]), {"role": "user", "content": "start recon"}]
    for no_craft in (False, True):
        payload = {"model": "open", "messages": msgs, "tools": [BASH]}
        if no_craft:
            payload["_no_craft"] = True
        with A.app.test_request_context("/v1/chat/completions"):
            A._ctx_begin({"model": "auto"}, msgs, A._est_tokens(msgs, [BASH]))
            A._upstream_chat("uncloseai", payload, False)
    sent_normal, sent_stage = posted[0]["messages"], posted[-1]["messages"]
    assert any(str(m.get("content", "")).startswith("PROGRAM SCOPE") for m in sent_normal)
    assert not any(str(m.get("content", "")).startswith("PROGRAM SCOPE") for m in sent_stage)


def test_api_scope_returns_the_card(scope):
    (scope["proj"] / "SCOPE.md").write_text(PROGRAM, "utf-8")
    A._scope_refresh(str(scope["proj"]), [_env(scope["proj"])])
    r = A.app.test_client().get("/api/scope", query_string={"project_dir": str(scope["proj"])})
    assert r.status_code == 200
    body = r.get_json()
    assert [e["p"] for e in body["card"]["out"]] == ["payments.example.com", "legacy.example.com"]
    assert body["note"].startswith("PROGRAM SCOPE") and r.headers["Cache-Control"] == "no-store"
    assert A.app.test_client().get("/api/scope").status_code == 400


# --------------------------------------------------------------------------- #
# app: the guard on the real handlers (non-stream, streamed) and the roles path
# --------------------------------------------------------------------------- #

class _Resp:
    status_code = 200
    text = ""

    def __init__(self, command):
        self.command = command
        self.headers = {}

    def json(self):
        return {"id": "chatcmpl-x", "object": "chat.completion", "created": 1,
                "choices": [{"index": 0, "message": {
                    "role": "assistant", "content": None,
                    "tool_calls": [_call(self.command, cid="call_77")]},
                    "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 12, "total_tokens": 52}}

    def close(self):
        pass

    def _frames(self, sep):
        args = json.dumps({"command": self.command})
        yield b"data: " + json.dumps({"choices": [{"index": 0, "delta": {
            "role": "assistant", "tool_calls": [{"index": 0, "id": "call_77", "type": "function",
                                                 "function": {"name": "bash", "arguments": args}}]},
            "finish_reason": None}]}).encode() + sep
        yield b"data: " + json.dumps({"choices": [{"index": 0, "delta": {},
                                                   "finish_reason": "tool_calls"}]}).encode() + sep
        yield b"data: [DONE]" + sep

    def iter_lines(self, decode_unicode=False):
        return self._frames(b"")

    def iter_content(self, chunk_size=None):
        return self._frames(b"\n\n")


@pytest.fixture
def world(scope, monkeypatch):
    for name in ("_record_chat_usage", "_save_perf_stats", "_act_pick", "_note_ttft",
                 "_note_provider_timeout", "_throttle_failed_hop", "_note_request_data",
                 "_note_request_reply"):
        monkeypatch.setattr(A, name, lambda *a, **k: None)
    monkeypatch.setattr(A, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(A, "_model_block_reason", lambda pid, m: None)
    monkeypatch.setattr(A, "_is_provider_dead", lambda pid: False)
    monkeypatch.setattr(A, "_is_trivial_turn", lambda *a, **k: False)
    monkeypatch.setattr(A, "_request_deadline_seconds", lambda: 20)
    monkeypatch.setattr(A, "_stream_peek_timeout", lambda *a, **k: 1.0)
    monkeypatch.setattr(A, "_record_outcome", lambda *a, **k: None)
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("p1", "m1", "hard"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [("p1", "m1")])
    reply = {"command": "curl -s https://payments.example.com/api"}
    monkeypatch.setattr(A, "_dispatch_chat", lambda pid, payload, stream: _Resp(reply["command"]))
    (scope["proj"] / "SCOPE.md").write_text(PROGRAM, "utf-8")
    A._scope_refresh(str(scope["proj"]), [_env(scope["proj"])])
    reply["proj"] = scope["proj"]
    return reply


def _request(proto, stream, folder):
    env = _env(folder)["content"]
    if proto == "chat":
        return "/v1/chat/completions", {
            "model": "auto", "stream": stream, "tools": [BASH],
            "messages": [{"role": "system", "content": env},
                         {"role": "user", "content": "check the api"}]}
    if proto == "responses":
        # codex: the folder rides in a user message that is only its environment block
        return "/v1/responses", {
            "model": "auto", "stream": stream,
            "input": [{"role": "user", "content": [{
                "type": "input_text",
                "text": "<environment_context>\n  <cwd>%s</cwd>\n</environment_context>" % folder}]},
                {"role": "user", "content": [{"type": "input_text", "text": "check the api"}]}],
            "tools": [{"type": "function", "name": "bash", "description": "run",
                       "parameters": BASH["function"]["parameters"]}]}
    return "/v1/messages", {
        "model": "auto", "max_tokens": 400, "stream": stream, "system": env,
        "messages": [{"role": "user", "content": "check the api"}],
        "tools": [{"name": "bash", "description": "run",
                   "input_schema": BASH["function"]["parameters"]}]}


def _served(proto, stream, resp):
    """(text the client got, whether it got an executable tool call)."""
    raw = resp.get_data(as_text=True)
    if not stream:
        j = json.loads(raw)
        if proto == "chat":
            m = j["choices"][0]["message"]
            return m.get("content") or "", bool(m.get("tool_calls"))
        if proto == "responses":
            items = j.get("output") or []
            text = " ".join(c.get("text", "") for it in items for c in (it.get("content") or [])
                            if isinstance(c, dict))
            return text, any(it.get("type") == "function_call" for it in items)
        text = " ".join(b.get("text", "") for b in j["content"] if b.get("type") == "text")
        return text, any(b.get("type") == "tool_use" for b in j["content"])
    if proto == "chat":
        return raw, '"tool_calls"' in raw
    if proto == "responses":
        return raw, '"function_call"' in raw
    return raw, '"tool_use"' in raw


@pytest.mark.parametrize("proto", ["chat", "responses", "messages"])
@pytest.mark.parametrize("stream", [False, True])
def test_the_guard_blocks_an_out_of_scope_call_on_every_protocol(world, proto, stream, caplog):
    path, body = _request(proto, stream, world["proj"])
    with caplog.at_level(logging.WARNING):
        resp = A.app.test_client().post(path, json=body)
    assert resp.status_code == 200
    text, has_call = _served(proto, stream, resp)
    assert not has_call
    assert "The hub blocked a call to payments.example.com" in text
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[scope] blocked")]
    assert len(lines) == 1 and "payments.example.com" in lines[0]
    assert "proj" in lines[0] and str(world["proj"]) not in lines[0]   # folder NAME only


@pytest.mark.parametrize("proto", ["chat", "responses", "messages"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("command", ["curl -s https://app.example.com/api",
                                     "npm view express version",
                                     "curl -s https://registry.npmjs.org/express"])
def test_an_in_scope_or_unlisted_host_is_never_blocked(world, proto, stream, command):
    world["command"] = command
    path, body = _request(proto, stream, world["proj"])
    resp = A.app.test_client().post(path, json=body)
    text, has_call = _served(proto, stream, resp)
    assert has_call and "The hub blocked" not in text


def test_the_roles_path_is_guarded_too(world, monkeypatch):
    msgs = [_env(world["proj"]), {"role": "user", "content": "check the api"}]
    data = _Resp("curl https://payments.example.com/x").json()
    msg = data["choices"][0]["message"]
    monkeypatch.setattr(A, "_team_notes_for_turn", lambda body, *a, **k: body)
    monkeypatch.setattr(A, "_rr_prefilter_skip", lambda *a, **k: None)
    monkeypatch.setattr(A, "_window_fits", lambda *a, **k: True)
    monkeypatch.setattr(A, "_bandit_delta", lambda *a, **k: 0.0)
    monkeypatch.setattr(A, "_role_actor_hop", lambda *a, **k: ("p1", "m1", data, msg))
    monkeypatch.setattr(A, "_role_verify_and_correct", lambda *a, **k: None)
    monkeypatch.setattr(A, "_bandit_remember", lambda *a, **k: None)
    monkeypatch.setattr(A, "_note_continuation_outcome", lambda *a, **k: None)
    monkeypatch.setattr(A, "_role_activity", lambda *a, **k: None)
    monkeypatch.setattr(A, "_role_log", lambda *a, **k: None)
    with A.app.test_request_context("/v1/chat/completions"):
        out = A._tool_turn_roles({"model": "swarm", "messages": msgs, "tools": [BASH]})
    assert out is not None
    served = out[0]["choices"][0]
    assert not served["message"].get("tool_calls")
    assert "The hub blocked a call to payments.example.com" in served["message"]["content"]
    assert served["finish_reason"] == "stop"


def test_a_streamed_call_that_slipped_through_is_flagged_in_the_next_note(world, monkeypatch):
    monkeypatch.setattr(S, "HOLD_MAX_BYTES", 10)
    real = S.guard_stream
    monkeypatch.setattr(S, "guard_stream",
                        lambda items, framing, check, on_event=None, **kw:
                        real(items, framing, check, on_event=on_event, hold_max_bytes=10))
    path, body = _request("chat", True, world["proj"])
    resp = A.app.test_client().post(path, json=body)
    text, has_call = _served("chat", True, resp)
    assert has_call                                      # fail-open: it went out
    note = A._scope_apply_note(body["messages"])[1]["content"]
    assert len(note) <= 800
    assert "WARNING: a streamed call to payments.example.com (OUT of scope)" in note
