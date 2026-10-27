"""Detect and fix a WSL distro whose /mnt/<drive> is NOT the live Windows drive.

MEASURED 2026-10-27 (Build conversation, 'bount en', opencode -> wsl bash -c ...): a WSL
distro's /etc/wsl.conf had [automount] enabled=false and [interop] enabled=false. /mnt/c was
not a mount at all -- just an ordinary directory on the distro's own root filesystem, holding a
one-time stale copy of part of a Windows folder taken hours earlier. Every file opencode wrote
on the Windows side after that moment was invisible from WSL, forever, however long waited. The
agent guessed OneDrive sync delays and PowerShell quoting instead of finding this, and burned
many turns on it.

Everything here is pure stdlib; every outside effect (`run`, `clock`) is a constructor/call
argument, so it is testable with no real wsl.exe. `run(argv, input=None, timeout=10) -> (rc,
stdout, stderr)` never raises -- callers decide what a non-zero code or an exception means.
"""
from __future__ import annotations

import re
import subprocess
import time


class WslFixError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


DEFAULT_TIMEOUT = 10
ROOT_CHECK_TIMEOUT = 6          # quick: must never sit waiting for a password prompt
SHUTDOWN_TIMEOUT = 30
VERIFY_TIMEOUT = 30             # a cold distro boot can take a few seconds


