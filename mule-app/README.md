# Sprawl Scanner on CloudHub 2.0

Mule 4 app that scans the Anypoint Exchange org (REST/HTTP APIs, MCP servers, agents,
agent networks) and finds sprawl: capabilities that are unique, redundant or overlapping
across assets. Port of the Python reference in [`../python-reference/`](../python-reference/); the
Java engine was verified against it on the real Exchange (same 171 shared capabilities,
same 44 recommendations).

- **UI:** https://&lt;your-app&gt;.&lt;region&gt;.cloudhub.io/ (Scan / Assets / Sprawl)
- **API:** `/api/v1`, spec `sprawl-scanner-api` 1.0.0 in Exchange ([`../api-spec/`](../api-spec/))
- **Deployment:** Sandbox, a CloudHub 2.0 private space, Mule 4.12.3 Edge, Java 17, 1 × 0.5 vCore

## How it fits together

| Part | Where |
|---|---|
| REST API (APIkit) | `src/main/mule/sprawl-scanner-api-impl.xml` |
| Async scan (VM queue), scheduler, Object Store helpers, engine config | `src/main/mule/scan.xml` |
| UI (single page, served at `/`) | `src/main/mule/ui.xml`, `src/main/resources/web/index.html` |
| Scan engine: Exchange + API Manager ingest, live MCP `tools/list`, Gemini embeddings + judge, analysis | `src/main/java/com/mycompany/sprawl/` |

Mule owns the API, persistence and scheduling; the Java engine owns the scan. They exchange
JSON strings (`SprawlEngine.scan`, `find`, `progress`).

## How sprawl is determined

A scan runs five steps. Each step has a clear place to change its behaviour.

```
1 Ingest      every Exchange asset -> capabilities (API operation | MCP tool | agent skill)
2 Embed       each capability's text -> Gemini embedding (cached)
3 Compare     every cross-asset pair -> cosine similarity -> redundant / gray / ignored
4 Judge       gray pairs -> Gemini judge -> duplicate / overlap / distinct + rationale
5 Roll up     clusters, asset-to-asset coverage, recommendations
```

### 1. What counts as a capability
| Asset | One capability = | Read from |
|---|---|---|
| REST/HTTP API | one operation (`GET /orders/{id}` or its `operationId`) | OAS or RAML spec in Exchange |
| MCP server | one tool | `mcp-metadata` in Exchange, else **live** `tools/list` via the upstream URL in API Manager |
| Agent | one skill | the agent card inside its `agent-network` asset |

The text compared is `humanised name + ". " + description` (for example
`getAccountById` becomes `get account by id. Retrieve a Salesforce account…`). Descriptions
matter most: a capability with **fewer than 4 words** of description, or a description that
just repeats its name, is **thin**. It can be at most *gray*, never *redundant*.

### 2–3. The similarity rule
Only pairs on **different assets** are compared (an asset splitting its own tools is a design
choice). Each pair gets a cosine similarity between its two embeddings:

| Score | Classification |
|---|---|
| **≥ `thresholds.redundant`** (0.91) | **redundant**: same job |
| **≥ `thresholds.gray`** (0.88) | **gray**: overlapping, sent to the judge |
| below | **unique** (not related) |

Redundant pairs are merged into **clusters** by average linkage: two groups join only if their
*average* cross-similarity still clears the redundant bar, so A~B and B~C don't drag in an
unrelated C, and a cluster never holds two capabilities of the same asset.

### Designed relationships: not sprawl
Some overlap is intended. These pairs are **excluded** from redundant/gray, clustering and
judging, and labelled in the overlap tables:

| Relationship | Rule (asset types / names) | Label |
|---|---|---|
| Facade | MCP server or agent ↔ API | `facade` (e.g. MCP-enabling an API) |
| Agent uses tool | agent ↔ MCP server | `agent-tool` |
| API-led layer | same name after stripping `exp-`/`prc-`/`sys-`/`proc-`/`xapi-`/`papi-`/`sapi-` | `api-led-layer` |

Everything else (MCP↔MCP, API↔API, agent↔agent) is treated as **possible sprawl**. These
rules are deterministic (no LLM). They assume the designed pattern: they can't tell an MCP
server that *calls* the API from one that *re-implements* it.

### 4. The judge
Gray pairs (highest score first, up to `scan.judgeLimit`) go to `gemini.judgeModel` with this
instruction: decide *duplicate* (one could replace the other), *overlap* (share part of the
job) or *distinct* (only look similar), and give a one-sentence rationale. It runs at
temperature 0, JSON output. Verdicts are cached per model, so rescans only judge new pairs.
Pro models are paced (`gemini.proRpm`) and judging stops at the first HTTP 429.

