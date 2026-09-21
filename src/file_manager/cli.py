from __future__ import annotations

import argparse
import os


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="file-manager",
        description="Run the file manager web app (FastAPI + SQLite).",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("FM_HOST", "127.0.0.1"),
        help="bind address (default: FM_HOST env or 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("FM_PORT", "8000")),
        help="bind port (default: FM_PORT env or 8000)",
    )
    args = parser.parse_args()

    # heavy imports deferred so --help stays instant
    import uvicorn

    from .app import create_app

    uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
