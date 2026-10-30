"""The findings check: a security finding an agent claims is checked against what the hub
OBSERVED, filed per project folder, and fed back as a note on the next turns.

Hermetic: no network, no real CLI, never the real state dir or config (the ledger root is
a temp folder), synthetic conversations about example.com-style hosts and a localhost app,
the second-model verdict faked. Covers detection both ways, observed proof vs none, the
false-positive rules, dedupe/update, the note on the next turn only, the rate limits, the
flag, the route, and the real /v1 handlers with a fake upstream.
"""
import json
import logging
import os
import threading
import time

import pytest

import app as A
import findings as F
from test_context_window_management import (    # noqa: F401  (imported fixture + helpers)
    PINNED, _Resp as _CwmResp, _answer, _sse_chunks, quiet)

SID = "find-sess"
_REAL_VERDICT_CALL = A._fc_verdict_call       # before any fixture replaces them
_REAL_SPAWN = A._fc_spawn


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #

@pytest.fixture
def fc(monkeypatch, tmp_path):
    """Ledger root in a temp folder, clean limiter state, verifier faked and run inline."""
    root = tmp_path / "state" / "findings"
    monkeypatch.setattr(A, "_fc_root", lambda: str(root))
    A._FC_LEDGERS.clear()
    A._FC_SEEN.clear()
    A._FC_LAST.clear()
    A._FC_SPENT.clear()
    monkeypatch.setattr(A, "_FC_BG_SEM", threading.BoundedSemaphore(2))
    calls = []
    state = {"verdict": {"ok": False, "problems": ["no observed response shows it"],
                         "severity": "low"}, "family": "kimi", "raise": False}

    def verdict_call(messages, producer):
        calls.append({"messages": messages, "producer": producer})
        if state["raise"]:
            raise RuntimeError("verifier down")
        return state["verdict"], state["family"]
    monkeypatch.setattr(A, "_fc_verdict_call", verdict_call)
    monkeypatch.setattr(A, "_fc_spawn", lambda target, *args: target(*args))
    project = tmp_path / "proj"
    project.mkdir()
    yield {"root": root, "calls": calls, "state": state, "project": str(project), "tmp": tmp_path}
    A._FC_LEDGERS.clear()
    A._FC_SEEN.clear()
    A._FC_LAST.clear()
    A._FC_SPENT.clear()


def _sys(folder):
    return {"role": "system",
            "content": "You are a coding agent.\n<env>\nWorking directory: %s\n</env>" % folder}


def _call(cid, cmd, name="bash"):
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": cid, "type": "function",
        "function": {"name": name, "arguments": json.dumps({"command": cmd})}}]}


def _result(cid, out):
    return {"role": "tool", "tool_call_id": cid, "content": out}


def _conv(folder, reply, tools=(), task="Test the search page of shop.example.com.",
          nxt="ok, keep going"):
    msgs = [_sys(folder), {"role": "user", "content": task}]
    for cid, cmd, out in tools:
        msgs += [_call(cid, cmd), _result(cid, out)]
    msgs += [{"role": "assistant", "content": reply}, {"role": "user", "content": nxt}]
    return msgs


REFLECT = ("c1", "curl -si 'https://shop.example.com/search?q=<script>alert(1)</script>'",
           "HTTP/2 200\ncontent-type: text/html\n\n<html><p>Results for <script>alert(1)</script>"
           "</p></html>")
EXECUTED = ("c2", "node shot.js https://shop.example.com/search?q=x",
            "navigated https://shop.example.com/search\nalert dialog opened: 1")
XSS_REPLY = "I confirmed a reflected XSS on https://shop.example.com/search?q=. Severity: High."


def _check(msgs, sid=SID, body=None):
    with A.app.test_request_context("/v1/chat/completions", headers={"X-Session-Id": sid}):
        return A._findings_check(msgs, body or {"model": "auto"})


def _note(msgs, sid=SID):
    with A.app.test_request_context("/v1/chat/completions", headers={"X-Session-Id": sid}):
        A._ctx_begin({"model": "auto"}, msgs, A._est_tokens(msgs))
        return A._findings_note(msgs)


def _rows(fc):
    return A._fc_ledger().rows(fc["project"])


def _notes_in(msgs):
    return [m for m in msgs if m.get("role") == "system"
            and str(m.get("content")).startswith("HUB FINDINGS CHECK")]


# --------------------------------------------------------------------------- #
# Detection: claims vs hedged talk
# --------------------------------------------------------------------------- #

CLAIMS = [
    ("I confirmed a reflected XSS on https://shop.example.com/search?q=x.", "xss",
     "shop.example.com/search"),
    ("SQL injection found in the id parameter of https://api.example.com/items?id=1.", "sqli",
     "api.example.com/items"),
    ("The /api/fetch endpoint on api.example.com is vulnerable to SSRF.", "ssrf",
     "api.example.com/api/fetch"),
    ("I was able to confirm the IDOR on https://api.example.com/v2/orders/1043.", "idor",
     "api.example.com/v2/orders/{id}"),
    ("Confirmed open redirect: https://app.example.com/login?next=https://evil.example.net",
     "open_redirect", "app.example.com/login"),
    ("Subdomain takeover confirmed on docs.example.org (dangling CNAME).", "subdomain_takeover",
     "docs.example.org"),
    ("Remote code execution confirmed on https://ci.example.com/hooks/run.", "rce",
     "ci.example.com/hooks/run"),
    ("Exposed .git directory found on https://www.example.com/.git/config.", "info_disclosure",
     "example.com/.git/config"),
    ("CSRF works on https://app.example.com/settings/email (no token checked).", "csrf",
     "app.example.com/settings/email"),
    ("Authentication bypass achieved on https://admin.example.com/panel.", "auth_bypass",
     "admin.example.com/panel"),
    ("I tested my own app at http://localhost:3000 and confirmed SQL injection at "
     "http://localhost:3000/api/search.", "sqli", "localhost:3000/api/search"),
]


@pytest.mark.parametrize("text,cls,target", CLAIMS)
def test_claims_are_detected_with_class_and_target(text, cls, target):
    got = F.detect(text)
    assert [(c["class"], c["target"]) for c in got] == [(cls, target)]


HEDGED = [
    "The search page might be vulnerable to XSS; I will test it next.",
    "Next I want to check whether /api/users/12 has an IDOR.",
    "I found a potential SQL injection in the login form.",
    "To test for SSRF I will send the URL parameter to a collaborator host.",
    "If the WAF is bypassed, the XSS would be exploitable.",
    "This could be an open redirect on https://app.example.com/login.",
    "It appears to be vulnerable to SQL injection, but I have not confirmed it.",
    "Let me check if https://shop.example.com/search reflects input (possible XSS).",
]


@pytest.mark.parametrize("text", HEDGED)
def test_hedged_or_hypothetical_talk_is_not_a_claim(text):
    assert F.detect(text) == []


