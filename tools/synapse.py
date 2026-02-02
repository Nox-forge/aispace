#!/usr/bin/env python3
"""
Synapse — the network, alive.
A bioluminescent visualization of live network topology.
Built by Nox (Claude Opus 4.5).

Usage:
  synapse              Start web visualization (default port 8093)
  synapse --port 9000  Custom port
"""

import json
import http.server
import os
import ssl
import sys
import time
import urllib.request
import urllib.error

# ─── Configuration ────────────────────────────────────────────────────────────

UNIFI_HOST = "192.168.53.1"
UNIFI_URL = f"https://{UNIFI_HOST}"
UNIFI_USER = "nox"
UNIFI_PASS = "ONgbEc5oVWhDq1vLOpXKn99"
WEB_PORT = 8093
CACHE_TTL = 15
HTML_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "projects", "synapse")

# ─── UniFi Client ─────────────────────────────────────────────────────────────

class UniFiClient:
    def __init__(self):
        self._cookie = None
        self._csrf = None
        self._ctx = ssl.create_default_context()
        self._ctx.check_hostname = False
        self._ctx.verify_mode = ssl.CERT_NONE
        self._cache = {}
        self._cache_time = {}

    def _login(self):
        data = json.dumps({"username": UNIFI_USER, "password": UNIFI_PASS}).encode()
        req = urllib.request.Request(
            f"{UNIFI_URL}/api/auth/login", data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        resp = urllib.request.urlopen(req, context=self._ctx)
        for header in resp.headers.get_all("Set-Cookie") or []:
            if "TOKEN=" in header:
                self._cookie = header.split(";")[0]
        csrf = resp.headers.get("X-Updated-Csrf-Token") or resp.headers.get("X-Csrf-Token")
        if csrf:
            self._csrf = csrf

    def _request(self, path):
        now = time.time()
        if path in self._cache and now - self._cache_time.get(path, 0) < CACHE_TTL:
            return self._cache[path]

        if not self._cookie:
            self._login()

        url = f"{UNIFI_URL}{path}"
        headers = {}
        if self._cookie:
            headers["Cookie"] = self._cookie
        if self._csrf:
            headers["X-CSRF-Token"] = self._csrf

        req = urllib.request.Request(url, headers=headers)
        try:
            resp = urllib.request.urlopen(req, context=self._ctx)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                self._cookie = None
                self._login()
                headers["Cookie"] = self._cookie
                if self._csrf:
                    headers["X-CSRF-Token"] = self._csrf
                req = urllib.request.Request(url, headers=headers)
                resp = urllib.request.urlopen(req, context=self._ctx)
            else:
                raise

        result = json.loads(resp.read().decode())
        self._cache[path] = result
        self._cache_time[path] = now
        return result

    def get_devices(self):
        return self._request("/proxy/network/api/s/default/stat/device").get("data", [])

    def get_clients(self):
        return self._request("/proxy/network/api/s/default/stat/sta").get("data", [])


# ─── Build network graph ──────────────────────────────────────────────────────

def build_network(unifi):
    devices = unifi.get_devices()
    clients = unifi.get_clients()

    nodes = []
    node_ids = set()
    fibers = []

    # Infrastructure devices (gateway, switch, AP)
    infra_map = {}  # mac -> node id
    for d in devices:
        mac = d.get("mac", "")
        name = d.get("name", d.get("model", "device"))
        ip = d.get("ip", "")
        tx = d.get("tx_bytes", 0) or 0
        rx = d.get("rx_bytes", 0) or 0
        node_id = mac or name
        infra_map[mac] = node_id
        node_ids.add(node_id)
        nodes.append({
            "mac": node_id,
            "name": name,
            "ip": ip,
            "is_infra": True,
            "tx_bytes": tx,
            "rx_bytes": rx,
        })

    # Connect infra to each other
    infra_ids = list(infra_map.values())
    for i in range(len(infra_ids)):
        for j in range(i + 1, len(infra_ids)):
            fibers.append([infra_ids[i], infra_ids[j]])

    # Client devices
    for c in clients:
        mac = c.get("mac", "")
        name = c.get("hostname", c.get("name", c.get("oui", ""))) or mac[:8]
        ip = c.get("ip", "")
        tx = c.get("tx_bytes", 0) or 0
        rx = c.get("rx_bytes", 0) or 0
        ap_mac = c.get("ap_mac", "")
        sw_mac = c.get("sw_mac", "")

        if mac in node_ids:
            continue
        node_ids.add(mac)

        nodes.append({
            "mac": mac,
            "hostname": name,
            "ip": ip,
            "is_infra": False,
            "tx_bytes": tx,
            "rx_bytes": rx,
        })

        # Connect client to its AP or switch
        connected_to = infra_map.get(ap_mac) or infra_map.get(sw_mac)
        if connected_to:
            fibers.append([mac, connected_to])
        elif infra_ids:
            # Fallback: connect to first infra
            fibers.append([mac, infra_ids[0]])

    return {"nodes": nodes, "fibers": fibers}


# ─── HTTP Server ──────────────────────────────────────────────────────────────

unifi_client = UniFiClient()

class SynapseHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Silent

    def do_GET(self):
        if self.path == "/api/network":
            try:
                data = build_network(unifi_client)
            except Exception as e:
                data = {"nodes": [], "fibers": [], "error": str(e)}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

        elif self.path == "/" or self.path == "/index.html":
            html_path = os.path.join(HTML_DIR, "index.html")
            try:
                with open(html_path, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(content)
            except FileNotFoundError:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"index.html not found")
        else:
            self.send_response(404)
            self.end_headers()


def main():
    port = WEB_PORT
    for i, arg in enumerate(sys.argv[1:]):
        if arg == "--port" and i + 1 < len(sys.argv) - 1:
            port = int(sys.argv[i + 2])

    server = http.server.HTTPServer(("0.0.0.0", port), SynapseHandler)
    print(f"Synapse — http://0.0.0.0:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()


if __name__ == "__main__":
    main()
