"""``ocr-backend`` CLI + model-registry tests — no network, no model load."""

import builtins

import pytest

from ocr_backend import cli
from ocr_backend.backends import paddleocr_vl
from ocr_backend.models import MODELS, is_ready


def test_registry_agrees_with_the_adapter_snapshot_name():
    """The adapter's default snapshot must be provisionable by the CLI."""
    assert paddleocr_vl._SNAPSHOT_NAME in MODELS


def test_registry_entries_pin_a_full_commit_sha():
    """A branch name here would let an upstream push change what we fetch."""
    for name, spec in MODELS.items():
        assert len(spec.revision) == 40, name
        assert spec.revision.isalnum(), name


def test_is_ready_requires_safetensors(tmp_path):
    assert not is_ready(tmp_path)
    (tmp_path / "model.safetensors").write_bytes(b"")
    assert is_ready(tmp_path)


def test_download_fetches_registry_repo_into_model_store(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "model_dir", lambda name: tmp_path / name)
    seen = {}

    def fake_fetch(spec, dest):
        seen["spec"] = spec
        (dest / "model.safetensors").write_bytes(b"")

    monkeypatch.setattr(cli, "_fetch", fake_fetch)

    dest = cli.download("paddleocr-vl-1.6")

    assert dest == tmp_path / "paddleocr-vl-1.6"
    assert seen["spec"] == MODELS["paddleocr-vl-1.6"]


def test_cli_reports_the_ready_store_path(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "model_dir", lambda name: tmp_path / name)

    def fake_fetch(spec, dest):
        (dest / "model.safetensors").write_bytes(b"")

    monkeypatch.setattr(cli, "_fetch", fake_fetch)

    assert cli.main(["download", "paddleocr-vl-1.6"]) == 0
    assert str(tmp_path / "paddleocr-vl-1.6") in capsys.readouterr().out


def test_incomplete_snapshot_fails(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "model_dir", lambda name: tmp_path / name)
    monkeypatch.setattr(cli, "_fetch", lambda spec, dest: None)

    assert cli.main(["download", "paddleocr-vl-1.6"]) == 1
    assert "model.safetensors" in capsys.readouterr().err


def test_unknown_model_is_rejected():
    with pytest.raises(SystemExit):
        cli.main(["download", "not-a-model"])


def test_missing_huggingface_hub_reports_install_hint(monkeypatch, tmp_path):
    original_import = builtins.__import__

    def import_without_hf(name, *args, **kwargs):
        if name == "huggingface_hub":
            raise ImportError("huggingface_hub is unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_hf)

    with pytest.raises(ImportError, match=r"uv sync --extra ocr --extra paddle-cpu"):
        cli._fetch(MODELS["paddleocr-vl-1.6"], tmp_path)
