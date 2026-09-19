"""Scopus discovery and entitled ScienceDirect retrieval via Elsevier's APIs.

Adapted from mildwall's openags/paper-search-mcp PR #89, commit
81e46d7e45c0abaa4a40f515c581baf66ceaac24 (MIT).
"""

from datetime import datetime
from pathlib import Path
import re
import time
from urllib.parse import unquote
from xml.etree import ElementTree as ET

import httpx

from ..config import get_env
from ..paper import Paper
from ..http import ProviderHTTP, RequestAllowance, RequestFailure, sanitize, retry_delay
from ..library import identifiers, validate_date
from ..provider_models import Metadata, SavedQuery, ProviderPage, ProviderError, RejectedRecord, ReportedTotal
from .base import PaperSource


class ScopusAPIError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"Elsevier API (HTTP {status_code}): {message}")


def _items(value) -> list:
    return value if isinstance(value, list) else [value] if value else []


class ScopusSearcher(PaperSource):
    BASE_URL = "https://api.elsevier.com/content"

    def __init__(self, api_key: str | None = None, inst_token: str | None = None):
        self.api_key = (get_env("SCOPUS_API_KEY") if api_key is None else api_key).strip()
        self.inst_token = (get_env("SCOPUS_INST_TOKEN") if inst_token is None else inst_token).strip()
        if not self.api_key:
            raise ValueError("Set PAPER_SEARCH_MCP_SCOPUS_API_KEY (or SCOPUS_API_KEY) to enable Scopus.")

    def _client(self) -> httpx.Client:
        headers = {"X-ELS-APIKey": self.api_key, "User-Agent": "paper-search-mcp (https://github.com/openags/paper-search-mcp)"}
        if self.inst_token:
            headers["X-ELS-Insttoken"] = self.inst_token
        return httpx.Client(headers=headers, timeout=httpx.Timeout(30, connect=10))

    _retry_delay = staticmethod(retry_delay)

    def _request(self, client: httpx.Client, url: str, *, params: dict | None = None,
                 accept: str = "application/json") -> httpx.Response:
        authenticated = bool(client.headers.get("X-ELS-APIKey"))
        origin = self.BASE_URL if authenticated else url
        headers = {"Accept": accept}
        if authenticated:
            headers["X-ELS-APIKey"] = self.api_key
            if self.inst_token:
                headers["X-ELS-Insttoken"] = self.inst_token
        try:
            return ProviderHTTP("Elsevier", origin, headers=headers).get(
                client, url, RequestAllowance(remaining=3, wait_seconds=60),
                params=params, allow_redirect_response=True)
        except RequestFailure as exc:
            if exc.error.status_code:
                raise ScopusAPIError(exc.error.status_code, exc.error.message) from None
            raise

    @staticmethod
    def _json(response: httpx.Response, envelope: str) -> dict:
        try:
            data = response.json()[envelope]
            if isinstance(data, dict):
                return data
        except (ValueError, KeyError, TypeError):
            pass
        raise RuntimeError(f"Elsevier returned an invalid {envelope} response.")

    @staticmethod
    def _paper_id(paper_id: str) -> str:
        identifier = paper_id.strip().removeprefix("SCOPUS_ID:") if isinstance(paper_id, str) else ""
        if not re.fullmatch(r"[0-9]+", identifier):
            raise ValueError("Expected a numeric Scopus ID, optionally prefixed with SCOPUS_ID:.")
        return identifier

    @staticmethod
    def _normalize_date_range(date: str) -> str:
        date = date.strip()
        if re.fullmatch(r"[0-9]{4}", date) and int(date):
            return date
        if not re.fullmatch(r"([0-9]{4})?-([0-9]{4})?", date) or date == "-":
            raise ValueError("Use a year or year range: 2024, 2020-2024, 2020-, or -2024.")
        lower, upper = date.split("-")
        start, end = int(lower or 1788), int(upper or datetime.now().year + 1)
        if not 0 < start <= end:
            raise ValueError("Scopus date range must have positive, ascending years.")
        return f"{start}-{end}"

    @classmethod
    def _parse_paper(cls, item: dict) -> Paper:
        if not isinstance(item, dict) or not isinstance(item.get("dc:title"), str) or not item["dc:title"].strip():
            raise RuntimeError("Scopus returned a result without a title.")
        identifier = cls._paper_id(item.get("dc:identifier", ""))
        authors = [author if isinstance(author, str) else author.get("authname") or author.get("ce:indexed-name")
                   for author in _items(item.get("author")) if isinstance(author, (dict, str))]
        date = item.get("prism:coverDate") or ""
        try:
            published_date = datetime.fromisoformat(f"{date}-01-01" if len(date) == 4 else f"{date}-01" if len(date) == 7 else date)
        except (TypeError, ValueError):
            published_date = None
        try:
            citations = max(0, int(item.get("citedby-count", 0)))
        except (TypeError, ValueError):
            citations = 0
        doi = item.get("prism:doi") or ""
        url = next((link["@href"] for link in _items(item.get("link"))
                    if isinstance(link, dict) and link.get("@ref") == "scopus" and link.get("@href")), "")
        return Paper(
            paper_id=identifier, title=item["dc:title"].strip(), authors=[author for author in authors if author],
            abstract=item.get("dc:description") or "", doi=doi, published_date=published_date,
            pdf_url="", url=url or (f"https://doi.org/{doi}" if doi else item.get("prism:url") or ""),
            source="scopus", citations=citations,
            extra={"venue": item.get("prism:publicationName") or ""},
            categories=[subject["@abbrev"] for subject in _items(item.get("subject-area"))
                        if isinstance(subject, dict) and subject.get("@abbrev")],
        )

    def search_page(self, query: SavedQuery, continuation=None, *, allowance=None) -> ProviderPage:
        if query.source != "scopus" or query.mode != "native_query" or set(query.filters) - {"date", "field"}:
            raise ValueError("Unsupported Scopus query options.")
        if not query.query.strip():
            raise ValueError("Scopus query must not be empty.")
        state = continuation or {"cursor": "*", "received": 0}
        cursor_mode = "cursor" in state
        start = state.get("received", 0) if cursor_mode else state.get("start")
        if (cursor_mode and (set(state) != {"cursor", "received"} or not isinstance(state["cursor"], str) or not state["cursor"])) or type(start) is not int or start < 0:
            raise ValueError("Invalid Scopus continuation.")
        field = query.filters.get("field")
        params = {"query": f"{field}({query.query})" if field else query.query,
                  "view": "COMPLETE", "sort": (query.sort or "relevance").replace("relevance", "relevancy"),
                  "count": min(25, query.page_size)}
        params.update({"cursor": state["cursor"]} if cursor_mode else {"start": start})
        if date := query.filters.get("date"):
            params["date"] = self._normalize_date_range(date)
        headers = {"X-ELS-APIKey": self.api_key}
        if self.inst_token:
            headers["X-ELS-Insttoken"] = self.inst_token
        http = ProviderHTTP("Elsevier", self.BASE_URL, headers=headers)
        allowance = allowance if allowance is not None else RequestAllowance()
        before = allowance.used
        page = ProviderPage(continuation=state)
        try:
            with httpx.Client() as client:
                data = self._json(http.get(client, f"{self.BASE_URL}/search/scopus", allowance, params=params), "search-results")
            total = int(data["opensearch:totalResults"])
            page.total = ReportedTotal(value=total, precision="exact")
            entries = data.get("entry", [])
            if isinstance(entries, dict):
                entries = [entries]
            if not isinstance(entries, list) or (total and not entries):
                raise ValueError("Invalid result entries")
            for entry in entries:
                if total == 0 and isinstance(entry, dict) and entry.get("error") == "Result set was empty":
                    continue
                clean = sanitize(entry, (self.api_key, self.inst_token))
                try:
                    record = Metadata.from_paper(self._parse_paper(clean))
                    record.venue = record.extra.get("venue", "")
                    date = clean.get("prism:coverDate", "")
                    if date:
                        record.published_date = date
                        record.date_precision = {4: "year", 7: "month"}.get(len(date), "day")
                    identifiers(record)
                    validate_date(record.published_date, record.date_precision)
                    page.records.append(record)
                except (ValueError, TypeError, KeyError, AttributeError, RuntimeError):
                    page.rejected.append(RejectedRecord(raw=clean, reason="Invalid Scopus record (identifier, title or metadata)."))
            more = start + len(entries) < total
            if cursor_mode and more:
                cursor = data.get("cursor", {}).get("@next")
                if not isinstance(cursor, str) or not cursor:
                    raise ValueError("Missing cursor")
                if cursor == state["cursor"]:
                    page.error = ProviderError(kind="nonadvancing", message="Scopus cursor did not advance.")
                    page.state = "failed"
                else:
                    page.continuation = {"cursor": cursor, "received": start + len(entries)}
            else:
                page.continuation = {"start": start + len(entries)} if more else None
            if not page.error:
                page.state = "ready" if page.continuation else "exhausted"
        except RequestFailure as exc:
            page.error = exc.error
            if exc.error.status_code == 403:
                page.error.message += " Check institutional network/VPN or SCOPUS_INST_TOKEN entitlement."
            page.state = "budget" if exc.error.kind == "budget" else "waiting" if exc.error.retry_at else "failed"
        except (ValueError, TypeError, KeyError, AttributeError, RuntimeError):
            page.error = ProviderError(kind="malformed_response", message="Elsevier returned an invalid search-results response.")
            page.state = "failed"
        page.requests_used = allowance.used - before
        return page

    def search(self, query: str, max_results: int = 10, sort: str = "relevance",
               field: str | None = None, date: str | None = None) -> list[Paper]:
        if max_results <= 0:
            return []
        if not query.strip():
            raise ValueError("Scopus query must not be empty.")
        filters = {key: value for key, value in {"field": field, "date": date}.items() if value}
        papers = {}
        continuation = {"start": 0}
        while len(papers) < max_results:
            page = self.search_page(SavedQuery(source="scopus", query=query, sort=sort, filters=filters,
                page_size=min(25, max_results - len(papers))), continuation,
                allowance=RequestAllowance(remaining=3, wait_seconds=60))
            for record in page.records:
                papers.setdefault(record.paper_id, record.to_paper())
            if page.rejected:
                raise RuntimeError(page.rejected[0].reason)
            if page.error:
                if page.error.status_code:
                    raise ScopusAPIError(page.error.status_code, page.error.message)
                raise RequestFailure(page.error)
            if page.state == "exhausted":
                break
            continuation = page.continuation
        return list(papers.values())[:max_results]

    @staticmethod
    def _full_text(response: httpx.Response) -> str:
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError:
            raise RuntimeError("Elsevier returned invalid article XML.") from None
        if root.tag.rsplit("}", 1)[-1] != "full-text-retrieval-response":
            raise RuntimeError("Elsevier returned an unexpected article XML envelope.")
        original = root.find("{*}originalText")
        if original is None:
            return ""
        body = original.find(".//{*}body")
        if body is not None:
            # Keep the article's abstract and references alongside its body.
            article = original.find(".//{*}serial-item")
            if article is not None:
                body = article
        if body is None:
            body = original.find(".//{*}rawtext")
        if body is None:
            return (original.text or "").strip() if not len(original) else ""
        for node in body.iter():
            if node.tag.rsplit("}", 1)[-1] in {"section-title", "para", "simple-para", "entry"}:
                node.tail = "\n\n" + (node.tail or "")
        return "\n".join(" ".join(line.split()) for line in "".join(body.itertext()).splitlines()).strip()

    def read_paper(self, paper_id: str, save_path: str = "./downloads") -> str:
        identifier = self._paper_id(paper_id)
        with self._client() as client:
            details = self._json(self._request(client, f"{self.BASE_URL}/abstract/scopus_id/{identifier}",
                                               params={"view": "FULL"}), "abstracts-retrieval-response")
            core = details.get("coredata")
            if not isinstance(core, dict):
                raise RuntimeError("Scopus abstract response is missing coredata.")
            paper = self._parse_paper({**core, "dc:identifier": f"SCOPUS_ID:{identifier}",
                                       "author": (details.get("authors") or {}).get("author")})
            try:
                response = self._request(client, f"{self.BASE_URL}/article/scopus_id/{identifier}",
                                         params={"view": "FULL"}, accept="text/xml")
                text = self._full_text(response)
                reason = "The article response contains no full-text body."
            except ScopusAPIError as exc:
                if exc.status_code not in {403, 404}:
                    raise
                text, reason = "", str(exc)
        metadata = (f"Title: {paper.title}\nAuthors: {', '.join(paper.authors)}\nDOI: {paper.doi}\n"
                    f"Published: {core.get('prism:coverDate') or ''}\nSource: Scopus + ScienceDirect\n\n")
        return metadata + (f"FULL TEXT\n{text}" if text else
                           f"ABSTRACT ONLY — Full text unavailable: {reason}\n\n{paper.abstract or 'No abstract available.'}")

    def download_pdf(self, paper_id: str, save_path: str = "./downloads") -> str:
        identifier = self._paper_id(paper_id)
        # Use a separate client so custom Elsevier auth headers cannot cross origins.
        with self._client() as authenticated, httpx.Client(timeout=httpx.Timeout(30, connect=10)) as public:
            response = self._request(authenticated, f"{self.BASE_URL}/article/scopus_id/{identifier}",
                                     params={"view": "FULL"}, accept="application/pdf")
            for _ in range(5):
                if response.status_code not in {301, 302, 303, 307, 308}:
                    break
                location = response.headers.get("Location")
                if not location:
                    raise RuntimeError("Elsevier PDF redirect has no Location header.")
                url = response.url.join(location)
                if url.scheme != "https" or url.userinfo:
                    raise RuntimeError("Elsevier PDF redirect must use HTTPS without URL credentials.")
                if any(secret and secret in unquote(str(url)) for secret in (self.api_key, self.inst_token)):
                    raise RuntimeError("Elsevier PDF redirect contains provider credentials.")
                client = authenticated if url.host == "api.elsevier.com" and url.port in (None, 443) else public
                response = self._request(client, str(url), accept="application/pdf")
        if response.status_code != 200 or not response.content.startswith(b"%PDF-"):
            raise RuntimeError("Elsevier did not return a PDF; the article may be unavailable or require entitlement.")
        path = Path(save_path).expanduser() / f"{identifier}.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(response.content)
        return str(path)
