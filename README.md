# Sprawl Scanner

Finds **sprawl** in an Anypoint Exchange catalog: across REST/HTTP APIs, MCP servers, agents
and agent networks, it shows which capabilities are **unique**, **redundant** or a **gray
area**, and recommends what to consolidate. Built in response to an ask from Fassil Molla
(KPMG): registries list assets and evals grade agents, but nothing *finds* the duplication.

| Folder | What it is |
|---|---|
| [`mule-app/`](mule-app/) | **The product.** Mule 4.12 app for CloudHub 2.0: REST API, web UI, async scans, Java scan engine. |
| [`api-spec/`](api-spec/) | OAS 3.0 spec of the API (`sprawl-scanner-api`, published to Exchange). |
| [`python-reference/`](python-reference/) | Original Python prototype + calibration/model-comparison scripts. The Java engine was verified against it. |

How it works, and the demo talk track: [`mule-app/README.md`](mule-app/README.md),
[`mule-app/docs/talk-track.md`](mule-app/docs/talk-track.md).

---

## Credentials: rules

**No credential is ever committed.** Everything secret is supplied at deploy/run time.

- `.gitignore` blocks `.env`, `*secrets*`, `secure*.properties`, `local*.properties`, keys and keystores.
- A **gitleaks** pre-commit hook blocks commits containing secrets. Enable once per clone:
  ```bash
  brew install gitleaks          # or see github.com/gitleaks/gitleaks
  git config core.hooksPath .githooks
  ```
- CI runs the same gitleaks scan on every push and pull request (`.github/workflows/secret-scan.yml`).
- `application.properties` holds **non-secret** settings only (models, thresholds, schedule,
  token URLs). Secrets are referenced as `${...}` placeholders and resolved at runtime.

If a secret is ever committed: **rotate it first**, then remove it from history.
Deleting the file in a new commit is not enough.

---

## What you need

| Secured property (app name) | What it is | Required |
|---|---|---|
| `gemini.apiKey` | Gemini API key (Google AI Studio) for embeddings and the judge | yes |
| `anypoint.clientId` / `anypoint.clientSecret` | Anypoint **Connected App** (client credentials) with **Exchange Viewer** and **API Manager** read access, used to scan the catalog | yes |
| `mcpAuth.salesforce.clientId` / `mcpAuth.salesforce.clientSecret` | Salesforce Connected App (client credentials, `mcp_api` scope), used to read tool lists from Salesforce platform MCP servers | optional |

Also check the **non-secret** org-specific settings in
`mule-app/src/main/resources/application.properties` and change them for your org:
`anypoint.orgId`, `mcpAuth.salesforce.tokenUrl` (your Salesforce My Domain), and optionally
`scan.preferredAssets`. In `mule-app/pom.xml`, set `<ch2.target>` (your private space, or a shared space such as
`Cloudhub-US-East-2`) and `<environment>`. The Anypoint org ID also appears in `pom.xml`,
`api-spec/exchange.json` and the APIkit config: publish `api-spec/` to **your** Exchange and
replace it with your org ID.

---

## Setting the secure properties

### Option A: Runtime Manager UI (simplest)
1. Runtime Manager → your app → **Settings → Properties**.
2. For each property above: add the **key** and **value**, and tick **Secured** (or use the
   *Secure properties* section).
3. **Apply Changes**. The app restarts with the values. They're stored encrypted and shown
   masked (`******`) from then on.

### Option B: script (repeatable, no UI)
`mule-app/scripts/set_secure_properties.py` reads values from **environment variables only**
and updates a running CloudHub 2.0 deployment. It needs a Connected App allowed to manage
Runtime Manager apps (`DEPLOYER_*`). See [`.env.example`](.env.example) for the variable names.

```bash
cp .env.example .env            # .env is gitignored; fill in values
set -a; source .env; set +a
python3 mule-app/scripts/set_secure_properties.py --dry-run   # shows what will be set
python3 mule-app/scripts/set_secure_properties.py
```
Runtime Manager replaces the whole secure set on update, so provide **all** required values
each time.

### Option C: running locally (Anypoint Code Builder / Studio / standalone)
Pass the values as system properties. Don't put them in a properties file in the repo.
- **Code Builder / Studio:** Run configuration → *Arguments*:
  `-M-Dgemini.apiKey=… -M-Danypoint.clientId=… -M-Danypoint.clientSecret=…`
- **Standalone runtime:** `mule -M-Dgemini.apiKey=… -M-Danypoint.clientId=… -M-Danypoint.clientSecret=…`

`-M-D` passes the value through to the Mule runtime as a system property.

The Python reference reads `GEMINI_API_KEY`, `ANYPOINT_CLIENT_ID` and `ANYPOINT_CLIENT_SECRET`
from the environment (or user-only files under `~/.config/sprawl-scanner/`); see its README.

---

## Deploying the Mule app
See [`mule-app/README.md`](mule-app/README.md): build with JDK 17, deploy to CloudHub 2.0,
then set the secure properties with option A or B.
