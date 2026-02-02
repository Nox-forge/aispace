#!/usr/bin/env python3
"""
Voice Call — Outbound call CLI + utilities using Plivo.
Make TTS phone calls and check call status via the Plivo API.
Built by Claude (Opus 4.5).
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote_plus

# ─── Configuration ────────────────────────────────────────────────────────────

CONFIG_PATH = Path.home() / ".voice-call.json"


def load_config() -> dict:
    """Load configuration from ~/.voice-call.json."""
    if not CONFIG_PATH.exists():
        print(f"ERROR: Config file not found: {CONFIG_PATH}", file=sys.stderr)
        print("Create it with your Plivo credentials. See template:", file=sys.stderr)
        print(json.dumps({
            "plivo_auth_id": "YOUR_AUTH_ID",
            "plivo_auth_token": "YOUR_AUTH_TOKEN",
            "from_number": "+1XXXXXXXXXX",
            "webhook_port": 5050,
            "webhook_base_url": "https://your-hostname.example",
            "allowed_inbound": ["+1YYYYYYYYYY"],
            "inbound_session": "general",
            "default_to": "+1YYYYYYYYYY",
            "transcript_dir": "/home/clawdbot/.claude/projects",
        }, indent=2), file=sys.stderr)
        sys.exit(1)

    with open(CONFIG_PATH) as f:
        return json.load(f)


def notify(message: str):
    """Send a Telegram notification (best-effort)."""
    try:
        subprocess.run(
            ["send-telegram", message],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass


# ─── Commands ─────────────────────────────────────────────────────────────────

def cmd_call(args):
    """Make an outbound TTS call via Plivo."""
    import plivo

    cfg = load_config()
    to_number = args.to or cfg.get("default_to")
    if not to_number:
        print("ERROR: No --to number specified and no default_to in config.", file=sys.stderr)
        sys.exit(1)

    message = args.message
    if not message:
        print("ERROR: --message is required.", file=sys.stderr)
        sys.exit(1)

    client = plivo.RestClient(cfg["plivo_auth_id"], cfg["plivo_auth_token"])

    # Build answer URL with URL-encoded message
    answer_url = f"{cfg['webhook_base_url']}/voice/outbound?msg={quote_plus(message)}"

    print(f"Calling {to_number} with message: {message[:80]}...", file=sys.stderr)

    try:
        response = client.calls.create(
            from_=cfg["from_number"],
            to_=to_number,
            answer_url=answer_url,
            answer_method="GET",
        )
        call_uuid = response.request_uuid if hasattr(response, "request_uuid") else str(response)
        print(f"Call initiated. UUID: {call_uuid}")
        notify(f"[Voice] Outbound call to {to_number}: {message[:100]}")
    except Exception as e:
        print(f"ERROR: Failed to create call: {e}", file=sys.stderr)
        notify(f"[Voice] FAILED to call {to_number}: {e}")
        sys.exit(1)


def cmd_status(args):
    """Check status of a call by UUID."""
    import plivo

    cfg = load_config()
    client = plivo.RestClient(cfg["plivo_auth_id"], cfg["plivo_auth_token"])

    try:
        call = client.calls.get(args.call_uuid)
        print(json.dumps({
            "uuid": call.call_uuid if hasattr(call, "call_uuid") else args.call_uuid,
            "from": getattr(call, "from_number", "N/A"),
            "to": getattr(call, "to_number", "N/A"),
            "status": getattr(call, "call_status", "N/A"),
            "direction": getattr(call, "call_direction", "N/A"),
            "duration": getattr(call, "call_duration", "N/A"),
            "end_time": getattr(call, "end_time", "N/A"),
        }, indent=2))
    except Exception as e:
        print(f"ERROR: Failed to get call status: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_test(args):
    """Verify config and Plivo connectivity."""
    import plivo

    cfg = load_config()
    print("Config loaded successfully.", file=sys.stderr)
    print(f"  Auth ID:      {cfg['plivo_auth_id'][:8]}...", file=sys.stderr)
    print(f"  From number:  {cfg['from_number']}", file=sys.stderr)
    print(f"  Webhook URL:  {cfg['webhook_base_url']}", file=sys.stderr)
    print(f"  Webhook port: {cfg['webhook_port']}", file=sys.stderr)

    client = plivo.RestClient(cfg["plivo_auth_id"], cfg["plivo_auth_token"])

    print("\nTesting Plivo API connectivity...", file=sys.stderr)
    try:
        numbers = client.numbers.list(limit=5)
        count = len(numbers.objects) if hasattr(numbers, "objects") else 0
        print(f"  API connection OK. Numbers on account: {count}", file=sys.stderr)
        if hasattr(numbers, "objects"):
            for num in numbers.objects:
                number_str = getattr(num, "number", "N/A")
                print(f"    - {number_str}", file=sys.stderr)
        print("\nAll checks passed.", file=sys.stderr)
    except Exception as e:
        print(f"  API connection FAILED: {e}", file=sys.stderr)
        sys.exit(1)


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="voice-call",
        description="Plivo voice call CLI — outbound TTS calls and status checks.",
    )
    sub = parser.add_subparsers(dest="command", help="Available commands")

    # call
    p_call = sub.add_parser("call", help="Make an outbound TTS call")
    p_call.add_argument("--to", help="Destination phone number (E.164 format)")
    p_call.add_argument("--message", "-m", required=True, help="Message to speak via TTS")
    p_call.set_defaults(func=cmd_call)

    # status
    p_status = sub.add_parser("status", help="Check call status by UUID")
    p_status.add_argument("call_uuid", help="Call UUID to query")
    p_status.set_defaults(func=cmd_status)

    # test
    p_test = sub.add_parser("test", help="Verify config and Plivo connectivity")
    p_test.set_defaults(func=cmd_test)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
