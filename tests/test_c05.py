"""C05 endpoint contracts and persisted review retrieval, entirely offline."""
import json

import httpx
import pytest

from paper_search_mcp.academic_platforms.openalex import OpenAlexSearcher
from paper_search_mcp.academic_platforms.semantic import SemanticSearcher
from paper_search_mcp.http import RequestAllowance, RequestFailure
from paper_search_mcp.provider_models import SavedQuery
from paper_search_mcp.review_models import ReviewProtocol, SearchRequest
from paper_search_mcp.reviews import Reviews


PROVIDERS = {"openalex": OpenAlexSearcher, "semantic": SemanticSearcher}


def record(source, identifier="1", **fields):
    base = {"id": "https://openalex.org/W" + identifier} if source == "openalex" else {"paperId": identifier}
    return {**base, "title": "Study " + identifier, **fields}


def response(source, entries, cursor=None, total=10, **kwargs):
    data = ({"results": entries, "meta": {"count": total, "next_cursor": cursor}} if source == "openalex"
            else {"data": entries, "total": total, "token": cursor})
    return httpx.Response(200, json=data, **kwargs)


@pytest.mark.parametrize("source", PROVIDERS)
def test_invalid_cursor_preserves_received_records(source, mock_http):
    mock_http.responses = [response(source, [record(source)], cursor=123)]
    page = PROVIDERS[source]().search_page(SavedQuery(source=source, query="test"))
    assert page.state == "failed" and page.error.kind == "malformed_response"
    assert len(page.records) == 1 and page.records[0].title == "Study 1"
    assert page.continuation is None


def start(tmp_path, source, **options):
    service = Reviews(tmp_path)
    query = SavedQuery(source=source, query='"native query" | other', **options)
    review = service.create_review(ReviewProtocol(question="Test", selected_sources=[source], queries=[query]),
                                   idempotency_key="review")["review_id"]
    started = service.start_search(SearchRequest(review_id=review, request_budget=8), idempotency_key="run")
    return service, review, started["run_id"]


@pytest.mark.parametrize("source", PROVIDERS)
def test_review_restart_overlap_whole_pages_and_replay(source, tmp_path, mock_http):
    service, review, run = start(tmp_path, source, page_size=1)
    mock_http.responses = [response(source, [record(source), record(source, "2")], "opaque+/="),
                           response(source, [record(source, "2"), record(source, "3")], "last"),
                           response(source, [], None)]
    first = service.advance_run(run, idempotency_key="first")
    assert first["unique_publications"] == 2  # Never truncate to requested/display size.
    restarted = Reviews(tmp_path)
    assert restarted.advance_run(run, idempotency_key="first") == first
    second = restarted.advance_run(run, idempotency_key="second")
    progress = second["state"]["sources"][source]
    assert progress["received"] == 4 and progress["linked_duplicates"] == 1
    assert progress["status"] == "ready" and second["unique_publications"] == 3
    assert progress["total"]["precision"] == ("estimated" if source == "semantic" else "exact")
    final = restarted.advance_run(run, idempotency_key="last")
    assert final["state"]["sources"][source]["status"] == "exhausted"
    assert len(restarted.query_review(review)["papers"]) == 3
    assert mock_http.requests[1].url.params["cursor" if source == "openalex" else "token"] == "opaque+/="
    assert all(r.url.params["search" if source == "openalex" else "query"] == '"native query" | other'
               for r in mock_http.requests)
    assert len(mock_http.requests) == 3
    if source == "semantic":
        assert first["specification"]["queries"][0]["mode"] == "bulk"
        assert all(r.url.path.endswith("/bulk") and "limit" not in r.url.params for r in mock_http.requests)


def test_openalex_auth_page_limit_usage_and_optional_metadata(tmp_path, monkeypatch, mock_http):
    monkeypatch.setenv("PAPER_SEARCH_MCP_OPENALEX_API_KEY", "private-secret")
    service, _, run = start(tmp_path, "openalex", page_size=100)
    mock_http.responses = [response("openalex", [record("openalex", publication_year=2021,
        abstract_inverted_index={"two": [1], "one": [0]}, ids={"doi": "https://doi.org/10.1234/test", "pmid": "https://pubmed.ncbi.nlm.nih.gov/42"},
        authorships=None, primary_location=None)], headers={"X-RateLimit-Credits-Used": "0.25"})]
    result = service.advance_run(run, idempotency_key="advance")
    request = mock_http.requests[0]
    assert request.headers["Authorization"] == "Bearer private-secret"
    assert request.url.params["per_page"] == "100" and request.url.params["cursor"] == "*"
    assert "api_key" not in request.url.params
    assert result["state"]["sources"]["openalex"]["request_usage"]["X-RateLimit-Credits-Used"] == "0.25"
    with service.db.transaction() as connection:
        payload = json.loads(connection.execute("SELECT payload FROM received_pages").fetchone()[0])
    metadata = payload["records"][0]
    assert metadata["published_date"] == "2021" and metadata["date_precision"] == "year"
    assert metadata["identifiers"]["pmid"] == "42" and metadata["abstract"] == "one two"
    assert "private-secret" not in json.dumps(payload)


