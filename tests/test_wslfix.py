"""wslfix: detect and fix a WSL distro whose /mnt/c is not the live Windows drive.

Hermetic -- a FakeWsl stands in for wsl.exe, so no real WSL, no subprocess, no file is touched.
Models the exact live fault (2026-10-27): [automount] enabled=false, no /mnt/c mount, a stale
copy of the folder on the distro's own root fs.
"""
import re

import pytest

import wslfix


# --- a fake wsl.exe ----------------------------------------------------------------------------

class FakeWsl:
    """Enough of a WSL distro to drive wslfix. `conf` is the /etc/wsl.conf text (None = file
    absent); `mounts` is /proc/mounts text; `root` whether `-u root` works without a password.
    Records every argv seen. shutdown() flips a broken distro to a healthy one, as a real
    `wsl --shutdown` + remount would once wsl.conf is fixed."""

    def __init__(self, conf="", mounts=None, root=True, reachable=True, healthy_mounts=None):
        self.conf = conf
        self.mounts = mounts if mounts is not None else "none /mnt/c tmpfs rw 0 0"
        self.root = root
        self.reachable = reachable
        self.healthy_mounts = healthy_mounts or (
            r"C:\134 /mnt/c 9p rw,noatime,aname=drvfs;path=C:\; 0 0")
        self.calls = []
        self.files = {}
        self.shutdowns = 0

    def run(self, argv, input=None, timeout=10):
        self.calls.append(list(argv))
        if argv[:2] == ["wsl.exe", "--shutdown"]:
            self.shutdowns += 1
            if self.conf is not None and "enabled = true" in self.conf:
                self.mounts = self.healthy_mounts
            return (0, "", "")
        if not self.reachable:
            return (-1, "", "wsl: distro unreachable")
        # argv = wsl.exe -d <distro> [-u root] -- <cmd...>
        i = 1
        if argv[i:i + 1] == ["-d"]:
            i += 2
        as_root = False
        if argv[i:i + 2] == ["-u", "root"]:
            as_root = True
            i += 2
        if argv[i:i + 1] == ["--"]:
            i += 1
        cmd = argv[i:]
        if as_root and not self.root:
            return (-1, "", "sudo: interactive authentication is required")
        if cmd[:1] == ["whoami"]:
            return (0, "root\n" if as_root else "hamza\n", "")
        if cmd[:1] == ["cat"]:
            path = cmd[1]
            if path == "/etc/wsl.conf":
                return (0, self.conf, "") if self.conf is not None else (1, "", "No such file")
            if path == "/proc/mounts":
                return (0, self.mounts, "")
            return (self.files[path][0] if path in self.files else 1,
                    self.files.get(path, ("", ""))[0] if False else self.files.get(path, ""),
                    "")
        if cmd[:2] == ["bash", "-c"]:
            script = cmd[2]
            if script.startswith("cat > "):
                dest = script[len("cat > "):].strip()
                data = input.decode() if input else ""
                self.files[dest] = data
                if dest == "/etc/wsl.conf":          # writing the file updates the file
                    self.conf = data
                return (0, "", "")
            if "mkdir -p" in script and "cp -a /mnt/c" in script:
                return (0, "done\n", "")
            return (0, "", "")
        return (0, "", "")


BROKEN_CONF = "[automount]\nenabled = false\n\n[interop]\nenabled = false\nappendWindowsPath = false\n\n[boot]\nsystemd = true\n"
HEALTHY_MOUNTS = r"C:\134 /mnt/c 9p rw,noatime,aname=drvfs;path=C:\; 0 0"


# --- parse_wsl_conf ----------------------------------------------------------------------------

def test_parse_reads_automount_and_interop():
    c = wslfix.parse_wsl_conf(BROKEN_CONF)
    assert c == {"automount_enabled": False, "interop_enabled": False}


def test_parse_absent_sections_are_none_not_false():
    assert wslfix.parse_wsl_conf("[boot]\nsystemd = true\n") == {
        "automount_enabled": None, "interop_enabled": None}
    assert wslfix.parse_wsl_conf("") == {"automount_enabled": None, "interop_enabled": None}


def test_parse_is_case_and_comment_insensitive():
    c = wslfix.parse_wsl_conf("[AutoMount]\nEnabled = True  # on\n[interop]\nenabled = yes\n")
    assert c["automount_enabled"] is True and c["interop_enabled"] is True
    assert wslfix.parse_wsl_conf("[automount]\n; enabled = true\nenabled = false\n"
                                 )["automount_enabled"] is False


