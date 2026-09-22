"""``ocr-review`` CLI — register documents and serve the review UI.

    ocr-review serve                      # inbox = data/ocr_backend/out/
    ocr-review serve --inbox D:/shared/ocr-out --host 0.0.0.0 --port 8765
    ocr-review add scan.pdf --ocr scan.ocr.json   # workspace for a pair outside
                                                  # the inbox convention

``serve`` is the long-lived entry (deployment box for business colleagues);
``add`` is a one-shot that only builds the workspace (page raster + meta),
so the document shows up in the list even with an unusual file layout.

Declaration notes (post-migration from argparse, repo-wide click convention):
tokens are unchanged — ``--work-root`` is still a group option that goes
*before* the subcommand, and ``add <pdf> --ocr <json>`` / ``serve --inbox
--host --port`` keep their spellings. ``add`` stays a module-level function
(callers build a workspace without going through argv); the click bodies only
map arguments onto it and turn its errors into exit codes.
"""

from __future__ import annotations

from pathlib import Path

import click

from ocr_backend.contract import OcrDocument

from .workspace import (
    INBOX_DEFAULT,
    WORK_ROOT_DEFAULT,
    InboxDoc,
    Workspace,
)


def add(pdf: str | Path, ocr_json: str | Path, work_root: Path) -> Path:
    """Build the workspace for an explicit (source, contract-JSON) pair."""
    json_path = Path(ocr_json)
    doc = OcrDocument.model_validate_json(json_path.read_text(encoding="utf-8"))
    source = Path(pdf)
    if not source.is_file():
        raise FileNotFoundError(f"源文件不存在: {source}")
    if doc.source.kind != "pdf":
        raise ValueError("v1 校对仅支持 PDF 源（图片源列为 follow-up）")
    from storage import sha256_hex

    if sha256_hex(source.read_bytes()) != doc.source.sha256:
        print(f"警告: {source} 与契约记录的 sha256 不一致，仍按现状建工作区")
    item = InboxDoc(ocr_json=json_path, doc=doc, source=source)
    ws = Workspace.open(work_root, item)
    print(f"workspace ready: {ws.root}")
    return ws.root


@click.group()
@click.option(
    "--work-root",
    default=WORK_ROOT_DEFAULT,
    type=click.Path(path_type=Path),
    show_default=True,
    help="workspace store",
)
@click.pass_context
def cli(ctx: click.Context, work_root: Path) -> None:
    """human proofreading UI for OcrDocument JSON"""
    ctx.obj = work_root


@cli.command("serve")
@click.option(
    "--inbox",
    default=INBOX_DEFAULT,
    type=click.Path(path_type=Path),
    show_default=True,
    help="directory of parse bundles to list",
)
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", type=int, default=8765, show_default=True)
@click.pass_obj
def serve_cmd(work_root: Path, inbox: Path, host: str, port: int) -> None:
    """serve the review UI"""
    import uvicorn

    from .app import create_app

    # Deferred so that `add` (and --help) work without uvicorn/fastapi loaded.
    uvicorn.run(create_app(inbox, work_root), host=host, port=port)


@cli.command("add")
@click.argument("pdf")
@click.option("--ocr", required=True, help="contract JSON for that pdf")
@click.pass_obj
def add_cmd(work_root: Path, pdf: str, ocr: str) -> None:
    """register a (pdf, ocr.json) pair as a workspace"""
    try:
        add(pdf, ocr, work_root)
    except (FileNotFoundError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc


if __name__ == "__main__":
    cli()
