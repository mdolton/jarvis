"""Native SDK tool exposing document passage search to agent runs."""

from __future__ import annotations

from agents import function_tool

_MAX_PASSAGE_CHARS = 1_200
# Total tool output cap: passages are additional turn context, so keep the
# whole result well under the runner's history budget.
_MAX_TOTAL_CHARS = 6_000


def build_document_search_tool(document_service):
    @function_tool(name_override="search_documents")
    async def search_documents(query: str) -> str:
        """Search the user's own documents (notes, PDFs, attachments) for passages
        relevant to the query. Use this whenever the question may be answerable
        from the user's personal content rather than general knowledge."""
        passages = await document_service.search(query)
        if not passages:
            return "No matching passages found in the document index."
        blocks: list[str] = []
        total = 0
        for passage in passages:
            snippet = passage.content[:_MAX_PASSAGE_CHARS]
            block = (
                f"[{passage.title} · {passage.source_ref} · chunk {passage.chunk_index} "
                f"· score {passage.score:.2f}]\n{snippet}"
            )
            if total + len(block) > _MAX_TOTAL_CHARS:
                break
            blocks.append(block)
            total += len(block)
        return "\n\n".join(blocks)

    return search_documents
