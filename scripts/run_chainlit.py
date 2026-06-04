from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import uvicorn
from chainlit.auth import ensure_jwt_secret
from chainlit.cache import init_lc_cache
from chainlit.config import config, load_module
from chainlit.markdown import init_markdown
from chainlit.server import app
from chainlit.utils import check_file
from sniffio import current_async_library_cvar


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Chainlit without the CLI event-loop patch.")
    parser.add_argument("target", help="Path to the Chainlit app file.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7860)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    target = str(Path(args.target))
    check_file(target)

    config.run.host = args.host
    config.run.port = args.port
    config.run.module_name = target

    load_module(target)
    assert_app_callbacks()
    ensure_jwt_secret()
    init_markdown(config.root)
    init_lc_cache()

    current_async_library_cvar.set("asyncio")
    asyncio.run(serve(args.host, args.port))


async def serve(host: str, port: int) -> None:
    current_async_library_cvar.set("asyncio")
    server_config = uvicorn.Config(
        app,
        host=host,
        port=port,
        ws="auto",
        log_level="error",
        ws_per_message_deflate=True,
    )
    await uvicorn.Server(server_config).serve()


def assert_app_callbacks() -> None:
    if (
        not config.code.on_chat_start
        and not config.code.on_message
        and not config.code.on_audio_chunk
    ):
        raise RuntimeError(
            "Configure at least one of on_chat_start, on_message, or on_audio_chunk."
        )


if __name__ == "__main__":
    main()
