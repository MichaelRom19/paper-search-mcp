FROM python:3.14.7-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY paper_search_mcp/ paper_search_mcp/
RUN uv sync --locked --extra dev --no-editable --no-install-project \
    && uv build --wheel --no-build-isolation \
    && uv sync --locked --no-dev --no-editable --no-install-project \
    && uv pip install --no-deps dist/*.whl

FROM python:3.14.7-slim
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
# Pass optional credentials with --env-file or -e. Empty defaults would mask aliases.
CMD ["paper-search-mcp"]
