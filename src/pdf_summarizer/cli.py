"""``pdf-summarize`` CLI — PDF -> chunks -> map-reduce summary (click).

Error mapping from the old argparse code: each ``print(..., file=sys.stderr)``
plus ``sys.exit(1)`` becomes a ``click.ClickException`` (stderr
"Error: <message>", exit 1) except the "Unexpected error:" line, which is
echoed raw before ``SystemExit(1)`` so its exact wording survives.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import click

from .config import load_config
from .summarizer import summarize

logger = logging.getLogger("pdf_summarizer")


@click.command("pdf-summarize")
@click.argument("pdf_path")
@click.option(
    "-o",
    "--output",
    default=None,
    help="Output file path (default: print to stdout)",
)
@click.option(
    "--backend",
    type=click.Choice(["auto", "pymupdf", "paddleocr-vl"]),
    default=None,
    help="Extractor backend (default: auto)",
)
@click.option("--model", default=None, help="LLM model name (default: deepseek-chat)")
@click.option("--api-key", default=None, help="API key (env: LLM_API_KEY)")
@click.option("--base-url", default=None, help="API base URL (env: LLM_BASE_URL)")
@click.option(
    "-s",
    "--style",
    type=click.Choice(["concise", "detailed", "bullets", "executive"]),
    default=None,
    help="Summary style (default: concise)",
)
@click.option(
    "--chunk-size",
    type=int,
    default=None,
    help="Tokens per chunk (default: 3000)",
)
@click.option(
    "--chunk-overlap",
    type=int,
    default=None,
    help="Overlap tokens between chunks (default: 200)",
)
@click.option(
    "--concurrent",
    type=int,
    default=None,
    help="Max parallel LLM calls (default: 4)",
)
@click.option("-v", "--verbose", is_flag=True, help="Verbose progress output")
def cli(
    pdf_path: str,
    output: str | None,
    backend: str | None,
    model: str | None,
    api_key: str | None,
    base_url: str | None,
    style: str | None,
    chunk_size: int | None,
    chunk_overlap: int | None,
    concurrent: int | None,
    verbose: bool,
) -> None:
    """Summarize a PDF document using an LLM (DeepSeek by default)."""
    if verbose:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    overrides: dict[str, Any] = {
        "extractor_backend": backend,
        "model": model,
        "api_key": api_key,
        "base_url": base_url,
        "chunk_size": chunk_size,
        "chunk_overlap": chunk_overlap,
        "max_concurrency": concurrent,
    }
    if style:
        overrides["summary_style"] = style

    asyncio.run(_async_main(pdf_path, output, overrides))


async def _async_main(
    pdf_path: str, output: str | None, overrides: dict[str, Any]
) -> None:
    config = load_config(**{k: v for k, v in overrides.items() if v is not None})

    if not config.api_key:
        raise click.ClickException(
            "LLM_API_KEY not set. Export the environment variable or pass --api-key."
        )

    try:
        result = await summarize(pdf_path, config)
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    except Exception as e:
        msg = str(e)
        if "401" in msg or "auth" in msg.lower() or "api key" in msg.lower():
            raise click.ClickException(
                "Authentication failed. Check your API key is valid."
            ) from e
        # kept raw (no ClickException) so the "Unexpected error:" prefix
        # matches the old message instead of gaining click's "Error: " one
        click.echo(f"Unexpected error: {e}", err=True)
        raise SystemExit(1) from e

    if output:
        with open(output, "w", encoding="utf-8") as f:
            f.write(result)
        logger.info("Summary written to %s", output)
    else:
        click.echo(result)


if __name__ == "__main__":
    cli()
