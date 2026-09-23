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

import functools
from collections.abc import Callable
from typing import Any

import click

from .config import load_settings
from .scan import scan


def _common_options(f: Callable[..., Any]) -> Callable[..., Any]:
    """--root/--data-dir 挂每个子命令（与 03-usage 文档口径一致）。"""
    f = click.option("--root", default=None, help="照片源根（默认取 PHOTO_ROOT）")(f)
    f = click.option(
        "--data-dir", default=None, help="产出目录（默认取 PHOTO_DESK_DATA_DIR）"
    )(f)
    return f


def _clean_errors(f: Callable[..., Any]) -> Callable[..., Any]:
    """CLI 出口统一给一行清晰错误（与迁移前的 argparse main 行为一致）。"""

    @functools.wraps(f)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return f(*args, **kwargs)
        except click.exceptions.Exit:
            raise
        except click.ClickException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise click.ClickException(str(exc)) from exc

    return wrapper


@click.group()
def cli() -> None:
    """家庭照片管理（NAS 只读 + 可逆筛查 + 时间线）"""


@cli.command("scan")
@_common_options
@_clean_errors
def scan_cmd(root: str | None, data_dir: str | None) -> None:
    """增量清点照片目录"""
    settings = load_settings(root, data_dir)
    from storage import SqliteClient

    from .library import ensure_schema

    settings.ensure_writable_dirs()
    storage = SqliteClient(settings.db_path)
    ensure_schema(storage)
    with storage.session() as db:
        stats = scan(settings, db)
    click.echo(
        f"scan 完成: new={stats.new} updated={stats.updated} "
        f"unchanged={stats.unchanged} recovered={stats.recovered} "
        f"missing={stats.missing}"
    )


@cli.command()
@click.option("--apply", is_flag=True, help="执行移入 blurred/（默认只打印清单）")
@_common_options
@_clean_errors
def triage(apply: bool, root: str | None, data_dir: str | None) -> None:
    """连拍归组+清晰度排名，生成隔离计划（默认 dry-run）"""
    settings = load_settings(root, data_dir)
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
        click.echo(line)
        for mv in report.moves:
            click.echo(
                f"  隔离 [{mv.group_id}] {mv.rel_from}"
                f"  分 {mv.sharpness:.1f} < 0.5×最佳 {mv.best:.1f}"
            )
        if not report.moves:
            click.echo("无可判定废片。")
            return
        if not apply:
            click.echo(f"dry-run：{len(report.moves)} 张待隔离。确认后加 --apply。")
            return
        moved = apply_plan(settings, db, report.moves)
        click.echo(f"已隔离 {moved} 张 → {settings.blurred_dir}")


@cli.command()
@click.option("--photo", type=int, default=None, help="照片 id（单张放回）")
@click.option("--group", default=None, help="连拍组 id（整组放回）")
@_common_options
@_clean_errors
def restore(
    photo: int | None, group: str | None, root: str | None, data_dir: str | None
) -> None:
    """从隔离区放回（单张或整组，先验 sha256）"""
    if (photo is None) == (group is None):
        raise click.UsageError("--photo 与 --group 必须二选一")
    settings = load_settings(root, data_dir)
    from storage import SqliteClient

    from .library import ensure_schema
    from .triage import restore as restore_plan

    settings.ensure_writable_dirs()
    storage = SqliteClient(settings.db_path)
    ensure_schema(storage)
    with storage.session() as db:
        outcomes = restore_plan(settings, db, photo_id=photo, group_id=group)
    if not outcomes:
        click.echo("账本里没有待放回记录（核对 photo/group id）。")
        raise SystemExit(1)
    failed = 0
    for o in outcomes:
        tag = "放回" if o.restored else "失败"
        click.echo(f"  {tag} {o.rel_in_blurred}: {o.detail}")
        failed += 0 if o.restored else 1
    click.echo(f"共 {len(outcomes)} 张，失败 {failed} 张")
    raise SystemExit(1 if failed else 0)


@cli.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", type=int, default=8790, show_default=True)
@_common_options
@_clean_errors
def serve(host: str, port: int, root: str | None, data_dir: str | None) -> None:
    """启动时间线 Web UI"""
    import uvicorn

    from .app import create_app

    settings = load_settings(root, data_dir)
    uvicorn.run(create_app(settings), host=host, port=port)


if __name__ == "__main__":
    cli()
