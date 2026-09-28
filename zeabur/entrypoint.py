#!/usr/bin/env python3
"""Run the player and MCP server behind Zeabur's single public port."""

from __future__ import annotations

import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path


APP_ROOT = Path("/app")
SERVER_DIR = APP_ROOT / "server"
PERSIST_DIR = Path(os.environ.get("MUSIC_DATA_DIR", "/data/music"))


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    if "\n" in value or "\r" in value:
        raise RuntimeError(f"{name} must be a single line")
    return value


def write_secret_files() -> None:
    music_u = required_env("MUSIC_U")
    player_token = required_env("PLAYER_TOKEN")

    if not music_u.startswith("MUSIC_U="):
        music_u = f"MUSIC_U={music_u}"

    credential_file = SERVER_DIR / ".netease_cred"
    token_file = SERVER_DIR / ".secret"
    credential_file.write_text(music_u + "\n", encoding="utf-8")
    token_file.write_text(player_token + "\n", encoding="utf-8")
    credential_file.chmod(0o600)
    token_file.chmod(0o600)


def prepare_persistent_data() -> None:
    """Move the player's mutable data to the mounted Zeabur volume."""
    source = SERVER_DIR / "data"
    PERSIST_DIR.mkdir(parents=True, exist_ok=True)

    if source.is_symlink():
        source.unlink()
    elif source.exists():
        for item in source.iterdir():
            destination = PERSIST_DIR / item.name
            if destination.exists():
                continue
            if item.is_dir():
                shutil.copytree(item, destination)
            else:
                shutil.copy2(item, destination)
        shutil.rmtree(source)

    source.symlink_to(PERSIST_DIR, target_is_directory=True)


def wait_for_port(port: int, process: subprocess.Popen[bytes], timeout: int = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Child process exited before port {port} became ready")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.25)
    raise RuntimeError(f"Timed out waiting for port {port}")


def nginx_config(public_port: int, path_secret: str) -> str:
    mcp_prefix = f"/mcp-music-{path_secret}/"
    return f"""
worker_processes 1;
pid /tmp/nginx.pid;
error_log /dev/stderr info;

events {{ worker_connections 1024; }}

http {{
    access_log /dev/stdout;
    proxy_temp_path /tmp/nginx_proxy;
    client_body_temp_path /tmp/nginx_body;
    client_max_body_size 10m;

    server {{
        listen {public_port};

        location = /healthz {{
            proxy_pass http://127.0.0.1:18012/healthz;
        }}

        location ^~ {mcp_prefix} {{
            rewrite ^{mcp_prefix}(.*)$ /$1 break;
            proxy_pass http://127.0.0.1:18012;
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_buffering off;
            proxy_read_timeout 1800s;
        }}

        location / {{
            proxy_pass http://127.0.0.1:9090;
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_buffering off;
            proxy_read_timeout 3600s;
        }}
    }}
}}
""".strip() + "\n"


def terminate_all(processes: list[subprocess.Popen[bytes]]) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()
    deadline = time.monotonic() + 8
    for process in processes:
        remaining = max(0.1, deadline - time.monotonic())
        if process.poll() is None:
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                process.kill()


def main() -> int:
    public_port = int(os.environ.get("PORT", "8080"))
    path_secret = required_env("MCP_PATH_SECRET")
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,80}", path_secret):
        raise RuntimeError(
            "MCP_PATH_SECRET must contain 20-80 letters, digits, underscores, or hyphens"
        )

    write_secret_files()
    prepare_persistent_data()

    child_env = os.environ.copy()
    child_env.update(
        {
            "HOST": "127.0.0.1",
            "PORT": "9090",
            "MCP_HOST": "127.0.0.1",
            "MCP_PORT": "18012",
            "MUSIC_BASE": "http://127.0.0.1:9090",
            "MUSIC_GATEWAY_TOKEN": os.environ.get(
                "MUSIC_GATEWAY_TOKEN", secrets.token_urlsafe(32)
            ),
        }
    )

    public_base = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if public_base:
        child_env.setdefault("MUSIC_CARD_BASE", public_base)
        child_env.setdefault("MUSIC_PUBLIC_URL", public_base)

    config_path = Path("/tmp/nginx.conf")
    config_path.write_text(nginx_config(public_port, path_secret), encoding="utf-8")

    processes: list[subprocess.Popen[bytes]] = []

    def stop(_signum: int, _frame: object) -> None:
        terminate_all(processes)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        player = subprocess.Popen(
            [sys.executable, "music.py"], cwd=SERVER_DIR, env=child_env
        )
        processes.append(player)
        wait_for_port(9090, player)

        mcp = subprocess.Popen(
            [sys.executable, "music_mcp.py"], cwd=APP_ROOT / "mcp", env=child_env
        )
        processes.append(mcp)
        wait_for_port(18012, mcp)

        nginx = subprocess.Popen(
            ["nginx", "-c", str(config_path), "-g", "daemon off;"]
        )
        processes.append(nginx)
        print("Player and MCP are ready behind the Zeabur gateway.", flush=True)

        while True:
            for process in processes:
                code = process.poll()
                if code is not None:
                    raise RuntimeError(f"A child process exited with status {code}")
            time.sleep(1)
    finally:
        terminate_all(processes)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Startup failed: {exc}", file=sys.stderr, flush=True)
        raise