### 5. Roll-up and recommendations
- **Coverage** (row → column in the heatmap): share of the row asset's capabilities that have a
  redundant match in the column asset. It's directional.
- **Near-duplicates:** coverage **≥ 80% both ways** → one "consolidate into X" recommendation per
  family of assets.
- **Fold:** coverage **≥ 50% one way** → "retire or fold A into B".
- **Cluster note:** a capability implemented on **3 or more** assets → "standardise on X". The
  suggested canonical one is the cluster's most central member, or a `scan.preferredAssets`
  entry if one is in the cluster.
- **`scan.preferredAssets`:** governed routes (for example the Omni gateway one) win ties and
  are never recommended for retirement.

## Customising the matching rules

### Without code (`application.properties` or Runtime Manager properties)
| Property | Default | Effect |
|---|---|---|
| `thresholds.redundant` | `0.91` | raise it for fewer, surer duplicates; lower it to catch more |
| `thresholds.gray` | `0.88` | width of the "needs a look" band (pairs sent to the judge) |
| `scan.preferredAssets` | (empty) | comma-separated asset names that win consolidation ties |
| `scan.judgeLimit` | `400` | max gray pairs judged per scan |
| `gemini.judgeModel` | `gemini-3.8-flash` | judge model (per-scan override from the UI) |
| `gemini.embedModel` | `gemini-embedding-001` | embedding model; **recalibrate thresholds if you change it** |
| `gemini.proRpm` / `gemini.flashRpm` | `20` / `0` | judge pacing in requests per minute (0 = unpaced) |
| `mcpAuth.salesforce.*` | | OAuth client-credentials rule for reading tool lists from protected MCP servers |

Properties set on the deployment in Runtime Manager override the file, so you can tune a
running app without rebuilding.

### Recalibrating thresholds
Thresholds depend on the embedding model: scores aren't comparable across models.
`../python-reference/scripts/calibrate_embeddings.py MODEL [MODEL…]` scores known duplicate,
overlap and distinct pairs and prints where the groups separate. Put the redundant line just
under the lowest true duplicate and the gray line just under the lowest real overlap. Better
still: label 30–50 pairs from **your** catalog and add them to the script's lists.
`../python-reference/scripts/compare_judges.py` re-judges a scan's gray pairs with another
model and uses a third as tiebreaker.

### In code (`src/main/java/com/mycompany/sprawl/`)
| Change | Where |
|---|---|
| Add or change a designed relationship (e.g. "experience APIs over the same process API are fine") | `Analyzer.relationship()` (plus wording in `Analyzer.intentional()`) |
| Recognise other API-led prefixes | `Analyzer.LAYER` regex |
| What counts as a thin description | `Cap.thin()` |
| Fold / near-duplicate cut-offs (50% / 80%), "3+ assets" cluster note | `Analyzer.analyze()` → `recommendations(…, 0.5, 0.8, …)` and the cluster loop |
| The judge's instruction | `SprawlEngine.PROMPT` (cache keys include the model, not the prompt: clear `cache:judge` after a prompt change) |
| Which Exchange files are read, in what order | `Exchange.CLASSIFIER_ORDER`, `parseDocument()` |
| Auth for more MCP hosts | add another entry to `mcpAuth` in `src/main/mule/scan.xml` (`build-config`) and its properties |

After a change: `mvn -B clean package -DskipTests` (JDK 17), then redeploy (below) and rescan.

## Configuration

Non-secret defaults are in `src/main/resources/application.properties` (models, thresholds,
nightly cron `0 0 2 * * ?` UTC, `scan.schedule.enabled`). Secrets are **Secured Application
Properties** on the deployment, never in files. See the repo root README for the list and the
three ways to set them (Runtime Manager UI, `scripts/set_secure_properties.py`, local `-M-D`).

## Redeploying a code change

The MCP deploy tool only creates deployments. To update in place (keeps secrets/settings):
1. `mvn -B clean package -DskipTests` with JDK 17.
2. Publish the jar to Exchange as the next version of asset
   `sprawl-scanner-api-impl-1.0.0-SNAPSHOT-mule-application` (type `app`).
3. PATCH the deployment's `application.ref.version` to that version (AMC API).

## Known limitations

- **No auth** on UI/API (by request). Anyone with the URL can start scans on the Gemini key.
- **Object Store v2 is off.** Enabling it in this private space failed (`auth.rtf.svc.cluster.local`
  unresolvable), so history, results and caches live in the replica and are lost on
  restart/redeploy. A fresh scan rebuilds them.
- **Live progress** (phase, n/N) isn't surfaced while a scan runs; final status is correct.
- Thresholds (redundant ≥ 0.91, gray ≥ 0.88) are calibrated for `gemini-embedding-001`.
