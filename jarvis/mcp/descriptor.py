"""MCPToolDescriptor — typed contract for MCP tool metadata.

Produced by MCPManager when it enumerates tools from an MCP server.
Consumed by MCPToolRepo.replace_for_server when shadowing the catalog
to SQLite for the dashboard.
"""

from pydantic import BaseModel, ConfigDict


class MCPToolDescriptor(BaseModel):
    """Metadata for a single MCP tool, independent of its runtime invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str = ""
    input_schema: dict
    read_only_hint: bool | None = None
    destructive_hint: bool | None = None
