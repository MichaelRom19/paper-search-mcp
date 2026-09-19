# Saved review protocols and resumable searches (C04–C06)

C04 connects Scopus and Google Scholar through SerpAPI to the persistent library.
C05 adds OpenAlex and Semantic Scholar; C06 adds Crossref and arXiv through the same MCP/CLI services.
Legacy searches remain bounded and stateless. Other sources remain discoverable
in `list_sources`, with visible limitations until their adapter packets land.
The Python 3.14.7 baseline and locked dependencies are unchanged.

## Protocol and search example

Save `protocol.json`:

```json
{
  "question": "How does intervention X affect outcome Y?",
  "scope": "Adult populations; published and unpublished studies",
  "eligibility_criteria": ["Comparative studies", "Reports outcome Y"],
  "selected_sources": ["scopus", "google_scholar"],
  "query_rationale": "Combine the intervention name and outcome synonyms",
  "seed_papers": ["10.1234/example"],
  "queries": [
    {"source": "scopus", "query": "TITLE-ABS-KEY(intervention AND outcome)", "page_size": 25},
    {"source": "google_scholar", "query": "\"intervention\" \"outcome\"", "page_size": 20}
  ]
}
```

Run `paper-search create-review protocol.json --idempotency-key review-1`.
Save `search.json` using the returned review ID:

```json
{
  "review_id": "REVIEW_ID",
  "request_budget": 100,
  "provider_budgets": {"google_scholar": 30}
}
```

```sh
paper-search start-search search.json --idempotency-key search-1
paper-search advance-run RUN_ID --idempotency-key batch-1
paper-search get-run RUN_ID
paper-search query-review REVIEW_ID --limit 20
paper-search advance-run RUN_ID --idempotency-key batch-2
```

Each new batch needs a new key. Repeating a completed advance key with identical
arguments returns its saved outcome without fetching again. Changed arguments
under that key fail. `start_search` can supply a `queries` list to override the
protocol queries for that run. Every selected source requires exactly one native
query. Save separately recorded runs for multiple query strategies.

| MCP tool | CLI command | Inputs |
| --- | --- | --- |
| `create_review` | `create-review FILE --idempotency-key KEY` | `protocol`, `idempotency_key` |
| `update_review` | `update-review ID FILE --idempotency-key KEY` | `review_id`, full replacement `protocol`, `idempotency_key` |
| `get_review` | `get-review ID` | `review_id` |
| `list_reviews` | `list-reviews [--limit 20] [--after ID]` | `limit`, `after` |
| `start_search` | `start-search FILE --idempotency-key KEY` | typed `request`, `idempotency_key` |
| `advance_run` | `advance-run ID --idempotency-key KEY [--max-requests 4]` | `run_id`, `idempotency_key`, `max_requests` |
| `get_run` | `get-run ID` | `run_id` |

MCP operations use typed inputs/outputs and bridge the same synchronous services
through `asyncio.to_thread`. The CLI accepts explicit JSON file paths. Display
pages default to 20 and allow at most 100; neither limits the stored corpus.

## Validation and frozen specifications

Protocols preserve question, scope, criteria, source selection, rationale, seeds
and source-native queries. Updates add immutable protocol revisions. Runs save
the selected revision, source names, native queries, resolved dates, page sizes,
sorts and budgets. Later protocol/configuration changes never add sources to an
existing run. Open Scopus year ranges are resolved once at run creation.

Creation and updates return visible source validation. Run creation retains all
requested names, marking unknown, unavailable, unimplemented or invalid sources
`invalid`; valid sources can still progress. A later configuration fix requires
a new run for a source marked invalid at creation. No provider requests occur
until `advance_run`. Credentials are neither copied into protocols nor used as
proof of access. Runtime credential/access failures are isolated per source.

The registry declares supported filters and sorts. Scopus supports the `date`
filter (year or ascending year range) and the declared relevance/date/citation
sorts. Write field expressions in the native Scopus query. Scholar currently
accepts native/relevance queries with no common filters or explicit sort. Other
options produce visible validation failures; arbitrary Boolean syntax is never
translated. Seeds are recorded references, not automatically fetched papers.

