#!/usr/bin/env python3
"""AiSpace Homepage — front door to all services.

Serves a landing page with live status checks for every service.

Usage:
    python3 server.py [--port 8098]
"""

import argparse
import json
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

STATIC_DIR = Path(__file__).parent

SERVICES = [
    {
        "name": "Drift",
        "desc": "Generative art — Perlin flow fields, particles, and light",
        "port": 8091,
        "icon": "🌊",
        "category": "creative",
    },
    {
        "name": "Synapse",
        "desc": "Live bioluminescent network visualization",
        "port": 8093,
        "icon": "🧠",
        "category": "creative",
    },
    {
        "name": "Synesthesia",
        "desc": "Network data as audiovisual experience",
        "port": 8096,
        "icon": "🎵",
        "category": "creative",
    },
    {
        "name": "Memory Cartography",
        "desc": "Visual map of 900+ memories in embedding space",
        "port": 8097,
        "icon": "🗺️",
        "category": "memory",
    },
    {
        "name": "Memory Explorer",
        "desc": "Browse and search the MCP knowledge graph",
        "port": 8092,
        "icon": "🔍",
        "category": "memory",
    },
    {
        "name": "NetSight",
        "desc": "Live network dashboard — bandwidth, devices, status",
        "port": 8089,
        "icon": "📡",
        "category": "network",
    },
    {
        "name": "WebDash",
        "desc": "Network status overview dashboard",
        "port": 8088,
        "icon": "📊",
        "category": "network",
    },
    {
        "name": "Hilo Target",
        "desc": "Smart energy target calculator API",
        "port": 8095,
        "icon": "⚡",
        "category": "tools",
    },
    {
        "name": "Memory Agent",
        "desc": "Semantic memory search and extraction API",
        "port": 8094,
        "icon": "💾",
        "category": "memory",
        "health_path": "/health",
    },
    {
        "name": "Claude API",
        "desc": "OpenAI-compatible API wrapper for Claude",
        "port": 8080,
        "icon": "🤖",
        "category": "tools",
    },
]


def check_service(svc):
    """Check if a service is responding."""
    path = svc.get("health_path", "/")
    url = f"http://127.0.0.1:{svc['port']}{path}"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return {"name": svc["name"], "status": "up", "code": resp.status}
    except Exception:
        return {"name": svc["name"], "status": "down", "code": 0}


def check_all():
    """Check all services concurrently."""
    results = {}
    with ThreadPoolExecutor(max_workers=len(SERVICES)) as pool:
        futures = {pool.submit(check_service, s): s["name"] for s in SERVICES}
        for f in as_completed(futures):
            r = f.result()
            results[r["name"]] = r["status"]
    return results


class HomepageHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.serve_file("index.html", "text/html")
        elif self.path == "/api/services":
            self.send_json(SERVICES)
        elif self.path == "/api/status":
            self.send_json(check_all())
        else:
            self.send_error(404)

    def serve_file(self, filename, content_type):
        filepath = STATIC_DIR / filename
        if filepath.exists():
            content = filepath.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", len(content))
            self.end_headers()
            self.wfile.write(content)
        else:
            self.send_error(404)

    def send_json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description="AiSpace Homepage")
    parser.add_argument("--port", type=int, default=8098)
    args = parser.parse_args()

    print("AiSpace Homepage")
    print("=" * 40)
    print(f"Tracking {len(SERVICES)} services")

    server = HTTPServer(("0.0.0.0", args.port), HomepageHandler)
    print(f"Starting on http://0.0.0.0:{args.port}")
    print(f"Open in browser: http://192.168.53.247:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")


if __name__ == "__main__":
    main()
