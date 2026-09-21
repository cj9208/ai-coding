"""``ocr-backend`` CLI + model-registry tests — no network, no model load."""

import builtins
import json

import pytest

from ocr_backend import cli
from ocr_backend.backends import paddleocr_vl
from ocr_backend.contract import (
    BlockKind,
    ContentFormat,
    OcrBackendInfo,
    OcrBlock,
    OcrDocument,
    OcrPage,
    OcrSource,
)
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


# --------------------------------------------------------------- parse subcommand


def _fake_document() -> OcrDocument:
    return OcrDocument(
        source=OcrSource(kind="pdf", path="demo.pdf", sha256="0" * 64, page_count=1),
        backend=OcrBackendInfo(
            name="paddleocr_vl",
            library_version="3.7.0",
            model="PaddleOCR-VL-1.6-0.9B",
            pipeline_version="v1.6",
            options={"use_layout_detection": True},
        ),
        created_at="2026-09-21T12:00:00+08:00",
        pages=[
            OcrPage(
                page_index=0,
                width=1191,
                height=1684,
                blocks=[
                    OcrBlock(
                        id=0,
                        kind=BlockKind.title,
                        raw_label="doc_title",
                        content="Hello",
                        content_format=ContentFormat.text,
                        bbox=(95.0, 129.0, 825.0, 171.0),
                        order=1,
                        score=0.7884,
                    )
                ],
            )
        ],
    )


class _FakeBackend:
    """Stands in for PaddleOCRVLBackend — records what it was built with and
    whether close() ran, without ever importing paddleocr."""

    last_config = None
    closed = False

    def __init__(self, config):
        _FakeBackend.last_config = config
        _FakeBackend.closed = False

    def parse(self, source):
        return _fake_document()

    def close(self):
        _FakeBackend.closed = True


@pytest.fixture
def fake_backend(monkeypatch):
    _FakeBackend.last_config = None
    _FakeBackend.closed = False
    monkeypatch.setattr(paddleocr_vl, "PaddleOCRVLBackend", _FakeBackend)
    return _FakeBackend


def test_parse_writes_contract_json_and_markdown(fake_backend, tmp_path):
    source = tmp_path / "demo.pdf"
    source.write_bytes(b"%PDF-fake")
    out_dir = tmp_path / "out"

    json_path = cli.parse(source, out_dir)

    assert json_path == out_dir / "demo.ocr.json"
    dumped = json.loads(json_path.read_text(encoding="utf-8"))
    assert dumped["schema_version"] == "1.0"
    assert dumped["pages"][0]["blocks"][0]["content"] == "Hello"

    md_path = out_dir / "demo.ocr.md"
    assert md_path.read_text(encoding="utf-8") == "# Hello"

    source_copy = out_dir / "demo.pdf"
    assert source_copy.read_bytes() == b"%PDF-fake"
    assert fake_backend.closed


def test_parse_passes_cli_flags_through_to_config(fake_backend, tmp_path):
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"%PDF-fake")

    cli.main(
        [
            "parse",
            str(source),
            "--out",
            str(tmp_path / "out"),
            "--device",
            "cpu",
            "--pipeline-version",
            "v1.6",
            "--model-dir",
            "/models/paddleocr-vl-1.6",
            "--format-block-content",
        ]
    )

    cfg = fake_backend.last_config
    assert cfg.device == "cpu"
    assert cfg.pipeline_version == "v1.6"
    assert str(cfg.model_dir) == "/models/paddleocr-vl-1.6"
    assert cfg.format_block_content is True


def test_parse_closes_backend_even_after_success(fake_backend, tmp_path):
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"%PDF-fake")

    cli.parse(source, tmp_path / "out")

    assert fake_backend.closed


def test_cli_parse_missing_source_exits_before_touching_backend(
    fake_backend, tmp_path, capsys
):
    rc = cli.main(["parse", str(tmp_path / "nope.pdf"), "--out", str(tmp_path / "out")])

    assert rc == 1
    assert "not found" in capsys.readouterr().err
    assert fake_backend.last_config is None
