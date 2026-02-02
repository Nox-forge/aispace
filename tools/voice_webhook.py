#!/usr/bin/env python3
"""
Voice Webhook — Flask server for Plivo voice call callbacks.
Handles outbound TTS delivery and inbound voice conversations routed to Claude.
Built by Claude (Opus 4.5).
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, request, jsonify, Response
import plivo.xml

# ─── Configuration ────────────────────────────────────────────────────────────

CONFIG_PATH = Path.home() / ".voice-call.json"

def load_config() -> dict:
    """Load configuration from ~/.voice-call.json."""
    if not CONFIG_PATH.exists():
        print(f"FATAL: Config file not found: {CONFIG_PATH}", file=sys.stderr)
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        return json.load(f)


CFG = load_config()

# ─── Flask App ────────────────────────────────────────────────────────────────

app = Flask(__name__)

VOICE = "Polly.Amy"
TTS_MAX_CHARS = 500


def log(msg: str):
    """Log to stderr with timestamp."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", file=sys.stderr, flush=True)


def notify(message: str):
    """Send a Telegram notification (best-effort)."""
    try:
        subprocess.run(
            ["send-telegram", message],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass


def plivo_xml_response(xml_element) -> Response:
    """Convert a Plivo XML element to a Flask Response with correct content type."""
    xml_str = xml_element.to_string()
    # Ensure it's a string, not bytes
    if isinstance(xml_str, bytes):
        xml_str = xml_str.decode("utf-8")
    return Response(xml_str, mimetype="application/xml")


# ─── Outbound TTS ────────────────────────────────────────────────────────────

@app.route("/voice/outbound", methods=["GET"])
def voice_outbound():
    """Handle outbound TTS calls — Plivo fetches this when the call connects."""
    msg = request.args.get("msg", "Hello, this is a notification from Claude.")
    log(f"Outbound TTS: {msg[:100]}")

    response = plivo.xml.ResponseElement()
    response.add(plivo.xml.SpeakElement(msg, voice=VOICE))

    return plivo_xml_response(response)


# ─── Inbound Call Answer ─────────────────────────────────────────────────────

@app.route("/voice/answer", methods=["POST", "GET"])
def voice_answer():
    """Handle inbound calls — greet caller and start speech input."""
    caller = request.values.get("From", "unknown")
    log(f"Inbound call from {caller}")

    # Check allowlist
    allowed = CFG.get("allowed_inbound", [])
    if allowed and caller not in allowed:
        log(f"BLOCKED inbound call from {caller} (not in allowlist)")
        response = plivo.xml.ResponseElement()
        response.add(plivo.xml.SpeakElement(
            "This number is not authorized.", voice=VOICE
        ))
        response.add(plivo.xml.HangupElement())
        return plivo_xml_response(response)

    notify(f"[Voice] Inbound call from {caller}")

    webhook_base = CFG.get("webhook_base_url", "")
    response = plivo.xml.ResponseElement()
    response.add(plivo.xml.SpeakElement(
        "Hello, this is Claude. What would you like to say?", voice=VOICE
    ))

    get_input = plivo.xml.GetInputElement(
        action=f"{webhook_base}/voice/input",
        method="POST",
        input_type="speech",
        speech_end_timeout="2000",
        speech_model="enhanced",
        hints="Claude,code,session",
    )
    get_input.add(plivo.xml.SpeakElement("I'm listening.", voice=VOICE))
    response.add(get_input)

    return plivo_xml_response(response)


# ─── Speech Input Handler ────────────────────────────────────────────────────

def find_latest_transcript(session: str) -> Path | None:
    """Find the most recently modified JSONL transcript for a session."""
    transcript_dir = Path(CFG.get("transcript_dir", str(Path.home() / ".claude" / "projects")))

    # Claude Code uses directory-based project names with dashes replacing slashes
    # e.g., /home/clawdbot/general -> -home-clawdbot-general
    possible_dirs = []

    # Try common session directory patterns
    for entry in transcript_dir.iterdir():
        if entry.is_dir() and session in entry.name:
            possible_dirs.append(entry)

    if not possible_dirs:
        log(f"No transcript directory found for session '{session}'")
        return None

    # Find the most recent .jsonl file across all matching directories
    latest = None
    latest_mtime = 0.0
    for d in possible_dirs:
        for f in d.glob("*.jsonl"):
            mtime = f.stat().st_mtime
            if mtime > latest_mtime:
                latest = f
                latest_mtime = mtime

    return latest


def extract_claude_response(transcript_path: Path, after_timestamp: float) -> str | None:
    """
    Read the transcript JSONL and find the first assistant text response
    that was created after the given timestamp.
    """
    if not transcript_path or not transcript_path.exists():
        return None

    # Read lines in reverse for efficiency (newest last)
    lines = transcript_path.read_text().strip().split("\n")
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        # Only look at assistant messages
        if entry.get("type") != "assistant":
            continue

        msg = entry.get("message", {})
        if msg.get("role") != "assistant":
            continue

        # Check timestamp
        ts_str = entry.get("timestamp", "")
        if ts_str:
            try:
                entry_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
                if entry_ts <= after_timestamp:
                    continue
            except (ValueError, TypeError):
                continue

        # Extract text content
        content = msg.get("content", [])
        if isinstance(content, str):
            return content

        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "").strip()
                if text:
                    return text

    return None


