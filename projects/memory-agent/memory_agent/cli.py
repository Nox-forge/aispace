#!/usr/bin/env python3
"""Memory Agent CLI — semantic memory store for Claude sessions.

Usage:
    memory-agent store "content" [--importance N] [--type TYPE] [--tags tag1,tag2]
    memory-agent search "query" [--limit N] [--threshold N]
    memory-agent get ID
    memory-agent delete ID
    memory-agent list [--limit N] [--sort FIELD]
    memory-agent stats
    memory-agent import-mcp
    memory-agent health
"""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from .store import MemoryStore
from .embeddings import EmbeddingClient


def fmt_time(ts: float) -> str:
    """Format a unix timestamp as human-readable."""
    if not ts:
        return "never"
    dt = datetime.fromtimestamp(ts)
    age = time.time() - ts
    if age < 3600:
        return f"{int(age / 60)}m ago"
    elif age < 86400:
        return f"{int(age / 3600)}h ago"
    elif age < 604800:
        return f"{int(age / 86400)}d ago"
    else:
        return dt.strftime("%Y-%m-%d")


def cmd_store(store: MemoryStore, args):
    content = args.content
    tags = [t.strip() for t in args.tags.split(",")] if args.tags else []

    # Check for duplicates first
    dupes = store.find_duplicates(content)
    if dupes:
        print(f"  Warning: found {len(dupes)} similar memory(ies):")
        for d in dupes:
            print(f"    [{d.memory.id}] ({d.similarity:.2f}) {d.memory.content[:80]}")
        print(f"  Storing anyway...")

    mid = store.store(
        content=content,
        importance=args.importance,
        memory_type=args.type,
        topic_tags=tags,
        source_session=args.session or "",
    )
    print(f"  Stored memory #{mid} (importance={args.importance}, type={args.type})")


def cmd_search(store: MemoryStore, args):
    results = store.search(
        query=args.query,
        limit=args.limit,
        threshold=args.threshold,
    )

    if not results:
        print("  No results found.")
        return

    print(f"  {len(results)} result(s) for: \"{args.query}\"")
    print()
    for r in results:
        m = r.memory
        tags = " ".join(f"[{t}]" for t in m.topic_tags) if m.topic_tags else ""
        print(f"  #{m.id}  score={r.score:.3f}  sim={r.similarity:.3f}  imp={m.importance}  {m.memory_type}")
        print(f"  {m.content[:120]}")
        print(f"  {fmt_time(m.created_at)}  accessed={m.access_count}x  {tags}")
        print()


def cmd_get(store: MemoryStore, args):
    m = store.get(args.id)
    if not m:
        print(f"  Memory #{args.id} not found.")
        return

    tags = ", ".join(m.topic_tags) if m.topic_tags else "none"
    print(f"  Memory #{m.id}")
    print(f"  Type:       {m.memory_type}")
    print(f"  Importance: {m.importance}")
    print(f"  Tags:       {tags}")
    print(f"  Session:    {m.source_session or 'unknown'}")
    print(f"  Created:    {fmt_time(m.created_at)}")
    print(f"  Accessed:   {m.access_count}x (last: {fmt_time(m.last_accessed)})")
    print(f"  Content:")
    print(f"    {m.content}")

    links = store.get_links(m.id)
    if links:
        print(f"  Links:")
        for linked_id, rel in links:
            linked = store.get(linked_id)
            if linked:
                print(f"    -> #{linked_id} ({rel}): {linked.content[:60]}")


def cmd_delete(store: MemoryStore, args):
    if store.delete(args.id):
        print(f"  Deleted memory #{args.id}")
    else:
        print(f"  Memory #{args.id} not found.")


def cmd_list(store: MemoryStore, args):
    memories = store.list_all(limit=args.limit, sort_by=args.sort)
    total = store.count()

    print(f"  Showing {len(memories)} of {total} memories (sort: {args.sort})")
    print()
    for m in memories:
        tags = " ".join(f"[{t}]" for t in m.topic_tags[:3]) if m.topic_tags else ""
        content_preview = m.content[:80].replace("\n", " ")
        print(f"  #{m.id:<4} imp={m.importance} {m.memory_type:<12} {fmt_time(m.created_at):<10} {content_preview}")


def cmd_stats(store: MemoryStore, args):
    s = store.stats()
    print(f"  Total memories: {s['total_memories']}")
    print(f"  Total links:    {s['total_links']}")
    print(f"  Avg importance: {s['avg_importance']}")
    if s['oldest']:
        print(f"  Oldest:         {fmt_time(s['oldest'])}")
        print(f"  Newest:         {fmt_time(s['newest'])}")
    print(f"  By type:")
    for t, c in sorted(s['by_type'].items()):
        print(f"    {t:<15} {c}")
    print(f"  By importance:")
    for i in range(1, 6):
        c = s['by_importance'].get(i, 0)
        bar = "█" * c
        print(f"    {i}: {c:>4} {bar}")


