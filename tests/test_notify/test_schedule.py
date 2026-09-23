"""``notify schedule`` tests — the Task Scheduler wrapper only ever *builds*
PowerShell here; ``subprocess.run``, ``shutil.which`` and ``platform.system``
are faked, so no test registers a task on the machine running it.

Paths come from ``tmp_path``/``REPO_ROOT`` rather than literals, so the same
assertions hold on Windows and on CI's Linux runner.
"""

import subprocess
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from notify import cli, schedule

INTERVAL = 5


class FakeRun:
    """Records argv; answers like PowerShell would."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def __call__(self, argv: list[str], *args: Any, **kwargs: Any) -> Any:
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(
            argv, self.returncode, stdout=self.stdout, stderr=self.stderr
        )


@pytest.fixture(autouse=True)
def windows_monkeypatched(monkeypatch: pytest.MonkeyPatch) -> None:
    """This machine is Windows and CI is not; the wrapper's platform gate must
    not decide which tests run."""
    monkeypatch.setattr(schedule.platform, "system", lambda: "Windows")


@pytest.fixture
def fake_exe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    exe = tmp_path / "bin" / "notify.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(schedule.shutil, "which", lambda name: str(exe))
    return exe


@pytest.fixture
def fake_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    log = tmp_path / "data" / "notify" / "dispatch.log"
    monkeypatch.setattr(schedule, "LOG_PATH", log)
    return log


@pytest.fixture
def fake_run(
    monkeypatch: pytest.MonkeyPatch, fake_exe: Path, fake_log: Path
) -> FakeRun:
    fake = FakeRun()
    monkeypatch.setattr(schedule.subprocess, "run", fake)
    return fake


PREAMBLE = "$ErrorActionPreference = 'Stop'; "


def script(fake_run: FakeRun) -> str:
    """The one command that ran, minus the shared preamble."""
    assert len(fake_run.calls) == 1
    command = fake_run.calls[0][-1]
    assert command.startswith(PREAMBLE)
    return command[len(PREAMBLE) :]


def test_every_command_is_powershell_that_fails_loudly(fake_run: FakeRun) -> None:
    for run in (
        lambda: schedule.install(INTERVAL),
        schedule.remove,
        schedule.run_now,
        schedule.status,
    ):
        run()
    assert len(fake_run.calls) == 4
    for argv in fake_run.calls:
        # powershell, and the command stops on the first error: without the
        # preamble a broken cmdlet is a non-terminating error, so `status`
        # could print nothing and still exit 0 — a quiet empty scheduler.
        assert argv[:4] == ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
        assert argv[-1].startswith(PREAMBLE)


# --- what the registration says ------------------------------------------


def test_install_sets_the_repo_root_as_working_directory(fake_run: FakeRun) -> None:
    schedule.install(INTERVAL)
    assert f"-WorkingDirectory '{schedule.REPO_ROOT}'" in script(fake_run)


def test_install_redirects_to_the_absolute_ledger_log_dir(fake_run: FakeRun) -> None:
    # a bare filename would land in the working directory, i.e. the repo root —
    # the log is the only evidence an unattended round produced, so its path
    # has to be the one `status` reads.
    schedule.install(INTERVAL)
    assert f"dispatch >> {schedule.LOG_PATH} 2>&1" in script(fake_run)
    assert schedule.LOG_PATH.parent.is_dir()


def test_install_runs_the_installed_executable_not_uv_run(
    fake_run: FakeRun, fake_exe: Path
) -> None:
    schedule.install(INTERVAL)
    assert f"/c {fake_exe} dispatch" in script(fake_run)
    assert "uv" not in script(fake_run)


def test_install_repeats_on_the_requested_interval(fake_run: FakeRun) -> None:
    schedule.install(17)
    power_shell = script(fake_run)
    assert "-RepetitionInterval (New-TimeSpan -Minutes 17)" in power_shell
    # a round that overruns must die before the next one wants the ledger
    assert "-ExecutionTimeLimit (New-TimeSpan -Minutes 17)" in power_shell


def test_install_enables_the_sleep_wake_catch_up_and_the_battery_switches(
    fake_run: FakeRun,
) -> None:
    schedule.install(INTERVAL)
    power_shell = script(fake_run)
    assert "-StartWhenAvailable" in power_shell
    assert "-AllowStartIfOnBatteries" in power_shell
    assert "-DontStopIfGoingOnBatteries" in power_shell


def test_install_overwrites_an_existing_registration(fake_run: FakeRun) -> None:
    schedule.install(INTERVAL)
    assert "-Force" in script(fake_run)


def test_install_rejects_a_quietly_broken_checkout_path(
    monkeypatch: pytest.MonkeyPatch, fake_log: Path, tmp_path: Path
) -> None:
    spaced = tmp_path / "Program Files" / "notify.exe"
    spaced.parent.mkdir(parents=True, exist_ok=True)
    spaced.write_text("", encoding="utf-8")
    monkeypatch.setattr(schedule.shutil, "which", lambda name: str(spaced))
    fake = FakeRun()
    monkeypatch.setattr(schedule.subprocess, "run", fake)

    with pytest.raises(schedule.ScheduleError, match="spaces"):
        schedule.install(INTERVAL)

    assert fake.calls == []


@pytest.mark.parametrize("interval", [0, -3])
def test_install_refuses_a_nonsense_interval(interval: int) -> None:
    with pytest.raises(ValueError, match="at least 1 minute"):
        schedule.install_script(interval)


# --- removal, manual round, reporting ------------------------------------


def test_remove_unregisters_the_task_by_name(fake_run: FakeRun) -> None:
    schedule.remove()
    assert schedule.TASK_NAME in script(fake_run)
    assert "Unregister-ScheduledTask" in script(fake_run)


def test_run_now_starts_the_task_without_running_dispatch_itself(
    fake_run: FakeRun,
) -> None:
    out = schedule.run_now()
    assert script(fake_run) == f"Start-ScheduledTask -TaskName '{schedule.TASK_NAME}'"
    assert out == ""


def test_status_reports_the_scheduler_view_and_the_log_tail(
    fake_run: FakeRun,
) -> None:
    fake_run.stdout = "state        Ready\nrepeat       00:05:00"
    schedule.LOG_PATH.parent.mkdir(parents=True)
    schedule.LOG_PATH.write_text("round one\nround two\n", encoding="utf-8")

    out = schedule.status(log_lines=5)

    power_shell = script(fake_run)
    assert "Get-ScheduledTask" in power_shell
    assert "Get-ScheduledTaskInfo" in power_shell
    assert out.startswith("state        Ready")
    assert "round two" in out


def test_status_lines_interpolate_the_property_not_the_object(
    fake_run: FakeRun,
) -> None:
    schedule.status()
    power_shell = script(fake_run)
    assert power_shell.count("Write-Output") == 9
    # a bare "$t.State" inside an expanding string is the object's ToString()
    # followed by the literal ".State" — what the first real run printed.
    assert 'Write-Output "state        $($t.State)"' in power_shell


def test_status_says_so_when_the_log_has_never_been_written(
    fake_run: FakeRun, fake_log: Path
) -> None:
    assert "not written yet" in schedule.status()
    assert not fake_log.exists()


# --- the environment it refuses to pretend about ------------------------


def test_missing_executable_is_reported_before_anything_runs(
    monkeypatch: pytest.MonkeyPatch, fake_log: Path
) -> None:
    monkeypatch.setattr(schedule.shutil, "which", lambda name: None)
    called: list[Any] = []
    monkeypatch.setattr(schedule.subprocess, "run", lambda *a, **k: called.append(a))

    with pytest.raises(schedule.ScheduleError, match="uv sync"):
        schedule.install(INTERVAL)

    assert called == []


def test_a_platform_without_task_scheduler_gets_the_recipe(
    monkeypatch: pytest.MonkeyPatch, fake_exe: Path, fake_log: Path
) -> None:
    monkeypatch.setattr(schedule.platform, "system", lambda: "Linux")
    fake = FakeRun()
    monkeypatch.setattr(schedule.subprocess, "run", fake)

    with pytest.raises(schedule.ScheduleError, match="cron"):
        schedule.install(INTERVAL)

    assert fake.calls == []


def test_task_scheduler_refusal_surfaces_its_own_words(
    monkeypatch: pytest.MonkeyPatch, fake_exe: Path, fake_log: Path
) -> None:
    fake = FakeRun(returncode=1, stderr="Access is denied.")
    monkeypatch.setattr(schedule.subprocess, "run", fake)

    with pytest.raises(schedule.ScheduleError, match="Access is denied"):
        schedule.install(INTERVAL)


def test_run_failure_with_no_output_still_reports_the_exit_code(
    monkeypatch: pytest.MonkeyPatch, fake_exe: Path, fake_log: Path
) -> None:
    monkeypatch.setattr(schedule.subprocess, "run", FakeRun(returncode=3))

    with pytest.raises(schedule.ScheduleError, match="exit 3"):
        schedule.remove()


# --- the CLI surface ----------------------------------------------------


def test_cli_install_reports_the_cadence_and_where_to_check(
    fake_run: FakeRun,
) -> None:
    result = CliRunner().invoke(
        cli.cli, ["schedule", "install", "--interval-minutes", "7"]
    )

    assert result.exit_code == 0, result.output + repr(result.exception)
    assert schedule.TASK_NAME in result.output
    assert "every 7 min" in result.output
    assert "schedule status" in result.output


def test_cli_status_prints_the_report(fake_run: FakeRun) -> None:
    fake_run.stdout = "state        Ready"
    result = CliRunner().invoke(cli.cli, ["schedule", "status"])

    assert result.exit_code == 0, result.output + repr(result.exception)
    assert "state        Ready" in result.output


def test_cli_run_now_names_the_task(fake_run: FakeRun) -> None:
    result = CliRunner().invoke(cli.cli, ["schedule", "run-now"])

    assert result.exit_code == 0, result.output + repr(result.exception)
    assert schedule.TASK_NAME in script(fake_run)


def test_cli_reports_a_refusal_as_exit_1(
    monkeypatch: pytest.MonkeyPatch, fake_exe: Path, fake_log: Path
) -> None:
    monkeypatch.setattr(
        schedule.subprocess, "run", FakeRun(returncode=1, stderr="Access is denied.")
    )
    result = CliRunner().invoke(cli.cli, ["schedule", "install"])

    assert result.exit_code == 1
    assert "Access is denied" in result.output


def test_cli_rejects_a_zero_interval_before_touching_the_scheduler(
    fake_run: FakeRun,
) -> None:
    result = CliRunner().invoke(
        cli.cli, ["schedule", "install", "--interval-minutes", "0"]
    )

    assert result.exit_code != 0
    assert fake_run.calls == []


def test_remove_is_wired_into_the_cli(fake_run: FakeRun) -> None:
    result = CliRunner().invoke(cli.cli, ["schedule", "remove"])

    assert result.exit_code == 0, result.output + repr(result.exception)
    assert "Unregister-ScheduledTask" in script(fake_run)


def test_default_interval_matches_the_design_recipe() -> None:
    assert schedule.DEFAULT_INTERVAL_MINUTES == 5
