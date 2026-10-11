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
import datetime
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


def parse_since(text):
    """A `--since` value -> a unix timestamp (float), or None. Accepts a unix
    timestamp, an ISO datetime/date (naive = UTC), or a relative age like
    `36h` / `7d` / `90m` / `2w` (before now). Returns None (no filtering) on
    anything unreadable."""
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    try:
        return float(s)                      # a bare unix timestamp
    except ValueError:
        pass
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([mhdw])", s, re.I)
    if m:
        n = float(m.group(1))
        unit = {"m": 60.0, "h": 3600.0, "d": 86400.0, "w": 604800.0}[m.group(2).lower()]
        return _now_ts() - n * unit
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _now_ts():
    return datetime.datetime.now(datetime.timezone.utc).timestamp()


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


def wasted_of(r):
    """Calls of one roles row that served nothing: the row's own `wasted_calls`,
    else (a row written before the field existed) its failed hops that really
    spent a call -- a hop skipped for a too-small window spent none."""
    if r.get("wasted_calls") is not None:
        return int(r.get("wasted_calls") or 0)
    return sum(1 for f in (r.get("failed") or [])
               if isinstance(f, dict) and "window too small" not in str(f.get("why")))


def roles_stats(rows):
    # ROLES turns only: a `single: true` row is team notes logged for a
    # single-model (Normal/Max) turn, not a roles-pipeline turn (no actor, no
    # verdict, served=None), so counting it would inflate turns and no-answer.
    # Old rows have no `single` field and are kept.
    turns = [r for r in rows if r.get("event", "turn") == "turn" and r.get("turn") == "tool"
             and not r.get("single")]
    single = [r for r in rows if r.get("event", "turn") == "turn" and r.get("turn") == "tool"
              and r.get("single")]
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
    wasted = sum(wasted_of(r) for r in turns)
    return {
        "turns": n,
        "single_team_turns": len(single),           # team notes on single-model turns
        "served": len(served),
        "calls": calls,
        "calls_per_turn": round(calls / n, 2) if n else None,
        "wasted_calls": wasted,
        "wasted_calls_pct": round(100.0 * wasted / calls, 1) if calls else None,
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
        "verifier_unparsed": sum(int(r.get("verifier_unparsed") or 0) for r in turns + text),
        "verifier_retry": sum(int(r.get("verifier_retry") or 0) for r in turns + text),
        "verifier_usable_rate": (round(sum(1 for r in turns + text if r.get("verifier")
                                           and str(r.get("verdict") or "").split(" ")[0]
                                           in ("ok", "revise"))
                                       / max(1, sum(1 for r in turns + text if r.get("verifier"))), 3)
                                 if any(r.get("verifier") for r in turns + text) else None),
        "team_medium_turns": sum(1 for r in turns if r.get("team_tier") == "medium"),
        "text_turns_reviewed": len(text),
        "next_turn_credits": sum(int(r.get("credited") or 0) for r in credits),
    }


def _hop_cause(why):
    """One failed-hop `why` string -> a coarse cause class, for --why."""
    w = str(why or "").lower()
    if "window too small" in w or "schema" in w and "exceed" in w or "too small for" in w:
        return "window"
    if "no answer in time" in w or "deadline" in w or "hop budget" in w or "no content" in w:
        return "deadline"
    if "429" in w:
        return "429"
    if re.search(r"\bhttp 5\d\d\b", w) or re.search(r"\b5\d\d\b", w):
        return "5xx"
    if re.search(r"\bhttp 4\d\d\b", w) or re.search(r"\b4\d\d\b", w):
        return "http4xx"
    if "empty" in w or "junk" in w:
        return "empty/junk"
    if "invalid" in w or "prose" in w or "cannot run" in w or "refus" in w or "announc" in w:
        return "invalid/prose"
    if "dns" in w or "resolve" in w or "network" in w:
        return "dns"
    if "conn" in w or "exception" in w or "reset" in w or "refused" in w:
        return "exc"
    return "other"


