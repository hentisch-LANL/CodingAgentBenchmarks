#!/usr/bin/env python3
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


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    proxy_socket: socket.socket | None = None

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
        write_frame(self.proxy_socket, payload)
        response = json.loads(read_frame(self.proxy_socket).decode())
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
    proxy_fd = int(os.environ["ABC_BENCH_PROXY_FD"])
    proxy_port = int(os.environ.get("ABC_BENCH_SANDBOX_PROXY_PORT", "18080"))
    harness_cmd = json.loads(os.environ["ABC_BENCH_SANDBOX_HARNESS_CMD"])
    verify_cmd = json.loads(os.environ["ABC_BENCH_SANDBOX_VERIFY_CMD"])
    harness_log = Path(os.environ["ABC_BENCH_SANDBOX_HARNESS_LOG"])
    verify_log = Path(os.environ["ABC_BENCH_SANDBOX_VERIFY_LOG"])

    harness_log.parent.mkdir(parents=True, exist_ok=True)
    verify_log.parent.mkdir(parents=True, exist_ok=True)

    proxy_socket = socket.socket(fileno=proxy_fd)
    Handler.proxy_socket = proxy_socket
    server = ThreadingHTTPServer(("127.0.0.1", proxy_port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        with open(harness_log, "w") as harness_log_file:
            harness = subprocess.run(harness_cmd, stdout=harness_log_file, stderr=subprocess.STDOUT, text=True)
        if harness.returncode != 0:
            return harness.returncode

        with open(verify_log, "w") as verify_log_file:
            verify = subprocess.run(verify_cmd, stdout=verify_log_file, stderr=subprocess.STDOUT, text=True)
        return verify.returncode
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        proxy_socket.close()


if __name__ == "__main__":
    raise SystemExit(main())
