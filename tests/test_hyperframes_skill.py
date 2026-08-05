"""User 2026-08-05: "give him the hyperframe skills... use it by default in
all web page generation... in deployment he should also install it."

_ensure_hyperframes_skill (app.py) is the deployment-time hook, called from
_agent_cli_autoinstall_once for every claude/codex isolated CLI at hub boot
-- so a fresh deployment gets the skill without a manual one-off step, the
same way the CLI binaries themselves get auto-installed.
"""
import os
import shutil
import tempfile

import pytest

import app


@pytest.fixture
def state_dir(monkeypatch):
    d = tempfile.mkdtemp(prefix="hub-pytest-hyperframes-")
    monkeypatch.setattr(app, "_isolated_config_dir", lambda cli_id: os.path.join(d, cli_id, "config"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_opencode_is_skipped_entirely(state_dir, monkeypatch):
    """Not in hyperframes' own supported-agent list, and its isolated config
    has no skills/ convention -- must be a silent no-op, not an attempt."""
    called = []
    monkeypatch.setattr(app.shutil, "which", lambda name: called.append(name) or "/bin/npx")
    app._ensure_hyperframes_skill("opencode")
    assert not called, "opencode must never even look for npx"


def test_already_installed_is_a_noop(state_dir, monkeypatch):
    marker = os.path.join(state_dir, "claude", "config", "skills", "hyperframes-animation")
    os.makedirs(marker, exist_ok=True)
    ran = []
    monkeypatch.setattr(app.subprocess, "run", lambda *a, **k: ran.append(a) or None)
    app._ensure_hyperframes_skill("claude")
    assert not ran, "must not re-run the installer when the skill dir already exists"


def test_missing_npx_is_a_silent_noop(state_dir, monkeypatch):
    monkeypatch.setattr(app.shutil, "which", lambda name: None)
    ran = []
    monkeypatch.setattr(app.subprocess, "run", lambda *a, **k: ran.append(a) or None)
    app._ensure_hyperframes_skill("claude")
    assert not ran


def test_claude_install_sets_claude_config_dir_env(state_dir, monkeypatch):
    monkeypatch.setattr(app.shutil, "which", lambda name: "C:\\nodejs\\npx.cmd" if name == "npx" else None)
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        captured["env"] = kw.get("env")
        class R:
            returncode = 0
        return R()
    monkeypatch.setattr(app.subprocess, "run", fake_run)
    monkeypatch.setattr(app, "_ensure_isolated_dirs", lambda cli_id: None)
    app._ensure_hyperframes_skill("claude")
    assert "hyperframes-animation" in captured["argv"]
    assert captured["env"]["CLAUDE_CONFIG_DIR"] == os.path.join(state_dir, "claude", "config")


def test_codex_install_promotes_gsap_from_staging_and_cleans_up(state_dir, monkeypatch):
    """MEASURED 2026-08-05: the installer stages the plugin under .tmp and
    never registers it for codex -- this function must finish the job by
    copying just the gsap skill into codex's real flat skills/ layout."""
    monkeypatch.setattr(app.shutil, "which", lambda name: "C:\\nodejs\\npx.cmd" if name == "npx" else None)
    config_dir = os.path.join(state_dir, "codex", "config")

    def fake_run(argv, **kw):
        # Simulate what the real installer leaves behind: a staged gsap skill.
        staged = os.path.join(config_dir, ".tmp", "plugins", "plugins",
                              "hyperframes", "skills", "gsap")
        os.makedirs(staged, exist_ok=True)
        with open(os.path.join(staged, "SKILL.md"), "w") as f:
            f.write("---\nname: gsap\n---\n")
        class R:
            returncode = 0
        return R()
    monkeypatch.setattr(app.subprocess, "run", fake_run)
    monkeypatch.setattr(app, "_ensure_isolated_dirs", lambda cli_id: None)

    app._ensure_hyperframes_skill("codex")

    target = os.path.join(config_dir, "skills", "gsap", "SKILL.md")
    assert os.path.isfile(target), "gsap skill must be promoted to the real skills/ layout"
    assert not os.path.isdir(os.path.join(config_dir, ".tmp", "plugins")), \
        "the staging clone must be cleaned up, not left behind"


def test_a_failed_install_never_raises(state_dir, monkeypatch):
    monkeypatch.setattr(app.shutil, "which", lambda name: "npx" if name == "npx" else None)
    monkeypatch.setattr(app, "_ensure_isolated_dirs", lambda cli_id: None)

    def boom(*a, **k):
        raise OSError("network unreachable")
    monkeypatch.setattr(app.subprocess, "run", boom)
    app._ensure_hyperframes_skill("claude")   # must not raise
