"""MCP/CLI registration and aggregate behavior for optional research APIs."""

import argparse
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from paper_search_mcp import cli
from paper_search_mcp.paper import Paper

pytestmark = pytest.mark.usefixtures("isolated_env")


@pytest.fixture
def interfaces(monkeypatch, mock_http):
    def load(scholar=False, scopus=False):
        if scholar:
            monkeypatch.setenv("PAPER_SEARCH_MCP_SERPAPI_API_KEY", "fake-serpapi")
        if scopus:
            monkeypatch.setenv("PAPER_SEARCH_MCP_SCOPUS_API_KEY", "fake-scopus")
        # A fresh module avoids reloading the server used by unrelated tests.
        name = "paper_search_mcp._test_server"
        path = Path(cli.__file__).with_name("server.py")
        spec = importlib.util.spec_from_file_location(name, path)
        server = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, server)
        spec.loader.exec_module(server)
        monkeypatch.setattr(cli, "SEARCHERS", {})
        cli._init_searchers()
        return server
    return load


def paper(source, doi="10.1000/shared"):
    return Paper(paper_id="123", title="Shared paper", authors=["Author"], abstract="Snippet",
                 doi=doi, published_date=None, url="https://publisher.test/paper", pdf_url="", source=source)


@pytest.mark.parametrize("scholar,scopus", [(False, False), (True, False), (False, True), (True, True)])
def test_conditional_registration_and_all_sources(interfaces, mock_http, scholar, scopus):
    server = interfaces(scholar, scopus)
    tools = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
    for source, enabled, names in [
        ("google_scholar", scholar, ["search_google_scholar"]),
        ("scopus", scopus, ["search_scopus", "read_scopus_paper", "download_scopus"]),
    ]:
        assert (source in server._parse_sources("all")) == enabled
        assert (source in cli._parse_sources("all")) == enabled
        assert (source in cli.SEARCHERS) == enabled
        assert all((name in tools) == enabled for name in names)
        assert server._parse_sources(source) == ([source] if enabled else [])
        assert cli._parse_sources(source) == ([source] if enabled else [])
    assert set(server._parse_sources("all")) == set(cli._parse_sources("all"))
    assert "sciencedirect" not in server.ALL_SOURCES
    assert not mock_http.requests  # Configuration/registration makes no API calls.


def test_cli_all_tracks_initialized_registry(interfaces, monkeypatch):
    interfaces()
    monkeypatch.setitem(cli.SEARCHERS, "another_optional_source", Mock())
    assert cli._parse_sources("all") == list(cli.SEARCHERS)


def test_mcp_all_dispatches_both_sources_and_deduplicates(interfaces, monkeypatch):
    server = interfaces(True, True)
    for source in server.ALL_SOURCES:
        if source not in {"google_scholar", "scopus"}:
            monkeypatch.setattr(server, f"search_{source}", AsyncMock(return_value=[]))
    scholar = Mock(return_value=[paper("google_scholar")])
    scopus = Mock(return_value=[paper("scopus")])
    monkeypatch.setattr(server.google_scholar_searcher, "search", scholar)
    monkeypatch.setattr(server.scopus_searcher, "search", scopus)
    result = asyncio.run(server.search_papers("query"))
    assert result["total"] == 1
    assert result["raw_total"] == 2
    assert result["source_results"]["scopus"] == result["source_results"]["google_scholar"] == 1
    assert result["errors"] == {}
    scholar.assert_called_once_with("query", max_results=5)
    scopus.assert_called_once_with("query", max_results=5)


@pytest.mark.parametrize("source", ["scopus", "google_scholar"])
def test_mcp_isolates_provider_errors(interfaces, monkeypatch, source):
    server = interfaces(True, True)
    for name in ("scopus", "google_scholar"):
        monkeypatch.setattr(getattr(server, f"{name}_searcher"), "search",
                            Mock(side_effect=RuntimeError("Provider unavailable")) if name == source
                            else Mock(return_value=[paper(name)]))
    result = asyncio.run(server.search_papers("query", sources="scopus,google_scholar"))
    assert result["errors"] == {source: "Provider unavailable"}
    assert result["source_results"][source] == 0
    assert result["total"] == 1


