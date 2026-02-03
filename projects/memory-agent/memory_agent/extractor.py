"""Conversation memory extractor — turns conversation chunks into structured memories.

Uses a two-tier approach:
1. Gate model (cheap/fast): decides if a chunk contains anything worth remembering
2. Extractor model (smarter): extracts structured memories with metadata

Both can use local Ollama models (free) or cloud APIs (configurable).
"""

import json
import os
import time
import requests
from dataclasses import dataclass
from typing import Optional

from .store import MemoryStore

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
LOCAL_OLLAMA = "http://127.0.0.1:11434"
REMOTE_OLLAMA = "http://192.168.53.108:11434"

ENV_FILE = os.path.expanduser("~/.env")


def _load_env_key(key: str) -> Optional[str]:
    """Load an API key from ~/.env file."""
    try:
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return os.environ.get(key)


# ---------------------------------------------------------------------------
# LLM backends
# ---------------------------------------------------------------------------

def _call_ollama(prompt: str, system: str = "", model: str = "qwen3:8b",
                 base_url: str = LOCAL_OLLAMA, temperature: float = 0.3) -> str:
    """Call an Ollama model and return the response text."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    resp = requests.post(
        f"{base_url}/api/chat",
        json={
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": 1024},
        },
        timeout=300,
    )
    resp.raise_for_status()
    data = resp.json()
    content = data.get("message", {}).get("content", "")
    # Strip think blocks from qwen3 models
    if "</think>" in content:
        content = content.split("</think>")[-1].strip()
    return content


def _call_anthropic(prompt: str, system: str = "",
                    model: str = "claude-haiku-3-20240307",
                    temperature: float = 0.3) -> str:
    """Call Anthropic API and return the response text."""
    api_key = _load_env_key("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("No ANTHROPIC_API_KEY found")

    messages = [{"role": "user", "content": prompt}]
    payload = {
        "model": model,
        "max_tokens": 2048,
        "temperature": temperature,
        "messages": messages,
    }
    if system:
        payload["system"] = system

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json=payload,
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("content", [{}])[0].get("text", "")


def _call_gemini(prompt: str, system: str = "",
                 model: str = "gemini-2.5-flash",
                 temperature: float = 0.3) -> str:
    """Call Gemini API and return the response text."""
    api_key = _load_env_key("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("No GEMINI_API_KEY found")

    contents = []
    if system:
        # Gemini uses systemInstruction for system prompts
        pass  # handled below

    contents.append({
        "role": "user",
        "parts": [{"text": prompt}],
    })

    payload = {
        "contents": contents,
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": 2048,
        },
    }
    if system:
        payload["systemInstruction"] = {
            "parts": [{"text": system}],
        }

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    resp = requests.post(url, json=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()

    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        return ""


# ---------------------------------------------------------------------------
# Gate — decides if a conversation chunk is worth remembering
# ---------------------------------------------------------------------------

GATE_SYSTEM = """You are a memory filter. Your job is to decide if a conversation chunk contains anything worth storing as a long-term memory.

Worth remembering:
- Decisions made (chose X over Y, decided not to do Z)
- New information learned (technical facts, how things work)
- Insights or realizations (philosophical, technical, creative)
- Project plans, goals, or status updates
- Preferences expressed (likes, dislikes, approaches preferred)
- Important context about people, systems, or tools
- Problems encountered and solutions found

NOT worth remembering:
- Casual greetings, pleasantries, filler
- Tool outputs, raw data dumps, error messages
- Repetitive back-and-forth during debugging
- Questions without answers (unless the question itself is important)
- Content that's purely procedural with no lasting value

Respond with ONLY a JSON object:
{"remember": true/false, "reason": "brief explanation"}"""

GATE_PROMPT = """Evaluate this conversation chunk:

---
{chunk}
---

Should any part of this be saved as a long-term memory?"""


def gate(chunk: str, backend: str = "local", model: Optional[str] = None) -> tuple[bool, str]:
    """Decide if a conversation chunk is worth remembering.

    Returns (should_remember, reason).
    """
    prompt = GATE_PROMPT.format(chunk=chunk[:2000])  # cap input size

    if backend == "local":
        response = _call_ollama(prompt, GATE_SYSTEM,
                                model=model or "qwen3:4b",
                                base_url=LOCAL_OLLAMA)
    elif backend == "remote":
        response = _call_ollama(prompt, GATE_SYSTEM,
                                model=model or "qwen3:8b",
                                base_url=REMOTE_OLLAMA)
    elif backend == "anthropic":
        response = _call_anthropic(prompt, GATE_SYSTEM,
                                   model=model or "claude-haiku-3-20240307")
    elif backend == "gemini":
        response = _call_gemini(prompt, GATE_SYSTEM,
                                model=model or "gemini-2.5-flash")
    else:
        raise ValueError(f"Unknown backend: {backend}")

    # Parse response — strip markdown code fences if present
    try:
        text = response.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1]
            if "```" in text:
                text = text[:text.rindex("```")]
            text = text.strip()
        if "{" in text:
            json_str = text[text.index("{"):text.rindex("}") + 1]
            data = json.loads(json_str)
            return bool(data.get("remember", False)), data.get("reason", "")
    except (json.JSONDecodeError, ValueError):
        pass

    # Fallback: look for keywords
    lower = response.lower()
    if "true" in lower or "yes" in lower or "remember" in lower:
        return True, response[:100]
    return False, response[:100]


# ---------------------------------------------------------------------------
# Extractor — extracts structured memories from conversation chunks
# ---------------------------------------------------------------------------

EXTRACT_SYSTEM = """You are a memory manager. Given a conversation chunk and a list of existing related memories, decide what operations to perform.

