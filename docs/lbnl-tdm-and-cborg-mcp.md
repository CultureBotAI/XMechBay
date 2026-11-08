# LBNL Library Services, Publisher TDM APIs, and CBORG MCP Setup

This document records the reusable configuration, permission syntax, and license boundaries for integrating CBORG MCP servers (`tavily`, `context7`, `valency`) and LBNL Library / publisher Text and Data Mining (TDM) APIs across Codex CLI, Claude Code, and Antigravity (`agy`).

## 1. CBORG MCP Servers (`tavily`, `context7`, `valency`)

CBORG provides streamable HTTP MCP endpoints at `https://api.cborg.lbl.gov/mcp/<server>` authenticated via `CBORG_API_KEY`:

- **`context7`** (2 read-only tools: `context7-resolve-library-id`, `context7-query-docs`)
- **`tavily`** (5 read-only tools: `tavily-tavily_search`, `tavily-tavily_extract`, `tavily-tavily_crawl`, `tavily-tavily_map`, `tavily-tavily_research`)
- **`valency`** (35 read-only scholarly corpus/citation tools + 1 outbound feedback tool `valency-submit_feedback`)

### Helper Scripts

- [`scripts/adhoc/configure_cborg_tavily.sh`](../scripts/adhoc/configure_cborg_tavily.sh): Adds `[mcp_servers.tavily]`, `[mcp_servers.context7]`, and `[mcp_servers.valency]` to `~/.codex/config.toml` (with backup `config.toml.bak-tavily-20261107`).
- [`scripts/adhoc/enable_cborg_mcp.py`](../scripts/adhoc/enable_cborg_mcp.py): Enables the three CBORG MCP servers in `~/.codex/config.toml` while preserving prompt policies and crawl/map exclusions (with backup `config.toml.bak-cborg-enable-20261107`).
- [`scripts/adhoc/probe_cborg_tavily.py`](../scripts/adhoc/probe_cborg_tavily.py): Performs a 1-request JSON-RPC `initialize` + `tools/list` (or single reviewed read-only tool call) against `https://api.cborg.lbl.gov/mcp/<server>` without printing credentials.
- [`scripts/adhoc/allow_cborg_mcp_tools.py`](../scripts/adhoc/allow_cborg_mcp_tools.py): Updates `~/.gemini/antigravity-cli/settings.json` and `~/.claude/settings.json` (with `.bak-cborg-mcp-allow-20261107` backups) so read-only `context7`, `tavily`, and `valency` tools are allowed while keeping outbound `valency-submit_feedback`, `gmail`, and `gdrive` in `permissions.ask`.

### Antigravity MCP Permission Syntax (`/` vs. `:`)

- **Antigravity (`~/.gemini/antigravity-cli/settings.json`)** formats MCP permission targets using a **slash (`/`)**:
  - Server-wide wildcard: `mcp(context7/*)`, `mcp(tavily/*)`, `mcp(valency/*)`
  - Per-tool rule: `mcp(valency/valency-list_sources)`, `mcp(valency/valency-submit_feedback)`
  - Colon-separated strings (`mcp(valency:*)`) do **not** match in Antigravity.
- **Claude Code (`~/.claude/settings.json`)** uses double-underscore syntax:
  - `mcp__context7__*`, `mcp__tavily__*`, `mcp__valency__*` in `permissions.allow`
  - `mcp__valency__valency-submit_feedback` in `permissions.ask`

## 2. LBNL Library Services & Publisher TDM APIs

[`scripts/adhoc/setup_lbnl_tdm_keys.py`](../scripts/adhoc/setup_lbnl_tdm_keys.py) prompts interactively (via `getpass`, with zero terminal echo or shell-history exposure) and writes `~/.config/lbnl-tdm.env` (`mode 0600`), sourced automatically by `~/.bashrc` and `~/.zshrc` (with `.bak-lbnl-tdm-20261108` backups).

### Environment Variables and Endpoint Scoping

| Variable | Compatibility Alias | Target Endpoint / Scope |
|---|---|---|
| `ZOTERO_DATA_DIR` | — | Local Zotero directory containing `zotero.sqlite` and `storage/` (`$HOME/Zotero`) |
| `WILEY_TDM_TOKEN` | `TDM_API_TOKEN` | `https://api.wiley.com/onlinelibrary/tdm/v1/articles/<encoded_doi>` (`Wiley-TDM-Client-Token` header) |
| `SPRINGER_OPENACCESS_API_KEY` | `SPRINGER_API_KEY`, `SPRINGERNATURE_API_KEY` | `https://api.springernature.com/openaccess/jats` and `/openaccess/json` (CC-licensed full text) |
| `SPRINGER_META_API_KEY` | — | `https://api.springernature.com/meta/v2/json` (bibliographic metadata & abstracts) |
| `SPRINGER_LIFESCIENCE_API_KEY` | — | `https://api.springernature.com/metadata/json` (LifeScience metadata) |
| `CORE_API_KEY` | — | `https://api.core.ac.uk/v3/search/works` (`Authorization: Bearer` header) |
| `ELSEVIER_API_KEY` | — | `https://api.elsevier.com/content/article/doi/<doi>` (`X-ELS-APIKey` header) |

### License and Provenance Boundaries

1. **No Redistribution of Paywalled Full Text**:
   - Under the Wiley TDM Agreement and Elsevier TDM provisions, downloaded full-text PDFs/XML from subscribed non-OA articles may be mounted locally for automated entity/relationship extraction and cited by DOI with brief fair-use quotations, but **must not** be committed to public Git repositories or redistributed on external websites.
   - Stage authenticated TDM full-text downloads into gitignored `.temp/` (for example, `scripts/fulltext_access.py <DOI> --fetch-to .temp/fulltext/`), and record only extracted structured claims, `snippet` quotes, and `SourceType` provenance (`PUBLISHER_TDM_FULL_TEXT` vs. `OPEN_ACCESS_FULL_TEXT` vs. `ABSTRACT_ONLY`) in committed curation records.
2. **Springer Nature Metadata API Section 8.2 (Non-OA Abstracts)**:
   - The Springer Nature Open Access API (`/openaccess/jats`) returns Creative Commons-licensed content suitable for full-text extraction.
   - The Springer Nature Metadata API Agreement (Section 8.2) restricts using **non-OA** abstracts retrieved from `/meta/v2/json` or `/metadata/json` with generative AI systems; restrict `/meta/v2/json` queries to deterministic bibliographic verification (DOI, title, publication year, OA flag) unless the record is confirmed Open Access.
3. **Anonymous External API Calls**:
   - Never include personal or institutional email addresses in `mailto` parameters or `User-Agent` headers when calling Crossref, OpenAlex, Unpaywall, Europe PMC, NCBI, OSTI, CORE, or publisher APIs.
