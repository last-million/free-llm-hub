"""Every top-level init call must resolve to a top-level definition.

CAUGHT IN PRODUCTION 2026-08-30, by the user, not by the suite: initSwarmSwitch
was DEFINED inside initChat's scope but CALLED at top level. That is a
ReferenceError, it fired during the boot sequence, and it took the ENTIRE
dashboard down -- /agent rendered nothing at all.

The tests written alongside that feature all passed, because every one of them
asserted the source text CONTAINED a string. None asserted the call was in
scope. This checks the thing that actually broke: indentation in this file is
consistent, so a call at two spaces is top level and must be answered by a
definition at two spaces.
"""
import io
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _main_script():
    html = io.open(os.path.join(ROOT, "templates", "index.html"),
                   encoding="utf-8").read()
    blocks = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
    assert blocks, "no <script> block found"
    return max(blocks, key=len)


TOP_LEVEL_CALL = re.compile(r"^  ([A-Za-z_$][\w$]*)\(\);\s*$", re.M)
TOP_LEVEL_DEF = re.compile(r"^  (?:async\s+)?function ([A-Za-z_$][\w$]*)\s*\(", re.M)
NESTED_DEF = re.compile(r"^\s{4,}(?:async\s+)?function ([A-Za-z_$][\w$]*)\s*\(", re.M)

# Browser/library globals and helpers defined as `var x = function(){}` or
# assigned onto window elsewhere. Anything here is deliberately exempt.
_EXEMPT = {"main", "init"}


def test_no_top_level_call_targets_a_nested_function():
    js = _main_script()
    top_defs = set(TOP_LEVEL_DEF.findall(js))
    nested_defs = set(NESTED_DEF.findall(js))
    bad = []
    for name in set(TOP_LEVEL_CALL.findall(js)):
        if name in _EXEMPT or name in top_defs:
            continue
        if name in nested_defs:
            bad.append(name)
    assert not bad, (
        "called at top level but only defined inside another function "
        "(ReferenceError at boot, takes the whole dashboard down): %s" % sorted(bad))


def test_the_swarm_switch_init_is_called_where_it_is_defined():
    """The specific regression: definition and call must share a scope."""
    js = _main_script()
    assert "    function initSwarmSwitch(){" in js, "definition moved or reindented"
    assert "    initSwarmSwitch();" in js, "call is not at the definition's indent"
    assert "\n  initSwarmSwitch();" not in js, "call is back at top level"
