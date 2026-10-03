# Sprawl Scanner on CloudHub 2.0

Mule 4 app that scans the Anypoint Exchange org (REST/HTTP APIs, MCP servers, agents,
agent networks) and finds sprawl: capabilities that are unique, redundant or overlapping
across assets. Port of the Python reference at `~/projects/active/sprawl-scanner`; the
Java engine was verified against it on the real Exchange (same 171 shared capabilities,
same 44 recommendations).

- **UI:** https://&lt;your-app&gt;.&lt;region&gt;.cloudhub.io/ (Scan / Assets / Sprawl)
- **API:** `/api/v1`, spec `sprawl-scanner-api` 1.0.0 in Exchange (`~/projects/active/sprawl-scanner-api`)
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
