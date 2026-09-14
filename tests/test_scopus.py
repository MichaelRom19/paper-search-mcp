"""Elsevier API contract tests, adapted and expanded from mildwall's PR #89."""

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from paper_search_mcp.academic_platforms.scopus import ScopusAPIError, ScopusSearcher

pytestmark = pytest.mark.usefixtures("isolated_env")
KEY = "fake-scopus-key"
TOKEN = "fake-institution-token"
ENTRY = {
    "dc:identifier": "SCOPUS_ID:123", "dc:title": "A Scopus paper",
    "author": [{"authname": "Author A"}, {"authname": "Author B"}],
    "dc:description": "The abstract.", "prism:doi": "10.1000/test",
    "prism:coverDate": "2024-01-15", "citedby-count": "7",
    "subject-area": [{"@abbrev": "COMP"}],
    "link": [{"@ref": "self", "@href": "https://api.elsevier.com/123"},
             {"@ref": "scopus", "@href": "https://www.scopus.com/record/123"}],
}
XML = '''<full-text-retrieval-response xmlns="urn:elsevier" xmlns:ce="urn:common">
<coredata><title>Metadata should not become body text</title></coredata>
<objects><object>Binary object metadata</object></objects>
<originalText><doc><serial-item><article><body><ce:section>
<ce:section-title>Introduction</ce:section-title>
<ce:para>A <ce:bold>complete</ce:bold> paragraph.</ce:para>
<ce:para>Another paragraph.</ce:para>
</ce:section></body></article></serial-item></doc></originalText>
</full-text-retrieval-response>'''
PDF = b"%PDF-1.7\nmock document\n%%EOF"


def page(entries, total=None):
    return httpx.Response(200, json={"search-results": {
        "entry": entries, "opensearch:totalResults": str(total if total is not None else len(entries)),
    }})


def details():
    return httpx.Response(200, json={"abstracts-retrieval-response": {
        "coredata": ENTRY, "authors": {"author": {"ce:indexed-name": "Author A"}},
    }})


@pytest.fixture
def sleep(monkeypatch):
    sleep = Mock()
    monkeypatch.setattr("paper_search_mcp.academic_platforms.scopus.time.sleep", sleep)
    return sleep


@pytest.mark.parametrize("prefix", ["PAPER_SEARCH_MCP_", ""])
def test_credentials_and_headers(monkeypatch, mock_http, prefix):
    monkeypatch.setenv(prefix + "SCOPUS_API_KEY", " " + KEY + " ")
    monkeypatch.setenv(prefix + "SCOPUS_INST_TOKEN", TOKEN)
    mock_http.responses.append(page([ENTRY]))
    paper, = ScopusSearcher().search("neural networks", field="TITLE", date="2024", sort="-citedby-count")
    assert paper.paper_id == "123"
    assert paper.authors == ["Author A", "Author B"]
    assert paper.abstract == "The abstract."
    assert paper.doi == "10.1000/test"
    assert paper.url == "https://www.scopus.com/record/123"
    assert paper.published_date == datetime(2024, 1, 15)
    assert paper.citations == 7
    assert paper.categories == ["COMP"]
    assert paper.source == "scopus"
    request, = mock_http.requests
    assert request.headers["X-ELS-APIKey"] == KEY
    assert request.headers["X-ELS-Insttoken"] == TOKEN
    assert "Mozilla" not in request.headers["User-Agent"]
    assert request.url.host == "api.elsevier.com"
    assert dict(request.url.params) == {
        "query": "TITLE(neural networks)", "view": "COMPLETE", "date": "2024",
        "sort": "-citedby-count", "start": "0", "count": "10",
    }
    assert request.extensions["timeout"] == {"connect": 10, "read": 30, "write": 30, "pool": 30}


def test_missing_key_and_precedence(monkeypatch):
    with pytest.raises(ValueError, match="SCOPUS_API_KEY"):
        ScopusSearcher()
    monkeypatch.setenv("SCOPUS_API_KEY", "legacy")
    monkeypatch.setenv("PAPER_SEARCH_MCP_SCOPUS_API_KEY", "prefixed")
    assert ScopusSearcher().api_key == "prefixed"
    assert ScopusSearcher(KEY).api_key == KEY
    monkeypatch.setenv("PAPER_SEARCH_MCP_SCOPUS_API_KEY", "")
    with pytest.raises(ValueError):
        ScopusSearcher()
    with pytest.raises(ValueError):
        ScopusSearcher(api_key="")