def test_semantic_metadata_and_no_enrichment(mock_http, monkeypatch):
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "private-secret")
    provider = SemanticSearcher()
    mock_http.responses = [httpx.Response(200, json={"data": [record("semantic", year=2020, authors=None,
        externalIds={"DOI": "10.1234/test", "ArXiv": "2001.12345", "PubMed": "42", "CorpusId": 9},
        abstract="Mentions 10.9999/other", openAccessPdf=None)], "total": 1})]
    papers = provider.search("test", year="2020-", fetch_details=True)
    assert len(mock_http.requests) == 1
    request = mock_http.requests[0]
    assert request.headers["x-api-key"] == "private-secret"
    assert request.url.path.endswith("/search") and request.url.params["year"] == "2020-"
    assert "externalIds" in request.url.params["fields"] and "abstract" in request.url.params["fields"]
    assert papers[0].doi == "10.1234/test" and papers[0].extra["external_ids"]["CorpusId"] == 9


@pytest.mark.parametrize("source", PROVIDERS)
def test_legacy_multiple_pages_and_overlaps(source, mock_http):
    if source == "openalex":
        mock_http.responses = [response(source, [record(source)], "a"),
                               response(source, [record(source), record(source, "2")])]
    else:
        mock_http.responses = [httpx.Response(200, json={"data": [record(source)], "next": 1}),
                               httpx.Response(200, json={"data": [record(source), record(source, "2")]})]
    papers = PROVIDERS[source]().search("test", max_results=2)
    assert [p.paper_id for p in papers] == (["W1", "W2"] if source == "openalex" else ["1", "2"])
    assert len(mock_http.requests) == 2


@pytest.mark.parametrize("source", PROVIDERS)
@pytest.mark.parametrize("status,headers,kind", [(401, {}, "authentication"), (403, {}, "authentication"),
    (429, {"X-RateLimit-Remaining": "0"}, "quota"), (429, {"Retry-After": "3600"}, "rate_limit"),
    (503, {"Retry-After": "0"}, "service")])
def test_failure_and_late_page_preservation(source, status, headers, kind, tmp_path, mock_http):
    service, _, run = start(tmp_path, source)
    mock_http.responses = [response(source, [record(source)], "next"), httpx.Response(status, headers=headers)]
    service.advance_run(run, idempotency_key="first")
    result = service.advance_run(run, idempotency_key="failure", max_requests=1)
    assert result["unique_publications"] == 1
    assert result["state"]["sources"][source]["error"]["kind"] == kind
    assert result["state"]["sources"][source]["continuation"]["cursor"] == "next"
    assert result["state"]["requests_used"] == 2 and len(mock_http.requests) == 2


@pytest.mark.parametrize("source", PROVIDERS)
def test_missing_metadata_and_rejections(source, mock_http):
    mock_http.responses = [response(source, [record(source), {"title": "Missing identity"}, None,
                                            record(source, "2", title=None)])]
    page = PROVIDERS[source]().search_page(SavedQuery(source=source, query="test"))
    assert len(page.records) == 1 and len(page.rejected) == 3
    assert page.records[0].published_date is None
    assert page.records[0].date_precision == "unknown"
    assert page.records[0].authors == []


@pytest.mark.parametrize("source", PROVIDERS)
@pytest.mark.parametrize("envelope", [{}, [], {"data": None, "results": None}, {"error": "denied"}])
def test_malformed_envelope_is_not_empty_success(source, envelope, mock_http):
    mock_http.responses = [httpx.Response(200, json=envelope)]
    page = PROVIDERS[source]().search_page(SavedQuery(source=source, query="test"))
    assert page.state == "failed" and page.error.kind == "malformed_response"


@pytest.mark.parametrize("source", PROVIDERS)
def test_expired_continuation_and_no_legacy_empty_success(source, mock_http):
    mock_http.responses = [httpx.Response(400, text="Invalid expired token"), httpx.Response(403)]
    page = PROVIDERS[source]().search_page(SavedQuery(source=source, query="test"), {"cursor": "next"})
    assert page.error.kind == "expired_cursor"
    with pytest.raises(RequestFailure, match="authentication"):
        PROVIDERS[source]().search("test")


@pytest.mark.parametrize("source", PROVIDERS)
def test_repeated_cursor_retains_received_records(source, mock_http):
    mock_http.responses = [response(source, [record(source)], "next")]
    page = PROVIDERS[source]().search_page(SavedQuery(source=source, query="test"), {"cursor": "next"})
    assert len(page.records) == 1 and page.error.kind == "nonadvancing"


@pytest.mark.parametrize("mode,sort,expected", [("native_query", None, "bulk"), ("bulk", "relevance", "relevance"),
    ("relevance", None, "relevance"), ("bulk", "citationCount:desc", "bulk")])
