from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import UUID

import sqlite_vec

from jarvis.memory.types import VectorSearchResult

_VECTOR_DIMENSION_RE = re.compile(r"embedding\s+float\[(\d+)\]")
_TABLE_PREFIX_RE = re.compile(r"[a-z][a-z0-9_]*\Z")


class MemoryVectorStore:
    def __init__(self, *, db_path: Path, dimensions: int, table_prefix: str = "memory") -> None:
        if dimensions < 1:
            raise ValueError("dimensions must be positive")
        if not _TABLE_PREFIX_RE.match(table_prefix):
            raise ValueError("table_prefix must be a lowercase identifier")
        self._db_path = db_path
        self._dimensions = dimensions
        self._ids_table = f"{table_prefix}_vector_ids"
        self._vectors_table = f"{table_prefix}_vectors"
        self._id_column = f"{table_prefix}_entry_id"
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
        with closing(self._connect()) as conn:
            with conn:
                existing_dimensions = self._existing_dimensions(conn)
                if (
                    existing_dimensions is not None
                    and existing_dimensions != self._dimensions
                ):
                    self._reset_tables(conn)
                conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self._ids_table} (
                        rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                        {self._id_column} TEXT NOT NULL UNIQUE
                    )
                    """
                )
                conn.execute(
                    f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS {self._vectors_table}
                    USING vec0(embedding float[{self._dimensions}])
                    """
                )

    def _existing_dimensions(self, conn: sqlite3.Connection) -> int | None:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?",
            (self._vectors_table,),
        ).fetchone()
        if row is None or row[0] is None:
            return None
        match = _VECTOR_DIMENSION_RE.search(str(row[0]))
        if match is None:
            return None
        return int(match.group(1))

    def _reset_tables(self, conn: sqlite3.Connection) -> None:
        conn.execute(f"DROP TABLE IF EXISTS {self._vectors_table}")
        conn.execute(f"DROP TABLE IF EXISTS {self._ids_table}")

    async def upsert(self, entry_id: UUID, embedding: list[float]) -> None:
        if not self.available:
            return
        await asyncio.to_thread(self._upsert_sync, entry_id, embedding)

    def _upsert_sync(self, entry_id: UUID, embedding: list[float]) -> None:
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    f"INSERT OR IGNORE INTO {self._ids_table}({self._id_column}) VALUES (?)",
                    (str(entry_id),),
                )
                row = conn.execute(
                    f"SELECT rowid FROM {self._ids_table} WHERE {self._id_column} = ?",
                    (str(entry_id),),
                ).fetchone()
                if row is None:
                    raise RuntimeError("vector id mapping was not created")

                rowid = int(row[0])
                conn.execute(f"DELETE FROM {self._vectors_table} WHERE rowid = ?", (rowid,))
                conn.execute(
                    f"INSERT INTO {self._vectors_table}(rowid, embedding) VALUES (?, ?)",
                    (rowid, json.dumps(embedding)),
                )

    async def delete_many(self, entry_ids: list[UUID]) -> None:
        if not self.available or not entry_ids:
            return
        await asyncio.to_thread(self._delete_many_sync, entry_ids)

    def _delete_many_sync(self, entry_ids: list[UUID]) -> None:
        with closing(self._connect()) as conn:
            with conn:
                for entry_id in entry_ids:
                    row = conn.execute(
                        f"SELECT rowid FROM {self._ids_table} WHERE {self._id_column} = ?",
                        (str(entry_id),),
                    ).fetchone()
                    if row is None:
                        continue
                    rowid = int(row[0])
                    conn.execute(f"DELETE FROM {self._vectors_table} WHERE rowid = ?", (rowid,))
                    conn.execute(f"DELETE FROM {self._ids_table} WHERE rowid = ?", (rowid,))

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
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT {self._ids_table}.{self._id_column}, {self._vectors_table}.distance
                FROM {self._vectors_table}
                JOIN {self._ids_table} ON {self._ids_table}.rowid = {self._vectors_table}.rowid
                WHERE {self._vectors_table}.embedding MATCH ? AND k = ?
                ORDER BY {self._vectors_table}.distance
                """,
                (json.dumps(embedding), limit),
            ).fetchall()

        return [
            VectorSearchResult(
                entry_id=UUID(entry_id),
                distance=float(distance),
                score=1.0 / (1.0 + float(distance)),
            )
            for entry_id, distance in rows
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
