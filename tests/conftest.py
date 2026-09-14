"""Opt-in fixtures for deterministic connector tests."""

import os

import httpx
import pytest

from paper_search_mcp import config


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
