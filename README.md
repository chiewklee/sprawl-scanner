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

How sprawl is determined and how to tune the rules: [`mule-app/README.md` → How sprawl is determined](mule-app/README.md#how-sprawl-is-determined)
and [Customising the matching rules](mule-app/README.md#customising-the-matching-rules).
Demo: [`talk-track.md`](mule-app/docs/talk-track.md) and intro slides [`sprawl-scanner-intro.pptx`](mule-app/docs/sprawl-scanner-intro.pptx)
(regenerate with `python mule-app/docs/make_slides.py`, needs `python-pptx`).

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

## Deploy to your own environment

Step by step, from a fresh clone to a first scan. About 30 minutes, plus CloudHub rollout time.

### 0. Prerequisites
| You need | Notes |
|---|---|
| Anypoint Platform org with **CloudHub 2.0** | a **private space** is recommended: the live MCP / agent-card fallback reads running servers' upstream URLs, which are usually private. A shared space works, but those assets show as catalog gaps. |
| Mule runtime **4.12.x** (Edge) on that target | check Runtime Manager → your target → available runtimes |
| **JDK 17** and **Maven 3.9+** | `JAVA_HOME` must point at JDK 17 |
| A **Gemini API key** | Google AI Studio. Tier 1 or above recommended (the free tier rate-limits quickly) |
| An Anypoint **Connected App for scanning** (client credentials) | scopes: **Exchange Viewer** and **API Manager: View APIs Configuration**. Becomes `anypoint.clientId` / `anypoint.clientSecret`. |
| *(optional)* a Salesforce Connected App, client credentials, `mcp_api` scope | only to read tool lists from Salesforce platform MCP servers. Becomes `mcpAuth.salesforce.*` |

### 1. Point the code at your org
Your **Anypoint org ID** (Access Management → Organization) appears in five places.
Replace it everywhere with:
```bash
NEW_ORG=<your-org-id>
grep -rl fa76c43c-f6d0-41fd-bdcd-214ccae74d41 api-spec mule-app | xargs sed -i '' "s/fa76c43c-f6d0-41fd-bdcd-214ccae74d41/$NEW_ORG/g"   # macOS; on Linux drop the ''
```
(`mule-app/pom.xml`, the APIkit config in `sprawl-scanner-api-impl.xml`, `api-spec/exchange.json` ×2,
`application.properties`.)

### 2. Publish the API spec to your Exchange
The app's build pulls the spec from **your** Exchange, so publish it first. Either:
- **Code Builder / Studio:** open `api-spec/` → *Publish to Exchange* (asset `sprawl-scanner-api`,
  version `1.0.0`, type REST API), or
- **Anypoint CLI:** `anypoint-cli-v4 exchange:asset:upload --organization <org> sprawl-scanner-api/1.0.0 --name "Sprawl Scanner API" --type rest-api --files '{"oas.zip":"<zip of api-spec/>"}'`

### 3. Let Maven read Exchange
Add Exchange credentials to `~/.m2/settings.xml` (server id must be **`anypoint-exchange-v3`**,
as in `pom.xml`). For a Connected App with Exchange access:
```xml
<server>
  <id>anypoint-exchange-v3</id>
  <username>~~~Client~~~</username>
  <password>CLIENT_ID~?~CLIENT_SECRET</password>
</server>
```

### 4. Build
```bash
cd mule-app
JAVA_HOME=<path to JDK 17> mvn -B clean package -DskipTests -Dch2.target=<your-private-space-or-shared-space>
# -> target/sprawl-scanner-api-impl-1.0.0-SNAPSHOT-mule-application.jar
```

### 5. Deploy to CloudHub 2.0
**Runtime Manager UI (simplest):** *Deploy application* → upload the jar → target = your space,
runtime **4.12.x**, 1 replica, **0.5 vCore** (scans hold embeddings in memory) → enable a public
URL if you want the UI reachable → **Deploy**.

**Or Maven:** the `<cloudhub2Deployment>` block in `mule-app/pom.xml` is ready. Add deployer
Connected App credentials (Runtime Manager *Create/Manage Applications* scope) under
`<connectedAppClientId>` / `<connectedAppClientSecret>` / `<connectedAppGrantType>client_credentials`
(from `-D` properties, not literals) and run `mvn deploy -DmuleDeploy`.

Leave **Object Store v2 off** unless you've confirmed it works in your space (see
`mule-app/README.md` → Known limitations).

### 6. Set properties
In Runtime Manager → the app → **Settings → Properties**:
- **Secured:** `gemini.apiKey`, `anypoint.clientId`, `anypoint.clientSecret`
  (+ `mcpAuth.salesforce.clientId` / `clientSecret` if used). See *Setting the secure properties* above.
- **Plain (if used):** `mcpAuth.salesforce.tokenUrl=https://<your-domain>.my.salesforce.com/services/oauth2/token`
- Anything from *Customising the matching rules* (thresholds, preferred assets, judge model, nightly schedule).

**Apply**. The app restarts with them.

### 7. First scan
Open `https://<your-app-url>/`, go to **Scan**, then **Start scan**. A first scan of ~100 assets takes
2–4 minutes; rescans are faster (embeddings and verdicts are cached). Or by API:
```bash
curl -X POST https://<your-app-url>/api/v1/scans -H 'Content-Type: application/json' -d '{}'
```

### 8. Updating later
After a code change: rebuild (step 4), then in Runtime Manager choose **Choose file → upload the new
jar → Apply**. Properties and secrets are kept. (The MuleSoft MCP `deploy_mule_application` tool
can create a deployment but not replace one.)

### Troubleshooting
| Symptom | Cause / fix |
|---|---|
| `mvn package` can't resolve `sprawl-scanner-api` | spec not published to your org (step 2), or Exchange credentials missing (step 3) |
| Scan fails `Anypoint token HTTP 401` | wrong `anypoint.clientId/Secret`, or the Connected App lacks Exchange Viewer |
| Many MCP servers / agents listed as gaps with `tools/list failed` or timeouts | the app can't reach their upstream URLs: deploy into the same private space, or accept them as gaps |
| Judge verdicts missing, `HTTP 429` | Gemini rate limit: use Flash, lower `gemini.proRpm`, or raise your Gemini tier |
| `OS:STORE_NOT_AVAILABLE` | Object Store v2 enabled but not working in your space: turn it off |
| "No completed scan yet" after a redeploy | expected: results live in the replica; run a scan |

Matching-rule tuning: [`mule-app/README.md` → Customising the matching rules](mule-app/README.md#customising-the-matching-rules).
