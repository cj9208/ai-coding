"""``photos`` CLI — M0 提供 scan / serve；triage / restore 属 M1。

photos scan                        # 增量清点（PHOTO_ROOT / env 可覆盖）
photos scan --root Z:/photo --data-dir D:/pd-out
photos serve [--port 8790]         # 时间线 Web UI
"""

from __future__ import annotations

import argparse
import sys

from .config import load_settings
from .scan import scan


def cmd_scan(args: argparse.Namespace) -> int:
    settings = load_settings(args.root, args.data_dir)
    from storage import SqliteClient

    from .library import Base

    settings.ensure_writable_dirs()
    storage = SqliteClient(settings.db_path)
    storage.init_schema(Base.metadata)
    with storage.session() as db:
        stats = scan(settings, db)
    print(
        f"scan 完成: new={stats.new} updated={stats.updated} "
        f"unchanged={stats.unchanged} recovered={stats.recovered} "
        f"missing={stats.missing}"
    )
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .app import create_app

    settings = load_settings(args.root, args.data_dir)
    uvicorn.run(create_app(settings), host=args.host, port=args.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    # --root/--data-dir 挂每个子命令（argparse 父子同名项会互相覆盖，故总 parser 不挂）。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=None, help="照片源根（默认取 PHOTO_ROOT）")
    common.add_argument(
        "--data-dir", default=None, help="产出目录（默认取 PHOTO_DESK_DATA_DIR）"
    )

    parser = argparse.ArgumentParser(
        prog="photos",
        description="家庭照片管理（NAS 只读 + 可逆筛查 + 时间线）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("scan", help="增量清点照片目录", parents=[common])

    s = sub.add_parser("serve", help="启动时间线 Web UI", parents=[common])
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8790)

    args = parser.parse_args(argv)
    try:
        if args.cmd == "scan":
            return cmd_scan(args)
        if args.cmd == "serve":
            return cmd_serve(args)
    except Exception as exc:  # noqa: BLE001 - CLI 出口统一给一行清晰错误
        print(str(exc), file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
