#!/usr/bin/env python3
"""Backfill historical CCC conversations into the Memory Agent.

Reads all messages from CCC's SQLite database, reconstructs them as
conversation text per session, and sends them through the memory-agent
ingestion pipeline.
"""

import json
import sqlite3
import time
import requests

API = "http://127.0.0.1:8094"
CCC_DB = "/home/clawdbot/.ccc/sessions.db"

# How many characters per ingestion chunk
CHUNK_SIZE = 2000
OVERLAP = 200


def get_sessions(conn):
    """Get all sessions with message counts."""
    rows = conn.execute("""
        SELECT session, COUNT(*) as msgs,
               SUM(LENGTH(content)) as chars
        FROM messages
        GROUP BY session
        ORDER BY msgs DESC
    """).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def get_messages(conn, session):
    """Get all messages for a session, ordered by ID."""
    rows = conn.execute("""
        SELECT id, role, content, channel
        FROM messages
        WHERE session = ?
        ORDER BY id ASC
    """, (session,)).fetchall()
    return rows


def format_conversation(messages):
    """Format messages as conversation text."""
    lines = []
    for msg_id, role, content, channel in messages:
        content = content.strip()
        if not content:
            continue
        # Skip very short system-like messages
        if role == "system":
            continue
        if role == "assistant" and len(content) < 15:
            continue

        if role == "user":
            lines.append(f"User: {content}")
        elif role == "assistant":
            lines.append(f"Assistant: {content}")

    return "\n\n".join(lines)


def chunk_text(text, chunk_size=CHUNK_SIZE, overlap=OVERLAP):
    """Split text into overlapping chunks."""
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size

        # Try to break at a paragraph boundary
        if end < len(text):
            break_pos = text.rfind("\n\n", max(start + chunk_size // 2, end - 300), end + 200)
            if break_pos > start + chunk_size // 2:
                end = break_pos + 2

        chunk = text[start:end].strip()
        if chunk and len(chunk) > 50:  # skip tiny fragments
            chunks.append(chunk)

        start = end - overlap

    return chunks


def ingest_chunk(chunk, session):
    """Send a chunk to the memory-agent ingestion endpoint."""
    try:
        resp = requests.post(
            f"{API}/ingest",
            json={"chunk": chunk, "session": f"backfill-{session}"},
            timeout=60,
        )
        if resp.status_code == 200:
            return resp.json()
        else:
            print(f"    HTTP {resp.status_code}: {resp.text[:100]}")
            return None
    except Exception as e:
        print(f"    Error: {e}")
        return None


def main():
    print("=" * 60)
    print("CCC HISTORY BACKFILL")
    print("=" * 60)

    # Check service health
    try:
        resp = requests.get(f"{API}/health", timeout=5)
        health = resp.json()
        print(f"Memory Agent: {health['status']} ({health['memories']} memories)")
    except Exception as e:
        print(f"Memory Agent not available: {e}")
        return

    conn = sqlite3.connect(CCC_DB)
    sessions = get_sessions(conn)

    print(f"\nSessions to backfill:")
    total_msgs = 0
    total_chars = 0
    for name, msgs, chars in sessions:
        print(f"  {name}: {msgs} messages, {chars:,} chars")
        total_msgs += msgs
        total_chars += chars
    print(f"  Total: {total_msgs} messages, {total_chars:,} chars")

    total_stored = 0
    total_updated = 0
    total_chunks = 0
    total_errors = 0

    for session_name, msg_count, char_count in sessions:
        print(f"\n--- {session_name} ({msg_count} messages) ---")

        messages = get_messages(conn, session_name)
        conversation = format_conversation(messages)

        if len(conversation) < 100:
            print(f"  Skipping (too short: {len(conversation)} chars)")
            continue

        chunks = chunk_text(conversation)
        print(f"  {len(conversation):,} chars -> {len(chunks)} chunks")

        session_stored = 0
        session_updated = 0
        for i, chunk in enumerate(chunks):
            result = ingest_chunk(chunk, session_name)
            if result:
                stored = result.get("memories_stored", 0)
                stats = result.get("pipeline_stats", {})
                updated = stats.get("memories_updated", 0)
                session_stored += stored
                session_updated += updated
                total_chunks += 1

                status = f"stored={stored}"
                if updated:
                    status += f" updated={updated}"
                print(f"  [{i+1}/{len(chunks)}] {len(chunk):,} chars -> {status}")
            else:
                total_errors += 1
                print(f"  [{i+1}/{len(chunks)}] FAILED")

            # Brief pause to avoid overwhelming the pipeline
            time.sleep(0.5)

        total_stored += session_stored
        total_updated += session_updated
        print(f"  Session total: {session_stored} stored, {session_updated} updated")

    conn.close()

    # Final stats
    print(f"\n{'=' * 60}")
    print(f"BACKFILL COMPLETE")
    print(f"{'=' * 60}")
    print(f"Chunks processed: {total_chunks}")
    print(f"Memories stored:  {total_stored}")
    print(f"Memories updated: {total_updated}")
    print(f"Errors:           {total_errors}")

    # Check final memory count
    resp = requests.get(f"{API}/health", timeout=5)
    print(f"Total memories:   {resp.json()['memories']}")


if __name__ == "__main__":
    main()