## Budgets, checkpoints and recovery

A total HTTP request budget is mandatory. Per-provider budgets are optional.
Each advance allows 1–4 HTTP attempts (default 4), including retries. It performs
at most one page operation per selected source, sequentially, rotating the first
source between batches. It may therefore return before spending all four attempts.
This stays within the four-provider concurrency ceiling and keeps only one page
operation active at a time. There is no worker, scheduler or automatic follow-up.

An OS file lock serializes advances of one run across processes and releases on
process death. Database transactions remain short and never span HTTP I/O.
Each attempt is committed against the budget before sending; an interrupted
request remains potentially spent. Received pages are journaled before ingestion.
Observations, query hits, checkpoint, counters and journal completion are committed
atomically. The next advance recovers journaled pages before doing new work.
An interrupted advance key returns a recovered outcome marked `interrupted` and
never repeats its uncertain request. Continue with a new key. A response lost
before it could be journaled may need refetching under that new key; the reserved
attempt is still charged. Keep the SQLite database and lock files on local storage.

Retry-After waiting states persist their earliest retry time and avoid premature
requests. Authentication, quota, malformed response, expired cursor, nonadvancing
pagination, provider ceiling and budget outcomes remain distinguishable. Earlier
pages and other sources survive failure. A terminal source can be retried through
a new explicitly budgeted run; existing budgets/specifications are immutable.

Provider totals remain separate from received records, rejected records, linked
duplicates and run-wide unique publications. Received includes rejected records.
Linked duplicates count observations attached to already stored publications,
including records from other reviews/runs. Unique publications are derived from
current identity assignments, so manual resolution can change that live count.
Every fetched page is retained, even when the display page is full. Duplicate-only
pages never imply exhaustion. Exact repeated pages/checkpoints stop visibly.
Exhaustion means exhaustion of the particular provider query, not completeness
of the literature.

## Provider behavior and limitations

