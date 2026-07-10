"""search_documents function tool formatting + agent wiring."""

import json
from uuid import uuid4

from agents.tool_context import ToolContext

from jarvis.agents.document_tool import build_document_search_tool
from jarvis.agents.factory import build_agent
from jarvis.config.schema import LLMConfig
from jarvis.memory.documents import DocumentPassage


class FakeDocumentService:
    def __init__(self, passages):
        self._passages = passages
        self.queries = []

    async def search(self, query, *, limit=None):
        self.queries.append(query)
        return self._passages


def _passage(content, title="note", chunk_index=0, score=0.9):
    return DocumentPassage(
        document_id=uuid4(),
        title=title,
        source_ref=f"/docs/{title}.md",
        chunk_index=chunk_index,
        content=content,
        score=score,
    )


async def _invoke(tool, query):
    arguments = json.dumps({"query": query})
    ctx = ToolContext(
        context=None,
        tool_name=tool.name,
        tool_call_id="call_1",
        tool_arguments=arguments,
    )
    return await tool.on_invoke_tool(ctx, arguments)


async def test_tool_returns_formatted_passages():
    service = FakeDocumentService([_passage("the wifi password is hunter2")])
    tool = build_document_search_tool(service)

    assert tool.name == "search_documents"
    output = await _invoke(tool, "wifi password")

    assert service.queries == ["wifi password"]
    assert "hunter2" in output
    assert "note" in output


async def test_tool_reports_no_matches():
    tool = build_document_search_tool(FakeDocumentService([]))
    output = await _invoke(tool, "anything")
    assert "No matching passages" in output


async def test_tool_output_is_bounded():
    passages = [_passage("x" * 5_000, title=f"n{i}", chunk_index=i) for i in range(10)]
    tool = build_document_search_tool(FakeDocumentService(passages))
    output = await _invoke(tool, "big")
    assert len(output) <= 7_000


def test_build_agent_attaches_tools():
    llm = LLMConfig(base_url="http://localhost", api_key="k", model="m")
    tool = build_document_search_tool(FakeDocumentService([]))
    agent, _ = build_agent(llm_config=llm, mcp_servers_provider=list, tools=[tool])
    assert [t.name for t in agent.tools] == ["search_documents"]


def test_build_agent_defaults_to_no_tools():
    llm = LLMConfig(base_url="http://localhost", api_key="k", model="m")
    agent, _ = build_agent(llm_config=llm, mcp_servers_provider=list)
    assert agent.tools == []
