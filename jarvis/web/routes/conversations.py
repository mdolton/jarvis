"""Conversation list and detail pages."""

from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from jarvis.persistence.repositories import (
    ConversationRepo,
    MemoryEntryRepo,
    MemoryRecallRepo,
    MessageRepo,
)

router = APIRouter()


@router.get("/conversations", response_class=HTMLResponse)
async def conversation_list(request: Request):
    ctx = request.app.state.ctx
    templates = request.app.state.templates

    async with ctx.session_factory() as session:
        convs = await ConversationRepo(session).list_recent(limit=50)

    return templates.TemplateResponse(
        request,
        "conversations.html",
        {"conversations": convs},
    )


@router.get("/conversations/{conv_id}", response_class=HTMLResponse)
async def conversation_detail(request: Request, conv_id: UUID):
    ctx = request.app.state.ctx
    templates = request.app.state.templates

    async with ctx.session_factory() as session:
        msg_repo = MessageRepo(session)
        recall_repo = MemoryRecallRepo(session)
        memory_repo = MemoryEntryRepo(session)

        from jarvis.persistence.models import ConversationRow

        conv = await session.get(ConversationRow, conv_id)
        messages = await msg_repo.history(conv_id) if conv else []
        recall_events = await recall_repo.list_for_conversation(conv_id) if conv else []
        recalled_memories = await memory_repo.list_by_ids(
            [event.memory_entry_id for event in recall_events if event.memory_entry_id is not None]
        )
        memory_by_id = {memory.id: memory for memory in recalled_memories}
        recall_items = [
            {
                "event": event,
                "memory": memory_by_id.get(event.memory_entry_id) if event.memory_entry_id else None,
            }
            for event in recall_events
        ]

    return templates.TemplateResponse(
        request,
        "conversation_detail.html",
        {
            "conversation": conv,
            "messages": messages,
            "recall_items": recall_items,
        },
    )
