"""Local SQLite lifecycle. Each operation owns its connection and transaction."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import sys
from uuid import uuid4


MIGRATIONS = ((1, (
    "CREATE TABLE reviews (id TEXT PRIMARY KEY, protocol TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE TABLE publications (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, merged_into TEXT REFERENCES publications)",
    """CREATE TABLE runs (id TEXT PRIMARY KEY, review_id TEXT NOT NULL REFERENCES reviews,
        specification TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE observations (id TEXT PRIMARY KEY, publication_id TEXT NOT NULL REFERENCES publications,
        source TEXT NOT NULL, original_id TEXT NOT NULL, retrieved_at TEXT NOT NULL, metadata TEXT NOT NULL,
        title_key TEXT NOT NULL, author_key TEXT NOT NULL, year TEXT NOT NULL)""",
    "CREATE INDEX observation_publication ON observations(publication_id)",
    "CREATE INDEX observation_title ON observations(title_key)",
    "CREATE INDEX observation_author_year ON observations(author_key, year)",
    """CREATE TABLE identifiers (observation_id TEXT NOT NULL REFERENCES observations,
        namespace TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(observation_id, namespace, value))""",
    "CREATE INDEX identifier_lookup ON identifiers(namespace, value)",
    """CREATE TABLE pages (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs,
        payload TEXT NOT NULL, received_at TEXT NOT NULL)""",
    """CREATE TABLE query_hits (review_id TEXT NOT NULL REFERENCES reviews,
        observation_id TEXT NOT NULL REFERENCES observations, run_id TEXT REFERENCES runs,
        page_id TEXT REFERENCES pages, position INTEGER NOT NULL)""",
    "CREATE INDEX hit_review ON query_hits(review_id, observation_id)",
    """CREATE TABLE operations (key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, outcome TEXT NOT NULL)""",
    """CREATE TABLE resolutions (id TEXT PRIMARY KEY, request TEXT NOT NULL, before_state TEXT NOT NULL,
        after_state TEXT NOT NULL, created_at TEXT NOT NULL, undone_by TEXT REFERENCES resolutions)""",
    """CREATE TABLE resolution_publications (resolution_id TEXT NOT NULL REFERENCES resolutions,
        publication_id TEXT NOT NULL REFERENCES publications, PRIMARY KEY(resolution_id, publication_id))""",
    "CREATE INDEX resolution_publication ON resolution_publications(publication_id)",
    """CREATE TABLE overrides (publication_id TEXT NOT NULL REFERENCES publications, field TEXT NOT NULL,
        value TEXT NOT NULL, resolution_id TEXT NOT NULL REFERENCES resolutions, PRIMARY KEY(publication_id, field))""",
    """CREATE TABLE relationships (id TEXT PRIMARY KEY, publication_id TEXT NOT NULL REFERENCES publications,
        related_id TEXT NOT NULL REFERENCES publications, kind TEXT NOT NULL,
        resolution_id TEXT NOT NULL REFERENCES resolutions, CHECK(publication_id != related_id))""",
)), (2, (
    """CREATE TABLE protocol_revisions (review_id TEXT NOT NULL REFERENCES reviews,
        revision INTEGER NOT NULL, protocol TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(review_id, revision))""",
    "INSERT INTO protocol_revisions SELECT id, 1, protocol, created_at FROM reviews",
    """CREATE TABLE advances (key TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs,
        fingerprint TEXT NOT NULL, outcome TEXT)""",
    """CREATE TABLE received_pages (id TEXT PRIMARY KEY, advance_key TEXT NOT NULL REFERENCES advances,
        source TEXT NOT NULL, payload TEXT NOT NULL, applied INTEGER NOT NULL DEFAULT 0)""",
)),)


def data_directory() -> Path:
    if override := os.environ.get("PAPER_SEARCH_MCP_DATA_DIR"):
        return Path(override).expanduser()
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "paper-search-mcp"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/paper-search-mcp"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "paper-search-mcp"


def now():
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, directory: Path | str | None = None):
        self.path = Path(directory) / "library.sqlite3" if directory is not None else data_directory() / "library.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def transaction(self, *, write=False):
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self):
        with closing(self.connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
        with self.transaction(write=True) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > MIGRATIONS[-1][0]:
                raise RuntimeError("Library schema is newer than this application.")
            pending = [(number, statements) for number, statements in MIGRATIONS if number > version]
            if not pending:
                return
            # The writer reservation excludes competing migrations. A separate reader
            # backs up the committed state, including committed WAL pages.
            backup_path = self.path.with_name(f"library.before-v{version + 1}.{uuid4().hex}.sqlite3")
            source = self.connect()
            destination = sqlite3.connect(backup_path)
            try:
                source.backup(destination)
            finally:
                source.close()
                destination.close()
            for number, statements in pending:
                for statement in statements:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version={number}")


@contextmanager
def run_lock(path):
    """OS lock releases on process death; never hold a SQLite transaction over I/O."""
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.write(b"\0")
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
