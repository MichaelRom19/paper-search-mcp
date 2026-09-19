"""C02 contracts: saved pages, request accounting, safe failures and resumptions."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import logging

import httpx
import pytest

from paper_search_mcp.http import ProviderHTTP, RequestAllowance, RequestFailure
from paper_search_mcp.provider_models import ProviderPage, SavedQuery
from paper_search_mcp.academic_platforms.google_scholar import GoogleScholarSearcher
from paper_search_mcp.academic_platforms.scopus import ScopusSearcher


@pytest.mark.parametrize("source,adapter,envelope", [
    ("google_scholar", GoogleScholarSearcher, lambda entries: {"search_metadata": {"status": "Success"}, "organic_results": entries}),
    ("scopus", ScopusSearcher, lambda entries: {"search-results": {"opensearch:totalResults": str(len(entries)), "entry": entries}}),
])
def test_empty_malformed_records_and_envelopes(mock_http, source, adapter, envelope):
    query = SavedQuery(source=source, query="test")
    provider = adapter("secret")
    mock_http.responses.extend([httpx.Response(200, json=envelope([])),
        httpx.Response(200, json=envelope([{"api_key": "secret", "url": "https://x.test?api_key=secret"}])),
        httpx.Response(200, json={})])
    empty = provider.search_page(query)
    assert empty.state == "exhausted" and not empty.error and not empty.records
    rejected = provider.search_page(query)
    assert len(rejected.rejected) == 1 and rejected.rejected[0].reason
    assert "secret" not in rejected.model_dump_json()
    malformed = provider.search_page(query)
    assert malformed.state == "failed" and malformed.error.kind == "malformed_response"
    assert all(page.requests_used == 1 for page in (empty, rejected, malformed))


@pytest.mark.parametrize("fields", [{"prism:doi": "invalid"}, {"prism:coverDate": "2024-02-30"}])
def test_scopus_invalid_metadata_is_a_rejected_record(mock_http, fields):
    item = {"dc:identifier": "SCOPUS_ID:123", "dc:title": "A paper", **fields}
    mock_http.responses.append(httpx.Response(200, json={"search-results": {
        "opensearch:totalResults": "1", "entry": [item]}}))
    page = ScopusSearcher("secret").search_page(SavedQuery(source="scopus", query="test"))
    assert page.state == "exhausted" and not page.records
    assert len(page.rejected) == 1 and page.rejected[0].raw == item


def test_resume_wait_and_serialization_preserve_query_and_records(mock_http):
    provider = GoogleScholarSearcher("secret")
    query = SavedQuery(source="google_scholar", query="original", page_size=1)
    mock_http.responses.extend([
        httpx.Response(429, headers={"Retry-After": "120"}),
        httpx.Response(200, json={"search_metadata": {"status": "Success"}, "organic_results": [
            {"title": "One", "result_id": "1", "link": "https://x.test?api_key=secret", "publication_info": {"summary": "A - Journal 2024"}},
            {"title": "Two", "result_id": "2"}], "serpapi_pagination": {"next": "https://evil.test?start=20&api_key=secret"}}),
    ])
    waiting = provider.search_page(query)
    assert waiting.state == "waiting" and waiting.error.retry_at > datetime.now(timezone.utc)
    assert waiting.continuation == {"start": 0}
    page = provider.search_page(SavedQuery.model_validate_json(query.model_dump_json()), waiting.continuation)
    restored = ProviderPage.model_validate_json(page.model_dump_json())
    assert len(restored.records) == 2  # Never truncate a fetched page to its requested/display size.
    assert restored.continuation == {"start": 20}
    assert restored.records[0].date_precision == "year"
    assert restored.records[0].text_kind == "snippet"
    assert restored.records[0].to_paper().published_date.year == 2024
    assert "secret" not in restored.model_dump_json()
    assert all(request.url.params["q"] == "original" for request in mock_http.requests)


@pytest.mark.parametrize("status,headers,body,kind", [
    (401, {}, "secret", "authentication"), (403, {}, "secret", "authentication"),
    (429, {"X-RateLimit-Remaining": "0"}, "", "quota"),
    (429, {}, "Quota exhausted", "quota"), (503, {}, "secret", "service"),
])
def test_safe_errors_and_no_retry_without_wait_allowance(mock_http, caplog, status, headers, body, kind):
    caplog.set_level(logging.DEBUG)
    mock_http.responses.append(httpx.Response(status, headers=headers, text=body))
    with httpx.Client() as client, pytest.raises(RequestFailure) as failure:
        ProviderHTTP("test", "https://api.test", params={"api_key": "secret"}).get(
            client, "https://api.test/search", RequestAllowance())
    assert failure.value.error.kind == kind
    assert "secret" not in str(failure.value) + failure.value.error.model_dump_json() + caplog.text
    assert len(mock_http.requests) == 1


@pytest.mark.parametrize("budget,attempts", [(0, 0), (1, 1), (2, 2), (9, 4)])
def test_retry_ceiling_and_reservations(mock_http, monkeypatch, budget, attempts):
    monkeypatch.setattr("paper_search_mcp.http.time.sleep", lambda _: None)
    mock_http.responses.extend(httpx.Response(503) for _ in range(attempts))
    allowance = RequestAllowance(remaining=budget, wait_seconds=100)
    with httpx.Client() as client, pytest.raises(RequestFailure):
        ProviderHTTP("test", "https://api.test").get(client, "https://api.test/search", allowance)
    assert allowance.used == len(mock_http.requests) == attempts
    assert allowance.remaining == budget - attempts


def test_retry_after_http_date_and_followup_share_allowance(mock_http, monkeypatch):
    sleeps = []
    monkeypatch.setattr("paper_search_mcp.http.time.sleep", sleeps.append)
    date = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=10))
    mock_http.responses.extend([httpx.Response(429, headers={"Retry-After": date}), httpx.Response(200)])
    allowance = RequestAllowance(remaining=2, wait_seconds=20)
    with httpx.Client() as client:
        http = ProviderHTTP("test", "https://api.test")
        assert http.get(client, "https://api.test/search", allowance).status_code == 200
        with pytest.raises(RequestFailure) as failure:
            http.get(client, "https://api.test/metadata", allowance)
    assert failure.value.error.kind == "budget"
    assert 8 <= sleeps[0] <= 10
    assert allowance.used == 2


@pytest.mark.parametrize("url", ["https://evil.test", "http://api.test", "https://api.test:444", "https://user:secret@api.test"])
def test_credentials_never_sent_to_another_origin(mock_http, url):
    with httpx.Client() as client, pytest.raises(RequestFailure) as failure:
        ProviderHTTP("test", "https://api.test", headers={"Authorization": "secret"}).get(client, url, RequestAllowance())
    assert failure.value.error.kind == "unsafe_origin"
    assert not mock_http.requests


def test_redirect_is_not_followed(mock_http):
    mock_http.responses.append(httpx.Response(302, headers={"Location": "https://evil.test"}))
    with httpx.Client(follow_redirects=True) as client, pytest.raises(RequestFailure):
        ProviderHTTP("test", "https://api.test", headers={"Authorization": "secret"}).get(
            client, "https://api.test/search", RequestAllowance())
    assert len(mock_http.requests) == 1


@pytest.mark.parametrize("module,cls", [("openaire", "OpenAiresearcher"), ("citeseerx", "CiteSeerXSearcher")])
def test_certificate_failures_are_never_retried_unverified(monkeypatch, module, cls):
    from importlib import import_module
    from unittest.mock import Mock
    import requests
    provider = getattr(import_module(f"paper_search_mcp.academic_platforms.{module}"), cls)()
    get = Mock(side_effect=requests.exceptions.SSLError("certificate failure"))
    monkeypatch.setattr(provider.session, "get", get)
    with pytest.raises(requests.exceptions.SSLError):
        provider._get("https://example.test")
    get.assert_called_once_with("https://example.test", timeout=30)


def test_transport_retry_counts_and_omits_exception_details(mock_http, monkeypatch):
    monkeypatch.setattr("paper_search_mcp.http.time.sleep", lambda _: None)
    mock_http.responses.extend([httpx.ReadTimeout("secret URL"), httpx.Response(200)])
    allowance = RequestAllowance(remaining=2, wait_seconds=5)
    with httpx.Client() as client:
        assert ProviderHTTP("test", "https://api.test").get(client, "https://api.test", allowance).status_code == 200
    assert allowance.used == 2


def test_sanitization_covers_embedded_url_credentials():
    from paper_search_mcp.http import sanitize
    assert "password" not in sanitize("https://user:password@host.test/path")
    assert "secret" not in sanitize("https://host.test/?authorization=secret&token=secret")


@pytest.mark.parametrize("kwargs", [{"remaining": -1}, {"wait_seconds": -1}])
def test_negative_allowances_are_invalid(kwargs):
    with pytest.raises(ValueError):
        RequestAllowance(**kwargs)


@pytest.mark.parametrize("message,kind", [("Your account has run out of searches", "quota"), ("Invalid API key", "authentication"), ("Temporary service failure", "service")])
def test_scholar_error_envelope_classification(mock_http, message, kind):
    mock_http.responses.append(httpx.Response(200, json={"error": message}))
    page = GoogleScholarSearcher("secret").search_page(SavedQuery(source="google_scholar", query="x"))
    assert page.state == "failed" and page.error.kind == kind
    assert page.requests_used == 1


def test_scopus_month_precision(mock_http):
    mock_http.responses.append(httpx.Response(200, json={"search-results": {
        "opensearch:totalResults": "1", "entry": [{"dc:identifier": "SCOPUS_ID:1", "dc:title": "Title", "prism:coverDate": "2024-06"}]}}))
    record = ScopusSearcher("secret").search_page(SavedQuery(source="scopus", query="x")).records[0]
    assert record.published_date == "2024-06" and record.date_precision == "month"


def test_custom_credential_parameter_is_redacted_from_httpx_logs(mock_http, caplog):
    caplog.set_level(logging.INFO, logger="httpx")
    mock_http.responses.append(httpx.Response(200))
    with httpx.Client() as client:
        ProviderHTTP("test", "https://api.test", params={"custom_auth": "very-secret"}).get(
            client, "https://api.test/search", RequestAllowance())
    assert "very-secret" not in caplog.text
    assert "[REDACTED]" in caplog.text


@pytest.mark.parametrize("value", ["NaN", "inf", "-inf"])
def test_nonfinite_retry_headers_use_backoff(value):
    from paper_search_mcp.http import retry_delay
    assert retry_delay(value, 2) == 2
