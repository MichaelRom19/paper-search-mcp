"""SerpAPI contract tests; no Google Scholar or paid API traffic."""

from datetime import datetime
import logging
from urllib.parse import quote, quote_plus

import httpx
import pytest

from paper_search_mcp.academic_platforms.google_scholar import GoogleScholarSearcher

pytestmark = pytest.mark.usefixtures("isolated_env")
KEY = "test/key+ with spaces"
ENTRY = {
    "result_id": "result-1", "title": "A paper", "link": "https://doi.org/10.1000/test",
    "snippet": "A search snippet", "publication_info": {
        "summary": "A Smith, B Jones - Journal, 2024 - publisher.test",
        "authors": [{"name": "A Smith"}, {"name": "B Jones"}],
    },
    "resources": [{"file_format": "HTML", "link": "https://publisher.test/article"},
                  {"file_format": "PDF", "link": "https://repository.test/paper.pdf"}],
    "inline_links": {"cited_by": {"total": 42}},
}


def page(entries, next_start=None):
    data = {"search_metadata": {"status": "Success"}, "organic_results": entries}
    if next_start is not None:
        data["serpapi_pagination"] = {"next": f"https://untrusted.test/search?start={next_start}"}
    return httpx.Response(200, json=data)


@pytest.mark.parametrize("name", ["PAPER_SEARCH_MCP_SERPAPI_API_KEY", "SERPAPI_API_KEY"])
def test_key_environment_aliases(monkeypatch, name):
    monkeypatch.setenv(name, " env-key ")
    assert GoogleScholarSearcher().api_key == "env-key"
    assert GoogleScholarSearcher(api_key="override").api_key == "override"


def test_missing_key_and_explicit_empty_key(monkeypatch):
    with pytest.raises(ValueError, match="PAPER_SEARCH_MCP_SERPAPI_API_KEY"):
        GoogleScholarSearcher()
    monkeypatch.setenv("SERPAPI_API_KEY", KEY)
    monkeypatch.setenv("PAPER_SEARCH_MCP_SERPAPI_API_KEY", "")
    with pytest.raises(ValueError):
        GoogleScholarSearcher()
    with pytest.raises(ValueError):
        GoogleScholarSearcher(api_key="")


def test_metadata_and_request_contract(mock_http, caplog):
    mock_http.responses.append(page([ENTRY]))
    caplog.set_level(logging.DEBUG)
    paper, = GoogleScholarSearcher(KEY).search("test query", max_results=1)
    assert paper.paper_id == "gs_result-1"
    assert paper.title == "A paper"
    assert paper.authors == ["A Smith", "B Jones"]
    assert paper.abstract == "A search snippet"
    assert paper.extra["abstract_source"] == "snippet"
    assert paper.doi == "10.1000/test"
    assert paper.pdf_url == "https://repository.test/paper.pdf"
    assert paper.published_date == datetime(2024, 1, 1)
    assert paper.citations == 42
    assert paper.source == "google_scholar"
    assert paper.to_dict()["authors"] == "A Smith; B Jones"
    request, = mock_http.requests
    assert request.url.host == "serpapi.com"
    assert request.url.path == "/search.json"
    assert dict(request.url.params) == {
        "engine": "google_scholar", "api_key": KEY, "q": "test query",
        "hl": "en", "start": "0", "num": "1",
    }
    assert request.extensions["timeout"]["connect"] == 10
    for secret in (KEY, quote(KEY, safe=""), quote_plus(KEY)):
        assert secret not in caplog.text


@pytest.mark.parametrize("count", [1, 20, 21, 45])
def test_pagination_uses_fixed_endpoint_and_remaining_count(mock_http, count):
    for start in range(0, count, 20):
        entries = [{**ENTRY, "result_id": str(i)} for i in range(start, min(start + 20, count))]
        mock_http.responses.append(page(entries, start + 20 if start + 20 < count else None))
    papers = GoogleScholarSearcher(KEY).search("query", count)
    assert len(papers) == count
    assert len({p.paper_id for p in papers}) == count
    assert [int(r.url.params["num"]) for r in mock_http.requests] == [min(20, count - i) for i in range(0, count, 20)]
    assert all(r.url.host == "serpapi.com" for r in mock_http.requests)


def test_short_page_uses_provider_offset(mock_http):
    mock_http.responses.extend([page([ENTRY], 10), page([{**ENTRY, "result_id": "other"}])])
    assert len(GoogleScholarSearcher(KEY).search("query", 30)) == 2
    assert mock_http.requests[1].url.params["start"] == "10"


@pytest.mark.parametrize("next_start", [0, -10])
def test_nonadvancing_offset_stops(mock_http, next_start):
    mock_http.responses.append(page([ENTRY], next_start))
    with pytest.raises(RuntimeError, match="did not advance"):
        GoogleScholarSearcher(KEY).search("query")
    assert len(mock_http.requests) == 1


def test_snippet_doi_is_not_publication_identity(mock_http):
    item = {"result_id": "snippet", "title": "A replication study", "link": "https://example.test/study",
            "snippet": "Replicates the methods from doi:10.1234/unrelated"}
    mock_http.responses.append(page([item]))
    assert GoogleScholarSearcher(KEY).search("query")[0].doi == ""


def test_duplicate_only_page_does_not_imply_exhaustion(mock_http):
    mock_http.responses.extend([page([ENTRY], 10), page([ENTRY], 20), page([])])
    assert len(GoogleScholarSearcher(KEY).search("query")) == 1
    assert len(mock_http.requests) == 3


