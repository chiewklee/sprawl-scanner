#!/usr/bin/env python3
"""Set the Sprawl Scanner's Secured Application Properties on a CloudHub 2.0 deployment.

Values are read from environment variables only (see ../../.env.example); nothing is
written to disk. Runtime Manager replaces the whole secure-property set on update, so all
required values must be provided every time. Triggers a rolling restart of the app.

Usage:
    export DEPLOYER_CLIENT_ID=... DEPLOYER_CLIENT_SECRET=...   # Connected App: Runtime Manager manage
    export ANYPOINT_ORG_ID=... ANYPOINT_ENV_ID=... DEPLOYMENT_ID=...
    export GEMINI_API_KEY=... SCANNER_ANYPOINT_CLIENT_ID=... SCANNER_ANYPOINT_CLIENT_SECRET=...
    export SALESFORCE_CLIENT_ID=... SALESFORCE_CLIENT_SECRET=...   # optional
    python3 scripts/set_secure_properties.py [--dry-run]

Standard library only.
"""

import json
import os
import sys
import urllib.request

BASE = os.environ.get("ANYPOINT_BASE_URL", "https://anypoint.mulesoft.com")

# app property name -> environment variable
REQUIRED = {
    "gemini.apiKey": "GEMINI_API_KEY",
    "anypoint.clientId": "SCANNER_ANYPOINT_CLIENT_ID",
    "anypoint.clientSecret": "SCANNER_ANYPOINT_CLIENT_SECRET",
}
OPTIONAL = {
    "mcpAuth.salesforce.clientId": "SALESFORCE_CLIENT_ID",
    "mcpAuth.salesforce.clientSecret": "SALESFORCE_CLIENT_SECRET",
}


def env(name):
    v = os.environ.get(name, "").strip()
    if not v:
        sys.exit(f"missing environment variable {name}")
    return v


def call(method, url, token=None, body=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, method=method, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"{}")


def main():
    dry = "--dry-run" in sys.argv
    secure = {prop: env(var) for prop, var in REQUIRED.items()}
    opt = {prop: os.environ.get(var, "").strip() for prop, var in OPTIONAL.items()}
    if any(opt.values()) and not all(opt.values()):
        sys.exit("set both SALESFORCE_CLIENT_ID and SALESFORCE_CLIENT_SECRET, or neither")
    secure.update({k: v for k, v in opt.items() if v})

    token = call("POST", f"{BASE}/accounts/api/v2/oauth2/token", body={
        "grant_type": "client_credentials", "client_id": env("DEPLOYER_CLIENT_ID"),
        "client_secret": env("DEPLOYER_CLIENT_SECRET")})["access_token"]
    url = (f"{BASE}/amc/application-manager/api/v2/organizations/{env('ANYPOINT_ORG_ID')}"
           f"/environments/{env('ANYPOINT_ENV_ID')}/deployments/{env('DEPLOYMENT_ID')}")
    dep = call("GET", url, token)
    svc = dep["application"]["configuration"]["mule.agent.application.properties.service"]
    print(f"deployment {dep['name']}: setting {len(secure)} secure properties: {sorted(secure)}")
    if dry:
        print("dry run: nothing changed")
        return
    body = {"application": {"configuration": {"mule.agent.application.properties.service": {
        "applicationName": svc.get("applicationName"),
        "properties": svc.get("properties") or {},   # keep plain properties as they are
        "secureProperties": secure,
    }}}}
    out = call("PATCH", url, token, body)
    keys = sorted(out["application"]["configuration"]["mule.agent.application.properties.service"]
                  .get("secureProperties", {}))
    print(f"done; secure properties now set (values masked by Runtime Manager): {keys}")
    print("the app restarts with the new values (a rolling update, usually a few minutes)")


if __name__ == "__main__":
    main()