Scopus review runs begin with `cursor=*`, persist opaque next cursors and continue
using COMPLETE view with at most 25 records. The legacy bounded collector retains
its offset requests. Elsevier documents a 5,000-result limit without cursor paging:
[request limits](https://dev.elsevier.com/api_key_settings.html) and
[cursor guide](https://dev.elsevier.com/guides/Scopus%20API%20Guide_V1_20230907.pdf).
Subscriber entitlement remains necessary and is not verified by a configured key.

Scholar persists the provider's next offset, retains SerpAPI's default cache
behavior, labels snippets explicitly, and exposes the approximate 1,000-result
ceiling. Reaching that ceiling reports `provider_cap`, not exhaustion. Counts are
estimated, and actual accessible results may be lower. See
[SerpAPI pagination and caching](https://serpapi.com/google-scholar-api) and
[Google Scholar's result limit](https://scholar.google.com/intl/en/scholar/help.html).
HTTP attempts still count against local budgets when SerpAPI serves cached data.

## Validation

Deterministic offline tests cover protocol revisions, frozen source selection and
resolved dates, unsupported options, source-isolated failures, complete-page
storage, duplicates/rejections/totals, exact replay, simultaneous advances,
request retries and total/per-provider budgets, Retry-After, expired cursors,
Scopus cursor parameters, Scholar ceilings/cache behavior, journal recovery,
actual subprocess death/restart, real MCP stdio and matching CLI calls.
Live provider access is unverified; these tests use injected providers/HTTP fixtures.


## C05: OpenAlex and Semantic Scholar

Use `openalex` and `semantic` in `selected_sources`. Example native query entries:

```json
[
  {"source": "openalex", "query": "intervention outcome", "page_size": 100},
  {"source": "semantic", "query": "intervention + outcome", "mode": "bulk",
   "filters": {"year": "2020-2026"}, "sort": "paperId:asc"}
]
```

OpenAlex uses `search` with cursor pagination beginning at `*`. Its supported
maximum is 100 records per request; deprecated 200-record requests are not used.
Set optional `PAPER_SEARCH_MCP_OPENALEX_API_KEY` (or `OPENALEX_API_KEY`) through the
existing environment loader. The key is sent as a bearer header, never in the URL.
OpenAlex currently accepts `native_query` and `keyword` modes; unsupported filters
and sorts are rejected visibly. Its reported total is kept separately from stored
record counts. See [paging](https://help.openalex.org/api/paging/) and
[authentication and credit reporting](https://help.openalex.org/api/authentication/).

Provider pages retain reported credit headers, including individual failed/retried
attempts when supplied, and OpenAlex's `meta.cost_usd`/`credits_used` fields. The
latest page's reported usage is exposed as `state.sources.openalex.request_usage`;
prior reports remain in the stored page journal. Missing usage remains unknown.
These observations are not a credit budget or billing estimate: the run budget
still counts HTTP attempts, and request costs are never assumed to be equal.

Semantic Scholar resolves `native_query` to `bulk` by default. Explicit
`mode: "relevance"` or `sort: "relevance"` selects relevance discovery instead.
The resolved mode and exact query are saved before any requests. Bulk and relevance
queries have different semantics; the adapter does not translate Boolean syntax.
Relevance mode combined with a bulk sort is invalid. Bulk sort fields are
`paperId`, `publicationDate` and `citationCount`, each with `:asc` or `:desc`.
The supported common filter is `year`: `YYYY`, `YYYY-YYYY`, `YYYY-`, or `-YYYY`.
These are literal provider filters, not a claim of an immutable upstream snapshot.

Bulk search returns up to 1,000 records per request and does not support the
requested `page_size`; every returned record is retained and this limitation is
reported in warnings. Opaque continuation tokens are stored under `cursor` (they
are pagination state, not credentials). Bulk totals are estimates and its retrieval
ceiling is 10,000,000 records. Relevance uses offsets, pages of at most 100, and a
1,000-result ceiling. A reached ceiling reports `provider_cap`, never exhaustion.
See the [Semantic Scholar API tutorial](https://webflow.semanticscholar.org/product/api/tutorial)
and [endpoint reference](https://api.semanticscholar.org/api-docs/graph).

The optional Semantic Scholar key continues to use
`PAPER_SEARCH_MCP_SEMANTIC_SCHOLAR_API_KEY` or its unprefixed alias, sent in
`x-api-key`. Rejected keys do not trigger an anonymous retry. Both adapters use
shared HTTPX request accounting, safe errors and Retry-After waiting states.
Discovery makes no per-result enrichment requests. External identifiers, available
abstracts and partial publication dates survive ingestion; DOIs are not inferred
from abstract text. Legacy wrappers remain bounded, returning the existing paper
shape; Semantic Scholar legacy searches remain relevance searches and
`fetch_details` remains a compatibility no-op. Provider failures raise errors rather
than returning an apparent successful empty search.

C04 was rechecked against its acceptance criteria before C05: all 270 baseline
offline tests passed, including restarts, late failures and idempotent advances.
C05 adds deterministic adapter, restart, overlap, quota, metadata, credit reporting,
legacy, CLI and real stdio MCP tests. No live searches were invoked; access-dependent
behavior remains unverified. Python 3.14.7 and locked dependencies are unchanged.


## C06: Crossref and arXiv

Add `crossref` or `arxiv` to the selected sources, with native query entries such as:

```json
[
  {"source": "crossref", "query": "intervention outcome", "page_size": 100,
   "sort": "created", "filters": {"filter": "from-created-date:2020-01-01,type:journal-article", "order": "asc"}},
  {"source": "arxiv", "query": "ti:intervention AND cat:cs.AI", "page_size": 100,
   "sort": "submittedDate", "filters": {"sort_order": "ascending"}}
]
```

Crossref starts at `cursor=*` and preserves query, native `filter`, page size,
`sort` and `order` on every continuation. Supported cursor sorts are `relevance`,
`updated`, `deposited`, `indexed`, `created`, `is-referenced-by-count`, `references`
and `score`; order is `asc` or `desc`. Cursor sorts `issued`, `published`,
`published-print` and `published-online` are rejected before HTTP execution with
an explicit validation message. A different sort is never silently substituted.
Omitted sort/order resolve to `relevance`/`desc` in the saved specification.
A short or empty provider page ends the query; duplicate counts do not.

Crossref's [current cursor contract](https://community.crossref.org/t/changes-to-cursors-filtering-and-sorting-in-the-rest-api/16246)
requires all parameters on continuation and a new cursor for each full page.
The changing index can produce overlaps or omissions, so runs warn that retrieval
is not an immutable upstream snapshot. Repeated native DOI page identities or
nonadvancing cursors fail visibly while preserving the received records.
Complete deposited reference objects, including unresolved reference text and
keys, remain in each observation's `extra.deposited_references`; available DOI
references also populate `references`. No citation resolution is performed yet.
Publication dates preserve year/month/day precision, and missing dates stay unknown.

arXiv sends native queries unchanged. Its explicit `keyword` mode and legacy
search wrapper retain the `all:` prefix. Supported sorts are `relevance`,
`submittedDate` and `lastUpdatedDate`, with `filters.sort_order` set to `ascending`
or `descending`. Omitted options resolve to `relevance`/`descending` in the saved
run. Each request repeats the same sort and advances the offset by the number of
received entries, including rejected entries. Returned offsets and versioned
record identities detect repeated pages independently of Crossref's cursor rules.
A premature empty page is a failure, not evidence of exhaustion. At the documented
30,000-result ceiling, remaining results produce `provider_cap`.

The [arXiv API manual](https://info.arxiv.org/help/api/user-manual.html) describes
paging, sorting, error feeds and request spacing. Every arXiv discovery attempt,
including retries, waits at least three seconds after the previous completed
attempt. An application-wide file lock and timestamp in the configured data
directory coordinate instances, processes and restarts. These files use
`PAPER_SEARCH_MCP_DATA_DIR` (or the standard application data directory), including
when a Python caller uses a separate review database directory. Discovery uses
HTTPS and shared HTTPX budgeting; spacing waits do not consume HTTP attempts.
HTTP Retry-After still produces a resumable waiting state when necessary.

Versioned arXiv IDs (including old category/slash IDs) remain in `paper_id` and
`identifiers.arxiv_version`; `identifiers.arxiv` holds the underlying work ID.
The explicit `version` and original URLs remain available in observations.
Different versions remain distinct publications, and repeat observations of an
already stored version link to that version. DOI metadata comes from the explicit
arXiv DOI element, never an incidental DOI in the abstract.

Both adapters preserve whole pages and rejection reasons. Invalid JSON/Atom,
HTML challenges, arXiv error feeds, HTTP failures and pagination failures become
visible outcomes. Legacy searches raise on failures rather than return a misleading
empty result; a valid empty search still returns `[]`. Crossref DOI lookup retains
`None` for a genuine 404 and raises other request/parser failures.

When a valid envelope contains records but malformed continuation metadata,
the received records are retained before the source stops. This also applies to
OpenAlex and Semantic Scholar. The last usable checkpoint is kept; an invalid
cursor never causes a received page to disappear.

Scholar derives DOI identity only from result/document links, never DOI mentions
in snippets, titles or publication summaries. Scopus rejects invalid explicit
DOIs and dates while preserving the original rejected record.

C05 verification passed all 47 focused tests and the 318-test pre-C06 suite without
requiring changes to its adapters. C06 tests cover review/legacy pagination,
versions and overlaps, restart/replay/journal recovery, invalid sorts, native
parameters, references, malformed envelopes/records, quota and late failures,
request spacing, CLI and real stdio MCP calls. Tests are offline; live provider
access remains unverified. Runtime and dependencies are unchanged. C07 is not part
of this delivery.
