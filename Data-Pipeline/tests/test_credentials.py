from pathlib import Path

import pytest

from credentials import KEYS, MissingCredentialError, require


def test_returns_value(monkeypatch):
    monkeypatch.setenv("NOAA_CDO_TOKEN", "abc123")
    assert require("NOAA_CDO_TOKEN") == "abc123"


def test_strips_trailing_newline(monkeypatch):
    monkeypatch.setenv("CENSUS_API_KEY", "abc123\n")
    assert require("CENSUS_API_KEY") == "abc123"


def test_keeps_inner_spaces(monkeypatch):
    monkeypatch.setenv("NWS_USER_AGENT", "(boston-resilience-twin, someone@example.org)")
    assert require("NWS_USER_AGENT") == "(boston-resilience-twin, someone@example.org)"


@pytest.mark.parametrize("name", KEYS)
def test_missing_names_the_key(monkeypatch, name):
    monkeypatch.delenv(name, raising=False)
    with pytest.raises(MissingCredentialError, match=f"^{name} is not set"):
        require(name)


@pytest.mark.parametrize("value", ["", "   ", "\n"])
def test_empty_counts_as_missing(monkeypatch, value):
    monkeypatch.setenv("NOAA_CDO_TOKEN", value)
    with pytest.raises(MissingCredentialError, match="NOAA_CDO_TOKEN"):
        require("NOAA_CDO_TOKEN")


def test_error_does_not_leak_other_values(monkeypatch):
    monkeypatch.setenv("CENSUS_API_KEY", "secret-census-value")
    monkeypatch.delenv("NOAA_CDO_TOKEN", raising=False)
    with pytest.raises(MissingCredentialError) as excinfo:
        require("NOAA_CDO_TOKEN")
    assert "secret-census-value" not in str(excinfo.value)


def test_every_key_is_in_env_example():
    example = Path(__file__).resolve().parents[2] / ".env.example"
    lines = example.read_text().splitlines()
    names = {line.split("=", 1)[0] for line in lines if "=" in line and not line.startswith("#")}
    assert set(KEYS) <= names
