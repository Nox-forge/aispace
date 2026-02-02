#!/usr/bin/env python3
"""
Signal Bridge — Signal ↔ Claude Code Bridge

Bridges Signal messaging with Claude Code sessions via tmux.
Listens for incoming Signal messages via signal-cli's SSE endpoint,
routes them to the appropriate Claude Code tmux session, and provides
a JSON-RPC interface for sending replies.

Architecture:
  Signal app → Signal servers → signal-cli daemon (SSE events + JSON-RPC send)
  → signal_bridge.py → tmux send-keys → Claude Code session
  → hooks → send-signal → signal-cli → Signal app

Built by Claude (Opus 4.5).
"""

import asyncio
import json
import logging
import os
import random
import signal
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

import sseclient

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("signal-bridge")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CONFIG_PATH = Path.home() / ".signal-bridge.json"


def load_config() -> dict:
    """Load and validate configuration from ~/.signal-bridge.json."""
    if not CONFIG_PATH.exists():
        log.error("Config file not found: %s", CONFIG_PATH)
        sys.exit(1)

    with open(CONFIG_PATH) as f:
        config = json.load(f)

    required_keys = ["signal_number", "routing", "allowed_numbers"]
    for key in required_keys:
        if key not in config:
            log.error("Missing required config key: %s", key)
            sys.exit(1)

    # Defaults
    config.setdefault("daemon_host", "127.0.0.1")
    config.setdefault("daemon_port", 8080)

    return config


# ---------------------------------------------------------------------------
# SSE listener with exponential backoff
# ---------------------------------------------------------------------------
BACKOFF_INITIAL = 1.0   # seconds
BACKOFF_MAX = 10.0       # seconds
BACKOFF_MULTIPLIER = 2.0
BACKOFF_JITTER = 0.20    # 20%


def sse_events(config: dict):
    """
    Generator that yields SSE events from signal-cli.

    Reconnects with exponential backoff on failure.
    Yields parsed JSON event data dicts.
    """
    host = config["daemon_host"]
    port = config["daemon_port"]
    url = f"http://{host}:{port}/api/v1/events"
    backoff = BACKOFF_INITIAL

    while True:
        try:
            log.info("Connecting to SSE endpoint: %s", url)
            req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
            response = urllib.request.urlopen(req)
            client = sseclient.SSEClient(response)
            backoff = BACKOFF_INITIAL  # reset on successful connection
            log.info("SSE connection established")

            for event in client.events():
                if event.data:
                    try:
                        data = json.loads(event.data)
                        yield data
                    except json.JSONDecodeError:
                        log.warning("Failed to parse SSE event data: %s", event.data[:200])

        except (urllib.error.URLError, ConnectionError, OSError) as e:
            log.warning("SSE connection error: %s", e)
        except Exception as e:
            log.error("Unexpected SSE error: %s", e)

        # Exponential backoff with jitter
        jitter = backoff * BACKOFF_JITTER * (2 * random.random() - 1)
        sleep_time = backoff + jitter
        log.info("Reconnecting in %.1fs ...", sleep_time)
        time.sleep(sleep_time)
        backoff = min(backoff * BACKOFF_MULTIPLIER, BACKOFF_MAX)