@pytest.mark.parametrize("count", [1, 25, 26, 60])
def test_paginated_search_and_relevance_alias(mock_http, count):
    for start in range(0, count, 25):
        entries = [{**ENTRY, "dc:identifier": f"SCOPUS_ID:{i}"} for i in range(start, min(start + 25, count))]
        mock_http.responses.append(page(entries, count))
    papers = ScopusSearcher(KEY).search('TITLE("deep learning") AND ABS(test)', count)
    assert len(papers) == count
    assert len({p.paper_id for p in papers}) == count
    assert [int(r.url.params["count"]) for r in mock_http.requests] == [min(25, count - i) for i in range(0, count, 25)]
    assert all(r.url.params["sort"] == "relevancy" for r in mock_http.requests)
    assert all("X-ELS-Insttoken" not in r.headers for r in mock_http.requests)


def test_short_and_repeated_pages(mock_http):
    mock_http.responses.extend([page([ENTRY], 100), page([ENTRY], 100)])
    assert len(ScopusSearcher(KEY).search("query", 30)) == 1
    assert mock_http.requests[1].url.params["start"] == "1"


def test_singletons_and_optional_metadata(mock_http):
    entry = {**ENTRY, "author": {"authname": "Only Author"}, "subject-area": {"@abbrev": "MATH"},
             "citedby-count": None, "prism:coverDate": "1997", "link": None}
    mock_http.responses.append(page(entry, 1))
    paper, = ScopusSearcher(KEY).search("query")
    assert paper.authors == ["Only Author"]
    assert paper.categories == ["MATH"]
    assert paper.citations == 0
    assert paper.published_date == datetime(1997, 1, 1)
    assert paper.url == "https://doi.org/10.1000/test"
    assert ScopusSearcher._parse_paper({**ENTRY, "prism:coverDate": "bad"}).published_date is None


@pytest.mark.parametrize("entries", [[], {"error": "Result set was empty"}, [{"error": "Result set was empty"}]])
def test_genuine_empty_search(mock_http, entries):
    mock_http.responses.append(page(entries, 0))
    assert ScopusSearcher(KEY).search("query") == []


@pytest.mark.parametrize("date,expected", [
    ("2024", "2024"), (" 2020-2024 ", "2020-2024"),
    ("2020-", f"2020-{datetime.now().year + 1}"), ("-2024", "1788-2024"),
])
def test_date_normalization(mock_http, date, expected):
    mock_http.responses.append(page([]))
    ScopusSearcher(KEY).search("query", date=date)
    assert mock_http.requests[0].url.params["date"] == expected


@pytest.mark.parametrize("date", ["-", "0000", "2025-2020", "2020-0000", "nonsense", "2020-01-01"])
def test_invalid_dates_make_no_request(mock_http, date):
    with pytest.raises(ValueError):
        ScopusSearcher(KEY).search("query", date=date)
    assert not mock_http.requests


@pytest.mark.parametrize("paper_id", ["../123", "SCOPUS_ID:../123", "123/456", "", "１２３", "١٢٣"])
@pytest.mark.parametrize("operation", ["read_paper", "download_pdf"])
def test_invalid_ids_make_no_request(mock_http, paper_id, operation, tmp_path):
    with pytest.raises(ValueError, match="numeric Scopus ID"):
        getattr(ScopusSearcher(KEY), operation)(paper_id, str(tmp_path))
    assert not mock_http.requests
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_nonretryable_errors(mock_http, sleep, status):
    mock_http.responses.append(httpx.Response(status, json={"service-error": {"status": {"statusText": "Denied"}}}))
    with pytest.raises(ScopusAPIError, match="Denied") as error:
        ScopusSearcher(KEY).search("query")
    assert error.value.status_code == status
    assert len(mock_http.requests) == 1
    sleep.assert_not_called()
    if status == 403:
        assert "VPN" in str(error.value)


