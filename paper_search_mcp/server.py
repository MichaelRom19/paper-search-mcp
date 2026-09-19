# paper_search_mcp/server.py
from typing import List, Dict, Optional, Any
import asyncio
import os
import logging
import re
import httpx
from mcp.server.mcpserver import MCPServer
from .registry import (
    SOURCES, SourceInfo, available_sources, list_sources as describe_sources,
    provider, unpaywall_fallback,
)
from .academic_platforms.sci_hub import SciHubFetcher  # Legacy import/patch compatibility.
from .utils import extract_doi
from .library_models import ResolutionRequest, PublicationView, ReviewPage, DuplicateSuggestions, ResolutionResult

# from .academic_platforms.hub import SciHubSearcher
from .paper import Paper

# Initialize MCP server
mcp = MCPServer("paper_search_server")
logger = logging.getLogger(__name__)

# Lazy compatibility handles; provider definitions live only in registry.py.
arxiv_searcher = provider("arxiv")
pubmed_searcher = provider("pubmed")
biorxiv_searcher = provider("biorxiv")
medrxiv_searcher = provider("medrxiv")
iacr_searcher = provider("iacr")
semantic_searcher = provider("semantic")
crossref_searcher = provider("crossref")
openalex_searcher = provider("openalex")
pmc_searcher = provider("pmc")
core_searcher = provider("core")
europepmc_searcher = provider("europepmc")
dblp_searcher = provider("dblp")
openaire_searcher = provider("openaire")
citeseerx_searcher = provider("citeseerx")
doaj_searcher = provider("doaj")
base_searcher = provider("base")
zenodo_searcher = provider("zenodo")
hal_searcher = provider("hal")
ssrn_searcher = provider("ssrn")
unpaywall_resolver = unpaywall_fallback()
unpaywall_searcher = provider("unpaywall", resolver=unpaywall_resolver)

# Asynchronous helper to adapt synchronous searchers
# Runs blocking requests-based calls in a thread pool to avoid blocking the event loop.
async def async_search(searcher, query: str, max_results: int, **kwargs) -> List[Dict]:
    if 'year' in kwargs:
        papers = await asyncio.to_thread(searcher.search, query, max_results=max_results, year=kwargs['year'])
    elif kwargs:
        papers = await asyncio.to_thread(searcher.search, query, max_results=max_results, **kwargs)
    else:
        papers = await asyncio.to_thread(searcher.search, query, max_results=max_results)
    return [paper.to_dict() for paper in papers]


ALL_SOURCES = available_sources()

# Preserve conditional legacy tools, including explicit errors for configured stubs.
ieee_searcher = provider("ieee") if SOURCES["ieee"].describe().configured else None
acm_searcher = provider("acm") if SOURCES["acm"].describe().configured else None
google_scholar_searcher = provider("google_scholar") if "google_scholar" in ALL_SOURCES else None
scopus_searcher = provider("scopus") if "scopus" in ALL_SOURCES else None


@mcp.tool()
async def list_sources() -> list[SourceInfo]:
    """List every source's configuration, implementation, capabilities and limitations.

    Access is unverified: configured credentials do not establish entitlement.
    This operation makes no provider requests.
    """
    return describe_sources()



from .review_models import (ReviewProtocol, SearchRequest, ReviewCreated, ReviewUpdated, ReviewView,
                            ReviewList, SearchStarted, RunView)
from .reviews import Reviews


@mcp.tool()
async def create_review(protocol: ReviewProtocol, idempotency_key: str) -> ReviewCreated:
    """Save a review protocol and report unsupported or unavailable sources."""
    return await asyncio.to_thread(lambda: Reviews().create_review(protocol, idempotency_key=idempotency_key))


@mcp.tool()
async def update_review(review_id: str, protocol: ReviewProtocol, idempotency_key: str) -> ReviewUpdated:
    """Save a new protocol revision without changing existing search specifications."""
    return await asyncio.to_thread(lambda: Reviews().update_review(review_id, protocol, idempotency_key=idempotency_key))


@mcp.tool()
async def get_review(review_id: str) -> ReviewView:
    """Read the saved protocol and its revision history."""
    return await asyncio.to_thread(lambda: Reviews().get_review(review_id))


@mcp.tool()
async def list_reviews(limit: int = 20, after: str | None = None) -> ReviewList:
    """List reviews with bounded display pagination."""
    return await asyncio.to_thread(lambda: Reviews().list_reviews(limit=limit, after=after))


@mcp.tool()
async def start_search(request: SearchRequest, idempotency_key: str) -> SearchStarted:
    """Freeze native queries and explicit request budgets; makes no provider requests."""
    return await asyncio.to_thread(lambda: Reviews().start_search(request, idempotency_key=idempotency_key))


@mcp.tool()
async def advance_run(run_id: str, idempotency_key: str, max_requests: int = 4) -> RunView:
    """Advance a saved search by at most four HTTP attempts, including retries."""
    return await asyncio.to_thread(lambda: Reviews().advance_run(run_id, idempotency_key=idempotency_key, max_requests=max_requests))


@mcp.tool()
async def get_run(run_id: str) -> RunView:
    """Read immutable search specifications, budgets, checkpoints and source outcomes."""
    return await asyncio.to_thread(lambda: Reviews().get_run(run_id))


@mcp.tool()
async def get_paper(publication_id: str) -> PublicationView:
    """Read persisted metadata, conflicting alternatives, source observations and query hits."""
    from .library import Library
    return await asyncio.to_thread(lambda: Library().get_paper(publication_id))


@mcp.tool()
async def query_review(review_id: str, limit: int = 20, after: str | None = None) -> ReviewPage:
    """Page through persisted review publications (20 by default; maximum 100)."""
    from .library import Library
    return await asyncio.to_thread(lambda: Library().query_review(review_id, limit=limit, after=after))


@mcp.tool()
async def possible_duplicates(publication_id: str, limit: int = 20) -> DuplicateSuggestions:
    """Suggest similar publications for human review; never merge automatically."""
    from .library import Library
    return await asyncio.to_thread(lambda: Library().possible_duplicates(publication_id, limit=limit))


