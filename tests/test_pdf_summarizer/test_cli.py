"""pdf-summarize CLI wiring under click: help surface and arg validation.

``cli.main([...])`` became ``CliRunner().invoke(cli.cli, [...])``; the old
subprocess run of ``python -m pdf_summarizer.cli`` is covered here in-process
(the ``__main__`` guard is smoke-tested separately, per the repo-wide click
migration).
"""

from click.testing import CliRunner

from pdf_summarizer import cli as cli_mod


def test_cli_help():
    result = CliRunner().invoke(cli_mod.cli, ["--help"])
    assert result.exit_code == 0, result.output
    # click renders the positional as the metavar PDF_PATH -> fold case
    out = result.output.lower()
    assert "pdf-summarize" in out
    assert "pdf_path" in out
    assert "--output" in out
    assert "--style" in out
    assert "--verbose" in out


def test_cli_missing_required_arg():
    result = CliRunner().invoke(cli_mod.cli, [])
    assert result.exit_code != 0
