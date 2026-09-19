"""Shared page handling and bounded legacy collection."""
from hashlib import sha256
import json
import re

import httpx

from .http import RequestAllowance, RequestFailure, sanitize
from .library import identifiers, validate_date
from .provider_models import ProviderError, ProviderPage, RejectedRecord


def fetch_page(provider, query, continuation, allowance):
    url, params = provider.page_request(query, continuation)
    allowance = allowance if allowance is not None else RequestAllowance()
    before = allowance.used
    usage_start = len(allowance.response_usage)
    page = ProviderPage(continuation=continuation)
    try:
        with httpx.Client() as client:
            response = provider.http.get(client, url, allowance, params=params)
        data = provider.decode_response(response) if hasattr(provider, "decode_response") else response.json()
        entries = provider.page_entries(data)
        for item in entries:
            clean = sanitize(item, provider.http.secrets)
            try:
                record = provider.metadata(clean)
                if not record.paper_id.strip() or not record.title.strip():
                    raise ValueError("Missing identity or title")
                identifiers(record)
                validate_date(record.published_date, record.date_precision)
                page.records.append(record)
            except (ValueError, TypeError, KeyError, AttributeError):
                page.rejected.append(RejectedRecord(raw=clean, reason="Invalid identifier, title or metadata."))
        provider.page_response(data, response, query, continuation, page)
    except RequestFailure as exc:
        page.error = exc.error
        page.state = "budget" if exc.error.kind == "budget" else "waiting" if exc.error.retry_at else "failed"
    except (ValueError, TypeError, KeyError, AttributeError):
        page.error = ProviderError(kind="malformed_response", message="Provider returned an invalid search response.")
        page.state = "failed"
    if allowance.response_usage[usage_start:]:
        page.request_usage["attempts"] = allowance.response_usage[usage_start:]
    page.requests_used = allowance.used - before
    return page


def collect(provider, query, max_results):
    papers, seen = {}, set()
    continuation = None
    while len(papers) < max_results:
        page = provider.search_page(query, continuation)
        if page.error:
            raise RequestFailure(page.error)
        if page.rejected:
            raise ValueError(page.rejected[0].reason)
        for record in page.records:
            papers.setdefault(record.paper_id, record.to_paper())
        if len(papers) >= max_results or page.state == "exhausted":
            break
        if page.state == "provider_cap":
            raise RequestFailure(ProviderError(kind="provider_cap", message="Provider retrieval ceiling reached."))
        checkpoint = repr(page.continuation)
        if page.continuation is None or checkpoint in seen:
            raise RequestFailure(ProviderError(kind="nonadvancing", message="Provider continuation did not advance."))
        seen.add(checkpoint)
        continuation = page.continuation
    return list(papers.values())[:max_results]


def semantic_mode(query):
    """Resolve endpoint semantics before saving a run or making a request."""
    mode = "relevance" if query.mode == "relevance" or query.sort == "relevance" else "bulk"
    if mode == "relevance" and query.sort not in (None, "relevance"):
        raise ValueError("Relevance mode does not support bulk sorting.")
    year = query.filters.get("year")
    if year is not None and (not isinstance(year, str) or
            not re.fullmatch(r"\d{4}|\d{4}-\d{4}|\d{4}-|-\d{4}", year) or
            len(year) == 9 and year[:4] > year[5:]):
        raise ValueError("Invalid Semantic Scholar year filter.")
    return mode


def page_fingerprint(identities):
    """Compare native record identities, including rejected entries, independent of order."""
    return sha256(json.dumps(sorted(json.dumps(item, sort_keys=True) for item in identities)).encode()).hexdigest()