@mcp.tool()
async def resolve_publications(request: ResolutionRequest, idempotency_key: str) -> ResolutionResult:
    """Apply an explicit manual merge, separation, metadata override, relationship or undo.

    Record the human actor and reason. Use a unique idempotency key per operation.
    """
    from .library import Library
    return await asyncio.to_thread(lambda: Library().resolve_publications(request, idempotency_key=idempotency_key))

def _parse_sources(sources: str) -> List[str]:
    if not sources or sources.strip().lower() == "all":
        return ALL_SOURCES

    normalized = [part.strip().lower() for part in sources.split(",") if part.strip()]
    return [source for source in normalized if source in ALL_SOURCES]


def _paper_unique_key(paper: dict[str, Any]) -> str:
    doi = (paper.get("doi") or "").strip().lower()
    if doi:
        return f"doi:{doi}"

    title = (paper.get("title") or "").strip().lower()
    authors = (paper.get("authors") or "").strip().lower()
    if title:
        return f"title:{title}|authors:{authors}"

    paper_id = (paper.get("paper_id") or "").strip().lower()
    return f"id:{paper_id}"


def _dedupe_papers(papers: List[dict[str, Any]]) -> List[dict[str, Any]]:
    deduped: List[dict[str, Any]] = []
    seen: set[str] = set()

    for paper in papers:
        key = _paper_unique_key(paper)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(paper)

    return deduped


def _safe_filename(filename_hint: str, default: str = "paper") -> str:
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", filename_hint).strip("._")
    if not safe:
        return default
    return safe[:120]


async def _download_from_url(pdf_url: str, save_path: str, filename_hint: str = "paper") -> Optional[str]:
    if not pdf_url:
        return None

    os.makedirs(save_path, exist_ok=True)
    output_name = f"{_safe_filename(filename_hint)}.pdf"
    output_path = os.path.join(save_path, output_name)

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            response = await client.get(pdf_url)

        if response.status_code >= 400 or not response.content:
            return None

        content_type = (response.headers.get("content-type") or "").lower()
        is_pdf = "pdf" in content_type or response.content.startswith(b"%PDF") or pdf_url.lower().endswith(".pdf")
        if not is_pdf:
            logger.warning("Resolved URL is not a PDF candidate: %s (content-type=%s)", pdf_url, content_type)
            return None

        with open(output_path, "wb") as file_obj:
            file_obj.write(response.content)

        return output_path
    except Exception as exc:
        logger.warning("Direct URL download failed for %s: %s", pdf_url, exc)
        return None


async def _try_repository_fallback(doi: str, title: str, save_path: str) -> tuple[Optional[str], str]:
    repository_searchers = [
        ("openaire", openaire_searcher),
        ("core", core_searcher),
        ("europepmc", europepmc_searcher),
        ("pmc", pmc_searcher),
    ]

    query_candidates = [(doi or "").strip(), (title or "").strip()]
    query_candidates = [candidate for candidate in query_candidates if candidate]
    if not query_candidates:
        return None, "no DOI/title provided for repository fallback"

    repository_errors: List[str] = []

    for repo_name, searcher in repository_searchers:
        for query in query_candidates:
            try:
                papers = await asyncio.to_thread(searcher.search, query, max_results=3)
            except Exception as exc:
                repository_errors.append(f"{repo_name}:{exc}")
                continue

            if not papers:
                continue

            for paper in papers:
                pdf_url = str(getattr(paper, "pdf_url", "") or "").strip()
                if not pdf_url:
                    continue

                raw_paper_id = getattr(paper, "paper_id", "")
                paper_id = str(raw_paper_id or query).strip()
                downloaded = await _download_from_url(pdf_url, save_path, f"{repo_name}_{paper_id}")
                if downloaded:
                    return downloaded, ""

    return None, "; ".join(repository_errors)


@mcp.tool()
async def search_papers(
    query: str,
    max_results_per_source: int = 5,
    sources: str = "all",
    year: Optional[str] = None,
) -> dict[str, Any]:
    """Unified top-level search across all configured academic platforms.

    Args:
        query: Search query string.
        max_results_per_source: Max results to fetch from each selected source.
        sources: Comma-separated source names or 'all'.
            Use list_sources for capabilities and configuration requirements.
            Unavailable sources and stubs are excluded from aggregate discovery.
            'all' includes configured Google Scholar and consumes SerpAPI searches.
        year: Optional year filter for Semantic Scholar only.
    Returns:
        Aggregated dictionary with per-source stats, errors, and deduplicated papers.
        total counts retrieved, deduplicated results, not upstream matches.
    """
    selected_sources = _parse_sources(sources)

    if not selected_sources:
        return {
            "query": query,
            "sources_requested": sources,
            "sources_used": [],
            "source_results": {},
            "errors": {"sources": "No valid sources selected."},
            "papers": [],
            "total": 0,
        }

    task_map = {}
    for source in selected_sources:
        extra = {"year": year} if source == "semantic" else {}
        if source == "iacr":
            extra["fetch_details"] = False
        if source == "scopus":
            task_map[source] = async_search(scopus_searcher, query, max_results_per_source)
            continue
        task_map[source] = globals()[f"search_{source}"](
            query, max_results=max_results_per_source, **extra,
        )

    source_names = list(task_map.keys())
    source_outputs = await asyncio.gather(*task_map.values(), return_exceptions=True)

    source_results: Dict[str, int] = {}
    errors: Dict[str, str] = {}
    merged_papers: List[dict[str, Any]] = []

    for source_name, output in zip(source_names, source_outputs):
        if isinstance(output, Exception):
            errors[source_name] = str(output)
            source_results[source_name] = 0
            continue

        source_results[source_name] = len(output)
        for paper in output:
            if not paper.get("source"):
                paper["source"] = source_name
            merged_papers.append(paper)

    deduped_papers = _dedupe_papers(merged_papers)

    return {
        "query": query,
        "sources_requested": sources,
        "sources_used": source_names,
        "source_results": source_results,
        "errors": errors,
        "papers": deduped_papers,
        "total": len(deduped_papers),
        "raw_total": len(merged_papers),
    }


