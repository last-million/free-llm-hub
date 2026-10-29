"""The authorized-testing brief (craft.TARGET_TESTING, Settings -> Skills id `app_testing`).

Free models doing authorized testing -- a bug bounty program, a client
engagement, or the owner's own app -- need scope and rules of engagement FIRST,
an evidence bar for what counts as a finding, and a report template. This pins:
  * what the brief says, at checklist level only (no payloads, no recipes);
  * where it fires: program words AND self-testing words ("pentest my SaaS",
    "check my site for vulnerabilities"), never ordinary coding security work
    ("add security headers", "fix the XSS in comments.js");
  * that it REPLACES the build briefs (a website pentest is not a website build)
    and keeps the engineering + builder-security briefs;
  * its cost, and that switching it off in Settings restores the old behaviour.
"""
import re

import pytest

import app
import craft
import skills


# --------------------------------------------------------------------------- #
# What it says
# --------------------------------------------------------------------------- #

def test_size():
    assert len(craft.TARGET_TESTING) <= 1800


def test_rules_of_engagement_come_first():
    body = craft.TARGET_TESTING
    lines = body.splitlines()
    assert lines[1].startswith("- Scope first")
    for word in ("rate limits", "no DoS", "no brute force", "no social engineering",
                 "own test accounts", "other users' data", "stop at proof"):
        assert word in body, word
    assert body.index("Scope first") < body.index("Method:")


def test_own_app_rules():
    body = craft.TARGET_TESTING
    own = [ln for ln in body.splitlines() if ln.startswith("- Own app")]
    assert own
    for word in ("staging", "test accounts", "never real users' data", "production",
                 "back up before anything destructive"):
        assert word in own[0], word


def test_evidence_false_positives_and_report():
    body = craft.TARGET_TESTING
    assert "observed, reproducible request and response" in body
    assert '"unverified"' in body
    for fp in ("reflected input that never executes", "a 200 with no protected data",
               "self-XSS", "missing headers"):
        assert fp in body, fp
    assert "duplicates" in body and "exclusions" in body
    for field in ("title", "asset", "severity with reasoning", "steps to reproduce", "impact",
                  "proof", "remediation"):
        assert field in body, field


def test_never_claim_what_was_not_observed():
    anti = craft.TARGET_TESTING.split("ANTI:", 1)[1]
    assert "you did not observe" in anti
    assert "theory called a vulnerability" in anti


def test_checklist_level_only_no_payloads():
    """Discipline and evidence, never attack material."""
    body = craft.TARGET_TESTING.lower()
    for marker in ("<script", "alert(", "' or ", "union select", "../", "${jndi",
                   "/etc/passwd", "sleep(", "onerror=", "169.254.169.254", "curl ", "sqlmap"):
        assert marker not in body, marker


# --------------------------------------------------------------------------- #
# Where it fires
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text", [
    "do recon on *.example.com for the hackerone program",
    "I want to start bug bounty on bugcrowd",
    "read the intigriti program and list the in-scope assets",
    "pentest my SaaS at app.example.com",
    "security test my app before launch",
    "audit my app for vulnerabilities",
    "check my site for vulnerabilities",
    "security assessment of our app",
    "audit our API's security",
    "find XSS in https://app.example.com/search",
    "hunt for IDORs in the API of example.com",
    "test for SSRF on the image fetcher",
    "write a bug bounty report for this IDOR",
    "is my server vulnerable to CVE-2024-3094?",
    "run nuclei against the in-scope assets",
    "security testing of our platform",
    "responsible disclosure of an issue I found on example.org",
    "test d'intrusion de mon site",
])
def test_fires_on_authorized_testing(text):
    assert craft.names(text)[0] == "app_testing", text


@pytest.mark.parametrize("text,expected", [
    ("add security headers to my app", ["security"]),
    ("fix the XSS in comments.js", ["security"]),
    ("upgrade lodash to fix CVE-2021-23337", []),
    ("run npm audit and fix the vulnerabilities", ["security"]),
    ("write unit tests for the login", ["programming", "security"]),
    ("write a unit test for the XSS escaper", ["programming", "security"]),
    ("write security tests for the login endpoint", ["security"]),
    ("add CSRF protection to the forms", ["security"]),
    ("that is out of scope for this PR", []),
    ("reconcile the ledger", []),
    ("this function is vulnerable to a race condition, fix it", ["security"]),
    ("build me a restaurant website", ["web_design", "seo", "images"]),
])
def test_ordinary_coding_security_work_keeps_the_existing_briefs(text, expected):
    assert craft.names(text) == expected


