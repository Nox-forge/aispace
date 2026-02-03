"""CCC conversation listener — polls CCC gateway for new messages
and feeds them through the memory extraction pipeline.

Connects to the CCC (Claude Code Companion) WebSocket Gateway REST API
to capture conversation content from all active Claude sessions and
automatically extract memories.

Architecture:
  - Polls CCC REST API every N seconds for new messages
  - Buffers messages per session until threshold reached
  - Sends buffered conversation chunks through extraction pipeline
  - Tracks last processed message ID per session in state file
  - Runs as a daemon thread alongside the HTTP server
"""

import json
import logging
import os
import threading
import time
import requests

from pathlib import Path
from typing import Optional

from .extractor import ExtractionPipeline

log = logging.getLogger("memory-agent.listener")

STATE_DIR = Path.home() / ".memory-agent"
STATE_FILE = STATE_DIR / "listener_state.json"


class CCCListener:
    """Polls CCC gateway for conversation messages and extracts memories."""

    def __init__(
        self,
        pipeline: ExtractionPipeline,
        gateway_url: str = "http://127.0.0.1:18789",
        token: Optional[str] = None,
        poll_interval: int = 30,
        buffer_size: int = 1500,
        flush_age: int = 120,
        sessions: Optional[list[str]] = None,
    ):
        """
        Args:
            pipeline: Extraction pipeline to process chunks through
            gateway_url: CCC gateway base URL
            token: Gateway auth token (auto-loaded if not provided)
            poll_interval: Seconds between poll cycles
            buffer_size: Min chars before flushing a buffer
            flush_age: Seconds of idle before flushing a partial buffer
            sessions: If set, only listen to these session names
        """
        self.pipeline = pipeline
        self.gateway_url = gateway_url.rstrip("/")
        self.token = token or self._load_token()
        self.poll_interval = poll_interval
        self.buffer_size = buffer_size
        self.flush_age = flush_age
        self.sessions_filter = set(sessions) if sessions else None

        # Per-session message buffers
        self.buffers: dict[str, list[str]] = {}
        self.buffer_timestamps: dict[str, float] = {}

        # Track processed messages
        self.state = self._load_state()

        self._running = False
        self._thread: Optional[threading.Thread] = None

        # Stats
        self.stats = {
            "polls": 0,
            "messages_received": 0,
            "chunks_flushed": 0,
            "memories_stored": 0,
            "errors": 0,
            "last_poll": 0,
            "last_flush": 0,
        }

    def _load_token(self) -> str:
        """Load gateway token from OpenClaw config or environment."""
        # Try OpenClaw config first
        try:
            config_path = Path.home() / ".openclaw" / "openclaw.json"
            with open(config_path) as f:
                config = json.load(f)
            token = config["gateway"]["auth"]["token"]
            if token:
                return token
        except (FileNotFoundError, KeyError, json.JSONDecodeError):
            pass

        # Try CCC config
        try:
            config_path = Path.home() / ".ccc.json"
            with open(config_path) as f:
                config = json.load(f)
            token = config.get("gateway", {}).get("token", "")
            if token:
                return token
        except (FileNotFoundError, KeyError, json.JSONDecodeError):
            pass

        # Try env var
        token = os.environ.get("CCC_GATEWAY_TOKEN", "")
        if token:
            return token

        raise RuntimeError(
            "No CCC gateway token found. "
            "Check ~/.openclaw/openclaw.json or set CCC_GATEWAY_TOKEN"
        )

    def _load_state(self) -> dict:
        """Load listener state (last processed message IDs per session)."""
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"last_id": {}}

    def _save_state(self):
        """Persist listener state to disk."""
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(self.state, f, indent=2)

    def _api_get(self, path: str):
        """Make an authenticated GET to CCC gateway."""
        try:
            resp = requests.get(
                f"{self.gateway_url}{path}",
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=10,
            )
            if resp.status_code == 200:
                return resp.json()
            else:
                log.warning("CCC API %s returned %d", path, resp.status_code)
                return None
        except requests.RequestException as e:
            log.debug("CCC API %s failed: %s", path, e)
            return None

    def _get_active_sessions(self) -> list[str]:
        """Get list of active CCC sessions."""
        sessions = self._api_get("/sessions")
        if not sessions:
            return []

        active = [s["name"] for s in sessions if s.get("state") == "active"]

        if self.sessions_filter:
            active = [s for s in active if s in self.sessions_filter]

        return active

    def _get_messages(self, session: str) -> list[dict]:
        """Get recent messages for a session."""
        messages = self._api_get(f"/sessions/{session}/messages")
        if not messages or not isinstance(messages, list):
            return []
        return messages

    def _process_new_messages(self, session: str, messages: list[dict]):
        """Buffer new messages for a session."""
        last_id = self.state["last_id"].get(session, 0)

        # Filter to only new messages (by ID)
        new_messages = [m for m in messages if m.get("ID", 0) > last_id]

        if not new_messages:
            return

        # Sort by ID to maintain order
        new_messages.sort(key=lambda m: m.get("ID", 0))

        # Initialize buffer if needed
        if session not in self.buffers:
            self.buffers[session] = []
            self.buffer_timestamps[session] = time.time()

        for msg in new_messages:
            role = msg.get("Role", "unknown")
            content = msg.get("Content", "")
            channel = msg.get("Channel", "")

            # Skip empty, system messages, and very short assistant fragments
            if not content.strip() or role == "system":
                continue

            # Skip tool-use fragments (very common in Claude output, low memory value)
            if role == "assistant" and len(content.strip()) < 20:
                # Short assistant messages are usually status updates
                continue

            # Format as conversation text
            if role == "user":
                self.buffers[session].append(f"User: {content}")
            elif role == "assistant":
                self.buffers[session].append(f"Assistant: {content}")

            self.stats["messages_received"] += 1

        # Update last processed ID
        max_id = max(m.get("ID", 0) for m in new_messages)
        if max_id > last_id:
            self.state["last_id"][session] = max_id
            self._save_state()

    def _flush_buffer(self, session: str, force: bool = False):
        """Flush a session's buffer through the extraction pipeline if ready."""
        if session not in self.buffers or not self.buffers[session]:
            return

        buffer = self.buffers[session]
        buffer_text = "\n\n".join(buffer)
        buffer_age = time.time() - self.buffer_timestamps.get(session, time.time())

        # Flush conditions:
        # 1. Buffer is large enough for good extraction
        # 2. Buffer has been idle long enough (partial flush)
        # 3. Forced flush (shutdown, manual trigger)
        should_flush = (
            force
            or len(buffer_text) >= self.buffer_size
            or (buffer_age >= self.flush_age and len(buffer_text) > 100)
        )

        if not should_flush:
            return

        log.info("Flushing %d messages (%d chars) for session '%s'",
                 len(buffer), len(buffer_text), session)

        try:
            # Set session context for source attribution
            self.pipeline.config.source_session = session
            stored_ids = self.pipeline.process_chunk(buffer_text)

            self.stats["chunks_flushed"] += 1
            self.stats["memories_stored"] += len(stored_ids)
            self.stats["last_flush"] = time.time()

            if stored_ids:
                log.info("Stored %d memories from session '%s': %s",
                         len(stored_ids), session, stored_ids)
            else:
                log.debug("No memories extracted from session '%s' buffer", session)

        except Exception as e:
            log.error("Pipeline failed for session '%s': %s", session, e)
            self.stats["errors"] += 1

        # Clear buffer regardless of extraction result
        self.buffers[session] = []
        self.buffer_timestamps[session] = time.time()

    def _poll_once(self):
        """Single poll cycle: fetch new messages from all active sessions."""
        self.stats["polls"] += 1
        self.stats["last_poll"] = time.time()

        sessions = self._get_active_sessions()

        for session in sessions:
            try:
                messages = self._get_messages(session)
                self._process_new_messages(session, messages)
                self._flush_buffer(session)
            except Exception as e:
                log.error("Error processing session '%s': %s", session, e)
                self.stats["errors"] += 1

        # Check for idle buffers from sessions that may no longer be active
        for session in list(self.buffers.keys()):
            if session not in sessions:
                self._flush_buffer(session, force=True)

    def _initialize_state(self):
        """On first start, record current message IDs so we only process new messages."""
        if self.state["last_id"]:
            # Already initialized from previous run
            return

        log.info("First start — recording current message positions")
        sessions = self._get_active_sessions()
        for session in sessions:
            messages = self._get_messages(session)
            if messages:
                max_id = max(m.get("ID", 0) for m in messages)
                self.state["last_id"][session] = max_id
                log.info("  Session '%s': starting after message #%d", session, max_id)

        self._save_state()

    def start(self):
        """Start the listener in a background thread."""
        if self._running:
            log.warning("Listener already running")
            return

        self._initialize_state()
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True,
                                        name="ccc-listener")
        self._thread.start()
        log.info("CCC listener started (poll=%ds, buffer=%d chars, flush=%ds)",
                 self.poll_interval, self.buffer_size, self.flush_age)

    def stop(self):
        """Stop the listener and flush remaining buffers."""
        self._running = False

        # Flush all remaining buffers
        for session in list(self.buffers.keys()):
            self._flush_buffer(session, force=True)

        if self._thread:
            self._thread.join(timeout=5)

        log.info("CCC listener stopped (stats: %s)", self.stats)

    def _run_loop(self):
        """Main polling loop."""
        # Initial delay to let server start up
        time.sleep(2)

        while self._running:
            try:
                self._poll_once()
            except Exception as e:
                log.error("Poll cycle failed: %s", e)
                self.stats["errors"] += 1

            # Sleep in small increments so stop() is responsive
            for _ in range(self.poll_interval * 2):
                if not self._running:
                    break
                time.sleep(0.5)

    def get_stats(self) -> dict:
        """Return listener statistics."""
        return {
            **self.stats,
            "running": self._running,
            "buffer_sessions": len(self.buffers),
            "buffer_messages": sum(len(b) for b in self.buffers.values()),
            "tracked_sessions": list(self.state["last_id"].keys()),
            "poll_interval": self.poll_interval,
            "buffer_size": self.buffer_size,
            "flush_age": self.flush_age,
        }
