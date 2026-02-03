"""Memory Agent HTTP API server.

Provides REST API for memory operations and conversation ingestion.
Uses stdlib http.server to avoid external dependencies.

Endpoints:
  GET  /health             — health check
  GET  /stats              — memory store statistics
  GET  /memories           — list memories (params: limit, offset, sort)
  GET  /memories/<id>      — get specific memory
  POST /search             — semantic search (body: {"query": "...", "limit": 5})
  POST /store              — store a memory (body: {"content": "...", ...})
  POST /ingest             — ingest conversation chunk (runs extraction pipeline)
  POST /ingest/conversation — ingest full conversation text (chunks + extracts)
  DELETE /memories/<id>    — delete a memory

Run:
  memory-agent serve [--port 8094] [--host 0.0.0.0]
"""

import json
import logging
import time
import traceback
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional
from urllib.parse import urlparse, parse_qs

from .store import MemoryStore, Memory, SearchResult
from .extractor import ExtractionPipeline, PipelineConfig

log = logging.getLogger("memory-agent")


def _memory_to_dict(m: Memory) -> dict:
    return {
        "id": m.id,
        "content": m.content,
        "importance": m.importance,
        "memory_type": m.memory_type,
        "topic_tags": m.topic_tags,
        "source_session": m.source_session,
        "created_at": m.created_at,
        "last_accessed": m.last_accessed,
        "access_count": m.access_count,
        "age_days": round(m.age_days, 1),
    }


def _result_to_dict(r: SearchResult) -> dict:
    d = _memory_to_dict(r.memory)
    d["score"] = round(r.score, 4)
    d["similarity"] = round(r.similarity, 4)
    return d