# Tool definitions
@mcp.tool()
async def search_arxiv(query: str, max_results: int = 10, sort_by: str = 'relevance', sort_order: str = 'descending') -> List[Dict]:
    """Search academic papers from arXiv.

    Args:
        query: Search query string (e.g., 'machine learning').
        max_results: Maximum number of papers to return (default: 10).
        sort_by: Sort criterion — 'relevance', 'submittedDate', or 'lastUpdatedDate' (default: 'relevance').
        sort_order: Sort direction — 'descending' or 'ascending' (default: 'descending').
    Returns:
        List of paper metadata in dictionary format.
    """
    papers = await async_search(arxiv_searcher, query, max_results, sort_by=sort_by, sort_order=sort_order)
    return papers if papers else []


@mcp.tool()
async def search_pubmed(query: str, max_results: int = 10, sort: str = 'relevance') -> List[Dict]:
    """Search academic papers from PubMed.

    Args:
        query: Search query string (e.g., 'machine learning').
        max_results: Maximum number of papers to return (default: 10).
        sort: Sort order — 'relevance' or 'pub_date' (default: 'relevance').
    Returns:
        List of paper metadata in dictionary format.
    """
    papers = await async_search(pubmed_searcher, query, max_results, sort=sort)
    return papers if papers else []


@mcp.tool()
async def search_biorxiv(query: str, max_results: int = 10) -> List[Dict]:
    """Search academic papers from bioRxiv.

    Note: bioRxiv API filters by category name within the last 30 days, not full-text
    keyword search. Use a category keyword such as 'bioinformatics', 'neuroscience',
    'cell biology', etc.

    Args:
        query: Category name to filter by (e.g., 'bioinformatics', 'neuroscience').
        max_results: Maximum number of papers to return (default: 10).
    Returns:
        List of paper metadata in dictionary format.
    """
    papers = await async_search(biorxiv_searcher, query, max_results)
    return papers if papers else []


@mcp.tool()
async def search_medrxiv(query: str, max_results: int = 10) -> List[Dict]:
    """Search academic papers from medRxiv.

    Note: medRxiv API filters by category name within the last 30 days, not full-text
    keyword search. Use a category keyword such as 'infectious_diseases',
    'cardiovascular_medicine', 'oncology', etc.

    Args:
        query: Category name to filter by (e.g., 'infectious_diseases', 'oncology').
        max_results: Maximum number of papers to return (default: 10).
    Returns:
        List of paper metadata in dictionary format.
    """
    papers = await async_search(medrxiv_searcher, query, max_results)
    return papers if papers else []


async def search_google_scholar(query: str, max_results: int = 10) -> List[Dict]:
    """Search Google Scholar through SerpAPI. Requires PAPER_SEARCH_MCP_SERPAPI_API_KEY.

    Args:
        query: Search query string (e.g., 'machine learning').
        max_results: Maximum number of papers to return (default: 10).
    Returns:
        Paper metadata with citation counts and PDF links when available.
        The abstract field contains a search snippet, not a full abstract.
    """
    if google_scholar_searcher is None:
        raise ValueError("Set PAPER_SEARCH_MCP_SERPAPI_API_KEY to enable Google Scholar.")
    return await async_search(google_scholar_searcher, query, max_results)


if google_scholar_searcher is not None:
    mcp.tool()(search_google_scholar)


@mcp.tool()
async def search_iacr(
    query: str, max_results: int = 10, fetch_details: bool = True
) -> List[Dict]:
    """Search academic papers from IACR ePrint Archive.

    Args:
        query: Search query string (e.g., 'cryptography', 'secret sharing').
        max_results: Maximum number of papers to return (default: 10).
        fetch_details: Whether to fetch detailed information for each paper (default: True).
    Returns:
        List of paper metadata in dictionary format.
    """
    papers = await asyncio.to_thread(iacr_searcher.search, query, max_results, fetch_details)
    return [paper.to_dict() for paper in papers] if papers else []


@mcp.tool()
async def download_arxiv(paper_id: str, save_path: str = "./downloads") -> str:
    """Download PDF of an arXiv paper.

    Args:
        paper_id: arXiv paper ID (e.g., '2106.12345').
        save_path: Directory to save the PDF (default: './downloads').
    Returns:
        Path to the downloaded PDF file.
    """
    return await asyncio.to_thread(arxiv_searcher.download_pdf, paper_id, save_path)


@mcp.tool()
async def download_pubmed(paper_id: str, save_path: str = "./downloads") -> str:
    """Attempt to download PDF of a PubMed paper.

    Args:
        paper_id: PubMed ID (PMID).
        save_path: Directory to save the PDF (default: './downloads').
    Returns:
        str: Message indicating that direct PDF download is not supported.
    """
    try:
        return pubmed_searcher.download_pdf(paper_id, save_path)
    except NotImplementedError as e:
        return str(e)


@mcp.tool()
async def download_biorxiv(paper_id: str, save_path: str = "./downloads") -> str:
    """Download PDF of a bioRxiv paper.

    Args:
        paper_id: bioRxiv DOI.
        save_path: Directory to save the PDF (default: './downloads').
    Returns:
        Path to the downloaded PDF file.
    """
    return biorxiv_searcher.download_pdf(paper_id, save_path)


@mcp.tool()
async def download_medrxiv(paper_id: str, save_path: str = "./downloads") -> str:
    """Download PDF of a medRxiv paper.

    Args:
        paper_id: medRxiv DOI.
        save_path: Directory to save the PDF (default: './downloads').
    Returns:
        Path to the downloaded PDF file.
    """
    return medrxiv_searcher.download_pdf(paper_id, save_path)


@mcp.tool()
async def download_iacr(paper_id: str, save_path: str = "./downloads") -> str:
    """Download PDF of an IACR ePrint paper.

    Args:
        paper_id: IACR paper ID (e.g., '2009/101').
        save_path: Directory to save the PDF (default: './downloads').
    Returns:
        Path to the downloaded PDF file.
    """
    return iacr_searcher.download_pdf(paper_id, save_path)


