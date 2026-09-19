"""JSON-native contracts for synchronous, resumable provider operations."""
from dataclasses import asdict
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from .paper import Paper


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Author(Model):
    name: str
    given: str | None = None
    family: str | None = None
    orcid: str | None = None


class Metadata(Model):
    paper_id: str
    source: str
    title: str
    authors: list[Author] = Field(default_factory=list)
    abstract: str = ""
    text_kind: Literal["snippet", "abstract", "full_text"] = "abstract"
    doi: str = ""
    identifiers: dict[str, str] = Field(default_factory=dict)
    venue: str = ""
    publication_type: str = ""
    version: str = ""
    published_date: str | None = None
    date_precision: Literal["year", "month", "day", "unknown"] = "unknown"
    url: str = ""
    pdf_url: str = ""
    extra: dict[str, JsonValue] = Field(default_factory=dict)
    updated_date: str | None = None
    categories: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    citations: int = 0
    references: list[str] = Field(default_factory=list)

    @classmethod
    def from_paper(cls, paper: Paper):
        values = asdict(paper)
        for key in ("published_date", "updated_date"):
            values[key] = values[key].isoformat() if values[key] else None
        snippet = paper.extra.get("abstract_source") == "snippet"
        return cls(paper_id=paper.paper_id, source=paper.source, title=paper.title,
                   authors=[Author(name=name) for name in paper.authors], abstract=paper.abstract,
                   text_kind="snippet" if snippet else "abstract", doi=paper.doi,
                   published_date=str(paper.published_date.year) if snippet and paper.published_date else paper.published_date.date().isoformat() if paper.published_date else None,
                   date_precision="year" if snippet and paper.published_date else "day" if paper.published_date else "unknown",
                   url=paper.url, pdf_url=paper.pdf_url, extra=paper.extra,
                   updated_date=values["updated_date"], categories=paper.categories,
                   keywords=paper.keywords, citations=paper.citations, references=paper.references)

    def to_paper(self):
        values = self.model_dump(exclude={"text_kind", "date_precision", "identifiers", "venue", "publication_type", "version"})
        values["authors"] = [author.name for author in self.authors]
        if values["published_date"] and len(values["published_date"]) in (4, 7):
            values["published_date"] += "-01-01" if len(values["published_date"]) == 4 else "-01"
        for key in ("published_date", "updated_date"):
            values[key] = datetime.fromisoformat(values[key]) if values[key] else None
        return Paper(**values)


class SavedQuery(Model):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source: str
    query: str = Field(min_length=1)
    mode: str = "native_query"
    page_size: int = Field(default=20, ge=1, le=100)
    filters: dict[str, JsonValue] = Field(default_factory=dict)
    sort: str | None = None


class ReportedTotal(Model):
    value: int | None = Field(default=None, ge=0)
    precision: Literal["exact", "estimated", "unknown"] = "unknown"


class ProviderError(Model):
    kind: Literal["authentication", "quota", "rate_limit", "service", "malformed_response", "budget", "unsafe_origin", "nonadvancing", "unsupported", "expired_cursor", "provider_cap"]
    message: str
    status_code: int | None = None
    retry_at: datetime | None = None


class RejectedRecord(Model):
    raw: JsonValue
    reason: str


class ProviderPage(Model):
    pagination_validated: bool = False
    records: list[Metadata] = Field(default_factory=list)
    rejected: list[RejectedRecord] = Field(default_factory=list)
    continuation: dict[str, JsonValue] | None = None
    total: ReportedTotal = Field(default_factory=ReportedTotal)
    warnings: list[str] = Field(default_factory=list)
    requests_used: int = 0
    request_usage: dict[str, JsonValue] = Field(default_factory=dict)
    state: Literal["ready", "exhausted", "waiting", "failed", "budget", "provider_cap"] = "ready"
    error: ProviderError | None = None
