import socket
import urllib.request

import pytest

from conftest import NetworkBlocked


def test_connect_is_blocked():
    # A raw connect to an address skips the lookup, so this reaches the connect guard.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(NetworkBlocked, match="connect to"):
            sock.connect(("192.0.2.1", 443))


def test_lookup_is_blocked():
    with pytest.raises(NetworkBlocked, match="example.org"):
        socket.getaddrinfo("example.org", 443)


def test_http_request_is_blocked():
    with pytest.raises(NetworkBlocked):
        urllib.request.urlopen("https://example.org", timeout=1)


def test_loopback_is_allowed():
    # What socket.socketpair() and asyncio do on Windows, where there is no AF_UNIX.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.connect(server.getsockname())


def test_socketpair_works():
    left, right = socket.socketpair()
    with left, right:
        left.sendall(b"ok")
        assert right.recv(2) == b"ok"
