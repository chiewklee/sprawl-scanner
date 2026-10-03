# Sprawl Scanner

One button that scans your agents, MCP servers and APIs and tells you where you have
**uniqueness**, **redundancy** and **gray areas**. It's built for the gap raised in the
KPMG call (2026-09-24): registries and evals catalogue and grade assets, but nothing
compares assets with each other to find the overlap.

```
ingest  ->  normalise  ->  embed (Gemini)  ->  compare + cluster  ->  judge gray (Gemini)  ->  report / MCP
```

| Source kind | Reads |
|---|---|
| `mcp` | live `tools/list` over Streamable HTTP (handles JSON and SSE, sessions, pagination) |
| `agent_card` | A2A agent card skills (file or URL) |
| `openapi` | every path + method operation in an OAS 3.x / Swagger spec |
| `exchange` | every `rest-api`, `http-api`, `mcp`, `agent` and `agent-network` asset in the Anypoint org |
| `seed` | YAML inventory (`seed/inventory.yaml`: synthetic, with planted duplicates) |

`sources-exchange.yaml` scans only the real Exchange org; `sources.yaml` adds the seed set,
local specs and live MCP servers.

### Exchange specifics

- Auth: Connected App (client credentials, Exchange Viewer scope) from
  `ANYPOINT_CLIENT_ID`/`ANYPOINT_CLIENT_SECRET`, or `~/.config/sprawl-scanner/anypoint_connected_app`
  (two lines: id, secret; mode 600), or `ANYPOINT_TOKEN`. Optional `ANYPOINT_ORG_ID`, `ANYPOINT_BASE_URL`.
- MCP assets: tools come from the `mcp-metadata` file. Agents: an `agent` asset is a stub
  pointing at its `agent-network`, and the network YAML carries every broker/agent card with
  its skills, so cards are read from the network and deduplicated.
- APIs: OAS (plain or zipped) or RAML (`!include` tags tolerated).
- **Catalog gaps**: assets published without tool/operation metadata can't be compared.
  The report lists them so they can be republished with their tool list or spec.

## Run

```bash
uv venv && uv pip install -e '.[dev]'
.venv/bin/sprawl-scan scan --sources sources-exchange.yaml --out out/exchange --judge-limit 120
.venv/bin/sprawl-scan scan                        # all sources -> out/sprawl-report.html
.venv/bin/sprawl-scan scan --no-live --embedder local --no-judge   # offline, no keys
.venv/bin/sprawl-scan find "create a ServiceNow incident"           # does this exist?
.venv/bin/sprawl-scan serve                        # MCP server (stdio)
.venv/bin/sprawl-scan serve --transport streamable-http --port 8765
.venv/bin/pytest
```

Register with Claude Code: `claude mcp add sprawl-scanner -- $PWD/.venv/bin/sprawl-scan serve --sources $PWD/sources.yaml`

MCP tools: `scan_inventory`, `find_existing_capability`, `get_redundancy_clusters`, `get_gray_areas`.

## Secrets

Nothing secret goes in `sources.yaml`. Headers use `${ENV_VAR}`, and a source whose
variable is unset is **skipped**, not failed. `bearer_file:` reads a token from disk.
The Gemini key is read from `GEMINI_API_KEY`, falling back to
`~/.config/sprawl-scanner/gemini_api_key` (mode 600). To add a server listed as skipped,
export its token, for example `export A2D_TOKEN=...`.

## How classification works

- Every capability (MCP tool / agent skill / API operation) is embedded as
  "humanised name + description" with `gemini-embedding-001` (`SEMANTIC_SIMILARITY`).
  Embeddings are cached in `~/.cache/sprawl-scanner/`.
- Only **cross-asset** pairs count as sprawl. An asset splitting its own tools (H360
  `dispatch` vs `dispatch_readonly`) is a design choice.
- **Redundant ≥ 0.91, gray ≥ 0.88.** These are calibrated on the seed set: known duplicates
  scored 0.91–0.99, known overlaps 0.88–0.90, distinct ≤ 0.875. The Gemini space is
  compressed (median pair ≈ 0.78), so recalibrate if you change models (`GEMINI_EMBED_MODEL`).
- Clusters merge by **average linkage**, so chains (A~B, B~C, A≁C) don't snowball, and a
  cluster never holds two capabilities of the same asset.
- Gray pairs go to a Gemini judge (`GEMINI_JUDGE_MODEL`, default `gemini-3.1-pro-preview`), which
  returns `duplicate | overlap | distinct` plus a one-line rationale.
- `preferred_assets` (for example the Omni-gateway route) win consolidation ties and are
  never recommended for retirement.
- Capabilities with no real description (e.g. a bare `POST /entries`) can be at most gray.
- Expected overlap is called out rather than flagged for retirement: API-led layers
  (`exp-`/`prc-`/`sys-` of the same service) and MCP/agent facades over an API.
- Asset roll-up: share of asset A's capabilities that already exist in asset B. That share
  drives the "fold A into B" and "near-duplicate" recommendations.

The local TF-IDF fallback (`--embedder local`) needs no key. It catches obvious
duplicates but misses paraphrases, such as the four differently worded Salesforce account
lookups in the seed set.

## Next

- For MCP assets without tool metadata, resolve their API Manager instance URL and read
  `tools/list` live.
- Put `serve --transport streamable-http` behind the Omni gateway so the scanner is itself
  a governed MCP server.
- Track scans over time (sprawl trend) and add CI gating: fail a PR that adds a redundant tool.
