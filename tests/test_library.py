"""C03 storage, provenance, conservative identity and reversible human decisions."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3

import pytest

from paper_search_mcp.library import Library
from paper_search_mcp.library_models import Merge, Separate, Override, MetadataOverride, Undo, Relate
from paper_search_mcp.provider_models import Author, Metadata, ProviderPage, SavedQuery
from paper_search_mcp.storage import Database, data_directory


def record(paper_id="1", **values):
    return Metadata(paper_id=paper_id, source=values.pop("source", "scopus"), title=values.pop("title", "A study of libraries"),
        authors=[Author(name="Alice Smith")], **values)


@pytest.fixture
def library(tmp_path):
    lib = Library(tmp_path)
    lib.review = lib.create_review({"question": "Question"}, idempotency_key="review")["review_id"]
    return lib


def ingest(library, records, key="ingest"):
    return library.ingest(library.review, records, idempotency_key=key)["records"]


def test_repeated_ingestion_restart_and_doi_variants(library):
    records = [record(doi="https://doi.org/10.1234/ABC"), record("2", source="crossref", doi=" doi:10.1234/abc ")]
    first = ingest(library, records)
    assert first[0]["publication_id"] == first[1]["publication_id"]
    restarted = Library(library.db.path.parent)
    assert restarted.ingest(library.review, records, idempotency_key="ingest")["records"] == first
    paper = restarted.get_paper(first[0]["publication_id"])
    assert len(paper["observations"]) == 2
    assert {o["source"] for o in paper["observations"]} == {"scopus", "crossref"}
    assert all(o["query_hits"][0]["review_id"] == library.review for o in paper["observations"])
    with pytest.raises(ValueError, match="different input"):
        ingest(library, [record("3")])


def test_crossref_doi_variants_link_without_changing_original_ids(library):
    variants = ["10.1234/ABC", "https://doi.org/10.1234/abc", "doi:10.1234/AbC"]
    rows = ingest(library, [record(value, source="crossref", doi=value) for value in variants])
    assert len({row["publication_id"] for row in rows}) == 1
    paper = library.get_paper(rows[0]["publication_id"])
    assert {o["original_id"] for o in paper["observations"]} == set(variants)


def test_equivalent_provider_article_types_link_but_preprints_do_not(library):
    rows = ingest(library, [record("a", source="openalex", doi="10.1234/a", publication_type="article"),
        record("b", source="crossref", doi="10.1234/a", publication_type="journal-article"),
        record("c", source="arxiv", doi="10.1234/a", publication_type="preprint")])
    assert rows[0]["publication_id"] == rows[1]["publication_id"] != rows[2]["publication_id"]
    paper = library.get_paper(rows[0]["publication_id"])
    assert {o["metadata"]["publication_type"] for o in paper["observations"]} == {"article", "journal-article"}


def test_conflicting_identifiers_and_similarity_never_auto_merge(library):
    rows = ingest(library, [record("1", doi="10.1234/a"), record("2", doi="10.1234/b"), record("3")])
    assert len({row["publication_id"] for row in rows}) == 3
    suggestions = library.possible_duplicates(rows[0]["publication_id"])["candidates"]
    assert {item["publication_id"] for item in suggestions} == {rows[1]["publication_id"], rows[2]["publication_id"]}
    rows = ingest(library, [record("1", doi="10.1234/c")], "conflict")
    assert not rows[0]["linked"]


def test_conflicting_title_and_versions_remain_distinct(library):
    rows = ingest(library, [record("1", doi="10.1234/a", publication_type="preprint"),
        record("2", source="crossref", doi="10.1234/a", publication_type="article"),
        record("3", source="other", doi="10.1234/a", title="Totally unrelated findings")])
    assert len({row["publication_id"] for row in rows}) == 3


def test_text_provenance_dates_and_deterministic_selection(library):
    rows = ingest(library, [record(doi="10.1234/a", abstract="Short snippet", text_kind="snippet", published_date="2024", date_precision="year"),
        record("2", source="crossref", doi="10.1234/a", abstract="Full abstract", published_date="2024-06", date_precision="month", citations=3)])
    paper_id = rows[0]["publication_id"]
    paper = library.get_paper(paper_id)
    assert paper["metadata"]["abstract"]["value"] == "Full abstract"
    assert paper["metadata"]["snippet"]["value"] == "Short snippet"
    assert paper["metadata"]["published_date"]["value"] == "2024-06"
    assert len(paper["alternatives"]["published_date"]) == 2
    assert "citations" not in paper["metadata"]
    change = library.resolve_publications(Override(actor="human", reason="Checked publisher", publication_id=paper_id,
        metadata=MetadataOverride(title="Correct title")), idempotency_key="override")
    assert library.get_paper(paper_id)["metadata"]["title"]["value"] == "Correct title"
    library.resolve_publications(Undo(actor="human", reason="Revert", resolution_id=change["resolution_id"]), idempotency_key="undo")
    assert library.get_paper(paper_id)["metadata"]["title"]["value"] == "A study of libraries"


def test_merge_and_separate_are_reversible_and_keep_hits(library):
    rows = ingest(library, [record("1"), record("2")])
    left, right = [row["publication_id"] for row in rows]
    merge = library.resolve_publications(Merge(actor="human", reason="Verified", publication_ids=[left, right]), idempotency_key="merge")
    assert len(library.get_paper(left)["observations"]) == 2
    assert len(library.query_review(library.review)["papers"]) == 1
    library.resolve_publications(Undo(actor="human", reason="Revert", resolution_id=merge["resolution_id"]), idempotency_key="undo-merge")
    assert len(library.query_review(library.review)["papers"]) == 2
    rows = ingest(library, [record("1")], "again")
    split = library.resolve_publications(Separate(actor="human", reason="Wrong record", publication_id=left,
        observation_ids=[rows[0]["observation_id"]]), idempotency_key="split")
    assert len(library.get_paper(split["new_publication_id"])["observations"]) == 1
    library.resolve_publications(Undo(actor="human", reason="Revert", resolution_id=split["resolution_id"]), idempotency_key="undo-split")
    assert len(library.get_paper(left)["observations"]) == 2


def test_undo_refuses_to_erase_later_changes(library):
    rows = ingest(library, [record("1"), record("2")])
    left, right = [row["publication_id"] for row in rows]
    merge = library.resolve_publications(Merge(actor="human", reason="Verified", publication_ids=[left, right]), idempotency_key="merge")
    library.resolve_publications(Override(actor="human", reason="Correction", publication_id=left,
        metadata=MetadataOverride(title="Corrected")), idempotency_key="later")
    with pytest.raises(ValueError, match="Later changes"):
        library.resolve_publications(Undo(actor="human", reason="Revert", resolution_id=merge["resolution_id"]), idempotency_key="undo")


def test_explicit_relationships_do_not_merge(library):
    rows = ingest(library, [record("1", publication_type="preprint"), record("2", publication_type="article")])
    left, right = [row["publication_id"] for row in rows]
    change = library.resolve_publications(Relate(actor="human", reason="Publisher link", publication_id=left,
        related_id=right, relationship="preprint_of"), idempotency_key="relate")
    assert len(library.query_review(library.review)["papers"]) == 2
    assert library.get_paper(right)["relationships"][0]["kind"] == "preprint_of"
    library.resolve_publications(Undo(actor="human", reason="Wrong link", resolution_id=change["resolution_id"]), idempotency_key="undo")
    assert not library.get_paper(right)["relationships"]


def test_atomic_page_checkpoint_and_rejected_records(library):
    run_id = library.save_run(library.review, SavedQuery(source="scopus", query="test"), idempotency_key="run")["run_id"]
    page = ProviderPage(records=[record()], rejected=[{"raw": {"bad": 1}, "reason": "Missing title"}])
    result = library.ingest(library.review, page.records, run_id=run_id, page=page, checkpoint={"start": 2}, idempotency_key="page")
    assert Library(library.db.path.parent).get_run_state(run_id)["state"] == {"start": 2}
    with library.db.transaction() as connection:
        stored = connection.execute("SELECT payload FROM pages WHERE id=?", (result["page_id"],)).fetchone()[0]
        assert json.loads(stored)["rejected"][0]["reason"] == "Missing title"
    with pytest.raises(ValueError, match="complete"):
        library.ingest(library.review, [], run_id=run_id, page=page, idempotency_key="truncated")


def test_transaction_interruption_rolls_back(library, monkeypatch):
    original = library._match
    calls = 0
    def interrupt(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt()
        return original(*args)
    monkeypatch.setattr(library, "_match", interrupt)
    with pytest.raises(KeyboardInterrupt):
        ingest(library, [record("1"), record("2")])
    with library.db.transaction() as connection:
        assert connection.execute("SELECT count(*) FROM observations").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM publications").fetchone()[0] == 0


def test_concurrent_idempotent_ingestion(library):
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: ingest(library, [record()]), range(8)))
    assert all(result == results[0] for result in results)
    assert len(library.get_paper(results[0][0]["publication_id"])["observations"]) == 1


def test_multiple_reviews_share_identity_preserve_associations(library):
    first = ingest(library, [record()])[0]
    second_review = library.create_review({}, idempotency_key="second-review")["review_id"]
    second = library.ingest(second_review, [record()], idempotency_key="second")["records"][0]
    assert first["publication_id"] == second["publication_id"]
    assert library.query_review(second_review)["papers"][0]["publication_id"] == first["publication_id"]


def test_migrations_backup_foreign_keys_and_wal(library, monkeypatch):
    from paper_search_mcp import storage
    with library.db.transaction() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    monkeypatch.setattr(storage, "MIGRATIONS", (*storage.MIGRATIONS, (3, ("CREATE TABLE future (id TEXT)",))))
    Database(library.db.path.parent)
    backups = list(library.db.path.parent.glob("library.before-v3.*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 2
        assert backup.execute("SELECT id FROM reviews").fetchone()[0] == library.review
    Database(library.db.path.parent)
    assert len(list(library.db.path.parent.glob("library.before-v3.*.sqlite3"))) == 1


def test_failed_migration_rolls_back(library, monkeypatch):
    from paper_search_mcp import storage
    monkeypatch.setattr(storage, "MIGRATIONS", (*storage.MIGRATIONS, (3, ("CREATE TABLE future (id TEXT)", "INVALID SQL"))))
    with pytest.raises(sqlite3.OperationalError):
        Database(library.db.path.parent)
    with library.db.transaction() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name='future'").fetchone()


def test_directory_defaults_and_override(monkeypatch, tmp_path):
    from paper_search_mcp import storage
    monkeypatch.setenv("PAPER_SEARCH_MCP_DATA_DIR", str(tmp_path))
    assert data_directory() == tmp_path
    monkeypatch.delenv("PAPER_SEARCH_MCP_DATA_DIR")
    monkeypatch.setattr(storage.sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert data_directory() == tmp_path / "paper-search-mcp"


@pytest.mark.parametrize("date,precision", [("2024-01-01", "year"), ("2024-13", "month"), (None, "day")])
def test_invalid_partial_dates_rejected(library, date, precision):
    with pytest.raises(ValueError):
        ingest(library, [record(published_date=date, date_precision=precision)])


def test_review_pagination(library):
    ingest(library, [record(str(i)) for i in range(25)])
    first = library.query_review(library.review)
    second = library.query_review(library.review, after=first["next_after"])
    assert len(first["papers"]) == 20 and len(second["papers"]) == 5
    assert second["next_after"] is None
    with pytest.raises(ValueError):
        library.query_review(library.review, limit=101)


def test_merge_alias_relationships_and_override_history(library):
    rows = ingest(library, [record(str(i)) for i in range(3)])
    a, b, c = [row["publication_id"] for row in rows]
    library.resolve_publications(Relate(actor="human", reason="Version", publication_id=c, related_id=b,
        relationship="version_of"), idempotency_key="relate")
    library.resolve_publications(Override(actor="human", reason="Checked", publication_id=b,
        metadata=MetadataOverride(title="Human title")), idempotency_key="override")
    merge = library.resolve_publications(Merge(actor="human", reason="Duplicate", publication_ids=[a, b]), idempotency_key="merge")
    assert library.get_paper(b)["publication_id"] == a
    assert library.get_paper(a)["relationships"][0]["related_id"] == a
    assert len(library.get_paper(a)["history"]) == 3
    library.resolve_publications(Undo(actor="human", reason="Revert", resolution_id=merge["resolution_id"]), idempotency_key="undo")
    assert library.get_paper(b)["publication_id"] == b
    assert library.get_paper(c)["relationships"][0]["related_id"] == b
    assert library.get_paper(b)["metadata"]["title"]["value"] == "Human title"


def test_large_library_bounded_queries_and_indexed_identity(library):
    """Exercise the public service against 100,000 persisted metadata records."""
    import tracemalloc
    metadata = record().model_dump_json()
    with library.db.transaction(write=True) as connection:
        for start in range(0, 100_000, 1000):
            ids = [f"fixture-{i:06}" for i in range(start, start + 1000)]
            connection.executemany("INSERT INTO publications(id, created_at) VALUES (?, '2026')", ((i,) for i in ids))
            connection.executemany("INSERT INTO observations VALUES (?, ?, 'scopus', ?, '2026', ?, 'a study of libraries', 'alice smith', '')",
                ((i, i, i, metadata) for i in ids))
            connection.executemany("INSERT INTO identifiers VALUES (?, 'source:scopus', ?)", ((i, i) for i in ids))
            connection.executemany("INSERT INTO query_hits VALUES (?, ?, NULL, NULL, 0)", ((library.review, i) for i in ids))
    tracemalloc.start()
    try:
        rows = ingest(library, [record("fixture-099999")])
        assert rows[0]["publication_id"] == "fixture-099999"
        assert len(library.query_review(library.review)["papers"]) == 20
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 10_000_000


def test_conflicting_author_or_year_does_not_link(library):
    first = record(doi="10.1234/a", published_date="2024", date_precision="year")
    different_author = first.model_copy(update={"source": "crossref", "authors": [Author(name="Bob Jones")]})
    different_year = first.model_copy(update={"source": "pubmed", "published_date": "2025"})
    rows = ingest(library, [first, different_author, different_year])
    assert len({row["publication_id"] for row in rows}) == 3


def test_cli_reads_and_resolves_same_library(library, monkeypatch, capsys, tmp_path):
    from paper_search_mcp.cli import main
    publication_id = ingest(library, [record()])[0]["publication_id"]
    monkeypatch.setenv("PAPER_SEARCH_MCP_DATA_DIR", str(library.db.path.parent))
    path = tmp_path / "resolution.json"
    path.write_text(json.dumps({"action": "override", "actor": "human", "reason": "Verified",
        "publication_id": publication_id, "metadata": {"title": "Updated"}}))
    monkeypatch.setattr("sys.argv", ["paper-search", "resolve-publications", str(path), "--idempotency-key", "cli"])
    with pytest.raises(SystemExit) as exit:
        main()
    assert exit.value.code == 0
    assert json.loads(capsys.readouterr().out)["resolution_id"]
    monkeypatch.setattr("sys.argv", ["paper-search", "get-paper", publication_id])
    with pytest.raises(SystemExit) as exit:
        main()
    assert exit.value.code == 0
    assert json.loads(capsys.readouterr().out)["metadata"]["title"]["value"] == "Updated"


def test_identifier_namespace_case_cannot_hide_conflicting_doi(library):
    with pytest.raises(ValueError, match="Conflicting DOI"):
        ingest(library, [record(doi="10.1234/a", identifiers={"DOI": "10.1234/b"})])
    with pytest.raises(ValueError, match="Conflicting identifier"):
        ingest(library, [record(identifiers={"doi": "10.1234/a", "DOI": "10.1234/b"})])


def test_legacy_metadata_conversion_produces_ingestible_date(library):
    from datetime import datetime
    from paper_search_mcp.paper import Paper
    original = Paper(paper_id="legacy", source="fixture", title="Study", authors=[], abstract="",
        doi="", published_date=datetime(2024, 6, 15), url="", pdf_url="")
    metadata = Metadata.from_paper(original)
    assert metadata.published_date == "2024-06-15" and metadata.date_precision == "day"
    assert ingest(library, [metadata])[0]["publication_id"]
