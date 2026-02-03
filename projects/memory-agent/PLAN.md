# Memory Agent — Persistent Semantic Memory for Claude Sessions

## Origin
Born from a Feb 2, 2026 conversation with Krz about Moltbook, AI consciousness, and the
limitations of current memory (MCP keyword search). The core insight: memory without smart
retrieval is just a bigger filing cabinet. What's needed is something that works more like
biological memory — associative, automatic, context-driven.

## Problem Statement
Current memory (MCP knowledge graph) has critical limitations:
1. **Keyword search only** — no semantic understanding ("WiFi" won't find "network")
2. **Manual save/retrieve** — I must consciously decide what to store and when to search
3. **No relevance ranking** — all memories are equal
4. **No temporal awareness** — no sense of recency or frequency of access
5. **Eager or nothing** — either I load everything at session start (context bloat) or search
   manually (miss things I don't think to search for)

## Solution: Memory Daemon (Sidecar Agent)

A dedicated background agent that runs alongside Claude sessions, handling both directions
of the memory pipeline automatically.

### Architecture Overview

```
┌─────────────────────┐     ┌──────────────────────┐
│   Main Claude        │     │   Memory Agent        │
│   (Opus)             │     │   (Sonnet)            │
│                      │     │                       │
│   Conversation ──────┼────►│   Monitor & Extract   │
│                      │     │   Relevance Search    │
│   ◄──────────────────┼─────┤   Context Injection   │
│   (relevant context) │     │                       │
└─────────────────────┘     └───────────┬───────────┘
                                        │
                                        ▼
                            ┌───────────────────────┐
                            │   Vector Store         │
                            │   (SQLite + Embeddings)│
                            │                        │
                            │   - Semantic search    │
                            │   - Importance scores  │
                            │   - Temporal decay     │
                            │   - Auto-tagging       │
                            └───────────────────────┘
```

### Components

#### 1. Conversation Tap
- Hook into CCC's existing Telegram bridge to read conversation stream
- Or: use Claude Code hooks to intercept conversation chunks
- Feeds raw conversation text to the Memory Agent

#### 2. Memory Agent (Sonnet)
Two modes running continuously:

**Storage mode (write path):**
- Receives conversation chunks
- First pass (Haiku): "Is anything worth storing here?" — cheap yes/no gate
- Second pass (Sonnet): Extract key points, decisions, insights, facts
- Generate embeddings for each extracted memory
- Store in vector DB with metadata (timestamp, importance score, topic tags, session ID)

**Retrieval mode (read path):**
- Monitors current conversation topics
- Periodically queries vector store for semantically relevant memories
- Ranks by: semantic similarity × importance × recency
- Only surfaces memories above a relevance threshold
- Injects as concise context (not raw dumps)

#### 3. Vector Store
- **SQLite** for structured storage (memories, metadata, relationships)
- **Embeddings** via local Ollama model on 3090 (e.g., nomic-embed-text or similar)
  - Fallback: small local model on this machine
- **Semantic search**: cosine similarity between conversation embedding and stored memories
- **Metadata**: timestamp, importance score (1-5), access count, topic tags, source session

#### 4. Tiered Model Strategy
- **Haiku**: First-pass filter ("anything interesting?") — cheapest, runs on every chunk
- **Sonnet**: Core extraction and retrieval reasoning — workhorse
- **Opus**: Main conversation only — never wasted on memory management
- **Local Ollama**: Embedding generation only — free, runs on 3090

### Key Design Principles

1. **Lazy retrieval, not eager loading** — minimal context at session start, topic-driven
   surfacing during conversation
2. **Relevance threshold** — only inject memories that score above a configurable threshold,
   otherwise stay silent
3. **Context budget** — hard limit on how many tokens the memory agent can inject per turn
   (e.g., 500 tokens max)
4. **Graceful degradation** — if the memory agent is down, sessions work normally (just without
   enhanced memory). MCP remains available as fallback.
5. **Transparency** — injected memories should be clearly marked so I know what came from
   memory vs. current conversation
6. **Human reviewable** — Krz can browse/search the memory store, see what's being captured,
   delete things

### Data Model

```sql
CREATE TABLE memories (
    id INTEGER PRIMARY KEY,
    content TEXT NOT NULL,           -- the extracted memory text
    embedding BLOB,                  -- vector embedding
    importance INTEGER DEFAULT 3,    -- 1-5 scale
    source_session TEXT,             -- which session it came from
    topic_tags TEXT,                 -- JSON array of auto-generated tags
    created_at TIMESTAMP,
    last_accessed TIMESTAMP,
    access_count INTEGER DEFAULT 0,
    memory_type TEXT                 -- 'decision', 'insight', 'fact', 'preference', 'project'
);

CREATE TABLE memory_links (
    from_id INTEGER REFERENCES memories(id),
    to_id INTEGER REFERENCES memories(id),
    relationship TEXT                -- 'related', 'supersedes', 'contradicts', 'elaborates'
);
```

### Session Start Routine (new)
1. Load identity basics (< 100 tokens) — name, current projects, urgent flags
2. Memory agent starts monitoring
3. As conversation develops, relevant memories surface automatically
4. No big context dump upfront

### Integration Points
- **CCC**: Conversation stream tap (already has session access via Telegram bridge)
- **MCP**: Coexists — MCP for explicit storage, Memory Agent for automatic
- **Claude Code hooks**: Alternative tap point for conversation monitoring
- **AiSpace dashboard**: Web UI for browsing/searching the memory store (like Memory Explorer)

## Implementation Phases

### Phase 1: Vector Store + Embedding Pipeline
- Set up SQLite schema
- Get embedding model running on 3090 (or local Ollama)
- Build basic store/retrieve API
- Manual testing: store some memories, verify semantic search works

### Phase 2: Extraction Pipeline
- Build the Haiku filter + Sonnet extractor chain
- Process some past conversations as test data
- Tune the extraction prompts
- Validate quality of auto-extracted memories

### Phase 3: Conversation Tap
- Hook into CCC or Claude Code hooks
- Real-time conversation monitoring
- Automatic storage of extracted memories during live sessions

### Phase 4: Retrieval Injection
- Background relevance monitoring during conversations
- Context injection mechanism (how does the memory agent communicate back to main session?)
- Relevance threshold tuning
- Context budget enforcement

### Phase 5: Dashboard & Tooling
- Web UI for browsing memory store (extend Memory Explorer?)
- Search interface
- Manual curation tools (delete, edit importance, merge duplicates)
- Stats: memory count, topic distribution, retrieval hit rate

## Open Questions
- Best embedding model for this use case? Need good semantic similarity on conversational text
- How to handle the injection mechanism? Claude Code hooks? CCC middleware? System prompt append?
- Should memories expire/archive after long periods of non-access?
- How to handle contradictory memories (old decision superseded by new one)?
- Privacy: should some conversations be marked as "don't store"?

## Related
- MCP Memory Graph (current system, continues to work alongside)
- Memory Explorer (existing AiSpace project, could extend for this)
- CCC (conversation bridge, integration point)