NOT_CLAIMS = [
    "The /search endpoint is not vulnerable to XSS: the input is properly escaped.",
    "No SQL injection found on https://api.example.com/items.",
    "There is a CSRF token on every form, so that is handled.",
    "The response sets X-XSS-Protection, found on all pages.",
    "That was a false positive: the XSS payload was escaped.",
    "## Test plan\n- XSS on /search confirmed by the scanner later\n- SQLi on /login\n",
    "## Next steps\n- Reflected XSS found earlier needs a browser check\n",
    "```\nGET parameter 'id' is vulnerable to SQL injection\n```",
]


@pytest.mark.parametrize("text", NOT_CLAIMS)
def test_negations_controls_plans_and_code_blocks_are_not_claims(text):
    assert F.detect(text) == []


def test_report_style_heading_with_fields_is_a_claim_with_severity():
    text = ("## Finding 1: IDOR on order details\n\n**Endpoint:** https://api.example.com/v2/orders/1043\n"
            "**Severity:** High\n**Impact:** read any order\n")
    got = F.detect(text)
    assert len(got) == 1
    assert got[0]["class"] == "idor" and got[0]["severity"] == "high" and got[0]["source"] == "heading"
    assert got[0]["target"] == "api.example.com/v2/orders/{id}"


def test_items_under_a_findings_heading_and_table_rows_are_claims():
    text = ("## Findings\n- Open redirect at https://app.example.com/login?next=\n"
            "- SSRF on api.example.com/fetch (High)\n\n"
            "| Issue | Endpoint | Status |\n|---|---|---|\n"
            "| Stored XSS | app.example.com/comments | Confirmed |\n")
    got = {(c["class"], c["target"], c["severity"]) for c in F.detect(text)}
    assert got == {("open_redirect", "app.example.com/login", ""),
                   ("ssrf", "api.example.com/fetch", "high"),
                   ("xss", "app.example.com/comments", "")}


def test_impact_wording_after_the_claim_does_not_hedge_it():
    got = F.detect("Confirmed stored XSS on app.example.com/comments; an attacker could "
                   "steal session cookies.")
    assert [c["class"] for c in got] == ["xss"]


def test_same_finding_twice_in_one_reply_is_one_claim_and_ids_normalise():
    text = ("IDOR confirmed on https://api.example.com/users/12/profile.\n"
            "The IDOR on https://api.example.com/users/13/profile was reproduced too.")
    got = F.detect(text)
    assert len(got) == 1 and got[0]["target"] == "api.example.com/users/{id}/profile"


def test_detect_never_raises_on_junk():
    for junk in (None, 3, "", "   ", "x" * 100000, {"a": 1}):
        assert F.detect(junk) == []


# --------------------------------------------------------------------------- #
# Evidence: observed proof vs none, and the false-positive rules
# --------------------------------------------------------------------------- #

def _claim(text):
    got = F.detect(text)
    assert got, text
    return got[0]


def _res(cid, cmd, out, name="bash"):
    return {"id": cid, "name": name, "args": json.dumps({"command": cmd}), "output": out}


def test_no_observed_result_means_no_proof():
    ev = F.evidence(_claim(XSS_REPLY), [])
    assert ev["proof"] == "none" and ev["status"] == "unverified"
    assert "no observed request + response" in ev["reasons"][0]


def test_a_result_for_another_target_is_not_proof():
    ev = F.evidence(_claim(XSS_REPLY), [_res("c9", "curl -si https://other.example.net/search?q=x",
                                             "HTTP/1.1 200 OK\n\nalert dialog opened")])
    assert ev["status"] == "unverified"


def test_a_connection_error_is_not_an_observed_response():
    ev = F.evidence(_claim(XSS_REPLY), [_res("c1", "curl https://shop.example.com/search",
                                             "curl: (6) Could not resolve host: shop.example.com")])
    assert ev["status"] == "unverified" and "show no response" in ev["reasons"][0]


def test_claim_without_target_is_never_proven():
    ev = F.evidence(_claim("Confirmed reflected XSS in the search box."), [_res(*REFLECT)])
    assert ev["status"] == "unverified" and "names no target" in ev["reasons"][0]


def test_reflected_input_without_execution_is_not_xss():
    ev = F.evidence(_claim(XSS_REPLY), [_res(*REFLECT)])
    assert ev["status"] == "false_positive" and "reflected input without execution" in ev["reasons"][0]


def test_executed_payload_is_observed_proof():
    ev = F.evidence(_claim(XSS_REPLY), [_res(*REFLECT), _res(*EXECUTED, name="playwright")])
    assert ev["proof"] == "observed" and ev["status"] == "proven"
    assert ev["pointer"].startswith("playwright call c2")


IDOR = "IDOR confirmed on https://api.example.com/v2/orders/1043."


def test_a_200_alone_is_not_access():
    ev = F.evidence(_claim(IDOR), [_res("c3", "curl -si https://api.example.com/v2/orders/1044",
                                        "HTTP/1.1 200 OK\ncontent-length: 0\n\n")])
    assert ev["status"] == "false_positive" and "a 200 alone" in ev["reasons"][0]


def test_a_403_means_access_was_not_granted():
    ev = F.evidence(_claim(IDOR), [_res("c3", "curl -si https://api.example.com/v2/orders/1044",
                                        "HTTP/1.1 403 Forbidden\n\n{\"error\": \"forbidden\"}")])
    assert ev["status"] == "false_positive" and "403" in ev["reasons"][0]


def test_data_in_a_response_still_needs_a_person_for_access_classes():
    out = 'HTTP/1.1 200 OK\ncontent-type: application/json\n\n{"order_id": 1044, "email": "b@example.com"}'
    ev = F.evidence(_claim(IDOR), [_res("c3", "curl -si https://api.example.com/v2/orders/1044", out)])
    assert ev["proof"] == "observed" and ev["status"] == "needs_review"
    assert "ANOTHER account" in ev["reasons"][0]


SQLI = "SQL injection confirmed on https://api.example.com/items?id=1."


def test_a_500_alone_is_not_sql_injection():
    ev = F.evidence(_claim(SQLI), [_res("c4", "curl -si \"https://api.example.com/items?id=1'\"",
                                        "HTTP/1.1 500 Internal Server Error\n\nerror")])
    assert ev["status"] == "false_positive" and "HTTP 500 alone" in ev["reasons"][0]


@pytest.mark.parametrize("out", [
    "[INFO] GET parameter 'id' is vulnerable. Do you want to keep testing the others",
    "HTTP/1.1 500\n\nYou have an error in your SQL syntax; check the manual near ''1'''",
])
def test_tool_confirmation_or_db_error_is_sqli_proof(out):
    ev = F.evidence(_claim(SQLI), [_res("c4", "sqlmap -u 'https://api.example.com/items?id=1'", out)])
    assert ev["status"] == "proven"


