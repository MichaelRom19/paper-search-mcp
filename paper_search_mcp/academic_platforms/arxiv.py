"""arXiv Atom discovery with native offsets and version-aware identities."""
from datetime import datetime
import os
import re
import sys
from xml.etree import ElementTree as ET

from pypdf import PdfReader
import requests

from ..discovery import collect, fetch_page, page_fingerprint
from ..http import ProviderHTTP
from ..provider_models import Author, Metadata, ProviderError, ReportedTotal, SavedQuery
from .base import PaperSource


ATOM = "{http://www.w3.org/2005/Atom}"
OPEN_SEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"
ARXIV = "{http://arxiv.org/schemas/atom}"


class ArxivSearcher(PaperSource):
    BASE_URL = "https://export.arxiv.org/api/query"
    SORTS = ("relevance", "lastUpdatedDate", "submittedDate")
    CEILING = 30_000

    def __init__(self):
        self.http = ProviderHTTP("arxiv", self.BASE_URL, interval=3, headers={
            "User-Agent": "paper-search-mcp (https://github.com/Dragonatorul/paper-search-mcp)",
            "Accept": "application/atom+xml"})

    @classmethod
    def validate_query(cls, query):
        if (query.source != "arxiv" or query.mode not in {"native_query", "keyword"} or
                set(query.filters) - {"sort_order"} or query.sort not in (None, *cls.SORTS)):
            raise ValueError("Unsupported arXiv query options.")
        if not query.query.strip():
            raise ValueError("Native query must not be blank.")
        if query.filters.get("sort_order", "descending") not in ("ascending", "descending"):
            raise ValueError("arXiv sort_order must be ascending or descending.")

    def page_request(self, query, continuation):
        self.validate_query(query)
        state = continuation or {}
        start = state.get("offset", 0)
        if type(start) is not int or not 0 <= start < self.CEILING or set(state) - {"offset", "page_identity"}:
            raise ValueError("Invalid arXiv offset.")
        return self.BASE_URL, {"search_query": f"all:{query.query}" if query.mode == "keyword" else query.query,
            "start": start, "max_results": min(query.page_size, self.CEILING - start),
            "sortBy": query.sort or "relevance", "sortOrder": query.filters.get("sort_order", "descending")}

    @staticmethod
    def decode_response(response):
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise ValueError("Invalid Atom XML") from exc
        if root.tag != ATOM + "feed":
            raise ValueError("Expected Atom feed")
        entries = root.findall(ATOM + "entry")
        if any((entry.findtext(ATOM + "id") or "").startswith("http://arxiv.org/api/errors") or
               (entry.findtext(ATOM + "id") or "").startswith("https://arxiv.org/api/errors") for entry in entries):
            raise ValueError("arXiv returned an API error feed")
        return {"total": int(root.findtext(OPEN_SEARCH + "totalResults")),
                "start": int(root.findtext(OPEN_SEARCH + "startIndex")),
                "entries": [{"xml": ET.tostring(entry, encoding="unicode")} for entry in entries]}

    @classmethod
    def page_entries(cls, data):
        return data["entries"]

    @classmethod
    def page_response(cls, data, response, query, continuation, page):
        entries, start, total = data["entries"], data["start"], data["total"]
        offset = (continuation or {}).get("offset", 0)
        if total < 0 or start < 0:
            raise ValueError("Invalid arXiv pagination metadata")
        identity = page_fingerprint([ET.fromstring(item["xml"]).findtext(ATOM + "id") or item for item in entries])
        end = offset + len(entries)
        page.pagination_validated = True
        page.total = ReportedTotal(value=total, precision="exact")
        page.state = "exhausted" if end >= total else "provider_cap" if end >= cls.CEILING else "ready"
        page.continuation = {"offset": end, "page_identity": identity} if page.state != "exhausted" else None
        page.warnings = ["arXiv offsets are not an immutable snapshot; retrieval is limited to 30,000 results."]
        if start != offset or entries and identity == (continuation or {}).get("page_identity") or not entries and end < total:
            page.state = "failed"
            page.error = ProviderError(kind="nonadvancing", message="arXiv returned a repeated page, incorrect offset, or empty page before its reported total.")
        return entries

    @staticmethod
    def metadata(item):
        entry = ET.fromstring(item["xml"])
        url = entry.findtext(ATOM + "id") or ""
        match = re.fullmatch(r"https?://arxiv\.org/abs/((?:\d{4}\.\d{4,5}|[a-zA-Z.-]+/\d{7}))(v[1-9]\d*)?", url)
        if not match:
            raise ValueError("Invalid arXiv identifier")
        work, version = match.groups()
        paper_id = work + (version or "")
        published = entry.findtext(ATOM + "published")
        updated = entry.findtext(ATOM + "updated")
        date = datetime.fromisoformat(published).date().isoformat() if published else None
        if updated:
            datetime.fromisoformat(updated)
        doi = entry.findtext(ARXIV + "doi") or ""
        return Metadata(paper_id=paper_id, source="arxiv", title=" ".join((entry.findtext(ATOM + "title") or "").split()),
            authors=[Author(name=author.findtext(ATOM + "name") or "") for author in entry.findall(ATOM + "author")],
            abstract=entry.findtext(ATOM + "summary") or "", doi=doi,
            identifiers={"arxiv": work, **({"arxiv_version": paper_id} if version else {})},
            version=version or "", publication_type="preprint", published_date=date,
            date_precision="day" if date else "unknown", updated_date=updated,
            url=url, pdf_url=next((link.get("href") for link in entry.findall(ATOM + "link")
                                  if link.get("type") == "application/pdf" and link.get("href")), ""),
            categories=[tag.get("term") for tag in entry.findall(ATOM + "category") if tag.get("term")],
            venue=entry.findtext(ARXIV + "journal_ref") or "",
            extra={"arxiv_work_id": work, "arxiv_version_id": paper_id if version else None,
                   "comment": entry.findtext(ARXIV + "comment")})

    def search_page(self, query, continuation=None, *, allowance=None):
        return fetch_page(self, query, continuation, allowance)

    def search(self, query: str, max_results: int = 10, sort_by: str = "relevance", sort_order: str = "descending"):
        if max_results <= 0:
            return []
        return collect(self, SavedQuery(source="arxiv", query=query, mode="keyword", page_size=min(max_results, 100),
                       sort=sort_by, filters={"sort_order": sort_order}), max_results)

    def download_pdf(self, paper_id: str, save_path: str) -> str:
        pdf_url = f"https://arxiv.org/pdf/{paper_id}.pdf"
        response = requests.get(pdf_url)
        os.makedirs(save_path, exist_ok=True)
        output_file = f"{save_path}/{paper_id}.pdf"
        with open(output_file, 'wb') as f:
            f.write(response.content)
        return output_file

    def read_paper(self, paper_id: str, save_path: str = "./downloads") -> str:
        """Read a paper and convert it to text format.
        
        Args:
            paper_id: arXiv paper ID
            save_path: Directory where the PDF is/will be saved
            
        Returns:
            str: The extracted text content of the paper
        """
        # First ensure we have the PDF
        pdf_path = f"{save_path}/{paper_id}.pdf"
        if not os.path.exists(pdf_path):
            pdf_path = self.download_pdf(paper_id, save_path)
        
        # Read the PDF
        try:
            reader = PdfReader(pdf_path)
            text = ""
            
            # Extract text from each page
            for page in reader.pages:
                text += page.extract_text() + "\n"
            
            return text.strip()
        except Exception as e:
            print(f"Error reading PDF for paper {paper_id}: {e}", file=sys.stderr)
            return ""
