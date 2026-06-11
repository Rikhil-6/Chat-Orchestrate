from __future__ import annotations

import argparse
import functools
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from terminal_control import shutdown_message, start_q_listener


class FrontendPreviewHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, directory: str, api_base: str, **kwargs):
        self.api_base = api_base
        super().__init__(*args, directory=directory, **kwargs)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"", "/", "/index.html"}:
            self._send_index()
            return
        super().do_GET()

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format: str, *args) -> None:
        return

    def _send_index(self) -> None:
        index_path = Path(self.directory) / "index.html"
        html = index_path.read_text(encoding="utf-8")
        api_base_json = json.dumps(self.api_base)
        api_config = (
            "<script>\n"
            f"      window.FORGEHUB_API_BASE = {api_base_json};\n"
            f"      window.SEARCHLY_API_BASE = {api_base_json};\n"
            f"      window.CHAT_ORCHESTRATE_API_BASE = {api_base_json};\n"
            "    </script>"
        )
        app_script = '    <script src="./app.js"></script>'
        if app_script in html:
            html = html.replace(app_script, f"    {api_config}\n{app_script}", 1)
        else:
            html = html.replace("</body>", f"    {api_config}\n  </body>", 1)
        payload = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def resolve_workspace(value: str) -> Path:
    candidate = Path(value)
    if candidate.exists():
        return candidate.resolve()
    named = Path("workspaces") / value
    return named.resolve()


def find_available_port(preferred: int, host: str = "127.0.0.1", attempts: int = 20) -> int:
    for port in range(preferred, preferred + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return port
    return preferred


def start_backend(workspace: Path, host: str, port: int, frontend_port: int) -> subprocess.Popen:
    env = os.environ.copy()
    origins = [
        f"http://localhost:{frontend_port}",
        f"http://127.0.0.1:{frontend_port}",
    ]
    allowed_origins = ",".join(origins)
    env["FORGEHUB_CORS_ORIGINS"] = allowed_origins
    env["GOOGLE_LIKE_CORS_ORIGINS"] = allowed_origins
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "backend.app:app",
            "--host",
            host,
            "--port",
            str(port),
            "--no-access-log",
        ],
        cwd=workspace,
        env=env,
    )


def wait_for_backend(port: int, timeout_seconds: float = 10.0) -> bool:
    import httpx

    deadline = time.monotonic() + timeout_seconds
    url = f"http://127.0.0.1:{port}/api/health"
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=1.0, trust_env=False)
            if response.status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    return False


def backend_preview_endpoints(port: int) -> list[tuple[str, str]]:
    import httpx

    base_url = f"http://127.0.0.1:{port}"
    candidates = [
        ("Backend health", "/api/health"),
        ("Backend repos", "/api/repos"),
    ]
    available: list[tuple[str, str]] = []
    for label, path in candidates:
        try:
            response = httpx.get(f"{base_url}{path}", timeout=1.0, trust_env=False)
        except httpx.HTTPError:
            continue
        if response.status_code < 400:
            available.append((label, f"{base_url}{path}"))
    return available


def serve_frontend(frontend: Path, host: str, port: int, api_base: str, stop_event: threading.Event) -> None:
    handler = functools.partial(
        FrontendPreviewHandler,
        directory=str(frontend),
        api_base=api_base,
    )
    with ThreadingHTTPServer((host, port), handler) as server:
        server.timeout = 0.5
        while not stop_event.is_set():
            server.handle_request()


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview a generated Chat Orchestrate workspace.")
    parser.add_argument("--workspace", default="default", help="Workspace name or path.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--frontend-port", type=int, default=5173)
    parser.add_argument("--backend-port", type=int, default=8000)
    args = parser.parse_args()

    workspace = resolve_workspace(args.workspace)
    frontend = workspace / "frontend"
    backend = workspace / "backend"
    if not (frontend / "index.html").exists():
        raise SystemExit(f"No frontend/index.html found under {workspace}")
    if not (backend / "app.py").exists():
        raise SystemExit(f"No backend/app.py found under {workspace}")

    frontend_port = find_available_port(args.frontend_port, args.host)
    backend_port = find_available_port(args.backend_port, args.host)
    api_base = f"http://127.0.0.1:{backend_port}"
    stop_event = threading.Event()
    start_q_listener(stop_event.set)

    backend_process = start_backend(workspace, args.host, backend_port, frontend_port)
    try:
        if not wait_for_backend(backend_port):
            raise SystemExit("Backend did not answer /api/health in time.")
        frontend_thread = threading.Thread(
            target=serve_frontend,
            args=(frontend, args.host, frontend_port, api_base, stop_event),
            daemon=True,
        )
        frontend_thread.start()
        print(f"Workspace: {workspace}")
        print(f"Frontend: http://localhost:{frontend_port}")
        for label, url in backend_preview_endpoints(backend_port):
            print(f"{label}: {url}")
        print("Press q then Enter, or Ctrl-C, to stop preview.")
        while not stop_event.is_set():
            if backend_process.poll() is not None:
                raise SystemExit(f"Backend exited with code {backend_process.returncode}.")
            time.sleep(0.5)
    except KeyboardInterrupt:
        shutdown_message()
    finally:
        stop_event.set()
        if backend_process.poll() is None:
            backend_process.terminate()
            try:
                backend_process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                backend_process.kill()


if __name__ == "__main__":
    main()