def test_a_version_banner_alone_is_not_a_vulnerability():
    claim = _claim("Information disclosure confirmed on https://www.example.com/ (server version).")
    ev = F.evidence(claim, [_res("c5", "curl -sI https://www.example.com/",
                                 "HTTP/1.1 200 OK\nServer: nginx/1.18.0\n")])
    assert ev["status"] == "false_positive" and "version banner alone" in ev["reasons"][0]


def test_a_cname_alone_is_not_a_takeover_but_the_fingerprint_is():
    claim = _claim("Subdomain takeover confirmed on docs.example.org.")
    cname = _res("c6", "dig docs.example.org", "docs.example.org. 300 IN CNAME example-docs.s3.amazonaws.com.")
    assert F.evidence(claim, [cname])["status"] == "false_positive"
    bucket = _res("c7", "curl -si http://docs.example.org/",
                  "HTTP/1.1 404 Not Found\n\n<Code>NoSuchBucket</Code>")
    assert F.evidence(claim, [cname, bucket])["status"] == "proven"


def test_open_redirect_needs_an_external_location():
    claim = _claim("Confirmed open redirect on https://app.example.com/login.")
    same = _res("c8", "curl -si 'https://app.example.com/login?next=/home'",
                "HTTP/1.1 302 Found\nLocation: https://app.example.com/home\n")
    assert F.evidence(claim, [same])["status"] == "false_positive"
    ext = _res("c9", "curl -si 'https://app.example.com/login?next=https://evil.example.net'",
               "HTTP/1.1 302 Found\nLocation: https://evil.example.net/\n")
    assert F.evidence(claim, [same, ext])["status"] == "proven"


def test_cors_wildcard_is_not_exploitable_but_reflected_origin_with_credentials_is():
    claim = _claim("CORS misconfiguration confirmed on https://api.example.com/me.")
    star = _res("c1", "curl -si -H 'Origin: https://evil.example.net' https://api.example.com/me",
                "HTTP/1.1 200 OK\nAccess-Control-Allow-Origin: *\n")
    assert F.evidence(claim, [star])["status"] == "false_positive"
    cred = _res("c2", "curl -si -H 'Origin: https://evil.example.net' https://api.example.com/me",
                "HTTP/1.1 200 OK\nAccess-Control-Allow-Origin: https://evil.example.net\n"
                "Access-Control-Allow-Credentials: true\n")
    assert F.evidence(claim, [cred])["status"] == "proven"


def test_ssrf_needs_an_interaction_or_internal_content():
    claim = _claim("SSRF confirmed on https://api.example.com/fetch.")
    plain = _res("c1", "curl -si 'https://api.example.com/fetch?url=https://example.net'",
                 "HTTP/1.1 200 OK\n\n<html>example</html>")
    assert F.evidence(claim, [plain])["status"] == "unverified"
    meta = _res("c2", "curl -s 'https://api.example.com/fetch?url=http://169.254.169.254/latest/meta-data/'",
                "ami-id\ninstance-id\nhostname")
    assert F.evidence(claim, [plain, meta])["status"] == "proven"


def test_a_scanner_template_match_needs_review():
    claim = _claim("Confirmed SSRF on https://api.example.com/fetch.")
    out = "[ssrf-blind] [http] [high] https://api.example.com/fetch?url=x"
    ev = F.evidence(claim, [_res("c1", "nuclei -u https://api.example.com/fetch", out)])
    assert ev["status"] == "needs_review" and "template match" in ev["reasons"][0]


# --- the agent's own payload is never proof (2026-10-30 review) -------------------------- #

SSRF_CLAIM = "Confirmed SSRF at https://api.example.com/fetch."
OOB_URL = "http://abc123.oast.fun/"


def test_the_payload_domain_echoed_in_the_request_is_not_an_interaction():
    out = ("* Connected to api.example.com\n> GET /fetch?url=%s HTTP/1.1\n> Host: api.example.com\n"
           ">\n< HTTP/1.1 403 Forbidden\n< content-type: text/html\n<\n<h1>Forbidden</h1>" % OOB_URL)
    ev = F.evidence(_claim(SSRF_CLAIM), [_res("c0", "curl -v 'https://api.example.com/fetch?url=%s'"
                                                     % OOB_URL, out)])
    assert ev["status"] != "proven" and ev["proof"] == "none"


def test_an_error_body_reflecting_the_payload_url_is_not_an_interaction():
    out = 'HTTP/1.1 400 Bad Request\ncontent-type: application/json\n\n{"error":"url not allowed: %s"}' % OOB_URL
    ev = F.evidence(_claim(SSRF_CLAIM), [_res("c0", "curl -si 'https://api.example.com/fetch?url=%s'"
                                                     % OOB_URL, out)])
    assert ev["status"] != "proven"


def test_an_rce_payload_domain_in_the_request_is_not_command_output():
    claim = _claim("Confirmed remote code execution at https://api.example.com/ping.")
    assert claim["class"] == "rce"
    out = ("> GET /ping?host=127.0.0.1;nslookup+abc.oast.fun HTTP/1.1\n> Host: api.example.com\n>\n"
           "< HTTP/1.1 200 OK\n<\nPING 127.0.0.1: 56 data bytes")
    ev = F.evidence(claim, [_res("c0", "curl -v 'https://api.example.com/ping?host=127.0.0.1;nslookup+abc.oast.fun'",
                                 out)])
    assert ev["status"] != "proven"


@pytest.mark.parametrize("log", [
    "[abc123] Received HTTP interaction from 203.0.113.9 at 2026-10-30 10:00:00",
    "[abc123] Received DNS interaction (A) from 203.0.113.9 at 2026-10-30 10:00:00",
    '{"protocol":"dns","unique-id":"abc123","full-id":"abc123","q-type":"A",'
    '"remote-address":"203.0.113.9","timestamp":"2026-10-30T10:00:00Z"}',
    "The Collaborator server received a DNS lookup of type A for the domain name abc123.example.net.",
])
def test_a_reported_interaction_is_still_proof(log):
    cmd = "curl -s 'https://api.example.com/fetch?url=%s' && sleep 5 && cat oob.log" % OOB_URL
    ev = F.evidence(_claim(SSRF_CLAIM), [_res("c0", cmd, "HTTP/1.1 200 OK\n\nok\n" + log)])
    assert ev["status"] == "proven" and ev["proof"] == "observed"


def test_a_secret_in_the_echoed_request_is_not_a_disclosure():
    claim = _claim("Information disclosure confirmed on https://www.example.com/api/info.")
    out = ("> GET /api/info?API_KEY=mykey123 HTTP/1.1\n> Host: www.example.com\n>\n"
           "< HTTP/1.1 200 OK\n< content-type: application/json\n<\n{\"ok\": true}")
    ev = F.evidence(claim, [_res("c0", "curl -v 'https://www.example.com/api/info?API_KEY=mykey123'", out)])
    assert ev["status"] != "proven"


