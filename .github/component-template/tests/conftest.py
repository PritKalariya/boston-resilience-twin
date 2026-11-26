"""Tests use fixtures and fakes only, never the network (#21, #159).

CI also runs the container with `--network none`; this guard fails a test that tries to
connect with a clear message even when the tests run somewhere that has a network. It is
the same guard as Data-Pipeline/tests/conftest.py: Unix sockets and loopback stay allowed.
"""

import socket

import pytest


class NetworkBlocked(RuntimeError):
    pass


LOOPBACK = {"127.0.0.1", "::1"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    real_connect = socket.socket.connect
    real_lookup = socket.getaddrinfo

    def guarded_connect(sock, address):
        if sock.family == getattr(socket, "AF_UNIX", None):
            return real_connect(sock, address)
        if isinstance(address, tuple) and address[0] in LOOPBACK:
            return real_connect(sock, address)
        raise NetworkBlocked(f"test tried to connect to {address!r}; use a fixture file or a fake instead")

    def blocked_lookup(host, *args, **kwargs):
        if host in LOOPBACK:
            return real_lookup(host, *args, **kwargs)
        raise NetworkBlocked(f"test tried to look up {host!r}; use a fixture file or a fake instead")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", blocked_lookup)
