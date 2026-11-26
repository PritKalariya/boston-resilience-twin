import socket

import pytest
from conftest import NetworkBlocked

from component_template.greeting import describe


def test_describe_names_the_component():
    assert describe("Serving") == "component Serving is wired into CI"


def test_describe_strips_the_name():
    assert describe("  Agents \n") == "component Agents is wired into CI"


@pytest.mark.parametrize("empty", ["", "   "])
def test_describe_rejects_an_empty_name(empty):
    with pytest.raises(ValueError, match="empty"):
        describe(empty)


def test_the_network_is_blocked():
    with pytest.raises(NetworkBlocked):
        socket.getaddrinfo("example.com", 80)
    with pytest.raises(NetworkBlocked):
        socket.socket().connect(("203.0.113.1", 80))
