#!/usr/bin/env python3
"""CLI interface for paper-search — search, download, and read academic papers."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any, Dict, List

from .registry import available_sources, list_sources, provider


SEARCHERS: dict[str, Any] = {}


def _init_searchers() -> None:
    """Create lazy handles for configured, implemented registry entries."""
    if not SEARCHERS:
        SEARCHERS.update((name, provider(name)) for name in available_sources())


def _parse_sources(sources: str) -> List[str]:
    if not sources or sources.strip().lower() == "all":
        return list(SEARCHERS)
    normalized = [p.strip().lower() for p in sources.split(",") if p.strip()]
    return [s for s in normalized if s in SEARCHERS]


def _paper_unique_key(paper: Dict[str, Any]) -> str:
    doi = (paper.get("doi") or "").strip().lower()
    if doi:
        return f"doi:{doi}"
    title = (paper.get("title") or "").strip().lower()
    authors = (paper.get("authors") or "").strip().lower()
    if title:
        return f"title:{title}|authors:{authors}"
    return f"id:{(paper.get('paper_id') or '').strip().lower()}"


def _dedupe(papers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: set[str] = set()
    out: list[Dict[str, Any]] = []
    for p in papers:
        k = _paper_unique_key(p)
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# Async helpers
# ---------------------------------------------------------------------------

async def _async_search(searcher: Any, query: str, max_results: int, **kwargs) -> List[Dict]:
    if kwargs:
        papers = await asyncio.to_thread(searcher.search, query, max_results=max_results, **kwargs)
    else:
        papers = await asyncio.to_thread(searcher.search, query, max_results=max_results)
    return [p.to_dict() for p in papers]


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

async def cmd_search(args: argparse.Namespace) -> int:
    _init_searchers()
    selected = _parse_sources(args.sources)
    if not selected:
        print(json.dumps({"error": "No valid sources selected", "available": sorted(SEARCHERS.keys())}))
        return 1

    tasks = {}
    for src in selected:
        searcher = SEARCHERS[src]
        extra = {}
        if src == "semantic" and args.year:
            extra["year"] = args.year
        tasks[src] = _async_search(searcher, args.query, args.max_results, **extra)

    names = list(tasks.keys())
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)

    merged: List[Dict[str, Any]] = []
    errors: Dict[str, str] = {}
    source_counts: Dict[str, int] = {}

    for name, result in zip(names, results):
        if isinstance(result, Exception):
            errors[name] = str(result)
            source_counts[name] = 0
        else:
            source_counts[name] = len(result)
            for p in result:
                if not p.get("source"):
                    p["source"] = name
                merged.append(p)

    deduped = _dedupe(merged)

    output = {
        "query": args.query,
        "sources_used": names,
        "source_results": source_counts,
        "errors": errors,
        "total": len(deduped),
        "papers": deduped,
    }
    print(json.dumps(output, indent=2, default=str))
    return 0


async def cmd_download(args: argparse.Namespace) -> int:
    _init_searchers()
    source = args.source.strip().lower()

    if source not in SEARCHERS:
        print(json.dumps({"error": f"Unknown source: {source}", "available": sorted(SEARCHERS.keys())}))
        return 1

    searcher = SEARCHERS[source]
    try:
        result = await asyncio.to_thread(searcher.download_pdf, args.paper_id, args.save_path)
        print(json.dumps({"status": "ok", "path": result}))
        return 0
    except Exception as e:
        print(json.dumps({"status": "error", "message": str(e)}))
        return 1


async def cmd_read(args: argparse.Namespace) -> int:
    _init_searchers()
    source = args.source.strip().lower()

    if source not in SEARCHERS:
        print(json.dumps({"error": f"Unknown source: {source}", "available": sorted(SEARCHERS.keys())}))
        return 1

    searcher = SEARCHERS[source]
    try:
        text = await asyncio.to_thread(searcher.read_paper, args.paper_id, args.save_path)
        print(text)
        return 0
    except Exception as e:
        print(json.dumps({"status": "error", "message": str(e)}))
        return 1


async def cmd_sources(args: argparse.Namespace) -> int:
    if args.details:
        print(json.dumps([source.model_dump() for source in list_sources()], indent=2))
    else:
        print(json.dumps({"sources": sorted(available_sources())}, indent=2))
    return 0


async def cmd_review(args: argparse.Namespace) -> int:
    from pathlib import Path
    from .reviews import Reviews
    from .review_models import ReviewProtocol, SearchRequest
    try:
        service = Reviews()
        command = args.command.replace("-", "_")
        if command in {"create_review", "update_review", "start_search"}:
            model = SearchRequest if command == "start_search" else ReviewProtocol
            request = model.model_validate_json(Path(args.request_file).read_text())
            positional = [args.review_id, request] if command == "update_review" else [request]
            result = getattr(service, command)(*positional, idempotency_key=args.idempotency_key)
        elif command == "advance_run":
            result = service.advance_run(args.run_id, idempotency_key=args.idempotency_key, max_requests=args.max_requests)
        elif command == "list_reviews":
            result = service.list_reviews(limit=args.limit, after=args.after)
        else:
            result = getattr(service, command)(args.review_id if command == "get_review" else args.run_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1


async def cmd_library(args: argparse.Namespace) -> int:
    from pathlib import Path
    from pydantic import TypeAdapter
    from .library import Library
    from .library_models import ResolutionRequest

    try:
        library = Library()
        if args.command == "resolve-publications":
            request = TypeAdapter(ResolutionRequest).validate_json(Path(args.request_file).read_text())
            result = library.resolve_publications(request, idempotency_key=args.idempotency_key)
        elif args.command == "get-paper":
            result = library.get_paper(args.publication_id)
        elif args.command == "possible-duplicates":
            result = library.possible_duplicates(args.publication_id, limit=args.limit)
        else:
            result = library.query_review(args.review_id, limit=args.limit, after=args.after)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="paper-search",
        description="Search, download, and read academic papers from 20+ sources.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # search
    p_search = sub.add_parser("search", help="Search for papers across academic platforms")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("-n", "--max-results", type=int, default=5, help="Max results per source (default: 5)")
    p_search.add_argument("-s", "--sources", default="all",
                          help="Comma-separated sources or 'all' (default: all)")
    p_search.add_argument("-y", "--year", default=None,
                          help="Year filter for Semantic Scholar (e.g. '2020', '2018-2022')")

    # download
    p_dl = sub.add_parser("download", help="Download a paper PDF")
    p_dl.add_argument("source", help="Source platform (e.g. arxiv, semantic)")
    p_dl.add_argument("paper_id", help="Paper identifier")
    p_dl.add_argument("-o", "--save-path", default="./downloads", help="Save directory (default: ./downloads)")

    # read
    p_read = sub.add_parser("read", help="Download and extract text from a paper")
    p_read.add_argument("source", help="Source platform (e.g. arxiv, semantic)")
    p_read.add_argument("paper_id", help="Paper identifier")
    p_read.add_argument("-o", "--save-path", default="./downloads", help="Save directory (default: ./downloads)")

    # sources
    p_sources = sub.add_parser("sources", help="List available sources")
    p_sources.add_argument("--details", action="store_true", help="Include every source and its capabilities")
    sub.add_parser("list-sources", help="List all source capabilities").set_defaults(details=True)

    for command in ("get-paper", "possible-duplicates"):
        subparser = sub.add_parser(command, help="Inspect the persistent library")
        subparser.add_argument("publication_id")
        if command == "possible-duplicates":
            subparser.add_argument("--limit", type=int, default=20)
    query = sub.add_parser("query-review", help="List persisted review publications")
    query.add_argument("review_id")
    query.add_argument("--limit", type=int, default=20)
    query.add_argument("--after")
    resolution = sub.add_parser("resolve-publications", help="Apply a typed manual resolution from a JSON file")
    resolution.add_argument("request_file")
    resolution.add_argument("--idempotency-key", required=True)
    for command in ("create-review", "update-review", "start-search", "advance-run", "get-run", "get-review", "list-reviews"):
        item = sub.add_parser(command, help="Saved review protocols and resumable searches")
        if command in {"update-review", "get-review"}:
            item.add_argument("review_id")
        if command in {"advance-run", "get-run"}:
            item.add_argument("run_id")
        if command in {"create-review", "update-review", "start-search"}:
            item.add_argument("request_file")
        if command in {"create-review", "update-review", "start-search", "advance-run"}:
            item.add_argument("--idempotency-key", required=True)
        if command == "advance-run":
            item.add_argument("--max-requests", type=int, default=4)
        if command == "list-reviews":
            item.add_argument("--limit", type=int, default=20)
            item.add_argument("--after")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    dispatch = {
        "get-paper": cmd_library,
        "query-review": cmd_library,
        "possible-duplicates": cmd_library,
        "resolve-publications": cmd_library,
        "search": cmd_search,
        "download": cmd_download,
        "read": cmd_read,
        "sources": cmd_sources,
        "list-sources": cmd_sources,
    }

    exit_code = asyncio.run(dispatch.get(args.command, cmd_review)(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
