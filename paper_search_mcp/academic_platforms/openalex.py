"""OpenAlex cursor discovery; no per-result enrichment."""
from ..config import get_env
from ..discovery import collect, fetch_page
from ..http import ProviderHTTP
from ..library import normalize_identifier
from ..provider_models import Author, Metadata, ProviderError, ReportedTotal, SavedQuery
from .base import PaperSource


class OpenAlexSearcher(PaperSource):
    BASE_URL = "https://api.openalex.org/works"

    def __init__(self, api_key=None):
        key = (get_env("OPENALEX_API_KEY") if api_key is None else api_key).strip()
        self.http = ProviderHTTP("OpenAlex", self.BASE_URL,
                                 headers={"Authorization": f"Bearer {key}"} if key else {})

    @staticmethod
    def _reconstruct_abstract(index):
        return " ".join(word for _, word in sorted(
            (position, word) for word, positions in (index or {}).items() for position in positions))

    def page_request(self, query, continuation):
        if query.source != "openalex" or query.mode not in {"native_query", "keyword"} or query.filters or query.sort:
            raise ValueError("Unsupported OpenAlex query options.")
        if not query.query.strip():
            raise ValueError("Native query must not be blank.")
        cursor = (continuation or {}).get("cursor", "*")
        if not isinstance(cursor, str) or not cursor or continuation is not None and set(continuation) != {"cursor"}:
            raise ValueError("Invalid OpenAlex continuation.")
        return self.BASE_URL, {"search": query.query, "per_page": query.page_size, "cursor": cursor}

    @staticmethod
    def page_entries(data):
        if not isinstance(data["results"], list) or not isinstance(data["meta"], dict):
            raise ValueError("Invalid envelope")
        return data["results"]

    @staticmethod
    def page_response(data, response, query, continuation, page):
        entries, meta = data["results"], data["meta"]
        if not isinstance(entries, list) or not isinstance(meta, dict):
            raise ValueError("Invalid envelope")
        page.request_usage = {key: value for key, value in meta.items() if key in {"credits_used", "cost_usd"}}
        for key in ("X-RateLimit-Credits-Used", "X-RateLimit-Remaining", "X-RateLimit-Limit", "X-RateLimit-Reset"):
            if key in response.headers:
                page.request_usage[key] = response.headers[key]
        page.total = ReportedTotal(value=meta.get("count"), precision="exact" if meta.get("count") is not None else "unknown")
        cursor = meta["next_cursor"]
        if cursor is not None and (not isinstance(cursor, str) or not cursor):
            raise ValueError("Invalid cursor")
        page.continuation = {"cursor": cursor} if cursor else None
        page.state = "ready" if cursor else "exhausted"
        if cursor == (continuation or {}).get("cursor", "*"):
            page.state = "failed"
            page.error = ProviderError(kind="nonadvancing", message="OpenAlex cursor did not advance.")
        return entries

    def metadata(self, item):
        location = item.get("primary_location") or {}
        date = item.get("publication_date") or str(item.get("publication_year") or "") or None
        ids = {key: str(value).rsplit("/", 1)[-1] if key in {"openalex", "pmid", "pmcid"} else str(value)
               for key, value in (item.get("ids") or {}).items() if value}
        paper_id = item["id"].rsplit("/", 1)[-1]
        ids["openalex"] = paper_id
        return Metadata(paper_id=paper_id, source="openalex", title=item["title"],
            authors=[Author(name=a["author"]["display_name"], orcid=a["author"].get("orcid"))
                     for a in item.get("authorships") or [] if (a.get("author") or {}).get("display_name")],
            abstract=self._reconstruct_abstract(item.get("abstract_inverted_index")),
            doi=normalize_identifier("doi", item["doi"]) if item.get("doi") else "", identifiers=ids, published_date=date,
            date_precision={4: "year", 7: "month", 10: "day"}.get(len(date or ""), "unknown"),
            venue=(location.get("source") or {}).get("display_name") or "",
            publication_type=item.get("type") or "", url=location.get("landing_page_url") or item["id"],
            pdf_url=location.get("pdf_url") or (item.get("open_access") or {}).get("oa_url") or "",
            citations=item.get("cited_by_count") or 0, references=item.get("referenced_works") or [],
            extra={"external_ids": ids},
            categories=[c["display_name"] for c in item.get("concepts") or [] if c.get("display_name")][:5])

    def search_page(self, query, continuation=None, *, allowance=None):
        return fetch_page(self, query, continuation, allowance)

    def search(self, query: str, max_results: int = 10):
        if max_results <= 0:
            return []
        return collect(self, SavedQuery(source="openalex", query=query, page_size=min(max_results, 100)), max_results)

    def download_pdf(self, paper_id: str, save_path: str) -> str:
        raise NotImplementedError("OpenAlex does not host PDFs. Use the paper's DOI or pdf_url.")

    def read_paper(self, paper_id: str, save_path: str = "./downloads") -> str:
        return "OpenAlex papers cannot be read directly. Use the paper's DOI or pdf_url."
