from typing import List, Optional
from datetime import datetime
import os
import requests
import random
from ..paper import Paper
from ..utils import extract_doi
from .base import PaperSource
import logging
from pypdf import PdfReader
import re
import httpx
from ..config import get_env
from ..discovery import collect, fetch_page, semantic_mode
from ..http import ProviderHTTP, RequestAllowance
from ..provider_models import Author, Metadata, ProviderError, ReportedTotal, SavedQuery

logger = logging.getLogger(__name__)


class SemanticSearcher(PaperSource):
    """Semantic Scholar paper search implementation"""

    SEMANTIC_SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
    SEMANTIC_BASE_URL = "https://api.semanticscholar.org/graph/v1"
    BROWSERS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    ]

    SEARCH_FIELDS = "title,abstract,year,citationCount,authors,url,publicationDate,externalIds,fieldsOfStudy,openAccessPdf,venue"

    def __init__(self, api_key=None):
        self._setup_session()
        key = (get_env("SEMANTIC_SCHOLAR_API_KEY") if api_key is None else api_key).strip()
        self.http = ProviderHTTP("Semantic Scholar", self.SEMANTIC_BASE_URL,
                                 headers={"x-api-key": key} if key else {})

    def _setup_session(self):
        """Initialize session with random user agent"""
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": random.choice(self.BROWSERS),
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )

    def _parse_date(self, date_str: Optional[str]) -> Optional[datetime]:
        """Parse date from Semantic Scholar format (e.g., '2025-06-02')"""
        if not date_str:
            return None

        try:
            return datetime.strptime(date_str.strip(), "%Y-%m-%d")
        except ValueError:
            logger.warning(f"Could not parse date: {date_str}")
            return None

    def _extract_url_from_disclaimer(self, disclaimer: str) -> str:
        """Extract URL from disclaimer text"""
        # 匹配常见的 URL 模式
        url_patterns = [
            r"https?://[^\s,)]+",  # 基本的 HTTP/HTTPS URL
            r"https?://arxiv\.org/abs/[^\s,)]+",  # arXiv 链接
            r"https?://[^\s,)]*\.pdf",  # PDF 文件链接
        ]

        all_urls = []
        for pattern in url_patterns:
            matches = re.findall(pattern, disclaimer)
            all_urls.extend(matches)

        if not all_urls:
            return ""

        doi_urls = [url for url in all_urls if "doi.org" in url]
        if doi_urls:
            return doi_urls[0]

        non_unpaywall_urls = [url for url in all_urls if "unpaywall.org" not in url]
        if non_unpaywall_urls:
            url = non_unpaywall_urls[0]
            if "arxiv.org/abs/" in url:
                pdf_url = url.replace("/abs/", "/pdf/")
                return pdf_url
            return url

        if all_urls:
            url = all_urls[0]
            if "arxiv.org/abs/" in url:
                pdf_url = url.replace("/abs/", "/pdf/")
                return pdf_url
            return url

        return ""

    def _parse_paper(self, item) -> Optional[Paper]:
        """Parse single paper entry from Semantic Scholar HTML and optionally fetch detailed info"""
        try:
            authors = [author["name"] for author in item.get("authors", [])]

            # Parse the publication date
            published_date = self._parse_date(item.get("publicationDate", ""))

            # Safely get PDF URL - 支持从 disclaimer 中提取
            pdf_url = ""
            if item.get("openAccessPdf"):
                open_access_pdf = item["openAccessPdf"]
                # 首先尝试直接获取 URL
                if open_access_pdf.get("url"):
                    pdf_url = open_access_pdf["url"]
                # 如果 URL 为空但有 disclaimer，尝试从 disclaimer 中提取
                elif open_access_pdf.get("disclaimer"):
                    pdf_url = self._extract_url_from_disclaimer(
                        open_access_pdf["disclaimer"]
                    )

            # Safely get DOI
            doi = ""
            if item.get("externalIds") and item["externalIds"].get("DOI"):
                doi = item["externalIds"]["DOI"]

            if not doi and item.get("abstract"):
                doi = extract_doi(item["abstract"])

            # Safely get categories
            categories = item.get("fieldsOfStudy", [])
            if not categories:
                categories = []
            elif not isinstance(categories, list):
                categories = [categories] if categories else []

            return Paper(
                paper_id=item["paperId"],
                title=item["title"],
                authors=authors,
                abstract=item.get("abstract", ""),
                url=item.get("url", ""),
                pdf_url=pdf_url,
                published_date=published_date,
                source="semantic",
                categories=categories,
                doi=doi,
                citations=item.get("citationCount", 0),
            )

        except Exception as e:
            logger.warning(f"Failed to parse Semantic paper: {e}")
            return None

    @staticmethod
    def get_api_key() -> Optional[str]:
        """
        Get the Semantic Scholar API key from environment variables.
        Returns None if no API key is set or if it's empty, enabling unauthenticated access.
        """
        api_key = get_env("SEMANTIC_SCHOLAR_API_KEY", "")
        if not api_key or api_key.strip() == "":
            logger.warning(
                "No SEMANTIC_SCHOLAR_API_KEY set or it's empty. Using unauthenticated access with lower rate limits."
            )
            return None
        return api_key.strip()

    def request_api(self, endpoint, params=None):
        with httpx.Client() as client:
            return self.http.get(client, f"{self.SEMANTIC_BASE_URL}/{endpoint}", RequestAllowance(), params=params)

    def page_request(self, query, continuation):
        from ..registry import SOURCES
        source = SOURCES["semantic"]
        if (query.source != "semantic" or query.mode not in source.query_modes or
                set(query.filters) - {"year"} or query.sort is not None and query.sort not in source.sorts):
            raise ValueError("Unsupported Semantic Scholar query options.")
        if not query.query.strip():
            raise ValueError("Native query must not be blank.")
        relevance = semantic_mode(query) == "relevance"
        params = {"query": query.query, "fields": self.SEARCH_FIELDS, **query.filters}
        state = continuation or {}
        received = state.get("received", 0)
        if type(received) is not int or received < 0 or set(state) - {"cursor", "received"}:
            raise ValueError("Invalid Semantic Scholar continuation.")
        if relevance:
            offset = state.get("cursor", 0)
            if type(offset) is not int or not 0 <= offset < 1000:
                raise ValueError("Invalid relevance offset.")
            params.update(offset=offset, limit=min(query.page_size, 1000 - offset))
        else:
            if state:
                if not isinstance(state.get("cursor"), str) or not state["cursor"]:
                    raise ValueError("Invalid bulk continuation.")
                params["token"] = state["cursor"]
            if query.sort:
                params["sort"] = query.sort
        return self.SEMANTIC_SEARCH_URL + ("" if relevance else "/bulk"), params

    @staticmethod
    def page_entries(data):
        if not isinstance(data["data"], list) or "error" in data:
            raise ValueError("Invalid result envelope")
        return data["data"]

    @staticmethod
    def page_response(data, response, query, continuation, page):
        entries = data["data"]
        if not isinstance(entries, list) or "error" in data:
            raise ValueError("Invalid result envelope")
        relevance = query.mode == "relevance" or query.sort == "relevance"
        page.total = ReportedTotal(value=data.get("total"), precision="estimated" if data.get("total") is not None else "unknown")
        cursor = data.get("next" if relevance else "token")
        invalid_cursor = (type(cursor) is not int or cursor < 0) if relevance else (not isinstance(cursor, str) or not cursor)
        if cursor is not None and invalid_cursor:
            raise ValueError("Invalid continuation")
        received = (continuation or {}).get("received", 0) + len(entries)
        page.continuation = {"cursor": cursor, "received": received} if cursor is not None else None
        page.state = "ready" if page.continuation else "exhausted"
        if relevance:
            offset = (continuation or {}).get("cursor", 0)
            if cursor is not None and cursor <= offset:
                page.state = "failed"
                page.error = ProviderError(kind="nonadvancing", message="Semantic Scholar offset did not advance.")
            if (cursor is not None and cursor >= 1000 or offset + len(entries) >= 1000 and
                    (page.total.value is None or page.total.value > 1000)):
                page.state = "provider_cap"
            page.warnings = ["Relevance discovery is limited to 1,000 results."]
        else:
            page.warnings = ["Bulk search returns up to 1,000 records per request; page_size is not supported upstream. Retrieval ceiling: 10,000,000 records."]
            if cursor is not None and cursor == (continuation or {}).get("cursor"):
                page.state = "failed"
                page.error = ProviderError(kind="nonadvancing", message="Semantic Scholar token did not advance.")
            elif cursor is not None and received >= 10_000_000:
                page.state = "provider_cap"
        return entries

    @staticmethod
    def metadata(item):
        external = item.get("externalIds") or {}
        namespaces = {"DOI": "doi", "ArXiv": "arxiv", "PubMed": "pmid", "PubMedCentral": "pmcid",
                      "CorpusId": "corpusid", "MAG": "mag", "DBLP": "dblp", "ACL": "acl"}
        date = item.get("publicationDate") or str(item.get("year") or "") or None
        return Metadata(paper_id=item["paperId"], source="semantic", title=item["title"],
            authors=[Author(name=a["name"]) for a in item.get("authors") or [] if a.get("name")],
            abstract=item.get("abstract") or "", doi=external.get("DOI") or "",
            identifiers={namespaces.get(key, key.lower()): str(value) for key, value in external.items() if value},
            published_date=date, date_precision={4: "year", 7: "month", 10: "day"}.get(len(date or ""), "unknown"),
            url=item.get("url") or "", pdf_url=(item.get("openAccessPdf") or {}).get("url") or "",
            venue=item.get("venue") or "", citations=item.get("citationCount") or 0,
            categories=item.get("fieldsOfStudy") or [], extra={"external_ids": external})

    def search_page(self, query, continuation=None, *, allowance=None):
        return fetch_page(self, query, continuation, allowance)

    def search(self, query: str, year: Optional[str] = None, max_results: int = 10, fetch_details: bool = False) -> List[Paper]:
        """Bounded relevance discovery; fetch_details remains a compatibility no-op."""
        if max_results <= 0:
            return []
        saved = SavedQuery(source="semantic", query=query, mode="relevance", page_size=min(max_results, 100),
                           filters={"year": year} if year else {})
        return collect(self, saved, max_results)

    def download_pdf(self, paper_id: str, save_path: str) -> str:
        """
        Download PDF from Semantic Scholar

        Args:
            paper_id (str): Paper identifier in one of the following formats:
            - Semantic Scholar ID (e.g., "649def34f8be52c8b66281af98ae884c09aef38b")
            - DOI:<doi> (e.g., "DOI:10.18653/v1/N18-3011")
            - ARXIV:<id> (e.g., "ARXIV:2106.15928")
            - MAG:<id> (e.g., "MAG:112218234")
            - ACL:<id> (e.g., "ACL:W12-3903")
            - PMID:<id> (e.g., "PMID:19872477")
            - PMCID:<id> (e.g., "PMCID:2323736")
            - URL:<url> (e.g., "URL:https://arxiv.org/abs/2106.15928v1")
            save_path: Path to save the PDF

        Returns:
            str: Path to downloaded file or error message
        """
        try:
            paper = self.get_paper_details(paper_id)
            if not paper or not paper.pdf_url:
                return f"Error: Could not find PDF URL for paper {paper_id}"
            pdf_url = paper.pdf_url
            pdf_response = requests.get(pdf_url, timeout=30)
            pdf_response.raise_for_status()

            # Create download directory if it doesn't exist
            os.makedirs(save_path, exist_ok=True)

            filename = f"semantic_{paper_id.replace('/', '_')}.pdf"
            pdf_path = os.path.join(save_path, filename)

            with open(pdf_path, "wb") as f:
                f.write(pdf_response.content)
            return pdf_path
        except Exception as e:
            logger.error(f"PDF download error: {e}")
            return f"Error downloading PDF: {e}"

    def read_paper(self, paper_id: str, save_path: str = "./downloads") -> str:
        """
        Download and extract text from Semantic Scholar paper PDF

        Args:
            paper_id (str): Paper identifier in one of the following formats:
            - Semantic Scholar ID (e.g., "649def34f8be52c8b66281af98ae884c09aef38b")
            - DOI:<doi> (e.g., "DOI:10.18653/v1/N18-3011")
            - ARXIV:<id> (e.g., "ARXIV:2106.15928")
            - MAG:<id> (e.g., "MAG:112218234")
            - ACL:<id> (e.g., "ACL:W12-3903")
            - PMID:<id> (e.g., "PMID:19872477")
            - PMCID:<id> (e.g., "PMCID:2323736")
            - URL:<url> (e.g., "URL:https://arxiv.org/abs/2106.15928v1")
            save_path: Directory to save downloaded PDF

        Returns:
            str: Extracted text from the PDF or error message
        """
        try:
            os.makedirs(save_path, exist_ok=True)
            filename = f"semantic_{paper_id.replace('/', '_')}.pdf"
            pdf_path = os.path.join(save_path, filename)

            if not os.path.exists(pdf_path):
                paper = self.get_paper_details(paper_id)
                if not paper or not paper.pdf_url:
                    return f"Error: Could not find PDF URL for paper {paper_id}"

                pdf_response = requests.get(paper.pdf_url, timeout=30)
                pdf_response.raise_for_status()

                with open(pdf_path, "wb") as f:
                    f.write(pdf_response.content)
            else:
                paper = self.get_paper_details(paper_id)

            # Extract text using PyPDF
            reader = PdfReader(pdf_path)
            text = ""

            for page_num, page in enumerate(reader.pages):
                try:
                    page_text = page.extract_text()
                    if page_text:
                        text += f"\n--- Page {page_num + 1} ---\n"
                        text += page_text + "\n"
                except Exception as e:
                    logger.warning(
                        f"Failed to extract text from page {page_num + 1}: {e}"
                    )
                    continue

            if not text.strip():
                return (
                    f"PDF downloaded to {pdf_path}, but unable to extract readable text"
                )

            # Add paper metadata at the beginning
            metadata = f"Title: {paper.title if paper else paper_id}\n"
            metadata += f"Authors: {', '.join(paper.authors) if paper else ''}\n"
            metadata += f"Published Date: {paper.published_date if paper else ''}\n"
            metadata += f"URL: {paper.url if paper else ''}\n"
            metadata += f"PDF downloaded to: {pdf_path}\n"
            metadata += "=" * 80 + "\n\n"

            return metadata + text.strip()

        except requests.RequestException as e:
            logger.error(f"Error downloading PDF: {e}")
            return f"Error downloading PDF: {e}"
        except Exception as e:
            logger.error(f"Read paper error: {e}")
            return f"Error reading paper: {e}"

    def get_paper_details(self, paper_id: str) -> Optional[Paper]:
        """
        Fetch detailed information for a specific Semantic Scholar paper

        Args:
            paper_id (str): Paper identifier in one of the following formats:
            - Semantic Scholar ID (e.g., "649def34f8be52c8b66281af98ae884c09aef38b")
            - DOI:<doi> (e.g., "DOI:10.18653/v1/N18-3011")
            - ARXIV:<id> (e.g., "ARXIV:2106.15928")
            - MAG:<id> (e.g., "MAG:112218234")
            - ACL:<id> (e.g., "ACL:W12-3903")
            - PMID:<id> (e.g., "PMID:19872477")
            - PMCID:<id> (e.g., "PMCID:2323736")
            - URL:<url> (e.g., "URL:https://arxiv.org/abs/2106.15928v1")

        Returns:
            Paper: Detailed paper object with full metadata
        """
        try:
            fields = [
                "title",
                "abstract",
                "year",
                "citationCount",
                "authors",
                "url",
                "publicationDate",
                "externalIds",
                "fieldsOfStudy",
                "openAccessPdf",
            ]
            params = {
                "fields": ",".join(fields),
            }

            response = self.request_api(f"paper/{paper_id}", params)

            # Check for errors
            if isinstance(response, dict) and "error" in response:
                error_msg = response.get("message", "Unknown error")
                if response.get("error") == "rate_limited":
                    logger.error(f"Rate limited by Semantic Scholar API: {error_msg}")
                else:
                    logger.error(f"Semantic Scholar API error: {error_msg}")
                return None

            # Check response status code
            if not hasattr(response, "status_code") or response.status_code != 200:
                status_code = getattr(response, "status_code", "unknown")
                logger.error(
                    f"Semantic Scholar paper details fetch failed with status {status_code}"
                )
                return None

            results = response.json()
            paper = self.metadata(results).to_paper()
            if paper:
                return paper
            else:
                return None
        except Exception as e:
            logger.error(f"Error fetching paper details for {paper_id}: {e}")
            return None