@pytest.mark.parametrize("headers", [
    {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "123456"},
    {"X-ELS-Status": "QUOTA_EXCEEDED - Quota Exceeded"},
    {"Retry-After": "120"},
])
def test_quota_or_long_retry_after_fails_fast(mock_http, sleep, headers):
    mock_http.responses.append(httpx.Response(429, headers=headers))
    with pytest.raises(ScopusAPIError, match="quota"):
        ScopusSearcher(KEY).search("query")
    assert len(mock_http.requests) == 1
    sleep.assert_not_called()


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_retryable_status_then_success(mock_http, sleep, status):
    mock_http.responses.extend([httpx.Response(status, headers={"Retry-After": "3"}), page([ENTRY])])
    assert len(ScopusSearcher(KEY).search("query")) == 1
    sleep.assert_called_once_with(3)


def test_retry_after_http_date_and_invalid_header(mock_http, sleep):
    date = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=20), usegmt=True)
    mock_http.responses.extend([httpx.Response(429, headers={"Retry-After": date}), page([])])
    ScopusSearcher(KEY).search("query")
    assert 18 <= sleep.call_args.args[0] <= 20
    assert ScopusSearcher._retry_delay("invalid", 4) == 4
    assert ScopusSearcher._retry_delay("-3", 4) == 0


@pytest.mark.parametrize("failure", [httpx.Response(503), httpx.ConnectError("network failure")])
def test_at_most_three_attempts(mock_http, sleep, failure):
    mock_http.responses.extend([failure, failure, failure])
    with pytest.raises(RuntimeError):
        ScopusSearcher(KEY).search("query")
    assert len(mock_http.requests) == 3
    assert [call.args[0] for call in sleep.call_args_list] == [2, 4]


def test_transport_retry_then_success(mock_http, sleep):
    mock_http.responses.extend([httpx.ReadTimeout("timeout"), page([ENTRY])])
    assert len(ScopusSearcher(KEY).search("query")) == 1
    sleep.assert_called_once_with(2)


@pytest.mark.parametrize("data", [{}, [], {"search-results": None}, {"search-results": []},
                                       {"search-results": {"entry": [{"error": "Unexpected provider error"}]}}])
def test_malformed_payloads_are_errors(mock_http, data):
    mock_http.responses.append(httpx.Response(200, json=data))
    with pytest.raises(RuntimeError):
        ScopusSearcher(KEY).search("query")


def test_invalid_json_and_later_page_error(mock_http):
    mock_http.responses.append(httpx.Response(200, text="bad JSON"))
    with pytest.raises(RuntimeError, match="search-results"):
        ScopusSearcher(KEY).search("query")
    mock_http.responses.extend([page([ENTRY], 100), httpx.Response(401)])
    with pytest.raises(ScopusAPIError):
        ScopusSearcher(KEY).search("query")


def test_native_xml_full_text_and_metadata(mock_http):
    mock_http.responses.extend([details(), httpx.Response(200, text=XML)])
    text = ScopusSearcher(KEY).read_paper(" SCOPUS_ID:123 ")
    assert "Title: A Scopus paper" in text
    assert "Authors: Author A" in text
    assert "FULL TEXT\nIntroduction" in text
    assert "A complete paragraph." in text
    assert "Another paragraph." in text
    assert "Binary object metadata" not in text
    assert "Metadata should not become body text" not in text
    assert [r.url.path for r in mock_http.requests] == ["/content/abstract/scopus_id/123", "/content/article/scopus_id/123"]
    assert mock_http.requests[1].url.params["view"] == "FULL"
    assert mock_http.requests[1].headers["Accept"] == "text/xml"


@pytest.mark.parametrize("response", [httpx.Response(403), httpx.Response(404),
    httpx.Response(200, text='<full-text-retrieval-response xmlns="urn:elsevier"><coredata/></full-text-retrieval-response>')])
def test_unavailable_full_text_is_labeled_abstract_only(mock_http, response):
    mock_http.responses.extend([details(), response])
    text = ScopusSearcher(KEY).read_paper("123")
    assert "ABSTRACT ONLY" in text
    assert "Full text unavailable" in text
    assert "The abstract." in text
    assert "FULL TEXT" not in text