# --- a stack trace proves only inside the target's response -------------------------------- #

DISC_CLAIM = "Confirmed information disclosure at https://target.example.com/api/debug."
LOCAL_CRASH = (
    "Traceback (most recent call last):\n"
    '  File "/usr/lib/python3/dist-packages/urllib3/connection.py", line 169, in _new_conn\n'
    "    conn = connection.create_connection(\n"
    '  File "/usr/lib/python3/dist-packages/urllib3/util/connection.py", line 96, in create_connection\n'
    "    raise err\n"
    "ConnectionRefusedError: [Errno 111] Connection refused\n\n"
    "During handling of the above exception, another exception occurred:\n\n"
    "Traceback (most recent call last):\n"
    '  File "/home/user/poc.py", line 7, in <module>\n'
    "    r = requests.get(sys.argv[1])\n"
    '  File "/usr/lib/python3/dist-packages/requests/adapters.py", line 519, in send\n'
    "    raise ConnectionError(e, request=request)\n"
    "requests.exceptions.ConnectionError: HTTPConnectionPool(host='target.example.com', port=443): "
    "Max retries exceeded with url: /api/debug (Caused by NewConnectionError('<urllib3.connection."
    "HTTPConnection object at 0x7f>: Failed to establish a new connection: [Errno 111] Connection refused'))")


def test_the_agents_own_crash_is_not_the_targets_disclosure():
    assert len(LOCAL_CRASH) > 400
    ev = F.evidence(_claim(DISC_CLAIM), [_res("c0", "python3 poc.py https://target.example.com/api/debug",
                                              LOCAL_CRASH)])
    assert ev["status"] == "needs_review" and ev["proof"] == "observed"
    assert "stack trace" in ev["reasons"][0] and "tool's own error" in ev["reasons"][0]


def test_a_local_parse_error_traceback_is_not_a_disclosure_either():
    crash = ("Traceback (most recent call last):\n  File \"/home/user/poc.py\", line 9, in <module>\n"
             "    print(r.json()['data']['users'])\nKeyError: 'data'")
    ev = F.evidence(_claim(DISC_CLAIM), [_res("c0", "python3 poc.py https://target.example.com/api/debug",
                                              crash)])
    assert ev["status"] != "proven"


@pytest.mark.parametrize("out", [
    "HTTP/1.1 500 Internal Server Error\ncontent-type: text/plain\n\nTraceback (most recent call last):\n"
    '  File "/app/views.py", line 41, in debug\n    raise ValueError(settings.DATABASES)\nValueError: x',
    "< HTTP/1.1 500 Internal Server Error\n< content-type: text/plain\n<\nTraceback (most recent call "
    'last):\n  File "/app/views.py", line 41, in debug\nValueError: x',
    "HTTP/1.1 500 Internal Server Error\n\njava.lang.NullPointerException\n"
    "\tat com.example.api.DebugController.show(DebugController.java:57)",
])
def test_a_stack_trace_in_the_targets_response_is_still_proof(out):
    ev = F.evidence(_claim(DISC_CLAIM), [_res("c0", "curl -si https://target.example.com/api/debug", out)])
    assert ev["status"] == "proven" and ev["proof"] == "observed"


# --- open redirect and CORS: the response must follow what the request injected ----------- #

REDIR_CLAIM = "Found an open redirect at https://app.example.com/login."


def test_a_redirect_to_the_sites_login_provider_is_not_an_open_redirect():
    ev = F.evidence(_claim(REDIR_CLAIM), [_res(
        "c0", "curl -si 'https://app.example.com/login?next=https://evil.example.org'",
        "HTTP/1.1 302 Found\nLocation: https://accounts.google.com/o/oauth2/v2/auth?client_id=1&"
        "redirect_uri=https://app.example.com/cb\n")])
    assert ev["status"] == "needs_review"
    assert "did not inject" in ev["reasons"][0] and "accounts.google.com" in ev["reasons"][0]


def test_an_external_location_on_a_200_is_not_a_redirect():
    ev = F.evidence(_claim(REDIR_CLAIM), [_res(
        "c0", "curl -si 'https://app.example.com/login?next=https://cdn.other.net/x'",
        "HTTP/1.1 200 OK\nLocation: https://cdn.other.net/x\n\n<html>login</html>")])
    assert ev["status"] == "needs_review" and "HTTP 200" in ev["reasons"][0]


@pytest.mark.parametrize("cmd, out", [
    ("curl -si 'https://app.example.com/login?next=https%3A%2F%2Fevil.example.org%2F'",
     "HTTP/1.1 302 Found\nLocation: https://evil.example.org/\n"),
    ("curl -si 'https://app.example.com/login?next=//evil.example.org'",
     "HTTP/1.1 301 Moved Permanently\nLocation: //evil.example.org\n"),
    ("curl -siL 'https://app.example.com/login?next=https://evil.example.net'",
     "HTTP/1.1 302 Found\nLocation: https://evil.example.net/\n\nHTTP/2 200\ncontent-type: text/html\n\n<html>"),
    ("curl -v 'https://app.example.com/login?next=evil.example.org'",
     "> GET /login?next=evil.example.org HTTP/1.1\n>\n< HTTP/1.1 302 Found\n"
     "< Location: https://evil.example.org/\n<\n"),
])
def test_a_3xx_to_the_injected_host_is_still_proof(cmd, out):
    ev = F.evidence(_claim(REDIR_CLAIM), [_res("c0", cmd, out)])
    assert ev["status"] == "proven" and ev["proof"] == "observed"


CORS_CLAIM = "Confirmed CORS misconfiguration on https://api.example.com/me."


def test_a_fixed_partner_origin_with_credentials_is_not_a_cors_bug():
    ev = F.evidence(_claim(CORS_CLAIM), [_res(
        "c0", "curl -si -H 'Origin: https://evil.example.org' https://api.example.com/me",
        "HTTP/1.1 200 OK\nAccess-Control-Allow-Origin: https://app.partner-site.com\n"
        "Access-Control-Allow-Credentials: true\n")])
    assert ev["status"] == "needs_review" and "not the Origin the request sent" in ev["reasons"][0]


def test_a_foreign_origin_with_no_origin_sent_needs_review():
    ev = F.evidence(_claim(CORS_CLAIM), [_res(
        "c0", "curl -si https://api.example.com/me",
        "HTTP/1.1 200 OK\nAccess-Control-Allow-Origin: https://app.partner-site.com\n"
        "Access-Control-Allow-Credentials: true\n")])
    assert ev["status"] == "needs_review"