if __name__ == "__main__":
    # Test Semantic searcher
    searcher = SemanticSearcher()

    print("Testing Semantic search functionality...")
    query = "secret sharing"
    max_results = 2

    print("\n" + "=" * 60)
    print("1. Testing search with detailed information")
    print("=" * 60)
    try:
        papers = searcher.search(query, year=None, max_results=max_results)
        print(f"\nFound {len(papers)} papers for query '{query}' (with details):")
        for i, paper in enumerate(papers, 1):
            print(f"\n{i}. {paper.title}")
            print(f"   Paper ID: {paper.paper_id}")
            print(f"   Authors: {', '.join(paper.authors)}")
            print(f"   Categories: {', '.join(paper.categories)}")
            print(f"   URL: {paper.url}")
            if paper.pdf_url:
                print(f"   PDF: {paper.pdf_url}")
            if paper.published_date:
                print(f"   Published Date: {paper.published_date}")
            if paper.abstract:
                print(f"   Abstract: {paper.abstract[:200]}...")
    except Exception as e:
        print(f"Error during detailed search: {e}")

    print("\n" + "=" * 60)
    print("2. Testing manual paper details fetching")
    print("=" * 60)
    test_paper_id = "5bbfdf2e62f0508c65ba6de9c72fe2066fd98138"
    try:
        paper_details = searcher.get_paper_details(test_paper_id)
        if paper_details:
            print(f"\nManual fetch for paper {test_paper_id}:")
            print(f"Title: {paper_details.title}")
            print(f"Authors: {', '.join(paper_details.authors)}")
            print(f"Categories: {', '.join(paper_details.categories)}")
            print(f"URL: {paper_details.url}")
            if paper_details.pdf_url:
                print(f"PDF: {paper_details.pdf_url}")
            if paper_details.published_date:
                print(f"Published Date: {paper_details.published_date}")
            print(f"DOI: {paper_details.doi}")
            print(f"Citations: {paper_details.citations}")
            print(f"Abstract: {paper_details.abstract[:200]}...")
        else:
            print(f"Could not fetch details for paper {test_paper_id}")
    except Exception as e:
        print(f"Error fetching paper details: {e}")
