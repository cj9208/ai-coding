"""``notify schedule`` — the one recurring op dispatch needs, wrapped.

Why this exists instead of a documented PowerShell block: registering the task
means getting a handful of things right every time — the working directory, the
repetition interval, ``StartWhenAvailable`` (so a reboot or a closed lid replays
the missed rounds), the battery policy (this is a laptop), the log redirect that
is the only evidence of "did the scheduler actually launch me", and a PowerShell
that fails loudly instead of exiting 0 on a broken cmdlet. Retyping that is how
it goes quietly wrong, so this module derives it from ``REPO_ROOT`` (same
posture as ``ocr_backend.container.py``).

Task Scheduler only, on purpose: the design's L2 premise (§4.6) is "a short
process a scheduler launches", and this machine is Windows. On another host,
cron or a systemd timer is the equivalent and the command to schedule is
already the one a person would type there.

The action calls the installed console script rather than ``uv run``: an
unattended process should not depend on ``uv`` being on ``PATH`` or on the
resolver agreeing in that moment. ``.env`` is read through ``notify.config``,
which anchors to ``REPO_ROOT``, and the log path is absolute, so nothing here
depends on the working directory — it is set to the repo root so the task reads
as "this checkout" in the scheduler UI.
"""

from __future__ import annotations

import platform
import shutil
import subprocess  # nosec B404 - Task Scheduler is the point; argv lists only
from pathlib import Path

from utils.paths import REPO_ROOT

from . import config

TASK_NAME = "notify-dispatch"

#: alert latency is bounded by this interval (§4.9), so it is the one number
#: worth changing later; the 1h throttle window makes it safe to shorten.
DEFAULT_INTERVAL_MINUTES = 5

LOG_PATH = config.notify_data_dir() / "dispatch.log"


class ScheduleError(RuntimeError):
    """Task Scheduler refused, or this platform has no Task Scheduler."""


def executable() -> Path:
    """The installed ``notify`` entry point inside this checkout's venv."""
    found = shutil.which("notify")
    if found is None:
        raise ScheduleError(
            "no notify executable found — run `uv sync` (or `uv pip install -e .`) first"
        )
    path = Path(found)
    if not path.exists():  # which() answered, but the file is gone (stale venv)
        raise ScheduleError(
            f"notify executable {path} does not exist — re-run `uv sync`"
        )
    return path


def _powershell(command: str) -> list[str]:
    # $ErrorActionPreference: a failed cmdlet must exit non-zero. PowerShell's
    # default treats most errors as non-terminating, so a broken command can
    # print nothing at all and still report success — the quiet failure this
    # whole module exists to prevent.
    preamble = "$ErrorActionPreference = 'Stop'"
    return [
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        f"{preamble}; {command}",
    ]


def install_script(interval_minutes: int = DEFAULT_INTERVAL_MINUTES) -> str:
    """The PowerShell that registers the task — pure string building, so a
    test can read exactly what will run on the machine."""
    if interval_minutes < 1:
        raise ValueError("interval must be at least 1 minute")
    log = LOG_PATH
    log.parent.mkdir(parents=True, exist_ok=True)
    exe = executable()
    # cmd.exe takes its command line unquoted here (its /c rule strips the
    # first and last quote of a quoted one), so a space anywhere in the two
    # paths would split the command into something else — fail loudly at
    # install time rather than register a task that fails quietly forever.
    spaced = [str(p) for p in (exe, log) if " " in str(p)]
    if spaced:
        raise ScheduleError(
            "cannot schedule a checkout whose paths contain spaces"
            f" ({', '.join(spaced)}) — cmd.exe would split the command line"
        )
    action = (
        f"$a = New-ScheduledTaskAction -Execute cmd.exe -Argument "
        f"'/c {exe} dispatch >> {log} 2>&1' "
        f"-WorkingDirectory '{REPO_ROOT}'"
    )
    trigger = (
        "$t = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) "
        f"-RepetitionInterval (New-TimeSpan -Minutes {interval_minutes}) "
        "-RepetitionDuration (New-TimeSpan -Days 9999)"
    )
    # -StartWhenAvailable is the sleep-wake catch-up (§4.9): rounds missed
    # during sleep replay at the next wake instead of vanishing. The time
    # limit equals the interval because a round that overruns must not stack
    # up — Task Scheduler will not start a second instance of a running task.
    # The battery switches: this is a laptop, and a dispatch round should not
    # be the first thing unplugging sacrifices.
    settings = (
        "$s = New-ScheduledTaskSettingsSet -StartWhenAvailable "
        "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        f"-ExecutionTimeLimit (New-TimeSpan -Minutes {interval_minutes})"
    )
    register = (
        f"Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $a -Trigger $t "
        "-Settings $s -Force | Out-Null"
    )
    return "; ".join((action, trigger, settings, register))


