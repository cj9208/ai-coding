import argparse
import asyncio
import logging
import sys

from .config import load_config
from .summarizer import summarize

logger = logging.getLogger("pdf_summarizer")


def main():
    parser = argparse.ArgumentParser(
        prog="pdf-summarize",
        description="Summarize a PDF document using an LLM (DeepSeek by default).",
    )
    parser.add_argument("pdf_path", help="Path to the PDF file")
    parser.add_argument(
        "-o", "--output", help="Output file path (default: print to stdout)"
    )
    parser.add_argument(
        "--backend",
        choices=["auto", "pymupdf", "paddleocr"],
        default=None,
        help="Extractor backend (default: auto)",
    )
    parser.add_argument(
        "--model", default=None, help="LLM model name (default: deepseek-chat)"
    )
    parser.add_argument("--api-key", default=None, help="API key (env: LLM_API_KEY)")
    parser.add_argument(
        "--base-url", default=None, help="API base URL (env: LLM_BASE_URL)"
    )
    parser.add_argument(
        "-s",
        "--style",
        choices=["concise", "detailed", "bullets", "executive"],
        default=None,
        help="Summary style (default: concise)",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=None,
        help="Tokens per chunk (default: 3000)",
    )
    parser.add_argument(
        "--chunk-overlap",
        type=int,
        default=None,
        help="Overlap tokens between chunks (default: 200)",
    )
    parser.add_argument(
        "--concurrent",
        type=int,
        default=None,
        help="Max parallel LLM calls (default: 4)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Verbose progress output"
    )

    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    asyncio.run(_async_main(args))


async def _async_main(args):
    overrides = {
        "extractor_backend": args.backend,
        "model": args.model,
        "api_key": args.api_key,
        "base_url": args.base_url,
        "chunk_size": args.chunk_size,
        "chunk_overlap": args.chunk_overlap,
        "max_concurrency": args.concurrent,
    }
    if args.style:
        overrides["summary_style"] = args.style

    config = load_config(**{k: v for k, v in overrides.items() if v is not None})

    if not config.api_key:
        print(
            "Error: LLM_API_KEY not set. "
            "Export the environment variable or pass --api-key.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        result = await summarize(args.pdf_path, config)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        msg = str(e)
        if "401" in msg or "auth" in msg.lower() or "api key" in msg.lower():
            print(
                "Error: Authentication failed. Check your API key is valid.",
                file=sys.stderr,
            )
        else:
            print(f"Unexpected error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(result)
        logger.info("Summary written to %s", args.output)
    else:
        print(result)


if __name__ == "__main__":
    main()
