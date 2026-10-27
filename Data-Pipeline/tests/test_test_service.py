"""The compose test service (#38) gives the tests no credentials and no network.

conftest.py already fails a test that tries the network; these check the container under it,
so a change to docker-compose.yaml that passed .env, a key or a network in would fail CI.
They run only in the test service (BRT_TEST_SERVICE=1).
"""

import _socket
import errno
import os
import socket
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("BRT_TEST_SERVICE") != "1", reason="checks the compose test service only")

REPO = Path(__file__).resolve().parents[2]
SECRETS = ("NOAA_CDO_TOKEN", "CENSUS_API_KEY", "NWS_USER_AGENT", "SLACK_ALERT_WEBHOOK",
           "GOOGLE_APPLICATION_CREDENTIALS", "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN")
TEST_NET = ("192.0.2.1", 443)  # RFC 5737 documentation address: never a real host


def test_no_keys_or_database_url_reach_the_tests():
    assert [name for name in SECRETS if os.environ.get(name)] == []


def test_no_env_file_or_google_credentials_are_mounted():
    assert not (REPO / ".env").exists()
    assert not (Path.home() / ".config" / "gcloud").exists()


def test_no_data_folder_contents_are_visible():
    # An empty folder hides Data-Pipeline/data, so a laptop run sees what CI sees.
    assert list((REPO / "Data-Pipeline" / "data").iterdir()) == []


def test_the_container_has_no_network():
    # The real connect, under conftest.py's guard: with only loopback there is no route at all.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2)
    try:
        with pytest.raises(OSError) as raised:
            _socket.socket.connect(sock, TEST_NET)
    finally:
        sock.close()
    assert raised.value.errno == errno.ENETUNREACH, raised.value