@pytest.mark.parametrize("response", [httpx.Response(401), httpx.Response(429, headers={"X-RateLimit-Remaining": "0"}),
                                      httpx.Response(200, text="<malformed>"), httpx.Response(200, text="<error/>")])
def test_full_text_operational_failures_are_errors(mock_http, response):
    mock_http.responses.extend([details(), response])
    with pytest.raises(RuntimeError):
        ScopusSearcher(KEY).read_paper("123")


def test_full_text_request_uses_common_retry_policy(mock_http, sleep):
    mock_http.responses.extend([details(), httpx.Response(503), httpx.Response(200, text=XML)])
    assert "FULL TEXT" in ScopusSearcher(KEY).read_paper("123")
    sleep.assert_called_once_with(2)


@pytest.mark.parametrize("body", ["<originalText>Plain full text</originalText>",
    "<originalText><doc><rawtext>Plain full text</rawtext></doc></originalText>"])
def test_unstructured_article_text(body):
    response = httpx.Response(200, text=f'<full-text-retrieval-response xmlns="urn:elsevier">{body}</full-text-retrieval-response>')
    assert ScopusSearcher._full_text(response) == "Plain full text"


def test_external_xml_entity_is_not_resolved():
    response = httpx.Response(200, text='''<!DOCTYPE article [<!ENTITY secret SYSTEM "file:///etc/passwd">]>
        <full-text-retrieval-response><originalText>&secret;</originalText></full-text-retrieval-response>''')
    with pytest.raises(RuntimeError, match="invalid article XML"):
        ScopusSearcher._full_text(response)


def test_pdf_saved_with_numeric_name(mock_http, tmp_path):
    mock_http.responses.append(httpx.Response(200, content=PDF, headers={"Content-Type": "application/pdf"}))
    path = Path(ScopusSearcher(KEY).download_pdf(" SCOPUS_ID:123 ", str(tmp_path / "new")))
    assert path == tmp_path / "new" / "123.pdf"
    assert path.read_bytes() == PDF
    assert mock_http.requests[0].headers["Accept"] == "application/pdf"


def test_pdf_redirect_strips_credentials(mock_http, tmp_path):
    mock_http.responses.extend([
        httpx.Response(303, headers={"Location": "https://cdn.test/paper"}),
        httpx.Response(307, headers={"Location": "/final"}),
        httpx.Response(200, content=PDF),
    ])
    assert Path(ScopusSearcher(KEY, TOKEN).download_pdf("123", str(tmp_path))).read_bytes() == PDF
    first, *redirects = mock_http.requests
    assert first.headers["X-ELS-APIKey"] == KEY
    assert first.headers["X-ELS-Insttoken"] == TOKEN
    for request in redirects:
        assert request.url.host == "cdn.test"
        assert "X-ELS-APIKey" not in request.headers
        assert "X-ELS-Insttoken" not in request.headers
        assert "Authorization" not in request.headers


@pytest.mark.parametrize("location", ["http://cdn.test/paper", "https://user:secret@cdn.test/paper", "file:///tmp/paper"])
def test_invalid_pdf_redirect_is_rejected(mock_http, tmp_path, location):
    mock_http.responses.append(httpx.Response(303, headers={"Location": location}))
    with pytest.raises(RuntimeError, match="HTTPS"):
        ScopusSearcher(KEY).download_pdf("123", str(tmp_path))
    assert len(mock_http.requests) == 1
    assert not list(tmp_path.iterdir())


def test_pdf_redirect_loop_is_bounded(mock_http, tmp_path):
    mock_http.responses.extend([httpx.Response(303, headers={"Location": "https://cdn.test/loop"}) for _ in range(6)])
    with pytest.raises(RuntimeError, match="did not return a PDF"):
        ScopusSearcher(KEY).download_pdf("123", str(tmp_path))
    assert len(mock_http.requests) == 6
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("response", [httpx.Response(200, text="<html>login</html>", headers={"Content-Type": "application/pdf"}),
                                      httpx.Response(200, content=b""), httpx.Response(403), httpx.Response(404)])