# --------------------------------------------------------------------------- #
# What it replaces, what it keeps
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text", [
    "check my website for vulnerabilities",
    "pentest my online store landing page",
    "pentest our production site and deploy nothing",
])
def test_a_testing_task_gets_no_build_briefs(text):
    names = craft.names(text)
    assert names[0] == "app_testing"
    assert not set(names) & set(craft._TESTING_REPLACES), names
    body = craft.system_message(text, tools=True)["content"]
    assert "DESIGN DECISIONS FIRST" not in body and "WEB MOTION" not in body
    assert "PUBLISH" not in body


def test_engineering_and_builder_security_briefs_stay():
    text = "pentest the api endpoint of my saas and debug the stack trace"
    assert craft.names(text) == ["app_testing", "programming", "security"]


def test_order_brief_first_loop_last():
    body = craft.system_message("pentest my SaaS at app.example.com", tools=True)["content"]
    assert body.startswith("TESTING BRIEF")
    assert body.index("TESTING BRIEF") < body.index("SECURITY BRIEF") < body.index("PLAN FIRST")
    assert body.rstrip().endswith(craft.VERIFY_RUN)


def test_a_tool_less_caller_gets_the_read_only_verify():
    body = craft.system_message("check my site for vulnerabilities", tools=False)["content"]
    assert "never say you ran" in body
    assert "PLAN FIRST" not in body


# --------------------------------------------------------------------------- #
# Cost
# --------------------------------------------------------------------------- #

def test_cost_with_programming_and_security_stays_small():
    for text in ("pentest the api endpoint of my saas and debug the stack trace, refactor "
                 "and write tests",
                 "pentest my online store landing page with checkout and login",
                 "bug bounty on hackerone: security assessment of the login, payment and "
                 "upload flows, find IDOR and XSS, write tests"):
        n = len(craft.system_message(text)["content"])
        assert n < craft.BRIEF_CEILING_CHARS, (text, n)
        assert n / 4 < 32768 * 0.07, (text, n)


def test_the_heaviest_request_is_still_a_build_not_a_test():
    """The ceiling test measures build requests; a testing request must stay
    well under the heaviest of them, so this brief can never be what moves it."""
    heaviest = max(len(craft.system_message(t)["content"])
                   for t in ("build an online store and deploy it",
                             "create a landing page for my saas",
                             "build me a restaurant website"))
    testing = max(len(craft.system_message(t)["content"])
                  for t in ("pentest my online store landing page with checkout and login",
                            "security test my saas landing page and its checkout"))
    assert testing < heaviest / 2


# --------------------------------------------------------------------------- #
# Settings -> Skills
# --------------------------------------------------------------------------- #

def test_it_is_a_builtin_skill_with_a_neutral_id():
    row = [b for b in skills.BUILTIN if b[0] == "app_testing"]
    assert row and row[0][1] == "App & API testing"
    assert "app_testing" in skills.BUILTIN_IDS
    assert not re.search(r"bounty|security", "app_testing")


def test_the_skills_api_lists_it(monkeypatch):
    # No control token: an earlier test on this worker may have created one.
    monkeypatch.setattr(app.config, "get_control_token", lambda: None)
    r = app.app.test_client().get("/api/skills")
    assert r.status_code == 200
    ids = [b["id"] for b in r.get_json()["builtin"]]
    assert "app_testing" in ids


def test_switched_off_the_old_behaviour_is_back(monkeypatch):
    text = "pentest my online store landing page"
    monkeypatch.setattr(craft, "_SKILL_SOURCE", lambda: ({"app_testing"}, []))
    off = craft.names(text)
    assert "app_testing" not in off
    assert off[:2] == ["landing", "ecommerce"]           # the build briefs come back
    body = craft.system_message(text, tools=True)["content"]
    assert "TESTING BRIEF" not in body
    monkeypatch.setattr(craft, "_SKILL_SOURCE", lambda: (set(), []))
    assert craft.names(text)[0] == "app_testing"
