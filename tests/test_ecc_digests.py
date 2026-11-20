"""ECC skills reach the model whole: a hub-authored digest per skill.

MEASURED 2026-11-20 (ecc.render on the vendored files): the injected block is
MAX_CHARS (1500) but the originals run 2.7-16.7K chars, so seven of the nine kept
only 8-11% (a title, a "When to Activate" list and the first section). Owner chose
option 1: a condensed digest per skill, used instead of the clipped original.
"""
import os
import re

import ecc


def test_every_catalog_skill_has_a_digest():
    for sid, filename, *_ in ecc.CATALOG:
        assert os.path.isfile(os.path.join(ecc.DIGEST_DIR, filename)), sid
        assert ecc.load_digest(filename).strip(), sid


def test_render_uses_the_digest_and_is_never_clipped():
    for sid, filename, *_ in ecc.CATALOG:
        block = ecc.render(sid)
        assert len(block) <= ecc.MAX_CHARS, sid
        assert not block.rstrip().endswith("[...]"), sid
        assert ecc.load_digest(filename) in block, sid
        assert block.startswith("ECC SKILL: "), sid
        assert ecc.CREDIT in block.split("\n", 1)[0], sid


def test_digest_drops_the_when_to_activate_list():
    # The skill has already fired when its block is injected; that list is dead weight.
    for sid, filename, *_ in ecc.CATALOG:
        assert "when to activate" not in ecc.load_digest(filename).lower(), sid


def test_digest_is_plain_text():
    bad = re.compile("[​-‏‪-‮⁠-⁤﻿\U000e0000-\U000e007f—–]")
    for sid, filename, *_ in ecc.CATALOG:
        text = ecc.load_digest(filename)
        assert not bad.search(text), sid
        assert "```" not in text and "<!--" not in text, sid


def test_missing_digest_falls_back_to_the_clipped_original(monkeypatch, tmp_path):
    monkeypatch.setattr(ecc, "DIGEST_DIR", str(tmp_path))
    ecc._cache.clear()
    try:
        block = ecc.render("ecc:tdd-workflow")
        assert block.startswith("ECC SKILL: ")
        assert len(block) <= ecc.MAX_CHARS
        assert "Test-Driven Development Workflow" in block      # the vendored body
    finally:
        ecc._cache.clear()


def test_unreadable_digest_falls_back(monkeypatch, tmp_path):
    (tmp_path / "tdd-workflow.md").mkdir()                     # a dir, not a file
    monkeypatch.setattr(ecc, "DIGEST_DIR", str(tmp_path))
    ecc._cache.clear()
    try:
        assert "Test-Driven Development Workflow" in ecc.render("ecc:tdd-workflow")
    finally:
        ecc._cache.clear()


def test_digest_is_cached_by_mtime(monkeypatch, tmp_path):
    p = tmp_path / "tdd-workflow.md"
    p.write_text("# one\nfirst rules", encoding="utf-8")
    monkeypatch.setattr(ecc, "DIGEST_DIR", str(tmp_path))
    ecc._cache.clear()
    try:
        assert "first rules" in ecc.render("ecc:tdd-workflow")
        p.write_text("# two\nsecond rules", encoding="utf-8")
        st = os.stat(p)
        os.utime(p, (st.st_atime, st.st_mtime + 5))
        assert "second rules" in ecc.render("ecc:tdd-workflow")
    finally:
        ecc._cache.clear()


def test_vendored_originals_are_untouched_by_the_digest_path():
    # The digests live beside the originals; the originals keep their frontmatter.
    for sid, filename, *_ in ecc.CATALOG:
        with open(os.path.join(ecc.DIR, filename), encoding="utf-8") as fh:
            assert fh.read().startswith("---\nname: "), sid