class MemoryHandler(BaseHTTPRequestHandler):
    """HTTP request handler for memory agent API."""

    store: MemoryStore = None
    pipeline: ExtractionPipeline = None

    def log_message(self, format, *args):
        """Route HTTP request logs through the logger."""
        log.debug(format % args)

    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def _send_error(self, status, message):
        log.warning("HTTP %d: %s", status, message)
        self._send_json({"error": message}, status)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        body = self.rfile.read(length)
        return json.loads(body)

    def _parse_path(self):
        parsed = urlparse(self.path)
        return parsed.path.rstrip("/"), parse_qs(parsed.query)

    # --- CORS ---
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # --- GET ---
    def do_GET(self):
        path, params = self._parse_path()

        try:
            if path == "/health":
                ok = self.store.embedder.health_check()
                self._send_json({
                    "status": "ok" if ok else "degraded",
                    "ollama": ok,
                    "memories": self.store.count(),
                    "pipeline": self.pipeline is not None,
                })

            elif path == "/stats":
                self._send_json(self.store.stats())

            elif path == "/memories":
                limit = int(params.get("limit", [50])[0])
                offset = int(params.get("offset", [0])[0])
                sort = params.get("sort", ["created_at"])[0]
                memories = self.store.list_all(limit=limit, offset=offset, sort_by=sort)
                self._send_json({
                    "memories": [_memory_to_dict(m) for m in memories],
                    "total": self.store.count(),
                    "limit": limit,
                    "offset": offset,
                })

            elif path.startswith("/memories/"):
                try:
                    mid = int(path.split("/")[-1])
                except ValueError:
                    self._send_error(400, "Invalid memory ID")
                    return
                m = self.store.get(mid)
                if not m:
                    self._send_error(404, f"Memory {mid} not found")
                    return
                data = _memory_to_dict(m)
                data["links"] = [
                    {"to_id": lid, "relationship": rel}
                    for lid, rel in self.store.get_links(mid)
                ]
                self._send_json(data)

            elif path == "/pipeline/stats":
                if self.pipeline:
                    self._send_json(self.pipeline.get_stats())
                else:
                    self._send_json({"error": "Pipeline not initialized"})

            else:
                self._send_error(404, f"Unknown endpoint: {path}")

        except Exception as e:
            log.error("GET %s failed: %s", path, e, exc_info=True)
            self._send_error(500, str(e))

    # --- POST ---
    def do_POST(self):
        path, params = self._parse_path()

        try:
            body = self._read_body()

            if path == "/search":
                query = body.get("query", "")
                if not query:
                    self._send_error(400, "Missing 'query' field")
                    return

                limit = body.get("limit", 5)
                threshold = body.get("threshold", 0.40)
                memory_type = body.get("memory_type")
                min_importance = body.get("min_importance", 1)

                start = time.time()
                results = self.store.search(
                    query=query,
                    limit=limit,
                    threshold=threshold,
                    memory_type=memory_type,
                    min_importance=min_importance,
                )
                elapsed = time.time() - start
                log.info("search query=%r results=%d time=%.1fms",
                         query[:50], len(results), elapsed * 1000)

                self._send_json({
                    "query": query,
                    "results": [_result_to_dict(r) for r in results],
                    "count": len(results),
                })

            elif path == "/store":
                content = body.get("content", "")
                if not content:
                    self._send_error(400, "Missing 'content' field")
                    return

                mid = self.store.store(
                    content=content,
                    importance=body.get("importance", 3),
                    memory_type=body.get("memory_type", "general"),
                    topic_tags=body.get("topic_tags", []),
                    source_session=body.get("source_session", ""),
                )
                log.info("stored memory #%d (%d chars)", mid, len(content))
                self._send_json({"id": mid, "stored": True})

            elif path == "/ingest":
                chunk = body.get("chunk", "") or body.get("text", "")
                if not chunk:
                    self._send_error(400, "Missing 'chunk' or 'text' field")
                    return

                if not self.pipeline:
                    self._send_error(503, "Extraction pipeline not initialized")
                    return

                session = body.get("session", "")
                if session:
                    self.pipeline.config.source_session = session

                start = time.time()
                stored_ids = self.pipeline.process_chunk(chunk)
                elapsed = time.time() - start
                log.info("ingest chunk=%d chars stored=%d time=%.1fs",
                         len(chunk), len(stored_ids), elapsed)

                self._send_json({
                    "stored_ids": stored_ids,
                    "memories_stored": len(stored_ids),
                    "pipeline_stats": self.pipeline.get_stats(),
                })

            elif path == "/ingest/conversation":
                text = body.get("text", "") or body.get("conversation", "")
                if not text:
                    self._send_error(400, "Missing 'text' or 'conversation' field")
                    return

                if not self.pipeline:
                    self._send_error(503, "Extraction pipeline not initialized")
                    return

                session = body.get("session", "")
                if session:
                    self.pipeline.config.source_session = session

                chunk_size = body.get("chunk_size", 1500)
                overlap = body.get("overlap", 200)

                start = time.time()
                stored_ids = self.pipeline.process_conversation(
                    text, chunk_size=chunk_size, overlap=overlap,
                )
                elapsed = time.time() - start
                log.info("ingest conversation=%d chars chunks=%d stored=%d time=%.1fs",
                         len(text), self.pipeline.stats["chunks_processed"],
                         len(stored_ids), elapsed)

                self._send_json({
                    "stored_ids": stored_ids,
                    "memories_stored": len(stored_ids),
                    "text_length": len(text),
                    "pipeline_stats": self.pipeline.get_stats(),
                })

            else:
                self._send_error(404, f"Unknown endpoint: {path}")

        except json.JSONDecodeError:
            self._send_error(400, "Invalid JSON body")
        except Exception as e:
            log.error("POST %s failed: %s", path, e, exc_info=True)
            self._send_error(500, str(e))

    # --- DELETE ---
    def do_DELETE(self):
        path, params = self._parse_path()

        try:
            if path.startswith("/memories/"):
                try:
                    mid = int(path.split("/")[-1])
                except ValueError:
                    self._send_error(400, "Invalid memory ID")
                    return
                if self.store.delete(mid):
                    log.info("deleted memory #%d", mid)
                    self._send_json({"deleted": True, "id": mid})
                else:
                    self._send_error(404, f"Memory {mid} not found")
            else:
                self._send_error(404, f"Unknown endpoint: {path}")

        except Exception as e:
            log.error("DELETE %s failed: %s", path, e, exc_info=True)
            self._send_error(500, str(e))


def run_server(
    host: str = "0.0.0.0",
    port: int = 8094,
    store: Optional[MemoryStore] = None,
    pipeline_config: Optional[PipelineConfig] = None,
):
    """Start the memory agent HTTP server."""
    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    store = store or MemoryStore()

    # Set up pipeline if config provided
    pipeline = None
    if pipeline_config:
        pipeline = ExtractionPipeline(store, pipeline_config)
        log.info("Pipeline enabled: gate=%s/%s extract=%s/%s",
                 pipeline_config.gate_backend,
                 pipeline_config.gate_model or "default",
                 pipeline_config.extract_backend,
                 pipeline_config.extract_model or "default")

    # Inject store and pipeline into handler class
    MemoryHandler.store = store
    MemoryHandler.pipeline = pipeline

    server = HTTPServer((host, port), MemoryHandler)
    log.info("Memory Agent server running on http://%s:%d", host, port)
    log.info("  Memories: %d", store.count())
    log.info("  Pipeline: %s", "enabled" if pipeline else "disabled")
    log.info("  Ollama: %s", "ok" if store.embedder.health_check() else "unavailable")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down...")
        server.server_close()
