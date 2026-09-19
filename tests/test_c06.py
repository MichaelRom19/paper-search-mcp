"""Crossref/arXiv contracts and persisted retrieval, without live requests."""
import json
from types import SimpleNamespace
from xml.sax.saxutils import escape

import httpx
import pytest

from paper_search_mcp.academic_platforms.arxiv import ArxivSearcher
from paper_search_mcp.academic_platforms.crossref import CrossRefSearcher
from paper_search_mcp.http import RequestAllowance, RequestFailure
from paper_search_mcp.provider_models import SavedQuery
from paper_search_mcp.review_models import ReviewProtocol, SearchRequest
from paper_search_mcp.reviews import Reviews

PROVIDERS = {"crossref": CrossRefSearcher, "arxiv": ArxivSearcher}


@pytest.fixture(autouse=True)
def spacing_clock(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPER_SEARCH_MCP_DATA_DIR", str(tmp_path))
    clock = SimpleNamespace(now=1000, waits=[])
    def sleep(delay):
        clock.waits.append(delay)
        clock.now += delay
    monkeypatch.setattr("paper_search_mcp.http.time", SimpleNamespace(time=lambda: clock.now, sleep=sleep))
    return clock


def entry(identifier="2001.12345v1", title="A study", **fields):
    body = f'<id>https://arxiv.org/abs/{identifier}</id><title>{escape(title)}</title>'
    body += '<published>2020-01-02T12:00:00Z</published><updated>2020-02-03T00:00:00Z</updated>'
    body += '<author><name>Alice</name></author><summary>Mentions 10.1234/unrelated</summary>'
    body += ''.join(f'<arxiv:{key}>{escape(value)}</arxiv:{key}>' for key, value in fields.items())
    return f'<entry>{body}</entry>'


def response(source, ids, *, cursor="next", start=0, total=10):
    if source == "crossref":
        return httpx.Response(200, json={"status": "ok", "message": {"items": [
            {"DOI": f"10.1234/{identifier}", "title": ["A study"]} for identifier in ids],
            "total-results": total, "next-cursor": cursor}})
    body = ''.join(entry(identifier) for identifier in ids)
    return atom(body, start=start, total=total)


def atom(body, start=0, total=1):
    return httpx.Response(200, text=f'''<feed xmlns="http://www.w3.org/2005/Atom"
        xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/" xmlns:arxiv="http://arxiv.org/schemas/atom">
        <opensearch:totalResults>{total}</opensearch:totalResults>
        <opensearch:startIndex>{start}</opensearch:startIndex>{body}</feed>''')


def start_review(tmp_path, source, **options):
    service = Reviews(tmp_path)
    query = SavedQuery(source=source, query='ti:"A study" AND cat:cs.AI', page_size=2, **options)
    review = service.create_review(ReviewProtocol(question="Test", selected_sources=[source], queries=[query]),
                                   idempotency_key="review")["review_id"]
    run = service.start_search(SearchRequest(review_id=review, request_budget=8), idempotency_key="run")["run_id"]
    return service, review, run


@pytest.mark.parametrize("source", PROVIDERS)
def test_restart_overlap_versions_and_replay(source, tmp_path, mock_http):
    service, review, run = start_review(tmp_path, source)
    ids = ["2001.12345v1", "2001.12345v2", "hep-ex/0307015v1"]
    mock_http.responses = [response(source, ids[:2], cursor="opaque+/=", total=5),
                           response(source, [ids[1], ids[2]], start=2, cursor="last", total=5),
                           response(source, ids[:1], start=4, total=5)]
    first = service.advance_run(run, idempotency_key="one")
    assert first["unique_publications"] == 2
    assert Reviews(tmp_path).advance_run(run, idempotency_key="one") == first
    second = Reviews(tmp_path).advance_run(run, idempotency_key="two")
    assert second["unique_publications"] == 3
    assert second["state"]["sources"][source]["linked_duplicates"] == 1
    final = Reviews(tmp_path).advance_run(run, idempotency_key="three")
    progress = final["state"]["sources"][source]
    assert progress["status"] == "exhausted" and progress["received"] == 5
    assert final["unique_publications"] == 3 and progress["linked_duplicates"] == 2
    assert len(service.query_review(review)["papers"]) == 3
    assert len(mock_http.requests) == 3
    params = [dict(request.url.params) for request in mock_http.requests]
    if source == "crossref":
        assert [p.pop("cursor") for p in params] == ["*", "opaque+/=", "last"]
    else:
        assert [p.pop("start") for p in params] == ["0", "2", "4"]
    assert params[0] == params[1] == params[2]


@pytest.mark.parametrize("sort", ["issued", "published", "published-print", "published-online"])
def test_crossref_disallowed_sort_is_invalid_before_network(sort, tmp_path, mock_http):
    service, _, run = start_review(tmp_path, "crossref", sort=sort)
    state = service.advance_run(run, idempotency_key="advance")["state"]["sources"]["crossref"]
    assert state["status"] == "invalid" and "cursor" in state["validation"][0]
    with pytest.raises(ValueError, match="cursor"):
        CrossRefSearcher().search("test", sort=sort)
    assert not mock_http.requests


@pytest.mark.parametrize("source,options", [
    ("crossref", {"filters": {"filter": 2}}), ("crossref", {"filters": {"order": "ascending"}}),
    ("arxiv", {"filters": {"sort_order": "desc"}}), ("arxiv", {"sort": "published"}),
    ("arxiv", {"filters": {"year": 2020}})])
def test_invalid_query_options(source, options, tmp_path, mock_http):
    service, _, run = start_review(tmp_path, source, **options)
    assert service.get_run(run)["state"]["sources"][source]["status"] == "invalid"
    assert not mock_http.requests


def test_crossref_parameters_references_and_partial_dates(mock_http):
    references = [{"key": "r1", "DOI": "10.1234/reference"}, {"key": "r2", "unstructured": "Unresolved citation"}]
    item = {"DOI": "10.1234/work", "title": ["Work"], "published": {"date-parts": [[2024, 2]]},
            "reference": references, "author": [{"name": "Study group"}], "is-referenced-by-count": 3}
    mock_http.responses = [httpx.Response(200, json={"message": {"items": [item], "total-results": 20, "next-cursor": "abc+/="}}),
                           response("crossref", [], cursor="end")]
    query = SavedQuery(source="crossref", query="native & query", page_size=1, sort="created",
                       filters={"filter": "from-created-date:2020-01-01,type:journal-article", "order": "asc"})
    provider = CrossRefSearcher()
    first = provider.search_page(query)
    assert first.records[0].extra["deposited_references"] == references
    assert first.records[0].references == ["10.1234/reference"]
    assert first.records[0].published_date == "2024-02" and first.records[0].date_precision == "month"
    assert first.records[0].authors[0].name == "Study group"
    assert provider.search_page(query, first.continuation).state == "exhausted"
    a, b = [dict(request.url.params) for request in mock_http.requests]
    assert a.pop("cursor") == "*" and b.pop("cursor") == "abc+/="
    assert a == b and a["sort"] == "created" and a["order"] == "asc"


@pytest.mark.parametrize("source", PROVIDERS)
def test_repeated_identity_with_changed_metadata_retains_page(source, mock_http):
    query = SavedQuery(source=source, query="test", page_size=1)
    mock_http.responses = [response(source, ["2001.12345v1"], cursor="a"),
                           response(source, ["2001.12345v1"], cursor="b", start=1)]
    mock_http.responses[1] = httpx.Response(200, content=mock_http.responses[1].content.replace(b"A study", b"Updated study"))
    provider = PROVIDERS[source]()
    first = provider.search_page(query)
    second = provider.search_page(query, first.continuation)
    assert second.error.kind == "nonadvancing" and len(second.records) == 1


@pytest.mark.parametrize("source", PROVIDERS)
def test_late_failure_and_waiting_resume(source, tmp_path, mock_http):
    service, _, run = start_review(tmp_path, source)
    mock_http.responses = [response(source, ["2001.12345v1", "2001.12346v1"], total=3),
        httpx.Response(429, headers={"Retry-After": "0"}),
        response(source, ["2001.12347v1"], start=2, total=3)]
    first = service.advance_run(run, idempotency_key="one")
    waiting = service.advance_run(run, idempotency_key="two", max_requests=1)
    assert waiting["state"]["sources"][source]["status"] == "waiting"
    assert waiting["unique_publications"] == 2
    assert waiting["state"]["sources"][source]["continuation"] == first["state"]["sources"][source]["continuation"]
    last = Reviews(tmp_path).advance_run(run, idempotency_key="three")
    assert last["unique_publications"] == 3 and last["state"]["requests_used"] == 3
    assert mock_http.requests[1].url == mock_http.requests[2].url


@pytest.mark.parametrize("source", PROVIDERS)
@pytest.mark.parametrize("status,headers,kind", [(401, {}, "authentication"), (429, {"X-RateLimit-Remaining": "0"}, "quota"),
                                               (503, {}, "service"), (400, {}, "expired_cursor")])
def test_errors_not_empty_success(source, status, headers, kind, mock_http):
    mock_http.responses = [httpx.Response(status, headers=headers, text="Invalid expired cursor")]
    with pytest.raises(RequestFailure) as error:
        PROVIDERS[source]().search("test")
    assert error.value.error.kind == kind


@pytest.mark.parametrize("body", ["<broken", "<html>challenge</html>", '<feed xmlns="http://www.w3.org/2005/Atom"/>'])
def test_arxiv_malformed_envelopes(body, mock_http):
    mock_http.responses = [httpx.Response(200, text=body)]
    page = ArxivSearcher().search_page(SavedQuery(source="arxiv", query="test"))
    assert page.state == "failed" and page.error.kind == "malformed_response"


def test_arxiv_error_feed_and_rejected_record(mock_http):
    mock_http.responses = [atom('<entry><id>http://arxiv.org/api/errors#bad_query</id><title>Error</title></entry>'),
                           atom(entry("bad-id") + entry("hep-ex/0307015v2", doi="10.1234/published"), total=2)]
    provider = ArxivSearcher()
    assert provider.search_page(SavedQuery(source="arxiv", query="test")).error.kind == "malformed_response"
    page = provider.search_page(SavedQuery(source="arxiv", query="test"))
    assert page.state == "exhausted" and len(page.rejected) == len(page.records) == 1
    record = page.records[0]
    assert record.paper_id == "hep-ex/0307015v2" and record.version == "v2"
    assert record.identifiers == {"arxiv": "hep-ex/0307015", "arxiv_version": "hep-ex/0307015v2"}
    assert record.doi == "10.1234/published"


@pytest.mark.parametrize("total,ids,state", [(30_001, ["2001.12345v1"], "provider_cap"), (30_000, ["2001.12345v1"], "exhausted"),
                                           (30_001, [], "failed")])
def test_arxiv_ceiling_and_premature_empty(total, ids, state, mock_http):
    mock_http.responses = [response("arxiv", ids, start=29_999, total=total)]
    page = ArxivSearcher().search_page(SavedQuery(source="arxiv", query="test"), {"offset": 29_999})
    assert page.state == state and mock_http.requests[0].url.params["max_results"] == "1"


@pytest.mark.parametrize("source", PROVIDERS)
def test_legacy_collects_multiple_pages(source, mock_http):
    mock_http.responses = [response(source, ["2001.12345v1", "2001.12346v1"], total=3),
                           response(source, ["2001.12347v1"], start=2, total=3)]
    # Crossref ends on a short page, so ask for two full records then one more via explicit pages.
    if source == "crossref":
        mock_http.responses[0] = response(source, ["2001.12345v1", "2001.12346v1", "2001.12345v1"], total=4)
    papers = PROVIDERS[source]().search("test", max_results=3)
    assert len(papers) == 3 and len(mock_http.requests) == 2
    if source == "arxiv":
        assert mock_http.requests[0].url.params["search_query"] == "all:test"
        assert papers[0].doi == ""  # No DOI inference from abstract prose.


def test_spacing_between_instances_retries_and_budget(tmp_path, mock_http, spacing_clock):
    query = SavedQuery(source="arxiv", query="test")
    mock_http.responses = [response("arxiv", [], total=0), httpx.Response(503, headers={"Retry-After": "0"}),
                           response("arxiv", [], total=0)]
    assert ArxivSearcher().search_page(query).requests_used == 1
    allowance = RequestAllowance(remaining=2)
    assert ArxivSearcher().search_page(query, allowance=allowance).requests_used == 2
    assert spacing_clock.waits == [0, 3, 0, 3]
    assert float((tmp_path / "arxiv-request-time").read_text()) == 1009
    assert ArxivSearcher().search_page(query, allowance=RequestAllowance(remaining=0)).state == "budget"
    assert len(mock_http.requests) == 3 and spacing_clock.now == 1006


@pytest.mark.parametrize("source", PROVIDERS)
def test_cli_uses_review_adapters(source, tmp_path, monkeypatch, capsys, mock_http):
    from paper_search_mcp.cli import main
    service, review, run = start_review(tmp_path, source)
    mock_http.responses = [response(source, ["2001.12345v1"], total=1)]
    monkeypatch.setattr("sys.argv", ["paper-search", "advance-run", run, "--idempotency-key", "cli"])
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 0
    assert json.loads(capsys.readouterr().out)["unique_publications"] == 1
    assert len(service.query_review(review)["papers"]) == 1


@pytest.mark.parametrize("source", PROVIDERS)
def test_received_page_recovers_after_interrupted_ingestion(source, tmp_path, mock_http, monkeypatch):
    service, _, run = start_review(tmp_path, source)
    mock_http.responses = [response(source, ["2001.12345v1", "2001.12346v1"], total=3),
                           response(source, ["2001.12347v1"], start=2, total=3)]
    def interrupted(*args):
        raise RuntimeError("Process ended after page journal")
    monkeypatch.setattr(service, "_apply_page", interrupted)
    with pytest.raises(RuntimeError):
        service.advance_run(run, idempotency_key="one")
    restarted = Reviews(tmp_path)
    recovered = restarted.advance_run(run, idempotency_key="one")
    assert recovered["interrupted"] and recovered["unique_publications"] == 2
    assert len(mock_http.requests) == 1
    assert restarted.advance_run(run, idempotency_key="two")["unique_publications"] == 3
    assert len(mock_http.requests) == 2


@pytest.mark.parametrize("source", PROVIDERS)
def test_malformed_later_page_preserves_earlier_records(source, tmp_path, mock_http):
    service, _, run = start_review(tmp_path, source)
    mock_http.responses = [response(source, ["2001.12345v1", "2001.12346v1"]), httpx.Response(200, text="<html>challenge</html>")]
    service.advance_run(run, idempotency_key="one")
    last = service.advance_run(run, idempotency_key="two")
    assert last["unique_publications"] == 2
    assert last["state"]["sources"][source]["error"]["kind"] == "malformed_response"


def test_crossref_record_rejection_empty_and_lookup(mock_http):
    provider = CrossRefSearcher()
    mock_http.responses = [httpx.Response(200, json={"message": {"total-results": 3, "items": [
        {"DOI": "10.1234/work", "title": ["Work"]}, {"DOI": "10.1234/missing-title"}, None]}}),
        response("crossref", [], total=0), httpx.Response(404), httpx.Response(503)]
    page = provider.search_page(SavedQuery(source="crossref", query="test"))
    assert len(page.rejected) == 2 and len(page.records) == 1
    assert page.records[0].published_date is None and page.records[0].date_precision == "unknown"
    assert provider.search("empty") == []
    assert provider.get_paper_by_doi("10.1234/missing") is None
    with pytest.raises(RequestFailure):
        provider.get_paper_by_doi("10.1234/failure")


def test_crossref_bad_cursor_preserves_received_records_and_checkpoint(tmp_path, mock_http):
    service, review, run = start_review(tmp_path, "crossref")
    mock_http.responses = [response("crossref", ["a", "b"], cursor="first"),
                           response("crossref", ["c", "d"], cursor=None)]
    service.advance_run(run, idempotency_key="first")
    result = service.advance_run(run, idempotency_key="bad-cursor")
    state = result["state"]["sources"]["crossref"]
    assert state["status"] == "failed" and state["error"]["kind"] == "malformed_response"
    assert state["continuation"]["cursor"] == "first"
    assert state["received"] == result["unique_publications"] == 4
    assert len(service.query_review(review)["papers"]) == 4
    assert service.advance_run(run, idempotency_key="bad-cursor") == result


def test_spacing_timestamp_is_observed_by_a_new_process(tmp_path, spacing_clock):
    import os
    import subprocess
    import sys
    (tmp_path / "arxiv-request-time").write_text("1003")
    code = '''
from types import SimpleNamespace
from paper_search_mcp import http
waits = []
http.time = SimpleNamespace(time=lambda: 1000, sleep=waits.append)
with http.ProviderHTTP("arxiv", "https://export.arxiv.org", interval=3).request_slot():
    pass
assert waits == [3], waits
'''
    subprocess.run([sys.executable, "-c", code], env={**os.environ, "PAPER_SEARCH_MCP_DATA_DIR": str(tmp_path)},
                   capture_output=True, text=True, check=True, timeout=10)