def test_cli_aggregate_and_error_isolation(interfaces, monkeypatch, capsys):
    interfaces(True, True)
    monkeypatch.setattr(cli.SEARCHERS["scopus"], "search", Mock(return_value=[paper("scopus")]))
    monkeypatch.setattr(cli.SEARCHERS["google_scholar"], "search", Mock(return_value=[paper("google_scholar")]))
    args = argparse.Namespace(query="query", sources="scopus,google_scholar", max_results=2, year=None)
    assert asyncio.run(cli.cmd_search(args)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["total"] == 1
    assert result["errors"] == {}
    monkeypatch.setattr(cli.SEARCHERS["google_scholar"], "search", Mock(side_effect=RuntimeError("Quota exceeded")))
    assert asyncio.run(cli.cmd_search(args)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["total"] == 1
    assert result["errors"] == {"google_scholar": "Quota exceeded"}


def test_cli_all_dispatches_configured_sources(interfaces, monkeypatch, capsys):
    interfaces(True, True)
    for source, searcher in cli.SEARCHERS.items():
        monkeypatch.setattr(searcher, "search", Mock(return_value=[paper(source)]))
    args = argparse.Namespace(query="query", sources="all", max_results=2, year=None)
    assert asyncio.run(cli.cmd_search(args)) == 0
    result = json.loads(capsys.readouterr().out)
    assert {"scopus", "google_scholar"} <= set(result["sources_used"])
    assert result["errors"] == {}
    assert result["total"] == 1


def test_scopus_tool_arguments_and_pdf_fallback_dispatch(interfaces, monkeypatch, tmp_path):
    server = interfaces(scopus=True)
    search = Mock(return_value=[])
    read = Mock(return_value="FULL TEXT\nThe article")
    path = tmp_path / "123.pdf"
    path.write_bytes(b"%PDF-1.7\nfixture")
    download = Mock(return_value=str(path))
    monkeypatch.setattr(server.scopus_searcher, "search", search)
    monkeypatch.setattr(server.scopus_searcher, "read_paper", read)
    monkeypatch.setattr(server.scopus_searcher, "download_pdf", download)
    assert asyncio.run(server.search_scopus("query", 26, "-citedby-count", "TITLE", "2020-")) == []
    search.assert_called_once_with("query", max_results=26, sort="-citedby-count", field="TITLE", date="2020-")
    assert "FULL TEXT" in asyncio.run(server.read_scopus_paper("123", str(tmp_path)))
    read.assert_called_once_with("123", str(tmp_path))
    assert asyncio.run(server.download_scopus("123", str(tmp_path))) == str(path)
    assert asyncio.run(server.download_with_fallback("scopus", "123", save_path=str(tmp_path), use_scihub=False)) == str(path)
    assert download.call_count == 2


def test_scopus_cli_read_and_download(interfaces, mock_http, tmp_path, capsys):
    interfaces(scopus=True)
    args = argparse.Namespace(source="scopus", paper_id="123", save_path=str(tmp_path))
    mock_http.responses.append(httpx.Response(200, content=b"%PDF-1.7\nfixture"))
    assert asyncio.run(cli.cmd_download(args)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {"status": "ok", "path": str(tmp_path / "123.pdf")}
    mock_http.responses.extend([
        httpx.Response(200, json={"abstracts-retrieval-response": {"coredata": {
            "dc:title": "Article", "dc:description": "Abstract", "prism:doi": "10.1000/article",
        }}}),
        httpx.Response(404),
    ])
    assert asyncio.run(cli.cmd_read(args)) == 0
    assert "ABSTRACT ONLY" in capsys.readouterr().out


def test_missing_scholar_key_gives_actionable_direct_error(interfaces):
    server = interfaces()
    with pytest.raises(ValueError, match="PAPER_SEARCH_MCP_SERPAPI_API_KEY"):
        asyncio.run(server.search_google_scholar("query"))