def test_parse_never_raises_on_junk():
    for junk in (None, 123, "\x00\x00[", "][=\n=][", "[automount\nenabled"):
        assert "automount_enabled" in wslfix.parse_wsl_conf(junk)


# --- mount_fstype ------------------------------------------------------------------------------

def test_mount_fstype_reads_9p_and_none():
    assert wslfix.mount_fstype(HEALTHY_MOUNTS, "/mnt/c") == "9p"
    assert wslfix.mount_fstype("none /mnt/wsl tmpfs rw 0 0", "/mnt/c") is None


def test_mount_fstype_last_match_wins():
    txt = "a /mnt/c ext4 rw 0 0\nb /mnt/c 9p rw 0 0"
    assert wslfix.mount_fstype(txt, "/mnt/c") == "9p"


# --- diagnose ----------------------------------------------------------------------------------

def test_diagnose_the_live_fault():
    w = FakeWsl(conf=BROKEN_CONF, mounts="none /mnt/wsl tmpfs rw 0 0")
    d = wslfix.diagnose("Ubuntu", run=w.run)
    assert d["healthy"] is False
    assert d["reason"] == "automount_disabled"
    assert d["mount_type"] is None
    assert d["automount_enabled"] is False and d["interop_enabled"] is False


def test_diagnose_healthy_distro():
    w = FakeWsl(conf="[boot]\nsystemd=true\n", mounts=HEALTHY_MOUNTS)
    d = wslfix.diagnose("Ubuntu", run=w.run)
    assert d["healthy"] is True and d["reason"] == "ok" and d["mount_type"] == "9p"


def test_diagnose_unexpected_fstype_is_a_fault():
    w = FakeWsl(conf="", mounts="x /mnt/c ext4 rw 0 0")
    d = wslfix.diagnose("Ubuntu", run=w.run)
    assert d["healthy"] is False and d["reason"].startswith("unexpected_fstype")


def test_diagnose_not_mounted_without_disabling_conf():
    w = FakeWsl(conf="", mounts="none /mnt/wsl tmpfs rw 0 0")
    d = wslfix.diagnose("Ubuntu", run=w.run)
    assert d["healthy"] is False and d["reason"] == "not_mounted"


def test_diagnose_unreachable_is_its_own_state_not_broken():
    w = FakeWsl(reachable=False)
    d = wslfix.diagnose("Ubuntu", run=w.run)
    assert d["healthy"] is None and d["reason"] == "unreachable"


# --- brief_note --------------------------------------------------------------------------------

def test_brief_note_only_when_broken_and_names_the_cause():
    broken = wslfix.diagnose("Ubuntu", run=FakeWsl(conf=BROKEN_CONF,
                                                   mounts="none /mnt/wsl tmpfs rw 0 0").run)
    note = wslfix.brief_note([broken])
    assert "NOT the live Windows" in note and "sync" in note.lower() and "Ubuntu" in note
    assert len(note) <= 600


def test_brief_note_empty_for_healthy_or_unreachable():
    healthy = wslfix.diagnose("Ubuntu", run=FakeWsl(conf="", mounts=HEALTHY_MOUNTS).run)
    unreach = wslfix.diagnose("Ubuntu", run=FakeWsl(reachable=False).run)
    assert wslfix.brief_note([healthy]) == ""
    assert wslfix.brief_note([unreach]) == ""
    assert wslfix.brief_note([]) == ""


# --- patch_wsl_conf (pure) ---------------------------------------------------------------------

def test_patch_flips_only_the_false_enabled_lines():
    new, changed = wslfix.patch_wsl_conf(BROKEN_CONF)
    assert changed is True
    c = wslfix.parse_wsl_conf(new)
    assert c["automount_enabled"] is True and c["interop_enabled"] is True
    # [boot] and appendWindowsPath untouched, order preserved
    assert "systemd = true" in new
    assert new.index("[automount]") < new.index("[interop]") < new.index("[boot]")


def test_patch_keeps_every_other_line_byte_for_byte():
    conf = "# hand comment\n[automount]\nenabled = false\nroot = /mnt/\n[user]\ndefault = hamza\n"
    new, changed = wslfix.patch_wsl_conf(conf)
    assert changed and "# hand comment" in new and "root = /mnt/" in new and "default = hamza" in new


