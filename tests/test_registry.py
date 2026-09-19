"""Offline source contracts, lazy factories and MCP/CLI capability parity."""

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

from paper_search_mcp import cli, registry

pytestmark = pytest.mark.usefixtures("isolated_env")


def fresh_server(monkeypatch):
    name = "paper_search_mcp._registry_test_server"
    spec = importlib.util.spec_from_file_location(name, Path(cli.__file__).with_name("server.py"))
    server = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, server)
    spec.loader.exec_module(server)
    return server


@pytest.mark.parametrize("configuration", [(), ("SERPAPI_API_KEY", "UNPAYWALL_EMAIL"),
    tuple({key for source in registry.SOURCES.values() for key in source.required + source.optional})])
@pytest.mark.parametrize("prefix", ["", "PAPER_SEARCH_MCP_"])
def test_configuration_parity_without_importing_or_constructing_providers(monkeypatch, capsys, configuration, prefix):
    for name in configuration:
        monkeypatch.setenv(prefix + name, "fixture-value")
    construct = Mock(side_effect=AssertionError("Listing constructed a provider"))
    monkeypatch.setattr(registry.Source, "create", construct)
    server = fresh_server(monkeypatch)
    monkeypatch.setattr(cli, "SEARCHERS", {})
    cli._init_searchers()
    assert set(server.ALL_SOURCES) == set(cli.SEARCHERS) == set(registry.available_sources())
    assert asyncio.run(cli.cmd_sources(argparse.Namespace(details=False))) == 0
    assert json.loads(capsys.readouterr().out) == {"sources": sorted(server.ALL_SOURCES)}
    assert asyncio.run(cli.cmd_sources(argparse.Namespace(details=True))) == 0
    expected = [source.model_dump() for source in asyncio.run(server.list_sources())]
    assert json.loads(capsys.readouterr().out) == expected
    assert all(not source["access_verified"] for source in expected)
    assert "fixture-value" not in json.dumps(expected)
    for name in ("ieee", "acm", "base", "chemrxiv", "scihub"):
        assert name not in server.ALL_SOURCES
    assert "list_sources" in {tool.name for tool in asyncio.run(server.mcp.list_tools())}
    construct.assert_not_called()


def test_stub_configuration_never_means_implemented_or_verified(monkeypatch):
    for name in ("ieee", "acm"):
        source = registry.SOURCES[name]
        monkeypatch.setenv("PAPER_SEARCH_MCP_" + source.required[0], "fixture")
        info = source.describe()
        assert info.configured
        assert not info.implemented and not info.available and not info.access_verified
        assert info.query_modes == []
        assert not info.lookup and not info.read and not info.download
        with pytest.raises(NotImplementedError, match="stubs"):
            source.create()


def test_required_configuration_and_doi_only_lookup(monkeypatch):
    source = registry.SOURCES["unpaywall"]
    assert source.describe().query_modes == ["doi_lookup"]
    assert source.describe().lookup
    assert not source.describe().available
    with pytest.raises(ValueError, match="PAPER_SEARCH_MCP_UNPAYWALL_EMAIL"):
        source.create()
    monkeypatch.setenv("UNPAYWALL_EMAIL", "fixture@example.test")
    assert source.describe().available
    monkeypatch.setenv("PAPER_SEARCH_MCP_UNPAYWALL_EMAIL", "  ")
    assert not source.describe().available  # An empty prefixed value masks its alias.


def test_lazy_factory_constructs_once_under_concurrency():
    instance = Mock()
    factory = Mock(return_value=instance)
    handle = registry.LazyProvider(factory)
    factory.assert_not_called()
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(lambda _: handle.search, range(20))) == [instance.search] * 20
    factory.assert_called_once_with()


def test_cli_search_initializes_only_requested_provider(monkeypatch, capsys):
    factory = Mock(return_value=Mock(search=Mock(return_value=[])))
    monkeypatch.setattr(registry.Source, "create", factory)
    monkeypatch.setattr(cli, "SEARCHERS", {})
    args = argparse.Namespace(query="fixture", sources="arxiv", max_results=2, year=None)
    assert asyncio.run(cli.cmd_search(args)) == 0
    assert json.loads(capsys.readouterr().out)["sources_used"] == ["arxiv"]
    factory.assert_called_once_with()


def test_registry_covers_every_concrete_adapter_and_factory():
    modules = {path.stem for path in Path(cli.__file__).with_name("academic_platforms").glob("*.py")}
    modules -= {"__init__", "base", "oaipmh"}  # Shared infrastructure, not standalone sources.
    assert {source.factory.split(":")[0] for source in registry.SOURCES.values()} == modules
    for source in registry.SOURCES.values():
        module, name = source.factory.split(":")
        cls = getattr(__import__(f"paper_search_mcp.academic_platforms.{module}", fromlist=[name]), name)
        assert callable(cls)
        assert source.limitations and source.pagination


def test_configured_stub_legacy_tools_raise_clear_errors(monkeypatch):
    monkeypatch.setenv("PAPER_SEARCH_MCP_IEEE_API_KEY", "fixture")
    monkeypatch.setenv("PAPER_SEARCH_MCP_ACM_API_KEY", "fixture")
    server = fresh_server(monkeypatch)
    names = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
    for source in ("ieee", "acm"):
        assert {f"search_{source}", f"download_{source}", f"read_{source}_paper"} <= names
        with pytest.raises(NotImplementedError, match="stubs"):
            asyncio.run(getattr(server, f"search_{source}")("fixture"))


def test_unavailable_legacy_wrapper_does_not_return_empty_success(monkeypatch):
    server = fresh_server(monkeypatch)
    with pytest.raises(NotImplementedError, match="incorrectly treats"):
        asyncio.run(server.search_base("fixture"))
    with pytest.raises(ValueError, match="CORE_API_KEY"):
        asyncio.run(server.search_core("fixture"))
