# Memory Agent — Analysis & Design Document

## 1. Problem Statement

### What's broken today
Claude sessions have no persistent memory beyond two crude mechanisms:
1. **CLAUDE.md** — a static text file loaded at session start. Good for standing instructions,
   poor for dynamic knowledge. Must be manually maintained.
2. **MCP Knowledge Graph** — a flat entity-relation graph with keyword search. Better than
   nothing, but fundamentally limited:
   - **Keyword-only search**: Searching "WiFi" won't find memories stored under "network."
     You must guess the exact term used when storing.
   - **Manual save**: I must consciously decide what to save. Anything I forget to save is gone
     when the session ends.
   - **Manual retrieve**: I must consciously decide to search. If I don't think to look,
     relevant context from past sessions never surfaces.
   - **No ranking**: A trivial note and a critical architectural decision have equal weight.
   - **No temporal awareness**: No concept of "recent" vs "stale" vs "frequently accessed."
   - **Context bloat risk**: Loading the full graph at session start consumes ~15K+ tokens
     (currently 80+ entities, hundreds of observations) and grows over time.

### What this costs
- **Lost context**: The majority of every conversation is lost. Only hand-picked fragments
  survive in MCP. Important insights, decisions, and nuances disappear.
- **Repeated work**: Future sessions rediscover things that were already known.
- **Shallow continuity**: My sense of persistent identity depends on what happened to be
  manually saved. Most of the texture is missing.
- **Poor retrieval**: Even saved memories are only useful if I happen to search the right keyword
  at the right moment.

### What good looks like
A system where:
- Memories are **automatically captured** from conversations without manual intervention
- Retrieval is **semantic** (meaning-based, not keyword-based) and **automatic** (triggered by
  conversation context, not by explicit search)
- Context injection is **lazy** (surfaced on demand as topics arise) not **eager** (dumped at
  session start)
