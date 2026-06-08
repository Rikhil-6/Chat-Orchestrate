from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from terminal_control import shutdown_message

RESTART_MARKER = Path(".tmp") / "restart-local"


def session_path(port: str) -> Path:
    return Path(".tmp") / f"local-session-{port}.json"


def lock_port(port: str) -> int:
    try:
        numeric_port = int(port)
    except ValueError:
        numeric_port = 7862
    return 30000 + (numeric_port % 20000)


def acquire_lock(port: str) -> socket.socket | None:
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", lock_port(port)))
        lock.listen(1)
        return lock
    except OSError:
        lock.close()
        return None


def process_exists(pid: int) -> bool:
    if pid <= 0 or pid == os.getpid():
        return False
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def stop_process_tree(pid: int) -> None:
    if not process_exists(pid):
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
        return
    try:
        os.kill(pid, 15)
    except OSError:
        return


def cleanup_previous_session(path: Path) -> None:
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        payload = {}
    pids = [int(pid) for pid in payload.get("pids", []) if str(pid).isdigit()]
    for pid in pids:
        stop_process_tree(pid)
    for pid_file in Path(".tmp").glob("hosted-coordinator-*.pid"):
        try:
            pid_text = pid_file.read_text(encoding="utf-8").strip()
            if pid_text.isdigit():
                stop_process_tree(int(pid_text))
            pid_file.unlink()
        except OSError:
            pass
    try:
        path.unlink()
    except OSError:
        pass


def write_session(path: Path, *processes: subprocess.Popen | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pids = [os.getpid(), *[process.pid for process in processes if process]]
    path.write_text(json.dumps({"launcher": os.getpid(), "pids": pids}, indent=2), encoding="utf-8")


def clear_session(path: Path) -> None:
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        payload = {}
    if payload.get("launcher") == os.getpid():
        path.unlink()


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

    local_session = session_path(str(args.port))
    cleanup_previous_session(local_session)
    local_lock = acquire_lock(str(args.port))
    if local_lock is None:
        print(
            f"Another Chat Orchestrate local launcher is already starting or running for port {args.port}. "
            "Close that session first, then run this command again."
        )
        return

    while True:
        if RESTART_MARKER.exists():
            RESTART_MARKER.unlink()
        worker = subprocess.Popen(
            [sys.executable, "scripts/run_worker.py", *env_args],
            stdin=subprocess.DEVNULL,
        )
        chainlit = None
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
            write_session(local_session, chainlit, worker)
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
            clear_session(local_session)
        if RESTART_MARKER.exists():
            print("Restart marker detected. Relaunching local UI and worker...")
            continue
        return


if __name__ == "__main__":
    main()
