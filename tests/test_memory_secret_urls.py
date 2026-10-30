"""A password inside a URL or a command line is never a lasting project fact.

The fact harvest drops a candidate that carries a credential (memory._SECRET_RE,
memory._looks_secret) and strips secret env assignments from a verified
command (memory._strip_secret_env). It did not know a password inside a URL
(postgres://user:pw@host), mysql's glued `-pPASSWORD`, `curl -u user:pass` or an
Authorization header, so `DATABASE_URL=postgres://u:pw@host/db pytest` that
passed was filed verbatim in "Commands verified to work here" -- and handed to
every later conversation in the folder and to free providers.

Every credential below is assembled at runtime so no credential-shaped literal
sits in the source. No network; the memory store is a temp folder.
"""
import os
import shutil
import tempfile

import pytest

import convmem
import memory


PW = "Tr0ub4" + "dor"


def _url(scheme="postgres", user="admin", pw=PW, host="db.prod.example.com:5432/app"):
    return scheme + "://" + user + ":" + pw + "@" + host


@pytest.fixture
def mem_dir(monkeypatch):
    d = tempfile.mkdtemp(prefix="hubmem-")
    monkeypatch.setenv(memory._ROOT_ENV, d)
    monkeypatch.setattr(memory, "_FACT_EXTRACTOR", None)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.mark.parametrize("text", [
    "DATABASE_URL=" + _url() + " pytest -q",
    "Decision: the app connects to " + _url("postgresql", "app", "S3cret" + "Pw!", "10.0.0.5/app"),
    "Always use the staging API at " + _url("https", "deploy", "hunter" + "22", "staging.example.com"),
    "redis://:" + "hunter" + "22" + "@cache:6379",
    "mysql -u root -p" + "S3cretPass" + "123 && npm test",
    "mariadb -h db -u app -p" + "S3cret" + "99 app",
    "curl -u admin:" + "secret" + "123 https://x.example.com",
    "curl --user=admin:" + "secret" + "123 https://x.example.com",
    "Authorization: Bearer " + "abcd" + "1234",
    "send the header Bearer " + "eyab" + "c123def456",
])
def test_credentials_in_urls_and_command_lines_are_secrets(text):
    assert memory._looks_secret(text)


@pytest.mark.parametrize("text", [
    "Decision: the API runs on http://localhost:8080/api",
    "Decision: deploy to https://example.com:443/@team",
    "Clone ssh://git@github.com:22/acme/repo.git",
    "Open https://user@example.com/x",
    "Proxy on http://[::1]:8080/x",
    "mysql -u root -p -P3306 mydb",
    "the bearer of bad news",
    "Decision: use pnpm, not npm.",
])
def test_ordinary_urls_and_commands_are_not(text):
    assert not memory._looks_secret(text)


def test_a_url_password_env_assignment_is_stripped_from_a_verified_command(mem_dir):
    proj = os.path.join(mem_dir, "p")
    os.makedirs(proj)
    cmd = "DATABASE_URL=" + _url() + " pytest -q"
    memory.harvest_facts("s1", reply="done", project_dir=proj, model_pass=False,
                         results=[{"command": cmd, "exit_code": 0,
                                   "output_tail": "4 passed in 0.12s"}])
    facts = memory.project_facts(proj)
    blob = "\n".join(facts)
    assert PW not in blob and "postgres://" not in blob
    cmds = [f for f in facts if f.startswith(memory.COMMANDS_FACT_PREFIX)]
    assert cmds and "`pytest -q`" in cmds[0]


def test_a_command_with_a_glued_mysql_password_is_not_filed(mem_dir):
    proj = os.path.join(mem_dir, "p2")
    os.makedirs(proj)
    cmd = "npm test && mysql -u root -p" + "S3cretPass" + "123 app"
    memory.harvest_facts("s1", reply="done", project_dir=proj, model_pass=False,
                         results=[{"command": cmd, "verdict": "PASS"}])
    blob = "\n".join(memory.project_facts(proj))
    assert "S3cretPass" not in blob


def test_a_decision_naming_a_url_password_is_not_filed(mem_dir):
    proj = os.path.join(mem_dir, "p3")
    os.makedirs(proj)
    memory.harvest_facts(
        "s1", project_dir=proj, model_pass=False,
        reply="Decision: the app connects to " + _url("postgresql", "app", "S3cret" + "Pw!",
                                                       "10.0.0.5/app") + "\n"
              "Decision: use pnpm, not npm.")
    facts = memory.project_facts(proj)
    assert "Decision: use pnpm, not npm." in facts
    assert not any("postgresql://" in f for f in facts)


def test_the_extractor_path_drops_a_url_password_too(mem_dir):
    memory._run_extractor(
        lambda q, r: ["The app connects to " + _url("postgresql", "app", "S3cret" + "Pw!",
                                                     "10.0.0.5/app"),
                      "The deploy target is Fly.io."],
        "s2", "x", "y", None)
    assert (memory.get("s2").get("facts") or []) == ["The deploy target is Fly.io."]


def test_the_archive_withholds_a_line_with_a_url_password():
    text = "connecting...\nDATABASE_URL=" + _url() + "\nok"
    out, n = convmem.redact_lines(text)
    assert n == 1 and PW not in out
    assert out.splitlines()[0] == "connecting..." and out.splitlines()[2] == "ok"
