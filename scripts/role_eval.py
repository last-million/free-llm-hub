"""Offline comparison: the old tool-turn RACE vs ROLES. No model calls.

    python scripts/role_eval.py                      # the hub's own state dir
    python scripts/role_eval.py --log hub.log --roles turn-roles.jsonl --json

RACE baseline, from hub.log's `[swarm-tools]` lines (the race logs one per
turn): "N/M models answered, K used a tool -> pid/model" (served) and
"0/M models answered in Ss -> ..." (nothing served). M = member calls.

ROLES, from turn-roles.jsonl (app._role_log, one row per role turn; credit
rows are counted separately): calls/turn, input tokens sent/turn, p50/p90
latency, zero-answer rate, invalid tool calls, backups, verifier verdicts and
corrections.
"""
import argparse
import glob
import json
import os
import re
import sys

_RACE_SERVED_RE = re.compile(
    r"\[swarm-tools\] (\d+)/(\d+) models answered, (\d+) used a tool -> (\S+)")
_RACE_EMPTY_RE = re.compile(r"\[swarm-tools\] 0/(\d+) models answered in (\d+)s")


def _default_state_dir():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        import config                                       # noqa: WPS433
        return config.state_dir()
    except Exception:                                       # noqa: BLE001
        return os.path.join(os.path.expanduser("~"), ".free-llm-hub")


def _rotated(path):
    """`path` plus its rotated siblings (path.1, path.2 ...), oldest first."""
    found = sorted(glob.glob(glob.escape(path) + ".*"),
                   key=lambda p: -int(p.rsplit(".", 1)[-1]) if p.rsplit(".", 1)[-1].isdigit()
                   else 0)
    return [p for p in found if p.rsplit(".", 1)[-1].isdigit()] + (
        [path] if os.path.exists(path) else [])


def _pct(values, pct):
    """Nearest-rank percentile (as app._percentile), None when empty."""
    if not values:
        return None
    ordered = sorted(values)
    k = int(round((pct / 100.0) * len(ordered) + 0.5)) - 1
    return ordered[max(0, min(k, len(ordered) - 1))]


def race_stats(lines):
    turns = served = calls = acted = 0
    for line in lines:
        m = _RACE_SERVED_RE.search(line)
        if m:
            turns += 1
            served += 1
            calls += int(m.group(2))
            acted += 1 if int(m.group(3)) else 0
            continue
        m = _RACE_EMPTY_RE.search(line)
        if m:
            turns += 1
            calls += int(m.group(1))
    return {
        "turns": turns,
        "served": served,
        "member_calls": calls,
        "calls_per_turn": round(calls / turns, 2) if turns else None,
        "calls_per_served_answer": round(calls / served, 2) if served else None,
        # every member call that was not the served answer did no work the
        # CLI ever saw: abandoned, out-raced, or failed
        "calls_that_served_nothing": calls - served,
        "share_that_served_nothing": round((calls - served) / calls, 3) if calls else None,
        "zero_answer_rate": round((turns - served) / turns, 3) if turns else None,
    }


def roles_stats(rows):
    turns = [r for r in rows if r.get("event", "turn") == "turn" and r.get("turn") == "tool"]
    text = [r for r in rows if r.get("event", "turn") == "turn" and r.get("turn") == "text"]
    credits = [r for r in rows if r.get("event") == "credit"]
    n = len(turns)
    served = [r for r in turns if r.get("served")]
    calls = sum(int(r.get("calls") or 0) for r in turns)
    sent = [int(r.get("sent_tokens") or 0) for r in turns]
    lat = [float(r["latency_s"]) for r in turns if isinstance(r.get("latency_s"), (int, float))]
    verdicts = {}
    for r in turns + text:
        if r.get("verifier"):
            v = str(r.get("verdict") or "none").split(" ")[0]
            verdicts[v] = verdicts.get(v, 0) + 1
    corrected = sum(1 for r in turns + text if r.get("corrected"))
    rejected = sum(1 for r in turns + text if r.get("corrector") and not r.get("corrected"))
    revise_kept = sum(1 for r in turns + text
                      if r.get("verdict") == "revise" and not r.get("corrector"))
    return {
        "turns": n,
        "served": len(served),
        "calls": calls,
        "calls_per_turn": round(calls / n, 2) if n else None,
        "calls_per_served_answer": round(calls / len(served), 2) if served else None,
        "input_tokens_sent_per_turn": round(sum(sent) / n) if n else None,
        "latency_p50_s": _pct(lat, 50),
        "latency_p90_s": _pct(lat, 90),
        "zero_answer_rate": round((n - len(served)) / n, 3) if n else None,
        "invalid_tool_calls": sum(int(r.get("invalid") or 0) for r in turns),
        "backup_fired": sum(1 for r in turns if r.get("hedge")),
        "verifier_runs": sum(1 for r in turns + text if r.get("verifier")),
        "verdicts": verdicts,
        "verifier_fixes": corrected,                 # a correction shipped
        "corrections_rejected": rejected,            # corrector ran, its step was not usable
        "revise_shipped_as_is": revise_kept,         # low severity / no time / no corrector
        "team_turns": sum(1 for r in turns if r.get("specialists")),
        "team_specialist_calls": sum(len(r.get("specialists") or []) for r in turns),
        "team_brief_chars_avg": (round(sum(int(r.get("brief_chars") or 0)
                                           for r in turns if r.get("specialists"))
                                       / max(1, sum(1 for r in turns if r.get("specialists"))))
                                 if any(r.get("specialists") for r in turns) else None),
        "text_turns_reviewed": len(text),
        "next_turn_credits": sum(int(r.get("credited") or 0) for r in credits),
    }


def _read_lines(paths):
    for p in paths:
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    yield line
        except OSError:
            continue


def _read_rows(paths):
    rows = []
    for line in _read_lines(paths):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--log", help="hub.log (default: the state dir's, with rotations)")
    ap.add_argument("--roles", help="turn-roles.jsonl (default: the state dir's)")
    ap.add_argument("--json", action="store_true", help="print JSON")
    args = ap.parse_args(argv)
    state = None
    if not (args.log and args.roles):
        state = _default_state_dir()
    log_paths = _rotated(args.log or os.path.join(state, "hub.log"))
    role_paths = _rotated(args.roles or os.path.join(state, "turn-roles.jsonl"))
    out = {"race": race_stats(_read_lines(log_paths)),
           "roles": roles_stats(_read_rows(role_paths)),
           "sources": {"log": log_paths, "roles": role_paths}}
    if args.json:
        print(json.dumps(out, indent=2))
        return out
    for name in ("race", "roles"):
        print("== %s ==" % name)
        for k, v in out[name].items():
            print("  %-28s %s" % (k, v))
    print("sources: %d log file(s), %d roles file(s)" % (len(log_paths), len(role_paths)))
    return out


if __name__ == "__main__":
    main()
