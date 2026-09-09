#!/usr/bin/env python3
"""calvoun swarm - drive the multi-agent orchestrator from a terminal.

WHY THIS EXISTS ALONGSIDE THE MCP TOOLS
---------------------------------------
MCP is the surface for a MODEL: the hub already writes an [mcp_servers.*] entry
into opencode, codex, claude and kimi, so swarm_windows_start shows up in their
tool lists with nothing to install and no knowledge that a binary exists.

This is the surface for a PERSON. Watching a run, stopping one, or kicking one
off from a shell is a worse fit for a tool call than for a command, and it works
when a CLI has no MCP wiring at all.

Both talk to the same HTTP endpoints, so neither can drift from the other.

    swarm start "build a landing page" --dir ./site
    swarm watch  swarm-a1b2c3d4
    swarm status swarm-a1b2c3d4 --events
    swarm stop   swarm-a1b2c3d4
    swarm list

Pure stdlib: this has to run from a bare python with no venv.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BASE = os.environ.get("FREE_LLM_HUB_URL", "http://127.0.0.1:8787")


def _token():
    """The hub's control token, from the env or its own config file.

    Read here rather than asked for: the whole point of a local tool is that it
    already has the same access the dashboard does."""
    env = os.environ.get("FREE_LLM_HUB_TOKEN")
    if env:
        return env
    path = os.environ.get("FREE_LLM_HUB_CONFIG") or os.path.join(
        os.path.expanduser("~"), ".free-llm-hub", "config.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return (json.load(fh) or {}).get("control_token") or ""
    except Exception:                                            # noqa: BLE001
        return ""


def _call(base, method, path, body=None):
    url = base.rstrip("/") + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Free-LLM-Hub-Token", _token())
    req.add_header("X-Free-LLM-Hub", "dashboard")   # the anti-CSRF header
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            msg = json.loads(raw)["error"]["message"]
        except Exception:                                        # noqa: BLE001
            msg = raw[:300] or str(exc)
        raise SystemExit("hub said: " + msg)
    except urllib.error.URLError as exc:
        raise SystemExit("cannot reach the hub at %s (%s). Is it running?"
                         % (base, exc.reason))


_MARK = {"done": "ok  ", "failed": "FAIL", "running": " .. ",
         "pending": "    ", "stopped": "stop"}


def _print_run(run, events=False):
    print("%s  %s  (%d/%d done, %d failed)"
          % (run["run_id"], run["state"], run.get("done", 0),
             run.get("total", 0), run.get("failed", 0)))
    print("goal: " + run.get("goal", ""))
    for a in run.get("agents", []):
        needs = (" needs %s" % ",".join(str(n) for n in a["needs"])) if a["needs"] else ""
        print("  [%s] %d. %-28s %s%s"
              % (_MARK.get(a["state"], a["state"]), a["index"],
                 a["title"][:28], a["session_id"] or "-", needs))
        if a.get("error"):
            print("        ! " + str(a["error"])[:150])
        if a.get("summary"):
            first = a["summary"].strip().splitlines()[0] if a["summary"].strip() else ""
            print("        " + first[:150])
        if events:
            for ev in a.get("log", [])[-12:]:
                text = (ev.get("text") or ev.get("error") or "")
                if text:
                    print("        | %-7s %s" % (ev.get("type", "?"), str(text)[:120]))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="swarm", description=__doc__.split("\n")[0])
    ap.add_argument("--base", default=DEFAULT_BASE, help="hub URL")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start", help="start a run")
    s.add_argument("goal", help="what the swarm should achieve")
    s.add_argument("--dir", default=".", help="project folder the agents work in")
    s.add_argument("--cli", default="opencode", help="which CLI each agent runs")
    s.add_argument("--watch", action="store_true", help="follow it until it ends")

    st = sub.add_parser("status", help="show one run")
    st.add_argument("run_id")
    st.add_argument("--events", action="store_true", help="include each agent's log")

    w = sub.add_parser("watch", help="follow a run until it ends")
    w.add_argument("run_id")
    w.add_argument("--interval", type=float, default=3.0)

    sp = sub.add_parser("stop", help="stop a run and its agents")
    sp.add_argument("run_id")

    sub.add_parser("list", help="every run this hub remembers")

    args = ap.parse_args(argv)

    if args.cmd == "start":
        run = _call(args.base, "POST", "/api/swarm-windows", {
            "goal": args.goal,
            "project_dir": os.path.abspath(args.dir),
            "cli": args.cli,
        })
        _print_run(run)
        if args.watch:
            return _watch(args.base, run["run_id"], 3.0)
        print("\nfollow it with:  swarm watch %s" % run["run_id"])
        return 0

    if args.cmd == "status":
        path = "/api/swarm-windows/%s%s" % (args.run_id, "?events=1" if args.events else "")
        _print_run(_call(args.base, "GET", path), events=args.events)
        return 0

    if args.cmd == "watch":
        return _watch(args.base, args.run_id, args.interval)

    if args.cmd == "stop":
        _print_run(_call(args.base, "DELETE", "/api/swarm-windows/" + args.run_id))
        return 0

    runs = _call(args.base, "GET", "/api/swarm-windows").get("runs", [])
    if not runs:
        print("no runs yet")
        return 0
    for r in runs:
        print("%s  %-8s %d/%d  %s"
              % (r["run_id"], r["state"], r.get("done", 0), r.get("total", 0),
                 r.get("goal", "")[:60]))
    return 0


def _watch(base, run_id, interval):
    """Poll until the run ends. Reprints only when something changed, so a long
    build does not scroll the same table past you every few seconds."""
    last = None
    while True:
        run = _call(base, "GET", "/api/swarm-windows/" + run_id)
        sig = json.dumps([run["state"]] + [a["state"] for a in run["agents"]])
        if sig != last:
            print("")
            _print_run(run)
            last = sig
        if run["state"] in ("done", "failed", "stopped"):
            return 0 if run["state"] == "done" else 1
        time.sleep(max(0.5, interval))


if __name__ == "__main__":
    sys.exit(main())