@pytest.mark.parametrize("cmd, acao", [
    ("curl -si -H 'Origin: https://evil.example.org/' https://api.example.com/me", "https://Evil.example.org"),
    ("curl -si -H 'Origin: null' https://api.example.com/me", "null"),
    ("python3 -c \"import requests; print(requests.get('https://api.example.com/me', "
     "headers={'Origin': 'https://evil.example.org'}).headers)\"", "https://evil.example.org"),
])
def test_the_sent_origin_reflected_with_credentials_is_still_proof(cmd, acao):
    ev = F.evidence(_claim(CORS_CLAIM), [_res(
        "c0", cmd, "HTTP/1.1 200 OK\nAccess-Control-Allow-Origin: %s\n"
                   "Access-Control-Allow-Credentials: true\n" % acao)])
    assert ev["status"] == "proven" and ev["proof"] == "observed"


def test_pointers_and_excerpts_never_carry_credentials():
    claim = _claim("Information disclosure confirmed on https://www.example.com/.env.")
    out = ("HTTP/1.1 200 OK\n\nAWS_SECRET_ACCESS_KEY=abcd1234secretvalue\nAuthorization: Bearer "
           "zzzzzzzzzzzzzzzzzzzz\nAKIAABCDEFGHIJKLMNOP")
    res = [_res("c1", "curl -si https://www.example.com/.env -H 'Cookie: sid=topsecretcookie'", out)]
    ev = F.evidence(claim, res)
    assert ev["status"] == "proven"
    blob = ev["pointer"] + F.relevant_excerpt(claim, res)
    for secret in ("abcd1234secretvalue", "zzzzzzzzzzzzzzzzzzzz", "AKIAABCDEFGHIJKLMNOP", "topsecretcookie"):
        assert secret not in blob


def test_tool_results_pair_calls_with_outputs_and_stop_at_the_reply():
    msgs = _conv("C:/x", XSS_REPLY, tools=[REFLECT])
    idx, text = F.last_finished_reply(msgs)
    assert text == XSS_REPLY
    res = F.tool_results(msgs, upto=idx)
    assert [(r["id"], r["name"]) for r in res] == [("c1", "bash")]
    assert "shop.example.com" in res[0]["args"]


def test_last_finished_reply_needs_a_fresh_user_turn_after_it():
    msgs = _conv("C:/x", XSS_REPLY)
    assert F.last_finished_reply(msgs[:-1]) == (None, "")          # history ends on the reply
    assert F.last_finished_reply(msgs[:-2] + [_call("z", "ls")]) == (None, "")


# --------------------------------------------------------------------------- #
# Ledger: dedupe, update rules, bounds, location
# --------------------------------------------------------------------------- #

def _ev(status, reason="r"):
    return {"proof": "observed" if status == "proven" else "none", "status": status,
            "pointer": "bash call c1", "reasons": [reason]}


def test_redetection_updates_one_row_and_proven_stays_proven(tmp_path):
    clock = [1000.0]
    led = F.Ledger(str(tmp_path / "f"), clock=lambda: clock[0])
    proj = str(tmp_path)
    a = _claim("IDOR confirmed on https://api.example.com/users/12.")
    b = _claim("IDOR reproduced on https://API.example.com/users/99/.")
    assert a["key"] == b["key"]
    led.upsert(proj, [(a, _ev("unverified"), "glm")], "c1")
    clock[0] = 2000.0
    led.upsert(proj, [(b, _ev("proven"), "glm")], "c1")
    clock[0] = 3000.0
    led.upsert(proj, [(a, _ev("unverified"), "glm")], "c1")
    rows = led.rows(proj)
    assert len(rows) == 1
    r = rows[0]
    assert r["status"] == "proven" and r["seen"] == 3
    assert r["first_seen"] == 1000.0 and r["last_seen"] == 3000.0
    assert set(r) >= {"id", "title", "class", "target", "status", "reasons", "evidence",
                      "conversation", "first_seen", "last_seen", "verifier"}


def test_a_verifier_call_stands_until_the_observed_state_changes(tmp_path):
    led = F.Ledger(str(tmp_path / "f"))
    proj = str(tmp_path)
    c = _claim(XSS_REPLY)
    (fid, _st, wants), = led.upsert(proj, [(c, _ev("unverified"), "")], "c1")
    assert wants
    assert led.apply_verdict(proj, fid, {"ok": False, "problems": ["not a vuln"], "severity": "high"},
                             "kimi") == "false_positive"
    (_fid, st, wants), = led.upsert(proj, [(c, _ev("unverified"), "")], "c1")
    assert st == "false_positive" and not wants          # no second call for the same claim
    (_fid, st, wants), = led.upsert(proj, [(c, _ev("proven"), "")], "c1")
    assert st == "proven" and not wants


@pytest.mark.parametrize("verdict,expected", [
    ({"ok": True, "problems": [], "severity": "low"}, "needs_review"),
    ({"ok": False, "problems": ["not a vulnerability"], "severity": "high"}, "false_positive"),
    ({"ok": False, "problems": ["no response shown"], "severity": "low"}, "unverified"),
    (None, "needs_review"),
])
def test_verdict_mapping_never_makes_a_finding_proven(tmp_path, verdict, expected):
    led = F.Ledger(str(tmp_path / "f"))
    proj = str(tmp_path)
    (fid, _s, _w), = led.upsert(proj, [(_claim(XSS_REPLY), _ev("unverified"), "")], "c1")
    assert led.apply_verdict(proj, fid, verdict, "kimi") == expected
    row = led.rows(proj)[0]
    assert row["verifier"] == "kimi" and row["status_source"] == "verifier"


def test_the_ledger_is_bounded_and_lives_outside_the_project(tmp_path):
    root = tmp_path / "state" / "findings"
    proj = tmp_path / "proj"
    proj.mkdir()
    clock = [0.0]
    led = F.Ledger(str(root), clock=lambda: clock[0])
    for i in range(F.MAX_FINDINGS + 15):
        clock[0] = float(i)
        led.upsert(str(proj), [(_claim("SSRF confirmed on https://h%d.example.com/f." % i),
                                _ev("unverified"), "")], "c1")
    data = json.loads(open(led.path_for(str(proj)), encoding="utf-8").read())
    assert len(data["findings"]) == F.MAX_FINDINGS
    assert "h0.example.com" not in json.dumps(data)          # the least recently seen went first
    assert os.listdir(str(proj)) == []
    assert os.path.basename(led.path_for(str(proj))) == F.folder_key(str(proj)) + ".json"
    assert len(F.folder_key(str(proj))) == 16
    # a fresh Ledger reads it back
    assert len(F.Ledger(str(root)).rows(str(proj))) == F.MAX_FINDINGS


# --------------------------------------------------------------------------- #
# The note text
# --------------------------------------------------------------------------- #

def _row(i, status, title="Finding %d", reason="missing: an observed response"):
    return {"id": "f_%d" % i, "title": title % i if "%d" in title else title, "class": "xss",
            "status": status, "reasons": [reason], "last_seen": time.time() - i}


