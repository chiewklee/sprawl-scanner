# Sprawl Scanner: demo talk track

**Length:** ~12 minutes (3 min rationale, 2 min Scan, 3 min Assets, 4 min Sprawl).
**URL:** https://&lt;your-app&gt;.&lt;region&gt;.cloudhub.io/
**Before you start:** make sure a completed scan exists (Scan tab, history), so Assets and
Sprawl have data even if the live scan is still running. Have the "Does this already exist?"
query ready to paste.

---

## 1. Why we built this (rationale)

> "This started with a question from **Fassil Molla at KPMG**. In our use-case session on
> 24 September, Fassil made a point that stuck with us: everyone is pitching a central
> catalog or registry as the answer to agent sprawl, but nobody actually offers a service
> that *finds* the sprawl.
>
> His ask was simple: as you bring thousands of agents, MCP servers and APIs into the
> catalog, give me a button that tells me **where I have uniqueness, where I have
> redundancy, and where I have gray areas**.
>
> Today's tools tell you *what you have*: the registry lists assets, and evals tell you
> *how well an agent performs*. Neither tells you that the same capability has been built
> four times by four teams. So we built that missing piece as a working prototype, on
> MuleSoft, in a few days."

**Why it matters (pick one or two):**
- **Cost:** every duplicate is built, run, secured and maintained more than once.
- **Risk:** five versions of the same capability means five places to patch, govern and audit.
- **Agent quality:** when an agent can pick between several near-identical tools, it picks
  inconsistently. Fewer, better tools make agents more reliable.
- **Speed:** teams reuse what exists instead of rebuilding it. The check happens *before*
  anyone writes code.

**How it works, in one breath:**
> "It reads every asset in Exchange, breaks each one into the things it can actually do
> (API operations, MCP tools, agent skills), and compares every capability with every
> other using Gemini semantic embeddings, so it matches on meaning, not wording. The
> borderline cases go to a Gemini model acting as a judge. It runs entirely on CloudHub
> 2.0 as a Mule application, alongside the assets it scans."

---

## 2. Scan tab

**Show:** the scan options and the history table.

> "Everything starts with a scan of our Exchange. We choose what to include: REST and HTTP
> APIs, MCP servers, agents and agent networks."

**Point at the options:**
- **Live MCP tool lists:** "Many MCP servers are published to Exchange *without* their tool
  list. When that happens we look the server up in API Manager and ask the running server
  for its tools. Because the scanner runs in the same CloudHub private space, it can reach
  them. For the Salesforce MCP servers it signs in with a Salesforce token first."
- **Judge model:** "Flash is the fast default. Pro is the stronger second opinion; it's
  paced to stay inside Gemini's rate limits."

**Click Start scan** (or point at the last completed one):

> "Scans run in the background and are queued, so nobody waits on a page. A full scan of
> our org takes a couple of minutes; repeat scans are faster because embeddings and
> verdicts are cached, so we only pay for what changed. It also runs nightly on a schedule."

**Point at the history row:**
> "Here's the latest result: **76 assets, 197 capabilities**. The models used are recorded
> on every scan, so the results are auditable."

---

## 3. Assets tab

**Show:** the full table, then filter.

> "This is the inventory, every asset in the catalog with what we found inside it."

**Walk the columns:**
- **Capabilities:** "how many distinct things the asset can do."
- **Source:** "where those capabilities came from. `oas` and `raml` are API specs,
  `mcp-metadata` is the tool list published in Exchange, and `live-tools-list` means we read
  it from the running server."
- **Status:** "unique, gray area or redundant, at a glance."

**Click `sap-order-mcp-server`:**
> "This MCP server exposes exactly the three operations of the SAP Order System API. A naive
> tool would flag that as 100% duplication. It isn't: we MCP-enabled the API on purpose.
> The scanner recognises that pattern and labels it **Facade (expected)**, not sprawl. It
> does the same for agents calling their own MCP tools and for API-led
> experience/process/system layers."

