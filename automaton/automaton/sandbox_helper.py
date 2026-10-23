"""Sandbox-side helper script template for loopback-only benchmark execution."""

from __future__ import annotations


SCRIPT = r'''#!/usr/bin/env python3
import json
import os
import socket
import struct
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def read_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise EOFError("unexpected EOF on proxy socket")
        chunks.extend(chunk)
    return bytes(chunks)


def read_frame(sock: socket.socket) -> bytes:
    size = struct.unpack("!Q", read_exact(sock, 8))[0]
    return read_exact(sock, size)


def write_frame(sock: socket.socket, payload: bytes) -> None:
    sock.sendall(struct.pack("!Q", len(payload)))
    sock.sendall(payload)


class ProxyFailure(RuntimeError):
    pass


def stream_process(cmd: list[str], log_path: Path) -> int:
    with open(log_path, "w") as log_file:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            log_file.write(line)
            log_file.flush()
            sys.stdout.write(line)
            sys.stdout.flush()
        return process.wait()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    proxy_socket: socket.socket | None = None
    proxy_lock = threading.Lock()

    def do_GET(self):
        self._proxy()

    def do_POST(self):
        self._proxy()

    def do_PUT(self):
        self._proxy()

    def do_PATCH(self):
        self._proxy()

    def do_DELETE(self):
        self._proxy()

    def handle_one_request(self):
        try:
            return super().handle_one_request()
        except ProxyFailure as exc:
            sys.stdout.write(f"[model-proxy] {exc}\n")
            sys.stdout.flush()
            os._exit(86)

    def log_message(self, fmt, *args):
        return

    def _proxy(self):
        assert self.proxy_socket is not None
        content_length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(content_length) if content_length else b""
        payload = json.dumps(
            {
                "method": self.command,
                "path": self.path,
                "headers": dict(self.headers),
                "body": body.decode("latin1"),
            }
        ).encode()
        with self.proxy_lock:
            try:
                write_frame(self.proxy_socket, payload)
                response = json.loads(read_frame(self.proxy_socket).decode())
            except (EOFError, OSError, json.JSONDecodeError) as exc:
                raise ProxyFailure(f"sandbox proxy transport failed: {type(exc).__name__}: {exc}") from exc
        response_body = str(response.get("body", "")).encode("latin1")
        self.send_response(int(response["status"]), str(response["reason"]))
        for key, value in dict(response.get("headers", {})).items():
            if key.lower() != "content-length":
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        if response_body:
            self.wfile.write(response_body)
            self.wfile.flush()


def main() -> int:
    subprocess.run(["ip", "link", "set", "lo", "up"], check=True)
    for key in [
        "http_proxy",
        "https_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "all_proxy",
        "no_proxy",
        "NO_PROXY",
    ]:
        os.environ.pop(key, None)

    proxy_fd = int(os.environ["AUTOMATON_PROXY_FD"])
    proxy_port = int(os.environ.get("AUTOMATON_SANDBOX_PROXY_PORT", "18080"))
    run_cmd = json.loads(os.environ["AUTOMATON_SANDBOX_CMD"])
    run_log = Path(os.environ["AUTOMATON_SANDBOX_LOG"])

    run_log.parent.mkdir(parents=True, exist_ok=True)

    proxy_socket = socket.socket(fileno=proxy_fd)
    Handler.proxy_socket = proxy_socket
    server = ThreadingHTTPServer(("127.0.0.1", proxy_port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        return stream_process(run_cmd, run_log)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        proxy_socket.close()


if __name__ == "__main__":
    raise SystemExit(main())
'''
