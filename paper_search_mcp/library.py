"""Persistent provenance and conservative publication identity; no provider I/O."""
from difflib import SequenceMatcher
from hashlib import sha256
import json
import re
import unicodedata
from urllib.parse import unquote
from uuid import uuid4

from pydantic import TypeAdapter

from .http import sanitize
from .library_models import ResolutionRequest
from .provider_models import Metadata, ProviderPage, SavedQuery
from .storage import Database, now


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def text_key(value):
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", value).casefold()))


def identifiers(record):
    values = {}
    for namespace, value in record.identifiers.items():
        namespace = namespace.lower()
        if namespace in values and normalize_identifier(namespace, values[namespace]) != normalize_identifier(namespace, value):
            raise ValueError("Conflicting identifier fields in one record.")
        values[namespace] = value
    if record.doi:
        existing = values.get("doi")
        if existing and normalize_identifier("doi", existing) != normalize_identifier("doi", record.doi):
            raise ValueError("Conflicting DOI fields in one record.")
        values["doi"] = record.doi
    if record.paper_id:
        values[f"source:{record.source}"] = record.paper_id.strip()
    return {namespace.lower(): normalize_identifier(namespace.lower(), value)
            for namespace, value in values.items() if value.strip()}


def normalize_identifier(namespace, value):
    value = unicodedata.normalize("NFKC", value).strip()
    if namespace == "source:crossref" and re.match(r"(?:https?://(?:dx\.)?doi\.org/|doi:|10\.\d{4,9}/)", value, re.I):
        namespace = "doi"
    if namespace == "doi":
        value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", unquote(value), flags=re.I).strip().lower()
        if not re.fullmatch(r"10\.\d{4,9}/\S+", value):
            raise ValueError("Invalid DOI.")
    elif namespace in {"pmid", "pmcid", "arxiv", "openalex"}:
        value = value.casefold()
    return value


def validate_date(date, precision):
    from datetime import date as Date
    if date is None:
        if precision not in (None, "unknown"):
            raise ValueError("Date precision requires a date.")
        return
    size = {"year": 4, "month": 7, "day": 10}.get(precision)
    if size is None or len(date) != size:
        raise ValueError("Published date must match its year/month/day precision.")
    Date.fromisoformat(date + {4: "-01-01", 7: "-01", 10: ""}[size])


