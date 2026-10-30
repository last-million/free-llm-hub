"""Review findings fixed 2026-10-30 (verify.py, plan_check.py).

1. verify.parse_verdict read a JSON fragment the reviewer QUOTED in its
   problems ('expects {"status": "ok"}') as its verdict, so a high-severity
   REVISE came back as ACCEPT and the corrector never ran. Its fence regex was
   also dead: html_blocks' regex had the same name and replaced it at import.
2. verify.is_risky cleared several commands that write or delete as
   read-only ('cat a & rm -rf src', 'cat <(rm -rf build)', 'sort -o',
   'uniq IN OUT', a sed script's w / e command, find -fls), so the verifier
   never saw them.
3. plan_check.check_plan cut its findings at 30 BEFORE the "replan" ones
   (added last), so a big plan never got its one planner re-ask for a part the
   user listed and no phase covers.

Pure: no network, no model, no config.
"""
import json

import pytest

import plan_check
import swarm_windows as SW
import verify


# --------------------------------------------------------------------------- #
# 1. parse_verdict
# --------------------------------------------------------------------------- #

def test_the_verdict_parser_keeps_its_own_fence_regex():
    assert verify._FENCE_RE is not verify._WEB_FENCE_RE
    m = verify._FENCE_RE.search('```json\n{"ok": false}\n```')
    assert m and m.group(1).strip() == '{"ok": false}', "group 1 is the body, not the language"


def test_html_blocks_still_reads_web_fences():
    text = "Here:\n```html\n<!doctype html><html><body>x</body></html>\n```\n```python\nx = 1\n```"
    blocks = verify.html_blocks(text)
    assert [name for name, _ in blocks] == ["block-1.html"]
    assert blocks[0][1].startswith("<!doctype html>")


def test_a_verdict_line_beats_json_quoted_in_its_problems():
    reply = ('VERDICT: REVISE\nPROBLEMS:\n- The test expects {"status": "ok"} but the handler '
             'now returns {"status": "error"}\nSEVERITY: high')
    v = verify.parse_verdict(reply)
    assert v["ok"] is False and v["severity"] == "high"
    assert any('"status": "ok"' in p for p in v["problems"])


def test_a_quoted_error_object_does_not_drop_the_problems_or_the_severity():
    reply = ('VERDICT: REVISE\nPROBLEMS:\n- the API answers {"ok": false, "error": "quota"} '
             'and the proposal ignores it\nSEVERITY: high')
    v = verify.parse_verdict(reply)
    assert v["ok"] is False and v["severity"] == "high"
    assert v["problems"] and "ignores it" in v["problems"][0]


def test_a_fenced_verdict_beats_json_quoted_in_prose_before_it():
    reply = ('The handler used to return {"status": "ok"}.\n```json\n'
             '{"ok": false, "problems": ["returns an error now"], "severity": "high"}\n```')
    assert verify.parse_verdict(reply) == {"ok": False, "problems": ["returns an error now"],
                                           "severity": "high"}


def test_json_quoted_in_prose_is_not_taken_for_the_verdict():
    reply = 'The test expects {"status": "ok"} but gets {"status": "error"}, so this should be revised.'
    v = verify.parse_verdict(reply)
    assert v["ok"] is False, "a quoted tool result is not the reviewer's ACCEPT"
    assert v["severity"] == "low"


def test_the_verdict_object_in_prose_is_preferred_over_a_quoted_one():
    reply = ('The API returned {"ok": true}. My verdict: '
             '{"ok": false, "problems": ["claims a success the output does not show"], '
             '"severity": "high"}')
    v = verify.parse_verdict(reply)
    assert (v["ok"], v["severity"]) == (False, "high")
    assert v["problems"] == ["claims a success the output does not show"]


def test_a_bare_revise_line_takes_its_problems_from_a_json_verdict():
    reply = ('VERDICT: REVISE\n```json\n'
             '{"ok": false, "problems": ["bad import"], "severity": "high"}\n```')
    assert verify.parse_verdict(reply) == {"ok": False, "problems": ["bad import"],
                                           "severity": "high"}


