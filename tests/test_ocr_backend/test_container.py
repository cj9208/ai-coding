"""``ocr-backend container`` tests — the Docker wrapper's command building and
path mapping, with ``subprocess.run`` and ``shutil.which`` faked. Never shells
out to a real Docker, and never builds an image. The ``container`` click
commands are driven through ``CliRunner`` for the same reason."""

import pytest
from click.testing import CliRunner, Result

from ocr_backend import cli, container


def run(*args: str) -> Result:
    return CliRunner().invoke(cli.cli, list(args))


class _FakeRun:
    def __init__(self, returncode=0):
        self.calls = []
        self.returncode = returncode

    def __call__(self, cmd, *args, **kwargs):
        self.calls.append(cmd)

        class _Result:
            pass

        result = _Result()
        result.returncode = self.returncode
        return result


@pytest.fixture(autouse=True)
def fake_docker_on_path(monkeypatch):
    monkeypatch.setattr(container.shutil, "which", lambda name: "/usr/bin/docker")


@pytest.fixture
def fake_run(monkeypatch):
    fake = _FakeRun()
    monkeypatch.setattr(container.subprocess, "run", fake)
    return fake


def test_compose_command_is_anchored_at_the_repo_compose_file(monkeypatch, fake_run):
    monkeypatch.setattr(container, "COMPOSE_FILE", "/repo/docker/ocr/compose.yaml")

    container.build()

    cmd = fake_run.calls[0]
    assert cmd[:6] == [
        "docker",
        "compose",
        "-f",
        "/repo/docker/ocr/compose.yaml",
        "--profile",
        "cpu",
    ]
    assert cmd[-2:] == ["build", "ocr-cpu"]


def test_gpu_flag_switches_profile_and_service(fake_run):
    container.build(gpu=True)
    assert "--profile" in fake_run.calls[0]
    assert "gpu" in fake_run.calls[0]
    assert fake_run.calls[0][-1] == "ocr-gpu"


def test_parse_maps_host_in_out_paths_to_container_mounts(
    monkeypatch, fake_run, tmp_path
):
    in_dir = tmp_path / "in"
    out_dir = tmp_path / "out"
    in_dir.mkdir()
    (in_dir / "scan.pdf").write_bytes(b"x")
    monkeypatch.setattr(container, "IN_DIR", in_dir)
    monkeypatch.setattr(container, "OUT_DIR", out_dir)

    rc = container.parse(in_dir / "scan.pdf")

    assert rc == 0
    cmd = fake_run.calls[0]
    assert "/work/in/scan.pdf" in cmd
    assert "--out" in cmd
    assert cmd[cmd.index("--out") + 1] == "/work/out"
    assert cmd[cmd.index("--device") + 1] == "cpu"
    # the host out dir must have been created before the container starts
    assert out_dir.is_dir()


def test_parse_maps_subdirectories_of_the_in_dir(monkeypatch, fake_run, tmp_path):
    in_dir = tmp_path / "in"
    (in_dir / "sub").mkdir(parents=True)
    (in_dir / "sub" / "doc.pdf").write_bytes(b"x")
    monkeypatch.setattr(container, "IN_DIR", in_dir)
    monkeypatch.setattr(container, "OUT_DIR", tmp_path / "out")

    container.parse(in_dir / "sub" / "doc.pdf")

    assert "/work/in/sub/doc.pdf" in fake_run.calls[0]


def test_parse_rejects_input_outside_the_mounted_in_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(container, "IN_DIR", tmp_path / "in")
    monkeypatch.setattr(container, "OUT_DIR", tmp_path / "out")
    outside = tmp_path / "elsewhere.pdf"
    outside.write_bytes(b"x")

    with pytest.raises(ValueError, match="not under"):
        container.parse(outside)


def test_parse_forwards_raw_dir_under_the_out_mount(monkeypatch, fake_run, tmp_path):
    in_dir = tmp_path / "in"
    out_dir = tmp_path / "out"
    in_dir.mkdir()
    (in_dir / "scan.pdf").write_bytes(b"x")
    monkeypatch.setattr(container, "IN_DIR", in_dir)
    monkeypatch.setattr(container, "OUT_DIR", out_dir)

    container.parse(in_dir / "scan.pdf", raw_dir=out_dir / "scan_raw")

    cmd = fake_run.calls[0]
    assert cmd[cmd.index("--raw-dir") + 1] == "/work/out/scan_raw"
    assert (out_dir / "scan_raw").is_dir()


def test_parse_rejects_raw_dir_outside_the_out_mount(monkeypatch, fake_run, tmp_path):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    (in_dir / "scan.pdf").write_bytes(b"x")
    monkeypatch.setattr(container, "IN_DIR", in_dir)
    monkeypatch.setattr(container, "OUT_DIR", tmp_path / "out")

    with pytest.raises(ValueError, match="not under"):
        container.parse(in_dir / "scan.pdf", raw_dir=tmp_path / "raw")
    assert fake_run.calls == []


def test_cli_container_parse_forwards_raw_dir(monkeypatch, tmp_path):
    seen = {}

    def fake_parse(source, **kwargs):
        seen["source"] = source
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(container, "parse", fake_parse)

    result = run("container", "parse", str(tmp_path / "scan.pdf"), "--raw-dir", "raw")

    assert result.exit_code == 0, result.output + repr(result.exception)
    assert seen["raw_dir"] == "raw"


def test_missing_docker_reports_install_hint_without_running_anything(monkeypatch):
    monkeypatch.setattr(container.shutil, "which", lambda name: None)
    called = []
    monkeypatch.setattr(container.subprocess, "run", lambda *a, **k: called.append(a))

    with pytest.raises(RuntimeError, match="Docker Desktop"):
        container.build()

    assert called == []


def test_download_model_runs_the_containerized_download(fake_run):
    container.download_model("paddleocr-vl-1.6")
    cmd = fake_run.calls[0]
    assert "run" in cmd and "--rm" in cmd
    assert cmd[-2:] == ["download", "paddleocr-vl-1.6"]


def test_cli_container_build_dispatches_to_the_wrapper(fake_run):
    result = run("container", "build", "--gpu")
    assert result.exit_code == 0
    assert fake_run.calls[0][-1] == "ocr-gpu"


def test_cli_container_reports_docker_errors_as_exit_1(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("docker is not on PATH")

    monkeypatch.setattr(container, "build", boom)

    result = run("container", "build")

    assert result.exit_code == 1
    assert "docker is not on PATH" in result.stderr
