"""codex's background app-server daemon is refreshed after Connect/Disconnect
(2026-10-25).

MEASURED on the owner's machine: codex keeps an "app-server daemon"
(<CODEX_HOME>/packages/app-server-daemon/.../codex.exe app-server ...) alive
across codex windows, and /model comes from it. One started before Connect kept
showing "gpt models only" after config.toml + model_catalog.json were rewritten;
the same daemon would keep the hub's modes after a Disconnect. The hub stops it,
but only while no codex window is open (codex starts a fresh one by itself).

Hermetic: a fake psutil, no real process is listed or killed.
"""
import os

import app as A

# Captured at import, before tests/conftest.py stubs it for every test.
_REAL_SOON = A._codex_restart_daemon_soon


class _NoThread:
    def start(self):
        pass


HOME = os.path.join("C:\\", "Users", "u", ".codex")
CFG = os.path.join(HOME, "config.toml")
DAEMON_EXE = os.path.join(HOME, "packages", "app-server-daemon", "releases",
                          "0.164.0", "bin", "codex.exe")


class _Proc:
    def __init__(self, pid, name, cmdline):
        self.info = {"pid": pid, "name": name, "cmdline": cmdline}


class _FakePsutil:
    def __init__(self, procs):
        self.procs = procs
        self.killed = []

    def process_iter(self, attrs=None):
        return list(self.procs)

    def Process(self, pid):
        fake = self

        class _P:
            def kill(self_inner):
                fake.killed.append(pid)
        return _P()


def _daemon(pid=10):
    return _Proc(pid, "codex.exe", ["\\\\?\\" + DAEMON_EXE, "app-server",
                                    "--listen", "unix://"])


def _updater(pid=11):
    return _Proc(pid, "codex.exe", [DAEMON_EXE, "app-server", "daemon",
                                    "pid-update-loop"])


def _tui(pid=20):
    return _Proc(pid, "codex.exe", [os.path.join("C:\\", "npm", "codex.exe")])


def _vscode(pid=30):
    return _Proc(pid, "codex.exe", [os.path.join("C:\\", "ext", "codex.exe"), "-c",
                                    "features.x=true", "app-server",
                                    "--analytics-default-enabled"])


def test_the_daemon_is_stopped_when_no_codex_window_is_open():
    ps = _FakePsutil([_daemon(), _updater(), _vscode()])
    assert A._codex_restart_daemon_once(CFG, psutil_mod=ps) == "stopped"
    assert sorted(ps.killed) == [10, 11]          # the VS Code app-server is left alone


def test_a_running_codex_window_is_never_cut_off():
    ps = _FakePsutil([_daemon(), _updater(), _tui()])
    assert A._codex_restart_daemon_once(CFG, psutil_mod=ps) == "busy"
    assert ps.killed == []


def test_nothing_to_do_without_a_daemon():
    ps = _FakePsutil([_tui(), _vscode()])
    assert A._codex_restart_daemon_once(CFG, psutil_mod=ps) == "none"
    assert ps.killed == []


def test_another_codex_homes_daemon_is_never_touched():
    other = _Proc(40, "codex.exe", [os.path.join("D:\\", "other", ".codex", "packages",
                                                 "app-server-daemon", "codex.exe"),
                                    "app-server"])
    ps = _FakePsutil([other])
    assert A._codex_restart_daemon_once(CFG, psutil_mod=ps) == "none"
    assert ps.killed == []


def test_unrelated_processes_are_ignored():
    ps = _FakePsutil([_daemon(), _Proc(50, "node.exe", ["node", "codex"])])
    assert A._codex_restart_daemon_once(CFG, psutil_mod=ps) == "stopped"
    assert ps.killed == [10]


def test_a_broken_process_table_never_raises():
    class _Bad:
        def process_iter(self, attrs=None):
            raise RuntimeError("boom")
    assert A._codex_restart_daemon_once(CFG, psutil_mod=_Bad()) == "error"