@mcp.tool()
async def read_arxiv_paper(paper_id: str, save_path: str = "./downloads") -> str:
    """Read and extract text content from an arXiv paper PDF.

    Args:
        paper_id: arXiv paper ID (e.g., '2106.12345').
        save_path: Directory where the PDF is/will be saved (default: './downloads').
    Returns:
        str: The extracted text content of the paper.
    """
    try:
        return arxiv_searcher.read_paper(paper_id, save_path)
    except Exception as e:
        logger.error("Error reading paper %s (%s)", paper_id, type(e).__name__)
        return ""


@mcp.tool()
async def read_pubmed_paper(paper_id: str, save_path: str = "./downloads") -> str:
    """Read and extract text content from a PubMed paper.

    Args:
        paper_id: PubMed ID (PMID).
        save_path: Directory where the PDF would be saved (unused).
    Returns:
        str: Message indicating that direct paper reading is not supported.
    """
    return pubmed_searcher.read_paper(paper_id, save_path)


@mcp.tool()
async def read_biorxiv_paper(paper_id: str, save_path: str = "./downloads") -> str:
    """Read and extract text content from a bioRxiv paper PDF.

    Args:
        paper_id: bioRxiv DOI.
        save_path: Directory where the PDF is/will be saved (default: './downloads').
    Returns:
        str: The extracted text content of the paper.
    """
    try:
        return biorxiv_searcher.read_paper(paper_id, save_path)
    except Exception as e:
        logger.error("Error reading paper %s (%s)", paper_id, type(e).__name__)
        return ""


@mcp.tool()
async def read_medrxiv_paper(paper_id: str, save_path: str = "./downloads") -> str:
    """Read and extract text content from a medRxiv paper PDF.

    Args:
        paper_id: medRxiv DOI.
        save_path: Directory where the PDF is/will be saved (default: './downloads').
    Returns:
        str: The extracted text content of the paper.
    """
    try:
        return medrxiv_searcher.read_paper(paper_id, save_path)
    except Exception as e:
        logger.error("Error reading paper %s (%s)", paper_id, type(e).__name__)
        return ""


@mcp.tool()
async def read_iacr_paper(paper_id: str, save_path: str = "./downloads") -> str:
    """Read and extract text content from an IACR ePrint paper PDF.

    Args:
        paper_id: IACR paper ID (e.g., '2009/101').
        save_path: Directory where the PDF is/will be saved (default: './downloads').
    Returns:
        str: The extracted text content of the paper.
    """
    try:
        return iacr_searcher.read_paper(paper_id, save_path)
    except Exception as e:
        logger.error("Error reading paper %s (%s)", paper_id, type(e).__name__)
        return ""


@mcp.tool()
async def search_semantic(query: str, year: Optional[str] = None, max_results: int = 10) -> List[Dict]:
    """Search academic papers from Semantic Scholar.

    Args:
        query: Search query string (e.g., 'machine learning').
        year: Optional year filter (e.g., '2019', '2016-2020', '2010-', '-2015').
        max_results: Maximum number of papers to return (default: 10).
    Returns:
        List of paper metadata in dictionary format.
    """
    kwargs = {}
    if year is not None:
        kwargs['year'] = year
    papers = await async_search(semantic_searcher, query, max_results, **kwargs)
    return papers if papers else []


@mcp.tool()
async def download_semantic(paper_id: str, save_path: str = "./downloads") -> str:
    """Download PDF of a Semantic Scholar paper.    

    Args:
        paper_id: Semantic Scholar paper ID, Paper identifier in one of the following formats:
            - Semantic Scholar ID (e.g., "649def34f8be52c8b66281af98ae884c09aef38b")
            - DOI:<doi> (e.g., "DOI:10.18653/v1/N18-3011")
            - ARXIV:<id> (e.g., "ARXIV:2106.15928")
            - MAG:<id> (e.g., "MAG:112218234")
            - ACL:<id> (e.g., "ACL:W12-3903")
            - PMID:<id> (e.g., "PMID:19872477")
            - PMCID:<id> (e.g., "PMCID:2323736")
            - URL:<url> (e.g., "URL:https://arxiv.org/abs/2106.15928v1")
        save_path: Directory to save the PDF (default: './downloads').
    Returns:
        Path to the downloaded PDF file.
    """ 
    return semantic_searcher.download_pdf(paper_id, save_path)


@mcp.tool()
async def read_semantic_paper(paper_id: str, save_path: str = "./downloads") -> str:
    """Read and extract text content from a Semantic Scholar paper. 

    Args:
        paper_id: Semantic Scholar paper ID, Paper identifier in one of the following formats:
            - Semantic Scholar ID (e.g., "649def34f8be52c8b66281af98ae884c09aef38b")
            - DOI:<doi> (e.g., "DOI:10.18653/v1/N18-3011")
            - ARXIV:<id> (e.g., "ARXIV:2106.15928")
            - MAG:<id> (e.g., "MAG:112218234")
            - ACL:<id> (e.g., "ACL:W12-3903")
            - PMID:<id> (e.g., "PMID:19872477")
            - PMCID:<id> (e.g., "PMCID:2323736")
            - URL:<url> (e.g., "URL:https://arxiv.org/abs/2106.15928v1")
        save_path: Directory where the PDF is/will be saved (default: './downloads').
    Returns:
        str: The extracted text content of the paper.
    """
    try:
        return semantic_searcher.read_paper(paper_id, save_path)
    except Exception as e:
        logger.error("Error reading paper %s (%s)", paper_id, type(e).__name__)
        return ""


@mcp.tool()
async def search_crossref(
    query: str,
    max_results: int = 10,
    filter: Optional[str] = None,
    sort: Optional[str] = None,
    order: Optional[str] = None,
) -> List[Dict]:
    """Search academic papers from CrossRef database.
    
    CrossRef is a scholarly infrastructure organization that provides 
    persistent identifiers (DOIs) for scholarly content and metadata.
    It's one of the largest citation databases covering millions of 
    academic papers, journals, books, and other scholarly content.

    Args:
        query: Search query string (e.g., 'machine learning', 'climate change').
        max_results: Maximum number of papers to return (default: 10).
        filter: CrossRef filter string (e.g., 'has-full-text:true,from-pub-date:2020').
        sort: Cursor-compatible sort, such as 'relevance', 'created', 'updated' or 'deposited'. Publication-date sorts are unsupported.
        order: Sort order ('asc' or 'desc').
    Returns:
        List of paper metadata in dictionary format.
    """
    extra = {k: v for k, v in {'filter': filter, 'sort': sort, 'order': order}.items() if v is not None}
    papers = await async_search(crossref_searcher, query, max_results, **extra)
    return papers if papers else []