def remove_script() -> str:
    return f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false"


def run_script() -> str:
    return f"Start-ScheduledTask -TaskName '{TASK_NAME}'"


def status_script() -> str:
    """One report line per fact worth seeing.

    PowerShell property names rather than ``schtasks /Query`` labels on
    purpose: the latter are translated into the console's UI language (this
    machine answers in Chinese), so a grep for English label text would
    silently return nothing instead of failing loudly. One ``Write-Output``
    per line because these pieces are joined into a single ";"-separated
    command, where only whole statements survive. And every property is
    wrapped in ``$( )``: an expanding string interpolates the *variable* only,
    so ``"$t.State"`` printed the object's ``ToString()`` plus a literal
    ``.State`` on the first real run.
    """
    quoted = f"'{TASK_NAME}'"
    return "; ".join(
        (
            f"$t = Get-ScheduledTask -TaskName {quoted}",
            f"$i = Get-ScheduledTaskInfo -TaskName {quoted}",
            "$a = $t.Actions[0]",
            "$g = $t.Triggers[0]",
            'Write-Output "state        $($t.State)"',
            'Write-Output "execute      $($a.Execute) $($a.Arguments)"',
            'Write-Output "working-dir  $($a.WorkingDirectory)"',
            'Write-Output "repeat       $($g.Repetition.Interval)"',
            'Write-Output "time-limit   $($t.Settings.ExecutionTimeLimit)"',
            'Write-Output "catch-up     $($t.Settings.StartWhenAvailable)"',
            'Write-Output "last-run     $($i.LastRunTime)"',
            'Write-Output "last-result  $($i.LastTaskResult)"',
            'Write-Output "next-run     $($i.NextRunTime)"',
        )
    )


def _run(argv: list[str]) -> str:
    if platform.system() != "Windows":
        raise ScheduleError(
            f"scheduling wraps Windows Task Scheduler, not {platform.system()}"
            " — put `notify dispatch` on a cron entry or a systemd timer"
        )
    result = subprocess.run(  # nosec B603 - fixed argv, no shell, no user input
        argv, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise ScheduleError(detail or f"exit {result.returncode}")
    return (result.stdout or "").strip()


def install(interval_minutes: int = DEFAULT_INTERVAL_MINUTES) -> str:
    """Register the task; ``-Force`` makes re-running this an update."""
    return _run(_powershell(install_script(interval_minutes)))


def remove() -> str:
    return _run(_powershell(remove_script()))


def run_now() -> str:
    """Fire one round now — the scheduler's own entry point, not a human one."""
    return _run(_powershell(run_script()))


def status(log_lines: int = 10) -> str:
    """Task Scheduler's view plus the tail of the log it writes to."""
    return _run(_powershell(status_script())) + "\n" + log_tail(log_lines)


def log_tail(lines: int = 10) -> str:
    """The last few dispatch rounds as the scheduler saw them."""
    if not LOG_PATH.exists():
        return f"log       {LOG_PATH} (not written yet)"
    tail = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(
        [f"log       {LOG_PATH} (last {min(lines, len(tail))} of {len(tail)} lines)"]
        + [f"  {line}" for line in tail[-lines:]]
    )
