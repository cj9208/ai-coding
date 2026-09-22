"""``file-manager`` CLI — one click command that runs the web app."""

from __future__ import annotations

import click


@click.command("file-manager")
@click.option(
    "--host",
    envvar="FM_HOST",
    default="127.0.0.1",
    help="bind address (default: FM_HOST env or 127.0.0.1)",
)
@click.option(
    "--port",
    type=int,
    envvar="FM_PORT",
    default=8000,
    help="bind port (default: FM_PORT env or 8000)",
)
def cli(host: str, port: int) -> None:
    """Run the file manager web app (FastAPI + SQLite)."""
    # heavy imports deferred so --help stays instant
    import uvicorn

    from .app import create_app

    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    cli()
