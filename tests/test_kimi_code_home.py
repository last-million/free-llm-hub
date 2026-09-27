"""Kimi Connect wires the config the installed `kimi` actually reads.

MEASURED 2026-09-27 (kimi-code 0.39.1, npm @moonshot-ai/kimi-code): the Node
Kimi Code resolves KIMI_CODE_HOME, else ~/.kimi-code -- NOT ~/.kimi, which is
the legacy Python kimi-cli's home. Connect wrote only ~/.kimi/config.toml, so
`kimi -p` in that home answered "No model configured" while the hub card said
Connected. Both generations are often installed side by side (kimi-cli /
kimi-legacy next to the npm `kimi`), so Connect writes the Kimi Code file and
the legacy one when it exists; Disconnect reverts every one.

Every test runs in a throwaway HOME."""
import os
import shutil
import tempfile
import tomllib

import pytest

import app

HUB_V1 = "http://127.0.0.1:%d/v1" % app.PORT
ROOT = "http://127.0.0.1:%d" % app.PORT

LEGACY = (
    'default_model = "kimi-code/kimi-for-coding"\n'
    '\n'
    '[providers."managed:kimi-code"]\n'
    'type = "kimi"\n'
    'base_url = "https://api.kimi.com/coding/v1"\n'
)
NEW = (
    'default_model = "kimi-code/k3"\n'
    '\n'
    '[providers."managed:kimi-code"]\n'
    'type = "kimi"\n'
    'base_url = "https://api.kimi.com/coding/v1"\n'
    '\n'
    '[models."kimi-code/k3"]\n'
    'provider = "managed:kimi-code"\n'
    'model = "k3"\n'
    'max_context_size = 262144\n'
)


@pytest.fixture
def home(monkeypatch):
    d = tempfile.mkdtemp(prefix="hub-pytest-kimihome-")
    monkeypatch.setattr(app, "_home", lambda: d)
    monkeypatch.delenv("KIMI_CODE_HOME", raising=False)
    store = {}
    monkeypatch.setattr(app.config, "set_setting", lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr(app.config, "get_setting", lambda k, default=None: store.get(k, default))
    try:
        yield d, store
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _w(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _r(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _entry():
    e = dict(app._get_cli_entry("kimi"))
    e["config_paths"] = app._kimi_config_paths()
    e["write_path"] = app._p_kimi()
    return e


def _new(d):
    return os.path.join(d, ".kimi-code", "config.toml")


def _old(d):
    return os.path.join(d, ".kimi", "config.toml")


def test_primary_is_kimi_code_home_unless_only_the_legacy_config_exists(home, monkeypatch):
    d, _ = home
    assert app._p_kimi() == _new(d)                     # nothing installed yet
    _w(_old(d), LEGACY)
    assert app._p_kimi() == _old(d)                     # legacy-only machine
    assert app._kimi_config_paths() == [_old(d)]
    os.makedirs(os.path.dirname(_new(d)))
    assert app._p_kimi() == _new(d)                     # kimi-code present -> it wins
    assert app._kimi_config_paths() == [_new(d), _old(d)]
    custom = os.path.join(d, "kh")
    monkeypatch.setenv("KIMI_CODE_HOME", custom)
    assert app._p_kimi() == os.path.join(custom, "config.toml")


def test_connect_wires_both_generations_and_disconnect_restores_each(home):
    d, store = home
    _w(_old(d), LEGACY)
    _w(_new(d), NEW)
    e = _entry()
    res = app._autofix_kimi(e, "free-llm-hub", ROOT, HUB_V1, "groq/x")
    assert res["ok"] and res["wrote_path"] == _new(d) and res["also_wrote"] == [_old(d)]
    for p in (_new(d), _old(d)):
        data = tomllib.loads(_r(p))
        assert data["default_model"] == "auto"
        assert data["providers"]["free-hub"]["base_url"] == HUB_V1
        assert set(app._HUB_TIER_IDS) <= set(data["models"])
        for mid in app._HUB_TIER_IDS:
            assert data["models"][mid]["provider"] == "free-hub"
            assert data["models"][mid]["model"] == mid
    assert tomllib.loads(_r(_new(d)))["models"]["kimi-code/k3"]["model"] == "k3"
    assert app._cli_connected(e)[0]

    out = app._disconnect_kimi(e)
    assert out["changed"] is True
    assert tomllib.loads(_r(_new(d)))["default_model"] == "kimi-code/k3"
    assert tomllib.loads(_r(_old(d)))["default_model"] == "kimi-code/kimi-for-coding"
    for p in (_new(d), _old(d)):
        text = _r(p)
        assert "free-hub" not in text and "127.0.0.1" not in text
        assert not os.path.exists(p + ".freehub-bak")
    assert _r(_new(d)) == NEW and _r(_old(d)) == LEGACY
    assert not app._cli_connected(e)[0]
    assert app._disconnect_kimi(e)["changed"] is False   # idempotent


def test_an_older_builds_legacy_only_connect_is_still_reverted(home):
    """Connected by a build that wrote only ~/.kimi (prev default under the
    historical setting key), disconnected by this one."""
    d, store = home
    _w(_old(d), app._kimi_apply_text(LEGACY, HUB_V1, "free-llm-hub"))
    store["kimi_prev_default_model"] = "kimi-code/kimi-for-coding"
    os.makedirs(os.path.dirname(_new(d)))              # kimi-code installed since
    out = app._disconnect_kimi(_entry())
    assert out["changed"] is True
    data = tomllib.loads(_r(_old(d)))
    assert data["default_model"] == "kimi-code/kimi-for-coding"
    assert "free-hub" not in data["providers"]


def test_a_tier_the_picker_saved_is_undone_and_a_users_same_named_alias_survives(home):
    d, _ = home
    _w(_new(d), NEW + '\n[models."swarm"]\nprovider = "managed:kimi-code"\nmodel = "k3"\n'
                      'max_context_size = 1000\n')
    e = _entry()
    app._autofix_kimi(e, "free-llm-hub", ROOT, HUB_V1, "groq/x")
    # The user's own "swarm" alias is kept and not duplicated (a second
    # [models."swarm"] table would make the whole file invalid TOML).
    text = _r(_new(d))
    data = tomllib.loads(text)
    assert data["models"]["swarm"]["provider"] == "managed:kimi-code"
    assert data["models"]["best"]["provider"] == "free-hub"
    text = text.replace('default_model = "auto"', 'default_model = "best"')   # /model picker
    _w(_new(d), text)
    app._disconnect_kimi(e)
    data = tomllib.loads(_r(_new(d)))
    assert data["default_model"] == "kimi-code/k3"
    assert "best" not in data["models"] and "auto" not in data["models"]
    assert data["models"]["swarm"]["provider"] == "managed:kimi-code"


def test_connect_created_file_is_removed_on_disconnect(home):
    d, _ = home
    e = _entry()
    app._autofix_kimi(e, "free-llm-hub", ROOT, HUB_V1, "groq/x")
    assert os.path.isfile(_new(d)) and not os.path.exists(_old(d))
    out = app._disconnect_kimi(e)
    assert out.get("deleted") is True and not os.path.exists(_new(d))
