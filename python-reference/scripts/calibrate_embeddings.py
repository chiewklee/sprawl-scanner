"""Score known duplicate / overlap / distinct pairs from the seed inventory under one or
more embedding models, to pick thresholds. Usage: calibrate_embeddings.py MODEL [MODEL...]"""

import sys

import numpy as np

from sprawl_scanner.embed import GeminiEmbedder
from sprawl_scanner.ingest import seed

DUP = [(("crm-sys-api", "getAccountById"), ("sales-copilot-mcp", "lookup_customer")),
       (("crm-sys-api", "getAccountById"), ("customer-360-api", "GET /customers/{id}")),
       (("itsm-sys-api", "createIncident"), ("servicenow-mcp", "create_incident")),
       (("itsm-sys-api", "createIncident"), ("helpdesk-agent", "raise-ticket")),
       (("peoplesoft-hr-api", "getEmployee"), ("workday-hr-mcp", "get_worker")),
       (("pricing-api", "calculateQuote"), ("deal-pricing-mcp", "price_engagement")),
       (("pricing-api", "getRateCard"), ("deal-pricing-mcp", "get_rate_card")),
       (("crm-sys-api", "getOpportunitiesByAccount"), ("customer-360-api", "GET /customers/{id}/opportunities")),
       (("crm-sys-api", "updateOpportunityStage"), ("sales-copilot-mcp", "move_deal_stage")),
       (("peoplesoft-hr-api", "getEmployeeLeaveBalance"), ("workday-hr-mcp", "get_time_off_balance"))]
GRAY = [(("exec-brief-agent", "executive-brief"), ("account-insights-mcp", "summarize_account")),
        (("workday-hr-mcp", "get_skills_profile"), ("engagement-staffing-agent", "recommend-staff")),
        (("sales-copilot-mcp", "draft_account_plan"), ("exec-brief-agent", "executive-brief")),
        (("crm-sys-api", "searchAccounts"), ("sales-copilot-mcp", "lookup_customer"))]
DISTINCT = [(("crm-sys-api", "getAccountById"), ("customer-360-api", "GET /customers/{id}/invoices")),
            (("pricing-api", "getRateCard"), ("deal-pricing-mcp", "margin_check")),
            (("partner-comp-agent", "comp-explainer"), ("pricing-api", "calculateQuote")),
            (("itsm-sys-api", "getIncident"), ("servicenow-mcp", "get_change_request")),
            (("risk-onboarding-api", "screenSanctions"), ("crm-sys-api", "searchAccounts")),
            (("knowledge-search-mcp", "get_credential"), ("account-insights-mcp", "sector_insights")),
            (("helpdesk-agent", "password-reset"), ("servicenow-mcp", "create_incident"))]

caps = seed("seed/inventory.yaml")
idx = {(c.asset, c.name): i for i, c in enumerate(caps)}
for model in sys.argv[1:]:
    E = GeminiEmbedder(model=model).embed([c.text() for c in caps])
    S = E @ E.T
    score = {lbl: [float(S[idx[a], idx[b]]) for a, b in L]
             for lbl, L in (("dup", DUP), ("gray", GRAY), ("distinct", DISTINCT))}
    off = S[np.triu_indices(len(caps), 1)]
    print(f"== {model}  (dims={E.shape[1]})")
    for lbl, v in score.items():
        print(f"   {lbl:8} min={min(v):.3f} max={max(v):.3f}  " + " ".join(f"{x:.3f}" for x in v))
    print(f"   all pairs median={np.median(off):.3f}  p90={np.percentile(off, 90):.3f}")
    gap = min(score["dup"]) - max(score["distinct"])
    print(f"   margin dup-min vs distinct-max: {gap:+.3f}   "
          f"spread (dup-min - median): {min(score['dup']) - np.median(off):.3f}")
