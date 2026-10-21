"""Run the mock API in a background thread on a free local port.

Used by the tests and by ``demo`` so that the runner talks to the mock over a
real TCP socket - timeouts, disconnects and concurrency are then real, not
simulated by an in-memory transport.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import uvicorn
from fastapi import FastAPI

HOST = "127.0.0.1"


@contextmanager
def serve_in_thread(app: FastAPI, host: str = HOST, startup_timeout: float = 10.0) -> Iterator[str]:
    """Start ``app`` with uvicorn and yield its base URL; stop it on exit."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind((host, 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(app, log_level="warning", lifespan="off", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + startup_timeout
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("mock API did not start")
        time.sleep(0.01)
    try:
        yield f"http://{host}:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
