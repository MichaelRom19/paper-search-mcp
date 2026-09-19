"""Crossref cursor discovery with deposited reference provenance."""
from urllib.parse import quote

import httpx

from ..discovery import collect, fetch_page, page_fingerprint
from ..http import ProviderHTTP, RequestAllowance, RequestFailure
from ..provider_models import Author, Metadata, ProviderError, ReportedTotal, SavedQuery
from .base import PaperSource


class CrossRefSearcher(PaperSource):
    BASE_URL = "https://api.crossref.org"
    SORTS = ("relevance", "updated", "deposited", "indexed", "created", "is-referenced-by-count", "references", "score")
    UNSUPPORTED_SORTS = {"issued", "published", "published-print", "published-online"}

    def __init__(self):
        self.http = ProviderHTTP("Crossref", self.BASE_URL, headers={
            "User-Agent": "paper-search-mcp (https://github.com/Dragonatorul/paper-search-mcp)",
            "Accept": "application/json"})

    @classmethod
    def validate_query(cls, query):
        if query.sort in cls.UNSUPPORTED_SORTS:
            raise ValueError(f"Crossref cursor pagination does not support sort '{query.sort}'. Choose a supported sort explicitly.")
        if (query.source != "crossref" or query.mode not in {"native_query", "keyword"} or
                set(query.filters) - {"filter", "order"} or query.sort not in (None, *cls.SORTS)):
            raise ValueError("Unsupported Crossref query options.")
        if not query.query.strip():
            raise ValueError("Native query must not be blank.")
        if "filter" in query.filters and (not isinstance(query.filters["filter"], str) or not query.filters["filter"].strip()):
            raise ValueError("Crossref filter must be a nonempty native filter expression.")
        if query.filters.get("order", "desc") not in {"asc", "desc"}:
            raise ValueError("Crossref order must be asc or desc.")

    def page_request(self, query, continuation):
        self.validate_query(query)
        state = continuation or {}
        cursor = state.get("cursor", "*")
        if (not isinstance(cursor, str) or not cursor or set(state) - {"cursor", "page_identity"}):
            raise ValueError("Invalid Crossref continuation.")
        return f"{self.BASE_URL}/works", {"query": query.query, "rows": query.page_size,
            "sort": query.sort or "relevance", "order": "desc", **query.filters, "cursor": cursor}

    @staticmethod
    def page_entries(data):
        message = data["message"]
        entries, total = message["items"], message["total-results"]
        if not isinstance(entries, list) or type(total) is not int or total < 0 or data.get("status", "ok") != "ok":
            raise ValueError("Invalid Crossref envelope")
        return entries

    @staticmethod
    def page_response(data, response, query, continuation, page):
        message = data["message"]
        entries, total = message["items"], message["total-results"]
        cursor = message.get("next-cursor")
        if cursor is not None and (not isinstance(cursor, str) or not cursor):
            raise ValueError("Invalid Crossref cursor")
        if len(entries) >= query.page_size and cursor is None:
            raise ValueError("Missing Crossref cursor")
        identity = page_fingerprint([item.get("DOI", item) if isinstance(item, dict) else item for item in entries])
        page.total = ReportedTotal(value=total, precision="exact")
        page.pagination_validated = True
        page.state = "exhausted" if len(entries) < query.page_size else "ready"
        page.continuation = {"cursor": cursor, "page_identity": identity} if page.state == "ready" else None
        page.warnings = ["Crossref's changing index is not an immutable snapshot; records can move between pages."]
        if entries and identity == (continuation or {}).get("page_identity") or (
                page.state == "ready" and cursor == (continuation or {}).get("cursor", "*")):
            page.state = "failed"
            page.error = ProviderError(kind="nonadvancing", message="Crossref repeated its page or cursor.")
        return entries

    @staticmethod
    def metadata(item):
        date_parts = next((parts[0] for key in ("published", "issued")
            if (parts := (item.get(key) or {}).get("date-parts")) and parts[0]), [])
        date = "-".join(f"{part:04d}" if i == 0 else f"{part:02d}" for i, part in enumerate(date_parts)) or None
        references = item.get("reference") or []
        if not isinstance(references, list):
            raise ValueError("Invalid deposited references")
        title = item.get("title") or []
        venue = item.get("container-title") or []
        doi = item["DOI"]
        primary_url = ((item.get("resource") or {}).get("primary") or {}).get("URL") or ""
        return Metadata(paper_id=doi, source="crossref", title=(title[0] if title else "") if isinstance(title, list) else title,
            doi=doi, identifiers={"doi": doi}, abstract=item.get("abstract") or "",
            authors=[Author(name=a.get("name") or " ".join(filter(None, (a.get("given"), a.get("family")))),
                            given=a.get("given"), family=a.get("family"), orcid=a.get("ORCID"))
                     for a in item.get("author") or []],
            published_date=date, date_precision={1: "year", 2: "month", 3: "day"}.get(len(date_parts), "unknown"),
            venue=venue[0] if isinstance(venue, list) and venue else venue or "",
            publication_type=item.get("type") or "", categories=[item["type"]] if item.get("type") else [],
            url=item.get("URL") or f"https://doi.org/{doi}",
            pdf_url=next((link["URL"] for link in item.get("link") or []
                          if link.get("content-type") == "application/pdf" and link.get("URL")),
                         primary_url if primary_url.endswith(".pdf") else ""),
            citations=item.get("is-referenced-by-count") or 0, keywords=item.get("subject") or [],
            references=[ref["DOI"] for ref in references if isinstance(ref, dict) and isinstance(ref.get("DOI"), str)],
            extra={"deposited_references": references, "publisher": item.get("publisher") or "",
                   "container_title": venue[0] if isinstance(venue, list) and venue else venue or "",
                   "crossref_type": item.get("type") or "", "issn": item.get("ISSN") or [],
                   "isbn": item.get("ISBN") or [], **{key: item[key] for key in
                   ("volume", "issue", "page", "member", "prefix", "relation") if key in item}})

    def search_page(self, query, continuation=None, *, allowance=None):
        return fetch_page(self, query, continuation, allowance)

    def search(self, query: str, max_results: int = 10, **kwargs):
        if max_results <= 0:
            return []
        if set(kwargs) - {"filter", "sort", "order"}:
            raise ValueError("Unsupported Crossref search options.")
        saved = SavedQuery(source="crossref", query=query, page_size=min(max_results, 100),
            sort=kwargs.get("sort"), filters={key: value for key, value in kwargs.items() if key != "sort"})
        return collect(self, saved, max_results)

    def _parse_crossref_item(self, item):
        return self.metadata(item).to_paper()

    def get_paper_by_doi(self, doi):
        try:
            with httpx.Client() as client:
                response = self.http.get(client, f"{self.BASE_URL}/works/{quote(doi, safe='')}", RequestAllowance())
        except RequestFailure as exc:
            if exc.error.status_code == 404:
                return None
            raise
        return self._parse_crossref_item(response.json()["message"])

    def download_pdf(self, paper_id: str, save_path: str) -> str:
        """
        CrossRef doesn't provide direct PDF downloads.
        
        Args:
            paper_id: DOI of the paper
            save_path: Directory to save the PDF
            
        Raises:
            NotImplementedError: Always raises this error as CrossRef doesn't provide direct PDF access
        """
        message = ("CrossRef does not provide direct PDF downloads. "
                  "CrossRef is a citation database that provides metadata about academic papers. "
                  "To access the full text, please use the paper's DOI or URL to visit the publisher's website.")
        raise NotImplementedError(message)
    
    def read_paper(self, paper_id: str, save_path: str = "./downloads") -> str:
        """
        CrossRef doesn't provide direct paper content access.
        
        Args:
            paper_id: DOI of the paper
            save_path: Directory for potential PDF storage (unused)
            
        Returns:
            str: Error message indicating PDF reading is not supported
        """
        message = ("CrossRef papers cannot be read directly through this tool. "
                  "CrossRef is a citation database that provides metadata about academic papers. "
                  "Only metadata and abstracts are available through CrossRef's API. "
                  "To access the full text, please use the paper's DOI or URL to visit the publisher's website.")
        return message
