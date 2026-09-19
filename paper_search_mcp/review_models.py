"""Public protocol and saved-search inputs."""
from pydantic import Field, JsonValue, model_validator

from .provider_models import Model, ProviderError, ReportedTotal, SavedQuery


class ReviewProtocol(Model):
    question: str = Field(min_length=1)
    scope: str = ""
    eligibility_criteria: list[str] = Field(default_factory=list)
    selected_sources: list[str] = Field(min_length=1)
    query_rationale: str = ""
    seed_papers: list[str] = Field(default_factory=list)
    queries: list[SavedQuery] = Field(default_factory=list)

    @model_validator(mode="after")
    def sources_match(self):
        if len(set(self.selected_sources)) != len(self.selected_sources):
            raise ValueError("Selected sources must be distinct.")
        if any(q.source not in self.selected_sources for q in self.queries):
            raise ValueError("Queries must belong to selected sources.")
        if len({q.source for q in self.queries}) != len(self.queries):
            raise ValueError("Use one native query per source per run.")
        return self


class SearchRequest(Model):
    review_id: str
    request_budget: int = Field(ge=1)
    provider_budgets: dict[str, int] = Field(default_factory=dict)
    queries: list[SavedQuery] | None = None

    @model_validator(mode="after")
    def positive_limits(self):
        if any(type(value) is not int or value < 1 for value in self.provider_budgets.values()):
            raise ValueError("Provider budgets must be positive integers.")
        return self


class ValidationResult(Model):
    source: str
    valid: bool
    message: str | None = None


class ReviewCreated(Model):
    review_id: str
    validation: list[ValidationResult]


class ReviewUpdated(ReviewCreated):
    revision: int


class SearchStarted(Model):
    run_id: str
    validation: list[ValidationResult]


# JSON-native views also accommodate protocols created by the C03 storage primitive.
class ReviewSummary(Model):
    review_id: str
    protocol: dict[str, JsonValue]
    created_at: str


class ReviewView(ReviewSummary):
    revision: int
    revisions: list[dict[str, JsonValue]]


class ReviewList(Model):
    reviews: list[ReviewSummary]
    next_after: str | None


class SourceProgress(Model):
    status: str
    validation: list[str]
    continuation: dict[str, JsonValue] | None
    requests_used: int
    request_usage: dict[str, JsonValue] = Field(default_factory=dict)
    received: int
    rejected: int
    linked_duplicates: int
    total: ReportedTotal
    error: ProviderError | None
    warnings: list[str]
    page_hash: str | None


class RunState(Model):
    requests_used: int
    sources: dict[str, SourceProgress]
    next_source: int


class RunView(Model):
    id: str
    review_id: str
    specification: dict[str, JsonValue]
    state: RunState
    created_at: str
    unique_publications: int
    remaining_requests: int
    status: str
    limitation: str
    interrupted: bool = False
