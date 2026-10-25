import socket
import urllib.request

import pytest

from conftest import NetworkBlocked


def test_connect_is_blocked():
    with pytest.raises(NetworkBlocked, match="fixture file"):
        socket.create_connection(("192.0.2.1", 443), timeout=1)


def test_lookup_is_blocked():
    with pytest.raises(NetworkBlocked, match="example.org"):
        socket.getaddrinfo("example.org", 443)


def test_http_request_is_blocked():
    with pytest.raises(NetworkBlocked):
        urllib.request.urlopen("https://example.org", timeout=1)
