"""Document corpus ingestion + retrieval.

Chunks user content (markdown, text, PDF), embeds chunks into the prefixed
sqlite-vec tables, and serves passage search for the ``search_documents``
agent tool. Idempotency: a file whose sha256 matches its stored
content_hash (and is fully indexed) is skipped, mirroring the
recall-summary source-hash dedup.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jarvis.core.types import AuditEvent, AuditEventType
from jarvis.memory.chunking import chunk_text
from jarvis.memory.embeddings import EmbeddingProvider
from jarvis.memory.vector_store import MemoryVectorStore
from jarvis.persistence.repositories import DocumentRepo

_log = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = frozenset({".md", ".markdown", ".txt", ".pdf"})


@dataclass(frozen=True, slots=True)
class DocumentIngestOutcome:
    status: str  # created | updated | unchanged | unindexed | failed
    source_ref: str
    document_id: UUID | None = None
    chunk_count: int = 0
    error: str | None = None


@dataclass(frozen=True, slots=True)
class DocumentPassage:
    document_id: UUID
    title: str
    source_ref: str
    chunk_index: int
    content: str
    score: float


class DocumentService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        embedding_provider: EmbeddingProvider,
        vector_store: MemoryVectorStore,
        chunk_chars: int,
        chunk_overlap: int,
        max_results: int,
        min_relevance_score: float,
        audit=None,
    ) -> None:
        self._session_factory = session_factory
        self._embedding_provider = embedding_provider
        self._vector_store = vector_store
        self._chunk_chars = chunk_chars
        self._chunk_overlap = chunk_overlap
        self._max_results = max_results
        self._min_relevance_score = min_relevance_score
        self._audit = audit

    @property
    def search_available(self) -> bool:
        return self._vector_store.available

    async def ingest_path(self, path: Path) -> list[DocumentIngestOutcome]:
        files = await asyncio.to_thread(_list_supported_files, path)
        return [await self.ingest_file(file) for file in files]

    async def ingest_file(self, path: Path) -> DocumentIngestOutcome:
        source_ref, raw, read_error = await asyncio.to_thread(_read_source, path)
        if raw is None:
            return await self._failed(
                source_ref, document_id=None, error=read_error or "unreadable file"
            )

        content_hash = hashlib.sha256(raw).hexdigest()
        async with self._session_factory() as session:
            existing = await DocumentRepo(session).get_by_source_ref(source_ref)
        if (
            existing is not None
            and existing.content_hash == content_hash
            and existing.status == "active"
        ):
            async with self._session_factory() as session:
                chunk_count = await DocumentRepo(session).count_chunks(existing.id)
            return DocumentIngestOutcome(
                status="unchanged",
                source_ref=source_ref,
                document_id=existing.id,
                chunk_count=chunk_count,
            )

        try:
            text = await asyncio.to_thread(_extract_text, path, raw)
            chunks = chunk_text(text, max_chars=self._chunk_chars, overlap=self._chunk_overlap)
        except Exception as exc:
            return await self._failed(
                source_ref, document_id=existing.id if existing else None, error=str(exc)
            )
        if not chunks:
            return await self._failed(
                source_ref,
                document_id=existing.id if existing else None,
                error="no extractable text",
            )

        async with self._session_factory() as session:
            repo = DocumentRepo(session)
            if existing is None:
                document = await repo.create(
                    source_type="file",
                    source_ref=source_ref,
                    title=path.stem,
                    content_hash=content_hash,
                )
                document_id = document.id
            else:
                document_id = existing.id
                await repo.mark_reingesting(document_id, title=path.stem, content_hash=content_hash)
            old_chunk_ids, new_rows = await repo.replace_chunks(document_id, chunks)

        status = "created" if existing is None else "updated"
        await self._vector_store.delete_many(old_chunk_ids)

        if not self._vector_store.available:
            return await self._unindexed(
                source_ref,
                document_id=document_id,
                chunk_count=len(new_rows),
                error=self._vector_store.last_error or "vector store unavailable",
            )

        try:
            embeddings = await self._embedding_provider.embed_many(
                [row.content for row in new_rows]
            )
            for row, embedding in zip(new_rows, embeddings, strict=True):
                await self._vector_store.upsert(row.id, embedding)
        except Exception as exc:
            return await self._unindexed(
                source_ref,
                document_id=document_id,
                chunk_count=len(new_rows),
                error=str(exc),
            )

        async with self._session_factory() as session:
            await DocumentRepo(session).set_status(document_id, "active")
        await self._emit(
            AuditEventType.DOCUMENT_INGESTED,
            payload={
                "document_id": str(document_id),
                "source_ref": source_ref,
                "status": status,
                "chunks": len(new_rows),
            },
        )
        return DocumentIngestOutcome(
            status=status,
            source_ref=source_ref,
            document_id=document_id,
            chunk_count=len(new_rows),
        )

    async def search(self, query: str, *, limit: int | None = None) -> list[DocumentPassage]:
        effective_limit = self._max_results if limit is None else limit
        if not query.strip() or effective_limit <= 0 or not self._vector_store.available:
            return []
        try:
            embedding = await self._embedding_provider.embed(query)
            results = await self._vector_store.search(embedding, limit=effective_limit)
        except Exception as exc:
            _log.warning("document search failed: %s", exc)
            return []
        filtered = [r for r in results if r.score >= self._min_relevance_score]
        if not filtered:
            return []
        async with self._session_factory() as session:
            found = await DocumentRepo(session).get_chunks_with_documents(
                [result.entry_id for result in filtered]
            )
        passages: list[DocumentPassage] = []
        for result in filtered:
            match = found.get(result.entry_id)
            if match is None:
                continue
            chunk, document = match
            passages.append(
                DocumentPassage(
                    document_id=document.id,
                    title=document.title,
                    source_ref=document.source_ref,
                    chunk_index=chunk.chunk_index,
                    content=chunk.content,
                    score=result.score,
                )
            )
        return passages

    async def _failed(
        self, source_ref: str, *, document_id: UUID | None, error: str
    ) -> DocumentIngestOutcome:
        if document_id is not None:
            await self._set_status_best_effort(document_id, "error", error=error)
        await self._emit(
            AuditEventType.DOCUMENT_FAILED,
            payload={"source_ref": source_ref, "stage": "extract", "error": error},
        )
        return DocumentIngestOutcome(
            status="failed", source_ref=source_ref, document_id=document_id, error=error
        )

    async def _unindexed(
        self, source_ref: str, *, document_id: UUID, chunk_count: int, error: str
    ) -> DocumentIngestOutcome:
        await self._set_status_best_effort(document_id, "unindexed", error=error)
        await self._emit(
            AuditEventType.DOCUMENT_FAILED,
            payload={"source_ref": source_ref, "stage": "index", "error": error},
        )
        return DocumentIngestOutcome(
            status="unindexed",
            source_ref=source_ref,
            document_id=document_id,
            chunk_count=chunk_count,
            error=error,
        )

    async def _set_status_best_effort(
        self, document_id: UUID, status: str, *, error: str | None
    ) -> None:
        try:
            async with self._session_factory() as session:
                await DocumentRepo(session).set_status(document_id, status, error=error)
        except Exception:
            _log.exception("failed to update document status")

    async def _emit(self, event_type: AuditEventType, *, payload: dict) -> None:
        if self._audit is None:
            return
        try:
            await self._audit.emit(AuditEvent(type=event_type, payload=payload))
        except Exception:
            return


def _list_supported_files(path: Path) -> list[Path]:
    path = path.expanduser()
    if not path.is_dir():
        return [path]
    return sorted(
        candidate
        for candidate in path.rglob("*")
        if candidate.is_file() and candidate.suffix.lower() in SUPPORTED_SUFFIXES
    )


def _read_source(path: Path) -> tuple[str, bytes | None, str | None]:
    path = path.expanduser()
    source_ref = str(path.resolve())
    try:
        return source_ref, path.read_bytes(), None
    except OSError as exc:
        return source_ref, None, str(exc)


def _extract_text(path: Path, raw: bytes) -> str:
    if path.suffix.lower() == ".pdf":
        return _extract_pdf_text(raw)
    return raw.decode("utf-8", errors="replace")


def _extract_pdf_text(raw: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(raw))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(part.strip() for part in pages if part.strip())
