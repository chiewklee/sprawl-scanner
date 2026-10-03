"""Sprawl Scanner as an MCP server, so agents can check for an existing capability
before anyone builds a new one, and so the scanner can sit behind the Omni gateway."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from .report import to_dict
from .scan import ScanRun, run_scan


def build_server(sources: str) -> MCPServer:
    mcp = MCPServer("sprawl-scanner", instructions=(
        "Finds redundancy across agents, MCP servers and APIs. Call find_existing_capability "
        "before building a new tool, skill or API. Call scan_inventory to refresh the scan."))
    state: dict[str, ScanRun] = {}

    def current(refresh: bool = False) -> ScanRun:
        if refresh or "run" not in state:
            state["run"] = run_scan(sources, "out")
        return state["run"]

    @mcp.tool()
    def scan_inventory(include_live: bool = True, judge: bool = True) -> dict:
        """Rescan every configured source. Returns summary counts, consolidation
        recommendations, source status and the HTML report path."""
        state["run"] = run_scan(sources, "out", include_live=include_live, judge=judge)
        d = to_dict(state["run"].analysis, state["run"].ingest)
        return {"summary": d["summary"], "embedder": d["embedder"],
                "recommendations": d["recommendations"], "ingest": d["ingest"],
                "report": str(state["run"].html_path)}

    @mcp.tool()
    def find_existing_capability(description: str, top_k: int = 5) -> list[dict]:
        """Before building something new: describe what it should do and get the most
        similar existing capabilities, each with a score and a redundant/gray/new verdict."""
        run = current()
        t = run.analysis.thresholds
        return [{"asset": c.asset, "type": c.asset_type, "name": c.name,
                 "description": c.description[:300], "score": round(s, 3),
                 "verdict": "redundant" if s >= t.redundant else
                            "gray" if s >= t.gray else "likely new"}
                for c, s in run.similar(description, top_k)]

    @mcp.tool()
    def get_redundancy_clusters(limit: int = 20) -> list[dict]:
        """Groups of capabilities implemented more than once across assets, with the
        suggested canonical implementation for each."""
        return to_dict(current().analysis, current().ingest)["clusters"][:limit]

    @mcp.tool()
    def get_gray_areas(limit: int = 25) -> list[dict]:
        """Partially overlapping capabilities that need a human decision, with the
        Gemini judge's verdict and rationale."""
        return to_dict(current().analysis, current().ingest)["gray_pairs"][:limit]

    return mcp


def serve(sources: str, transport: str = "stdio", port: int = 8765) -> None:
    server = build_server(sources)
    if transport == "stdio":
        server.run("stdio")
    else:
        server.run(transport, port=port)
