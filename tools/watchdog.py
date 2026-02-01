#!/usr/bin/env python3
"""
Watchdog — Network service monitor with Telegram alerts.
Periodically checks services and notifies via send-telegram when status changes.
Built by Claude (Opus 4.5).
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ─── Configuration ────────────────────────────────────────────────────────────

CHECK_INTERVAL = 300  # seconds between checks (5 minutes)
STATE_FILE = Path.home() / "aispace" / "logs" / "watchdog_state.json"

SERVICES = {
    "NAS DSM":           ("192.168.53.73", 5001),
    "NAS SMB":           ("192.168.53.73", 445),
    "Home Assistant":    ("192.168.53.246", 8123),
    "Plex":              ("192.168.56.231", 32400),
    "Audiobookshelf":    ("192.168.56.231", 13378),
    "Radarr":            ("192.168.56.244", 7878),
    "Sonarr":            ("192.168.56.244", 8989),
    "Readarr":           ("192.168.56.244", 8787),
    "Prowlarr":          ("192.168.56.244", 9696),
    "Jellyfin":          ("192.168.56.244", 8096),
    "qBittorrent":       ("192.168.56.244", 8080),
    "Ombi":              ("192.168.56.244", 5000),
    "UniFi":             ("192.168.53.1", 443),
}


# ─── Checks ───────────────────────────────────────────────────────────────────

async def check_port(ip: str, port: int, timeout: float = 5.0) -> bool:
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=timeout
        )
        writer.close()
        await writer.wait_closed()
        return True
    except:
        return False


async def check_all() -> dict[str, bool]:
    """Check all services, return name -> is_up mapping."""
    tasks = {}
    for name, (ip, port) in SERVICES.items():
        tasks[name] = check_port(ip, port)

    results = {}
    for name, task in tasks.items():
        results[name] = await task
    return results


# ─── State Management ─────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ─── Notifications ────────────────────────────────────────────────────────────

def notify(message: str):
    """Send notification via Telegram."""
    print(f"[{datetime.now():%H:%M:%S}] NOTIFY: {message}")
    try:
        subprocess.run(
            ["send-telegram", message],
            timeout=10, capture_output=True,
        )
    except Exception as e:
        print(f"  Failed to send notification: {e}")


# ─── Main Loop ────────────────────────────────────────────────────────────────

async def run_once():
    """Run a single check cycle. Returns (results, changes)."""
    prev_state = load_state()
    results = await check_all()

    now = datetime.now().isoformat()
    changes = []

    for name, is_up in results.items():
        was_up = prev_state.get(name, {}).get("up")

        if was_up is not None and was_up != is_up:
            if is_up:
                changes.append(f"  ✅ {name} is BACK UP")
            else:
                changes.append(f"  🔴 {name} is DOWN")

        prev_state[name] = {"up": is_up, "last_check": now}

    save_state(prev_state)
    return results, changes


async def main():
    one_shot = "--once" in sys.argv
    quiet = "--quiet" in sys.argv

    if not quiet:
        print(f"Watchdog — monitoring {len(SERVICES)} services")
        print(f"Check interval: {CHECK_INTERVAL}s")
        print(f"State file: {STATE_FILE}")
        print()

    while True:
        results, changes = await run_once()

        # Print status
        up = sum(1 for v in results.values() if v)
        down = len(results) - up

        if not quiet:
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {up}/{len(results)} services up", end="")
            if down:
                down_names = [n for n, v in results.items() if not v]
                print(f" | DOWN: {', '.join(down_names)}")
            else:
                print(" | All healthy")

        # Send notifications for changes
        if changes:
            msg = f"🐕 Watchdog Alert\n" + "\n".join(changes)
            notify(msg)

        if one_shot:
            break

        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nWatchdog stopped.")
