"""Saved protocols and crash-resumable, explicitly budgeted provider batches."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from uuid import uuid4

from .discovery import semantic_mode
from .http import RequestAllowance, sanitize
from .library import Library, encode, identifiers, validate_date
from .provider_models import ProviderError, ProviderPage, RejectedRecord, SavedQuery
from .registry import SOURCES, provider
from .review_models import ReviewProtocol, SearchRequest
from .storage import now, run_lock




class Reviews(Library):
    def __init__(self, directory=None, *, provider_factory=provider):
        super().__init__(directory)
        self.provider_factory = provider_factory

    @staticmethod
    def validate_sources(protocol):
        results = []
        for name in protocol.selected_sources:
            source = SOURCES.get(name)
            issue = None
            if source is None:
                issue = "Unknown source."
            elif not source.describe().available:
                issue = "Source unavailable: " + "; ".join(source.describe().missing_configuration + list(source.limitations))
            elif not source.provider_pages:
                issue = "Resumable review searches are not implemented for this source."
            results.append({"source": name, "valid": issue is None, "message": issue})
        for query in protocol.queries:
            source = SOURCES.get(query.source)
            issue = None
            if not query.query.strip():
                issue = "Native query must not be blank."
            elif source and query.mode not in source.query_modes:
                issue = "Unsupported query mode."
            elif source and set(query.filters) - set(source.filters):
                issue = "Unsupported filters: " + ", ".join(sorted(set(query.filters) - set(source.filters)))
            elif source and query.sort is not None and query.sort not in source.sorts:
                issue = "Unsupported sort."
            elif query.source == "scopus" and query.filters:
                from .academic_platforms.scopus import ScopusSearcher
                try:
                    if "date" in query.filters:
                        ScopusSearcher._normalize_date_range(query.filters["date"])
                    if "field" in query.filters:
                        issue = "Use the field expression directly in the native query."
                except (ValueError, TypeError, AttributeError):
                    issue = "Invalid Scopus date filter; use YYYY or YYYY-YYYY."
            if query.source in {"crossref", "arxiv"}:
                from .academic_platforms.crossref import CrossRefSearcher
                from .academic_platforms.arxiv import ArxivSearcher
                try:
                    (CrossRefSearcher if query.source == "crossref" else ArxivSearcher).validate_query(query)
                except (ValueError, TypeError) as exc:
                    issue = str(exc)
            if not issue and query.source == "semantic":
                try:
                    semantic_mode(query)
                except ValueError as exc:
                    issue = str(exc)
            if issue:
                results.append({"source": query.source, "valid": False, "message": issue})
        return results

    def create_review(self, protocol: ReviewProtocol, *, idempotency_key: str):
        protocol = ReviewProtocol.model_validate(protocol)
        payload = sanitize(protocol.model_dump(mode="json"))
        def create(connection):
            review_id = uuid4().hex
            timestamp = now()
            connection.execute("INSERT INTO reviews VALUES (?, ?, ?)", (review_id, encode(payload), timestamp))
            connection.execute("INSERT INTO protocol_revisions VALUES (?, 1, ?, ?)", (review_id, encode(payload), timestamp))
            return {"review_id": review_id, "validation": self.validate_sources(protocol)}
        return self._operation(idempotency_key, {"create_review": payload}, create)

    def update_review(self, review_id: str, protocol: ReviewProtocol, *, idempotency_key: str):
        protocol = ReviewProtocol.model_validate(protocol)
        payload = sanitize(protocol.model_dump(mode="json"))
        def update(connection):
            if not connection.execute("SELECT 1 FROM reviews WHERE id=?", (review_id,)).fetchone():
                raise ValueError("Unknown review.")
            revision = connection.execute("SELECT max(revision)+1 FROM protocol_revisions WHERE review_id=?", (review_id,)).fetchone()[0]
            connection.execute("UPDATE reviews SET protocol=? WHERE id=?", (encode(payload), review_id))
            connection.execute("INSERT INTO protocol_revisions VALUES (?, ?, ?, ?)", (review_id, revision, encode(payload), now()))
            return {"review_id": review_id, "revision": revision, "validation": self.validate_sources(protocol)}
        return self._operation(idempotency_key, {"update_review": review_id, "protocol": payload}, update)

    def get_review(self, review_id: str):
        with self.db.transaction() as connection:
            row = connection.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown review.")
            revisions = [{**dict(r), "protocol": json.loads(r["protocol"])} for r in connection.execute(
                "SELECT * FROM protocol_revisions WHERE review_id=? ORDER BY revision", (review_id,))]
            return {"review_id": review_id, "protocol": json.loads(row["protocol"]), "created_at": row["created_at"],
                    "revision": revisions[-1]["revision"], "revisions": revisions}

    def list_reviews(self, *, limit: int = 20, after: str | None = None):
        if not 1 <= limit <= 100:
            raise ValueError("Display limit must be between 1 and 100.")
        with self.db.transaction() as connection:
            rows = connection.execute("SELECT * FROM reviews WHERE id>? ORDER BY id LIMIT ?", (after or "", limit + 1)).fetchall()
            return {"reviews": [{"review_id": r["id"], "protocol": json.loads(r["protocol"]), "created_at": r["created_at"]}
                                for r in rows[:limit]], "next_after": rows[limit - 1]["id"] if len(rows) > limit else None}

    def start_search(self, request: SearchRequest, *, idempotency_key: str):
        request = SearchRequest.model_validate(request)
        def start(connection):
            row = connection.execute("SELECT protocol FROM reviews WHERE id=?", (request.review_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown review.")
            protocol = ReviewProtocol.model_validate_json(row[0])
            if request.queries is not None:
                protocol = ReviewProtocol.model_validate({**protocol.model_dump(), "queries": request.queries})
            validation = self.validate_sources(protocol)
            queried = {q.source for q in protocol.queries}
            for name in set(protocol.selected_sources) - queried:
                validation.append({"source": name, "valid": False, "message": "A source-specific native query is required."})
            if set(request.provider_budgets) - set(protocol.selected_sources):
                raise ValueError("Provider budgets must refer to selected sources.")
            run_id = uuid4().hex
            revision = connection.execute("SELECT max(revision) FROM protocol_revisions WHERE review_id=?", (request.review_id,)).fetchone()[0]
            resolved_queries = []
            for query in protocol.queries:
                data = query.model_dump(mode="json")
                if query.source == "scopus" and "date" in query.filters and not any(
                    not v["valid"] and v["source"] == query.source for v in validation):
                    from .academic_platforms.scopus import ScopusSearcher
                    data["filters"]["date"] = ScopusSearcher._normalize_date_range(query.filters["date"])
                if query.source == "semantic" and not any(
                    not v["valid"] and v["source"] == query.source for v in validation):
                    data["mode"] = semantic_mode(query)
                if query.source in {"crossref", "arxiv"}:
                    data["sort"] = query.sort or "relevance"
                    key, default = ("order", "desc") if query.source == "crossref" else ("sort_order", "descending")
                    data["filters"].setdefault(key, default)
                resolved_queries.append(data)
            specification = {**request.model_dump(mode="json"), "queries": resolved_queries,
                             "selected_sources": protocol.selected_sources, "protocol_revision": revision}
            sources = {}
            for name in protocol.selected_sources:
                issues = [item["message"] for item in validation if item["source"] == name and not item["valid"]]
                sources[name] = {"status": "invalid" if issues else "ready", "validation": issues,
                                 "continuation": None, "requests_used": 0, "received": 0, "rejected": 0,
                                 "linked_duplicates": 0, "total": {"value": None, "precision": "unknown"},
                                 "error": None, "warnings": [], "page_hash": None}
            state = {"requests_used": 0, "sources": sources, "next_source": 0}
            connection.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?)",
                (run_id, request.review_id, encode(sanitize(specification)), encode(state), now()))
            return {"run_id": run_id, "validation": validation}
        return self._operation(idempotency_key, {"start_search": request.model_dump(mode="json")}, start)

    def get_run(self, run_id: str):
        run = self.get_run_state(run_id)
        with self.db.transaction() as connection:
            run["unique_publications"] = connection.execute("""SELECT count(DISTINCT o.publication_id)
                FROM query_hits h JOIN observations o ON o.id=h.observation_id WHERE h.run_id=?""", (run_id,)).fetchone()[0]
        state, spec = run["state"], run["specification"]
        run["remaining_requests"] = max(0, spec["request_budget"] - state["requests_used"])
        active = any(s["status"] in {"ready", "waiting"} for s in state["sources"].values())
        run["status"] = ("ready" if run["remaining_requests"] else "budget") if active else "stopped"
        run["limitation"] = "Exhaustion refers only to each saved provider query, not completeness of the literature."
        return run

    def _write_state(self, connection, run_id, state):
        connection.execute("UPDATE runs SET state=? WHERE id=?", (encode(state), run_id))

    def _apply_page(self, run_id, row):
        run = self.get_run_state(run_id)
        state = run["state"]
        source = state["sources"][row["source"]]
        page = ProviderPage.model_validate_json(row["payload"])
        digest = sha256(encode([r.model_dump(mode="json") for r in page.records] + [r.model_dump(mode="json") for r in page.rejected]).encode()).hexdigest()
        if not page.pagination_validated and page.state == "ready" and (page.continuation is None or page.continuation == source["continuation"] or
                                      (page.records or page.rejected) and digest == source["page_hash"]):
            page.state = "failed"
            page.error = ProviderError(kind="nonadvancing", message="Provider repeated its page or checkpoint.")
        source.update(status=page.state, continuation=page.continuation,
                      error=page.error.model_dump(mode="json") if page.error else None, warnings=page.warnings)
        if page.request_usage:
            source["request_usage"] = page.request_usage
        if page.total.value is not None:
            source["total"] = page.total.model_dump()
        if page.records or page.rejected:
            source["page_hash"] = digest
        source["received"] += len(page.records) + len(page.rejected)
        source["rejected"] += len(page.rejected)
        def finish(connection, records):
            source["linked_duplicates"] += sum(r["linked"] for r in records)
            self._write_state(connection, run_id, state)
            connection.execute("UPDATE received_pages SET applied=1 WHERE id=?", (row["id"],))
        self.ingest(run["review_id"], page.records, run_id=run_id, page=page,
                    idempotency_key="received-page:" + row["id"], _finish=finish)

    def _recover(self, run_id):
        with self.db.transaction() as connection:
            pages = connection.execute("""SELECT p.* FROM received_pages p JOIN advances a ON a.key=p.advance_key
                WHERE a.run_id=? AND p.applied=0 ORDER BY p.rowid""", (run_id,)).fetchall()
        for page in pages:
            self._apply_page(run_id, page)
        # Any previous owner is gone: its OS lock was released. Never replay its uncertain request.
        with self.db.transaction(write=True) as connection:
            unfinished = connection.execute("SELECT key FROM advances WHERE run_id=? AND outcome IS NULL", (run_id,)).fetchall()
        for row in unfinished:
            outcome = self.get_run(run_id)
            outcome["interrupted"] = True
            with self.db.transaction(write=True) as connection:
                connection.execute("UPDATE advances SET outcome=? WHERE key=?", (encode(outcome), row[0]))

    def advance_run(self, run_id: str, *, idempotency_key: str, max_requests: int = 4):
        if not idempotency_key.strip() or not 1 <= max_requests <= 4:
            raise ValueError("Supply an idempotency key and 1–4 maximum HTTP attempts.")
        # Validate IDs before using an opaque hash for the lock filename.
        self.get_run_state(run_id)
        fingerprint = encode({"run_id": run_id, "max_requests": max_requests})
        with run_lock(self.db.path.parent / (sha256(run_id.encode()).hexdigest() + ".lock")):
            self._recover(run_id)
            with self.db.transaction(write=True) as connection:
                saved = connection.execute("SELECT * FROM advances WHERE key=?", (idempotency_key,)).fetchone()
                if saved:
                    if saved["fingerprint"] != fingerprint:
                        raise ValueError("Idempotency key was already used for different input.")
                    return json.loads(saved["outcome"])
                connection.execute("INSERT INTO advances VALUES (?, ?, ?, NULL)", (idempotency_key, run_id, fingerprint))
            used = 0
            run = self.get_run_state(run_id)
            names = run["specification"]["selected_sources"]
            start = run["state"]["next_source"]
            for index in range(len(names)):
                name = names[(start + index) % len(names)]
                run = self.get_run_state(run_id)
                state, spec = run["state"], run["specification"]
                source = state["sources"][name]
                if source["status"] not in {"ready", "waiting"}:
                    continue
                retry_at = (source["error"] or {}).get("retry_at")
                if retry_at and datetime.fromisoformat(retry_at) > datetime.now(timezone.utc):
                    continue
                remaining = min(max_requests - used, spec["request_budget"] - state["requests_used"],
                                spec["provider_budgets"].get(name, spec["request_budget"]) - source["requests_used"])
                if remaining <= 0:
                    if source["requests_used"] >= spec["provider_budgets"].get(name, spec["request_budget"]):
                        source["status"] = "budget"
                        with self.db.transaction(write=True) as connection:
                            self._write_state(connection, run_id, state)
                    continue
                service = self
                class DurableAllowance(RequestAllowance):
                    def reserve(self):
                        super().reserve()
                        state["requests_used"] += 1
                        source["requests_used"] += 1
                        with service.db.transaction(write=True) as connection:
                            service._write_state(connection, run_id, state)
                allowance = DurableAllowance(remaining=remaining)
                query = next(SavedQuery.model_validate(q) for q in spec["queries"] if q["source"] == name)
                try:
                    page = self.provider_factory(name).search_page(query, source["continuation"], allowance=allowance)
                except Exception:
                    page = ProviderPage(state="failed", error=ProviderError(kind="service", message="Provider operation failed; earlier pages are preserved."))
                used += allowance.used
                # Preserve individually invalid metadata instead of losing the whole fetched page.
                valid = []
                for record in page.records:
                    try:
                        identifiers(record)
                        validate_date(record.published_date, record.date_precision)
                        valid.append(record)
                    except ValueError as exc:
                        page.rejected.append(RejectedRecord(raw=record.model_dump(mode="json"), reason=str(exc)))
                page.records = valid
                page.requests_used = allowance.used
                state["next_source"] = (start + index + 1) % len(names)
                page_id = uuid4().hex
                with self.db.transaction(write=True) as connection:
                    self._write_state(connection, run_id, state)
                    connection.execute("INSERT INTO received_pages(id, advance_key, source, payload) VALUES (?, ?, ?, ?)",
                        (page_id, idempotency_key, name, encode(sanitize(page.model_dump(mode="json")))))
                    row = connection.execute("SELECT * FROM received_pages WHERE id=?", (page_id,)).fetchone()
                self._apply_page(run_id, row)
                if used >= max_requests:
                    break
            outcome = self.get_run(run_id)
            with self.db.transaction(write=True) as connection:
                connection.execute("UPDATE advances SET outcome=? WHERE key=?", (encode(outcome), idempotency_key))
            return outcome
