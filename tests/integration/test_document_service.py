"""End-to-end document ingestion + retrieval over real sqlite-vec."""

import math
import re
import zlib

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jarvis.memory.documents import DocumentService
from jarvis.memory.vector_store import MemoryVectorStore
from jarvis.persistence.db import Base
from jarvis.persistence.repositories import DocumentRepo

_DIMS = 16


class HashEmbeddings:
    """Deterministic bag-of-words embeddings: shared vocabulary → nearby vectors."""

    async def embed(self, text: str) -> list[float]:
        vec = [0.0] * _DIMS
        for token in re.findall(r"[a-z0-9]+", text.lower()):
            vec[zlib.crc32(token.encode()) % _DIMS] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [await self.embed(text) for text in texts]


@pytest.fixture
async def harness(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/docs.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = MemoryVectorStore(
        db_path=tmp_path / "docs.db", dimensions=_DIMS, table_prefix="document"
    )
    await store.initialize()
    service = DocumentService(
        session_factory=factory,
        embedding_provider=HashEmbeddings(),
        vector_store=store,
        chunk_chars=80,
        chunk_overlap=20,
        max_results=3,
        min_relevance_score=0.0,
    )
    yield service, factory, store, tmp_path
    await engine.dispose()


def _write_note(tmp_path, name="note.md"):
    note = tmp_path / name
    note.write_text(
        "# Home network\n\n"
        "The garage wifi network is called sparrowhawk and the password is hunter2.\n\n"
        "Grocery list: eggs, milk, coffee beans, and oat bread from the market.\n"
    )
    return note


async def test_question_answerable_only_from_document_retrieves_right_passage(harness):
    service, _, _, tmp_path = harness
    note = _write_note(tmp_path)

    outcome = await service.ingest_file(note)
    assert outcome.status == "created"
    assert outcome.chunk_count >= 2

    passages = await service.search("what is the garage wifi password?")

    assert passages
    assert "hunter2" in passages[0].content
    assert passages[0].title == "note"
    assert passages[0].source_ref == str(note.resolve())


async def test_reingest_unchanged_file_is_idempotent(harness):
    service, factory, _, tmp_path = harness
    note = _write_note(tmp_path)

    first = await service.ingest_file(note)
    second = await service.ingest_file(note)

    assert second.status == "unchanged"
    assert second.document_id == first.document_id
    assert second.chunk_count == first.chunk_count
    async with factory() as session:
        assert await DocumentRepo(session).count_chunks(first.document_id) == first.chunk_count


async def test_changed_file_reindexes_and_drops_stale_chunks(harness):
    service, factory, _store, tmp_path = harness
    note = _write_note(tmp_path)
    first = await service.ingest_file(note)

    note.write_text("Completely new content: the safe code is 4242.\n")
    second = await service.ingest_file(note)

    assert second.status == "updated"
    assert second.document_id == first.document_id
    async with factory() as session:
        assert await DocumentRepo(session).count_chunks(first.document_id) == second.chunk_count
    passages = await service.search("what is the safe code?")
    assert passages and "4242" in passages[0].content
    stale = await service.search("grocery list eggs milk")
    assert all("grocery" not in p.content.lower() for p in stale)


async def test_folder_ingest_walks_supported_files(harness):
    service, _, _, tmp_path = harness
    folder = tmp_path / "corpus"
    folder.mkdir()
    (folder / "a.md").write_text("alpha note about kayaks")
    (folder / "b.txt").write_text("beta note about telescopes")
    (folder / "ignored.bin").write_bytes(b"\x00\x01")

    outcomes = await service.ingest_path(folder)

    assert [o.status for o in outcomes] == ["created", "created"]


async def test_unavailable_vector_store_degrades_gracefully(harness):
    _, factory, _, tmp_path = harness
    broken = MemoryVectorStore(
        db_path=tmp_path / "other.db", dimensions=_DIMS, table_prefix="document"
    )
    # never initialized → available is False
    service = DocumentService(
        session_factory=factory,
        embedding_provider=HashEmbeddings(),
        vector_store=broken,
        chunk_chars=200,
        chunk_overlap=40,
        max_results=3,
        min_relevance_score=0.0,
    )
    note = tmp_path / "degraded.md"
    note.write_text("some content that cannot be indexed right now")

    outcome = await service.ingest_file(note)
    assert outcome.status == "unindexed"

    assert await service.search("anything") == []


async def test_unindexed_document_is_retried_when_store_recovers(harness):
    service, _factory, store, tmp_path = harness
    note = tmp_path / "retry.md"
    note.write_text("the spare car key hangs by the pantry door")

    store.available = False
    first = await service.ingest_file(note)
    assert first.status == "unindexed"

    store.available = True
    second = await service.ingest_file(note)

    assert second.status == "updated"
    passages = await service.search("where is the spare car key?")
    assert passages and "pantry" in passages[0].content


async def test_unreadable_file_reports_failure(harness):
    service, _, _, tmp_path = harness
    missing = tmp_path / "nope.md"

    outcome = await service.ingest_file(missing)

    assert outcome.status == "failed"
    assert outcome.error


async def test_pdf_extraction(harness):
    service, _, _, tmp_path = harness
    import io

    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import ArrayObject, DictionaryObject, NameObject, StreamObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    content = StreamObject()
    content.set_data(b"BT /F1 12 Tf 10 100 Td (The projector remote lives in the red drawer) Tj ET")
    ref = writer._add_object(content)
    page[NameObject("/Contents")] = ref
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {
                    NameObject("/F1"): DictionaryObject(
                        {
                            NameObject("/Type"): NameObject("/Font"),
                            NameObject("/Subtype"): NameObject("/Type1"),
                            NameObject("/BaseFont"): NameObject("/Helvetica"),
                        }
                    )
                }
            ),
            NameObject("/ProcSet"): ArrayObject([NameObject("/PDF"), NameObject("/Text")]),
        }
    )
    buf = io.BytesIO()
    writer.write(buf)
    # Sanity: pypdf itself can read the text back out.
    assert "red drawer" in PdfReader(io.BytesIO(buf.getvalue())).pages[0].extract_text()
    pdf_path = tmp_path / "manual.pdf"
    pdf_path.write_bytes(buf.getvalue())

    outcome = await service.ingest_file(pdf_path)
    assert outcome.status == "created"

    passages = await service.search("where is the projector remote?")
    assert passages and "red drawer" in passages[0].content