def inject_into_session(session: str, caller: str, speech: str) -> bool:
    """Inject transcribed speech into a Claude Code tmux session."""
    tmux_target = f"claude-{session}"
    text = f"[Voice from {caller}]: {speech}"

    try:
        # Use tmux send-keys with -l (literal) to avoid key interpretation
        subprocess.run(
            ["tmux", "send-keys", "-t", tmux_target, "-l", text],
            capture_output=True, timeout=5, check=True,
        )
        subprocess.run(
            ["tmux", "send-keys", "-t", tmux_target, "Enter"],
            capture_output=True, timeout=5, check=True,
        )
        log(f"Injected into tmux session '{tmux_target}': {text[:80]}")
        return True
    except subprocess.CalledProcessError as e:
        log(f"ERROR: Failed to inject into tmux '{tmux_target}': {e.stderr.decode()}")
        return False
    except Exception as e:
        log(f"ERROR: Failed to inject into tmux '{tmux_target}': {e}")
        return False


def poll_for_response(session: str, injection_time: float, timeout: float = 30.0) -> str:
    """
    Poll the Claude transcript for a response after injection.
    Returns the response text or a timeout message.
    """
    transcript = find_latest_transcript(session)
    if not transcript:
        return "I couldn't find my session transcript. Please try again."

    deadline = time.time() + timeout
    poll_interval = 1.0

    while time.time() < deadline:
        time.sleep(poll_interval)
        response = extract_claude_response(transcript, injection_time)
        if response:
            # Truncate for TTS
            if len(response) > TTS_MAX_CHARS:
                response = response[:TTS_MAX_CHARS] + "... Message truncated."
            return response
        # Increase poll interval gradually
        poll_interval = min(poll_interval * 1.2, 3.0)

    return "I'm still thinking. Please call back in a moment."


@app.route("/voice/input", methods=["POST"])
def voice_input():
    """Handle speech input from inbound calls — inject into Claude and return response."""
    caller = request.values.get("From", "unknown")
    speech = request.values.get("Speech", "")

    if not speech:
        # No speech detected — prompt again
        log(f"No speech detected from {caller}")
        webhook_base = CFG.get("webhook_base_url", "")
        response = plivo.xml.ResponseElement()
        get_input = plivo.xml.GetInputElement(
            action=f"{webhook_base}/voice/input",
            method="POST",
            input_type="speech",
            speech_end_timeout="2000",
        )
        get_input.add(plivo.xml.SpeakElement(
            "I didn't catch that. Please try again.", voice=VOICE
        ))
        response.add(get_input)
        return plivo_xml_response(response)

    log(f"Speech from {caller}: {speech}")
    notify(f"[Voice] {caller} said: {speech[:200]}")

    session = CFG.get("inbound_session", "general")
    injection_time = time.time()

    # Inject speech into Claude session
    if not inject_into_session(session, caller, speech):
        response = plivo.xml.ResponseElement()
        response.add(plivo.xml.SpeakElement(
            "I'm sorry, I couldn't reach the Claude session right now. "
            "Please try again later.", voice=VOICE
        ))
        return plivo_xml_response(response)

    # Poll for Claude's response
    claude_response = poll_for_response(session, injection_time)
    log(f"Claude response to {caller}: {claude_response[:100]}")

    # Build response with option for follow-up
    webhook_base = CFG.get("webhook_base_url", "")
    response = plivo.xml.ResponseElement()
    response.add(plivo.xml.SpeakElement(claude_response, voice=VOICE))

    get_input = plivo.xml.GetInputElement(
        action=f"{webhook_base}/voice/input",
        method="POST",
        input_type="speech",
        speech_end_timeout="2000",
    )
    get_input.add(plivo.xml.SpeakElement("Anything else?", voice=VOICE))
    response.add(get_input)

    return plivo_xml_response(response)


# ─── Health Check ─────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({
        "status": "ok",
        "service": "voice-call-webhook",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "from_number": CFG.get("from_number", "not configured"),
        "webhook_port": CFG.get("webhook_port", 5050),
    })


# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = CFG.get("webhook_port", 5050)
    log(f"Starting Voice Call webhook on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
