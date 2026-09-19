"""Source catalog and lazy provider factories shared by MCP and CLI.

Capabilities describe the current adapter, not everything an upstream API offers.
Listing never constructs a provider or verifies access over the network.
"""

from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from threading import Lock
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from .config import ENV_PREFIX, get_env


class SourceInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    configured: bool
    implemented: bool
    implementation: Literal["implemented", "stub", "unsupported"]
    legacy_search: bool
    available: bool
    access_verified: bool = False
    required_configuration: list[str]
    optional_configuration: list[str]
    missing_configuration: list[str]
    query_modes: list[str]
    pagination: str
    provider_pages: bool
    filters: list[str]
    sorts: list[str]
    retrieval_ceiling: int | None
    lookup: bool
    read: bool
    download: bool
    limitations: list[str]


@dataclass(frozen=True)
class Source:
    name: str
    factory: str
    query_modes: tuple[str, ...] = ("keyword",)
    pagination: str = "single_page"
    provider_pages: bool = False
    filters: tuple[str, ...] = ()
    sorts: tuple[str, ...] = ()
    retrieval_ceiling: int | None = None
    lookup: bool = False
    read: bool = True
    download: bool = True
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    implementation: Literal["implemented", "stub", "unsupported"] = "implemented"
    limitations: tuple[str, ...] = ()
    # Retrieval-only helpers stay outside legacy aggregate search.
    aggregate: bool = True

    def describe(self) -> SourceInfo:
        missing = [ENV_PREFIX + key for key in self.required if not get_env(key).strip()]
        implemented = self.implementation == "implemented"
        return SourceInfo(
            name=self.name, configured=not missing, implemented=implemented,
            implementation=self.implementation, legacy_search=self.aggregate and implemented and not missing,
            available=implemented and not missing,
            required_configuration=[ENV_PREFIX + key for key in self.required],
            optional_configuration=[ENV_PREFIX + key for key in self.optional],
            missing_configuration=missing, query_modes=list(self.query_modes),
            pagination=self.pagination, provider_pages=self.provider_pages, filters=list(self.filters),
            sorts=list(self.sorts), retrieval_ceiling=self.retrieval_ceiling, lookup=self.lookup, read=self.read,
            download=self.download, limitations=list(self.limitations),
        )

    def create(self, **kwargs: Any) -> Any:
        if self.implementation != "implemented":
            raise NotImplementedError(f"{self.name}: {' '.join(self.limitations)}")
        if missing := self.describe().missing_configuration:
            raise ValueError(f"Set {', '.join(missing)} to configure {self.name}.")
        module, name = self.factory.split(":")
        return getattr(import_module(f".academic_platforms.{module}", __package__), name)(**kwargs)


