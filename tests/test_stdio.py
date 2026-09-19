"""Exercise real MCP stdio initialization, listing and calls without provider traffic."""

import asyncio
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


SERVER = '''
import socket
from dataclasses import replace
from types import ModuleType
import sys
from paper_search_mcp import config, registry
from paper_search_mcp.paper import Paper

config._ENV_LOADED = True

def blocked(*args, **kwargs):
    raise AssertionError("Network access from offline stdio fixture")
socket.getaddrinfo = socket.socket.connect = blocked

class FixtureProvider:
    def search(self, query, max_results=10, sort_by="relevance", sort_order="descending"):
        return [Paper(paper_id="fixture:1", title="Fixture paper", authors=["Alice", "Bob"],
                      abstract="Fixture abstract", doi="10.1000/fixture", published_date=None,
                      url="https://example.test/paper", pdf_url="", source="arxiv", extra={"fixture": True})]

module = ModuleType("paper_search_mcp.academic_platforms.fixture")
module.FixtureProvider = FixtureProvider
sys.modules[module.__name__] = module
registry.SOURCES["arxiv"] = replace(registry.SOURCES["arxiv"], factory="fixture:FixtureProvider")
from paper_search_mcp.server import main
main()
'''


def test_stdio_initialization_listing_and_legacy_calls(tmp_path):
    async def exercise():
        params = StdioServerParameters(command=sys.executable, args=["-c", SERVER],
                                       cwd=Path(__file__).resolve().parents[1])
        with (tmp_path / "stderr.log").open("w") as stderr:
            async with stdio_client(params, errlog=stderr) as streams:
                async with ClientSession(*streams, read_timeout_seconds=15) as session:
                    await session.initialize()
                    names = {tool.name for tool in (await session.list_tools()).tools}
                    assert {"list_sources", "search_papers", "search_arxiv", "download_arxiv"} <= names
                    catalog = await session.call_tool("list_sources", {})
                    assert not catalog.is_error
                    sources = catalog.structured_content["result"]
                    assert next(s for s in sources if s["name"] == "ieee")["implemented"] is False
                    assert all(not s["access_verified"] for s in sources)
                    result = await session.call_tool("search_arxiv", {"query": "fixture", "max_results": 1})
                    assert not result.is_error
                    paper = result.structured_content["result"][0]
                    assert paper["paper_id"] == "fixture:1"
                    assert paper["authors"] == "Alice; Bob"
                    assert paper["extra"] == "{'fixture': True}"
                    aggregate = await session.call_tool("search_papers", {"query": "fixture", "sources": "arxiv"})
                    assert not aggregate.is_error
                    assert aggregate.structured_content["total"] == 1
                    assert aggregate.structured_content["papers"] == [paper]
        assert "Traceback" not in (tmp_path / "stderr.log").read_text()

    asyncio.run(asyncio.wait_for(exercise(), timeout=30))


def test_stdio_persistent_library_tools(tmp_path):
    from paper_search_mcp.library import Library
    from paper_search_mcp.provider_models import Metadata
    library = Library(tmp_path)
    review_id = library.create_review({}, idempotency_key="review")["review_id"]
    publication_id = library.ingest(review_id, [Metadata(paper_id="1", source="fixture", title="Original")],
        idempotency_key="ingest")["records"][0]["publication_id"]
    async def exercise():
        params = StdioServerParameters(command=sys.executable, args=["-c", SERVER],
            env={"PAPER_SEARCH_MCP_DATA_DIR": str(tmp_path)}, cwd=Path(__file__).resolve().parents[1])
        with (tmp_path / "stderr.log").open("w") as stderr:
            async with stdio_client(params, errlog=stderr) as streams:
                async with ClientSession(*streams, read_timeout_seconds=15) as session:
                    await session.initialize()
                    tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                    assert {"get_paper", "query_review", "resolve_publications", "possible_duplicates"} <= tools.keys()
                    query = await session.call_tool("query_review", {"review_id": review_id})
                    assert not query.is_error and len(query.structured_content["papers"]) == 1
                    result = await session.call_tool("resolve_publications", {"request": {
                        "action": "override", "actor": "human", "reason": "Verified publisher",
                        "publication_id": publication_id, "metadata": {"title": "Updated"}}, "idempotency_key": "stdio"})
                    assert not result.is_error
                    paper = await session.call_tool("get_paper", {"publication_id": publication_id})
                    assert not paper.is_error
                    assert paper.structured_content["metadata"]["title"]["value"] == "Updated"
    asyncio.run(asyncio.wait_for(exercise(), timeout=30))


