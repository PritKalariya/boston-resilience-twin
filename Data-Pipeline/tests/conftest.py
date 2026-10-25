import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


class NetworkBlocked(RuntimeError):
    pass


LOOPBACK = {"127.0.0.1", "::1"}


# Tests use fixture files and fakes only, never a live API or Cloud Storage
# (#21). Local Unix sockets and loopback stay allowed: on Windows, which has no
# AF_UNIX, socket.socketpair() and asyncio (gcsfs, used by DVC) connect over
# 127.0.0.1.
@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    real_connect = socket.socket.connect

    def guarded_connect(sock, address):
        if sock.family == getattr(socket, "AF_UNIX", None):
            return real_connect(sock, address)
        if isinstance(address, tuple) and address[0] in LOOPBACK:
            return real_connect(sock, address)
        raise NetworkBlocked(f"test tried to connect to {address!r}; use a fixture file or a fake instead")

    def blocked_lookup(host, *args, **kwargs):
        raise NetworkBlocked(f"test tried to look up {host!r}; use a fixture file or a fake instead")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", blocked_lookup)
