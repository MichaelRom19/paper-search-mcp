"""Google Scholar discovery through SerpAPI; no direct Scholar requests."""

from datetime import datetime
from hashlib import sha256
import re
from urllib.parse import parse_qs, quote, quote_plus, urlsplit

import httpx

from ..config import get_env
from ..paper import Paper
from ..http import ProviderHTTP, RequestAllowance, RequestFailure, sanitize
from ..library import identifiers, validate_date
from ..provider_models import Metadata, SavedQuery, ProviderPage, ProviderError, RejectedRecord, ReportedTotal
from ..utils import extract_doi
from .base import PaperSource


class GoogleScholarSearcher(PaperSource):
    """Search SerpAPI and return the project's standard paper metadata."""

    SEARCH_URL = "https://serpapi.com/search.json"

    def __init__(self, api_key: str | None = None):
        self.api_key = (get_env("SERPAPI_API_KEY") if api_key is None else api_key).strip()
        if not self.api_key:
            raise ValueError("Set PAPER_SEARCH_MCP_SERPAPI_API_KEY (or SERPAPI_API_KEY) to enable Google Scholar.")

    @property
    def http(self):
        return ProviderHTTP("SerpAPI", self.SEARCH_URL, params={"api_key": self.api_key})

    def _page(self, client: httpx.Client, query: str, start: int, count: int, allowance: RequestAllowance) -> dict:
        response = self.http.get(client, self.SEARCH_URL, allowance, params={
            "engine": "google_scholar", "q": query, "hl": "en", "start": start, "num": count,
        })
        try:
            data = response.json()
        except ValueError:
            raise RuntimeError(f"SerpAPI returned invalid JSON (HTTP {response.status_code}).") from None
        if not isinstance(data, dict):
            raise RuntimeError("SerpAPI returned an invalid response object.")

        metadata = data.get("search_metadata")
        status = metadata.get("status") if isinstance(metadata, dict) else None
        info = data.get("search_information") or {}
        empty = isinstance(info, dict) and (
            str(info.get("organic_results_state", "")).lower() == "fully empty"
            or str(info.get("total_results")) == "0"
        )
        entries = data.get("organic_results")
        if response.status_code == 200 and status == "Success":
            if empty and not entries:
                return {"organic_results": []}
            if not data.get("error") and isinstance(entries, list):
                return data

        if data.get("error"):
            message = str(data["error"]).lower()
            kind = "quota" if any(word in message for word in ("run out of searches", "quota", "credits exhausted")) else "authentication" if any(word in message for word in ("invalid api key", "invalid api_key", "unauthorized")) else "service"
            raise RequestFailure(ProviderError(kind=kind, message=f"SerpAPI: {kind} error."))
        message = str(data.get("error") or f"Unexpected response (HTTP {response.status_code}, status {status!r}).")
        for secret in (self.api_key, quote(self.api_key, safe=""), quote_plus(self.api_key)):
            message = message.replace(secret, "[REDACTED]")
        raise RuntimeError(f"SerpAPI: {message}")

    @staticmethod
    def _parse_paper(item: dict) -> Paper:
        if not isinstance(item, dict) or not isinstance(item.get("title"), str) or not item["title"].strip():
            raise RuntimeError("SerpAPI returned a result without a title.")
        title = item["title"].strip()
        url = item.get("link") or ""
        info = item.get("publication_info") or {}
        summary = info.get("summary") or ""
        authors = [author["name"] for author in info.get("authors", []) or []
                   if isinstance(author, dict) and author.get("name")]
        if not authors and " - " in summary:
            authors = [name.strip() for name in summary.split(" - ", 1)[0].split(",") if name.strip()]
        year = re.search(r"\b(?:18|19|20)\d{2}\b", summary)
        resources = item.get("resources") or []
        pdf_url = next((resource.get("link", "") for resource in resources
                        if isinstance(resource, dict) and resource.get("file_format", "").upper() == "PDF"), "")
        if not pdf_url and urlsplit(url).path.lower().endswith(".pdf"):
            pdf_url = url
        cited_by = (item.get("inline_links") or {}).get("cited_by") or {}
        try:
            citations = max(0, int(cited_by.get("total", 0)))
        except (TypeError, ValueError):
            citations = 0
        snippet = item.get("snippet") or ""
        identifier = item.get("result_id") or sha256(f"{url}\n{title}\n{summary}".encode()).hexdigest()[:24]
        return Paper(
            paper_id=f"gs_{identifier}", title=title, authors=authors, abstract=snippet,
            doi=next((doi for text in (url, pdf_url) if (doi := extract_doi(text))), ""),
            published_date=datetime(int(year[0]), 1, 1) if year else None,
            url=url, pdf_url=pdf_url, source="google_scholar", citations=citations,
            extra={"abstract_source": "snippet", "publication_info": summary},
        )

    def search_page(self, query: SavedQuery, continuation=None, *, allowance=None) -> ProviderPage:
        if query.source != "google_scholar" or query.mode not in ("native_query", "relevance") or query.filters or query.sort:
            raise ValueError("Unsupported Scholar query options.")
        if not query.query.strip():
            raise ValueError("Google Scholar query must not be empty.")
        allowance = allowance if allowance is not None else RequestAllowance()
        before = allowance.used
        state = continuation or {"start": 0}
        start = state.get("start")
        if set(state) != {"start"} or type(start) is not int or start < 0:
            raise ValueError("Invalid Scholar continuation.")
        page = ProviderPage(continuation=state, warnings=["Scholar retrieval ceiling is approximately 1000 results; totals are estimates."])
        if start >= 1000:
            page.state = "provider_cap"
            page.error = ProviderError(kind="provider_cap", message="Scholar retrieval ceiling reached.")
            return page
        try:
            with httpx.Client() as client:
                data = self._page(client, query.query, start, min(20, query.page_size), allowance)
            for item in data["organic_results"]:
                clean = sanitize(item, (self.api_key,))
                try:
                    record = Metadata.from_paper(self._parse_paper(clean))
                    identifiers(record)
                    validate_date(record.published_date, record.date_precision)
                    page.records.append(record)
                except (ValueError, TypeError, KeyError, AttributeError, RuntimeError):
                    page.rejected.append(RejectedRecord(raw=clean, reason="Invalid Scholar record (title or metadata)."))
            total = (data.get("search_information") or {}).get("total_results")
            if total is not None:
                page.total = ReportedTotal(value=int(total), precision="estimated")
            pagination = data.get("serpapi_pagination") or {}
            next_url = pagination.get("next") or pagination.get("next_link")
            if next_url:
                try:
                    offset = int(parse_qs(urlsplit(next_url).query)["start"][0])
                except (KeyError, ValueError, TypeError):
                    raise RuntimeError("SerpAPI returned invalid pagination.") from None
                if offset <= start:
                    page.state = "failed"
                    page.error = ProviderError(kind="nonadvancing", message="SerpAPI pagination did not advance.")
                else:
                    page.continuation = {"start": offset}
            else:
                page.state, page.continuation = "exhausted", None
            if start + len(data["organic_results"]) >= 1000 or page.continuation and page.continuation["start"] >= 1000:
                page.state = "provider_cap"
                page.error = ProviderError(kind="provider_cap", message="Scholar retrieval ceiling reached.")
        except RequestFailure as exc:
            page.error = exc.error
            page.state = "budget" if exc.error.kind == "budget" else "waiting" if exc.error.retry_at else "failed"
        except (ValueError, TypeError, AttributeError, RuntimeError) as exc:
            page.state = "failed"
            page.error = ProviderError(kind="malformed_response", message=sanitize(str(exc), (self.api_key,)))
        page.requests_used = allowance.used - before
        return page

    def search(self, query: str, max_results: int = 10) -> list[Paper]:
        if max_results <= 0:
            return []
        papers = {}
        continuation = None
        while len(papers) < max_results:
            page = self.search_page(SavedQuery(source="google_scholar", query=query,
                page_size=min(20, max_results - len(papers))), continuation)
            for record in page.records:
                papers.setdefault(record.paper_id, record.to_paper())
            if page.rejected:
                raise RuntimeError(page.rejected[0].reason)
            if page.error:
                raise RequestFailure(page.error)
            if page.state == "exhausted":
                break
            continuation = page.continuation
        return list(papers.values())[:max_results]

    def download_pdf(self, paper_id: str, save_path: str) -> str:
        raise NotImplementedError(
            "Google Scholar doesn't provide direct PDF downloads. "
            "Please use the paper's PDF URL or publisher's website."
        )

    def read_paper(self, paper_id: str, save_path: str = "./downloads") -> str:
        return (
            "Google Scholar doesn't support direct paper reading. "
            "Please use the paper URL to access the full text on the publisher's website."
        )
