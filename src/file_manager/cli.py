from __future__ import annotations

import os


def main() -> None:
    import uvicorn

    from .app import create_app

    host = os.environ.get("FM_HOST", "127.0.0.1")
    port = int(os.environ.get("FM_PORT", "8000"))
    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    main()
