"""Offline by default; live suites must be explicitly selected with --live."""

import os

import httpx
import pytest

from paper_search_mcp import config


def pytest_sessionstart(session):
    # Never read the developer's credentials during ordinary collection/imports.
    if session.config.getoption("--live"):
        return
    patch = pytest.MonkeyPatch()
    session.config._offline_env = patch
    for name in os.environ:
        if name.startswith("PAPER_SEARCH_MCP_") or name.endswith(("API_KEY", "INST_TOKEN", "ACCESS_TOKEN")) or name == "UNPAYWALL_EMAIL":
            patch.delenv(name)
    patch.setattr(config, "_ENV_LOADED", True)


def pytest_sessionfinish(session):
    if patch := getattr(session.config, "_offline_env", None):
        patch.undo()


def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", help="Collect live provider suites (may consume API quota)")


def pytest_ignore_collect(collection_path, config):
    return "live" in collection_path.parts and not config.getoption("--live")


@pytest.fixture(autouse=True)
def offline_network(request, monkeypatch):
    if "live" in request.path.parts and request.config.getoption("--live"):
        return

    def blocked(*args, **kwargs):
        raise AssertionError("Unexpected network access in an offline test; inject a mock provider")

    monkeypatch.setattr("socket.getaddrinfo", blocked)
    monkeypatch.setattr("socket.create_connection", blocked)
    monkeypatch.setattr("socket.socket.connect", blocked)



@pytest.fixture
def isolated_env(monkeypatch):
    for name in os.environ:
        if name.startswith("PAPER_SEARCH_MCP_") or name.endswith(("API_KEY", "INST_TOKEN")):
            monkeypatch.delenv(name)
    monkeypatch.setattr(config, "_ENV_LOADED", True)


@pytest.fixture
def mock_http(monkeypatch):
    """Queue HTTP responses/errors; fail on any unexpected network request."""
    class MockHTTP:
        def __init__(self):
            self.responses = []
            self.requests = []

        def __call__(self, request):
            self.requests.append(request)
            assert self.responses, f"Unexpected request to {request.url.host}{request.url.path}"
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response(request) if callable(response) else response

    mock = MockHTTP()
    client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: client(
        *args, transport=httpx.MockTransport(mock), **kwargs,
    ))
    return mock
