"""Conversation list and detail pages."""

from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from jarvis.persistence.repositories import ConversationRepo, MessageRepo

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

        from jarvis.persistence.models import ConversationRow

        conv = await session.get(ConversationRow, conv_id)
        messages = await msg_repo.history(conv_id) if conv else []

    return templates.TemplateResponse(
        request,
        "conversation_detail.html",
        {"conversation": conv, "messages": messages},
    )
