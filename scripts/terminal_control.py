from __future__ import annotations

import asyncio
import sys
import threading
from collections.abc import Callable


def install_clean_asyncio_exception_handler() -> None:
    loop = asyncio.get_running_loop()
    default_handler = loop.get_exception_handler()

    def handle(loop: asyncio.AbstractEventLoop, context: dict) -> None:
        exception = context.get("exception")
        if is_remote_connection_reset(exception):
            return
        if default_handler:
            default_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(handle)


def is_remote_connection_reset(exception: object) -> bool:
    if not isinstance(exception, ConnectionResetError):
        return False
    return getattr(exception, "winerror", None) == 10054 or getattr(exception, "errno", None) == 10054


def start_q_listener(on_quit: Callable[[], None]) -> threading.Thread | None:
    if not sys.stdin or not sys.stdin.isatty():
        return None

    def watch() -> None:
        while True:
            line = sys.stdin.readline()
            if not line:
                return
            if line.strip().lower() == "q":
                on_quit()
                return

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    return thread


def shutdown_message() -> None:
    print("Shutting down cleanly...")
