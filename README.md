# Paper Search MCP

A Model Context Protocol (MCP) server and CLI for searching academic papers and retrieving available full text. Start with public sources and optionally enable Google Scholar through SerpAPI or Scopus with ScienceDirect retrieval. Download and read support varies by source and access rights.

![PyPI](https://img.shields.io/pypi/v/paper-search-mcp.svg) ![License](https://img.shields.io/badge/license-MIT-blue.svg) ![Python](https://img.shields.io/badge/python-3.14.7-blue.svg)
[![smithery badge](https://smithery.ai/badge/@openags/paper-search-mcp)](https://smithery.ai/server/@openags/paper-search-mcp)

---

## Table of Contents

- [Overview](#overview)
- [Project Principles](#project-principles)
- [Features](#features)
- [Source Strategy](#source-strategy)
- [Platform Capability Matrix](#platform-capability-matrix)
- [Credential & API Key Requirements](#credential--api-key-requirements)
- [Known Upstream Limitations](#known-upstream-limitations)
- [Optional Platform Skeletons](#optional-platform-skeletons)
- [Scopus and Google Scholar API Connectors](#scopus-and-google-scholar-api-connectors)
- [Additional Repository and Discovery Sources](#additional-repository-and-discovery-sources)
- [Sci-Hub Notice](#sci-hub-notice)
- [Installation](#installation)
  - [Claude Code (Skill)](#claude-code-skill--recommended-for-claude-code-users)
  - [Method 1 — Smithery](#method-1--smithery-one-command-recommended-for-claude-desktop)
  - [Method 2 — uvx](#method-2--uvx-no-install)
  - [Method 3 — uv](#method-3--uv-persistent-install)
  - [Method 4 — pip](#method-4--pip-standard-python-install)
  - [Method 5 — npx](#method-5--npx-via-smithery-cli-no-local-python-needed)
  - [Method 6 — Docker](#method-6--docker)
  - [Method 7 — Clone & run from source](#method-7--clone--run-from-source-development--recommended-for-macos-local)
  - [Environment Variables](#environment-variables-env-file)
- [Contributing](#contributing)
  - [Validation](#validation)
- [Demo](#demo)
- [Star History](#star-history)
- [License](#license)
- [TODO](#todo)

---

## Overview

`paper-search-mcp` is a Python-based tool for searching and downloading academic papers from various platforms. It provides tools for searching papers, downloading PDFs, and extracting text, making it ideal for researchers and AI-driven workflows. It can be used as an MCP server (for Claude Desktop and other MCP clients) or as a Claude Code skill with a CLI interface.

## Project Principles

- **Free-First**: Public and open sources are the default roadmap. Paid or restricted sources are not the core direction of this project.
- **Optional Integrations**: The server and CLI work without API keys. Google Scholar requires a SerpAPI key, and Scopus requires an Elsevier key and applicable access rights; both connectors remain disabled until their keys are configured.
- **LLM-Friendly Retrieval**: Search results should be standardized, deduplicated, and as complete as possible for downstream LLM workflows.
- **Source Transparency**: Different sources have different strengths. The MCP should make those tradeoffs explicit instead of pretending every source supports full-text retrieval.

---

## Features

Persistent review searches support Scopus, Google Scholar, OpenAlex, Semantic Scholar bulk/relevance modes, Crossref cursor paging and arXiv offset paging. See the [review workflow](docs/reviews.md) for protocols, budgets and resumable batches.
The [C01–C06 verification report](docs/milestone-c06.md) records milestone checks and remaining validation limitations.

- **Two-Layer Architecture**:
  - **Layer 1 (Unified Tooling)**: `search_papers` for concurrent search and deduplication, and `download_with_fallback` for source-native downloads followed by repository and DOI-based fallbacks.
  - **Layer 2 (Platform Connectors)**: Modular connectors for specific academic platforms (arXiv, PubMed, bioRxiv, Semantic Scholar, etc.) equipped with intelligent DOI extraction via regex text analysis or API fields.
- **Multi-Source Discovery**: Search arXiv, PubMed, bioRxiv, medRxiv, Google Scholar (SerpAPI), Scopus, IACR ePrint Archive, Semantic Scholar, Crossref, OpenAlex, PubMed Central (PMC), CORE, Europe PMC, dblp, OpenAIRE, CiteSeerX, DOAJ, Zenodo, HAL, SSRN, and Unpaywall (DOI lookup). ScienceDirect provides retrieval for Scopus records and is not a separate search source.
- **Standardized Output**: Papers are returned in a consistent dictionary format via the `Paper` class.
- **Configured Sources in Both Interfaces**: MCP `sources="all"` and CLI `--sources all` include Google Scholar and Scopus automatically when configured. Selected sources are searched concurrently; `all` does not defer paid services until public searches fail.
- **Optional API-Key Enhancement**: Sources like Semantic Scholar can work without a key; a key can improve their rate limits. Other sources require credentials as listed below.
- **Discovery + Retrieval Workflow**: Google Scholar and Crossref can be used for discovery and DOI backfilling, while open repositories and publisher links are used for lawful full-text resolution where available.
- **Download Fallback Chain**: `download_with_fallback` tries source-native download (including configured Scopus) → OpenAIRE/CORE/Europe PMC/PMC discovery → Unpaywall DOI resolution → Sci-Hub when `use_scihub=True` (the current default). Set `use_scihub=False` to stop after the open-access fallbacks.
- **MCP Integration**: Compatible with MCP clients for LLM context enhancement.
- **Extensible Design**: Easily add new academic platforms by extending the `academic_platforms` module.

## Source Strategy

The connectors combine public metadata, open repositories, and optional services with different roles:

- **Open metadata backbone**: Crossref, OpenAlex, Semantic Scholar, dblp, CiteSeerX, SSRN, Unpaywall (DOI-centric OA metadata).
- **Discipline-specific sources**: arXiv, PubMed, PubMed Central, Europe PMC, IACR.
- **Open-access full-text sources**: arXiv, PMC, CORE, OpenAIRE, DOAJ, Zenodo, HAL, publisher open-access links.
- **Additional discovery**: Google Scholar via SerpAPI provides result metadata, snippets, DOI clues, and available PDF links. Scopus provides indexed metadata and citation counts; ScienceDirect supplies available article text and PDFs for Scopus records.

OpenAlex, PubMed Central, Europe PMC, CORE, and OpenAIRE are already integrated. Choose an explicit source list to control which providers receive a query; `all` searches every configured source and can consume SerpAPI credits and Elsevier quota.

## Platform Capability Matrix

The authoritative catalog is [`paper_search_mcp/registry.py`](paper_search_mcp/registry.py). See the generated [source capability matrix](docs/sources.md) for every adapter's query modes, pagination, configuration requirements and limitations.

Use MCP `list_sources`, CLI `paper-search list-sources`, or `paper-search sources --details` for the same JSON capability records. These commands make no provider requests and do not initialize providers. `paper-search sources` keeps its legacy `{"sources": [...]}` response listing configured, implemented aggregate sources.

- **Configured** means the current adapter's required environment values are present (or none are required). It does not validate credentials or access rights.
- **Implemented** means an operation exists in the adapter. Stubs and known unusable integrations are explicitly labeled and excluded from aggregate search.
- **Access verified** remains `false`: this catalog does not probe providers or record live verification. Successful mocked tests do not establish access.
- **Available** means configured and implemented. `legacy_search` identifies sources included in aggregate discovery; Sci-Hub remains retrieval-only.

Providers initialize on first use. Configuration is read when the MCP process or CLI command starts; restart the server after changing credentials. Unpaywall requires a contact email and only looks up DOIs. CORE requires a key in the shared registry. BASE and the experimental ChemRxiv adapter are listed as unsupported pending repair. IEEE and ACM remain stubs with or without keys.

Legacy search tools and CLI commands retain their successful response formats and bounded `max_results` behavior. Legacy `total` means **retrieved, deduplicated results**, not the provider's total matches or completeness of the literature. Authors and `extra` retain their legacy string serialization; the new capability API uses JSON arrays and objects.

---

## Credential & API Key Requirements

No credentials are required to start the server or CLI. Credentials marked **required to activate** enable individual connectors; configuring a key does not grant provider subscriptions or institutional access. Set credentials in `~/.config/paper-search-mcp/.env` (preferred) or as shell exports.

| Environment Variable | Provider | Required? | How to obtain |
|---|---|---|---|
| `PAPER_SEARCH_MCP_UNPAYWALL_EMAIL` | Unpaywall | **Required for lookup** | Any valid contact email; see [Unpaywall API](https://unpaywall.org/products/api) |
| `PAPER_SEARCH_MCP_CORE_API_KEY` | CORE | Required for registry availability | Free at [core.ac.uk/services/api](https://core.ac.uk/services/api) |
| `PAPER_SEARCH_MCP_OPENALEX_API_KEY` | OpenAlex | Optional | Bearer authentication; raises the available request-credit budget |
| `PAPER_SEARCH_MCP_SEMANTIC_SCHOLAR_API_KEY` | Semantic Scholar | Optional | Free at [semanticscholar.org](https://www.semanticscholar.org/product/api) — improves rate limits |
| `PAPER_SEARCH_MCP_SERPAPI_API_KEY` | Google Scholar via SerpAPI | **Configuration only; adapter is a stub** | [SerpAPI API key](https://serpapi.com/manage-api-key); requests use your account's search allowance |
| `PAPER_SEARCH_MCP_DOAJ_API_KEY` | DOAJ | Optional | Free at [doaj.org](https://doaj.org/apply-for-api-key/) — raises hourly rate limit |
| `PAPER_SEARCH_MCP_ZENODO_ACCESS_TOKEN` | Zenodo | Optional | Free at [zenodo.org](https://zenodo.org/account/settings/applications/) — required for private records |
| `PAPER_SEARCH_MCP_IEEE_API_KEY` | IEEE Xplore | **Configuration only; adapter is a stub** | Free at [developer.ieee.org](https://developer.ieee.org/) |
| `PAPER_SEARCH_MCP_ACM_API_KEY` | ACM DL | **Configuration only; adapter is a stub** | See [libraries.acm.org/digital-library/acm-open](https://libraries.acm.org/digital-library/acm-open) |
| `PAPER_SEARCH_MCP_SCOPUS_API_KEY` | Scopus / ScienceDirect | **Configuration only; adapter is a stub** | [Elsevier Developer Portal](https://dev.elsevier.com/); COMPLETE-view search and retrieval require applicable access rights |
| `PAPER_SEARCH_MCP_SCOPUS_INST_TOKEN` | Scopus / ScienceDirect | Optional; API key still required | [Elsevier institutional authentication](https://dev.elsevier.com/tecdoc_api_authentication.html); otherwise institutional network/VPN access may be needed |

All variables follow the `PAPER_SEARCH_MCP_<NAME>` prefix scheme. Legacy names without the prefix (e.g. `CORE_API_KEY`, `UNPAYWALL_EMAIL`) are still supported for backward compatibility. A present prefixed value takes precedence, including an empty value; remove it to use the unprefixed alias.

---

## Known Upstream Limitations

Some search failures are caused by external provider instability, not by bugs in this project:

| Source | Symptom | Cause | Workaround |
|---|---|---|---|
| Google Scholar | Source/tool absent, or API authentication/quota error | No key disables the connector; invalid keys and exhausted quotas fail requests | Configure `PAPER_SEARCH_MCP_SERPAPI_API_KEY`, restart the server, and check SerpAPI usage; genuine empty searches return an empty list |
| Scopus / ScienceDirect | Source/tool absent, 401/403, abstract-only read, or unavailable PDF | Missing key, insufficient API access, entitlement, or article availability | Configure the Elsevier key and restart; verify COMPLETE-view search access and institutional network/VPN or token. Scopus coverage does not guarantee ScienceDirect full text |
| Semantic Scholar | 429 rate-limited responses | Anonymous access rate limit | Set `PAPER_SEARCH_MCP_SEMANTIC_SCHOLAR_API_KEY`; if key is rejected (403) connector automatically retries without key |
| CORE | 500 / timeout errors | Unauthenticated rate limiting | Set `PAPER_SEARCH_MCP_CORE_API_KEY` (free); connector retries with exponential backoff and falls back to key-less on 401/403 |
| OpenAIRE | Transient 403 responses | IP-based session rate limiting | Connector retries 3× per profile, escalating: plain session → XML Accept header → raw `requests.get` with Mozilla UA |
| CiteSeerX | 404 via web archive redirect | PSU endpoint intermittently redirects to archive | No workaround; connector returns empty gracefully |
| BASE | Unavailable | Legacy adapter uses the wrong protocol for the search interface | Listed as unsupported until access configuration and the adapter are repaired |
| SSRN | HTTP 403 | Bot-detection (Cloudflare) | No workaround; connector tries two endpoints and returns a clear message on failure |
| PMC / Europe PMC | PDF download ProxyError | Local proxy blocking direct HTTPS PDF download | Disable proxy or use `download_with_fallback` instead |
| Unpaywall | Excluded from aggregate search; dedicated lookup reports missing configuration | Email not configured | Set `PAPER_SEARCH_MCP_UNPAYWALL_EMAIL` in `~/.config/paper-search-mcp/.env` |

## Optional Platform Skeletons

IEEE Xplore and ACM Digital Library are **unimplemented stubs**. Setting `PAPER_SEARCH_MCP_IEEE_API_KEY` or `PAPER_SEARCH_MCP_ACM_API_KEY` changes configuration status only. Neither is included in MCP or CLI `all` searches.

For compatibility, their original MCP search/download/read tools are registered at startup when the corresponding key is present. Every such call raises an explicit unimplemented error. No IEEE/ACM provider requests are made. Use browser search until these adapters are implemented.

## Scopus and Google Scholar API Connectors

Configure either or both keys in `~/.config/paper-search-mcp/.env`, then restart the MCP server. CLI commands read the configuration when they start:

```dotenv
PAPER_SEARCH_MCP_SCOPUS_API_KEY=your_elsevier_key
PAPER_SEARCH_MCP_SERPAPI_API_KEY=your_serpapi_key
# Optional alternative to institutional IP authentication:
# PAPER_SEARCH_MCP_SCOPUS_INST_TOKEN=your_institution_token
```

Unprefixed `SCOPUS_API_KEY`, `SERPAPI_API_KEY`, and `SCOPUS_INST_TOKEN` are also accepted. Each source and its MCP tools are enabled only when its API key is nonempty. Setting the institutional token alone does not enable Scopus.

| Source name | MCP tools registered when configured |
|---|---|
| `google_scholar` | `search_google_scholar` |
| `scopus` | `search_scopus`, `read_scopus_paper`, `download_scopus` |

Run `paper-search sources` to inspect the CLI's available sources. MCP `search_papers(..., sources="all")` and CLI `--sources all` include configured connectors automatically. The unified search defaults to five results **per source**; the dedicated Scopus and Scholar search tools default to ten. Larger limits can require multiple requests. SerpAPI's default caching remains enabled, but an `all` search can still consume account credits. Select explicit sources to control usage.

**Scopus / ScienceDirect:** `search_scopus(query, max_results=10, sort="relevance", field=None, date=None)` supports advanced Scopus queries. `field` optionally wraps the query, for example `field="TITLE-ABS-KEY"`. Sort options include `relevance` (translated to Elsevier's `relevancy`), `-citedby-count`, and `-coverDate`. Dates accept a single year or closed/open ranges such as `2024`, `2020-2024`, `2020-`, and `-2024`. Results use COMPLETE view and paginate in batches of up to 25; an API key alone may not authorize that view. The unified MCP `year` parameter and CLI `--year` option apply only to Semantic Scholar. Scopus-specific `field`, `date`, and `sort` options are exposed by the dedicated MCP tool and Python connector.

`read_scopus_paper(paper_id, save_path="./downloads")` retrieves Scopus metadata and an abstract, then requests native ScienceDirect article text directly by Scopus ID. If metadata retrieval succeeds but article access returns 403/404 or no full-text body, it returns a labeled **ABSTRACT ONLY** result with the reason. Metadata failures, exhausted quotas, malformed responses, and service failures remain errors. Reading returns text without saving a file; `save_path` is retained for interface compatibility.

`download_scopus(paper_id, save_path="./downloads")` requests a PDF through the same Article Retrieval API, validates it before writing, and saves `<numeric Scopus ID>.pdf`. It fails when a PDF is unavailable; it does not save abstract text as a PDF. Both read and download accept numeric IDs with or without `SCOPUS_ID:`. ScienceDirect is the retrieval backend, not a separate search source. Institutional access does not guarantee that every Scopus record has an accessible ScienceDirect article. MCP `download_with_fallback(source="scopus", ...)` can try repository and DOI-based alternatives after the native download fails; supply the paper's DOI or title for those lookups.

```bash
paper-search search 'TITLE("machine learning")' -s scopus -n 30
# Replace this example ID with paper_id from a Scopus search result:
paper-search read scopus 12345678900
paper-search download scopus 12345678900 -o ./downloads
paper-search search "machine learning" -s google_scholar -n 20
```

**Google Scholar:** `search_google_scholar(query, max_results=10)` uses [SerpAPI's Scholar API](https://serpapi.com/google-scholar-api), paginating up to 20 results per request. Results retain the existing `Paper` schema and `google_scholar` source name, with stable IDs, authors, publication year, citation counts, DOI extraction, publisher links, and PDF links when supplied. The `abstract` field contains a search snippet, identified by `abstract_source: snippet` in `extra` (serialized as Python's dictionary string representation). It is not a full paper abstract.

Scholar provides search only: there are no Scholar MCP read/download tools, and the CLI connector retains its unsupported read/download behavior. Follow the returned `pdf_url` or publisher link to obtain full text. Search never downloads linked PDFs. The former Scholar proxy setting, local HTML scraping, CAPTCHA handling, and scraping fallback have been removed. No citation, author, or versions tools are added.

Provider failures appear under `errors.scopus` or `errors.google_scholar` in aggregate search results, while successful sources still contribute papers. A failure on a later page fails that provider's entire search rather than returning partial results as complete. Genuine empty results return an empty list without a provider error. See [Validation](#validation) for mocked checks and credential-gated live scripts.

The Scopus integration is adapted from [mildwall's PR #89](https://github.com/openags/paper-search-mcp/pull/89), reviewed at commit `81e46d7e45c0abaa4a40f515c581baf66ceaac24` (MIT), with direct [Elsevier Article Retrieval](https://dev.elsevier.com/documentation/ArticleRetrievalAPI.wadl) for text and PDFs.

## Additional Repository and Discovery Sources

Four additional repository and discovery connectors are integrated into the MCP server and CLI:

- `zenodo`: Official Zenodo REST API connector (search + record-dependent PDF/read support).
- `hal`: HAL public API connector (search + record-dependent PDF/read support).
- `ssrn`: Discovery-first connector with hardened parser and best-effort download/read when a direct public PDF link is available.
- `unpaywall`: DOI-centric OA metadata source for standalone lookup (`search_unpaywall`) and fallback URL resolution.

SSRN integration remains compliance-first: it only attempts direct public PDF links exposed by SSRN pages. If login/restricted delivery is required, the connector returns a clear message instead of bypassing access controls.

## Sci-Hub Notice

Sci-Hub is a final fallback in the MCP `download_with_fallback` tool after source-native and open-access attempts. Its current parameter default is `use_scihub=True`; pass `use_scihub=False` to disable that fallback. Sci-Hub is not included in `sources="all"` searches.

- Availability is unstable and mirrors change frequently.
- Legal and policy risks vary by jurisdiction.
- Users are responsible for their use of this fallback.
- Open-access and publisher-permitted sources should be tried first whenever possible.

---

## Installation

This README describes the current source tree. To use the Scopus and SerpAPI changes from this checkout, [run from source](#method-7--clone--run-from-source-development--recommended-for-macos-local) or [build Docker locally](#method-6--docker). Package and hosted installs use the version published to their respective registries.

For local installations, configure [credentials in the user `.env` file](#environment-variables-env-file). The local MCP examples below use that file. If you instead add an `env` block to your MCP client configuration, include only the values you want to override: even an empty environment value takes precedence over the file.

---

### Claude Code (Skill) — recommended for Claude Code users

Install as a Claude Code skill instead of an MCP server. This gives Claude automatic access to paper search when you mention finding papers, academic literature, etc. — no MCP configuration needed.

**Prerequisites**: [uv](https://docs.astral.sh/uv/getting-started/installation/) and [Claude Code](https://docs.anthropic.com/en/docs/claude-code/overview).

**Step 1 — Install the CLI:**

```bash
uv tool install paper-search-mcp
```

**Step 2 — Install the skill:**

```bash
mkdir -p ~/.claude/skills/paper-search
curl -fsSL https://raw.githubusercontent.com/openags/paper-search-mcp/main/claude-code/SKILL.md \
  -o ~/.claude/skills/paper-search/SKILL.md
```

**Step 3 (optional) — Configure API keys:**

Create `~/.config/paper-search-mcp/.env` for optional API keys (see [Environment Variables](#environment-variables-env-file)).

**That's it.** Next time you start Claude Code, just ask it to find papers — the skill activates automatically. For example:

- "Find me recent papers on CRISPR base editing"
- "Search arxiv and semantic scholar for transformer attention mechanisms"
- "Download the PDF for arxiv paper 2106.12345"

The skill uses a CLI (`paper-search`) that wraps the same library as the MCP server, outputting JSON for search/download and plain text for read.

---

> **MCP Server Config file locations** (for methods below)
> - **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
> - **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
> - **Linux**: `~/.config/Claude/claude_desktop_config.json`

---

### Method 1 — Smithery (one-command, recommended for Claude Desktop)

```bash
npx -y @smithery/cli install @openags/paper-search-mcp --client claude
```

Smithery automatically writes the correct config block for you. No manual JSON editing needed.

---

### Method 2 — `uvx` (no install)

`uvx` runs the package directly from PyPI without a permanent install. Requires [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
# Install uv (skip if already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh
```

> ⚠️ **macOS note**: `uvx` generated wrapper scripts rely on `realpath`, which is not included in macOS by default. If you see a `realpath: command not found` error, either install GNU coreutils (`brew install coreutils`) or use **Method 3 (`uv run`)** instead — it does not have this limitation.

**Claude Desktop config:**

```json
{
  "mcpServers": {
    "paper-search-mcp": {
      "command": "uvx",
      "args": ["paper-search-mcp"]
    }
  }
}
```

---

### Method 3 — `uv` (persistent install)

```bash
uv tool install paper-search-mcp
```

**Claude Desktop config:**

```json
{
  "mcpServers": {
    "paper-search-mcp": {
      "command": "uv",
      "args": ["tool", "run", "paper-search-mcp"]
    }
  }
}
```

---

### Method 4 — `pip` (standard Python install)

```bash
pip install paper-search-mcp
```

**Claude Desktop config:**

```json
{
  "mcpServers": {
    "paper-search-mcp": {
      "command": "python",
      "args": ["-m", "paper_search_mcp.server"]
    }
  }
}
```

> If `python` is not on your PATH, replace it with the full path (e.g. `/usr/bin/python3` or `C:\Python311\python.exe`). Run `which python3` / `where python` to find it.

---

### Method 5 — `npx` (via Smithery CLI, no local Python needed)

```bash
npx -y @smithery/cli run @openags/paper-search-mcp
```

**Claude Desktop config:**

```json
{
  "mcpServers": {
    "paper-search-mcp": {
      "command": "npx",
      "args": ["-y", "@smithery/cli", "run", "@openags/paper-search-mcp"],
      "env": {
        "PAPER_SEARCH_MCP_UNPAYWALL_EMAIL": "your@email.com",
        "PAPER_SEARCH_MCP_CORE_API_KEY": "",
        "PAPER_SEARCH_MCP_SEMANTIC_SCHOLAR_API_KEY": ""
      }
    }
  }
}
```

---

### Method 6 — Docker

Create the [user `.env` file](#environment-variables-env-file) first, then pass it into the container:

```bash
docker build -t paper-search-mcp .
docker run --rm -i \
  --env-file "$HOME/.config/paper-search-mcp/.env" \
  paper-search-mcp
```

**Claude Desktop config** (replace the file path with your absolute host path):

```json
{
  "mcpServers": {
    "paper-search-mcp": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "--env-file", "/absolute/path/to/.config/paper-search-mcp/.env",
        "paper-search-mcp"
      ]
    }
  }
}
```

Docker does not automatically forward the host's environment or load the host's user config file. Use `--env-file` as above, or explicit `-e` arguments. The image includes both `paper-search-mcp` and `paper-search`; append `paper-search sources` to the `docker run` command to list configured CLI sources.

---

### Method 7 — Clone & run from source (development / recommended for macOS local)

This is the most reliable method on macOS — no wrapper scripts, no `realpath` issues.

```bash
# 1. Install uv (skip if already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Clone repo
git clone https://github.com/openags/paper-search-mcp.git
cd paper-search-mcp

# 3. Install the pinned runtime and locked dependencies
uv python install 3.14.7
uv sync --locked
uv run --locked -m paper_search_mcp.server
```

**Claude Desktop config** (replace the directory path with your actual clone location):

```json
{
  "mcpServers": {
    "paper-search-mcp": {
      "command": "uv",
      "args": [
        "run",
        "--directory", "/path/to/paper-search-mcp",
        "-m", "paper_search_mcp.server"
      ]
    }
  }
}
```

In an existing checkout, skip cloning and use its directory. To run the CLI against that checkout, prefix the earlier CLI examples with `uv run --locked`, for example `uv run --locked paper-search sources`.

> This implementation series targets Python **3.14.7**, pinned in `.python-version`, CI and Docker. Packaging accepts 3.14.7 through the 3.14 patch series; Python 3.15 and prereleases are excluded. `uv.lock` pins stable runtime and development dependencies. Use `--locked` to detect drift; dependency upgrades belong in an intentional baseline update. The official MCP SDK 2.2 uses `MCPServer`; the unused standalone `fastmcp` dependency has been removed.

For active development, optionally install an editable copy:

```bash
uv sync --locked --extra dev
```

---

### Environment Variables (`.env` file)

The server and CLI load `~/.config/paper-search-mcp/.env` on startup. Create or edit that file with the settings below, or use this checkout's [.env.example](.env.example) as a template:

```bash
mkdir -p ~/.config/paper-search-mcp
$EDITOR ~/.config/paper-search-mcp/.env
```

```dotenv
PAPER_SEARCH_MCP_UNPAYWALL_EMAIL=your@email.com
PAPER_SEARCH_MCP_CORE_API_KEY=
PAPER_SEARCH_MCP_SEMANTIC_SCHOLAR_API_KEY=
PAPER_SEARCH_MCP_DOAJ_API_KEY=
PAPER_SEARCH_MCP_ZENODO_ACCESS_TOKEN=
PAPER_SEARCH_MCP_SERPAPI_API_KEY=
PAPER_SEARCH_MCP_SCOPUS_API_KEY=
PAPER_SEARCH_MCP_SCOPUS_INST_TOKEN=
PAPER_SEARCH_MCP_IEEE_API_KEY=
PAPER_SEARCH_MCP_ACM_API_KEY=
```

Fill only the values you need. Leave the SerpAPI or Scopus key empty to disable that connector. Restart the MCP server after changing credentials; the next CLI invocation picks up the changes.

To use a custom file: `export PAPER_SEARCH_MCP_ENV_FILE=/absolute/path/to/.env`. A project-local `.env` is not loaded automatically.

Existing process environment values take precedence over the file, including empty values. Remove empty keys from your MCP client's `env` block if you want the file to supply them. Legacy names without the `PAPER_SEARCH_MCP_` prefix (including `SERPAPI_API_KEY`, `SCOPUS_API_KEY`, and `SCOPUS_INST_TOKEN`) are supported, but a present prefixed value takes precedence, even if empty; remove it to use the alias.

---

## Contributing

We welcome contributions! Here's how to get started:

1. **Fork the Repository**:
   Click "Fork" on GitHub.

2. **Clone and Set Up**:

   ```bash
   git clone https://github.com/yourusername/paper-search-mcp.git
   cd paper-search-mcp
   uv python install 3.14.7
   uv sync --locked --extra dev
   ```

3. **Make Changes**:

   - Add new platforms in `academic_platforms/`.
   - Update tests in `tests/`.

4. **Submit a Pull Request**:
   Push changes and create a PR on GitHub.

### Validation

Run all offline checks and build the package using locked build dependencies:

```bash
uv sync --locked --extra dev
uv run --locked pytest -q --tb=short
uv build --no-build-isolation
```

Ordinary test collection is offline. The original 156 deterministic regressions remain, alongside registry/CLI parity tests covering absent, partial and complete credentials, lazy initialization, truthful stubs, and an actual MCP stdio initialization/list/call session with an injected provider. Test configuration isolates local credentials and blocks accidental network access. Pull requests and default-branch pushes run these checks, wheel installation/entry-point checks, and a Docker smoke test. Tagged releases retain packaging verification before publication.

Live suites are under `tests/live/` and are excluded unless explicitly selected:

```bash
uv run --locked pytest --live tests/live/test_arxiv.py
uv run --locked python tests/live/functional_test.py --live
uv run --locked python tests/live/e2e_test.py --live
```

Live checks may consume quota and require provider-specific access; Scholar and Scopus script sections skip without credentials. They are not required for ordinary CI and have not been used to mark catalog access as verified. Saved protocols and resumable Scopus/Scholar searches are documented in [the review guide](docs/reviews.md).

Regenerate the documented capability matrix after editing the registry:

```bash
uv run --locked python scripts/update_source_catalog.py
```

---

## Demo

<img src="docs\images\demo.png" alt="Demo" width="800">

## TODO

### Planned Academic Platforms

- [√] arXiv
- [√] PubMed
- [√] bioRxiv
- [√] medRxiv
- [x] Google Scholar via SerpAPI (search only; API key required)
- [√] IACR ePrint Archive
- [√] Semantic Scholar
- [√] Crossref
- [√] PubMed Central (PMC)
- [√] CORE
- [√] Europe PMC
- [√] Sci-Hub warning and enablement docs

### Development Tasks
- [√] Fix Async search bugs and ensure reliable fast MCP events
- [√] End-to-End full pipeline testing script (search, parse, download)
- [√] Establish two-layer federated architecture (Layer 1 tool: `search_papers`)
- [√] Ensure pervasive DOI extraction across metadata fields & abstract fallbacks
- [ ] Citation graph & Paper relation context feature
- [√] Expand full-stack OpenAlex provider

### Priority Free and Open Sources

- [√] PubMed Central (PMC)
- [√] CORE
- [√] OpenAlex
- [√] Europe PMC
- [√] OpenAIRE
- [√] dblp
- [√] CiteSeerX
- [√] DOAJ
- [ ] BASE (legacy adapter requires repair)
- [√] Zenodo
- [√] HAL
- [√] SSRN (discovery + best-effort full-text)
- [√] Unpaywall (standalone DOI search source)

### Optional and Non-Core Integrations

- [ ] ResearchGate
- [ ] JSTOR
- [x] ScienceDirect full text / PDFs via Scopus (entitlement-dependent)
- [ ] Springer Link
- [ ] IEEE Xplore (unimplemented stub)
- [ ] ACM Digital Library (unimplemented stub)
- [ ] Web of Science
- [x] Scopus search, abstract retrieval, and ScienceDirect full text / PDFs

---

## Star History

[![Star History Chart](https://star-history.dera.page/svg?repos=openags/paper-search-mcp&type=Date)](https://star-history.dera.page/#openags/paper-search-mcp&Date)

---

## License

This project is licensed under the MIT License. See the LICENSE file for details.

---

Happy researching with `paper-search-mcp`! If you encounter issues, open a GitHub issue.

### Typed provider pages (C02)

The six review adapters' `search_page` methods accept a
JSON-serializable `SavedQuery`, optional provider continuation, and a shared
`RequestAllowance`. Their legacy `search(max_results=...)` methods collect these
pages and retain the existing `Paper` response format. Scopus review runs use saved cursors; the legacy bounded collector retains offsets. Adapters outside Scopus, Scholar, OpenAlex, Semantic Scholar, Crossref and arXiv explicitly report
that the page operation is unsupported until their migration packets.

Pages contain structured authors and metadata, text/date precision, reported
provider totals, rejected records with sanitized originals and reasons, request
usage, continuation, and a typed outcome. A received page is never truncated to
the requested size. Duplicate-only pages do not imply exhaustion. Malformed
records remain in `rejected`; malformed envelopes and invalid pagination produce
visible failures. Legacy collectors raise on failures or rejected records.

The shared HTTPX transport reserves each attempt before sending it, allows at most
three retries, and counts retries against the same allowance as follow-up calls.
The default page allowance is four attempts with no retry sleeping: a transient failure
returns `waiting`, its earliest retry time, and the unchanged continuation. A
caller may supply a bounded waiting allowance. arXiv additionally enforces its three-second request spacing. Authentication and explicit quota
exhaustion are terminal. Credentials are bound to the configured origin;
redirects are not followed. Error messages omit upstream bodies and request URLs.
TLS verification bypasses have been removed and provider print diagnostics go to
stderr. The older adapters' complete request/parser migrations remain assigned
to their later packets. HTTPX timeout and redirect behavior follows its
[official documentation](https://www.python-httpx.org/advanced/timeouts/).

The C02 audit also covers SerpAPI error envelopes returned with HTTP 200,
Scopus month-precision dates, shared Scopus reading/download request handling,
and MCP read diagnostics on stderr. Legacy PDF redirects are handled explicitly
with credentials retained only on the Elsevier origin.

### Persistent library (C03)

C03 adds SQLite storage, provenance-preserving identifier resolution, duplicate
suggestions, reversible manual merge/separate/override operations, and explicit
version/study relationships. `get_paper`, `query_review`, `possible_duplicates`
and `resolve_publications` have matching CLI commands. Configure the storage
location with `PAPER_SEARCH_MCP_DATA_DIR`.

See [the library guide](docs/library.md) for defaults, migration backups,
idempotency, metadata selection, request examples and validation. Saved protocols and resumable Scopus/Scholar searches are available through
`create_review`, `update_review`, `get_review`, `list_reviews`, `start_search`,
`advance_run` and `get_run`, with matching CLI commands. See [the C04 review
guide](docs/reviews.md) for protocol JSON, budgets, validation and crash recovery.
Legacy searches continue to be stateless.