def test_stdio_saved_review_and_search(tmp_path):
    fixture_server = SERVER.replace('from paper_search_mcp.server import main', '''
from paper_search_mcp.provider_models import Metadata, ProviderPage
from paper_search_mcp.reviews import Reviews
class PageFixture:
    def search_page(self, query, continuation, *, allowance):
        allowance.reserve()
        return ProviderPage(records=[Metadata(paper_id="1", source="scopus", title="Persisted")], state="exhausted")
registry.SOURCES["scopus"] = replace(registry.SOURCES["scopus"], required=())
from paper_search_mcp import server
server.Reviews = lambda: Reviews(provider_factory=lambda _: PageFixture())
from paper_search_mcp.server import main''')
    async def exercise():
        params = StdioServerParameters(command=sys.executable, args=["-c", fixture_server],
            env={"PAPER_SEARCH_MCP_DATA_DIR": str(tmp_path)}, cwd=Path(__file__).resolve().parents[1])
        with (tmp_path / "stdio.log").open("w") as stderr:
            async with stdio_client(params, errlog=stderr) as streams:
                async with ClientSession(*streams, read_timeout_seconds=15) as session:
                    await session.initialize()
                    names = {tool.name for tool in (await session.list_tools()).tools}
                    assert {"create_review", "update_review", "get_review", "list_reviews", "start_search", "advance_run", "get_run"} <= names
                    async def call(name, args):
                        result = await session.call_tool(name, args)
                        assert not result.is_error, result
                        return result.structured_content
                    protocol = {"question": "Test", "selected_sources": ["scopus"], "queries": [{"source": "scopus", "query": "TITLE(test)"}]}
                    review = await call("create_review", {"protocol": protocol, "idempotency_key": "review"})
                    assert (await call("get_review", {"review_id": review["review_id"]}))["revision"] == 1
                    assert len((await call("list_reviews", {}))["reviews"]) == 1
                    run = await call("start_search", {"request": {"review_id": review["review_id"], "request_budget": 3}, "idempotency_key": "run"})
                    assert (await call("get_run", {"run_id": run["run_id"]}))["state"]["requests_used"] == 0
                    advanced = await call("advance_run", {"run_id": run["run_id"], "idempotency_key": "advance"})
                    assert advanced["unique_publications"] == 1
                    assert await call("advance_run", {"run_id": run["run_id"], "idempotency_key": "advance"}) == advanced
                    assert (await call("update_review", {"review_id": review["review_id"], "protocol": protocol, "idempotency_key": "update"}))["revision"] == 2
    asyncio.run(asyncio.wait_for(exercise(), timeout=30))


def test_stdio_c05_adapters_and_resume(tmp_path):
    fixture_server = SERVER.replace('from paper_search_mcp.server import main', '''
import httpx
calls = {"openalex": 0, "semantic": 0}
def transport(request):
    source = "openalex" if request.url.host == "api.openalex.org" else "semantic"
    index = calls[source]
    calls[source] += 1
    assert request.url.params.get("search", request.url.params.get("query")) == "test"
    if index:
        assert request.url.params["cursor" if source == "openalex" else "token"] == "opaque+/="
    if source == "openalex":
        data = {"meta": {"count": 2, "next_cursor": "opaque+/=" if not index else None},
                "results": [{"id": f"https://openalex.org/W{index}", "title": f"Study {index}"}]}
    else:
        assert request.url.path.endswith("/bulk")
        data = {"total": 2, "token": "opaque+/=" if not index else None,
                "data": [{"paperId": str(index), "title": f"Study {index}"}]}
    return httpx.Response(200, json=data)
client = httpx.Client
httpx.Client = lambda **kwargs: client(transport=httpx.MockTransport(transport), **kwargs)
from paper_search_mcp.server import main''')
    async def exercise():
        params = StdioServerParameters(command=sys.executable, args=["-c", fixture_server],
            env={"PAPER_SEARCH_MCP_DATA_DIR": str(tmp_path)}, cwd=Path(__file__).resolve().parents[1])
        with (tmp_path / "stdio.log").open("w") as stderr:
            async with stdio_client(params, errlog=stderr) as streams:
                async with ClientSession(*streams, read_timeout_seconds=15) as session:
                    await session.initialize()
                    async def call(name, **args):
                        result = await session.call_tool(name, args)
                        assert not result.is_error, result
                        return result.structured_content
                    review = await call("create_review", idempotency_key="review", protocol={
                        "question": "Test", "selected_sources": ["openalex", "semantic"],
                        "queries": [{"source": source, "query": "test"} for source in ("openalex", "semantic")]})
                    assert all(v["valid"] for v in review["validation"])
                    started = await call("start_search", idempotency_key="run",
                        request={"review_id": review["review_id"], "request_budget": 4})
                    first = await call("advance_run", run_id=started["run_id"], idempotency_key="first")
                    assert first["state"]["requests_used"] == 2
                    assert await call("advance_run", run_id=started["run_id"], idempotency_key="first") == first
                    last = await call("advance_run", run_id=started["run_id"], idempotency_key="last")
                    assert last["state"]["requests_used"] == 4 and last["unique_publications"] == 4
                    assert all(s["status"] == "exhausted" for s in last["state"]["sources"].values())
    asyncio.run(asyncio.wait_for(exercise(), timeout=30))