def test_connect_and_disconnect_ask_for_a_refresh():
    src = open("app.py", encoding="utf-8").read()
    soon = src.split("def _refresh_codex_catalog_soon(", 1)[1]
    soon = soon[:soon.index("\ndef ")]
    assert "_codex_restart_daemon_soon(" in soon
    disc = src.split("def _disconnect_codex(", 1)[1]
    disc = disc[:disc.index("\ndef ")]
    assert "_codex_restart_daemon_soon(" in disc
    bulk = src.split("def _bulk_hub_off(", 1)[1]
    bulk = bulk[:bulk.index("\ndef ")]
    assert "_codex_restart_daemon_soon(" in bulk


def test_the_flag_turns_it_off(monkeypatch):
    started = []
    monkeypatch.setattr(A.threading, "Thread",
                        lambda *a, **k: started.append(k) or _NoThread())
    monkeypatch.setattr(A.config, "get_flag",
                        lambda name, default=None: False if name == "codex_daemon_refresh" else default)
    _REAL_SOON(CFG)
    assert started == []


def test_the_flag_on_starts_one_waiter(monkeypatch):
    started = []
    monkeypatch.setattr(A.threading, "Thread",
                        lambda *a, **k: started.append(k) or _NoThread())
    monkeypatch.setattr(A.config, "get_flag", lambda name, default=None: default)
    monkeypatch.setitem(A._codex_daemon_state, "waiting", False)
    _REAL_SOON(CFG)
    _REAL_SOON(CFG)                       # a second call while one waits adds none
    assert len(started) == 1


# --------------------------------------------------------------------------- #
# Connect names the catalog in config.toml (codex ignores it otherwise)
# --------------------------------------------------------------------------- #

def _codex_home(tmp_path, body):
    cfg = tmp_path / "config.toml"
    cfg.write_text(body, encoding="utf-8")
    cat = tmp_path / "model_catalog.json"
    cat.write_text('{"models": [{"slug": "all", "display_name": "All (Calvoun hub)"}]}',
                   encoding="utf-8")
    return str(cfg), str(cat)


def test_connect_writes_the_catalog_key(tmp_path):
    cfg, cat = _codex_home(tmp_path, 'model_provider = "freehub"\nmodel = "auto"\n\n'
                                     '[model_providers.freehub]\nname = "x"\n')
    assert A._codex_ensure_catalog_key(cfg, cat) is True
    import tomllib
    data = tomllib.loads(open(cfg, encoding="utf-8").read())
    assert data["model_catalog_json"] == cat               # a TOP-level key
    assert data["model_providers"]["freehub"]["name"] == "x"
    assert A._codex_ensure_catalog_key(cfg, cat) is False  # idempotent


def test_a_users_own_catalog_key_is_kept(tmp_path):
    cfg, cat = _codex_home(tmp_path, "model_catalog_json = 'D:\\mine.json'\nmodel = \"auto\"\n")
    before = open(cfg, encoding="utf-8").read()
    assert A._codex_ensure_catalog_key(cfg, cat) is False
    assert open(cfg, encoding="utf-8").read() == before


def test_no_key_when_the_catalog_is_missing(tmp_path):
    cfg, cat = _codex_home(tmp_path, 'model = "auto"\n')
    os.remove(cat)
    assert A._codex_ensure_catalog_key(cfg, cat) is False
    assert "model_catalog_json" not in open(cfg, encoding="utf-8").read()


def test_disconnect_removes_the_key_connect_wrote(tmp_path):
    cfg, cat = _codex_home(tmp_path, 'model_provider = "freehub"\nmodel = "auto"\n')
    A._codex_ensure_catalog_key(cfg, cat)
    os.remove(cat)                                         # Disconnect deleted the catalog
    text, removed = A._strip_codex_catalog_key(open(cfg, encoding="utf-8").read(), cat)
    assert removed and "model_catalog_json" not in text


def test_the_catalog_refresh_writes_the_key():
    src = open("app.py", encoding="utf-8").read()
    body = src.split("def _refresh_codex_catalog_locked(", 1)[1]
    body = body[:body.index("\ndef ")]
    # both the "file already up to date" path and the fresh-write path
    assert body.count("_codex_ensure_catalog_key(") >= 2