def test_invalid_pdf_does_not_write_or_overwrite(mock_http, tmp_path, response):
    existing = tmp_path / "123.pdf"
    existing.write_bytes(b"existing file")
    mock_http.responses.append(response)
    with pytest.raises(RuntimeError):
        ScopusSearcher(KEY).download_pdf("123", str(tmp_path))
    assert existing.read_bytes() == b"existing file"
    assert list(tmp_path.iterdir()) == [existing]


def test_credentials_are_redacted_from_provider_errors(mock_http):
    mock_http.responses.append(httpx.Response(403, json={"service-error": {
        "status": {"statusText": f"Failure containing {KEY} and {TOKEN}"},
    }}))
    with pytest.raises(ScopusAPIError) as error:
        ScopusSearcher(KEY, TOKEN).search("query")
    assert KEY not in str(error.value)
    assert TOKEN not in str(error.value)


def test_nonpositive_limit_and_blank_query_make_no_requests(mock_http):
    searcher = ScopusSearcher(KEY)
    assert searcher.search("query", 0) == searcher.search("query", -1) == []
    with pytest.raises(ValueError):
        searcher.search("  ")
    assert not mock_http.requests


@pytest.mark.parametrize("sort,expected", [("+relevance", "+relevancy"), ("-relevance", "-relevancy"),
                                          ("relevance,-coverDate", "relevancy,-coverDate")])
def test_relevance_alias_with_direction(mock_http, sort, expected):
    mock_http.responses.append(page([]))
    ScopusSearcher(KEY).search("query", sort=sort)
    assert mock_http.requests[0].url.params["sort"] == expected


def test_missing_entries_cannot_silently_look_empty(mock_http):
    mock_http.responses.append(httpx.Response(200, json={"search-results": {}}))
    with pytest.raises(RuntimeError, match="missing result entries"):
        ScopusSearcher(KEY).search("query")


def test_full_text_keeps_article_abstract_and_references():
    xml = XML.replace("<article><body>", "<article><head><abstract>Article abstract.</abstract></head><body>")
    xml = xml.replace("</body></article>", "</body><tail><bibliography>Reference text.</bibliography></tail></article>")
    text = ScopusSearcher._full_text(httpx.Response(200, text=xml))
    assert "Article abstract." in text
    assert "Reference text." in text
    assert "A complete paragraph." in text


def test_same_origin_pdf_redirect_retains_auth_then_strips_it(mock_http, tmp_path):
    def same_origin(request):
        assert request.headers["X-ELS-APIKey"] == KEY
        assert request.headers["X-ELS-Insttoken"] == TOKEN
        return httpx.Response(303, headers={"Location": "https://cdn.test/final"})
    mock_http.responses.extend([
        httpx.Response(303, headers={"Location": "/redirect/123"}), same_origin,
        httpx.Response(200, content=PDF),
    ])
    assert Path(ScopusSearcher(KEY, TOKEN).download_pdf("123", str(tmp_path))).read_bytes() == PDF
    assert "X-ELS-APIKey" not in mock_http.requests[-1].headers


def test_pdf_redirect_cannot_embed_provider_credentials(mock_http, tmp_path):
    mock_http.responses.append(httpx.Response(303, headers={"Location": f"https://cdn.test/paper?apiKey={KEY}"}))
    with pytest.raises(RuntimeError, match="contains provider credentials"):
        ScopusSearcher(KEY).download_pdf("123", str(tmp_path))
    assert len(mock_http.requests) == 1
    assert not list(tmp_path.iterdir())


def test_pdf_retrieval_reuses_retries_and_full_view(mock_http, sleep, tmp_path):
    mock_http.responses.extend([httpx.Response(503), httpx.Response(200, content=PDF)])
    assert Path(ScopusSearcher(KEY).download_pdf("123", str(tmp_path))).read_bytes() == PDF
    assert mock_http.requests[0].url.params["view"] == "FULL"
    sleep.assert_called_once_with(2)


def test_pdf_missing_redirect_location(mock_http, tmp_path):
    mock_http.responses.append(httpx.Response(303))
    with pytest.raises(RuntimeError, match="Location"):
        ScopusSearcher(KEY).download_pdf("123", str(tmp_path))
    assert not list(tmp_path.iterdir())