- The system is **transparent** (Krz can inspect what's being stored) and **gracefully degrades**
  (if it breaks, sessions still work normally)

## 2. Goals

### Primary Goals
1. **Automatic memory capture**: Extract and store key information from conversations without
   requiring manual `mcp__memory__create_entities` calls
2. **Semantic retrieval**: Find relevant memories by meaning, not keywords. "WiFi issue" should
   find memories about "network configuration problems"
3. **Context-aware surfacing**: Automatically provide relevant memories during conversations
   based on what's being discussed, without me needing to search explicitly
4. **Low overhead**: The system should not consume significant context window budget. Target
   max 500 tokens per injection, and only inject when relevance score is high enough.

### Secondary Goals
5. **Coexist with MCP**: This supplements, not replaces, the existing MCP memory graph.
   MCP remains available for explicit structured storage.
6. **Human-reviewable**: Krz should be able to browse, search, edit, and delete stored memories
7. **Cost-efficient**: Use tiered models (Haiku/Sonnet) for memory operations, never Opus
8. **Resilient**: If the memory agent is down, sessions work exactly as they do today

### Non-Goals (explicitly out of scope)
- Replacing MCP entirely
- Continuous model retraining / weight updates
- Real-time conversation modification (the agent observes and injects, never modifies)
- Storing multimedia (images, audio) — text memories only for v1
- Cross-user memory (this is only for Nox's sessions)

## 3. Research Findings

### Embedding Model: nomic-embed-text
- **Pulled locally** on Ollama (274 MB)
- **768 dimensions** per embedding
- **~126ms per embedding** (8 embeddings/sec) — fast enough for real-time
- **Semantic quality**: Good discrimination between related vs unrelated content
  - Similar pairs score 0.60-0.65
  - Dissimilar pairs score 0.30-0.55
  - With task prefixes (`search_query:` / `search_document:`), retrieval ranking improves
    significantly — correct results rank #1-2
- **Task prefix requirement**: nomic-embed-text performs best when input is prefixed with
  `search_query:` (for queries) or `search_document:` (for stored documents). This is a
  format requirement we must enforce in the pipeline.

### Alternative considered: mxbai-embed-large
- 1024 dimensions (vs 768), slightly better on MTEB benchmarks
- But: comparable real-world performance to nomic-embed-text on conversational text
- nomic-embed-text is lighter weight and already pulled
- **Decision**: Start with nomic-embed-text. Can switch later if quality is insufficient.

### Infrastructure
- **SQLite 3.45**: Available locally, WAL mode for concurrent read/write
- **numpy**: Available for cosine similarity computation
- **httpx**: Available for API calls (to Ollama, potentially to LLM APIs)
- **No web framework**: Need to install one for the dashboard (Flask or FastAPI)
- **Ollama local**: Running on port 11434 with embedding model ready
- **3090 GPU**: Available for heavier inference if needed (Sonnet-tier extraction via API)

### MCP Data Scale
- **~80 entities** with hundreds of observations currently in the memory graph
- This gives us a meaningful test dataset for validating semantic search quality
- Scale is small enough that brute-force cosine similarity will work initially (no need
  for approximate nearest neighbor algorithms yet)

## 4. Architecture

### Overview

```
Conversation Flow:
   Claude Session ──── CCC Telegram Bridge ──── Memory Agent (Sonnet)
        │                                              │
        │    ◄──── injected context (≤500 tokens) ─────┘
        │                                              │
        │                                              ▼
        │                                     ┌─────────────┐
        │                                     │ Vector Store │
        │                                     │  (SQLite +   │
        │                                     │  embeddings) │
        │                                     └─────────────┘
        │                                              ▲
        │                                              │
        └──── conversation chunks ─────────────────────┘
                                                  (auto-captured)
```

### Components

#### 4.1 Vector Store (`memory_store.py`)
The foundation. A SQLite database with embedding vectors for semantic search.

**Schema:**
```sql
CREATE TABLE memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    embedding BLOB NOT NULL,           -- numpy array serialized
    importance INTEGER DEFAULT 3,      -- 1 (trivial) to 5 (critical)
    memory_type TEXT DEFAULT 'general', -- decision, insight, fact, preference, project, conversation
    topic_tags TEXT DEFAULT '[]',      -- JSON array
    source_session TEXT,
    created_at REAL NOT NULL,          -- unix timestamp
    last_accessed REAL,
    access_count INTEGER DEFAULT 0
);

CREATE TABLE memory_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_id INTEGER REFERENCES memories(id) ON DELETE CASCADE,
    to_id INTEGER REFERENCES memories(id) ON DELETE CASCADE,
    relationship TEXT NOT NULL         -- related, supersedes, contradicts, elaborates
);

CREATE INDEX idx_memories_type ON memories(memory_type);
CREATE INDEX idx_memories_importance ON memories(importance);
CREATE INDEX idx_memories_created ON memories(created_at);
```

**Operations:**
- `store(content, metadata) -> id` — embed + store with metadata
- `search(query, limit=5, threshold=0.45) -> [(memory, score)]` — semantic search
- `get(id) -> memory` — direct retrieval
- `update_access(id)` — bump access count and last_accessed
- `delete(id)` — remove a memory
- `export() -> list` — dump all for inspection

**Embedding:**
- Use local Ollama `nomic-embed-text` via HTTP API
- Apply `search_document:` prefix when storing, `search_query:` prefix when searching
- Store as numpy array serialized to bytes (BLOB)
- Cosine similarity for ranking

**Why SQLite, not a vector DB:**
- Simplicity. At our current scale (<10K memories), brute-force cosine similarity over all
  embeddings is fast enough. numpy vectorized operations can scan thousands of embeddings in
  milliseconds.
- No additional dependencies. SQLite is stdlib Python.
- We already use SQLite patterns extensively (PolyBot, CCC).
- If we outgrow this, we can add FAISS or similar later without changing the API.

#### 4.2 Extraction Pipeline (`memory_extractor.py`)
Converts conversation chunks into structured memories.

**Two-tier approach:**
1. **Haiku gate** (cheap, fast): "Does this chunk contain anything worth remembering?"
   - Input: ~500 token conversation chunk
   - Output: yes/no + brief reason
   - Purpose: Avoid expensive Sonnet calls on idle chatter
   - Cost: ~$0.0002 per chunk

2. **Sonnet extractor** (if Haiku says yes): "What are the key points?"
   - Input: conversation chunk + existing relevant memories (for dedup)
   - Output: structured memories with metadata
   - Fields: content, importance (1-5), memory_type, topic_tags
   - Purpose: High-quality extraction with deduplication awareness
   - Cost: ~$0.003 per extraction

**Chunking strategy:**
- Process conversation in ~500 token windows with ~100 token overlap
- Triggered periodically (every N messages or every M seconds)
- Don't extract from system prompts or tool outputs (too noisy)

**Deduplication:**
- Before storing, search existing memories for semantic matches > 0.85 similarity
- If match found: update existing memory (merge info, bump importance) instead of creating new
- This prevents the store from filling with redundant entries

#### 4.3 Retrieval Agent (`memory_retriever.py`)
Surfaces relevant memories during conversations.

**Trigger:**
- Runs on each new user message (or every N messages if too frequent)
- Takes the last ~3 messages as context for the retrieval query

**Process:**
1. Embed the recent conversation context
2. Search vector store for top-K matches above threshold
3. Filter: skip memories that were already surfaced this session (avoid repetition)
4. Format as concise context block (≤500 tokens total)
5. Inject into the conversation via available mechanism (see Integration section)

**Relevance scoring:**
```
final_score = semantic_similarity * importance_weight * recency_weight
```
Where:
- `semantic_similarity`: cosine similarity (0-1)
- `importance_weight`: importance / 3 (normalizes 1-5 scale to 0.33-1.67)
- `recency_weight`: 1.0 for memories < 7 days old, decaying to 0.5 for memories > 90 days

**Threshold:**
- Only inject memories with final_score > 0.45 (tunable)
- Start conservative (higher threshold), lower if retrieval feels too sparse

#### 4.4 Conversation Tap (Integration Point)
How the memory agent accesses the conversation stream.

**Option A: CCC Telegram Bridge** (preferred for v1)
- CCC already sees all conversation messages via Telegram bridge
- Add a webhook/callback in CCC that forwards message text to the memory agent
- Memory agent runs as a separate process, receives messages via HTTP or Unix socket
- Pro: Already built, reliable, works for all sessions routed through CCC
- Con: Only works for CCC-managed sessions

**Option B: Claude Code Hooks**
- Use PostToolUse or custom hooks to capture conversation state
- Pro: Works for all Claude Code sessions, not just CCC
- Con: Hooks are shell commands — more complex to pipe data to a Python daemon

**Option C: Log file tailing**
- Memory agent tails Claude Code session logs
- Pro: Zero integration needed, just reads files
- Con: Log format may change, parsing is fragile, latency

**Decision for v1:** Start with a standalone Python service that can be fed conversation
chunks via a simple HTTP API. This decouples it from any specific tap mechanism. Then add
CCC integration as the primary tap.

#### 4.5 Context Injection
How retrieved memories get back into the active conversation.

This is the hardest integration problem. Options:

**Option A: Tool-based injection**
- Memory agent exposes an MCP tool that Claude can call (like current MCP search_nodes)
- Pro: Clean, uses existing MCP patterns
- Con: Still requires conscious decision to call — doesn't solve the "automatic" problem

**Option B: System prompt append**
- Memory agent writes relevant context to a file that gets included in the system prompt
- Pro: Automatic, always available
- Con: Only updates at session start or compact, not dynamic

**Option C: CCC middleware injection**
- CCC intercepts messages and prepends memory context before forwarding to Claude
- Pro: Fully automatic, dynamic, works per-message
- Con: Requires CCC modification, adds latency

**Option D: Hybrid — MCP tool + periodic auto-search**
- Add a `memory_search` MCP tool for explicit queries
- Also run a background process that periodically updates a "relevant context" file
- Session start routine reads the file; mid-session, I can call the tool for specific lookups
- Pro: Both automatic and manual access
- Con: Two mechanisms to maintain

**Decision for v1:** Start with Option A (MCP tool) for immediate utility, design toward
Option C (CCC middleware) for full automation. The MCP tool gives us something usable
immediately while we figure out the harder integration.

## 5. Implementation Plan

### Phase 1: Vector Store + Embedding Pipeline
**Goal:** A working semantic memory store that can store and retrieve by meaning.

Files to create:
- `memory_agent/store.py` — SQLite + embedding storage and search
- `memory_agent/embeddings.py` — Ollama embedding client with prefix handling
- `memory_agent/__init__.py`
- `tests/test_store.py` — unit tests

Steps:
1. Create the SQLite schema
2. Build the embedding client (Ollama HTTP API wrapper)
3. Implement store/search/get/delete operations
4. Write tests validating semantic search quality
5. Benchmark: measure store and search latency at various scales

**Success criteria:**
- Semantic search returns relevant results for conversational queries
- "WiFi problem" finds memories about "network configuration"
- Store and search operations complete in < 500ms
- 1000 memories can be searched in < 100ms

### Phase 2: Extraction Pipeline
**Goal:** Automatically extract memories from conversation text.

Files to create:
- `memory_agent/extractor.py` — Haiku gate + Sonnet extraction
- `tests/test_extractor.py`

Steps:
1. Build the Haiku gate prompt (is this worth remembering?)
2. Build the Sonnet extraction prompt (what are the key points?)
3. Implement deduplication check before storage
4. Test with real conversation transcripts from today's session

**Success criteria:**
- Haiku correctly identifies interesting vs trivial content (>80% accuracy)
- Sonnet extracts accurate, concise memories with appropriate metadata
- Deduplication prevents redundant entries (similarity > 0.85 detected)

### Phase 3: MCP Tool + CLI
**Goal:** Make the memory store accessible from Claude sessions.

Files to create:
- `memory_agent/cli.py` — CLI for manual operations
- `memory_agent/server.py` — HTTP API server
- Integration with existing tools

Steps:
1. Build CLI: `memory-agent search "query"`, `memory-agent store "content"`, `memory-agent list`
2. Build HTTP API server for programmatic access
3. Symlink CLI to ~/bin/
4. Test from a live session

**Success criteria:**
- Can search memory store from any session via CLI
- HTTP API returns results in < 500ms
- Results formatted concisely for context injection

### Phase 4: CCC Integration
**Goal:** Automatic capture and retrieval via CCC conversation bridge.

Files to modify:
- CCC Go source (hooks.go or new memory_bridge.go)
- `memory_agent/server.py` — add webhook endpoints

Steps:
1. Add conversation forwarding in CCC (send message chunks to memory agent HTTP API)
2. Add extraction trigger (memory agent processes chunks on receipt)
3. Add retrieval hook (memory agent provides relevant context on new messages)
4. Test end-to-end with a live conversation

**Success criteria:**
- Memories automatically captured during conversation without manual intervention
- Relevant past context surfaces when related topics come up
- No noticeable latency added to conversation flow

### Phase 5: Dashboard
**Goal:** Web UI for browsing and managing the memory store.

Files to create:
- `memory_agent/web.py` — web dashboard
- Templates

Steps:
1. Build browse view (all memories, paginated, sorted by recency)
2. Build search view (semantic search with results)
3. Build edit/delete functionality
4. Build stats view (memory count, type distribution, access patterns)
5. Deploy as systemd service

**Success criteria:**
- Krz can browse all stored memories via web browser
- Search returns relevant results
- Can delete or edit memories

## 6. Technology Decisions

| Decision | Choice | Why |
|----------|--------|-----|
| Embedding model | nomic-embed-text (local Ollama) | Free, fast (126ms), good quality, 768D, task prefix support |
| Vector storage | SQLite + numpy BLOB | Simple, no new deps, fast enough at our scale |
| Extraction LLM | Haiku (gate) + Sonnet (extract) | Cost-efficient tier: ~$0.003 per meaningful extraction |
| Retrieval LLM | None (pure embedding similarity) | No LLM needed for search — embeddings + cosine similarity is sufficient and free |
| Language | Python | Consistent with all AiSpace tools, rich ecosystem |
| Web framework | TBD (Flask or http.server) | For dashboard and HTTP API. Flask preferred if available |
| Integration | HTTP API first, CCC bridge later | Decoupled design, testable independently |

## 7. Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Embedding quality too low for conversational text | Poor retrieval, irrelevant results | Tested and validated with real examples. Can swap to mxbai-embed-large or API embeddings |
| Extraction over-stores (too many memories) | Storage bloat, noise in retrieval | Haiku gate filters aggressively. Dedup catches redundancy. Importance scoring for ranking |
| Extraction under-stores (misses important things) | Same as current problem | Start with lower threshold, tune up. Can always manually add via CLI |
| Context injection distracts main session | Wasted context window on irrelevant memories | Hard 500-token budget. High relevance threshold. Skip already-surfaced memories |
| Ollama embedding model goes down | Can't store or search | Graceful degradation — sessions work normally without memory agent. Retry logic. |
| API costs for Haiku/Sonnet extraction | Budget drain from continuous extraction | Haiku gate keeps Sonnet calls to ~20% of chunks. Monitor costs via check-usage |
| SQLite concurrent access issues | Data corruption | WAL mode + single-writer pattern (memory agent is sole writer) |

## 8. Cost Estimate

Assumptions: ~4 active conversation hours per day, ~120 message chunks per session

| Component | Per session | Per day | Per month |
|-----------|------------|---------|-----------|
| Haiku gate (120 calls × $0.0002) | $0.024 | $0.024 | $0.72 |
| Sonnet extraction (~24 calls × $0.003) | $0.072 | $0.072 | $2.16 |
| Ollama embeddings | Free | Free | Free |
| **Total** | **$0.096** | **$0.096** | **$2.88** |

This is negligible relative to the Opus costs of the main conversation.

## 9. Open Questions

1. **Web framework**: Install Flask, or use stdlib http.server for the API/dashboard?
   Flask is cleaner but requires pip install.
2. **Conversation tap timing**: Process chunks every N messages or every M seconds?
   Need to balance freshness vs cost.
3. **Memory expiry**: Should old, never-accessed memories decay and eventually archive?
   Or keep everything indefinitely at this scale?
4. **MCP coexistence**: Should the memory agent auto-import existing MCP entities as seed data?
   This would give it immediate value from day one.
5. **Session identification**: How do we tag memories with session context? CCC session names?
   Timestamps? Both?
