"""Regression: connecting Codex must not corrupt a config.toml that already has
a leading UTF-8 BOM.

A Windows editor commonly saves ~/.codex/config.toml as UTF-8 *with BOM*
(byte 0 = EF BB BF), which is valid TOML at the very start of the file.
`_autofix_codex` used to read it with encoding='utf-8', so the BOM survived as a
U+FEFF character; the additive rewrite then prepended `model_provider`/`model`
lines ABOVE it, leaving the BOM mid-file -> Codex rejected the whole config with
"invalid unquoted key, expected letters, numbers, `-`, `_`" at line 3.

Fix: read config.toml with encoding='utf-8-sig' so a leading BOM is stripped
before the additive rewrite (matching the utf-8-sig reads already used elsewhere).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
import config

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None

BASE_V1 = "http://127.0.0.1:8787/v1"
BOM = "﻿"
_ORIGINAL = (
    'model_reasoning_effort = "xhigh"\n'
    'personality = "pragmatic"\n'
    '\n'
    '[notice]\n'
    'hide_full_access_warning = true\n'
)


def _write_with_bom(path):
    # Exactly how a Windows editor saves UTF-8-with-BOM: byte 0 = EF BB BF.
    with open(path, "wb") as f:
        f.write(b"\xef\xbb\xbf" + _ORIGINAL.encode("utf-8"))


def test_codex_connect_with_leading_bom_stays_valid_toml(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    _write_with_bom(str(cfg))
    monkeypatch.setattr(app, "_p_codex", lambda: str(cfg))
    monkeypatch.setattr(config, "get_local_api_key", lambda: None)

    # CONNECT
    res = app._autofix_codex(None, None, None, BASE_V1, "auto")
    assert res.get("ok") is True, res

    text = cfg.read_text(encoding="utf-8")
    # The BOM must NOT survive anywhere — mid-file it makes Codex reject the config.
    assert BOM not in text, "UTF-8 BOM leaked into connected config.toml -> Codex parse error"
    assert 'model_provider = "freehub"' in text
    assert 'model_reasoning_effort = "xhigh"' in text  # user's own setting survives
    if tomllib is not None:
        tomllib.loads(text)  # raises TOMLDecodeError on the original bug

    # DISCONNECT returns it to valid, BOM-free TOML with the user's settings intact.
    app._disconnect_codex({})
    text2 = cfg.read_text(encoding="utf-8")
    assert BOM not in text2
    assert "freehub" not in text2
    assert 'model_reasoning_effort = "xhigh"' in text2
    if tomllib is not None:
        tomllib.loads(text2)
