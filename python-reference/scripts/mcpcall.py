"""Call a tool on an MCP server over Streamable HTTP. Usage:
mcpcall.py URL TOOL '{json args}' [--header 'K: V' ...]"""
import json, sys, httpx
url, tool, args = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
hdrs = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
rest = sys.argv[4:]
for i, a in enumerate(rest):
    if a == "--header":
        k, v = rest[i + 1].split(":", 1); hdrs[k.strip()] = v.strip()
c = httpx.Client(timeout=900)
def rpc(body, sid=None):
    r = c.post(url, headers={**hdrs, **({"mcp-session-id": sid} if sid else {})}, json=body)
    for line in r.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:]), r
    return (r.json() if r.text.strip() else {}), r
_, r = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "cli", "version": "1"}}})
sid = r.headers.get("mcp-session-id")
rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
m, _ = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": tool, "arguments": args}}, sid)
res = m.get("result", m)
for part in res.get("content", []):
    print(part.get("text", part))
if not res.get("content"):
    print(json.dumps(res, indent=1)[:4000])