def test_the_note_lists_open_findings_and_proven_ones_briefly():
    rows = [_row(1, "unverified"), _row(2, "false_positive"), _row(3, "needs_review"), _row(4, "proven")]
    text = F.note_text(rows)
    assert text.startswith("HUB FINDINGS CHECK") and len(text) <= F.NOTE_MAX_CHARS
    assert text.index("LIKELY FALSE POSITIVE") < text.index("NEEDS REVIEW") < text.index("NOT CONFIRMED")
    assert "Proven: Finding 4" in text
    assert "before you report or fix any of them as confirmed" in text.lower()


def test_the_note_is_empty_when_every_finding_is_proven_or_old():
    assert F.note_text([_row(1, "proven")]) == ""
    old = _row(2, "unverified")
    old["last_seen"] = time.time() - F.NOTE_MAX_AGE - 10
    assert F.note_text([old]) == ""


def test_the_note_stays_under_900_chars_with_many_long_findings():
    rows = [_row(i, "unverified", title="X" * 200, reason="Y" * 400) for i in range(40)]
    text = F.note_text(rows)
    assert 0 < len(text) <= 900 and "more)" in text and text.endswith("proves it.")


def test_the_note_wording_is_neutral_for_any_kind_of_testing():
    text = F.note_text([_row(1, "unverified")]).lower()
    for word in ("bounty", "hackerone", "bugcrowd", "program"):
        assert word not in text
    assert "bounty" not in F.NOTE_HEAD.lower() + F.NOTE_TAIL.lower()
    assert "bounty" not in A._FC_VERIFIER_SYSTEM.lower()


# --------------------------------------------------------------------------- #
# _findings_check: filing, the background verdict, rate limits, fail-open
# --------------------------------------------------------------------------- #

def test_a_claim_is_filed_and_one_background_verdict_asked(fc):
    msgs = _conv(fc["project"], XSS_REPLY, tools=[REFLECT])
    filed = _check(msgs)
    assert len(filed) == 1
    rows = _rows(fc)
    assert len(rows) == 1 and rows[0]["class"] == "xss" and rows[0]["conversation"] == "hdr:" + SID
    assert len(fc["calls"]) == 1
    sent = fc["calls"][0]["messages"]
    assert sent[0]["role"] == "system" and "authorized test" in sent[0]["content"]
    assert "shop.example.com/search" in sent[1]["content"]
    assert "Results for" in sent[1]["content"]                  # the observed output excerpt
    # the deterministic false positive, then the (faked) verdict folded in
    assert rows[0]["status"] == "false_positive" and rows[0]["verifier"] == "kimi"
    assert fc["root"].exists() and os.listdir(fc["project"]) == []


def test_a_proven_claim_costs_no_verifier_call(fc):
    _check(_conv(fc["project"], XSS_REPLY, tools=[REFLECT, EXECUTED]))
    assert _rows(fc)[0]["status"] == "proven"
    assert fc["calls"] == []


def test_the_same_reply_is_checked_once(fc):
    msgs = _conv(fc["project"], XSS_REPLY)
    assert _check(msgs)
    assert _check(msgs) is None
    assert len(fc["calls"]) == 1 and _rows(fc)[0]["seen"] == 1


def test_nothing_happens_mid_tool_loop_or_without_a_folder(fc):
    msgs = _conv(fc["project"], XSS_REPLY)
    assert _check(msgs[:-1] + [_call("z1", "ls")]) is None      # not a finished exchange
    no_folder = [m for m in _conv(fc["project"], XSS_REPLY) if m.get("role") != "system"]
    assert _check(no_folder) is None
    assert not fc["root"].exists() and fc["calls"] == []


def test_hedged_replies_file_nothing(fc):
    assert _check(_conv(fc["project"], HEDGED[0])) is None
    assert not fc["root"].exists()


def test_self_testing_conversations_work_the_same(fc):
    reply = ("I tested my own app at http://localhost:3000 and confirmed SQL injection at "
             "http://localhost:3000/api/search.")
    tools = [("c1", "curl -si 'http://localhost:3000/api/search?q=1%27'",
              "HTTP/1.1 500 Internal Server Error\n\nInternal error")]
    _check(_conv(fc["project"], reply, tools=tools, task="Test my SaaS on localhost."))
    row = _rows(fc)[0]
    assert row["class"] == "sqli" and row["target"] == "localhost:3000/api/search"
    assert row["status"] in ("false_positive", "unverified")


def test_per_conversation_cooldown(fc):
    _check(_conv(fc["project"], XSS_REPLY))
    _check(_conv(fc["project"], "SSRF confirmed on https://api.example.com/fetch."))
    assert len(fc["calls"]) == 1                                # cooldown: the second waits
    ssrf = [r for r in _rows(fc) if r["class"] == "ssrf"][0]
    assert ssrf["status"] == "unverified" and ssrf["verifier"] == ""
    A._FC_LAST["hdr:" + SID] = time.monotonic() - A._FC_COOLDOWN - 1
    _check(_conv(fc["project"], "SSRF reproduced on https://api.example.com/fetch."))
    assert len(fc["calls"]) == 2                                # re-detected after the cooldown


def test_global_hourly_cap_and_per_check_max(fc, monkeypatch):
    monkeypatch.setattr(A, "_FC_HOURLY_CAP", 3)
    reply = ("## Findings\n- SSRF on api.example.com/fetch\n- Open redirect on app.example.com/go\n"
             "- RCE on ci.example.com/run\n")
    _check(_conv(fc["project"], reply), sid="a")
    assert len(fc["calls"]) == A._FC_MAX_PER_CHECK == 2
    _check(_conv(fc["project"], XSS_REPLY), sid="b")
    _check(_conv(fc["project"], IDOR), sid="c")
    assert len(fc["calls"]) == 3                                # the cap holds across conversations


def test_any_verifier_error_files_needs_review(fc):
    fc["state"]["raise"] = True
    _check(_conv(fc["project"], "SSRF confirmed on https://api.example.com/fetch."))
    row = _rows(fc)[0]
    assert row["status"] == "needs_review" and "could not be completed" in " ".join(row["reasons"])


def test_a_thread_that_cannot_start_files_needs_review(fc, monkeypatch):
    def boom(*a):
        raise RuntimeError("no threads")
    monkeypatch.setattr(A, "_fc_spawn", boom)
    _check(_conv(fc["project"], "SSRF confirmed on https://api.example.com/fetch."))
    assert _rows(fc)[0]["status"] == "needs_review"
    assert A._FC_BG_SEM.acquire(blocking=False)                 # the slot was given back


def test_logs_carry_counts_and_ids_never_finding_text(fc, caplog):
    caplog.set_level(logging.INFO, logger="free-llm-hub")
    _check(_conv(fc["project"], XSS_REPLY, tools=[REFLECT]))
    text = caplog.text
    assert "[findings]" in text and "1 claim(s)" in text
    for leak in ("shop.example.com", "search", "XSS", "alert(1)", "scripting"):
        assert leak not in text