@mcp.tool()
async def get_crossref_paper_by_doi(doi: str) -> Dict:
    """Get a specific paper from CrossRef by its DOI.

    Args:
        doi: Digital Object Identifier (e.g., '10.1038/nature12373').
    Returns:
        Paper metadata in dictionary format, or empty dict if not found.
        
    Example:
        get_crossref_paper_by_doi("10.1038/nature12373")
    """
    paper = await asyncio.to_thread(crossref_searcher.get_paper_by_doi, doi)
    return paper.to_dict() if paper else {}


@mcp.tool()
async def download_crossref(paper_id: str, save_path: str = "./downloads") -> str:
    """Attempt to download PDF of a CrossRef paper.

    Args:
        paper_id: CrossRef DOI (e.g., '10.1038/nature12373').
        save_path: Directory to save the PDF (default: './downloads').
    Returns:
        str: Message indicating that direct PDF download is not supported.
        
    Note:
        CrossRef is a citation database and doesn't provide direct PDF downloads.
        Use the DOI to access the paper through the publisher's website.
    """
    try:
        return crossref_searcher.download_pdf(paper_id, save_path)
    except NotImplementedError as e:
        return str(e)


@mcp.tool()
async def download_scihub(
    identifier: str,
    save_path: str = "./downloads",
    base_url: str = "https://sci-hub.se",
) -> str:
    """Download paper PDF via Sci-Hub (optional fallback connector).

    Args:
        identifier: DOI, title, PMID, or paper URL.
        save_path: Directory to save the PDF.
        base_url: Sci-Hub mirror URL.
    Returns:
        Downloaded PDF path on success; error message on failure.
    """
    fetcher = SOURCES["scihub"].create(base_url=base_url, output_dir=save_path)
    result = await asyncio.to_thread(fetcher.download_pdf, identifier)
    if result:
        return result
    return "Sci-Hub download failed. Try DOI first, then title, or change mirror URL."


@mcp.tool()
async def download_with_fallback(
    source: str,
    paper_id: str,
    doi: str = "",
    title: str = "",
    save_path: str = "./downloads",
    use_scihub: bool = True,
    scihub_base_url: str = "https://sci-hub.se",
) -> str:
    """Try source-native download, OA repositories, Unpaywall, then optional Sci-Hub.

    Args:
        source: Source name (arxiv, biorxiv, medrxiv, iacr, semantic, crossref, pubmed, pmc, core, europepmc, citeseerx, doaj, base, zenodo, hal, ssrn, scopus).
        paper_id: Source-native paper identifier.
        doi: Optional DOI used for repository/unpaywall/Sci-Hub fallback.
        title: Optional title used for repository/Sci-Hub fallback when DOI is unavailable.
        save_path: Directory to save downloaded files.
        use_scihub: Whether to fallback to Sci-Hub after OA attempts fail.
        scihub_base_url: Sci-Hub mirror URL for fallback.
    Returns:
        Download path on success or explanatory error message.
    """
    source_name = source.strip().lower()

    primary_downloaders = {
        name: globals().get(f"{name}_searcher")
        for name in SOURCES if name in ALL_SOURCES
    }

    attempt_errors: List[str] = []
    primary_error = ""
    if source_name in primary_downloaders:
        try:
            primary_result = await asyncio.to_thread(primary_downloaders[source_name].download_pdf, paper_id, save_path)
            if isinstance(primary_result, str) and os.path.exists(primary_result):
                return primary_result
            if isinstance(primary_result, str) and primary_result:
                primary_error = primary_result
        except Exception as exc:
            primary_error = str(exc)
            logger.warning("Primary download failed for %s/%s: %s", source_name, paper_id, exc)
    else:
        primary_error = f"Unsupported source '{source_name}' for primary download."

    if primary_error:
        attempt_errors.append(f"primary: {primary_error}")

    repository_result, repository_error = await _try_repository_fallback(doi, title, save_path)
    if repository_result:
        return repository_result
    if repository_error:
        attempt_errors.append(f"repositories: {repository_error}")

    normalized_doi = (doi or "").strip()
    if normalized_doi:
        unpaywall_url = await asyncio.to_thread(unpaywall_resolver.resolve_best_pdf_url, normalized_doi)
        if unpaywall_url:
            unpaywall_result = await _download_from_url(unpaywall_url, save_path, f"unpaywall_{normalized_doi}")
            if unpaywall_result:
                return unpaywall_result
            attempt_errors.append("unpaywall: resolved OA URL but download failed")
        else:
            attempt_errors.append("unpaywall: no OA URL found (or PAPER_SEARCH_MCP_UNPAYWALL_EMAIL/UNPAYWALL_EMAIL missing)")
    else:
        attempt_errors.append("unpaywall: DOI not provided")

    if not use_scihub:
        return "Download failed after OA fallback chain. Details: " + " | ".join(attempt_errors)

    fallback_identifier = (doi or "").strip() or (title or "").strip() or paper_id
    fetcher = SOURCES["scihub"].create(base_url=scihub_base_url, output_dir=save_path)
    fallback_result = await asyncio.to_thread(fetcher.download_pdf, fallback_identifier)
    if fallback_result:
        return fallback_result

    return "Download failed after OA fallback chain and Sci-Hub fallback. Details: " + " | ".join(attempt_errors)


@mcp.tool()
async def read_crossref_paper(paper_id: str, save_path: str = "./downloads") -> str:
    """Attempt to read and extract text content from a CrossRef paper.

    Args:
        paper_id: CrossRef DOI (e.g., '10.1038/nature12373').
        save_path: Directory where the PDF is/will be saved (default: './downloads').
    Returns:
        str: Message indicating that direct paper reading is not supported.
        
    Note:
        CrossRef is a citation database and doesn't provide direct paper content.
        Use the DOI to access the paper through the publisher's website.
    """
    return crossref_searcher.read_paper(paper_id, save_path)