**Tick "Catalog gaps only":**
> "These **11** are a finding in their own right: assets published with no tool list or
> spec. If the scanner can't see inside them, neither can an agent trying to discover
> them, and neither can anyone checking for duplication. Several are stale (their endpoint
> returns 404) or have no running instance: clean-up candidates."

---

## 4. Sprawl tab

**Show:** the KPI tiles.

> "Here's the answer to Fassil's question: **unique** (green), **gray area** (amber),
> **redundant** (orange), plus the **undocumented** gaps."

### "Does this already exist?" (do this live)
**Paste:** *"Create a Jira ticket for a support issue"* → **Find**.

> "This is the check we want every team to run *before* building. It's a semantic search
> across the whole catalog. The top hit is the Jira Tickets API's `createTicket` at 0.97:
> it already exists, reuse it. The Jira MCP server has `createJiraTicket` at 0.95. And
> `POST /tickets` on the ticketing API shows up as a gray area, even though it shares
> almost no words with what I typed."

**Optional:** "The same check is available to agents and developer tools through the API,
so it can sit in a design review or a CI step."

### Recommendations
> "Instead of a raw list of matches, it tells you what to do."

Read two or three, for example:
- "**Four golf MCP servers** expose the same capabilities. Consolidate into one."
- "The **Employee PTO MCP Server** and `emp-mcp-server` are near-duplicates."
- "Two **SE Insights brokers** carry the same skills."

### Asset overlap heatmap
> "Each cell answers: *how much of this row's asset already exists in that column's
> asset?* Dark blue at 100% means everything in the row asset is already somewhere else.
> It's directional, so a small asset can be fully covered by a big one but not the other
> way round. Gray cells are the expected relationships, the facades, so the colour only
> highlights real sprawl."

### Clusters
> "A cluster is one capability implemented several times. The star marks the version
> we'd standardise on."

### Gray areas
**Filter verdict → overlap:**
> "These are the judgement calls, capabilities that are similar but maybe not the same.
> A Gemini model reads both descriptions and says *duplicate*, *overlap* or *distinct*,
> with a one-line reason. For example, a ServiceNow incident and a Jira ticket look alike
> but live in different systems, so one can't replace the other."

---

## 5. Close

> "So, back to Fassil's ask: one button, and you know where you're unique, where you're
> redundant, and where you need a human decision, across APIs, MCP servers and agents.
> Credit to Fassil for framing the problem so clearly.
>
> And it's built the way we'd want any agentic capability built: on MuleSoft, on CloudHub,
> reading the governed catalog."

**Next steps to offer:**
1. Run it against KPMG's Exchange and review the findings together.
2. Put the "does this already exist?" check into design reviews or CI.
3. Add asset lineage, so "facade vs re-implementation" is proven from dependencies rather
   than assumed from asset types.

---

## If asked

| Question | Answer |
|---|---|
| Is this a MuleSoft product? | No. It's a working prototype we built on MuleSoft to answer Fassil's ask, and a good input for the product team. |
| How accurate is it? | Thresholds were calibrated on known duplicate/overlap/distinct pairs. The judge reasons about the borderline cases. It compares descriptions, so poorly described assets are the weak spot; that's also what the catalog-gap list surfaces. |
| Is an LLM making every decision? | No. Similarity is embeddings plus maths; designed relationships (facades, agent tools, API-led layers) are deterministic rules; the LLM only judges the borderline pairs. |
| What does it cost to run? | Small. Embeddings and verdicts are cached, so rescans only pay for what changed. |
| Is the data safe? | It reads Exchange and API Manager with a read-only Connected App and only calls `tools/list` on MCP servers; secrets are Secured Application Properties. (Note: the POC UI has no login yet.) |

**Don't demo:** switching the judge to Pro live. It's paced to stay inside Gemini's
per-minute quota, so a full Pro judge takes about 8 minutes and can hit the daily cap.