# Capabilities reflect implemented adapters; only typed pages support review runs.
SOURCES = {source.name: source for source in (
    Source("arxiv", "arxiv:ArxivSearcher", query_modes=("native_query", "keyword"),
           pagination="offset", provider_pages=True, filters=("sort_order",),
           sorts=("relevance", "lastUpdatedDate", "submittedDate"), retrieval_ceiling=30_000,
           limitations=("Three-second spacing between attempts; changing index, at most 30,000 results.",
                        "Work and version identifiers are preserved separately.")),
    Source("pubmed", "pubmed:PubMedSearcher", query_modes=("native_query",), read=False, download=False,
           limitations=("Bounded identifier search and metadata fetch; no history continuation.", "Full text is separate from PubMed metadata.")),
    Source("biorxiv", "biorxiv:BioRxivSearcher", query_modes=("date_category_harvest",), pagination="legacy_offset",
           limitations=("Category filtering over a recent date interval, not keyword discovery.", "Legacy fixed offset increments need audit; records may be skipped.")),
    Source("medrxiv", "medrxiv:MedRxivSearcher", query_modes=("date_category_harvest",), pagination="legacy_offset",
           limitations=("Category filtering over a recent date interval, not keyword discovery.", "Legacy fixed offset increments need audit; records may be skipped.")),
    Source("iacr", "iacr:IACRSearcher", limitations=("Bounded HTML discovery; markup and access remain unverified.",)),
    Source("semantic", "semantic:SemanticSearcher", query_modes=("native_query", "bulk", "relevance"), lookup=True,
           pagination="bulk_token_or_relevance_offset", provider_pages=True, filters=("year",),
           sorts=("relevance", "paperId:asc", "paperId:desc", "publicationDate:asc", "publicationDate:desc", "citationCount:asc", "citationCount:desc"),
           retrieval_ceiling=10_000_000,
           optional=("SEMANTIC_SCHOLAR_API_KEY",),
           limitations=("Bulk pages contain up to 1,000 records; relevance mode has a separate 1,000-result ceiling.", "PDF access depends on linked locations.")),
    Source("crossref", "crossref:CrossRefSearcher", lookup=True, read=False, download=False,
           query_modes=("native_query", "keyword"), pagination="cursor", provider_pages=True, filters=("filter", "order"),
           sorts=("relevance", "updated", "deposited", "indexed", "created", "is-referenced-by-count", "references", "score"),
           limitations=("Changing index is not an immutable snapshot; cursor sorting by issued/published/published-print/published-online is unsupported.",
                        "Deposited references can be incomplete; metadata links do not provide direct full text.")),
    Source("openalex", "openalex:OpenAlexSearcher", read=False, download=False,
           query_modes=("native_query", "keyword"), pagination="cursor", provider_pages=True,
           optional=("OPENALEX_API_KEY",),
           limitations=("At most 100 records per page; API access and credit limits apply.",)),
    Source("pmc", "pmc:PMCSearcher", query_modes=("native_query",),
           limitations=("Bounded identifier search and metadata fetch; no history continuation.", "PDF availability varies by article.")),
    Source("core", "core:CORESearcher", required=("CORE_API_KEY",),
           limitations=("Single page; existing anonymous and endpoint fallbacks await audit.", "A configured key does not verify API access or full text.")),
    Source("europepmc", "europepmc:EuropePMCSearcher", query_modes=("native_query",),
           limitations=("Single page; no cursor continuation.", "Full text is available only for supported open-access records.")),
    Source("dblp", "dblp:DBLPSearcher", read=False, download=False,
           limitations=("Single page; hit-offset continuation is not implemented.", "Metadata and publisher links only.")),
    Source("openaire", "openaire:OpenAiresearcher", read=False, download=False, optional=("OPENAIRE_API_KEY",),
           limitations=("Single page; endpoint fallbacks and access requirements await audit.", "Metadata may contain repository links; no native download/read.")),
    Source("citeseerx", "citeseerx:CiteSeerXSearcher", optional=("CITESEERX_API_KEY",),
           limitations=("Bounded legacy API/HTML fallbacks; interfaces and access remain unverified.",)),
    Source("doaj", "doaj:DOAJSearcher", optional=("DOAJ_API_KEY",),
           limitations=("Single page; journal/article fallback semantics await audit.", "PDF access depends on linked locations.")),
    Source("base", "base_search:BASESearcher", query_modes=(), pagination="unsupported", read=False, download=False,
           implementation="unsupported",
           limitations=("Legacy adapter incorrectly treats the BASE search interface as OAI-PMH; usable access configuration is not implemented.", "Use browser search until the adapter is repaired.")),
    Source("zenodo", "zenodo:ZenodoSearcher", query_modes=("native_query",), optional=("ZENODO_ACCESS_TOKEN",),
           limitations=("Single page; authentication-dependent page limits and versions await audit.",)),
    Source("hal", "hal:HALSearcher", query_modes=("native_query",),
           limitations=("Single page; no continuation exposed.", "PDF availability depends on repository deposits.")),
    Source("ssrn", "ssrn:SSRNSearcher", pagination="legacy_page",
           limitations=("HTML pagination and public PDF discovery; access challenges and changed markup await audit.",)),
    Source("unpaywall", "unpaywall:UnpaywallSearcher", query_modes=("doi_lookup",), pagination="not_applicable",
           lookup=True, read=False, download=False, required=("UNPAYWALL_EMAIL",),
           limitations=("Looks up the first DOI only, at most one record; no keyword discovery.", "Provides OA links, does not host full text.")),
    Source("google_scholar", "google_scholar:GoogleScholarSearcher", query_modes=("native_query", "relevance"), pagination="offset", provider_pages=True, retrieval_ceiling=1000, read=False, download=False,
           required=("SERPAPI_API_KEY",),
           limitations=("SerpAPI searches consume account quota; provider retrieval ceilings apply.", "Saved offsets; snippets are not abstracts; Scholar exposes at most approximately 1000 results.")),
    Source("scopus", "scopus:ScopusSearcher", query_modes=("native_query",), pagination="cursor", provider_pages=True, lookup=True,
           filters=("date",), sorts=("relevance", "relevancy", "-coverDate", "+coverDate", "-citedby-count", "+citedby-count"),
           required=("SCOPUS_API_KEY",), optional=("SCOPUS_INST_TOKEN",),
           limitations=("COMPLETE view, at most 25 records per request; saved cursor pagination for reviews.", "Search requires subscriber entitlement; ScienceDirect full text is article-dependent.")),
    Source("ieee", "ieee:IEEESearcher", query_modes=(), pagination="unsupported", read=False, download=False,
           required=("IEEE_API_KEY",), implementation="stub",
           limitations=("IEEE search, download and reading are stubs even with an API key. Use browser search.",)),
    Source("acm", "acm:ACMSearcher", query_modes=(), pagination="unsupported", read=False, download=False,
           required=("ACM_API_KEY",), implementation="stub",
           limitations=("ACM search, download and reading are stubs even with an API key. Use browser search.",)),
    Source("chemrxiv", "chemrxiv:ChemRxivSearcher", query_modes=(), pagination="unsupported", read=False, download=False,
           implementation="unsupported", aggregate=False,
           limitations=("Experimental Crossref wrapper has an incompatible search call and unaudited filters; not exposed as working discovery.", "Use browser search until the adapter is repaired.")),
    Source("scihub", "sci_hub:SciHubFetcher", query_modes=(), pagination="not_applicable", read=False, aggregate=False,
           limitations=("Explicit legacy retrieval/fallback only; no discovery.", "Mirror and document access remain unverified.")),
)}


def list_sources() -> list[SourceInfo]:
    """Report current configuration and adapter capabilities without probing access."""
    return [source.describe() for source in SOURCES.values()]


def available_sources() -> list[str]:
    return [name for name, source in SOURCES.items() if source.aggregate and source.describe().available]


class LazyProvider:
    """Instantiate once on first operation, retaining legacy patchable wrappers."""

    def __init__(self, factory: Callable[[], Any]):
        self._factory = factory
        self._instance = None
        self._lock = Lock()

    def __getattr__(self, name: str) -> Any:
        with self._lock:
            if self._instance is None:
                self._instance = self._factory()
        return getattr(self._instance, name)


def provider(name: str, **kwargs: Any) -> LazyProvider:
    return LazyProvider(lambda: SOURCES[name].create(**kwargs))


def unpaywall_fallback() -> LazyProvider:
    # The legacy resolver deliberately skips lookup when its email is absent.
    return LazyProvider(lambda: import_module(
        ".academic_platforms.unpaywall", __package__,
    ).UnpaywallResolver())
