"""Tests run inside WSL count as observed evidence.

MEASURED 2026-10-19 (Multi run swarm-a35f33083165, folder "bount en"):
helpers ran `wsl bash -c "cd ~/bount-en && python3 -m pytest tests/..."`;
the hub did not see pytest behind `wsl`, so phases whose tests passed read
"claimed, not checked". Pure: evidence.py only, no process is started.
"""
import pytest

import evidence as E

OK_OUT = "....\n5 passed in 0.12s\n"
FAIL_OUT = "..F\n2 failed, 3 passed in 0.40s\n"


@pytest.mark.parametrize("cmd", [
    'wsl bash -c "cd ~/bount-en && python3 -m pytest tests/test_tool_x.py -q"',
    "wsl -d Ubuntu -- python3 -m pytest -q",
    "wsl --distribution Ubuntu-24.04 --user hamza -e bash -lc 'cd ~/p && pytest'",
    "wsl.exe --cd ~/bount-en python3 -m pytest",
    "C:\\Windows\\System32\\wsl.exe bash -c 'pytest -q'",
    # codex on Windows reports the pwsh wrapper around it
    "pwsh -NoProfile -Command 'wsl bash -c \"cd ~/p && pytest -q\"'",
])
def test_pytest_behind_wsl_is_seen(cmd):
    assert E.detect(cmd)["tool"] == "pytest"
    assert E.classify(cmd, 0, OK_OUT)["verdict"] == E.PASS
    r = E.classify(cmd, 1, FAIL_OUT)
    assert r["verdict"] == E.FAIL and r["failed"] == 2


def test_npm_test_behind_wsl_is_seen():
    assert E.detect("wsl -e bash -lc 'npm test'")["tool"]


@pytest.mark.parametrize("cmd", [
    "wsl --list --verbose",
    "wsl --shutdown",
    "wsl",
    "wsl -d Ubuntu",
    "wsl ls -la",
])
def test_wsl_without_a_check_is_not_evidence(cmd):
    assert E.detect(cmd) is None


def test_the_inner_command_is_what_ran():
    assert E.inner_command('wsl bash -c "cd ~/b && python3 -m pytest -q"') == \
        "cd ~/b && python3 -m pytest -q"
    assert E.inner_command("wsl --list") == "wsl --list"


def test_a_plain_command_is_unchanged():
    assert E.inner_command("python3 -m pytest -q") == "python3 -m pytest -q"
