"""Regenerate the static matrix from adapter definitions, without probing access."""

from pathlib import Path

from paper_search_mcp.registry import SOURCES


def render_catalog() -> str:
    lines = [
        "# Source capabilities", "",
        "Generated from `paper_search_mcp/registry.py` with `uv run --locked python scripts/update_source_catalog.py`.", "",
        "Capabilities describe existing adapters. Access remains unverified for every source. Scopus, Scholar, OpenAlex, Semantic Scholar, Crossref and arXiv expose resumable review runs; other adapters retain their stated limitations.", "",
        "Configuration names below use the `PAPER_SEARCH_MCP_` prefix; unprefixed aliases remain supported. Empty prefixed values override aliases. No required values means the current adapter needs no configuration, not that upstream access is guaranteed.", "",
        "| Source | Implementation | Query modes | Pagination / typed pages | Lookup / read / download | Required / optional configuration | Limitations |",
        "|---|---|---|---|---|---|---|",
    ]
    for source in SOURCES.values():
        capabilities = " / ".join("yes" if value else "no" for value in (source.lookup, source.read, source.download))
        configuration = f"{', '.join(source.required) or 'none'} / {', '.join(source.optional) or 'none'}"
        lines.append(f"| {source.name} | {source.implementation} | {', '.join(source.query_modes) or 'none'} | {source.pagination} / {"yes" if source.provider_pages else "no"} | {capabilities} | {configuration} | {' '.join(source.limitations)} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    Path(__file__).resolve().parents[1].joinpath("docs/sources.md").write_text(render_catalog())
