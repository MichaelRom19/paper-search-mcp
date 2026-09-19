"""C04 protocol, request-accounting and restart contracts; all providers are injected."""
from concurrent.futures import ThreadPoolExecutor
import json

import httpx
import pytest

from paper_search_mcp.provider_models import Metadata, ProviderError, ProviderPage, ReportedTotal, SavedQuery
from paper_search_mcp.review_models import ReviewProtocol, SearchRequest
from paper_search_mcp.reviews import Reviews


def protocol(sources=("scopus",), **kwargs):
    return ReviewProtocol(question="Which interventions work?", selected_sources=list(sources),
        queries=[SavedQuery(source=s, query="TITLE(test)") for s in sources], **kwargs)


class Fixture:
    def __init__(self):
        self.calls = []
        self.pages = []

    def search_page(self, query, continuation, *, allowance):
        allowance.reserve()
        self.calls.append((query, continuation))
        page = self.pages.pop(0)
        if isinstance(page, BaseException):
            raise page
        return page


def page(identifier="1", *, source="scopus", last=False):
    return ProviderPage(records=[Metadata(paper_id=identifier, source=source, title="Study " + identifier)],
        continuation=None if last else {"cursor": identifier, "received": int(identifier)},
        state="exhausted" if last else "ready", total=ReportedTotal(value=30, precision="exact"))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPER_SEARCH_MCP_SCOPUS_API_KEY", "fixture")
    monkeypatch.setenv("PAPER_SEARCH_MCP_SERPAPI_API_KEY", "fixture")
    fixture = Fixture()
    service = Reviews(tmp_path, provider_factory=lambda _: fixture)
    review = service.create_review(protocol(), idempotency_key="review")["review_id"]
    run = service.start_search(SearchRequest(review_id=review, request_budget=10), idempotency_key="run")["run_id"]
    return service, fixture, review, run


def test_protocol_history_frozen_queries_and_no_network(setup):
    service, fixture, review, run = setup
    service.update_review(review, protocol(("google_scholar",)), idempotency_key="update")
    assert service.get_review(review)["revision"] == 2
    assert len(service.get_review(review)["revisions"]) == 2
    saved = service.get_run(run)
    assert saved["specification"]["selected_sources"] == ["scopus"]
    assert saved["specification"]["protocol_revision"] == 1
    assert not fixture.calls
    assert service.list_reviews()["reviews"][0]["review_id"] == review


def test_resume_late_failure_and_idempotency(setup):
    service, fixture, review, run = setup
    fixture.pages = [page(), ProviderPage(state="failed", error=ProviderError(kind="authentication", message="Denied"))]
    first = service.advance_run(run, idempotency_key="a")
    assert first["unique_publications"] == 1
    restarted = Reviews(service.db.path.parent, provider_factory=lambda _: fixture)
    assert restarted.advance_run(run, idempotency_key="a") == first
    second = restarted.advance_run(run, idempotency_key="b")
    assert second["unique_publications"] == 1
    assert second["state"]["sources"]["scopus"]["status"] == "failed"
    assert second["state"]["sources"]["scopus"]["total"]["value"] == 30
    assert fixture.calls[1][1] == {"cursor": "1", "received": 1}
    assert len(service.query_review(review)["papers"]) == 1


def test_simultaneous_advances_fetch_once(setup):
    service, fixture, _, run = setup
    fixture.pages = [page()]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: service.advance_run(run, idempotency_key="same"), range(4)))
    assert all(r == results[0] for r in results)
    assert len(fixture.calls) == 1
    with pytest.raises(ValueError, match="different input"):
        service.advance_run(run, idempotency_key="same", max_requests=1)