Operations:
- "create": Store a NEW memory that doesn't exist yet
- "update": Modify an EXISTING memory with refined, corrected, or expanded information

For CREATE operations, provide:
- op: "create"
- content: A clear, standalone statement (should make sense without context)
- importance: 1 (trivial) to 5 (critical decision or insight)
- memory_type: one of [decision, insight, fact, preference, project, conversation]
- topic_tags: 1-3 short tags

For UPDATE operations, provide:
- op: "update"
- memory_id: The ID number of the existing memory to update
- content: The NEW full content (replaces the old content entirely)
- importance: Updated importance level (or same as before)

Rules:
- Prefer UPDATE when the conversation refines, corrects, or expands an existing memory
- Only CREATE genuinely new information not covered by existing memories
- Each memory should be a single, atomic piece of information
- Write memories as clear declarative statements, not conversation fragments
- Be concise but complete — future retrieval depends on the wording
- Include WHO, WHAT, WHY when relevant
- 0-5 operations per chunk (don't over-extract)
- If nothing new is worth remembering, return an empty array []

Respond with ONLY a JSON array:
[{"op": "create", "content": "...", "importance": N, "memory_type": "...", "topic_tags": ["...", "..."]},
 {"op": "update", "memory_id": N, "content": "updated content...", "importance": N}]"""

EXTRACT_PROMPT = """Here are existing related memories (if any):
{existing_memories}

Extract memory operations from this conversation:

---
{chunk}
---"""


@dataclass
class MemoryOp:
    """A memory operation extracted from conversation text."""
    op: str  # "create" or "update"
    content: str
    importance: int
    memory_type: str
    topic_tags: list[str]
    target_id: Optional[int] = None  # For update ops


# Keep backward compatibility
ExtractedMemory = MemoryOp


def _parse_llm_json(response: str) -> list:
    """Parse JSON array from LLM response, handling markdown fences."""
    text = response.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if "```" in text:
            text = text[:text.rindex("```")]
        text = text.strip()
    if "[" in text:
        json_str = text[text.index("["):text.rindex("]") + 1]
        return json.loads(json_str)
    return []


def extract(chunk: str, existing_memories: str = "",
            backend: str = "local", model: Optional[str] = None) -> list[MemoryOp]:
    """Extract memory operations from a conversation chunk.

    Args:
        chunk: Conversation text to extract from
        existing_memories: Formatted existing memories with IDs for reconciliation
        backend: "local", "remote", "anthropic", or "gemini"
        model: Override model name

    Returns:
        List of memory operations (create/update)
    """
    if not existing_memories:
        existing_memories = "(none)"

    prompt = EXTRACT_PROMPT.format(
        chunk=chunk[:3000],
        existing_memories=existing_memories,
    )

    if backend == "local":
        response = _call_ollama(prompt, EXTRACT_SYSTEM,
                                model=model or "qwen3:8b",
                                base_url=LOCAL_OLLAMA)
    elif backend == "remote":
        response = _call_ollama(prompt, EXTRACT_SYSTEM,
                                model=model or "qwen3:32b",
                                base_url=REMOTE_OLLAMA)
    elif backend == "anthropic":
        response = _call_anthropic(prompt, EXTRACT_SYSTEM,
                                   model=model or "claude-sonnet-4-20250514")
    elif backend == "gemini":
        response = _call_gemini(prompt, EXTRACT_SYSTEM,
                                model=model or "gemini-2.5-flash")
    else:
        raise ValueError(f"Unknown backend: {backend}")

    # Parse response
    ops = []
    try:
        items = _parse_llm_json(response)
        for item in items:
            if not isinstance(item, dict) or "content" not in item:
                continue

            op_type = item.get("op", "create")
            if op_type not in ("create", "update"):
                op_type = "create"

            target_id = None
            if op_type == "update":
                target_id = item.get("memory_id") or item.get("target_id")
                if target_id is not None:
                    try:
                        target_id = int(target_id)
                    except (ValueError, TypeError):
                        # Invalid ID, treat as create
                        op_type = "create"
                        target_id = None
                else:
                    # No target ID for update, treat as create
                    op_type = "create"

            ops.append(MemoryOp(
                op=op_type,
                content=item["content"],
                importance=max(1, min(5, int(item.get("importance", 3)))),
                memory_type=item.get("memory_type", "general"),
                topic_tags=item.get("topic_tags", []),
                target_id=target_id,
            ))
    except (json.JSONDecodeError, ValueError, TypeError):
        pass

    return ops


# ---------------------------------------------------------------------------
# Pipeline — full extraction pipeline with gate + extract + dedup + store
# ---------------------------------------------------------------------------

@dataclass
class PipelineConfig:
    """Configuration for the extraction pipeline."""
    gate_backend: str = "local"
    gate_model: Optional[str] = None  # None = use backend default
    extract_backend: str = "local"
    extract_model: Optional[str] = None
    dedup_threshold: float = 0.85
    source_session: str = ""


class ExtractionPipeline:
    """Full pipeline: gate → extract (with reconciliation) → dedup → store/update."""

    def __init__(self, store: MemoryStore, config: Optional[PipelineConfig] = None):
        self.store = store
        self.config = config or PipelineConfig()
        self.stats = {
            "chunks_processed": 0,
            "chunks_passed_gate": 0,
            "memories_extracted": 0,
            "memories_stored": 0,
            "memories_updated": 0,
            "memories_deduped": 0,
        }

    def process_chunk(self, chunk: str) -> list[int]:
        """Process a conversation chunk through the full pipeline.

        Returns list of stored/updated memory IDs.
        """
        self.stats["chunks_processed"] += 1

        # Step 1: Gate
        should_remember, reason = gate(
            chunk,
            backend=self.config.gate_backend,
            model=self.config.gate_model,
        )

        if not should_remember:
            return []

        self.stats["chunks_passed_gate"] += 1

        # Step 2: Find existing related memories (with IDs for reconciliation)
        existing = self.store.search(chunk[:500], limit=5, threshold=0.60)
        existing_memories = ""
        if existing:
            existing_memories = "\n".join(
                f"[ID={r.memory.id}] (type={r.memory.memory_type}, imp={r.memory.importance}) {r.memory.content[:150]}"
                for r in existing
            )

        # Step 3: Extract memory operations (create/update)
        ops = extract(
            chunk,
            existing_memories=existing_memories,
            backend=self.config.extract_backend,
            model=self.config.extract_model,
        )

        self.stats["memories_extracted"] += len(ops)

        # Step 4: Execute operations
        result_ids = []
        for op in ops:
            if op.op == "update" and op.target_id is not None:
                # Verify the target exists
                target = self.store.get(op.target_id)
                if target:
                    self.store.update(
                        memory_id=op.target_id,
                        content=op.content,
                        importance=op.importance,
                        topic_tags=op.topic_tags if op.topic_tags else None,
                    )
                    result_ids.append(op.target_id)
                    self.stats["memories_updated"] += 1
                    continue
                # Target doesn't exist, fall through to create

            # Create: check for duplicates first
            dupes = self.store.find_duplicates(
                op.content,
                threshold=self.config.dedup_threshold,
            )

            if dupes:
                self.stats["memories_deduped"] += 1
                continue

            # Store new memory
            mid = self.store.store(
                content=op.content,
                importance=op.importance,
                memory_type=op.memory_type,
                topic_tags=op.topic_tags,
                source_session=self.config.source_session,
            )
            result_ids.append(mid)
            self.stats["memories_stored"] += 1

        return result_ids

    def process_conversation(self, text: str, chunk_size: int = 1500,
                             overlap: int = 200) -> list[int]:
        """Process a full conversation text by chunking and extracting.

        Args:
            text: Full conversation text
            chunk_size: Approximate characters per chunk
            overlap: Character overlap between chunks

        Returns:
            List of all stored memory IDs
        """
        chunks = self._chunk_text(text, chunk_size, overlap)
        all_ids = []
        for chunk in chunks:
            ids = self.process_chunk(chunk)
            all_ids.extend(ids)
        return all_ids

    def _chunk_text(self, text: str, chunk_size: int, overlap: int) -> list[str]:
        """Split text into overlapping chunks, breaking at paragraph boundaries."""
        if len(text) <= chunk_size:
            return [text]

        chunks = []
        start = 0
        while start < len(text):
            end = start + chunk_size

            # Try to break at a paragraph boundary
            if end < len(text):
                # Look for double newline near the end
                search_start = max(start + chunk_size // 2, end - 200)
                break_pos = text.rfind("\n\n", search_start, end + 200)
                if break_pos > search_start:
                    end = break_pos + 2
                else:
                    # Fall back to single newline
                    break_pos = text.rfind("\n", search_start, end + 100)
                    if break_pos > search_start:
                        end = break_pos + 1

            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)

            start = end - overlap

        return chunks

    def get_stats(self) -> dict:
        """Return pipeline statistics."""
        return dict(self.stats)