def test_a_verdict_line_wins_over_a_contradicting_json_fragment():
    reply = 'VERDICT: ACCEPT\nThe output shows {"ok": false} only for the old build.'
    assert verify.parse_verdict(reply) == {"ok": True, "problems": [], "severity": "low"}


# --------------------------------------------------------------------------- #
# 2. is_risky: commands that only LOOK read-only
# --------------------------------------------------------------------------- #

def _bash(cmd):
    return {"id": "c1", "type": "function",
            "function": {"name": "Bash", "arguments": json.dumps({"command": cmd})}}


WRITES = [
    "cat notes.txt & rm -rf src",
    "cat <(rm -rf build)",
    "sort -o important.txt junk.txt",
    "sort -u -o list.txt list.txt",
    "sort -uo list.txt list.txt",
    "sort --output=list.txt list.txt",
    "uniq in.txt out.txt",
    "uniq -c -- in.txt out.txt",
    "sed 's/a/b/w out.txt' f",
    "sed '1,5w out.txt' f",
    "sed 's/a/b/e' f",
    "find . -name '*.log' -fls out.txt",
    "find . -name '*.log' -fprint0 out.txt",
    "tree -o tree.txt",
    "tree -R -H . .",
]

READS = [
    "sort data.txt | uniq -c",
    "grep -h import *.py | sort | uniq -c | sort -rn | head",
    "sort -k2 -t, data.csv",
    "sort -S 1G -T /tmp big.txt",
    "uniq -c in.txt",
    "uniq -f 2 -s 3 in.txt",
    "sed -n '1,40p' app.py",
    "sed -e 's/old/new/' notes.txt",
    "sed -n '/^def /p' app.py",
    "sed -n '/the new rule/p' notes.txt",
    "find . -name '*.py' -newer setup.py",
    "tree -L 2 -a",
    "cat a.py 2>&1 | grep foo",
    "ls -la &> /dev/null",
]


@pytest.mark.parametrize("cmd", WRITES)
def test_a_command_that_writes_is_risky(cmd):
    assert verify.is_risky([_bash(cmd)], "hard") is True


@pytest.mark.parametrize("cmd", READS)
def test_a_command_that_only_reads_stays_safe(cmd):
    assert verify.is_risky([_bash(cmd)], "hard") is False


# --------------------------------------------------------------------------- #
# 3. plan_check keeps its "replan" findings
# --------------------------------------------------------------------------- #

GOAL = "Deliver: a billing webhook handler, an audit log exporter, and a rate limiter."


def _one_file_plan(n):
    return [{"title": "Phase %d" % i, "task": "work on server part %d" % i,
             "files": ["server/app.py"], "done_when": "tests pass for part %d" % i}
            for i in range(1, n + 1)]


def test_the_findings_cap_keeps_every_replan_finding():
    _fixed, report = plan_check.check_plan(_one_file_plan(9), {}, GOAL, None)
    kinds = [f["kind"] for f in report["findings"]]
    assert len(kinds) == 30
    assert kinds.count("uncovered_part") == 3
    assert kinds[:27] == ["file_conflict"] * 27, "the order is kept; the extra fixes go"


def test_the_cap_is_unchanged_for_a_short_list():
    findings = [{"action": "warn", "text": str(i)} for i in range(5)]
    assert plan_check._cap_findings(findings) == findings
    many = [{"action": "fixed", "text": str(i)} for i in range(40)]
    assert plan_check._cap_findings(many) == many[:30]


def test_a_big_plan_still_earns_its_planner_re_ask(tmp_path):
    asks = []

    def planner(system, user):
        asks.append(user)
        return ""                       # an unusable answer: the run goes ahead anyway

    _phases, _design, report = SW.dry_run(GOAL, _one_file_plan(9), {}, str(tmp_path),
                                          planner=planner)
    assert len(asks) == 1, "the planner is asked once for the parts no phase covers"
    assert 'no phase covers "a rate limiter"' in asks[0]
    assert report["replanned"] is True
    assert sum(1 for f in report["findings"] if f["kind"] == "uncovered_part") == 3
