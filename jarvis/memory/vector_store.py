from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from uuid import UUID

import sqlite_vec

from jarvis.memory.types import VectorSearchResult


class MemoryVectorStore:
    def __init__(self, *, db_path: Path, dimensions: int) -> None:
        self._db_path = db_path
        self._dimensions = dimensions
        self.available = False
        self.last_error: str | None = None

    async def initialize(self) -> None:
        try:
            await asyncio.to_thread(self._initialize_sync)
        except Exception as exc:  # pragma: no cover - exercised by integration test
            self.available = False
            self.last_error = str(exc)
        else:
            self.available = True
            self.last_error = None

    def _initialize_sync(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_vector_ids (
                    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_entry_id TEXT NOT NULL UNIQUE
                )
                """
            )
            conn.execute(
                f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_vectors
                USING vec0(embedding float[{self._dimensions}])
                """
            )

    async def upsert(self, memory_entry_id: UUID, embedding: list[float]) -> None:
        if not self.available:
            return
        await asyncio.to_thread(self._upsert_sync, memory_entry_id, embedding)

    def _upsert_sync(self, memory_entry_id: UUID, embedding: list[float]) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO memory_vector_ids(memory_entry_id) VALUES (?)",
                (str(memory_entry_id),),
            )
            row = conn.execute(
                "SELECT rowid FROM memory_vector_ids WHERE memory_entry_id = ?",
                (str(memory_entry_id),),
            ).fetchone()
            if row is None:
                raise RuntimeError("memory vector id mapping was not created")

            rowid = int(row[0])
            conn.execute("DELETE FROM memory_vectors WHERE rowid = ?", (rowid,))
            conn.execute(
                "INSERT INTO memory_vectors(rowid, embedding) VALUES (?, ?)",
                (rowid, json.dumps(embedding)),
            )

    async def search(
        self,
        embedding: list[float],
        *,
        limit: int,
    ) -> list[VectorSearchResult]:
        if not self.available or limit <= 0:
            return []
        return await asyncio.to_thread(self._search_sync, embedding, limit)

    def _search_sync(
        self,
        embedding: list[float],
        limit: int,
    ) -> list[VectorSearchResult]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT memory_vector_ids.memory_entry_id, memory_vectors.distance
                FROM memory_vectors
                JOIN memory_vector_ids ON memory_vector_ids.rowid = memory_vectors.rowid
                WHERE memory_vectors.embedding MATCH ? AND k = ?
                ORDER BY memory_vectors.distance
                """,
                (json.dumps(embedding), limit),
            ).fetchall()

        return [
            VectorSearchResult(
                memory_entry_id=UUID(memory_entry_id),
                distance=float(distance),
                score=1.0 / (1.0 + float(distance)),
            )
            for memory_entry_id, distance in rows
        ]

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        try:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        except Exception:
            conn.close()
            raise
        return conn
