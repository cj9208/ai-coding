"""``ocr-review`` CLI — register documents and serve the review UI.

    ocr-review serve                      # inbox = data/ocr_backend/out/
    ocr-review serve --inbox D:/shared/ocr-out --host 0.0.0.0 --port 8765
    ocr-review add scan.pdf --ocr scan.ocr.json   # workspace for a pair outside
                                                  # the inbox convention

``serve`` is the long-lived entry (deployment box for business colleagues);
``add`` is a one-shot that only builds the workspace (page raster + meta),
so the document shows up in the list even with an unusual file layout.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ocr-review", description="human proofreading UI for OcrDocument JSON"
    )
    parser.add_argument(
        "--work-root",
        default=WORK_ROOT_DEFAULT,
        type=Path,
        help="workspace store (default: %(default)s)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="serve the review UI")
    s.add_argument(
        "--inbox",
        default=INBOX_DEFAULT,
        type=Path,
        help="directory of parse bundles to list (default: %(default)s)",
    )
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)

    a = sub.add_parser("add", help="register a (pdf, ocr.json) pair as a workspace")
    a.add_argument("pdf")
    a.add_argument("--ocr", required=True, help="contract JSON for that pdf")

    args = parser.parse_args(argv)

    if args.cmd == "add":
        try:
            add(args.pdf, args.ocr, Path(args.work_root))
        except (FileNotFoundError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0

    if args.cmd == "serve":
        import uvicorn

        from .app import create_app

        uvicorn.run(
            create_app(Path(args.inbox), Path(args.work_root)),
            host=args.host,
            port=args.port,
        )
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
