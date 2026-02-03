"""Semantic memory store backed by SQLite and vector embeddings."""

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from .embeddings import EmbeddingClient, batch_cosine_similarity

# Default database location
DEFAULT_DB_PATH = Path.home() / ".memory-agent" / "memories.db"


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
    """

    def __init__(
        self,
        db_path: Optional[Path] = None,
        embedding_client: Optional[EmbeddingClient] = None,
    ):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.embedder = embedding_client or EmbeddingClient()
        self._init_db()

    def _init_db(self):
        """Initialize database schema."""
        conn = self._connect()
        conn.executescript(self.SCHEMA)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()
        conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA foreign_keys=ON")
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
        now = time.time()

        conn = self._connect()
        cursor = conn.execute(
            """INSERT INTO memories
               (content, embedding, importance, memory_type, topic_tags, source_session, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                content,
                self._serialize_embedding(embedding),
                max(1, min(5, importance)),
                memory_type,
                json.dumps(topic_tags or []),
                source_session,
                now,
            ),
        )
        memory_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return memory_id

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

        Args:
            query: Natural language search query
            limit: Maximum results to return
            threshold: Minimum combined relevance score (0-1)
            memory_type: Filter by type (None = all types)
            min_importance: Minimum importance level
            exclude_ids: Set of memory IDs to skip

        Returns:
            List of SearchResult sorted by relevance score (descending)
        """
        query_embedding = self.embedder.embed(query, prefix="search_query")
        exclude_ids = exclude_ids or set()

        conn = self._connect()

        # Build query with optional filters
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

        # Extract embeddings and compute similarities in batch
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

        # Score with importance and recency weighting
        # Importance is a gentle nudge (0.85 to 1.15), not a dominant factor.
        # Semantic similarity should drive ranking; importance is a tiebreaker.
        now = time.time()
        results = []
        for i, (memory, sim) in enumerate(zip(memories, similarities)):
            sim = float(sim)
            importance_weight = 0.85 + (memory.importance * 0.06)  # 0.91 to 1.15
            age_days = (now - memory.created_at) / 86400
            recency_weight = max(0.7, 1.0 - (age_days / 180) * 0.3)

            score = sim * importance_weight * recency_weight

            if score >= threshold:
                results.append(SearchResult(
                    memory=memory,
                    score=score,
                    similarity=sim,
                ))

        results.sort(key=lambda r: r.score, reverse=True)

        # Update access counts for returned results
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

        if content is not None and content != memory.content:
            embedding = self.embedder.embed(content, prefix="search_document")
            updates.extend(["content = ?", "embedding = ?"])
            params.extend([content, self._serialize_embedding(embedding)])

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
        conn.close()

        return {
            "total_memories": total,
            "total_links": links,
            "by_type": by_type,
            "by_importance": by_importance,
            "avg_importance": round(avg_importance, 2),
            "oldest": oldest,
            "newest": newest,
        }