class Library:
    def __init__(self, directory=None):
        self.db = Database(directory)

    def _operation(self, key, request, callback):
        if not key or not key.strip():
            raise ValueError("An idempotency key is required.")
        fingerprint = sha256(encode(request).encode()).hexdigest()
        with self.db.transaction(write=True) as connection:
            saved = connection.execute("SELECT * FROM operations WHERE key=?", (key,)).fetchone()
            if saved:
                if saved["fingerprint"] != fingerprint:
                    raise ValueError("Idempotency key was already used for different input.")
                return json.loads(saved["outcome"])
            outcome = callback(connection)
            connection.execute("INSERT INTO operations VALUES (?, ?, ?)", (key, fingerprint, encode(outcome)))
            return outcome

    def create_review(self, protocol: dict, *, idempotency_key: str):
        """Storage primitive; protocol workflow and public review tools belong to C04."""
        protocol = sanitize(protocol)
        def create(connection):
            review_id = uuid4().hex
            connection.execute("INSERT INTO reviews VALUES (?, ?, ?)", (review_id, encode(protocol), now()))
            connection.execute("INSERT INTO protocol_revisions VALUES (?, 1, ?, ?)", (review_id, encode(protocol), now()))
            return {"review_id": review_id}
        return self._operation(idempotency_key, {"create_review": protocol}, create)

    def save_run(self, review_id: str, specification: SavedQuery, *, idempotency_key: str):
        def create(connection):
            run_id = uuid4().hex
            connection.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?)",
                               (run_id, review_id, encode(sanitize(specification.model_dump())), "{}", now()))
            return {"run_id": run_id}
        return self._operation(idempotency_key, {"save_run": specification.model_dump(), "review": review_id}, create)

    def get_run_state(self, run_id):
        with self.db.transaction() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown run.")
            return {**dict(row), "specification": json.loads(row["specification"]), "state": json.loads(row["state"])}

    @staticmethod
    def _require_publication(connection, publication_id):
        if not connection.execute("SELECT 1 FROM publications WHERE id=?", (publication_id,)).fetchone():
            raise ValueError("Unknown publication.")

    @staticmethod
    def _new_publication(connection):
        publication_id = uuid4().hex
        connection.execute("INSERT INTO publications(id, created_at) VALUES (?, ?)", (publication_id, now()))
        return publication_id

    def _match(self, connection, record, incoming):
        candidates = set()
        for namespace, value in incoming.items():
            candidates.update(row[0] for row in connection.execute(
                """SELECT DISTINCT o.publication_id FROM identifiers i JOIN observations o ON o.id=i.observation_id
                   WHERE i.namespace=? AND i.value=?""", (namespace, value)))
        if version_id := incoming.get("arxiv_version"):
            # Shared work IDs identify a family, not interchangeable manuscript versions.
            candidates = {candidate for candidate in candidates if not connection.execute(
                """SELECT 1 FROM identifiers i JOIN observations o ON o.id=i.observation_id
                   WHERE o.publication_id=? AND i.namespace='arxiv_version' AND i.value!=?""",
                (candidate, version_id)).fetchone()}
        if len(candidates) != 1:
            return None
        candidate = candidates.pop()
        existing = {}
        for row in connection.execute("""SELECT i.namespace, i.value FROM identifiers i
                JOIN observations o ON o.id=i.observation_id WHERE o.publication_id=?""", (candidate,)):
            existing.setdefault(row[0], set()).add(row[1])
        # Multiple contradictory identifiers are not resolved by a matching DOI.
        if any(namespace in existing and existing[namespace] != {value} for namespace, value in incoming.items()):
            return None
        for row in connection.execute("SELECT metadata FROM observations WHERE publication_id=?", (candidate,)):
            other = json.loads(row[0])
            for field in ("version", "publication_type"):
                left, right = getattr(record, field), other.get(field)
                if field == "publication_type":
                    left = "article" if left == "journal-article" else left
                    right = "article" if right == "journal-article" else right
                if left and right and left != right:
                    return None
            incoming_authors = {text_key(author.name) for author in record.authors}
            other_authors = {text_key(author["name"]) for author in other["authors"]}
            if incoming_authors and other_authors and incoming_authors.isdisjoint(other_authors):
                return None
            if record.published_date and other["published_date"] and record.published_date[:4] != other["published_date"][:4]:
                return None
            if record.title and other["title"] and SequenceMatcher(None, text_key(record.title), text_key(other["title"])).ratio() < .65:
                return None
        return candidate

    def ingest(self, review_id: str, records: list[Metadata], *, idempotency_key: str,
               run_id: str | None = None, page: ProviderPage | None = None,
               checkpoint: dict | None = None, _finish=None):
        """Persist an entire received page and its checkpoint in one atomic transaction."""
        records = [Metadata.model_validate(sanitize(record.model_dump())) for record in records]
        if page is not None and records != [Metadata.model_validate(sanitize(r.model_dump())) for r in page.records]:
            raise ValueError("Records must contain the complete provider page.")
        if (page is not None or checkpoint is not None) and run_id is None:
            raise ValueError("Pages and checkpoints require a run.")
        for record in records:
            validate_date(record.published_date, record.date_precision)
            identifiers(record)
        request = {"ingest": [record.model_dump() for record in records], "review": review_id,
                   "run": run_id, "page": page.model_dump(mode="json") if page else None, "checkpoint": checkpoint}
        def ingest_page(connection):
            if not connection.execute("SELECT 1 FROM reviews WHERE id=?", (review_id,)).fetchone():
                raise ValueError("Unknown review.")
            if run_id and not connection.execute("SELECT 1 FROM runs WHERE id=? AND review_id=?", (run_id, review_id)).fetchone():
                raise ValueError("Run does not belong to the review.")
            page_id = uuid4().hex if page is not None else None
            timestamp = now()
            if page is not None:
                connection.execute("INSERT INTO pages VALUES (?, ?, ?, ?)",
                    (page_id, run_id, encode(sanitize(page.model_dump(mode="json"))), timestamp))
            results = []
            for position, record in enumerate(records):
                incoming = identifiers(record)
                publication_id = self._match(connection, record, incoming)
                linked = publication_id is not None
                publication_id = publication_id or self._new_publication(connection)
                observation_id = uuid4().hex
                connection.execute("INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (observation_id, publication_id, record.source, record.paper_id, timestamp, encode(record.model_dump()),
                     text_key(record.title), text_key(record.authors[0].name) if record.authors else "", (record.published_date or "")[:4]))
                connection.executemany("INSERT INTO identifiers VALUES (?, ?, ?)",
                    ((observation_id, namespace, value) for namespace, value in incoming.items()))
                connection.execute("INSERT INTO query_hits VALUES (?, ?, ?, ?, ?)",
                                   (review_id, observation_id, run_id, page_id, position))
                results.append({"publication_id": publication_id, "observation_id": observation_id, "linked": linked})
            if checkpoint is not None:
                connection.execute("UPDATE runs SET state=? WHERE id=?", (encode(sanitize(checkpoint)), run_id))
            if _finish is not None:
                _finish(connection, results)
            return {"records": results, "page_id": page_id}
        return self._operation(idempotency_key, request, ingest_page)

    @staticmethod
    def _selected(connection, publication_id, observations):
        fields = ("title", "authors", "venue", "abstract", "snippet", "full_text", "published_date", "publication_type", "version", "url", "pdf_url")
        selected, alternatives = {}, {field: [] for field in fields}
        for observation in observations:
            metadata = observation["metadata"]
            for field in fields:
                value = metadata.get(field)
                if field in {"abstract", "snippet", "full_text"}:
                    value = metadata["abstract"] if metadata["text_kind"] == field else None
                if not value:
                    continue
                item = {"value": value, "observation_id": observation["id"], "source": observation["source"]}
                if field == "published_date":
                    item["date_precision"] = metadata["date_precision"]
                alternatives[field].append(item)
        # Structured records precede discovery snippets. Dates prefer genuine precision;
        # text prefers fuller content. Stable lexical ties make arrival order irrelevant.
        by_id = {observation["id"]: observation["metadata"] for observation in observations}
        for field, values in alternatives.items():
            def rank(item):
                metadata = by_id[item["observation_id"]]
                quality = len(item["value"]) if field in {"abstract", "full_text", "authors"} else 0
                if field == "published_date":
                    quality = {"year": 1, "month": 2, "day": 3, "unknown": 0}[item["date_precision"]]
                return (metadata["text_kind"] == "snippet", -quality, item["source"], encode(item["value"]), item["observation_id"])
            values.sort(key=rank)
            if values:
                selected[field] = values[0]
        for row in connection.execute("SELECT * FROM overrides WHERE publication_id=?", (publication_id,)):
            selected[row["field"]] = {"value": json.loads(row["value"]), "resolution_id": row["resolution_id"], "source": "human_override"}
        if "date_precision" in selected and "published_date" in selected:
            selected["published_date"]["date_precision"] = selected["date_precision"]["value"]
        return selected, alternatives

    def get_paper(self, publication_id: str):
        with self.db.transaction() as connection:
            self._require_publication(connection, publication_id)
            requested_id = publication_id
            while target := connection.execute("SELECT merged_into FROM publications WHERE id=?", (publication_id,)).fetchone()[0]:
                publication_id = target
            observations = [dict(row) for row in connection.execute(
                "SELECT * FROM observations WHERE publication_id=? ORDER BY id", (publication_id,))]
            for observation in observations:
                observation["metadata"] = json.loads(observation["metadata"])
                observation["identifiers"] = [dict(row) for row in connection.execute(
                    "SELECT namespace, value FROM identifiers WHERE observation_id=? ORDER BY namespace, value", (observation["id"],))]
                observation["query_hits"] = [dict(row) for row in connection.execute(
                    "SELECT * FROM query_hits WHERE observation_id=? ORDER BY review_id, run_id, position", (observation["id"],))]
            selected, alternatives = self._selected(connection, publication_id, observations)
            relationships = [dict(row) for row in connection.execute(
                "SELECT * FROM relationships WHERE publication_id=? OR related_id=?", (publication_id, publication_id))]
            members = [row[0] for row in connection.execute("""WITH RECURSIVE members(id) AS (
                SELECT ? UNION ALL SELECT p.id FROM publications p JOIN members m ON p.merged_into=m.id)
                SELECT id FROM members""", (publication_id,))]
            history = {}
            for member in members:
                for row in connection.execute("""SELECT r.id, r.request, r.created_at, r.undone_by FROM resolutions r
                    JOIN resolution_publications p ON p.resolution_id=r.id WHERE p.publication_id=?""", (member,)):
                    history[row["id"]] = {**dict(row), "request": json.loads(row["request"])}
                for row in connection.execute("SELECT * FROM overrides WHERE publication_id=?", (member,)):
                    alternatives.setdefault(row["field"], []).append({"value": json.loads(row["value"]),
                        "source": "human_override", "resolution_id": row["resolution_id"], "publication_id": member})
            return {"publication_id": publication_id, "requested_id": requested_id, "history": sorted(history.values(), key=lambda item: (item["created_at"], item["id"])), "metadata": selected, "alternatives": alternatives,
                    "observations": observations, "relationships": relationships}

    def query_review(self, review_id: str, *, limit: int = 20, after: str | None = None):
        if not 1 <= limit <= 100:
            raise ValueError("Display limit must be between 1 and 100.")
        with self.db.transaction() as connection:
            if not connection.execute("SELECT 1 FROM reviews WHERE id=?", (review_id,)).fetchone():
                raise ValueError("Unknown review.")
            ids = [row[0] for row in connection.execute("""SELECT DISTINCT o.publication_id FROM observations o
                JOIN query_hits h ON h.observation_id=o.id WHERE h.review_id=? AND o.publication_id>?
                ORDER BY o.publication_id LIMIT ?""", (review_id, after or "", limit + 1))]
            papers = []
            for publication_id in ids[:limit]:
                observations = [{**dict(row), "metadata": json.loads(row["metadata"])} for row in connection.execute(
                    "SELECT * FROM observations WHERE publication_id=?", (publication_id,))]
                selected, _ = self._selected(connection, publication_id, observations)
                papers.append({"publication_id": publication_id, "metadata": selected})
            return {"papers": papers, "next_after": ids[limit - 1] if len(ids) > limit else None}

    def possible_duplicates(self, publication_id: str, *, limit: int = 20):
        if not 1 <= limit <= 100:
            raise ValueError("Limit must be between 1 and 100.")
        with self.db.transaction() as connection:
            self._require_publication(connection, publication_id)
            matches = {}
            for row in connection.execute("SELECT * FROM observations WHERE publication_id=?", (publication_id,)):
                candidates = connection.execute("""SELECT * FROM observations WHERE publication_id!=? AND
                    (title_key=? OR (author_key=? AND author_key!='' AND year=? AND year!='')) ORDER BY id LIMIT 1001""",
                    (publication_id, row["title_key"], row["author_key"], row["year"]))
                for candidate in candidates:
                    score = SequenceMatcher(None, row["title_key"], candidate["title_key"]).ratio()
                    if score >= .8:
                        matches[candidate["publication_id"]] = {"publication_id": candidate["publication_id"],
                            "title_similarity": score, "reason": "Similar title; compare identifiers, authors, dates and versions."}
            return {"candidates": sorted(matches.values(), key=lambda item: (-item["title_similarity"], item["publication_id"]))[:limit],
                    "limitation": "Suggestions only; bounded to 1001 matching observations per source observation. No automatic similarity merges."}

    @staticmethod
    def _snapshot(connection, ids):
        state = {}
        for publication_id in ids:
            state[publication_id] = {
                "merged_into": connection.execute("SELECT merged_into FROM publications WHERE id=?", (publication_id,)).fetchone()[0],
                "observations": [row[0] for row in connection.execute("SELECT id FROM observations WHERE publication_id=? ORDER BY id", (publication_id,))],
                "overrides": [dict(row) for row in connection.execute("SELECT * FROM overrides WHERE publication_id=? ORDER BY field", (publication_id,))],
                "relationships": [dict(row) for row in connection.execute("SELECT * FROM relationships WHERE publication_id=? ORDER BY id", (publication_id,))]}
        return state

    def resolve_publications(self, request: ResolutionRequest, *, idempotency_key: str):
        request = TypeAdapter(ResolutionRequest).validate_python(request)
        payload = request.model_dump(mode="json")
        def resolve(connection):
            resolution_id = uuid4().hex
            action = request.action
            new_id = None
            if action == "undo":
                previous = connection.execute("SELECT * FROM resolutions WHERE id=?", (request.resolution_id,)).fetchone()
                if previous is None or previous["undone_by"] or json.loads(previous["request"])["action"] == "undo":
                    raise ValueError("Resolution cannot be undone.")
                expected = json.loads(previous["after_state"])
                ids = list(expected)
                if self._snapshot(connection, ids) != expected:
                    raise ValueError("Later changes affect these publications; undo those changes first.")
            elif action == "merge":
                ids = list(dict.fromkeys(request.publication_ids))
                if len(ids) < 2:
                    raise ValueError("Merge requires two distinct publications.")
            else:
                ids = [request.publication_id]
                if action == "relate":
                    if request.related_id == request.publication_id:
                        raise ValueError("A relationship requires distinct publications.")
                    ids.append(request.related_id)
            for publication_id in ids:
                self._require_publication(connection, publication_id)
                if action != "undo" and connection.execute("SELECT merged_into FROM publications WHERE id=?", (publication_id,)).fetchone()[0]:
                    raise ValueError("Use the current publication ID returned by get_paper.")
            merge_ids = list(ids)
            if action == "merge":
                # Capture all relationship owners affected by redirected endpoints.
                for publication_id in merge_ids:
                    for row in connection.execute("SELECT publication_id FROM relationships WHERE related_id=?", (publication_id,)):
                        if row[0] not in ids:
                            ids.append(row[0])
            if action == "separate":
                new_id = self._new_publication(connection)
                ids.append(new_id)
            before = self._snapshot(connection, ids)
            connection.execute("INSERT INTO resolutions VALUES (?, ?, ?, ?, ?, NULL)",
                (resolution_id, encode(payload), encode(before), "{}", now()))
            connection.executemany("INSERT INTO resolution_publications VALUES (?, ?)", ((resolution_id, publication_id) for publication_id in ids))
            if action == "merge":
                for publication_id in merge_ids[1:]:
                    connection.execute("UPDATE publications SET merged_into=? WHERE id=?", (ids[0], publication_id))
                    connection.execute("DELETE FROM relationships WHERE (publication_id=? AND related_id=?) OR (publication_id=? AND related_id=?)",
                        (publication_id, ids[0], ids[0], publication_id))
                    connection.execute("UPDATE relationships SET publication_id=? WHERE publication_id=?", (ids[0], publication_id))
                    connection.execute("UPDATE relationships SET related_id=? WHERE related_id=?", (ids[0], publication_id))
                    connection.execute("UPDATE observations SET publication_id=? WHERE publication_id=?", (ids[0], publication_id))
                    # Keep all overrides in history; target overrides win deterministic conflicts.
                    connection.execute("""INSERT OR IGNORE INTO overrides SELECT ?, field, value, resolution_id
                        FROM overrides WHERE publication_id=?""", (ids[0], publication_id))
            elif action == "separate":
                if not set(request.observation_ids) < set(before[ids[0]]["observations"]):
                    raise ValueError("Separate requires a nonempty proper subset of the publication's observations.")
                for observation_id in set(request.observation_ids):
                    connection.execute("UPDATE observations SET publication_id=? WHERE id=?", (new_id, observation_id))
            elif action == "override":
                values = request.metadata.model_dump(exclude_unset=True)
                if not values:
                    raise ValueError("Supply at least one metadata override.")
                if "published_date" in values or "date_precision" in values:
                    if not {"published_date", "date_precision"} <= values.keys():
                        raise ValueError("Override date and precision together.")
                    validate_date(values["published_date"], values["date_precision"])
                for field, value in values.items():
                    connection.execute("INSERT OR REPLACE INTO overrides VALUES (?, ?, ?, ?)",
                        (ids[0], field, encode(value), resolution_id))
            elif action == "relate":
                connection.execute("INSERT INTO relationships VALUES (?, ?, ?, ?, ?)",
                    (uuid4().hex, ids[0], ids[1], request.relationship, resolution_id))
            else:
                restore = json.loads(previous["before_state"])
                for publication_id in restore:
                    connection.execute("DELETE FROM relationships WHERE publication_id=?", (publication_id,))
                for publication_id, state in restore.items():
                    connection.execute("UPDATE publications SET merged_into=? WHERE id=?", (state["merged_into"], publication_id))
                    for observation_id in state["observations"]:
                        connection.execute("UPDATE observations SET publication_id=? WHERE id=?", (publication_id, observation_id))
                    connection.execute("DELETE FROM overrides WHERE publication_id=?", (publication_id,))
                    for override in state["overrides"]:
                        connection.execute("INSERT INTO overrides VALUES (?, ?, ?, ?)", tuple(override[key] for key in ("publication_id", "field", "value", "resolution_id")))
                    for relationship in state["relationships"]:
                        connection.execute("INSERT INTO relationships VALUES (?, ?, ?, ?, ?)", tuple(relationship[key] for key in ("id", "publication_id", "related_id", "kind", "resolution_id")))
                connection.execute("UPDATE resolutions SET undone_by=? WHERE id=?", (resolution_id, request.resolution_id))
            after = self._snapshot(connection, ids)
            connection.execute("UPDATE resolutions SET after_state=? WHERE id=?", (encode(after), resolution_id))
            return {"resolution_id": resolution_id, "publication_ids": ids, "new_publication_id": new_id}
        return self._operation(idempotency_key, {"resolve": payload}, resolve)