def test_recovery_after_received_page_before_ingestion(setup, monkeypatch):
    service, fixture, _, run = setup
    fixture.pages = [page(), page("2", last=True)]
    monkeypatch.setattr(service, "_apply_page", lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        service.advance_run(run, idempotency_key="interrupted")
    restarted = Reviews(service.db.path.parent, provider_factory=lambda _: fixture)
    saved = restarted.advance_run(run, idempotency_key="interrupted")
    assert saved["unique_publications"] == 1 and saved["interrupted"]
    assert len(fixture.calls) == 1
    complete = restarted.advance_run(run, idempotency_key="next")
    assert complete["unique_publications"] == 2
    assert complete["state"]["sources"]["scopus"]["status"] == "exhausted"


def test_interrupted_request_is_spent_and_same_key_never_refetches(setup):
    service, fixture, _, run = setup
    fixture.pages = [KeyboardInterrupt(), page(last=True)]
    with pytest.raises(KeyboardInterrupt):
        service.advance_run(run, idempotency_key="interrupted")
    restarted = Reviews(service.db.path.parent, provider_factory=lambda _: fixture)
    outcome = restarted.advance_run(run, idempotency_key="interrupted")
    assert outcome["state"]["requests_used"] == 1
    assert len(fixture.calls) == 1
    assert restarted.advance_run(run, idempotency_key="retry")["state"]["requests_used"] == 2


def test_validation_reports_unknown_unavailable_and_unsupported(setup, monkeypatch):
    service, fixture, _, _ = setup
    monkeypatch.delenv("PAPER_SEARCH_MCP_SCOPUS_API_KEY")
    result = service.create_review(protocol(("missing", "scopus", "pubmed")), idempotency_key="invalid-review")
    assert all(not item["valid"] for item in result["validation"])
    run = service.start_search(SearchRequest(review_id=result["review_id"], request_budget=3), idempotency_key="invalid-run")
    state = service.advance_run(run["run_id"], idempotency_key="invalid-advance")
    assert set(state["state"]["sources"]) == {"missing", "scopus", "pubmed"}
    assert not fixture.calls


def test_invalid_filters_and_sort_do_not_fetch(setup):
    service, fixture, review, _ = setup
    query = SavedQuery(source="scopus", query="test", filters={"language": "English"}, sort="unknown")
    run = service.start_search(SearchRequest(review_id=review, request_budget=2, queries=[query]), idempotency_key="filters")
    assert any(not item["valid"] for item in run["validation"])
    assert service.advance_run(run["run_id"], idempotency_key="filters-advance")["status"] == "stopped"
    assert not fixture.calls


def test_provider_failure_isolated_and_counts_are_distinct(setup):
    service, fixture, review, _ = setup
    service.update_review(review, protocol(("scopus", "google_scholar")), idempotency_key="two")
    run = service.start_search(SearchRequest(review_id=review, request_budget=5), idempotency_key="two-run")["run_id"]
    fixture.pages = [RuntimeError("secret"), page(source="google_scholar", last=True)]
    result = service.advance_run(run, idempotency_key="two-advance")
    assert result["unique_publications"] == 1
    assert result["state"]["sources"]["scopus"]["status"] == "failed"
    assert "secret" not in json.dumps(result)
    assert result["state"]["sources"]["google_scholar"]["received"] == 1


def test_whole_page_and_duplicates_never_imply_exhaustion(setup):
    service, fixture, review, run = setup
    records = [Metadata(paper_id=str(i), source="scopus", title="Study " + str(i)) for i in range(25)]
    fixture.pages = [ProviderPage(records=records, continuation={"cursor": "a"}),
                     ProviderPage(records=list(reversed(records)), continuation={"cursor": "b"})]
    service.advance_run(run, idempotency_key="whole")
    result = service.advance_run(run, idempotency_key="overlap")
    assert result["unique_publications"] == 25
    source = result["state"]["sources"]["scopus"]
    assert source["received"] == 50 and source["linked_duplicates"] == 25
    assert source["status"] == "ready"
    assert len(service.query_review(review)["papers"]) == 20


def test_repeated_page_stops_but_is_preserved(setup):
    service, fixture, _, run = setup
    fixture.pages = [page(), page()]
    service.advance_run(run, idempotency_key="first")
    result = service.advance_run(run, idempotency_key="repeat")
    assert result["state"]["sources"]["scopus"]["error"]["kind"] == "nonadvancing"
    assert result["state"]["sources"]["scopus"]["received"] == 2


def test_bad_individual_metadata_preserved_as_rejection(setup):
    service, fixture, _, run = setup
    fixture.pages = [ProviderPage(records=[Metadata(paper_id="bad", source="scopus", title="Bad", doi="invalid")], state="exhausted")]
    result = service.advance_run(run, idempotency_key="bad")
    source = result["state"]["sources"]["scopus"]
    assert source["received"] == source["rejected"] == 1
    with service.db.transaction() as connection:
        assert json.loads(connection.execute("SELECT payload FROM pages").fetchone()[0])["rejected"][0]["raw"]["doi"] == "invalid"


def test_http_retries_and_total_budget_are_durable(setup, mock_http):
    service, _, review, _ = setup
    from paper_search_mcp.academic_platforms.scopus import ScopusSearcher
    service.provider_factory = lambda _: ScopusSearcher("fixture")
    run = service.start_search(SearchRequest(review_id=review, request_budget=2), idempotency_key="budget-run")["run_id"]
    mock_http.responses = [httpx.Response(503, headers={"Retry-After": "0"}), httpx.Response(503, headers={"Retry-After": "0"})]
    result = service.advance_run(run, idempotency_key="budget-advance")
    assert result["state"]["requests_used"] == 2
    assert result["status"] == "budget"
    service.advance_run(run, idempotency_key="no-budget")
    assert len(mock_http.requests) == 2


def test_waiting_does_not_send_early_request(setup, mock_http):
    service, _, _, run = setup
    from paper_search_mcp.academic_platforms.scopus import ScopusSearcher
    service.provider_factory = lambda _: ScopusSearcher("fixture")
    mock_http.responses = [httpx.Response(429, headers={"Retry-After": "3600"})]
    service.advance_run(run, idempotency_key="wait")
    assert service.advance_run(run, idempotency_key="early")["state"]["requests_used"] == 1
    assert len(mock_http.requests) == 1


def test_scopus_cursor_and_complete_view(mock_http):
    from paper_search_mcp.academic_platforms.scopus import ScopusSearcher
    provider = ScopusSearcher("fixture")
    mock_http.responses = [httpx.Response(200, json={"search-results": {"opensearch:totalResults": "2",
        "entry": [{"dc:identifier": "SCOPUS_ID:1", "dc:title": "First"}], "cursor": {"@next": "next+/="}}}),
        httpx.Response(200, json={"search-results": {"opensearch:totalResults": "2",
        "entry": [{"dc:identifier": "SCOPUS_ID:2", "dc:title": "Second"}]}})]
    query = SavedQuery(source="scopus", query="TITLE(test)", page_size=100)
    first = provider.search_page(query)
    second = provider.search_page(query, first.continuation)
    assert second.state == "exhausted"
    assert mock_http.requests[0].url.params["cursor"] == "*"
    assert mock_http.requests[1].url.params["cursor"] == "next+/="
    assert all(r.url.params["count"] == "25" and r.url.params["view"] == "COMPLETE" and "start" not in r.url.params for r in mock_http.requests)


def test_scholar_ceiling_and_cache_default(mock_http):
    from paper_search_mcp.academic_platforms.google_scholar import GoogleScholarSearcher
    provider = GoogleScholarSearcher("fixture")
    query = SavedQuery(source="google_scholar", query="test")
    mock_http.responses = [httpx.Response(200, json={"search_metadata": {"status": "Success"},
        "organic_results": [{"title": "Study", "snippet": "Discovery text"}],
        "serpapi_pagination": {"next": "https://serpapi.com/search?start=1000"}})]
    result = provider.search_page(query, {"start": 980})
    assert result.state == "provider_cap" and result.records[0].text_kind == "snippet"
    assert "no_cache" not in mock_http.requests[0].url.params
    assert provider.search_page(query, {"start": 1000}).requests_used == 0


def test_resolved_dates_are_frozen_and_missing_queries_visible(setup):
    service, _, review, _ = setup
    query = SavedQuery(source="scopus", query="test", filters={"date": "2020-"})
    run = service.start_search(SearchRequest(review_id=review, request_budget=2, queries=[query]), idempotency_key="date-run")
    saved = service.get_run(run["run_id"])["specification"]["queries"][0]
    assert saved["filters"]["date"].startswith("2020-") and len(saved["filters"]["date"]) == 9
    result = service.start_search(SearchRequest(review_id=review, request_budget=2, queries=[]), idempotency_key="missing-query")
    assert any("native query" in (item["message"] or "") for item in result["validation"])


def test_per_provider_budget_preserves_other_source(setup):
    service, fixture, review, _ = setup
    service.update_review(review, protocol(("scopus", "google_scholar")), idempotency_key="protocol")
    run = service.start_search(SearchRequest(review_id=review, request_budget=5,
        provider_budgets={"scopus": 1}), idempotency_key="limited")["run_id"]
    fixture.pages = [page(), page("2", source="google_scholar"), page("3", source="google_scholar", last=True)]
    service.advance_run(run, idempotency_key="first-batch")
    result = service.advance_run(run, idempotency_key="second-batch")
    assert result["state"]["requests_used"] == 3
    assert result["state"]["sources"]["scopus"]["status"] == "budget"
    assert result["state"]["sources"]["google_scholar"]["status"] == "exhausted"


def test_expired_cursor_is_not_empty_success(setup, mock_http):
    service, _, _, run = setup
    from paper_search_mcp.academic_platforms.scopus import ScopusSearcher
    service.provider_factory = lambda _: ScopusSearcher("fixture")
    mock_http.responses = [httpx.Response(400, text='{"error": "Cursor expired"}')]
    result = service.advance_run(run, idempotency_key="expired")
    assert result["state"]["sources"]["scopus"]["error"]["kind"] == "expired_cursor"


def test_cli_protocol_and_search_operations(setup, monkeypatch, capsys, tmp_path):
    from paper_search_mcp.cli import main
    service, _, review, run = setup
    monkeypatch.setenv("PAPER_SEARCH_MCP_DATA_DIR", str(service.db.path.parent))
    def invoke(*args):
        monkeypatch.setattr("sys.argv", ["paper-search", *args])
        with pytest.raises(SystemExit) as result:
            main()
        assert result.value.code == 0
        return json.loads(capsys.readouterr().out)
    path = tmp_path / "protocol.json"
    path.write_text(protocol().model_dump_json())
    created = invoke("create-review", str(path), "--idempotency-key", "cli-create")
    assert invoke("get-review", created["review_id"])["protocol"]["question"]
    assert invoke("update-review", created["review_id"], str(path), "--idempotency-key", "cli-update")["revision"] == 2
    assert len(invoke("list-reviews")["reviews"]) == 2
    path.write_text(SearchRequest(review_id=review, request_budget=1).model_dump_json())
    started = invoke("start-search", str(path), "--idempotency-key", "cli-start")
    assert invoke("get-run", started["run_id"])["remaining_requests"] == 1
    fixture = Fixture()
    fixture.pages = [page(last=True)]
    monkeypatch.setattr("paper_search_mcp.reviews.Reviews", lambda: Reviews(service.db.path.parent, provider_factory=lambda _: fixture))
    assert invoke("advance-run", run, "--idempotency-key", "cli-advance")["unique_publications"] == 1


def test_process_death_releases_lock_and_retains_request_reservation(setup):
    import subprocess
    import sys
    service, fixture, _, run = setup
    script = '''
import os, sys
from paper_search_mcp import config
config._ENV_LOADED = True
from paper_search_mcp.reviews import Reviews
class Crash:
    def search_page(self, query, continuation, *, allowance):
        allowance.reserve()
        os._exit(23)
Reviews(sys.argv[1], provider_factory=lambda _: Crash()).advance_run(sys.argv[2], idempotency_key="process-crash")
'''
    result = subprocess.run([sys.executable, "-c", script, str(service.db.path.parent), run], timeout=15)
    assert result.returncode == 23
    recovered = service.advance_run(run, idempotency_key="process-crash")
    assert recovered["interrupted"] and recovered["state"]["requests_used"] == 1
    assert not fixture.calls
    fixture.pages = [page(last=True)]
    assert service.advance_run(run, idempotency_key="after-crash")["unique_publications"] == 1


def test_exhaustion_on_last_budgeted_request_is_not_a_budget_stop(setup):
    service, fixture, review, _ = setup
    run = service.start_search(SearchRequest(review_id=review, request_budget=1), idempotency_key="last-request")["run_id"]
    fixture.pages = [page(last=True)]
    result = service.advance_run(run, idempotency_key="last-page")
    assert result["remaining_requests"] == 0
    assert result["status"] == "stopped"
    assert result["state"]["sources"]["scopus"]["status"] == "exhausted"