def why_breakdown(rows):
    """For --why: classify the no-answer ROLES turns by cause. Returns a dict
    (hop classes, first-hop class, turn-level distinct-cause sets, how many
    spent no call, backup/verifier counts, calls distribution)."""
    turns = [r for r in rows if r.get("event", "turn") == "turn" and r.get("turn") == "tool"
             and not r.get("single")]
    noans = [r for r in turns if not r.get("served")]
    hop = {}
    first = {}
    sets = {}
    calls_hist = {}
    zero_call = backup = verifier = 0
    for r in noans:
        failed = r.get("failed") or []
        c = int(r.get("calls") or 0)
        calls_hist[c] = calls_hist.get(c, 0) + 1
        if c == 0:
            zero_call += 1
        if r.get("hedge"):
            backup += 1
        if r.get("verifier"):
            verifier += 1
        whys = [_hop_cause(f.get("why")) if isinstance(f, dict) else _hop_cause(f)
                for f in failed]
        for w in whys:
            hop[w] = hop.get(w, 0) + 1
        first[whys[0] if whys else "no-failed-hop"] = \
            first.get(whys[0] if whys else "no-failed-hop", 0) + 1
        key = tuple(sorted(set(whys))) if whys else ("no-failed-hop",)
        sets[key] = sets.get(key, 0) + 1
    return {
        "no_answer_turns": len(noans),
        "of_turns": len(turns),
        "hop_classes": dict(sorted(hop.items(), key=lambda kv: -kv[1])),
        "first_hop_class": dict(sorted(first.items(), key=lambda kv: -kv[1])),
        "turn_cause_sets": dict(sorted(sets.items(), key=lambda kv: -kv[1])),
        "spent_no_call": zero_call,
        "backup_fired": backup,
        "verifier_ran": verifier,
        "calls_distribution": dict(sorted(calls_hist.items())),
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
    ap.add_argument("--since", help="only roles rows at/after this time: a unix "
                    "timestamp, an ISO datetime/date (UTC), or a relative age "
                    "(36h, 7d, 90m, 2w). Rows with no ts are dropped when set.")
    ap.add_argument("--json", action="store_true", help="print JSON")
    ap.add_argument("--why", action="store_true",
                    help="also break the no-answer ROLES turns down by cause "
                    "(per-hop class, first-hop class, turn-level cause sets). "
                    "Without it the output is unchanged.")
    args = ap.parse_args(argv)
    state = None
    if not (args.log and args.roles):
        state = _default_state_dir()
    log_paths = _rotated(args.log or os.path.join(state, "hub.log"))
    role_paths = _rotated(args.roles or os.path.join(state, "turn-roles.jsonl"))
    rows = _read_rows(role_paths)
    since = parse_since(args.since)
    if since is not None:
        # Only rows timestamped at/after the cutoff (old rows that predate the
        # `ts` field cannot be placed in time, so they are dropped here).
        rows = [r for r in rows if isinstance(r.get("ts"), (int, float)) and r["ts"] >= since]
    out = {"race": race_stats(_read_lines(log_paths)),
           "roles": roles_stats(rows),
           "since": since,
           "sources": {"log": log_paths, "roles": role_paths}}
    if args.why:
        out["why"] = why_breakdown(rows)
    if args.json:
        if "why" in out:
            # tuple keys are not JSON; render the cause sets as " + "-joined names
            out["why"]["turn_cause_sets"] = {
                " + ".join(k): v for k, v in out["why"]["turn_cause_sets"].items()}
        print(json.dumps(out, indent=2))
        return out
    for name in ("race", "roles"):
        print("== %s ==" % name)
        for k, v in out[name].items():
            print("  %-28s %s" % (k, v))
    r = out["roles"]
    if since is not None:
        print("since: %s" % datetime.datetime.fromtimestamp(
            since, datetime.timezone.utc).isoformat())
    if r.get("turns"):
        # The five numbers the owner compares before/after a change.
        print("summary: %s roles turns, %s no-answer rate, %s calls/turn, "
              "%s%% calls wasted, %s verifier usable rate" % (
                  r.get("turns"), r.get("zero_answer_rate"), r.get("calls_per_turn"),
                  r.get("wasted_calls_pct"), r.get("verifier_usable_rate")))
    if "why" in out:
        w = out["why"]
        print("== why (no-answer roles turns) ==")
        print("  %-28s %s of %s" % ("no_answer_turns", w["no_answer_turns"], w["of_turns"]))
        print("  %-28s %s" % ("spent_no_call", w["spent_no_call"]))
        print("  %-28s %s" % ("backup_fired", w["backup_fired"]))
        print("  %-28s %s" % ("verifier_ran", w["verifier_ran"]))
        print("  %-28s %s" % ("calls_distribution", w["calls_distribution"]))
        print("  per-hop failure classes:")
        for c, n in w["hop_classes"].items():
            print("    %4d  %s" % (n, c))
        print("  first-hop class per turn:")
        for c, n in w["first_hop_class"].items():
            print("    %4d  %s" % (n, c))
        print("  turn-level distinct-cause sets:")
        for c, n in list(w["turn_cause_sets"].items())[:12]:
            print("    %4d  %s" % (n, " + ".join(c)))
    print("sources: %d log file(s), %d roles file(s)" % (len(log_paths), len(role_paths)))
    return out


if __name__ == "__main__":
    main()
