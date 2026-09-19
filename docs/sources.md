# Source capabilities

Generated from `paper_search_mcp/registry.py` with `uv run --locked python scripts/update_source_catalog.py`.

Capabilities describe existing adapters. Access remains unverified for every source. Scopus, Scholar, OpenAlex, Semantic Scholar, Crossref and arXiv expose resumable review runs; other adapters retain their stated limitations.

Configuration names below use the `PAPER_SEARCH_MCP_` prefix; unprefixed aliases remain supported. Empty prefixed values override aliases. No required values means the current adapter needs no configuration, not that upstream access is guaranteed.

| Source | Implementation | Query modes | Pagination / typed pages | Lookup / read / download | Required / optional configuration | Limitations |
|---|---|---|---|---|---|---|
| arxiv | implemented | native_query, keyword | offset / yes | no / yes / yes | none / none | Three-second spacing between attempts; changing index, at most 30,000 results. Work and version identifiers are preserved separately. |
| pubmed | implemented | native_query | single_page / no | no / no / no | none / none | Bounded identifier search and metadata fetch; no history continuation. Full text is separate from PubMed metadata. |
| biorxiv | implemented | date_category_harvest | legacy_offset / no | no / yes / yes | none / none | Category filtering over a recent date interval, not keyword discovery. Legacy fixed offset increments need audit; records may be skipped. |
| medrxiv | implemented | date_category_harvest | legacy_offset / no | no / yes / yes | none / none | Category filtering over a recent date interval, not keyword discovery. Legacy fixed offset increments need audit; records may be skipped. |
| iacr | implemented | keyword | single_page / no | no / yes / yes | none / none | Bounded HTML discovery; markup and access remain unverified. |
| semantic | implemented | native_query, bulk, relevance | bulk_token_or_relevance_offset / yes | yes / yes / yes | none / SEMANTIC_SCHOLAR_API_KEY | Bulk pages contain up to 1,000 records; relevance mode has a separate 1,000-result ceiling. PDF access depends on linked locations. |
| crossref | implemented | native_query, keyword | cursor / yes | yes / no / no | none / none | Changing index is not an immutable snapshot; cursor sorting by issued/published/published-print/published-online is unsupported. Deposited references can be incomplete; metadata links do not provide direct full text. |
| openalex | implemented | native_query, keyword | cursor / yes | no / no / no | none / OPENALEX_API_KEY | At most 100 records per page; API access and credit limits apply. |
| pmc | implemented | native_query | single_page / no | no / yes / yes | none / none | Bounded identifier search and metadata fetch; no history continuation. PDF availability varies by article. |
| core | implemented | keyword | single_page / no | no / yes / yes | CORE_API_KEY / none | Single page; existing anonymous and endpoint fallbacks await audit. A configured key does not verify API access or full text. |
| europepmc | implemented | native_query | single_page / no | no / yes / yes | none / none | Single page; no cursor continuation. Full text is available only for supported open-access records. |
| dblp | implemented | keyword | single_page / no | no / no / no | none / none | Single page; hit-offset continuation is not implemented. Metadata and publisher links only. |
| openaire | implemented | keyword | single_page / no | no / no / no | none / OPENAIRE_API_KEY | Single page; endpoint fallbacks and access requirements await audit. Metadata may contain repository links; no native download/read. |
| citeseerx | implemented | keyword | single_page / no | no / yes / yes | none / CITESEERX_API_KEY | Bounded legacy API/HTML fallbacks; interfaces and access remain unverified. |
| doaj | implemented | keyword | single_page / no | no / yes / yes | none / DOAJ_API_KEY | Single page; journal/article fallback semantics await audit. PDF access depends on linked locations. |
| base | unsupported | none | unsupported / no | no / no / no | none / none | Legacy adapter incorrectly treats the BASE search interface as OAI-PMH; usable access configuration is not implemented. Use browser search until the adapter is repaired. |
| zenodo | implemented | native_query | single_page / no | no / yes / yes | none / ZENODO_ACCESS_TOKEN | Single page; authentication-dependent page limits and versions await audit. |
| hal | implemented | native_query | single_page / no | no / yes / yes | none / none | Single page; no continuation exposed. PDF availability depends on repository deposits. |
| ssrn | implemented | keyword | legacy_page / no | no / yes / yes | none / none | HTML pagination and public PDF discovery; access challenges and changed markup await audit. |
| unpaywall | implemented | doi_lookup | not_applicable / no | yes / no / no | UNPAYWALL_EMAIL / none | Looks up the first DOI only, at most one record; no keyword discovery. Provides OA links, does not host full text. |
| google_scholar | implemented | native_query, relevance | offset / yes | no / no / no | SERPAPI_API_KEY / none | SerpAPI searches consume account quota; provider retrieval ceilings apply. Saved offsets; snippets are not abstracts; Scholar exposes at most approximately 1000 results. |
| scopus | implemented | native_query | cursor / yes | yes / yes / yes | SCOPUS_API_KEY / SCOPUS_INST_TOKEN | COMPLETE view, at most 25 records per request; saved cursor pagination for reviews. Search requires subscriber entitlement; ScienceDirect full text is article-dependent. |
| ieee | stub | none | unsupported / no | no / no / no | IEEE_API_KEY / none | IEEE search, download and reading are stubs even with an API key. Use browser search. |
| acm | stub | none | unsupported / no | no / no / no | ACM_API_KEY / none | ACM search, download and reading are stubs even with an API key. Use browser search. |
| chemrxiv | unsupported | none | unsupported / no | no / no / no | none / none | Experimental Crossref wrapper has an incompatible search call and unaudited filters; not exposed as working discovery. Use browser search until the adapter is repaired. |
| scihub | implemented | none | not_applicable / no | no / no / yes | none / none | Explicit legacy retrieval/fallback only; no discovery. Mirror and document access remain unverified. |
