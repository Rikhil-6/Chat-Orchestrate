from __future__ import annotations

import sys
import threading
from collections.abc import Callable


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