@pytest.mark.parametrize("data", [
    {"search_metadata": {"status": "Success"}, "organic_results": []},
    {"search_metadata": {"status": "Success"}, "search_information": {"organic_results_state": "Fully empty"},
     "error": "Google hasn't returned any results for this query."},
    {"search_metadata": {"status": "Success"}, "search_information": {"total_results": 0},
     "error": "Google hasn't returned any results for this query."},
])
def test_genuine_empty_search(mock_http, data):
    mock_http.responses.append(httpx.Response(200, json=data))
    assert GoogleScholarSearcher(KEY).search("no results") == []


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 503])
def test_api_errors_fail_without_retry_and_redact_credentials(mock_http, status):
    mock_http.responses.append(httpx.Response(status, json={"error": f"Failure with {KEY} or {quote_plus(KEY)}"}))
    with pytest.raises(RuntimeError, match="SerpAPI") as error:
        GoogleScholarSearcher(KEY).search("query")
    assert KEY not in str(error.value)
    assert quote_plus(KEY) not in str(error.value)
    assert len(mock_http.requests) == 1


@pytest.mark.parametrize("data", [
    {}, [], {"search_metadata": {"status": "Error"}, "error": "Backend failed"},
    {"search_metadata": {"status": "Processing"}},
    {"search_metadata": {"status": "Success"}},
    {"search_metadata": {"status": "Success"}, "organic_results": {}},
    {"search_metadata": {"status": "Success"}, "error": "Unexpected API error"},
])
def test_malformed_or_failed_payload_is_not_empty_success(mock_http, data):
    mock_http.responses.append(httpx.Response(200, json=data))
    with pytest.raises(RuntimeError, match="SerpAPI"):
        GoogleScholarSearcher(KEY).search("query")


def test_invalid_json_and_network_failure(mock_http):
    mock_http.responses.append(httpx.Response(200, text="<html>not JSON</html>"))
    with pytest.raises(RuntimeError, match="invalid JSON"):
        GoogleScholarSearcher(KEY).search("query")
    mock_http.responses.append(httpx.ReadTimeout(f"Timeout for {KEY}"))
    with pytest.raises(RuntimeError, match="ReadTimeout") as error:
        GoogleScholarSearcher(KEY).search("query")
    assert KEY not in str(error.value)


def test_later_page_failure_propagates(mock_http):
    mock_http.responses.extend([page([ENTRY], 10), httpx.Response(429, json={"error": "Quota exhausted"})])
    with pytest.raises(RuntimeError, match="quota"):
        GoogleScholarSearcher(KEY).search("query")


def test_missing_optional_metadata_and_stable_fallback_id():
    entry = {"title": "Citation only"}
    first = GoogleScholarSearcher._parse_paper(entry)
    second = GoogleScholarSearcher._parse_paper(entry)
    assert first.paper_id == second.paper_id
    assert first.paper_id != GoogleScholarSearcher._parse_paper({"title": "Other citation"}).paper_id
    assert first.authors == []
    assert first.published_date is None
    assert first.citations == 0
    assert first.url == first.pdf_url == first.doi == ""


def test_author_fallback_and_pdf_landing_page():
    paper = GoogleScholarSearcher._parse_paper({
        "title": "Paper", "link": "https://repo.test/paper.pdf?download=true",
        "publication_info": {"summary": "Anne-Marie Smith, B Jones - Journal, 1997 - publisher"},
        "inline_links": {"cited_by": {"total": "unknown"}},
    })
    assert paper.authors == ["Anne-Marie Smith", "B Jones"]
    assert paper.pdf_url == paper.url
    assert paper.published_date == datetime(1997, 1, 1)
    assert paper.citations == 0


def test_invalid_result_or_pagination_raises(mock_http):
    mock_http.responses.append(page([{}]))
    with pytest.raises(RuntimeError, match="title"):
        GoogleScholarSearcher(KEY).search("query")
    mock_http.responses.append(page([ENTRY], "not-a-number"))
    with pytest.raises(RuntimeError, match="pagination"):
        GoogleScholarSearcher(KEY).search("query")


def test_no_request_for_nonpositive_limit_or_blank_query(mock_http):
    searcher = GoogleScholarSearcher(KEY)
    assert searcher.search("query", 0) == searcher.search("query", -1) == []
    with pytest.raises(ValueError, match="empty"):
        searcher.search("  ")
    assert not mock_http.requests


def test_no_redirect_to_scholar(mock_http):
    mock_http.responses.append(httpx.Response(302, headers={"Location": "https://scholar.google.com"}))
    with pytest.raises(RuntimeError):
        GoogleScholarSearcher(KEY).search("query")
    assert len(mock_http.requests) == 1


def test_read_and_download_remain_unsupported(mock_http, tmp_path):
    searcher = GoogleScholarSearcher(KEY)
    with pytest.raises(NotImplementedError):
        searcher.download_pdf("id", str(tmp_path))
    assert "doesn't support direct paper reading" in searcher.read_paper("id")
    assert not mock_http.requests


def test_malformed_pagination_is_visible_even_when_display_is_full(mock_http):
    mock_http.responses.append(page([ENTRY], "malformed"))
    with pytest.raises(RuntimeError, match="pagination"):
        GoogleScholarSearcher(KEY).search("query", 1)
    assert len(mock_http.requests) == 1