@mcp.tool()
async def search_openalex(query: str, max_results: int = 10) -> List[Dict]:
    """Search academic papers from OpenAlex.

    Args:
        query: Search query string (e.g., 'machine learning').
        max_results: Maximum number of papers to return (default: 10).
    Returns:
        List of paper metadata in dictionary format.
    """
    papers = await async_search(openalex_searcher, query, max_results)
    return papers if papers else []


@mcp.tool()
async def search_pmc(query: str, max_results: int = 10) -> List[Dict]:
    """Search academic papers from PubMed Central (PMC).

    Args:
        query: Search query string (e.g., 'machine learning').
        max_results: Maximum number of papers to return (default: 10).
    Returns:
        List of paper metadata in dictionary format.
    """
    papers = await async_search(pmc_searcher, query, max_results)
    return papers if papers else []


@mcp.tool()
async def search_core(query: str, max_results: int = 10) -> List[Dict]:
    """Search academic papers from CORE.

    Args:
        query: Search query string (e.g., 'machine learning').
        max_results: Maximum number of papers to return (default: 10).
    Returns:
        List of paper metadata in dictionary format.
    """
    papers = await async_search(core_searcher, query, max_results)
    return papers if papers else []


@mcp.tool()
async def search_europepmc(query: str, max_results: int = 10) -> List[Dict]:
    """Search academic papers from Europe PMC.

    Args:
        query: Search query string (e.g., 'machine learning').
        max_results: Maximum number of papers to return (default: 10).
    Returns:
        List of paper metadata in dictionary format.
    """
    papers = await async_search(europepmc_searcher, query, max_results)
    return papers if papers else []


@mcp.tool()
async def search_dblp(query: str, max_results: int = 10) -> List[Dict]:
    """Search academic papers from dblp computer science bibliography.

    Args:
        query: Search query string (e.g., 'machine learning').
        max_results: Maximum number of papers to return (default: 10).
    Returns:
        List of paper metadata in dictionary format.
    """
    papers = await async_search(dblp_searcher, query, max_results)
    return papers if papers else []


@mcp.tool()
async def search_openaire(query: str, max_results: int = 10) -> List[Dict]:
    """Search academic papers from OpenAIRE European Open Access infrastructure.

    Args:
        query: Search query string (e.g., 'machine learning').
        max_results: Maximum number of papers to return (default: 10).
    Returns:
        List of paper metadata in dictionary format.
    """
    papers = await async_search(openaire_searcher, query, max_results)
    return papers if papers else []


@mcp.tool()
async def search_citeseerx(query: str, max_results: int = 10) -> List[Dict]:
    """Search academic papers from CiteSeerX digital library.

    Args:
        query: Search query string (e.g., 'machine learning').
        max_results: Maximum number of papers to return (default: 10).
    Returns:
        List of paper metadata in dictionary format.
    """
    papers = await async_search(citeseerx_searcher, query, max_results)
    return papers if papers else []


@mcp.tool()
async def search_doaj(query: str, max_results: int = 10) -> List[Dict]:
    """Search academic papers from DOAJ (Directory of Open Access Journals).

    Args:
        query: Search query string (e.g., 'machine learning').
        max_results: Maximum number of papers to return (default: 10).
    Returns:
        List of paper metadata in dictionary format.
    """
    papers = await async_search(doaj_searcher, query, max_results)
    return papers if papers else []


@mcp.tool()
async def search_base(query: str, max_results: int = 10) -> List[Dict]:
    """Search academic papers from BASE (Bielefeld Academic Search Engine).

    Args:
        query: Search query string (e.g., 'machine learning').
        max_results: Maximum number of papers to return (default: 10).
    Returns:
        List of paper metadata in dictionary format.
    """
    papers = await async_search(base_searcher, query, max_results)
    return papers if papers else []


@mcp.tool()
async def search_zenodo(query: str, max_results: int = 10) -> List[Dict]:
    """Search academic papers from Zenodo open repository.

    Args:
        query: Search query string (e.g., 'machine learning').
        max_results: Maximum number of papers to return (default: 10).
    Returns:
        List of paper metadata in dictionary format.
    """
    papers = await async_search(zenodo_searcher, query, max_results)
    return papers if papers else []


@mcp.tool()
async def search_hal(query: str, max_results: int = 10) -> List[Dict]:
    """Search academic papers from HAL open archive.

    Args:
        query: Search query string (e.g., 'machine learning').
        max_results: Maximum number of papers to return (default: 10).
    Returns:
        List of paper metadata in dictionary format.
    """
    papers = await async_search(hal_searcher, query, max_results)
    return papers if papers else []


@mcp.tool()
async def search_ssrn(query: str, max_results: int = 10) -> List[Dict]:
    """Search metadata records from SSRN.

    Note: SSRN connector is metadata-only and does not support direct PDF download.

    Args:
        query: Search query string (e.g., 'machine learning').
        max_results: Maximum number of papers to return (default: 10).
    Returns:
        List of paper metadata in dictionary format.
    """
    papers = await async_search(ssrn_searcher, query, max_results)
    return papers if papers else []


@mcp.tool()
async def search_unpaywall(query: str, max_results: int = 10) -> List[Dict]:
    """Lookup a DOI via Unpaywall and return OA metadata.

    Unpaywall is DOI-centric and does not support generic keyword search.
    This tool extracts the first DOI from `query` and returns at most one record.

    Args:
        query: DOI string or text containing a DOI.
        max_results: Kept for API consistency; Unpaywall returns max 1 record.
    Returns:
        List with one paper metadata dict when DOI is resolvable, else empty list.
    """
    papers = await async_search(unpaywall_searcher, query, max_results)
    return papers if papers else []


@mcp.tool()
async def read_dblp_paper(paper_id: str, save_path: str = "./downloads") -> str:
    """Attempt to read and extract text content from a dblp paper.

    Note: dblp doesn't provide direct paper content access.
    This function returns an informative message.

    Args:
        paper_id: dblp paper identifier.
        save_path: Directory where the PDF would be saved (unused).
    Returns:
        str: Message indicating that direct paper reading is not supported.
    """
    return dblp_searcher.read_paper(paper_id, save_path)