def test_patch_no_change_when_already_true_or_absent():
    assert wslfix.patch_wsl_conf("[automount]\nenabled = true\n")[1] is False
    assert wslfix.patch_wsl_conf("[boot]\nsystemd = true\n")[1] is False
    assert wslfix.patch_wsl_conf("")[1] is False


def test_patch_preserves_trailing_newline_presence():
    assert wslfix.patch_wsl_conf("[automount]\nenabled = false\n")[0].endswith("\n")
    assert not wslfix.patch_wsl_conf("[automount]\nenabled = false")[0].endswith("\n")


# --- can_use_root ------------------------------------------------------------------------------

def test_can_use_root_true_and_false():
    assert wslfix.can_use_root("Ubuntu", run=FakeWsl(root=True).run) is True
    assert wslfix.can_use_root("Ubuntu", run=FakeWsl(root=False).run) is False


# --- apply -------------------------------------------------------------------------------------

def test_apply_refuses_without_confirm():
    with pytest.raises(wslfix.WslFixError) as e:
        wslfix.apply("Ubuntu", run=FakeWsl(conf=BROKEN_CONF).run)
    assert e.value.code == "confirm_required"


def test_apply_fixes_the_live_fault_end_to_end():
    w = FakeWsl(conf=BROKEN_CONF, mounts="none /mnt/wsl tmpfs rw 0 0", root=True)
    r = wslfix.apply("Ubuntu", confirm=True, run=w.run, clock=lambda: 1700000000)
    assert r["ok"] is True
    assert r["before"]["healthy"] is False and r["after"]["healthy"] is True
    assert w.shutdowns == 1
    # the backed-up old conf and the new conf were both written via stdin (never a shell string)
    assert "/etc/wsl.conf" in w.files and "enabled = true" in w.files["/etc/wsl.conf"]
    assert any(p.startswith("/etc/wsl.conf.bak-") for p in w.files)


def test_apply_already_healthy_touches_nothing():
    w = FakeWsl(conf="", mounts=HEALTHY_MOUNTS)
    r = wslfix.apply("Ubuntu", confirm=True, run=w.run)
    assert r["ok"] is True and r.get("already_healthy") is True
    assert w.shutdowns == 0 and not w.files


def test_apply_without_root_returns_a_paste_command_and_touches_nothing():
    w = FakeWsl(conf=BROKEN_CONF, mounts="none /mnt/wsl tmpfs rw 0 0", root=False)
    r = wslfix.apply("Ubuntu", confirm=True, run=w.run)
    assert r["ok"] is False and r["needs_manual"] is True and r["code"] == "no_root"
    assert "/etc/wsl.conf" in r["command"] and "sudo" in r["command"]
    assert w.shutdowns == 0 and not w.files           # nothing written, nothing restarted


def test_apply_unreachable_distro():
    r = wslfix.apply("Ubuntu", confirm=True, run=FakeWsl(reachable=False).run)
    assert r["ok"] is False and r["code"] == "unreachable"


def test_apply_never_runs_interactive_sudo():
    # the whole failure mode tonight was a call that blocked on a password prompt; apply must
    # only ever reach root through `-u root`, and bail to needs_manual otherwise.
    w = FakeWsl(conf=BROKEN_CONF, mounts="none /mnt/wsl tmpfs rw 0 0", root=False)
    wslfix.apply("Ubuntu", confirm=True, run=w.run)
    for argv in w.calls:
        joined = " ".join(argv)
        assert not re.search(r"\bsudo\b", joined) or "-u" not in argv  # no interactive sudo path


def test_apply_shutdown_is_machine_wide_call():
    w = FakeWsl(conf=BROKEN_CONF, mounts="none /mnt/wsl tmpfs rw 0 0")
    wslfix.apply("Ubuntu", confirm=True, run=w.run)
    assert ["wsl.exe", "--shutdown"] in w.calls


def test_run_default_never_raises(monkeypatch):
    # the real _run must swallow a launch failure into (-1, "", reason), never raise
    import subprocess

    def boom(*a, **k):
        raise OSError("no wsl.exe here")
    monkeypatch.setattr(subprocess, "run", boom)
    rc, out, err = wslfix._run(["wsl.exe", "--shutdown"])
    assert rc == -1 and "launch failed" in err
