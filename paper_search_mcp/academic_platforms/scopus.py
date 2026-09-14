"""Scopus discovery and entitled ScienceDirect retrieval via Elsevier's APIs.

Adapted from mildwall's openags/paper-search-mcp PR #89, commit
81e46d7e45c0abaa4a40f515c581baf66ceaac24 (MIT).
"""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
import re
import time
from urllib.parse import unquote
from xml.etree import ElementTree as ET

import httpx

from ..config import get_env
from ..paper import Paper
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

    def _error(self, response: httpx.Response) -> ScopusAPIError:
        message = response.reason_phrase
        try:
            data = response.json()
            message = (data.get("service-error", {}).get("status", {}).get("statusText")
                       or data.get("error-response", {}).get("error-message") or message)
        except (ValueError, AttributeError):
            try:
                message = ET.fromstring(response.content).findtext(".//{*}statusText") or message
            except ET.ParseError:
                pass
        message = str(message)
        if response.status_code == 401:
            message += ". Check your Scopus API key."
        elif response.status_code == 403:
            message += ". Check institutional network/VPN or SCOPUS_INST_TOKEN entitlement."
        elif response.status_code == 429:
            message += ". Rate limit or quota exceeded."
            if reset := response.headers.get("X-RateLimit-Reset"):
                message += f" Quota reset: {reset}."
        if retry_after := response.headers.get("Retry-After"):
            message += f" Retry-After: {retry_after}."
        for secret in (self.api_key, self.inst_token):
            if secret:
                message = message.replace(secret, "[REDACTED]")
        return ScopusAPIError(response.status_code, message)

    @staticmethod
    def _retry_delay(value: str, default: float) -> float:
        try:
            return max(0, float(value))
        except ValueError:
            try:
                date = parsedate_to_datetime(value)
                return max(0, (date - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return default

    def _request(self, client: httpx.Client, url: str, *, params: dict | None = None,
                 accept: str = "application/json") -> httpx.Response:
        for attempt in range(3):
            delay = 2 ** (attempt + 1)
            try:
                response = client.get(url, params=params, headers={"Accept": accept})
            except httpx.RequestError as exc:
                if attempt == 2:
                    raise RuntimeError(f"Elsevier request failed ({type(exc).__name__}).") from None
            else:
                if response.status_code < 400:
                    return response
                error = self._error(response)
                exhausted = (response.headers.get("X-RateLimit-Remaining") == "0"
                             or "QUOTA_EXCEEDED" in response.headers.get("X-ELS-Status", "").upper())
                if response.status_code not in {429, 500, 502, 503, 504} or exhausted or attempt == 2:
                    raise error
                delay = self._retry_delay(response.headers.get("Retry-After", ""), delay)
                if delay > 60:
                    raise error  # Do not retry earlier than a long server-requested delay.
            time.sleep(delay)
        raise RuntimeError("Elsevier request attempts exhausted.")

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
            published_date = datetime.fromisoformat(f"{date}-01-01" if len(date) == 4 else date)
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
            categories=[subject["@abbrev"] for subject in _items(item.get("subject-area"))
                        if isinstance(subject, dict) and subject.get("@abbrev")],
        )

    def search(self, query: str, max_results: int = 10, sort: str = "relevance",
               field: str | None = None, date: str | None = None) -> list[Paper]:
        if max_results <= 0:
            return []
        if not query.strip():
            raise ValueError("Scopus query must not be empty.")
        params = {"query": f"{field}({query})" if field else query, "view": "COMPLETE",
                  "sort": sort.replace("relevance", "relevancy")}
        if date:
            params["date"] = self._normalize_date_range(date)
        start = 0
        papers: dict[str, Paper] = {}
        with self._client() as client:
            while len(papers) < max_results:
                data = self._json(self._request(client, f"{self.BASE_URL}/search/scopus", params={
                    **params, "start": start, "count": min(25, max_results - len(papers)),
                }), "search-results")
                if "entry" not in data and str(data.get("opensearch:totalResults")) != "0":
                    raise RuntimeError("Scopus search response is missing result entries.")
                entries = _items(data.get("entry"))
                previous_count = len(papers)
                for entry in entries:
                    if isinstance(entry, dict) and entry.get("error") == "Result set was empty":
                        continue
                    paper = self._parse_paper(entry)
                    papers.setdefault(paper.paper_id, paper)
                    if len(papers) == max_results:
                        break
                start += len(entries)
                if len(papers) == previous_count or start >= int(data.get("opensearch:totalResults", start)):
                    break
        return list(papers.values())

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