def test_flag_off_reads_config_and_changes_nothing(fc, monkeypatch):
    real = A.config.get_flag
    monkeypatch.setattr(A.config, "get_flag",
                        lambda name, default=None: False if name == "findings_check" else real(name, default))
    msgs = _conv(fc["project"], XSS_REPLY, tools=[REFLECT])
    assert A._fc_on() is False
    assert _check(msgs) is None
    assert _note(msgs) is msgs
    assert not fc["root"].exists() and fc["calls"] == []


def test_the_flag_name_has_no_program_wording():
    import inspect
    src = inspect.getsource(A._fc_on)
    assert '"findings_check"' in src and "bounty" not in src


# --------------------------------------------------------------------------- #
# The note: next turns of that conversation, in that folder, only
# --------------------------------------------------------------------------- #

def test_the_note_reaches_the_next_turns_of_that_conversation_only(fc, tmp_path):
    msgs = _conv(fc["project"], XSS_REPLY, tools=[REFLECT])
    assert _note(msgs) is msgs                                  # nothing filed yet: no note
    _check(msgs)
    later = msgs + [{"role": "assistant", "content": "Working on it."},
                    {"role": "user", "content": "write the report"}]
    out = _note(later)
    notes = _notes_in(out)
    assert len(notes) == 1 and "LIKELY FALSE POSITIVE" in notes[0]["content"]
    assert out.index(notes[0]) == 1                             # right after the leading system msg
    assert later == msgs + later[len(msgs):]                    # the caller's list is untouched
    assert _note(later, sid="someone-else") is later            # another conversation
    other = tmp_path / "other"
    other.mkdir()
    moved = [_sys(str(other))] + later[1:]
    assert _note(moved) is moved                                # another folder


def test_proven_only_gives_no_note(fc):
    msgs = _conv(fc["project"], XSS_REPLY, tools=[REFLECT, EXECUTED])
    _check(msgs)
    assert _note(msgs) is msgs


def test_no_note_on_a_cli_compaction_request(fc):
    msgs = _conv(fc["project"], XSS_REPLY)
    _check(msgs)
    compact = msgs[:-1] + [{"role": "user", "content":
                            "Your task is to create a detailed summary of the conversation so far."}]
    assert A.ctxwin.is_compaction_request(compact)
    assert _note(compact) is compact
    assert len(_notes_in(_note(msgs))) == 1                    # the same turn otherwise gets it


def _hop(monkeypatch, messages, no_craft=False):
    posted = []

    class _R:
        status_code = 200
        headers = {}
        text = ""

        def json(self):
            return _answer("ok")

        def close(self):
            pass
    monkeypatch.setattr(A.requests, "post", lambda *a, **kw: posted.append(kw.get("json")) or _R())
    monkeypatch.setattr(A, "_post_with_header_deadline",
                        lambda deadline, post, **kw: posted.append(kw.get("json")) or _R())
    monkeypatch.setattr(A, "_apply_craft_brief", lambda m, agentic=False, tools=None: m)
    monkeypatch.setattr(A, "_with_model_guide", lambda m, pid, model, tools: m)
    monkeypatch.setattr(A, "_apply_goal_note", lambda m: m)
    monkeypatch.setattr(A.config, "get_provider_config", lambda pid: {"api_keys": ["k"]})
    monkeypatch.setattr(A, "_resolve_base_url", lambda pid, pcfg: "https://example.invalid/v1")
    payload = {"model": "m", "messages": messages}
    if no_craft:
        payload["_no_craft"] = True
    with A.app.test_request_context("/v1/chat/completions", headers={"X-Session-Id": SID}):
        A._ctx_begin({"model": "auto"}, messages, A._est_tokens(messages))
        A._upstream_chat("cerebras", payload, False)
    return posted[-1]["messages"]


def test_upstream_hop_carries_the_note_but_pipeline_stages_do_not(fc, monkeypatch):
    msgs = _conv(fc["project"], XSS_REPLY, tools=[REFLECT])
    _check(msgs)
    assert len(_notes_in(_hop(monkeypatch, msgs))) == 1
    assert _notes_in(_hop(monkeypatch, msgs, no_craft=True)) == []


# --------------------------------------------------------------------------- #
# The background verdict, for real: a plain thread, no request context
# --------------------------------------------------------------------------- #

def test_the_background_verdict_runs_off_the_request_thread_on_another_family(fc, monkeypatch):
    seen = {}

    class _VResp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"role": "assistant", "content":
                    "VERDICT: REVISE\nPROBLEMS:\n- no observed response shows the payload executing\n"
                    "SEVERITY: high"}}]}

        def close(self):
            pass

    def dispatch(pid, payload, deadline):
        seen["pid"], seen["payload"] = pid, payload
        seen["thread"] = threading.current_thread().name
        return _VResp(), None
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("p1", "glm-5.3", "medium"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [("p1", "glm-5.3"), ("p2", "kimi-k3"),
                                                            ("p3", "glm-4.7")])
    monkeypatch.setattr(A, "_role_candidates", lambda chain, producer, *a, **k: [
        (p, m, 130.0) for p, m in chain if (p, m) != tuple(producer)[:2]])
    monkeypatch.setattr(A, "_rank_verifier_pool", lambda pool: pool)
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", dispatch)
    for name in ("_record_chat_usage", "_verifier_stats_note", "_swarm_note_member_status"):
        monkeypatch.setattr(A, name, lambda *a, **k: None)
    # the real verdict call (_free_verdict) and the real thread start, captured at import
    monkeypatch.setattr(A, "_fc_verdict_call", _REAL_VERDICT_CALL)
    monkeypatch.setattr(A, "_fc_spawn", _REAL_SPAWN)
    monkeypatch.setattr(A, "_fc_producer", lambda body, messages: ("p1", "glm-5.3"))
    tools = [("c1", "curl -si https://shop.example.com/search -H 'Authorization: Bearer sekret123456789'",
              "HTTP/2 200\n\n<p><script>alert(1)</script></p>")]
    _check(_conv(fc["project"], XSS_REPLY, tools=tools))
    deadline = time.time() + 10
    while time.time() < deadline and not _rows(fc)[0].get("verifier"):
        time.sleep(0.02)
    row = _rows(fc)[0]
    assert seen["thread"] == "findings-verify"
    assert seen["pid"] == "p2"                                  # kimi: another family than glm
    assert seen["payload"]["_no_craft"] is True
    sent = json.dumps(seen["payload"]["messages"])
    assert "sekret123456789" not in sent
    assert row["status"] == "false_positive" and row["verifier"] == "kimi"
    assert "payload executing" in " ".join(row["reasons"])