def test_mode_saved_and_endpoint_selected(mode, sort, expected, tmp_path, mock_http):
    service, _, run = start(tmp_path, "semantic", mode=mode, sort=sort)
    assert service.get_run(run)["specification"]["queries"][0]["mode"] == expected
    mock_http.responses = [httpx.Response(200, json={"data": [], "total": 0})]
    service.advance_run(run, idempotency_key="advance")
    assert mock_http.requests[0].url.path.endswith("/bulk") == (expected == "bulk")


@pytest.mark.parametrize("options", [{"mode": "relevance", "sort": "citationCount:desc"},
    {"filters": {"year": "2025-2020"}}, {"filters": {"year": 2020}}, {"filters": {"language": "en"}}])
def test_invalid_options_are_visible_before_network(options, tmp_path, mock_http):
    service, _, run = start(tmp_path, "semantic", **options)
    assert service.get_run(run)["state"]["sources"]["semantic"]["status"] == "invalid"
    assert not mock_http.requests


def test_relevance_ceiling_and_bulk_page_not_truncated(mock_http):
    provider = SemanticSearcher()
    mock_http.responses = [httpx.Response(200, json={"data": [record("semantic")], "total": 2000, "next": 1000}),
        response("semantic", [record("semantic", str(i)) for i in range(1000)], "next")]
    page = provider.search_page(SavedQuery(source="semantic", query="test", mode="relevance"), {"cursor": 999})
    assert page.state == "provider_cap" and len(page.records) == 1
    assert mock_http.requests[0].url.params["limit"] == "1"
    page = provider.search_page(SavedQuery(source="semantic", query="test", page_size=20))
    assert len(page.records) == 1000 and page.state == "ready" and page.warnings


@pytest.mark.parametrize("source", PROVIDERS)
def test_retries_use_allowance_and_preserve_query(source, mock_http):
    mock_http.responses = [httpx.Response(503, headers={"Retry-After": "0"}), response(source, [])]
    allowance = RequestAllowance(remaining=2)
    page = PROVIDERS[source]().search_page(SavedQuery(source=source, query="test"), allowance=allowance)
    assert page.state == "exhausted" and page.requests_used == allowance.used == 2
    assert mock_http.requests[0].url == mock_http.requests[1].url


def test_openalex_usage_for_failed_attempts_and_variable_costs(tmp_path, mock_http):
    service, _, run = start(tmp_path, "openalex")
    mock_http.responses = [httpx.Response(503, headers={"Retry-After": "0", "X-RateLimit-Credits-Used": "0.1"}),
        httpx.Response(200, headers={"X-RateLimit-Credits-Used": "2.5"},
            json={"meta": {"count": 0, "next_cursor": None, "cost_usd": 0.0025}, "results": []})]
    result = service.advance_run(run, idempotency_key="costs")
    usage = result["state"]["sources"]["openalex"]["request_usage"]
    assert usage["cost_usd"] == 0.0025
    assert [attempt["X-RateLimit-Credits-Used"] for attempt in usage["attempts"]] == ["0.1", "2.5"]
    assert result["state"]["requests_used"] == 2


@pytest.mark.parametrize("source", PROVIDERS)
def test_cli_review_adapter_and_legacy_search(source, tmp_path, monkeypatch, capsys, mock_http):
    from paper_search_mcp.cli import main
    monkeypatch.setenv("PAPER_SEARCH_MCP_DATA_DIR", str(tmp_path))
    service, review, run = start(tmp_path, source)
    mock_http.responses = [response(source, [record(source)])]
    monkeypatch.setattr("sys.argv", ["paper-search", "advance-run", run, "--idempotency-key", "cli"])
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 0
    assert json.loads(capsys.readouterr().out)["unique_publications"] == 1
    assert len(service.query_review(review)["papers"]) == 1
    mock_http.responses = [response(source, [record(source)])]
    monkeypatch.setattr("sys.argv", ["paper-search", "search", "test", "--sources", source])
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 0
    assert json.loads(capsys.readouterr().out)["papers"][0]["source"] == source


def test_openalex_bearer_secret_redacted_from_provider_metadata(mock_http):
    provider = OpenAlexSearcher("private-secret")
    mock_http.responses = [response("openalex", [record("openalex", title="private-secret")])]
    page = provider.search_page(SavedQuery(source="openalex", query="test"))
    assert "private-secret" not in page.model_dump_json()


@pytest.mark.parametrize("source", PROVIDERS)
def test_waiting_page_resumes_same_checkpoint(source, tmp_path, mock_http):
    service, _, run = start(tmp_path, source)
    mock_http.responses = [response(source, [record(source)], "opaque"),
        httpx.Response(429, headers={"Retry-After": "0"}), response(source, [record(source, "2")])]
    service.advance_run(run, idempotency_key="one")
    waiting = service.advance_run(run, idempotency_key="two", max_requests=1)
    assert waiting["state"]["sources"][source]["status"] == "waiting"
    result = Reviews(tmp_path).advance_run(run, idempotency_key="three")
    assert result["unique_publications"] == 2
    assert mock_http.requests[1].url == mock_http.requests[2].url
