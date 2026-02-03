#!/usr/bin/env python3
"""
Synesthesia Server — serves the audiovisual experience.

Provides:
- Static HTML/JS
- /api/clients endpoint with UniFi data

Usage:
    python3 server.py [--port 8095]
"""

import json
import os
import sys
import ssl
import urllib.request
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import base64

# Configuration
PORT = 8095
UNIFI_HOST = "192.168.53.1"
UNIFI_USER = "nox"
UNIFI_PASS = "ONgbEc5oVWhDq1vLOpXKn99"

# UniFi session
unifi_cookie = None
unifi_csrf = None
ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE


def unifi_login():
    global unifi_cookie, unifi_csrf

    url = f"https://{UNIFI_HOST}/api/auth/login"
    data = json.dumps({"username": UNIFI_USER, "password": UNIFI_PASS}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})

    try:
        resp = urllib.request.urlopen(req, context=ssl_ctx, timeout=10)
        for cookie in resp.headers.get_all('Set-Cookie') or []:
            if 'TOKEN=' in cookie:
                unifi_cookie = cookie.split(';')[0]
                token = unifi_cookie.split('=')[1]
                payload = token.split('.')[1]
                payload += '=' * (4 - len(payload) % 4)
                decoded = json.loads(base64.urlsafe_b64decode(payload))
                unifi_csrf = decoded.get('csrfToken', '')
        return True
    except Exception as e:
        print(f"UniFi login failed: {e}")
        return False


def get_unifi_clients():
    global unifi_cookie, unifi_csrf

    if not unifi_cookie and not unifi_login():
        return []

    url = f"https://{UNIFI_HOST}/proxy/network/api/s/default/stat/sta"
    headers = {"Cookie": unifi_cookie, "X-CSRF-Token": unifi_csrf}
    req = urllib.request.Request(url, headers=headers)

    try:
        resp = urllib.request.urlopen(req, context=ssl_ctx, timeout=10)
        data = json.loads(resp.read().decode())
        return data.get('data', [])
    except urllib.error.HTTPError as e:
        if e.code == 401:
            unifi_cookie = None
            if unifi_login():
                return get_unifi_clients()
        return []
    except Exception as e:
        print(f"Failed to get clients: {e}")
        return []


class SynesthesiaHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        # Serve from the project directory
        super().__init__(*args, directory=os.path.dirname(os.path.abspath(__file__)), **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == '/api/clients':
            self.send_json_response(get_unifi_clients())
        elif parsed.path == '/' or parsed.path == '/index.html':
            self.path = '/index.html'
            super().do_GET()
        else:
            super().do_GET()

    def send_json_response(self, data):
        response = json.dumps({'clients': data}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(response))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format, *args):
        # Quieter logging
        if '/api/' not in args[0]:
            print(f"[{self.log_date_time_string()}] {args[0]}")


def main():
    port = PORT

    # Parse args
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == '--port' and i + 1 < len(args):
            port = int(args[i + 1])

    print(f"Synesthesia Server")
    print(f"=" * 40)
    print(f"Starting on http://0.0.0.0:{port}")
    print(f"Open in browser: http://192.168.53.247:{port}")
    print()

    server = HTTPServer(('0.0.0.0', port), SynesthesiaHandler)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == '__main__':
    main()