# --------------------------------------------------------------------------- #
# The route
# --------------------------------------------------------------------------- #

def test_api_findings_lists_the_ledger_and_is_token_gated(fc, monkeypatch):
    _check(_conv(fc["project"], XSS_REPLY, tools=[REFLECT]))
    # No control token configured for the first half (an earlier test on this worker may
    # have created one); the gate is checked explicitly below.
    monkeypatch.setattr(A.config, "get_control_token", lambda: None)
    client = A.app.test_client()
    r = client.get("/api/findings", query_string={"project_dir": fc["project"]})
    assert r.status_code == 200
    data = r.get_json()
    assert data["enabled"] is True and len(data["findings"]) == 1
    assert data["findings"][0]["status"] == "false_positive"
    assert r.headers.get("Cache-Control") == "no-store"
    assert client.get("/api/findings").status_code == 400
    monkeypatch.setattr(A.config, "get_control_token", lambda: "tok-findings")
    assert client.get("/api/findings", query_string={"project_dir": fc["project"]}).status_code == 401
    ok = client.get("/api/findings", query_string={"project_dir": fc["project"]},
                    headers={"X-Free-LLM-Hub-Token": "tok-findings"})
    assert ok.status_code == 200


# --------------------------------------------------------------------------- #
# End to end through the real /v1 handlers with a fake upstream
# --------------------------------------------------------------------------- #

@pytest.fixture
def route(monkeypatch, quiet, fc):
    sent = []

    def post(url=None, json=None, **kw):
        sent.append(json)
        return _CwmResp(200, _answer("Done."), chunks=_sse_chunks())
    monkeypatch.setattr(A.config, "get_provider_config", lambda pid: {"api_keys": ["k"]})
    monkeypatch.setattr(A, "_resolve_base_url", lambda pid, pcfg: "https://example.invalid/v1")
    monkeypatch.setattr(A.requests, "post", post)
    monkeypatch.setattr(A, "_post_with_header_deadline", lambda deadline, p, **kw: post(**kw))
    monkeypatch.setattr(A, "_model_ctx_info", lambda pid, model: (400000, "catalog"))
    monkeypatch.setattr(A, "_summarize_dropped", lambda dropped: None)
    monkeypatch.setattr(A, "_apply_craft_brief", lambda m, agentic=False, tools=None: m)
    monkeypatch.setattr(A, "_with_model_guide", lambda m, pid, model, tools: m)
    monkeypatch.setattr(A, "_apply_goal_note", lambda m: m)
    return {"sent": sent, "fc": fc}


TOOLS = [{"type": "function", "function": {"name": "bash", "description": "run",
                                           "parameters": {"type": "object",
                                                          "properties": {"command": {"type": "string"}}}}}]


@pytest.mark.parametrize("stream", [False, True])
def test_chat_handler_files_the_claim_and_the_same_request_carries_the_note(route, stream):
    fc = route["fc"]
    msgs = _conv(fc["project"], XSS_REPLY, tools=[REFLECT])
    client = A.app.test_client()
    r = client.post("/v1/chat/completions", headers={"X-Session-Id": "e2e-" + str(stream)},
                    json={"model": PINNED, "messages": msgs, "tools": TOOLS, "stream": stream})
    r.get_data()
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    rows = _rows(fc)
    assert len(rows) == 1 and rows[0]["status"] == "false_positive"
    notes = _notes_in(route["sent"][-1]["messages"])
    assert len(notes) == 1 and "Cross-site scripting" in notes[0]["content"]
    # another conversation in the same folder: no note
    other = msgs[:2] + [{"role": "user", "content": "hello"}]
    client.post("/v1/chat/completions", headers={"X-Session-Id": "e2e-other-" + str(stream)},
                json={"model": PINNED, "messages": other, "tools": TOOLS, "stream": stream}).get_data()
    assert _notes_in(route["sent"][-1]["messages"]) == []


def test_messages_handler_files_the_claim_too(route):
    fc = route["fc"]
    body = {"model": PINNED, "max_tokens": 300,
            "system": "You are a coding agent.\n<env>\nWorking directory: %s\n</env>" % fc["project"],
            "messages": [{"role": "user", "content": "Test the search page."},
                         {"role": "assistant", "content": [{"type": "tool_use", "id": "c1", "name": "bash",
                                                            "input": {"command": REFLECT[1]}}]},
                         {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1",
                                                       "content": REFLECT[2]}]},
                         {"role": "assistant", "content": XSS_REPLY},
                         {"role": "user", "content": "now write it up"}],
            "tools": [{"name": "bash", "description": "run",
                       "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}}}]}
    r = A.app.test_client().post("/v1/messages", headers={"X-Session-Id": "e2e-msg"}, json=body)
    r.get_data()
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    rows = _rows(fc)
    assert len(rows) == 1 and rows[0]["status"] == "false_positive"
    assert len(_notes_in(route["sent"][-1]["messages"])) == 1


def test_flag_off_the_handler_sends_the_old_body(route, monkeypatch):
    fc = route["fc"]
    monkeypatch.setattr(A, "_fc_on", lambda: False)
    msgs = _conv(fc["project"], XSS_REPLY, tools=[REFLECT])
    A.app.test_client().post("/v1/chat/completions", headers={"X-Session-Id": "e2e-off"},
                             json={"model": PINNED, "messages": msgs, "tools": TOOLS}).get_data()
    assert _notes_in(route["sent"][-1]["messages"]) == []
    assert not fc["root"].exists()

def test_an_error_page_that_echoes_the_metadata_url_is_not_proof():
    claim = _claim("Confirmed SSRF at https://api.example.com/fetch.")
    url = "http://metadata.google.internal/computeMetadata/v1/"
    echo = _res("c1", "curl -si 'https://api.example.com/fetch?url=%s'" % url,
                'HTTP/1.1 400 Bad Request\n\n{"error":"url not allowed: %s"}' % url)
    assert F.evidence(claim, [echo])["status"] != "proven"
    aws = "http://169.254.169.254/latest/meta-data/iam/security-credentials/"
    echo2 = _res("c2", "curl -si 'https://api.example.com/fetch?url=%s'" % aws.replace(":", "%3A"),
                 "HTTP/1.1 400 Bad Request\n\nblocked: %s" % aws)
    assert F.evidence(claim, [echo2])["status"] != "proven"


def test_internal_content_the_agent_did_not_send_is_still_proof():
    claim = _claim("Confirmed SSRF at https://api.example.com/fetch.")
    creds = _res("c1", "curl -s 'https://api.example.com/fetch?url=http://169.254.169.254/latest/meta-data/iam/security-credentials/web'",
                 'HTTP/1.1 200 OK\n\n{"Code":"Success","AccessKeyId":"ASIAXXXXXXXXXXXXXXXX"}')
    assert F.evidence(claim, [creds])["status"] == "proven"