@mcp.tool()
async def download_dblp(paper_id: str, save_path: str = "./downloads") -> str:
    """Download PDF for a paper from dblp.

    Note: dblp doesn't provide direct PDF access.
    This function returns an informative message.

    Args:
        paper_id: dblp paper identifier.
        save_path: Directory to save the PDF (default: './downloads').
    Returns:
        str: Message indicating that direct PDF download is not supported.
    """
    return dblp_searcher.download_pdf(paper_id, save_path)


@mcp.tool()
async def read_openaire_paper(paper_id: str, save_path: str = "./downloads") -> str:
    """Attempt to read and extract text content from an OpenAIRE paper.

    Args:
        paper_id: OpenAIRE paper identifier.
        save_path: Directory where the PDF is/will be saved (default: './downloads').
    Returns:
        str: Extracted text or error message.
    """
    return openaire_searcher.read_paper(paper_id, save_path)


@mcp.tool()
async def download_openaire(paper_id: str, save_path: str = "./downloads") -> str:
    """Download PDF for a paper from OpenAIRE.

    Args:
        paper_id: OpenAIRE paper identifier.
        save_path: Directory to save the PDF (default: './downloads').
    Returns:
        str: Path to downloaded PDF or error message.
    """
    return openaire_searcher.download_pdf(paper_id, save_path)


@mcp.tool()
async def read_citeseerx_paper(paper_id: str, save_path: str = "./downloads") -> str:
    """Read and extract text content from a CiteSeerX paper.

    Args:
        paper_id: CiteSeerX paper identifier.
        save_path: Directory where the PDF is/will be saved (default: './downloads').
    Returns:
        str: Extracted text or fallback abstract/error message.
    """
    return citeseerx_searcher.read_paper(paper_id, save_path)


@mcp.tool()
async def download_citeseerx(paper_id: str, save_path: str = "./downloads") -> str:
    """Download PDF for a paper from CiteSeerX.

    Args:
        paper_id: CiteSeerX paper identifier.
        save_path: Directory to save the PDF (default: './downloads').
    Returns:
        str: Path to downloaded PDF or error message.
    """
    return citeseerx_searcher.download_pdf(paper_id, save_path)


@mcp.tool()
async def read_doaj_paper(paper_id: str, save_path: str = "./downloads") -> str:
    """Read and extract text content from a DOAJ paper.

    Args:
        paper_id: DOAJ paper identifier.
        save_path: Directory where the PDF is/will be saved (default: './downloads').
    Returns:
        str: Extracted text content.
    """
    return doaj_searcher.read_paper(paper_id, save_path)


@mcp.tool()
async def download_doaj(paper_id: str, save_path: str = "./downloads") -> str:
    """Download PDF for a paper from DOAJ.

    Args:
        paper_id: DOAJ paper identifier.
        save_path: Directory to save the PDF (default: './downloads').
    Returns:
        str: Path to downloaded PDF.
    """
    return doaj_searcher.download_pdf(paper_id, save_path)


@mcp.tool()
async def read_base_paper(paper_id: str, save_path: str = "./downloads") -> str:
    """Read and extract text content from a BASE paper.

    Args:
        paper_id: BASE paper identifier.
        save_path: Directory where the PDF is/will be saved (default: './downloads').
    Returns:
        str: Extracted text content.
    """
    return base_searcher.read_paper(paper_id, save_path)


@mcp.tool()
async def download_base(paper_id: str, save_path: str = "./downloads") -> str:
    """Download PDF for a paper from BASE.

    Args:
        paper_id: BASE paper identifier.
        save_path: Directory to save the PDF (default: './downloads').
    Returns:
        str: Path to downloaded PDF.
    """
    return base_searcher.download_pdf(paper_id, save_path)


@mcp.tool()
async def read_zenodo_paper(paper_id: str, save_path: str = "./downloads") -> str:
    """Read and extract text content from a Zenodo paper.

    Args:
        paper_id: Zenodo paper identifier.
        save_path: Directory where the PDF is/will be saved (default: './downloads').
    Returns:
        str: Extracted text content.
    """
    return zenodo_searcher.read_paper(paper_id, save_path)


@mcp.tool()
async def download_zenodo(paper_id: str, save_path: str = "./downloads") -> str:
    """Download PDF for a paper from Zenodo.

    Args:
        paper_id: Zenodo paper identifier.
        save_path: Directory to save the PDF (default: './downloads').
    Returns:
        str: Path to downloaded PDF.
    """
    return zenodo_searcher.download_pdf(paper_id, save_path)


@mcp.tool()
async def read_hal_paper(paper_id: str, save_path: str = "./downloads") -> str:
    """Read and extract text content from a HAL paper.

    Args:
        paper_id: HAL paper identifier.
        save_path: Directory where the PDF is/will be saved (default: './downloads').
    Returns:
        str: Extracted text content.
    """
    return hal_searcher.read_paper(paper_id, save_path)


@mcp.tool()
async def download_hal(paper_id: str, save_path: str = "./downloads") -> str:
    """Download PDF for a paper from HAL.

    Args:
        paper_id: HAL paper identifier.
        save_path: Directory to save the PDF (default: './downloads').
    Returns:
        str: Path to downloaded PDF.
    """
    return hal_searcher.download_pdf(paper_id, save_path)


@mcp.tool()
async def read_ssrn_paper(paper_id: str, save_path: str = "./downloads") -> str:
    """Read paper content from SSRN.

    Note: SSRN connector is metadata-only and read is not supported.

    Args:
        paper_id: SSRN paper identifier.
        save_path: Directory where the PDF is/will be saved (unused).
    Returns:
        str: Error message from metadata-only SSRN connector.
    """
    return ssrn_searcher.read_paper(paper_id, save_path)


@mcp.tool()
async def download_ssrn(paper_id: str, save_path: str = "./downloads") -> str:
    """Download PDF for a paper from SSRN.

    Note: SSRN connector is metadata-only and download is not supported.

    Args:
        paper_id: SSRN paper identifier.
        save_path: Directory to save the PDF (unused).
    Returns:
        str: Error message from metadata-only SSRN connector.
    """
    return ssrn_searcher.download_pdf(paper_id, save_path)


