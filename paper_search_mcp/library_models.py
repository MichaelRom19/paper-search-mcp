"""Typed library inputs and human resolution operations."""
from typing import Annotated, Literal

from pydantic import Field, JsonValue

from .provider_models import Author, Model


class MetadataOverride(Model):
    title: str | None = None
    authors: list[Author] | None = None
    venue: str | None = None
    abstract: str | None = None
    published_date: str | None = None
    date_precision: Literal["year", "month", "day", "unknown"] | None = None


class Resolution(Model):
    actor: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class Merge(Resolution):
    action: Literal["merge"] = "merge"
    publication_ids: list[str] = Field(min_length=2)


class Separate(Resolution):
    action: Literal["separate"] = "separate"
    publication_id: str
    observation_ids: list[str] = Field(min_length=1)


class Override(Resolution):
    action: Literal["override"] = "override"
    publication_id: str
    metadata: MetadataOverride


class Relate(Resolution):
    action: Literal["relate"] = "relate"
    publication_id: str
    related_id: str
    relationship: Literal["preprint_of", "version_of", "report_of_same_study"]


class Undo(Resolution):
    action: Literal["undo"] = "undo"
    resolution_id: str


ResolutionRequest = Annotated[Merge | Separate | Override | Relate | Undo, Field(discriminator="action")]


class PublicationSummary(Model):
    publication_id: str
    metadata: dict[str, JsonValue]


class PublicationView(PublicationSummary):
    requested_id: str
    alternatives: dict[str, list[dict[str, JsonValue]]]
    observations: list[dict[str, JsonValue]]
    relationships: list[dict[str, JsonValue]]
    history: list[dict[str, JsonValue]]


class ReviewPage(Model):
    papers: list[PublicationSummary]
    next_after: str | None


class DuplicateSuggestions(Model):
    candidates: list[dict[str, JsonValue]]
    limitation: str


class ResolutionResult(Model):
    resolution_id: str
    publication_ids: list[str]
    new_publication_id: str | None
