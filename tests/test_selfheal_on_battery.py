"""The 5-minute self-heal task must also run on a laptop on battery.

MEASURED 2026-10-11: after a restart that did not come back, the hub stayed
down 47 minutes because the CalvounFreeLLMHub task had schtasks' defaults
(DisallowStartIfOnBatteries / StopIfGoingOnBatteries = True) and the PC was on
battery, so every 5-minute check was skipped. scripts/autostart.bat now lifts
both and sets StartWhenAvailable. Static check only: nothing is installed.
"""
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _bat():
    with open(os.path.join(HERE, "scripts", "autostart.bat"), encoding="utf-8") as f:
        return f.read()


def test_the_self_heal_task_runs_on_battery():
    src = _bat()
    for needle in ("-AllowStartIfOnBatteries", "-DontStopIfGoingOnBatteries",
                   "-StartWhenAvailable", "Set-ScheduledTask -TaskName '%TASK%'"):
        assert needle in src, needle


def test_the_power_settings_come_after_the_task_is_created():
    src = _bat()
    create = src.index('schtasks /Create /TN "%TASK%" /SC MINUTE /MO 5')
    assert src.index("New-ScheduledTaskSettingsSet") > create


def test_the_power_settings_line_is_one_line():
    """LF-only files: a caret continuation silently created NO task before
    (see the note in autostart.bat), so the PowerShell call stays on one line."""
    line = next(l for l in _bat().splitlines() if "New-ScheduledTaskSettingsSet" in l)
    assert line.startswith("powershell ") and not line.rstrip().endswith("^")
