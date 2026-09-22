"""``photos`` CLI — scan / serve（M0）+ triage / restore（M1 写通道）。

photos scan                        # 增量清点（PHOTO_ROOT / env 可覆盖）
photos scan --root Z:/photo --data-dir D:/pd-out
photos triage                      # dry-run：连拍归组+评分，只打印隔离清单
photos triage --apply              # 执行：移入 blurred/ 并写账本
photos restore --photo 42          # 单张放回（sha256 验身后）
photos restore --group a1b2c3d4e5f6  # 整组放回
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

    from .library import ensure_schema

    settings.ensure_writable_dirs()
    storage = SqliteClient(settings.db_path)
    ensure_schema(storage)
    with storage.session() as db:
        stats = scan(settings, db)
    print(
        f"scan 完成: new={stats.new} updated={stats.updated} "
        f"unchanged={stats.unchanged} recovered={stats.recovered} "
        f"missing={stats.missing}"
    )
    return 0


def cmd_triage(args: argparse.Namespace) -> int:
    settings = load_settings(args.root, args.data_dir)
    from storage import SqliteClient

    from .library import ensure_schema
    from .triage import apply_plan, build_plan

    settings.ensure_writable_dirs()
    storage = SqliteClient(settings.db_path)
    ensure_schema(storage)
    with storage.session() as db:
        report = build_plan(settings, db)
        line = f"连拍组 {report.groups} 个，评分 {report.scored} 张"
        if report.unscored:
            line += f"；{report.unscored} 张解码失败、不参与判定"
        print(line)
        for mv in report.moves:
            print(
                f"  隔离 [{mv.group_id}] {mv.rel_from}"
                f"  分 {mv.sharpness:.1f} < 0.5×最佳 {mv.best:.1f}"
            )
        if not report.moves:
            print("无可判定废片。")
            return 0
        if not args.apply:
            print(f"dry-run：{len(report.moves)} 张待隔离。确认后加 --apply。")
            return 0
        moved = apply_plan(settings, db, report.moves)
        print(f"已隔离 {moved} 张 → {settings.blurred_dir}")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    settings = load_settings(args.root, args.data_dir)
    from storage import SqliteClient

    from .library import ensure_schema
    from .triage import restore

    settings.ensure_writable_dirs()
    storage = SqliteClient(settings.db_path)
    ensure_schema(storage)
    with storage.session() as db:
        outcomes = restore(settings, db, photo_id=args.photo, group_id=args.group)
    if not outcomes:
        print("账本里没有待放回记录（核对 photo/group id）。")
        return 1
    failed = 0
    for o in outcomes:
        tag = "放回" if o.restored else "失败"
        print(f"  {tag} {o.rel_in_blurred}: {o.detail}")
        failed += 0 if o.restored else 1
    print(f"共 {len(outcomes)} 张，失败 {failed} 张")
    return 1 if failed else 0


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

    t = sub.add_parser(
        "triage",
        help="连拍归组+清晰度排名，生成隔离计划（默认 dry-run）",
        parents=[common],
    )
    t.add_argument(
        "--apply", action="store_true", help="执行移入 blurred/（默认只打印清单）"
    )

    r = sub.add_parser(
        "restore", help="从隔离区放回（单张或整组，先验 sha256）", parents=[common]
    )
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--photo", type=int, help="照片 id（单张放回）")
    g.add_argument("--group", help="连拍组 id（整组放回）")

    s = sub.add_parser("serve", help="启动时间线 Web UI", parents=[common])
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8790)

    args = parser.parse_args(argv)
    try:
        if args.cmd == "scan":
            return cmd_scan(args)
        if args.cmd == "triage":
            return cmd_triage(args)
        if args.cmd == "restore":
            return cmd_restore(args)
        if args.cmd == "serve":
            return cmd_serve(args)
    except Exception as exc:  # noqa: BLE001 - CLI 出口统一给一行清晰错误
        print(str(exc), file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
