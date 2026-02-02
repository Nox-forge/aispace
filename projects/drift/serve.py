#!/usr/bin/env python3
"""Drift — generative art server. Serves on port 8091."""

import http.server
import os
import sys

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8091
DIR = os.path.dirname(os.path.abspath(__file__))

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIR, **kwargs)

    def log_message(self, format, *args):
        pass  # Silent

if __name__ == '__main__':
    server = http.server.HTTPServer(('0.0.0.0', PORT), Handler)
    print(f'Drift serving at http://0.0.0.0:{PORT}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