@mcp.tool()
async def read_openalex_paper(paper_id: str, save_path: str = "./downloads") -> str:
    """Attempt to read and extract text content from an OpenAlex paper.

    Args:
        paper_id: OpenAlex paper ID.
        save_path: Directory where the PDF is/will be saved (default: './downloads').
    Returns:
        str: Message indicating that direct paper reading is not supported natively.
    """
    return openalex_searcher.read_paper(paper_id, save_path)


@mcp.tool()
async def download_openalex(paper_id: str, save_path: str = "./downloads") -> str:
    """Download PDF for a paper from OpenAlex.

    Args:
        paper_id: OpenAlex paper ID.
        save_path: Directory to save the PDF (default: './downloads').
    Returns:
        str: Error message, typically OpenAlex relies on extracted pdf_url instead of direct downloads.
    """
    return await asyncio.to_thread(openalex_searcher.download_pdf, paper_id, save_path)


# ---------------------------------------------------------------------------
# Optional IEEE Xplore tools — registered only when API key is set
# ---------------------------------------------------------------------------
if ieee_searcher is not None:
    @mcp.tool()
    async def search_ieee(query: str, max_results: int = 10) -> List[Dict]:
        """Unimplemented IEEE search stub; always reports an error. Requires PAPER_SEARCH_MCP_IEEE_API_KEY (or IEEE_API_KEY).

        Args:
            query: Search query string.
            max_results: Maximum number of results (default: 10).
        Returns:
            List of paper dicts from IEEE Xplore.
        """
        return await async_search(ieee_searcher, query, max_results)

    @mcp.tool()
    async def download_ieee(paper_id: str, save_path: str = "./downloads") -> str:
        """Download a PDF from IEEE Xplore.  Requires PAPER_SEARCH_MCP_IEEE_API_KEY (or IEEE_API_KEY) and institutional access.

        Args:
            paper_id: IEEE Xplore paper identifier.
            save_path: Directory to save the PDF (default: './downloads').
        Returns:
            str: Path to saved PDF or error message.
        """
        return await asyncio.to_thread(ieee_searcher.download_pdf, paper_id, save_path)

    @mcp.tool()
    async def read_ieee_paper(paper_id: str, save_path: str = "./downloads") -> str:
        """Download and read an IEEE Xplore paper.  Requires PAPER_SEARCH_MCP_IEEE_API_KEY (or IEEE_API_KEY).

        Args:
            paper_id: IEEE Xplore paper identifier.
            save_path: Directory where the PDF is/will be saved (default: './downloads').
        Returns:
            str: Extracted text content.
        """
        return ieee_searcher.read_paper(paper_id, save_path)


# ---------------------------------------------------------------------------
# Optional ACM Digital Library tools — registered only when API key is set
# ---------------------------------------------------------------------------
if acm_searcher is not None:
    @mcp.tool()
    async def search_acm(query: str, max_results: int = 10) -> List[Dict]:
        """Unimplemented ACM search stub; always reports an error. Requires PAPER_SEARCH_MCP_ACM_API_KEY (or ACM_API_KEY).

        Args:
            query: Search query string.
            max_results: Maximum number of results (default: 10).
        Returns:
            List of paper dicts from ACM DL.
        """
        return await async_search(acm_searcher, query, max_results)

    @mcp.tool()
    async def download_acm(paper_id: str, save_path: str = "./downloads") -> str:
        """Download a PDF from ACM Digital Library.  Requires PAPER_SEARCH_MCP_ACM_API_KEY (or ACM_API_KEY) and institutional access.

        Args:
            paper_id: ACM DL paper identifier.
            save_path: Directory to save the PDF (default: './downloads').
        Returns:
            str: Path to saved PDF or error message.
        """
        return await asyncio.to_thread(acm_searcher.download_pdf, paper_id, save_path)

    @mcp.tool()
    async def read_acm_paper(paper_id: str, save_path: str = "./downloads") -> str:
        """Download and read an ACM Digital Library paper.  Requires PAPER_SEARCH_MCP_ACM_API_KEY (or ACM_API_KEY).

        Args:
            paper_id: ACM DL paper identifier.
            save_path: Directory where the PDF is/will be saved (default: './downloads').
        Returns:
            str: Extracted text content.
        """
        return acm_searcher.read_paper(paper_id, save_path)


if scopus_searcher is not None:
    @mcp.tool()
    async def search_scopus(query: str, max_results: int = 10, sort: str = "relevance",
                            field: str | None = None, date: str | None = None) -> list[dict]:
        """Search Scopus with PAPER_SEARCH_MCP_SCOPUS_API_KEY and subscriber entitlement.

        query supports Scopus TITLE/ABS/KEY/AUTH/AFFILORG fields, Boolean and
        proximity operators, and wildcards. field optionally wraps the query.
        sort accepts relevance, coverDate, citedby-count, creator, and other
        Scopus sort fields; prefix + for ascending or - for descending.
        date accepts a year or range: 2024, 2020-2024, 2020-, or -2024.
        Results use COMPLETE view, paginated in batches of at most 25.
        """
        return await async_search(scopus_searcher, query, max_results, sort=sort, field=field, date=date)

    @mcp.tool()
    async def read_scopus_paper(paper_id: str, save_path: str = "./downloads") -> str:
        """Read entitled ScienceDirect full text for a numeric Scopus ID.

        Returns metadata and full text, or a labeled abstract-only result when
        full text is unavailable. SCOPUS_ID: prefixes are accepted.
        Requires PAPER_SEARCH_MCP_SCOPUS_API_KEY. save_path is unused (no file is saved).
        """
        return await asyncio.to_thread(scopus_searcher.read_paper, paper_id, save_path)

    @mcp.tool()
    async def download_scopus(paper_id: str, save_path: str = "./downloads") -> str:
        """Save an entitled ScienceDirect PDF as <numeric Scopus ID>.pdf.

        Requires PAPER_SEARCH_MCP_SCOPUS_API_KEY and article entitlement.
        Accepts numeric IDs with or without SCOPUS_ID:. Returns the saved path.
        """
        return await asyncio.to_thread(scopus_searcher.download_pdf, paper_id, save_path)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