def test_stdio_c06_adapters_and_resume(tmp_path):
    fixture_server = SERVER.replace('from paper_search_mcp.server import main', '''
import httpx
from types import SimpleNamespace
from paper_search_mcp import http
http.time = SimpleNamespace(time=lambda: 1000, sleep=lambda _: None)
registry.SOURCES["arxiv"] = replace(registry.SOURCES["arxiv"], factory="arxiv:ArxivSearcher")
calls = {"crossref": 0, "arxiv": 0}
def transport(request):
    source = "crossref" if request.url.host == "api.crossref.org" else "arxiv"
    index = calls[source]
    calls[source] += 1
    if source == "crossref":
        assert request.url.params["cursor"] == ("*" if index == 0 else str(index))
        assert request.url.params["query"] == "test"
        assert request.url.params["sort"] == "created"
        return httpx.Response(200, json={"message": {"total-results": 2,
            "next-cursor": str(index + 1), "items": [] if index == 2 else
            [{"DOI": f"10.1234/{index}", "title": [f"Study {index}"]}]}})
    assert request.url.params["start"] == str(index)
    assert request.url.params["search_query"] == "ti:test"
    assert request.url.params["sortBy"] == "submittedDate"
    return httpx.Response(200, text=f\'''<feed xmlns="http://www.w3.org/2005/Atom"
        xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
        <opensearch:totalResults>2</opensearch:totalResults>
        <opensearch:startIndex>{index}</opensearch:startIndex>
        <entry><id>https://arxiv.org/abs/2001.12345v{index + 1}</id>
        <title>Study</title></entry></feed>\''')
client = httpx.Client
httpx.Client = lambda **kwargs: client(transport=httpx.MockTransport(transport), **kwargs)
from paper_search_mcp.server import main''')
    async def exercise():
        params = StdioServerParameters(command=sys.executable, args=["-c", fixture_server],
            env={"PAPER_SEARCH_MCP_DATA_DIR": str(tmp_path)}, cwd=Path(__file__).resolve().parents[1])
        with (tmp_path / "stdio.log").open("w") as stderr:
            async with stdio_client(params, errlog=stderr) as streams:
                async with ClientSession(*streams, read_timeout_seconds=15) as session:
                    await session.initialize()
                    async def call(name, **args):
                        result = await session.call_tool(name, args)
                        assert not result.is_error, result
                        return result.structured_content
                    review = await call("create_review", idempotency_key="review", protocol={
                        "question": "Test", "selected_sources": ["crossref", "arxiv"], "queries": [
                            {"source": "crossref", "query": "test", "sort": "created", "page_size": 1},
                            {"source": "arxiv", "query": "ti:test", "sort": "submittedDate", "page_size": 1}]})
                    assert all(v["valid"] for v in review["validation"])
                    started = await call("start_search", idempotency_key="run",
                        request={"review_id": review["review_id"], "request_budget": 5})
                    run_id = started["run_id"]
                    first = await call("advance_run", run_id=run_id, idempotency_key="first")
                    assert first["unique_publications"] == first["state"]["requests_used"] == 2
                    assert await call("advance_run", run_id=run_id, idempotency_key="first") == first
                    second = await call("advance_run", run_id=run_id, idempotency_key="second")
                    assert second["unique_publications"] == second["state"]["requests_used"] == 4
                    last = await call("advance_run", run_id=run_id, idempotency_key="last")
                    assert last["state"]["requests_used"] == 5
                    assert all(s["status"] == "exhausted" for s in last["state"]["sources"].values())
    asyncio.run(asyncio.wait_for(exercise(), timeout=30))