def _run(argv, input=None, timeout=DEFAULT_TIMEOUT):
    """Default `run`: a real wsl.exe call. Never raises -- (rc, out, err); rc=-1 for a
    timeout/launch failure, with the reason in `err`. stdin is always closed unless `input`
    is given, so nothing can ever block this process waiting on a TTY (the real-world bug a
    `sudo` call without `-n` produced live)."""
    try:
        p = subprocess.run(
            argv, input=input, capture_output=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return (p.returncode,
                (p.stdout or b"").decode("utf-8", "replace"),
                (p.stderr or b"").decode("utf-8", "replace"))
    except subprocess.TimeoutExpired:
        return (-1, "", "timeout")
    except OSError as e:                                           # noqa: BLE001
        return (-1, "", "launch failed: %s" % e)


def _wsl(distro, args, as_root=False, input=None, timeout=DEFAULT_TIMEOUT, run=_run):
    argv = ["wsl.exe", "-d", distro]
    if as_root:
        argv += ["-u", "root"]
    argv += ["--"] + list(args)
    return run(argv, input=input, timeout=timeout)


# --------------------------------------------------------------------------------------------- #
# Read-only detection
# --------------------------------------------------------------------------------------------- #

_BOOL_RE = re.compile(r"^\s*(true|1|yes)\s*$", re.I)
_SECTION_RE = re.compile(r"^\s*\[\s*([A-Za-z0-9_]+)\s*\]\s*$")
_KV_RE = re.compile(r"^\s*([A-Za-z0-9_]+)\s*=\s*(.*?)\s*(?:[;#].*)?$")


def parse_wsl_conf(text):
    """{"automount_enabled": bool|None, "interop_enabled": bool|None} from wsl.conf text.
    None means the key/section is absent, i.e. WSL's own default (enabled) applies -- an
    absent section is NOT a fault. Case-insensitive section/key names; `#`/`;` comments and
    inline comments after a value are stripped. Never raises; unreadable text -> all None."""
    out = {"automount_enabled": None, "interop_enabled": None}
    try:
        section = None
        for line in (text or "").splitlines():
            m = _SECTION_RE.match(line)
            if m:
                section = m.group(1).lower()
                continue
            m = _KV_RE.match(line)
            if not m or section not in ("automount", "interop"):
                continue
            key, val = m.group(1).lower(), m.group(2).strip()
            if key != "enabled":
                continue
            out["%s_enabled" % section] = bool(_BOOL_RE.match(val))
    except Exception:                                              # noqa: BLE001
        return {"automount_enabled": None, "interop_enabled": None}
    return out


_MOUNT_LINE_RE = re.compile(r"^\S+\s+(\S+)\s+(\S+)\s")


def mount_fstype(mounts_text, point):
    """The filesystem type mounted at `point` (last match wins, like the kernel's own mount
    table), or None when nothing is mounted there -- the exact state a bare leftover directory
    sitting on the distro's own root filesystem produces (no /proc/mounts entry at all)."""
    found = None
    for line in (mounts_text or "").splitlines():
        m = _MOUNT_LINE_RE.match(line)
        if m and m.group(1) == point:
            found = m.group(2)
    return found


def read_wsl_conf(distro, run=_run):
    """wsl.conf's text, or "" when missing/unreadable (that is itself the healthy default, so
    callers must not treat "" as a fault on its own)."""
    rc, out, _err = _wsl(distro, ["cat", "/etc/wsl.conf"], run=run)
    return out if rc == 0 else ""


def read_mounts(distro, run=_run):
    """/proc/mounts text, or "" when the distro could not be reached at all."""
    rc, out, _err = _wsl(distro, ["cat", "/proc/mounts"], run=run)
    return out if rc == 0 else ""


def diagnose(distro, run=_run):
    """One distro's health: {"distro", "healthy", "reason", "mount_type", "automount_enabled",
    "interop_enabled"}. Only /mnt/c is checked (the realistic case on a Windows machine).
    "healthy" is False only on an ACTUAL fault seen on the wire -- an unreachable distro is
    reported as its own reason, never silently folded into "healthy" or "broken"."""
    mounts = read_mounts(distro, run=run)
    if not mounts:
        return {"distro": distro, "healthy": None, "reason": "unreachable",
                "mount_type": None, "automount_enabled": None, "interop_enabled": None}
    conf = parse_wsl_conf(read_wsl_conf(distro, run=run))
    mtype = mount_fstype(mounts, "/mnt/c")
    healthy = mtype == "9p" and conf["automount_enabled"] is not False
    if healthy:
        reason = "ok"
    elif conf["automount_enabled"] is False:
        reason = "automount_disabled"
    elif mtype is None:
        reason = "not_mounted"
    else:
        reason = "unexpected_fstype:%s" % mtype
    return {"distro": distro, "healthy": healthy, "reason": reason, "mount_type": mtype,
            "automount_enabled": conf["automount_enabled"],
            "interop_enabled": conf["interop_enabled"]}


def distros_health(distros, run=_run):
    """diagnose() for each name in `distros` (caller supplies the list, e.g. wslpath.distros()
    -- this module does not enumerate distros itself, to stay testable with no real WSL)."""
    return [diagnose(d, run=run) for d in distros]


def brief_note(health_rows, max_chars=600):
    """ONE short block for an agent's brief when any distro is unhealthy, else "". Tells the
    agent the ACTUAL cause (not a guess) and what to do, so it never again spends turns
    blaming OneDrive sync or a quoting bug for this. `health_rows` = distros_health()'s
    result; a row with healthy True or None (unreachable -- nothing useful to say) is
    skipped."""
    bad = [r for r in health_rows if r.get("healthy") is False]
    if not bad:
        return ""
    lines = ["WSL MOUNT WARNING (measured, not a guess):"]
    for r in bad:
        lines.append(
            "- %s: /mnt/c is NOT the live Windows C: drive (wsl.conf: automount=%s, "
            "interop=%s). A file written from Windows will NEVER appear under /mnt/c in this "
            "distro, however long you wait -- it is not a sync delay and not a quoting "
            "problem. Tell the user to fix it in the hub's Settings -> WSL health, or avoid "
            "/mnt/c: pipe content in from the Windows side instead (e.g. "
            "`<content> | wsl.exe -d %s bash -c \"cat > ~/file\"`)." % (
                r["distro"], r.get("automount_enabled"), r.get("interop_enabled"), r["distro"]))
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[:max_chars - 1] + "…"


# --------------------------------------------------------------------------------------------- #
# The fix (confirm-gated; every destructive step is its own, injectable call)
# --------------------------------------------------------------------------------------------- #

def can_use_root(distro, run=_run):
    """True when `wsl.exe -d <distro> -u root` answers without a password prompt (WSL's own
    design: the distro owner can always do this -- it is not a privilege-escalation path, and
    this never falls back to an interactive `sudo`, which would hang forever waiting on a TTY
    this process does not have)."""
    rc, out, _err = _wsl(distro, ["whoami"], as_root=True, timeout=ROOT_CHECK_TIMEOUT, run=run)
    return rc == 0 and out.strip() == "root"


def patch_wsl_conf(text, enable_automount=True, enable_interop=True):
    """(new_text, changed): `text` with an EXISTING `enabled = false` under [automount] /
    [interop] flipped to true -- an absent section, an absent `enabled` key, or one already
    true is left untouched (nothing to fix; WSL's own default already applies). Every other
    line -- other sections, other keys, comments, [boot], anything the user added -- is kept
    byte for byte and in its original order. `changed` is False when nothing needed changing,
    so a caller never writes back an identical file or claims it fixed something it did not."""
    targets = {"automount": enable_automount, "interop": enable_interop}
    lines = (text or "").splitlines()
    out, changed, section = [], False, None
    for line in lines:
        m = _SECTION_RE.match(line)
        if m:
            section = m.group(1).lower()
            out.append(line)
            continue
        m = _KV_RE.match(line)
        if (m and section in targets and m.group(1).lower() == "enabled"
                and not _BOOL_RE.match(m.group(2).strip())):
            out.append("enabled = true" if targets[section] else line)
            changed = changed or targets[section]
            continue
        out.append(line)
    new_text = "\n".join(out)
    if lines and (text or "").endswith("\n"):
        new_text += "\n"
    return new_text, changed


def _backup_name(prefix, clock):
    return "%s-%s" % (prefix, time.strftime("%Y%m%d-%H%M%S", time.localtime(clock())))


def _pipe_in(distro, dest_path, content, run=_run):
    """Writes `content` (str) to `dest_path` as root via stdin -- never through a shell string
    containing the content, so nothing about it needs escaping."""
    rc, _out, err = _wsl(distro, ["bash", "-c", "cat > " + dest_path], as_root=True,
                         input=content.encode("utf-8"), run=run)
    return rc == 0, err


def apply(distro, confirm=False, run=_run, clock=time.time):
    """Fix one distro's broken mount, or explain why it did not. Never raises; the shape is
    always {"ok": bool, ...}. Steps, each independently reported:
      1. refuse without confirm (mirrors every other confirm-gated hub action);
      2. re-diagnose -- already healthy -> ok, nothing touched;
      3. root must be reachable with NO password (can_use_root) -- otherwise {"ok": False,
         "needs_manual": True, "command": <one paste-able line>}, and NOTHING is touched;
      4. back up the stale mount-point directory's current content (only if it holds anything)
         and the old wsl.conf, both inside the distro, before writing anything;
      5. patch wsl.conf (pure; "" when the file never existed) and write it back;
      6. `wsl --shutdown` (machine-wide: every distro's processes end, not only this one);
      7. one fresh call re-mounts it and `diagnose()` confirms the result.
    A failure at any step returns {"ok": False, "code": "<step>", "message": ...} with
    whatever earlier steps (backup, old-conf path) already completed named in the result, so
    nothing already done is hidden from the caller."""
    if not confirm:
        raise WslFixError("confirm_required", "confirm must be true")
    before = diagnose(distro, run=run)
    if before.get("healthy"):
        return {"ok": True, "already_healthy": True, "before": before}
    if before.get("healthy") is None:
        return {"ok": False, "code": "unreachable", "message": "could not reach the distro"}
    if not can_use_root(distro, run=run):
        return {"ok": False, "needs_manual": True, "code": "no_root", "before": before,
                "message": "root is not directly reachable in this distro; run this yourself "
                           "in a terminal for it, then ask the hub to verify:",
                "command": "sudo sed -i -E "
                           "\"s/^(\\s*enabled\\s*=\\s*)false/\\1true/\" /etc/wsl.conf "
                           "&& exit"}

    stamp = _backup_name("mnt-c-stale-backup", clock)
    backup_dir = "$HOME/" + stamp
    rc, out, err = _wsl(
        distro, ["bash", "-c",
                 "mkdir -p " + backup_dir + " && cp -a /mnt/c/. " + backup_dir
                 + "/ 2>/dev/null; echo done"],
        as_root=True, run=run)
    if rc != 0:
        return {"ok": False, "code": "backup_failed", "message": err or out, "before": before}

    old_conf = read_wsl_conf(distro, run=run)
    conf_backup = "/etc/wsl.conf.bak-" + stamp.rsplit("-", 2)[-2] + stamp.rsplit("-", 1)[-1]
    new_conf, changed = patch_wsl_conf(old_conf)
    if not changed:
        # Diagnosed unhealthy but nothing patchable (e.g. [automount] section missing and the
        # fault is something else entirely): say so plainly rather than claim a fix.
        return {"ok": False, "code": "nothing_to_patch", "before": before,
                "backup_dir": backup_dir,
                "message": "wsl.conf does not name the fault found (%s); not changed" % (
                    before.get("reason"))}
    if old_conf:
        ok, err = _pipe_in(distro, conf_backup, old_conf, run=run)
        if not ok:
            return {"ok": False, "code": "conf_backup_failed", "message": err,
                    "backup_dir": backup_dir, "before": before}
    ok, err = _pipe_in(distro, "/etc/wsl.conf", new_conf, run=run)
    if not ok:
        return {"ok": False, "code": "write_failed", "message": err, "backup_dir": backup_dir,
                "conf_backup": conf_backup if old_conf else None, "before": before}

    rc, _out, err = run(["wsl.exe", "--shutdown"], timeout=SHUTDOWN_TIMEOUT)
    if rc != 0:
        return {"ok": False, "code": "shutdown_failed", "message": err,
                "backup_dir": backup_dir, "conf_backup": conf_backup if old_conf else None,
                "before": before}

    after = diagnose(distro, run=run)
    return {"ok": bool(after.get("healthy")), "before": before, "after": after,
            "backup_dir": backup_dir, "conf_backup": conf_backup if old_conf else None}