# ---------------------------------------------------------------------------
# JSON-RPC: send message via signal-cli
# ---------------------------------------------------------------------------
def send_signal_message(config: dict, recipient: str, message: str) -> bool:
    """Send a message via signal-cli JSON-RPC endpoint."""
    host = config["daemon_host"]
    port = config["daemon_port"]
    url = f"http://{host}:{port}/api/v1/rpc"

    payload = json.dumps({
        "jsonrpc": "2.0",
        "method": "send",
        "params": {
            "account": config["signal_number"],
            "recipients": [recipient],
            "message": message,
        },
        "id": 1,
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if "error" in result:
                log.error("JSON-RPC error: %s", result["error"])
                return False
            return True
    except Exception as e:
        log.error("Failed to send message: %s", e)
        return False


# ---------------------------------------------------------------------------
# tmux integration
# ---------------------------------------------------------------------------
def tmux_session_exists(session_name: str) -> bool:
    """Check if a tmux session exists."""
    result = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        capture_output=True,
    )
    return result.returncode == 0


def send_to_tmux(session_name: str, text: str) -> bool:
    """
    Send text to a Claude Code tmux session.

    Uses send-keys -l for literal text, then Enter twice
    (Claude Code needs double Enter to submit).
    """
    tmux_target = f"claude-{session_name}"

    if not tmux_session_exists(tmux_target):
        log.warning("tmux session '%s' does not exist", tmux_target)
        return False

    try:
        # Send text literally
        subprocess.run(
            ["tmux", "send-keys", "-t", tmux_target, "-l", text],
            check=True,
            capture_output=True,
        )

        # Small delay for content to load
        delay = min(0.05 + len(text) * 0.0005, 5.0)
        time.sleep(delay)

        # Send Enter twice (Claude Code needs double Enter)
        subprocess.run(
            ["tmux", "send-keys", "-t", tmux_target, "C-m"],
            check=True,
            capture_output=True,
        )
        time.sleep(0.05)
        subprocess.run(
            ["tmux", "send-keys", "-t", tmux_target, "C-m"],
            check=True,
            capture_output=True,
        )

        log.info("Sent message to tmux session '%s'", tmux_target)
        return True

    except subprocess.CalledProcessError as e:
        log.error("Failed to send to tmux session '%s': %s", tmux_target, e)
        return False


# ---------------------------------------------------------------------------
# Message processing
# ---------------------------------------------------------------------------
def process_event(config: dict, event_data: dict):
    """Process a single SSE event from signal-cli."""
    envelope = event_data.get("envelope")
    if not envelope:
        return

    # Filter: only process dataMessage events
    data_message = envelope.get("dataMessage")
    if not data_message:
        return

    source = envelope.get("source")
    message_text = data_message.get("message")

    if not source or not message_text:
        return

    # Allowlist check
    if source not in config["allowed_numbers"]:
        log.warning("Ignoring message from non-allowed number: %s", source)
        return

    # Route to session
    routing = config.get("routing", {})
    session_name = routing.get(source)

    if not session_name:
        log.warning("No routing configured for number: %s", source)
        return

    log.info("Message from %s → session '%s': %s", source, session_name, message_text[:80])

    # Send to tmux
    if not send_to_tmux(session_name, message_text):
        # Notify sender that delivery failed
        send_signal_message(
            config, source,
            f"[Signal Bridge] Failed to deliver message. Session 'claude-{session_name}' may not be running.",
        )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
shutdown_event = asyncio.Event()


def handle_shutdown(sig, frame):
    """Handle SIGTERM/SIGINT for graceful shutdown."""
    log.info("Received signal %s, shutting down...", sig)
    shutdown_event.set()


async def main():
    """Main entry point — run SSE listener loop."""
    config = load_config()

    log.info("Signal Bridge starting")
    log.info("  Signal number: %s", config["signal_number"])
    log.info("  Daemon: %s:%s", config["daemon_host"], config["daemon_port"])
    log.info("  Routing: %s", json.dumps(config["routing"]))
    log.info("  Allowed numbers: %s", config["allowed_numbers"])

    # Register signal handlers
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    # Run the blocking SSE loop in a thread so we can check shutdown_event
    loop = asyncio.get_event_loop()

    def sse_loop():
        for event_data in sse_events(config):
            if shutdown_event.is_set():
                break
            try:
                process_event(config, event_data)
            except Exception as e:
                log.error("Error processing event: %s", e)

    # Run SSE loop in executor thread
    sse_task = loop.run_in_executor(None, sse_loop)

    # Wait for shutdown signal or SSE loop to finish
    try:
        await sse_task
    except asyncio.CancelledError:
        pass

    log.info("Signal Bridge stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Interrupted by user")
    except SystemExit:
        raise
    except Exception as e:
        log.error("Fatal error: %s", e)
        sys.exit(1)
