"""Google Scholar discovery through SerpAPI; no direct Scholar requests."""

from datetime import datetime
from hashlib import sha256
import logging
import re
from urllib.parse import parse_qs, quote, quote_plus, urlsplit

import httpx

from ..config import get_env
from ..paper import Paper
from ..utils import extract_doi
from .base import PaperSource


def _redact_request_log(record: logging.LogRecord) -> bool:
    # HTTPX logs query strings at INFO, including SerpAPI's required api_key.
    record.msg = re.sub(r"([?&]api_key=)[^&\s]+", r"\1[REDACTED]", record.getMessage())
    record.args = ()
    return True


logging.getLogger("httpx").addFilter(_redact_request_log)


class GoogleScholarSearcher(PaperSource):
    """Search SerpAPI and return the project's standard paper metadata."""

    SEARCH_URL = "https://serpapi.com/search.json"

    def __init__(self, api_key: str | None = None):
        self.api_key = (get_env("SERPAPI_API_KEY") if api_key is None else api_key).strip()
        if not self.api_key:
            raise ValueError("Set PAPER_SEARCH_MCP_SERPAPI_API_KEY (or SERPAPI_API_KEY) to enable Google Scholar.")

    def _page(self, client: httpx.Client, query: str, start: int, count: int) -> dict:
        try:
            response = client.get(self.SEARCH_URL, params={
                "engine": "google_scholar", "api_key": self.api_key,
                "q": query, "hl": "en", "start": start, "num": count,
            })
        except httpx.RequestError as exc:
            raise RuntimeError(f"SerpAPI request failed ({type(exc).__name__}).") from None
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
            doi=next((doi for text in (url, pdf_url, title, summary, snippet) if (doi := extract_doi(text))), ""),
            published_date=datetime(int(year[0]), 1, 1) if year else None,
            url=url, pdf_url=pdf_url, source="google_scholar", citations=citations,
            extra={"abstract_source": "snippet", "publication_info": summary},
        )

    def search(self, query: str, max_results: int = 10) -> list[Paper]:
        if max_results <= 0:
            return []
        if not query.strip():
            raise ValueError("Google Scholar query must not be empty.")
        papers: dict[str, Paper] = {}
        start = 0
        with httpx.Client(timeout=httpx.Timeout(60, connect=10)) as client:
            while len(papers) < max_results:
                data = self._page(client, query, start, min(20, max_results - len(papers)))
                previous_count = len(papers)
                for item in data["organic_results"]:
                    paper = self._parse_paper(item)
                    papers.setdefault(paper.paper_id, paper)
                    if len(papers) == max_results:
                        break
                if len(papers) == max_results:
                    break
                pagination = data.get("serpapi_pagination") or {}
                next_url = pagination.get("next") or pagination.get("next_link")
                if len(papers) == previous_count or not next_url:
                    break
                try:
                    next_start = int(parse_qs(urlsplit(next_url).query)["start"][0])
                except (KeyError, ValueError, TypeError):
                    raise RuntimeError("SerpAPI returned invalid pagination.") from None
                if next_start <= start:
                    break
                # Only consume the offset; never follow an API-supplied URL with our key.
                start = next_start
        return list(papers.values())

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
