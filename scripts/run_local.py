from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from terminal_control import shutdown_message

RESTART_MARKER = Path(".tmp") / "restart-local"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Chainlit UI and local worker together.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default="7862")
    parser.add_argument("--machine-id", default="")
    parser.add_argument("--backends", default="")
    args = parser.parse_args()

    env_args = []
    if args.machine_id:
        subprocess_env_machine_id = args.machine_id
    else:
        subprocess_env_machine_id = ""
    if args.backends:
        subprocess_env_backends = args.backends
    else:
        subprocess_env_backends = ""

    if subprocess_env_machine_id:
        os.environ["MACHINE_ID"] = subprocess_env_machine_id
    if subprocess_env_backends:
        os.environ["AGENT_BACKENDS"] = subprocess_env_backends

    if args.machine_id:
        env_args.extend(["--machine-id", args.machine_id])
    if args.backends:
        env_args.extend(["--backends", args.backends])

    while True:
        if RESTART_MARKER.exists():
            RESTART_MARKER.unlink()
        worker = subprocess.Popen(
            [sys.executable, "scripts/run_worker.py", *env_args],
            stdin=subprocess.DEVNULL,
        )
        try:
            chainlit = subprocess.Popen(
                [
                    sys.executable,
                    "scripts/run_chainlit.py",
                    "src/chat_orchestrate/chainlit_app.py",
                    "--host",
                    args.host,
                    "--port",
                    args.port,
                ]
            )
            chainlit.wait()
        except KeyboardInterrupt:
            shutdown_message()
            return
        finally:
            for process in [locals().get("chainlit"), worker]:
                if process and process.poll() is None:
                    process.terminate()
            for process in [locals().get("chainlit"), worker]:
                if process:
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
        if RESTART_MARKER.exists():
            print("Restart marker detected. Relaunching local UI and worker...")
            continue
        return


if __name__ == "__main__":
    main()
