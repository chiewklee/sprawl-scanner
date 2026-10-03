"""Common capability record that every source (MCP tool, agent skill, API operation)
is normalised into."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

ASSET_TYPES = ("mcp", "agent", "api")


def humanize(name: str) -> str:
    """'getAccountById' / 'get_account_by_id' / 'GET /accounts/{id}' -> 'get account by id'."""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    s = re.sub(r"[_\-/{}.:]+", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


_PLACEHOLDER_ID = re.compile(r"^(null|undefined|none|operation)?[_\-]?\d*$", re.I)


def operation_name(op: dict, method: str, route: str) -> str:
    """operationId, unless it's a generator placeholder like 'null_7': then 'METHOD /path'."""
    op_id = str(op.get("operationId") or "").strip()
    return op_id if op_id and not _PLACEHOLDER_ID.match(op_id) else f"{method.upper()} {route}"


@dataclass(frozen=True)
class Capability:
    asset: str  # owning asset, e.g. "sfdc-headless-360"
    asset_type: str  # one of ASSET_TYPES
    name: str  # tool / skill / operation name
    description: str
    source: str  # URL or file the capability was read from
    owner: str = ""

    @property
    def id(self) -> str:
        return f"{self.asset_type}:{self.asset}:{self.name}"

    @property
    def thin(self) -> bool:
        """No real description (e.g. a bare 'POST /entries'): too little signal to call
        it a duplicate, so it can be at most gray."""
        d = humanize(self.description)
        return len(d.split()) < 4 or d == humanize(self.name)

    def text(self) -> str:
        """Text used for embedding: humanised name plus description."""
        return f"{humanize(self.name)}. {self.description}".strip()

    def to_dict(self) -> dict:
        return {"id": self.id, **asdict(self)}
