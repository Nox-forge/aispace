"""Semantic memory store backed by SQLite and vector embeddings.

Uses sqlite-vec for SIMD-accelerated vector search when available,
falls back to numpy batch cosine similarity otherwise.
"""

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from .embeddings import EmbeddingClient, batch_cosine_similarity

# Default database location
DEFAULT_DB_PATH = Path.home() / ".memory-agent" / "memories.db"

log = logging.getLogger("memory-agent")

# Try to load sqlite-vec
try:
    import sqlite_vec
    HAS_SQLITE_VEC = True
except ImportError:
    HAS_SQLITE_VEC = False

# nomic-embed-text produces 768-dimensional embeddings
EMBEDDING_DIM = 768


@dataclass
class Memory:
    """A single memory entry."""
    id: int
    content: str
    importance: int  # 1-5
    memory_type: str  # decision, insight, fact, preference, project, conversation, general
    topic_tags: list[str]
    source_session: str
    created_at: float
    last_accessed: Optional[float]
    access_count: int

    @property
    def age_days(self) -> float:
        return (time.time() - self.created_at) / 86400


@dataclass
class SearchResult:
    """A memory with its relevance score."""
    memory: Memory
    score: float  # combined relevance score
    similarity: float  # raw cosine similarity


class MemoryStore:
    """Semantic memory store with embedding-based search."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content TEXT NOT NULL,
        embedding BLOB NOT NULL,
        importance INTEGER DEFAULT 3 CHECK(importance >= 1 AND importance <= 5),
        memory_type TEXT DEFAULT 'general',
        topic_tags TEXT DEFAULT '[]',
        source_session TEXT DEFAULT '',
        created_at REAL NOT NULL,
        last_accessed REAL,
        access_count INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS memory_links (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_id INTEGER REFERENCES memories(id) ON DELETE CASCADE,
        to_id INTEGER REFERENCES memories(id) ON DELETE CASCADE,
        relationship TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(memory_type);
    CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(importance);
    CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at);
    CREATE INDEX IF NOT EXISTS idx_memory_links_from ON memory_links(from_id);
    CREATE INDEX IF NOT EXISTS idx_memory_links_to ON memory_links(to_id);

    CREATE TABLE IF NOT EXISTS raw_chunks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session TEXT NOT NULL DEFAULT '',
        chunk_text TEXT NOT NULL,
        chunk_index INTEGER DEFAULT 0,
        ingested_at REAL NOT NULL,
        memory_ids TEXT DEFAULT '[]'
    );

    CREATE INDEX IF NOT EXISTS idx_raw_chunks_session ON raw_chunks(session);
    CREATE INDEX IF NOT EXISTS idx_raw_chunks_ingested ON raw_chunks(ingested_at);
    """

    def __init__(
        self,
        db_path: Optional[Path] = None,
        embedding_client: Optional[EmbeddingClient] = None,
    ):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.embedder = embedding_client or EmbeddingClient()
        self.use_vec = HAS_SQLITE_VEC
        self._init_db()

    def _init_db(self):
        """Initialize database schema."""
        conn = self._connect()
        conn.executescript(self.SCHEMA)
        conn.execute("PRAGMA journal_mode=WAL")

        if self.use_vec:
            try:
                conn.execute(f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec
                    USING vec0(embedding float[{EMBEDDING_DIM}] distance_metric=cosine)
                """)
                conn.commit()
                # Sync vec table with any memories that aren't indexed yet
                self._sync_vec_index(conn)
            except Exception as e:
                log.warning("sqlite-vec init failed, falling back to numpy: %s", e)
                self.use_vec = False

        conn.commit()
        conn.close()

    def _sync_vec_index(self, conn: sqlite3.Connection):
        """Ensure all memories have entries in the vec0 table."""
        # Find memories missing from vec index
        missing = conn.execute("""
            SELECT m.id, m.embedding FROM memories m
            LEFT JOIN memory_vec v ON m.id = v.rowid
            WHERE v.rowid IS NULL
        """).fetchall()

        if missing:
            log.info("Syncing %d memories to sqlite-vec index...", len(missing))
            for mem_id, embedding_blob in missing:
                try:
                    conn.execute(
                        "INSERT INTO memory_vec(rowid, embedding) VALUES (?, ?)",
                        (mem_id, embedding_blob),
                    )
                except Exception:
                    pass  # Skip any that fail (e.g., wrong dimension)
            conn.commit()
            log.info("Vec index sync complete")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        if self.use_vec:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        return conn

    def _serialize_embedding(self, embedding: np.ndarray) -> bytes:
        return embedding.astype(np.float32).tobytes()

    def _deserialize_embedding(self, blob: bytes) -> np.ndarray:
        return np.frombuffer(blob, dtype=np.float32)

    def _row_to_memory(self, row: tuple) -> Memory:
        return Memory(
            id=row[0],
            content=row[1],
            importance=row[2],
            memory_type=row[3],
            topic_tags=json.loads(row[4]),
            source_session=row[5],
            created_at=row[6],
            last_accessed=row[7],
            access_count=row[8],
        )

    def store(
        self,
        content: str,
        importance: int = 3,
        memory_type: str = "general",
        topic_tags: Optional[list[str]] = None,
        source_session: str = "",
    ) -> int:
        """Store a new memory with its embedding.

        Returns the memory ID.
        """
        embedding = self.embedder.embed(content, prefix="search_document")
        embedding_blob = self._serialize_embedding(embedding)
        now = time.time()

        conn = self._connect()
        cursor = conn.execute(
            """INSERT INTO memories
               (content, embedding, importance, memory_type, topic_tags, source_session, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                content,
                embedding_blob,
                max(1, min(5, importance)),
                memory_type,
                json.dumps(topic_tags or []),
                source_session,
                now,
            ),
        )
        memory_id = cursor.lastrowid

        # Also insert into vec index
        if self.use_vec:
            try:
                conn.execute(
                    "INSERT INTO memory_vec(rowid, embedding) VALUES (?, ?)",
                    (memory_id, embedding_blob),
                )
            except Exception as e:
                log.warning("Failed to insert into vec index: %s", e)

        conn.commit()
        conn.close()
        return memory_id

    @staticmethod
    def _compute_score(similarity: float, memory: Memory) -> float:
        """Compute relevance score from similarity and memory metadata.

        Factors:
          - similarity: raw cosine similarity (dominant factor)
          - importance: higher importance = slightly higher score
          - recency: newer memories score higher (exponential decay)
          - access: frequently accessed memories get a small boost
        """
        import math

        # Importance factor: range 0.88 (imp=1) to 1.20 (imp=5)
        importance_factor = 0.80 + (memory.importance * 0.08)

        # Recency factor: exponential decay with ~120-day half-life
        # range: ~0.5 (very old) to 1.0 (brand new)
        now = time.time()
        age_days = (now - memory.created_at) / 86400
        recency_factor = 0.5 + 0.5 * math.exp(-age_days / 120)

        # Access factor: small boost for frequently accessed memories
        # range: 1.0 (never accessed) to ~1.15 (heavily accessed)
        access_factor = 1.0 + min(0.15, math.log1p(memory.access_count) * 0.04)

        return similarity * importance_factor * recency_factor * access_factor

    def search(
        self,
        query: str,
        limit: int = 5,
        threshold: float = 0.40,
        memory_type: Optional[str] = None,
        min_importance: int = 1,
        exclude_ids: Optional[set[int]] = None,
    ) -> list[SearchResult]:
        """Search memories by semantic similarity.

        Uses sqlite-vec SIMD KNN search when available, otherwise falls back
        to numpy batch cosine similarity.
        """
        query_embedding = self.embedder.embed(query, prefix="search_query")
        exclude_ids = exclude_ids or set()

        if self.use_vec:
            return self._search_vec(
                query_embedding, limit, threshold,
                memory_type, min_importance, exclude_ids,
            )
        else:
            return self._search_numpy(
                query_embedding, limit, threshold,
                memory_type, min_importance, exclude_ids,
            )

    def _search_vec(
        self,
        query_embedding: np.ndarray,
        limit: int,
        threshold: float,
        memory_type: Optional[str],
        min_importance: int,
        exclude_ids: set[int],
    ) -> list[SearchResult]:
        """Search using sqlite-vec SIMD-accelerated KNN."""
        conn = self._connect()
        query_blob = self._serialize_embedding(query_embedding)

        # Fetch more candidates than needed to account for metadata filtering
        fetch_limit = max(limit * 5, 50)

        # KNN search via vec0 virtual table
        vec_rows = conn.execute(
            """SELECT v.rowid, v.distance
               FROM memory_vec v
               WHERE v.embedding MATCH ?
               ORDER BY v.distance
               LIMIT ?""",
            (query_blob, fetch_limit),
        ).fetchall()

        if not vec_rows:
            conn.close()
            return []

        # Get memory metadata for candidates
        candidate_ids = [row[0] for row in vec_rows]
        distances = {row[0]: row[1] for row in vec_rows}

        placeholders = ",".join("?" * len(candidate_ids))
        mem_rows = conn.execute(
            f"""SELECT id, content, importance, memory_type, topic_tags,
                       source_session, created_at, last_accessed, access_count
                FROM memories WHERE id IN ({placeholders})""",
            candidate_ids,
        ).fetchall()
        conn_for_update = conn  # Keep connection open for access count updates

        # Apply metadata filters and scoring
        results = []
        for row in mem_rows:
            mem = self._row_to_memory(row)

            if mem.id in exclude_ids:
                continue
            if memory_type and mem.memory_type != memory_type:
                continue
            if mem.importance < min_importance:
                continue

            # Convert cosine distance to similarity (distance 0 = identical)
            cosine_distance = distances.get(mem.id, 1.0)
            sim = 1.0 - cosine_distance
            score = self._compute_score(sim, mem)

            if score >= threshold:
                results.append(SearchResult(memory=mem, score=score, similarity=sim))

        results.sort(key=lambda r: r.score, reverse=True)

        # Update access counts
        if results:
            access_time = time.time()
            for r in results[:limit]:
                conn_for_update.execute(
                    "UPDATE memories SET last_accessed = ?, access_count = access_count + 1 WHERE id = ?",
                    (access_time, r.memory.id),
                )
            conn_for_update.commit()

        conn_for_update.close()
        return results[:limit]

    def _search_numpy(
        self,
        query_embedding: np.ndarray,
        limit: int,
        threshold: float,
        memory_type: Optional[str],
        min_importance: int,
        exclude_ids: set[int],
    ) -> list[SearchResult]:
        """Fallback search using numpy batch cosine similarity."""
        conn = self._connect()

        where_clauses = ["importance >= ?"]
        params: list = [min_importance]
        if memory_type:
            where_clauses.append("memory_type = ?")
            params.append(memory_type)

        where_sql = " AND ".join(where_clauses)
        rows = conn.execute(
            f"""SELECT id, content, importance, memory_type, topic_tags,
                       source_session, created_at, last_accessed, access_count, embedding
                FROM memories WHERE {where_sql}""",
            params,
        ).fetchall()
        conn.close()

        if not rows:
            return []

        memories = []
        embeddings = []
        for row in rows:
            if row[0] in exclude_ids:
                continue
            memories.append(self._row_to_memory(row[:9]))
            embeddings.append(self._deserialize_embedding(row[9]))

        if not memories:
            return []

        embedding_matrix = np.array(embeddings)
        similarities = batch_cosine_similarity(query_embedding, embedding_matrix)

        now = time.time()
        results = []
        for memory, sim in zip(memories, similarities):
            sim = float(sim)
            score = self._compute_score(sim, memory)

            if score >= threshold:
                results.append(SearchResult(memory=memory, score=score, similarity=sim))

        results.sort(key=lambda r: r.score, reverse=True)

        if results:
            conn = self._connect()
            for r in results[:limit]:
                conn.execute(
                    "UPDATE memories SET last_accessed = ?, access_count = access_count + 1 WHERE id = ?",
                    (now, r.memory.id),
                )
            conn.commit()
            conn.close()

        return results[:limit]

    def get(self, memory_id: int) -> Optional[Memory]:
        """Get a single memory by ID."""
        conn = self._connect()
        row = conn.execute(
            """SELECT id, content, importance, memory_type, topic_tags,
                      source_session, created_at, last_accessed, access_count
               FROM memories WHERE id = ?""",
            (memory_id,),
        ).fetchone()
        conn.close()

        if not row:
            return None
        return self._row_to_memory(row)

    def delete(self, memory_id: int) -> bool:
        """Delete a memory by ID. Returns True if deleted."""
        conn = self._connect()
        cursor = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        deleted = cursor.rowcount > 0

        if deleted and self.use_vec:
            try:
                conn.execute("DELETE FROM memory_vec WHERE rowid = ?", (memory_id,))
            except Exception:
                pass

        conn.commit()
        conn.close()
        return deleted

    def update(
        self,
        memory_id: int,
        content: Optional[str] = None,
        importance: Optional[int] = None,
        topic_tags: Optional[list[str]] = None,
    ) -> bool:
        """Update a memory's content and/or metadata.

        If content changes, the embedding is regenerated.
        """
        conn = self._connect()
        memory = self.get(memory_id)
        if not memory:
            conn.close()
            return False

        updates = []
        params = []
        new_embedding_blob = None

        if content is not None and content != memory.content:
            embedding = self.embedder.embed(content, prefix="search_document")
            new_embedding_blob = self._serialize_embedding(embedding)
            updates.extend(["content = ?", "embedding = ?"])
            params.extend([content, new_embedding_blob])

        if importance is not None:
            updates.append("importance = ?")
            params.append(max(1, min(5, importance)))

        if topic_tags is not None:
            updates.append("topic_tags = ?")
            params.append(json.dumps(topic_tags))

        if not updates:
            conn.close()
            return False

        params.append(memory_id)
        conn.execute(
            f"UPDATE memories SET {', '.join(updates)} WHERE id = ?",
            params,
        )

        # Update vec index if embedding changed
        if new_embedding_blob and self.use_vec:
            try:
                conn.execute("DELETE FROM memory_vec WHERE rowid = ?", (memory_id,))
                conn.execute(
                    "INSERT INTO memory_vec(rowid, embedding) VALUES (?, ?)",
                    (memory_id, new_embedding_blob),
                )
            except Exception:
                pass

        conn.commit()
        conn.close()
        return True

    def find_duplicates(self, content: str, threshold: float = 0.85) -> list[SearchResult]:
        """Find existing memories semantically similar to content.

        Used for deduplication before storing new memories.
        """
        return self.search(content, limit=3, threshold=threshold)

    def link(self, from_id: int, to_id: int, relationship: str) -> bool:
        """Create a link between two memories."""
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO memory_links (from_id, to_id, relationship) VALUES (?, ?, ?)",
                (from_id, to_id, relationship),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False
        finally:
            conn.close()

    def get_links(self, memory_id: int) -> list[tuple[int, str]]:
        """Get all links from a memory. Returns [(linked_id, relationship)]."""
        conn = self._connect()
        rows = conn.execute(
            "SELECT to_id, relationship FROM memory_links WHERE from_id = ?",
            (memory_id,),
        ).fetchall()
        conn.close()
        return [(row[0], row[1]) for row in rows]

    def count(self) -> int:
        """Return total number of memories."""
        conn = self._connect()
        count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        conn.close()
        return count

    def list_all(
        self,
        limit: int = 50,
        offset: int = 0,
        sort_by: str = "created_at",
        descending: bool = True,
    ) -> list[Memory]:
        """List memories with pagination."""
        valid_sorts = {"created_at", "importance", "access_count", "last_accessed"}
        if sort_by not in valid_sorts:
            sort_by = "created_at"

        direction = "DESC" if descending else "ASC"
        conn = self._connect()
        rows = conn.execute(
            f"""SELECT id, content, importance, memory_type, topic_tags,
                       source_session, created_at, last_accessed, access_count
                FROM memories ORDER BY {sort_by} {direction} LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
        conn.close()
        return [self._row_to_memory(row) for row in rows]

    def store_raw_chunk(
        self,
        chunk_text: str,
        session: str = "",
        chunk_index: int = 0,
        memory_ids: Optional[list[int]] = None,
    ) -> int:
        """Store a raw conversation chunk for future reprocessing.

        Returns the raw chunk ID.
        """
        conn = self._connect()
        cursor = conn.execute(
            """INSERT INTO raw_chunks (session, chunk_text, chunk_index, ingested_at, memory_ids)
               VALUES (?, ?, ?, ?, ?)""",
            (session, chunk_text, chunk_index, time.time(), json.dumps(memory_ids or [])),
        )
        chunk_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return chunk_id

    def raw_chunk_stats(self) -> dict:
        """Return statistics about stored raw chunks."""
        conn = self._connect()
        total = conn.execute("SELECT COUNT(*) FROM raw_chunks").fetchone()[0]
        total_chars = conn.execute("SELECT COALESCE(SUM(LENGTH(chunk_text)), 0) FROM raw_chunks").fetchone()[0]
        sessions = conn.execute("SELECT DISTINCT session FROM raw_chunks").fetchall()
        conn.close()
        return {
            "total_chunks": total,
            "total_chars": total_chars,
            "sessions": [s[0] for s in sessions],
        }

    def stats(self) -> dict:
        """Return statistics about the memory store."""
        conn = self._connect()
        total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        by_type = dict(conn.execute(
            "SELECT memory_type, COUNT(*) FROM memories GROUP BY memory_type"
        ).fetchall())
        by_importance = dict(conn.execute(
            "SELECT importance, COUNT(*) FROM memories GROUP BY importance"
        ).fetchall())
        avg_importance = conn.execute(
            "SELECT AVG(importance) FROM memories"
        ).fetchone()[0] or 0
        oldest = conn.execute(
            "SELECT MIN(created_at) FROM memories"
        ).fetchone()[0]
        newest = conn.execute(
            "SELECT MAX(created_at) FROM memories"
        ).fetchone()[0]
        links = conn.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0]

        vec_info = {}
        if self.use_vec:
            vec_count = conn.execute("SELECT COUNT(*) FROM memory_vec").fetchone()[0]
            vec_info["vec_indexed"] = vec_count
            vec_info["vec_backend"] = "sqlite-vec"
        else:
            vec_info["vec_backend"] = "numpy"

        raw_chunks = conn.execute("SELECT COUNT(*) FROM raw_chunks").fetchone()[0]
        conn.close()

        return {
            "total_memories": total,
            "total_links": links,
            "raw_chunks": raw_chunks,
            "by_type": by_type,
            "by_importance": by_importance,
            "avg_importance": round(avg_importance, 2),
            "oldest": oldest,
            "newest": newest,
            **vec_info,
        }
