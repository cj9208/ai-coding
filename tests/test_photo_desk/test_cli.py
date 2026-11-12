"""photos recover 的 Click 入口：无待对账 exit 0；存在歧义 exit 1 且不继续。"""

from click.testing import CliRunner

from photo_desk.cli import cli
from photo_desk.library import MoveLedger, ensure_schema
from storage import SqliteClient

from .helpers import make_settings


def test_recover_cli_empty_and_unresolved(tmp_path):
    settings = make_settings(tmp_path)
    args = [
        "recover",
        "--root",
        str(settings.root),
        "--data-dir",
        str(settings.data_dir),
    ]
    runner = CliRunner()
    empty = runner.invoke(cli, args)
    assert empty.exit_code == 0
    assert "没有待对账" in empty.output
    client = SqliteClient(settings.db_path)
    try:
        ensure_schema(client)
        with client.session() as session:
            session.add(
                MoveLedger(
                    photo_id=999,
                    rel_from="missing.jpg",
                    rel_to="blurred/missing.jpg",
                    sha256="a" * 64,
                    reason="blur_rank",
                    phase="quarantine_pending",
                )
            )
            session.commit()
    finally:
        client.dispose()
    blocked = runner.invoke(cli, args)
    assert blocked.exit_code != 0
    assert "需人工核对" in blocked.output
