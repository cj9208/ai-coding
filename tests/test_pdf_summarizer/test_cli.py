import subprocess
import sys


def test_cli_help():
    result = subprocess.run(
        [sys.executable, "-m", "pdf_summarizer.cli", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "pdf-summarize" in result.stdout
    assert "pdf_path" in result.stdout
    assert "--output" in result.stdout
    assert "--style" in result.stdout
    assert "--verbose" in result.stdout


def test_cli_missing_required_arg():
    result = subprocess.run(
        [sys.executable, "-m", "pdf_summarizer.cli"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