def cmd_import_mcp(store: MemoryStore, args):
    """Import existing MCP memory graph entities as seed data."""
    try:
        import subprocess
        # Use the MCP memory tool to read the graph — but since we can't call MCP tools
        # directly from Python, we'll read from a JSON export if available
        export_path = Path.home() / ".memory-agent" / "mcp_export.json"
        if not export_path.exists():
            print("  No MCP export found at ~/.memory-agent/mcp_export.json")
            print("  To create one, run from a Claude session:")
            print("    mcp__memory__read_graph > ~/.memory-agent/mcp_export.json")
            print("  Then run this command again.")
            return

        with open(export_path) as f:
            graph = json.load(f)

        entities = graph.get("entities", [])
        imported = 0
        skipped = 0

        for entity in entities:
            name = entity.get("name", "")
            etype = entity.get("entityType", "")
            observations = entity.get("observations", [])

            for obs in observations:
                content = f"[{name}] ({etype}): {obs}"

                # Check for duplicates
                dupes = store.find_duplicates(content, threshold=0.90)
                if dupes:
                    skipped += 1
                    continue

                store.store(
                    content=content,
                    importance=3,
                    memory_type="fact",
                    topic_tags=[etype.lower(), name.lower().replace(" ", "-")],
                    source_session="mcp-import",
                )
                imported += 1

        print(f"  Imported {imported} memories from {len(entities)} MCP entities")
        print(f"  Skipped {skipped} duplicates")

    except Exception as e:
        print(f"  Error importing MCP data: {e}")


def cmd_serve(args):
    """Start the HTTP API server."""
    from .server import run_server
    from .extractor import PipelineConfig

    pipeline_config = None
    if args.with_pipeline:
        pipeline_config = PipelineConfig(
            gate_backend=args.gate_backend,
            gate_model=args.gate_model,
            extract_backend=args.extract_backend,
            extract_model=args.extract_model,
        )

    run_server(
        host=args.host,
        port=args.port,
        pipeline_config=pipeline_config,
        ccc_listen=args.ccc_listen,
        ccc_poll_interval=args.ccc_poll_interval,
    )


def cmd_health(store: MemoryStore, args):
    """Check system health."""
    # Check Ollama
    ok = store.embedder.health_check()
    status = "OK" if ok else "FAIL"
    print(f"  Ollama ({store.embedder.model}): {status}")

    # Check DB
    try:
        count = store.count()
        print(f"  SQLite DB: OK ({count} memories)")
    except Exception as e:
        print(f"  SQLite DB: FAIL ({e})")

    # Test embedding
    if ok:
        try:
            start = time.time()
            store.embedder.embed("test", prefix="search_query")
            elapsed = (time.time() - start) * 1000
            print(f"  Embedding latency: {elapsed:.0f}ms")
        except Exception as e:
            print(f"  Embedding test: FAIL ({e})")


def main():
    parser = argparse.ArgumentParser(
        description="Memory Agent — semantic memory store",
        prog="memory-agent",
    )
    sub = parser.add_subparsers(dest="command")

    # store
    p = sub.add_parser("store", help="Store a new memory")
    p.add_argument("content", help="Memory content text")
    p.add_argument("--importance", "-i", type=int, default=3, help="Importance 1-5 (default: 3)")
    p.add_argument("--type", "-t", default="general",
                   help="Memory type: decision, insight, fact, preference, project, conversation, general")
    p.add_argument("--tags", default="", help="Comma-separated topic tags")
    p.add_argument("--session", "-s", default="", help="Source session name")

    # search
    p = sub.add_parser("search", help="Search memories semantically")
    p.add_argument("query", help="Natural language search query")
    p.add_argument("--limit", "-l", type=int, default=5, help="Max results (default: 5)")
    p.add_argument("--threshold", type=float, default=0.40, help="Min relevance score (default: 0.40)")

    # get
    p = sub.add_parser("get", help="Get a specific memory")
    p.add_argument("id", type=int, help="Memory ID")

    # delete
    p = sub.add_parser("delete", help="Delete a memory")
    p.add_argument("id", type=int, help="Memory ID")

    # list
    p = sub.add_parser("list", help="List memories")
    p.add_argument("--limit", "-l", type=int, default=20, help="Max results (default: 20)")
    p.add_argument("--sort", default="created_at",
                   help="Sort by: created_at, importance, access_count, last_accessed")

    # stats
    sub.add_parser("stats", help="Show memory store statistics")

    # import-mcp
    sub.add_parser("import-mcp", help="Import MCP memory graph as seed data")

    # health
    sub.add_parser("health", help="Check system health")

    # serve
    p = sub.add_parser("serve", help="Start HTTP API server")
    p.add_argument("--port", "-p", type=int, default=8094, help="Port (default: 8094)")
    p.add_argument("--host", default="0.0.0.0", help="Host (default: 0.0.0.0)")
    p.add_argument("--with-pipeline", action="store_true",
                   help="Enable extraction pipeline for automatic memory capture")
    p.add_argument("--gate-backend", default="gemini", help="Gate backend: local, remote, anthropic, gemini")
    p.add_argument("--gate-model", default=None, help="Gate model override")
    p.add_argument("--extract-backend", default="gemini", help="Extract backend: local, remote, anthropic, gemini")
    p.add_argument("--extract-model", default=None, help="Extract model override")
    p.add_argument("--ccc-listen", action="store_true",
                   help="Enable CCC conversation listener for automatic memory capture")
    p.add_argument("--ccc-poll-interval", type=int, default=30,
                   help="CCC poll interval in seconds (default: 30)")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "serve":
        cmd_serve(args)
        return

    store = MemoryStore()

    commands = {
        "store": cmd_store,
        "search": cmd_search,
        "get": cmd_get,
        "delete": cmd_delete,
        "list": cmd_list,
        "stats": cmd_stats,
        "import-mcp": cmd_import_mcp,
        "health": cmd_health,
    }

    commands[args.command](store, args)


if __name__ == "__main__":
    main()
