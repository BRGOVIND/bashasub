"""Helpers for the Phase 6 preview tests: real servers, raw requests,
workspace setup and Docker inspection. Not a test module."""

from __future__ import annotations

import http.client
import json
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import uvicorn

from redstone.domain.models import Framework
from redstone.workspace.manager import WorkspaceManager

FIXTURES = Path(__file__).parent / "fixtures"
HOSTILE_APP = FIXTURES / "preview_app"
VITE_APP = FIXTURES / "preview_vite"
REACT = Framework.REACT_VITE_TS


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class LiveServer:
    """A real uvicorn server on loopback, in a thread -- so requests travel
    over real sockets with exactly the bytes the test sends."""

    def __init__(self, app, port: int | None = None) -> None:
        self.port = port or free_port()
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port,
                                                    log_level="warning", lifespan="on",
                                                    server_header=False))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> "LiveServer":
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("server did not start")
            time.sleep(0.05)
        return self

    def __exit__(self, *exc) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=15)


def raw_request(port: int, host: str, path: str, *, method: str = "GET",
                headers: dict | None = None, body: bytes | None = None,
                timeout: float = 20.0) -> tuple[int, list[tuple[str, str]], bytes]:
    """Send the request line byte-for-byte (no client-side normalisation of
    `..` or percent-encoding) with an explicit Host header."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        connection.putheader("Host", host)
        for name, value in (headers or {}).items():
            connection.putheader(name, value)
        if body is not None:
            connection.putheader("Content-Length", str(len(body)))
        connection.endheaders(body)
        response = connection.getresponse()
        return response.status, response.getheaders(), response.read()
    finally:
        connection.close()


def header(headers: list[tuple[str, str]], name: str) -> list[str]:
    return [v for k, v in headers if k.lower() == name.lower()]


def make_workspace(root: Path, workspace_id: str, source: Path = HOSTILE_APP, *,
                   identity: str | None = None, extra: dict[str, str] | None = None):
    workspace = WorkspaceManager(root / "workspaces").create(workspace_id)
    for item in source.iterdir():
        if item.is_file():
            shutil.copy(item, workspace.project_root / item.name)
    if identity is not None:
        (workspace.project_root / "identity.txt").write_text(identity, encoding="utf-8")
    for name, content in (extra or {}).items():
        (workspace.project_root / name).write_text(content, encoding="utf-8")
    (workspace.root / "outside-secret.txt").write_text("HOST-SIDE-SECRET", encoding="utf-8")
    return workspace


def docker(*args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def inspect(name: str) -> dict:
    return json.loads(docker("inspect", name).stdout)[0]


def network(name: str) -> dict:
    return json.loads(docker("network", "inspect", name).stdout)[0]


def managed() -> tuple[set[str], set[str]]:
    containers = set(docker("ps", "-a", "--filter", "label=redstone.managed=true",
                            "--format", "{{.Names}}").stdout.split())
    networks = set(docker("network", "ls", "--filter", "label=redstone.managed=true",
                          "--format", "{{.Name}}").stdout.split())
    return containers, networks


def preview_host(preview_id: str, port: int, domain: str = "localhost") -> str:
    return f"{preview_id}.{domain}:{port}"


def get_json(port: int, host: str, path: str, timeout: float = 40.0):
    status, _, body = raw_request(port, host, path, timeout=timeout)
    assert status == 200, (status, body[:300])
    return json.loads(body)
