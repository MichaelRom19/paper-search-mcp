# C01–C06 milestone verification

Reviewed on 2026-09-20 against the implementation plan, starting from `6651d14`.
This milestone provides persistent, resumable discovery through Scopus, Scholar,
OpenAlex, Semantic Scholar, Crossref and arXiv. C07 and later packets remain outside
this delivery.

| Packet | Verified behavior | Main deterministic coverage |
| --- | --- | --- |
| C01 | Python 3.14.7, locked stable dependencies, lazy shared registry, truthful configuration/stubs, CLI/MCP compatibility, PR/release CI | `test_registry.py`, `test_source_registration.py`, `test_package_entrypoints.py` |
| C02 | Typed pages, complete records and rejections, bounded HTTP attempts/retries, safe credentials, visible errors and waiting states | `test_provider_pages.py`, legacy Scopus/Scholar tests |
| C03 | SQLite migrations/backups, provenance, conservative identity, reversible resolutions, partial dates and bounded display | `test_library.py`, including 100,000 records |
| C04 | Protocol revisions, frozen queries, durable budgets, source isolation, replay, simultaneous advances and crash recovery | `test_reviews.py`, CLI and real stdio MCP tests |
| C05 | OpenAlex cursors/bearer authentication/credit observations, Semantic bulk tokens and explicit relevance mode, metadata without enrichment | `test_c05.py`, real stdio MCP tests |
| C06 | Crossref cursor parameters/sort validation/references, arXiv offsets/spacing/versions, provider-specific repeated-page detection | `test_c06.py`, real stdio MCP tests |

The final review corrected these gaps:

- Retain received records when continuation metadata is malformed, while stopping
  the source and preserving its previous checkpoint.
- Normalize Crossref DOI-based provider IDs and recognize equivalent
  `journal-article`/`article` types without changing original observations or
  merging preprints into published articles.
- Do not use incidental DOI mentions in Scholar snippets, titles or summaries as
  publication identity. Validate Scopus DOI/date fields at the provider boundary.
- Restore 36 deterministic parser, capability and mocked-download regressions
  that had been moved into the opt-in live directory with network tests.
- Correct outdated source-status and Crossref tool documentation.

Validation completed:

- **405 offline tests passed**, with network access blocked in ordinary tests.
- Source, tests and scripts compiled; generated source catalog matches the registry.
- Wheel and source distribution built using the locked build dependencies.
- The wheel was installed into a separate Python 3.14.7 environment with the
  exported locked dependencies. Both console entry points, CLI source listing,
  real MCP initialization/tool listing and persistent review/run operations passed
  from outside the repository.
- `git diff --check` passed.

Live searches were not invoked; credentials and provider entitlements remain
unverified. Local Docker execution was unavailable because its daemon was not
running. CI includes the container build, runtime and CLI checks. See the
[review guide](reviews.md) for provider contracts and their official references,
and the [source catalog](sources.md) for remaining adapter limitations.
