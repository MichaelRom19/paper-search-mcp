# Persistent library (C03)

C03 adds local storage and manual identity resolution. Saved protocols and resumable execution are available in [C04](reviews.md);
legacy searches remain stateless.
The existing Python 3.14.7 runtime and locked dependencies are unchanged.

## Storage

`PAPER_SEARCH_MCP_DATA_DIR` overrides the data directory. Defaults are:

| Platform | Directory |
| --- | --- |
| macOS | `~/Library/Application Support/paper-search-mcp` |
| Linux | `$XDG_DATA_HOME/paper-search-mcp`, or `~/.local/share/paper-search-mcp` |
| Windows | `%LOCALAPPDATA%/paper-search-mcp`, or `~/AppData/Local/paper-search-mcp` |

The library opens lazily when a library operation is invoked. `library.sqlite3`
uses foreign keys, WAL, a 30-second lock timeout, and a separate connection and
transaction per operation. No network I/O occurs inside transactions. Numbered
schema migrations run transactionally under a writer reservation. SQLite's backup
API creates `library.before-vN.<unique-id>.sqlite3` before a schema upgrade,
including committed WAL data. Backups are retained; failed migrations roll back.
A database from a newer schema is rejected.

Reviews share publications but keep separate query hits. Runs store immutable
query specifications and mutable checkpoints. Whole provider pages, including
rejected records and errors, are saved in the same transaction as observations,
hits and the checkpoint. A failed or interrupted transaction leaves none of that
page committed. The C04 engine additionally journals received pages before this atomic ingestion.

Every mutation requires an idempotency key. Repeating identical input with the
same key returns the original saved outcome without a second mutation. Reusing a
key for different input fails. A new retrieval under a new key deliberately adds
a dated observation, even when it links to an existing publication. Records with
no identifiers are not silently linked by title; retry their original ingestion
key to avoid repeating an operation.

## Identity and provenance

Automatic linking requires a normalized identifier match to exactly one existing
publication, with no contradictory identifiers, author sets, publication years,
titles, explicit publication types or versions. DOI URLs, `doi:` prefixes, case
and percent encoding normalize to the same DOI, including Crossref's DOI-based
provider IDs. Original identifiers remain unchanged in source observations.
Crossref's `journal-article` and OpenAlex's `article` are equivalent for identity
comparison; preprints remain distinct. Provider-local IDs remain scoped
to their source. arXiv version suffixes remain intact. Invalid DOIs or inconsistent
date precision fail ingestion atomically.

Identical/similar titles alone never merge. `possible_duplicates` suggests title
matches and similar titles with the same first author and year. It examines at
most 1001 indexed candidate observations per observation, returns at most 100
suggestions, and explicitly reports that limitation. It is not an exhaustive
fuzzy matching system. Conflicting identifier records remain separately inspectable.
Conservative checks can leave genuine duplicates for human resolution.

Observations retain provider/import source, original ID, retrieval time, structured
metadata, normalized identifiers and review/run/page associations. Source metadata
is not overwritten by linking or manual corrections. Citation counts stay inside
the dated provider observations; there is no combined citation total.

Snippets, abstracts and full text occupy separate selected fields. Dates retain
`year`, `month` or `day` precision without filling missing components. Selected
metadata is deterministic: human overrides first, then structured observations
before discovery snippets; longer abstract/full text and fuller author lists win,
and dates prefer explicit higher precision. Ties use source and lexical value.
All alternatives keep their originating observation IDs. This is a transparent
selection policy, not a claim that one source is always authoritative.

## MCP and CLI

| MCP tool | CLI command | Purpose |
| --- | --- | --- |
| `get_paper` | `get-paper ID` | Metadata, alternatives, observations, hits, relationships and resolution history |
| `query_review` | `query-review ID [--limit 20] [--after ID]` | Bounded keyset paging, maximum 100 publications |
| `possible_duplicates` | `possible-duplicates ID [--limit 20]` | Suggestions only |
| `resolve_publications` | `resolve-publications FILE --idempotency-key KEY` | Typed manual resolution |

MCP resolution accepts `request` and `idempotency_key`. The CLI reads the same
request from an explicit JSON file. Every request includes an actor and reason:

```json
{
  "action": "merge",
  "actor": "reviewer@example.org",
  "reason": "Verified the publisher record",
  "publication_ids": ["target-id", "duplicate-id"]
}
```

Supported request shapes (in addition to `actor` and `reason`):

- `merge`: `publication_ids`; the first is the target. Original IDs remain
  resolvable aliases. Observations and review hits survive, relationships redirect,
  and target overrides win conflicts while source overrides remain in history.
- `separate`: `publication_id`, `observation_ids`; moves a nonempty proper subset
  to a new publication without losing its original review/run associations.
- `override`: `publication_id`, `metadata`; supports title, structured authors,
  venue, abstract and publication date/precision. Supply date and precision
  together. Explicit null represents a deliberately unknown field.
- `relate`: `publication_id`, `related_id`, `relationship`; one of `preprint_of`,
  `version_of`, `report_of_same_study`. These relationships do not merge identity.
- `undo`: `resolution_id`; restores the prior assignment, overrides and
  relationships. If subsequent changes affect the captured state, undo refuses
  until those changes are reversed. History is retained, including the undo actor.

Manual resolution changes shared publication identity across reviews. Ingestion,
review creation and run storage currently have Python service primitives, used by
the C04 search and forthcoming C07 import workflows. No import/export or provider execution tool is
introduced in C03.

```python
from paper_search_mcp.library import Library
from paper_search_mcp.provider_models import Metadata

library = Library()
review = library.create_review({"question": "My review"}, idempotency_key="review-1")
result = library.ingest(
    review["review_id"],
    [Metadata(paper_id="1", source="example", title="A paper", doi="10.1234/example")],
    idempotency_key="page-1",
)
```

## Validation

Offline tests cover normalized DOI matches, conflicting IDs/authors/years,
similarity suggestions, preprint/version relationships, partial dates, snippets,
metadata alternatives, merge/separate/override undo, restart recovery, concurrent
idempotent writes, transaction interruption, migration backups and rollback,
real stdio MCP calls and matching CLI operations. A 100,000-record fixture checks
indexed ingestion and bounded review paging with a bounded Python allocation peak.
Import/export scale testing belongs to C07; live provider access is not tested here.
