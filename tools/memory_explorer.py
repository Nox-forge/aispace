#!/usr/bin/env python3
"""Memory Explorer — Web tool to browse and edit Claude's memory systems.

Serves on port 8092. Provides:
- CLAUDE.md file viewer/editor
- MCP memory graph browser (entities, relations, observations)
- Interactive graph visualization (vis-network)
- CRUD operations on all memory components
- Search/filter across everything
"""

import json
import os
import re
import shutil
import sys
import glob
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

PORT = 8092
LOG_DIR = os.path.expanduser("~/aispace/logs")
MEMORY_FILE = os.path.expanduser("~/.claude/memory.json")
CLAUDE_MD_PATHS = [
    os.path.expanduser("~/CLAUDE.md"),
    os.path.expanduser("~/setup/.claude/CLAUDE.md") if os.path.exists(os.path.expanduser("~/setup/.claude/CLAUDE.md")) else None,
]

def find_claude_md_files():
    """Find all CLAUDE.md files, excluding node_modules and .git."""
    home = os.path.expanduser("~")
    results = []
    for root, dirs, files in os.walk(home):
        dirs[:] = [d for d in dirs if d not in ('node_modules', '.git', '__pycache__', '.venv', 'venv')]
        if 'CLAUDE.md' in files:
            path = os.path.join(root, 'CLAUDE.md')
            rel = os.path.relpath(path, home)
            results.append({"path": path, "label": f"~/{rel}"})
    return sorted(results, key=lambda x: x["label"])

def read_memory_graph():
    """Read the NDJSON memory file and return entities and relations."""
    entities = []
    relations = []
    if not os.path.exists(MEMORY_FILE):
        return entities, relations
    with open(MEMORY_FILE, 'r') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if obj.get("type") == "entity":
                    entities.append(obj)
                elif obj.get("type") == "relation":
                    relations.append(obj)
            except json.JSONDecodeError:
                continue
    return entities, relations

def write_memory_graph(entities, relations):
    """Write entities and relations back to the NDJSON memory file."""
    backup = MEMORY_FILE + f".bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    if os.path.exists(MEMORY_FILE):
        shutil.copy2(MEMORY_FILE, backup)
    with open(MEMORY_FILE, 'w') as f:
        for e in entities:
            f.write(json.dumps(e) + '\n')
        for r in relations:
            f.write(json.dumps(r) + '\n')

class MemoryExplorerHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Silence request logs

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html):
        body = html.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        length = int(self.headers.get('Content-Length', 0))
        return self.rfile.read(length)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == '/':
            self.send_html(HTML)
        elif path == '/api/claude-md/files':
            files = find_claude_md_files()
            self.send_json(files)
        elif path == '/api/claude-md/read':
            fpath = params.get('path', [None])[0]
            if not fpath or not os.path.exists(fpath):
                self.send_json({"error": "File not found"}, 404)
                return
            with open(fpath, 'r') as f:
                content = f.read()
            self.send_json({"path": fpath, "content": content})
        elif path == '/api/graph':
            entities, relations = read_memory_graph()
            self.send_json({"entities": entities, "relations": relations})
        elif path == '/api/graph/stats':
            entities, relations = read_memory_graph()
            types = {}
            for e in entities:
                t = e.get("entityType", "unknown")
                types[t] = types.get(t, 0) + 1
            total_obs = sum(len(e.get("observations", [])) for e in entities)
            self.send_json({
                "entity_count": len(entities),
                "relation_count": len(relations),
                "observation_count": total_obs,
                "entity_types": types,
                "file_size": os.path.getsize(MEMORY_FILE) if os.path.exists(MEMORY_FILE) else 0,
            })
        elif path == '/api/dream/logs':
            self.handle_dream_logs()
        elif path == '/api/dream/log':
            date = params.get('date', [None])[0]
            self.handle_dream_log_detail(date)
        else:
            self.send_response(404)
            self.end_headers()

    def handle_dream_logs(self):
        """List all dream log files, newest first."""
        logs = []
        if os.path.isdir(LOG_DIR):
            # Collect all dream log dates
            dates = set()
            for f in os.listdir(LOG_DIR):
                m = re.match(r'^dream-(\d{4}-\d{2}-\d{2})\.(json|log)$', f)
                if m:
                    dates.add(m.group(1))
            for date in sorted(dates, reverse=True):
                json_path = os.path.join(LOG_DIR, f"dream-{date}.json")
                log_path = os.path.join(LOG_DIR, f"dream-{date}.log")
                if os.path.exists(json_path):
                    try:
                        with open(json_path, 'r') as f:
                            data = json.load(f)
                        logs.append({
                            "date": data.get("date", date),
                            "timestamp": data.get("timestamp", ""),
                            "total_issues": data.get("total_issues", 0),
                            "summary": data.get("summary", ""),
                            "has_details": True,
                        })
                    except (json.JSONDecodeError, IOError):
                        logs.append(self._parse_log_summary(date, log_path))
                elif os.path.exists(log_path):
                    logs.append(self._parse_log_summary(date, log_path))
        self.send_json(logs)

    def _parse_log_summary(self, date, log_path):
        """Parse a .log file to extract a summary entry."""
        total_issues = 0
        summary = "Log file only"
        try:
            with open(log_path, 'r') as f:
                text = f.read()
            m = re.search(r'Total issues:\s*(\d+)', text)
            if m:
                total_issues = int(m.group(1))
            if total_issues == 0:
                summary = "All coherent"
            else:
                # Extract category issue counts
                parts = []
                for cm in re.finditer(r'── (.+?) \[(\d+) issue', text):
                    cat_name = cm.group(1)
                    # Strip leading number prefix like "1. "
                    cat_name = re.sub(r'^\d+\.\s*', '', cat_name)
                    parts.append(f"{cm.group(2)} {cat_name.lower()}")
                summary = ", ".join(parts) if parts else f"{total_issues} issue(s)"
        except IOError:
            pass
        return {
            "date": date,
            "timestamp": "",
            "total_issues": total_issues,
            "summary": summary,
            "has_details": False,
        }

    def handle_dream_log_detail(self, date):
        """Return full dream log details for a specific date."""
        if not date or not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
            self.send_json({"error": "Invalid date format, use YYYY-MM-DD"}, 400)
            return
        json_path = os.path.join(LOG_DIR, f"dream-{date}.json")
        log_path = os.path.join(LOG_DIR, f"dream-{date}.log")
        if os.path.exists(json_path):
            try:
                with open(json_path, 'r') as f:
                    data = json.load(f)
                self.send_json(data)
                return
            except (json.JSONDecodeError, IOError):
                pass
        if os.path.exists(log_path):
            try:
                with open(log_path, 'r') as f:
                    text = f.read()
                self.send_json({"date": date, "raw_log": text})
                return
            except IOError:
                pass
        self.send_json({"error": "No dream log found for this date"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = json.loads(self.read_body()) if self.headers.get('Content-Length') else {}

        if path == '/api/claude-md/write':
            fpath = body.get('path')
            content = body.get('content')
            if not fpath:
                self.send_json({"error": "Missing path"}, 400)
                return
            backup = fpath + f".bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            if os.path.exists(fpath):
                shutil.copy2(fpath, backup)
            with open(fpath, 'w') as f:
                f.write(content)
            self.send_json({"ok": True, "backup": backup})

        elif path == '/api/graph/entity/create':
            name = body.get('name', '').strip()
            etype = body.get('entityType', '').strip()
            observations = body.get('observations', [])
            if not name or not etype:
                self.send_json({"error": "Name and entityType required"}, 400)
                return
            entities, relations = read_memory_graph()
            for e in entities:
                if e['name'] == name:
                    self.send_json({"error": f"Entity '{name}' already exists"}, 409)
                    return
            entities.append({"type": "entity", "name": name, "entityType": etype, "observations": observations})
            write_memory_graph(entities, relations)
            self.send_json({"ok": True})

        elif path == '/api/graph/entity/update':
            old_name = body.get('old_name', '').strip()
            new_name = body.get('name', '').strip()
            etype = body.get('entityType', '').strip()
            observations = body.get('observations')
            if not old_name:
                self.send_json({"error": "old_name required"}, 400)
                return
            entities, relations = read_memory_graph()
            found = False
            for e in entities:
                if e['name'] == old_name:
                    if new_name:
                        e['name'] = new_name
                    if etype:
                        e['entityType'] = etype
                    if observations is not None:
                        e['observations'] = observations
                    found = True
                    break
            if not found:
                self.send_json({"error": "Entity not found"}, 404)
                return
            # Update relations if name changed
            if new_name and new_name != old_name:
                for r in relations:
                    if r.get('from') == old_name:
                        r['from'] = new_name
                    if r.get('to') == old_name:
                        r['to'] = new_name
            write_memory_graph(entities, relations)
            self.send_json({"ok": True})

        elif path == '/api/graph/entity/delete':
            name = body.get('name', '').strip()
            if not name:
                self.send_json({"error": "Name required"}, 400)
                return
            entities, relations = read_memory_graph()
            entities = [e for e in entities if e['name'] != name]
            relations = [r for r in relations if r.get('from') != name and r.get('to') != name]
            write_memory_graph(entities, relations)
            self.send_json({"ok": True})

        elif path == '/api/graph/entity/add-observation':
            name = body.get('name', '').strip()
            observation = body.get('observation', '').strip()
            if not name or not observation:
                self.send_json({"error": "Name and observation required"}, 400)
                return
            entities, relations = read_memory_graph()
            for e in entities:
                if e['name'] == name:
                    e.setdefault('observations', []).append(observation)
                    write_memory_graph(entities, relations)
                    self.send_json({"ok": True})
                    return
            self.send_json({"error": "Entity not found"}, 404)

        elif path == '/api/graph/entity/remove-observation':
            name = body.get('name', '').strip()
            index = body.get('index')
            if not name or index is None:
                self.send_json({"error": "Name and index required"}, 400)
                return
            entities, relations = read_memory_graph()
            for e in entities:
                if e['name'] == name:
                    obs = e.get('observations', [])
                    if 0 <= index < len(obs):
                        obs.pop(index)
                        write_memory_graph(entities, relations)
                        self.send_json({"ok": True})
                        return
                    self.send_json({"error": "Index out of range"}, 400)
                    return
            self.send_json({"error": "Entity not found"}, 404)

        elif path == '/api/graph/entity/update-observation':
            name = body.get('name', '').strip()
            index = body.get('index')
            text = body.get('text', '').strip()
            if not name or index is None or not text:
                self.send_json({"error": "Name, index, and text required"}, 400)
                return
            entities, relations = read_memory_graph()
            for e in entities:
                if e['name'] == name:
                    obs = e.get('observations', [])
                    if 0 <= index < len(obs):
                        obs[index] = text
                        write_memory_graph(entities, relations)
                        self.send_json({"ok": True})
                        return
                    self.send_json({"error": "Index out of range"}, 400)
                    return
            self.send_json({"error": "Entity not found"}, 404)

        elif path == '/api/graph/relation/create':
            frm = body.get('from', '').strip()
            to = body.get('to', '').strip()
            rel = body.get('relationType', '').strip()
            if not frm or not to or not rel:
                self.send_json({"error": "from, to, and relationType required"}, 400)
                return
            entities, relations = read_memory_graph()
            # Check for duplicate
            for r in relations:
                if r.get('from') == frm and r.get('to') == to and r.get('relationType') == rel:
                    self.send_json({"error": "Relation already exists"}, 409)
                    return
            relations.append({"type": "relation", "from": frm, "to": to, "relationType": rel})
            write_memory_graph(entities, relations)
            self.send_json({"ok": True})

        elif path == '/api/graph/relation/delete':
            frm = body.get('from', '').strip()
            to = body.get('to', '').strip()
            rel = body.get('relationType', '').strip()
            if not frm or not to or not rel:
                self.send_json({"error": "from, to, and relationType required"}, 400)
                return
            entities, relations = read_memory_graph()
            relations = [r for r in relations if not (r.get('from') == frm and r.get('to') == to and r.get('relationType') == rel)]
            write_memory_graph(entities, relations)
            self.send_json({"ok": True})

        elif path == '/api/graph/search':
            query = body.get('query', '').lower().strip()
            if not query:
                self.send_json({"entities": [], "relations": []})
                return
            entities, relations = read_memory_graph()
            matched_entities = []
            for e in entities:
                if (query in e.get('name', '').lower()
                    or query in e.get('entityType', '').lower()
                    or any(query in obs.lower() for obs in e.get('observations', []))):
                    matched_entities.append(e)
            matched_relations = []
            matched_names = {e['name'] for e in matched_entities}
            for r in relations:
                if (query in r.get('relationType', '').lower()
                    or r.get('from') in matched_names
                    or r.get('to') in matched_names):
                    matched_relations.append(r)
            self.send_json({"entities": matched_entities, "relations": matched_relations})

        else:
            self.send_response(404)
            self.end_headers()


HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Memory Explorer</title>
<script src="https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>
<style>
:root {
  --bg: #0d1117;
  --bg2: #161b22;
  --bg3: #21262d;
  --border: #30363d;
  --text: #e6edf3;
  --text2: #8b949e;
  --accent: #58a6ff;
  --accent2: #3fb950;
  --danger: #f85149;
  --warn: #d29922;
  --purple: #bc8cff;
  --radius: 8px;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }

/* Layout */
.app { display: flex; height: 100vh; }
.sidebar {
  width: 260px;
  background: var(--bg2);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  transition: transform 0.25s ease;
}
.sidebar-header {
  padding: 16px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  gap: 12px;
}
.sidebar-header h1 {
  font-size: 18px;
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: 8px;
}
.sidebar-header .subtitle { font-size: 12px; color: var(--text2); margin-top: 4px; }
.sidebar-close { display: none; }
.sidebar-nav { flex: 1; overflow-y: auto; padding: 8px; }
.nav-section { margin-bottom: 16px; }
.nav-section-title {
  font-size: 11px;
  text-transform: uppercase;
  color: var(--text2);
  padding: 4px 8px;
  letter-spacing: 0.5px;
}
.nav-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  border-radius: var(--radius);
  cursor: pointer;
  font-size: 14px;
  color: var(--text);
  transition: background 0.15s;
}
.nav-item:hover { background: var(--bg3); }
.nav-item.active { background: var(--accent); color: #fff; }
.nav-item .badge {
  margin-left: auto;
  background: var(--bg3);
  color: var(--text2);
  font-size: 11px;
  padding: 2px 6px;
  border-radius: 10px;
}
.nav-item.active .badge { background: rgba(255,255,255,0.2); color: #fff; }
.sidebar-overlay {
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.5);
  z-index: 49;
}
.hamburger {
  display: none;
  background: none;
  border: none;
  color: var(--text);
  font-size: 22px;
  cursor: pointer;
  padding: 6px 8px;
  border-radius: var(--radius);
  line-height: 1;
  flex-shrink: 0;
}
.hamburger:hover { background: var(--bg3); }
.main { flex: 1; overflow-y: auto; min-width: 0; }
.main-header {
  position: sticky;
  top: 0;
  z-index: 10;
  background: var(--bg);
  border-bottom: 1px solid var(--border);
  padding: 12px 16px;
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.main-header h2 { font-size: 16px; font-weight: 600; white-space: nowrap; }
.header-actions {
  margin-left: auto;
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}
.main-content { padding: 24px; }

/* Search */
.search-bar {
  display: flex;
  gap: 8px;
  margin-bottom: 16px;
}
.search-bar input {
  flex: 1;
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 8px 12px;
  color: var(--text);
  font-size: 14px;
  outline: none;
}
.search-bar input:focus { border-color: var(--accent); }

/* Buttons */
.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 8px 14px;
  border-radius: var(--radius);
  border: 1px solid var(--border);
  background: var(--bg3);
  color: var(--text);
  font-size: 13px;
  cursor: pointer;
  transition: all 0.15s;
  white-space: nowrap;
  min-height: 36px;
  -webkit-tap-highlight-color: transparent;
}
.btn:hover { border-color: var(--text2); }
.btn:active { transform: scale(0.97); }
.btn-primary { background: var(--accent); border-color: var(--accent); color: #fff; }
.btn-primary:hover { opacity: 0.9; }
.btn-danger { background: var(--danger); border-color: var(--danger); color: #fff; }
.btn-danger:hover { opacity: 0.9; }
.btn-sm { padding: 6px 10px; font-size: 12px; min-height: 30px; }

/* Cards */
.card {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  margin-bottom: 12px;
}
.card-header {
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.card-header h3 { font-size: 14px; font-weight: 600; }
.card-body { padding: 16px; }

/* Stats row */
.stats-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 12px;
  margin-bottom: 24px;
}
.stat-card {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 16px;
  text-align: center;
}
.stat-card .value { font-size: 28px; font-weight: 700; color: var(--accent); }
.stat-card .label { font-size: 12px; color: var(--text2); margin-top: 4px; }

/* Entity type badges */
.type-badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 11px;
  font-weight: 500;
  background: var(--bg3);
  color: var(--text2);
}
.type-badge.person { background: #1f3a2e; color: var(--accent2); }
.type-badge.machine, .type-badge.device { background: #1f2937; color: var(--accent); }
.type-badge.tool, .type-badge.toolset { background: #2d1f3a; color: var(--purple); }
.type-badge.project { background: #3a2e1f; color: var(--warn); }
.type-badge.credential, .type-badge.account { background: #3a1f1f; color: var(--danger); }

/* Entity list */
.entity-list { list-style: none; }
.entity-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
  cursor: pointer;
  transition: background 0.15s;
  min-height: 48px;
  -webkit-tap-highlight-color: transparent;
}
.entity-item:last-child { border-bottom: none; }
.entity-item:hover { background: var(--bg3); }
.entity-item .name { font-weight: 500; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.entity-item .obs-count { color: var(--text2); font-size: 12px; white-space: nowrap; }

/* Entity detail */
.entity-detail { max-width: 900px; }
.entity-detail .field-group { margin-bottom: 16px; }
.entity-detail label {
  display: block;
  font-size: 12px;
  color: var(--text2);
  margin-bottom: 4px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.entity-detail input[type="text"] {
  width: 100%;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 8px 12px;
  color: var(--text);
  font-size: 14px;
  outline: none;
}
.entity-detail input[type="text"]:focus { border-color: var(--accent); }

/* Observations */
.obs-list { list-style: none; }
.obs-item {
  display: flex;
  gap: 8px;
  padding: 8px 0;
  border-bottom: 1px solid var(--border);
  align-items: flex-start;
}
.obs-item:last-child { border-bottom: none; }
.obs-item .obs-index {
  color: var(--text2);
  font-size: 12px;
  min-width: 24px;
  text-align: right;
  padding-top: 2px;
}
.obs-item .obs-text {
  flex: 1;
  font-size: 13px;
  line-height: 1.5;
  word-break: break-word;
}
.obs-item .obs-actions {
  display: flex;
  gap: 4px;
  flex-shrink: 0;
  opacity: 0;
  transition: opacity 0.15s;
}
.obs-item:hover .obs-actions { opacity: 1; }
@media (hover: none) {
  .obs-item .obs-actions { opacity: 1; }
}

/* Relation table */
.table-scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
.rel-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
  min-width: 500px;
}
.rel-table th {
  text-align: left;
  padding: 10px 12px;
  border-bottom: 2px solid var(--border);
  color: var(--text2);
  font-size: 12px;
  text-transform: uppercase;
  white-space: nowrap;
}
.rel-table td {
  padding: 10px 12px;
  border-bottom: 1px solid var(--border);
}
.rel-table tr:hover { background: var(--bg3); }
.rel-table .rel-arrow { color: var(--text2); text-align: center; }

/* Editor */
.editor-area {
  width: 100%;
  min-height: 500px;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 16px;
  color: var(--text);
  font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace;
  font-size: 14px;
  line-height: 1.6;
  resize: vertical;
  outline: none;
  tab-size: 2;
  -webkit-text-size-adjust: 100%;
}
.editor-area:focus { border-color: var(--accent); }

/* File selector */
.file-selector {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 8px 10px;
  color: var(--text);
  font-size: 14px;
  outline: none;
  min-height: 36px;
  max-width: 100%;
}
.file-selector:focus { border-color: var(--accent); }

/* Graph container */
#graph-container {
  width: 100%;
  height: calc(100vh - 120px);
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
}

/* Toast */
.toast {
  position: fixed;
  bottom: 24px;
  right: 24px;
  padding: 12px 20px;
  border-radius: var(--radius);
  background: var(--accent2);
  color: #fff;
  font-size: 14px;
  z-index: 1000;
  animation: fadeIn 0.2s ease-out;
}
.toast.error { background: var(--danger); }
@keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } }

/* Modal */
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.6);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}
.modal {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 12px;
  width: 500px;
  max-width: 90vw;
  max-height: 80vh;
  overflow-y: auto;
}
.modal-header {
  padding: 16px 20px;
  border-bottom: 1px solid var(--border);
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.modal-header h3 { font-size: 16px; }
.modal-body { padding: 20px; }
.modal-footer {
  padding: 12px 20px;
  border-top: 1px solid var(--border);
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
.form-group { margin-bottom: 14px; }
.form-group label { display: block; font-size: 13px; color: var(--text2); margin-bottom: 4px; }
.form-group input, .form-group textarea {
  width: 100%;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 8px 12px;
  color: var(--text);
  font-size: 14px;
  outline: none;
  font-family: inherit;
}
.form-group textarea { min-height: 100px; resize: vertical; }
.form-group input:focus, .form-group textarea:focus { border-color: var(--accent); }

/* Sensitive content */
.sensitive { filter: blur(4px); transition: filter 0.2s; cursor: pointer; }
.sensitive:hover, .sensitive.revealed { filter: none; }

/* Scrollbar */
::-webkit-scrollbar { width: 8px; }
::-webkit-scrollbar-track { background: var(--bg); }
::-webkit-scrollbar-thumb { background: var(--bg3); border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: var(--border); }

/* Dream Log */
.dream-banner {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 20px 24px;
  margin-bottom: 24px;
  display: flex;
  align-items: center;
  gap: 24px;
  flex-wrap: wrap;
}
.dream-banner .issue-count {
  font-size: 42px;
  font-weight: 700;
  line-height: 1;
  min-width: 60px;
  text-align: center;
}
.dream-banner .issue-count.green { color: var(--accent2); }
.dream-banner .issue-count.amber { color: var(--warn); }
.dream-banner .issue-count.red { color: var(--danger); }
.dream-banner .banner-info { flex: 1; min-width: 200px; }
.dream-banner .banner-date { font-size: 13px; color: var(--text2); margin-bottom: 4px; }
.dream-banner .banner-summary { font-size: 15px; color: var(--text); }

.dream-timeline-item {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  margin-bottom: 8px;
  transition: background 0.15s;
}
.dream-timeline-header {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 16px;
  cursor: pointer;
  -webkit-tap-highlight-color: transparent;
}
.dream-timeline-header:hover { background: var(--bg3); border-radius: var(--radius); }
.dream-timeline-date { font-weight: 500; font-size: 14px; min-width: 100px; }
.dream-issue-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 28px;
  height: 24px;
  padding: 0 8px;
  border-radius: 12px;
  font-size: 12px;
  font-weight: 600;
}
.dream-issue-badge.green { background: #1f3a2e; color: var(--accent2); }
.dream-issue-badge.amber { background: #3a2e1f; color: var(--warn); }
.dream-issue-badge.red { background: #3a1f1f; color: var(--danger); }
.dream-timeline-summary { flex: 1; font-size: 13px; color: var(--text2); }
.dream-timeline-expand { color: var(--text2); font-size: 12px; transition: transform 0.2s; }
.dream-timeline-expand.open { transform: rotate(180deg); }

.dream-detail {
  display: none;
  padding: 0 16px 16px;
  border-top: 1px solid var(--border);
}
.dream-detail.open { display: block; }
.dream-category {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px 0;
  border-bottom: 1px solid var(--border);
}
.dream-category:last-child { border-bottom: none; }
.dream-cat-icon { font-size: 16px; min-width: 22px; text-align: center; padding-top: 1px; }
.dream-cat-name { font-size: 13px; font-weight: 500; min-width: 160px; }
.dream-cat-count { font-size: 12px; color: var(--text2); min-width: 50px; }
.dream-cat-issues { flex: 1; }
.dream-cat-issue {
  font-size: 12px;
  color: var(--text2);
  padding: 2px 0;
  line-height: 1.4;
  word-break: break-word;
}
.dream-raw-log {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 16px;
  font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text2);
  max-height: 500px;
  overflow-y: auto;
}

/* Responsive — tablet */
@media (max-width: 900px) {
  .main-content { padding: 16px; }
  .stats-row { grid-template-columns: repeat(2, 1fr); }
}

/* Responsive — mobile */
@media (max-width: 640px) {
  .hamburger { display: flex; }
  .sidebar-close { display: block; }
  .sidebar {
    position: fixed;
    top: 0;
    left: 0;
    bottom: 0;
    width: 280px;
    z-index: 50;
    transform: translateX(-100%);
  }
  .sidebar.open { transform: translateX(0); }
  .sidebar-overlay.open { display: block; }
  .main-content { padding: 12px; }
  .main-header { padding: 10px 12px; gap: 8px; }
  .main-header h2 { font-size: 15px; }
  .header-actions { width: 100%; justify-content: flex-end; }
  .stats-row { grid-template-columns: repeat(2, 1fr); gap: 8px; }
  .stat-card { padding: 12px 8px; }
  .stat-card .value { font-size: 22px; }
  .stat-card .label { font-size: 11px; }
  .search-bar { flex-direction: column; }
  .search-bar input { font-size: 16px; padding: 10px 12px; }
  .entity-item { padding: 14px 12px; gap: 8px; }
  .entity-item .name { font-size: 14px; }
  .entity-detail { max-width: 100%; }
  .entity-detail input[type="text"] { font-size: 16px; padding: 10px 12px; }
  .obs-item { flex-wrap: wrap; padding: 10px 0; }
  .obs-item .obs-actions { opacity: 1; width: 100%; padding-left: 32px; padding-top: 4px; }
  .editor-area { min-height: 300px; font-size: 14px; padding: 12px; }
  .file-selector { width: 100%; font-size: 14px; }
  .modal { width: 95vw; max-height: 90vh; border-radius: var(--radius); }
  .modal-body { padding: 16px; }
  .modal-header { padding: 14px 16px; }
  .form-group input, .form-group textarea, .form-group select { font-size: 16px; padding: 10px 12px; }
  #graph-container { height: calc(100vh - 110px); border-radius: 0; }
  .nav-item { padding: 14px 12px; font-size: 15px; }
  .btn { padding: 10px 14px; font-size: 14px; min-height: 40px; }
  .btn-sm { padding: 8px 12px; font-size: 13px; min-height: 34px; }
  .toast { bottom: 12px; right: 12px; left: 12px; text-align: center; }
  .type-badge { font-size: 10px; padding: 2px 6px; }
  .rel-table { font-size: 12px; }
  .rel-table th, .rel-table td { padding: 8px 6px; }
}
</style>
</head>
<body>
<div class="app">
  <div class="sidebar-overlay" id="sidebar-overlay" onclick="closeSidebar()"></div>
  <div class="sidebar" id="sidebar">
    <div class="sidebar-header">
      <div>
        <h1>Memory Explorer</h1>
        <div class="subtitle">Claude's Knowledge Base</div>
      </div>
      <button class="sidebar-close btn btn-sm" onclick="closeSidebar()" style="margin-left:auto;" aria-label="Close menu">X</button>
    </div>
    <div class="sidebar-nav">
      <div class="nav-section">
        <div class="nav-section-title">Overview</div>
        <div class="nav-item active" data-view="dashboard" onclick="switchView('dashboard')">
          Dashboard
        </div>
      </div>
      <div class="nav-section">
        <div class="nav-section-title">Memory Graph</div>
        <div class="nav-item" data-view="entities" onclick="switchView('entities')">
          Entities <span class="badge" id="entity-count">-</span>
        </div>
        <div class="nav-item" data-view="relations" onclick="switchView('relations')">
          Relations <span class="badge" id="relation-count">-</span>
        </div>
        <div class="nav-item" data-view="graph" onclick="switchView('graph')">
          Graph View
        </div>
      </div>
      <div class="nav-section">
        <div class="nav-section-title">Files</div>
        <div class="nav-item" data-view="claude-md" onclick="switchView('claude-md')">
          CLAUDE.md Files
        </div>
      </div>
      <div class="nav-section">
        <div class="nav-section-title">Monitoring</div>
        <div class="nav-item" data-view="dream-log" onclick="switchView('dream-log')">
          Dream Log
        </div>
      </div>
    </div>
  </div>

  <div class="main">
    <!-- Dashboard -->
    <div id="view-dashboard" class="view">
      <div class="main-header"><button class="hamburger" onclick="openSidebar()" aria-label="Menu">&#9776;</button><h2>Dashboard</h2></div>
      <div class="main-content">
        <div class="stats-row" id="stats-row"></div>
        <div class="search-bar">
          <input type="text" id="global-search" placeholder="Search across all entities and observations..." onkeyup="handleGlobalSearch(event)">
          <button class="btn btn-primary" onclick="doGlobalSearch()">Search</button>
        </div>
        <div id="search-results"></div>
        <div style="margin-top: 24px;">
          <h3 style="margin-bottom: 12px; font-size: 15px;">Entity Types</h3>
          <div id="type-chart"></div>
        </div>
      </div>
    </div>

    <!-- Entities -->
    <div id="view-entities" class="view" style="display:none">
      <div class="main-header">
        <button class="hamburger" onclick="openSidebar()" aria-label="Menu">&#9776;</button>
        <h2>Entities</h2>
        <div class="header-actions">
          <input type="text" id="entity-filter" placeholder="Filter..." oninput="filterEntities()" style="background:var(--bg2); border:1px solid var(--border); border-radius:var(--radius); padding:8px 10px; color:var(--text); font-size:14px; outline:none; width:160px; min-height:36px;">
          <button class="btn btn-primary" onclick="showCreateEntityModal()">+ New</button>
        </div>
      </div>
      <div class="main-content">
        <div id="entity-list-container"></div>
      </div>
    </div>

    <!-- Entity Detail -->
    <div id="view-entity-detail" class="view" style="display:none">
      <div class="main-header">
        <button class="hamburger" onclick="openSidebar()" aria-label="Menu">&#9776;</button>
        <h2 id="entity-detail-title">Entity</h2>
        <div class="header-actions">
          <button class="btn" onclick="switchView('entities')">Back</button>
          <button class="btn btn-danger" id="delete-entity-btn">Delete</button>
        </div>
      </div>
      <div class="main-content">
        <div class="entity-detail" id="entity-detail"></div>
      </div>
    </div>

    <!-- Relations -->
    <div id="view-relations" class="view" style="display:none">
      <div class="main-header">
        <button class="hamburger" onclick="openSidebar()" aria-label="Menu">&#9776;</button>
        <h2>Relations</h2>
        <div class="header-actions">
          <input type="text" id="relation-filter" placeholder="Filter..." oninput="filterRelations()" style="background:var(--bg2); border:1px solid var(--border); border-radius:var(--radius); padding:8px 10px; color:var(--text); font-size:14px; outline:none; width:160px; min-height:36px;">
          <button class="btn btn-primary" onclick="showCreateRelationModal()">+ New</button>
        </div>
      </div>
      <div class="main-content">
        <div id="relation-list-container"></div>
      </div>
    </div>

    <!-- Graph View -->
    <div id="view-graph" class="view" style="display:none">
      <div class="main-header">
        <button class="hamburger" onclick="openSidebar()" aria-label="Menu">&#9776;</button>
        <h2>Graph</h2>
        <div class="header-actions">
          <button class="btn" onclick="resetGraph()">Reset</button>
          <button class="btn" onclick="togglePhysics()">Physics</button>
        </div>
      </div>
      <div class="main-content" style="padding:0;">
        <div id="graph-container"></div>
      </div>
    </div>

    <!-- CLAUDE.md Editor -->
    <div id="view-claude-md" class="view" style="display:none">
      <div class="main-header">
        <button class="hamburger" onclick="openSidebar()" aria-label="Menu">&#9776;</button>
        <h2>CLAUDE.md</h2>
        <div class="header-actions">
          <select class="file-selector" id="claude-md-selector" onchange="loadClaudeMd()"></select>
          <button class="btn btn-primary" onclick="saveClaudeMd()">Save</button>
        </div>
      </div>
      <div class="main-content">
        <textarea class="editor-area" id="claude-md-editor" spellcheck="false"></textarea>
      </div>
    </div>

    <!-- Dream Log -->
    <div id="view-dream-log" class="view" style="display:none">
      <div class="main-header">
        <button class="hamburger" onclick="openSidebar()" aria-label="Menu">&#9776;</button>
        <h2>Dream Log</h2>
        <div class="header-actions">
          <button class="btn" onclick="loadDreamLogs()">Refresh</button>
        </div>
      </div>
      <div class="main-content">
        <div id="dream-latest-banner"></div>
        <div id="dream-timeline"></div>
      </div>
    </div>
  </div>
</div>

<!-- Modal container -->
<div id="modal-container"></div>

<script>
// Sidebar toggle
function openSidebar() {
  document.getElementById('sidebar').classList.add('open');
  document.getElementById('sidebar-overlay').classList.add('open');
}
function closeSidebar() {
  document.getElementById('sidebar').classList.remove('open');
  document.getElementById('sidebar-overlay').classList.remove('open');
}

// State
let graphData = { entities: [], relations: [] };
let visNetwork = null;
let physicsEnabled = true;

// Utility
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  return res.json();
}

function toast(msg, isError = false) {
  const el = document.createElement('div');
  el.className = 'toast' + (isError ? ' error' : '');
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3000);
}

function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

// Sensitive content detection
const SENSITIVE_PATTERNS = /password|token|secret|credential|auth.token|api.key|app.password|PAT|ghp_/i;
function isSensitive(text) {
  return SENSITIVE_PATTERNS.test(text);
}
function wrapSensitive(text) {
  if (isSensitive(text)) {
    return `<span class="sensitive" onclick="this.classList.toggle('revealed')" title="Click to reveal">${escHtml(text)}</span>`;
  }
  return escHtml(text);
}

// Navigation
function switchView(view) {
  closeSidebar();
  document.querySelectorAll('.view').forEach(v => v.style.display = 'none');
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  const el = document.getElementById('view-' + view);
  if (el) el.style.display = '';
  const nav = document.querySelector(`.nav-item[data-view="${view}"]`);
  if (nav) nav.classList.add('active');

  if (view === 'dashboard') loadDashboard();
  else if (view === 'entities') renderEntityList();
  else if (view === 'relations') renderRelationList();
  else if (view === 'graph') renderGraph();
  else if (view === 'claude-md') loadClaudeMdFiles();
  else if (view === 'dream-log') loadDreamLogs();
}

// Load graph data
async function loadGraph() {
  graphData = await api('/api/graph');
  document.getElementById('entity-count').textContent = graphData.entities.length;
  document.getElementById('relation-count').textContent = graphData.relations.length;
}

// Dashboard
async function loadDashboard() {
  const stats = await api('/api/graph/stats');
  const row = document.getElementById('stats-row');
  row.innerHTML = `
    <div class="stat-card"><div class="value">${stats.entity_count}</div><div class="label">Entities</div></div>
    <div class="stat-card"><div class="value">${stats.relation_count}</div><div class="label">Relations</div></div>
    <div class="stat-card"><div class="value">${stats.observation_count}</div><div class="label">Observations</div></div>
    <div class="stat-card"><div class="value">${(stats.file_size / 1024).toFixed(1)}K</div><div class="label">File Size</div></div>
  `;
  const chart = document.getElementById('type-chart');
  const types = Object.entries(stats.entity_types).sort((a,b) => b[1] - a[1]);
  const max = Math.max(...types.map(t => t[1]));
  chart.innerHTML = types.map(([type, count]) => `
    <div style="display:flex; align-items:center; gap:12px; margin-bottom:8px;">
      <span class="type-badge ${type.toLowerCase()}" style="min-width:100px; text-align:center;">${escHtml(type)}</span>
      <div style="flex:1; height:20px; background:var(--bg3); border-radius:4px; overflow:hidden;">
        <div style="height:100%; width:${(count/max*100)}%; background:var(--accent); border-radius:4px;"></div>
      </div>
      <span style="color:var(--text2); font-size:13px; min-width:24px; text-align:right;">${count}</span>
    </div>
  `).join('');
}

// Global search
function handleGlobalSearch(e) { if (e.key === 'Enter') doGlobalSearch(); }
async function doGlobalSearch() {
  const q = document.getElementById('global-search').value.trim();
  if (!q) { document.getElementById('search-results').innerHTML = ''; return; }
  const res = await api('/api/graph/search', { method: 'POST', body: { query: q } });
  const el = document.getElementById('search-results');
  if (!res.entities.length && !res.relations.length) {
    el.innerHTML = '<div style="color:var(--text2); padding:16px;">No results found.</div>';
    return;
  }
  let html = '';
  if (res.entities.length) {
    html += `<h3 style="margin-bottom:8px; font-size:15px;">Entities (${res.entities.length})</h3>`;
    html += '<div class="card"><ul class="entity-list">';
    res.entities.forEach(e => {
      const matchingObs = e.observations.filter(o => o.toLowerCase().includes(q.toLowerCase()));
      html += `<li class="entity-item" onclick="showEntityDetail('${escHtml(e.name)}')">
        <span class="type-badge ${e.entityType.toLowerCase()}">${escHtml(e.entityType)}</span>
        <span class="name">${escHtml(e.name)}</span>
        <span class="obs-count">${matchingObs.length} matching obs</span>
      </li>`;
    });
    html += '</ul></div>';
  }
  if (res.relations.length) {
    html += `<h3 style="margin:16px 0 8px; font-size:15px;">Relations (${res.relations.length})</h3>`;
    html += '<div class="card"><div class="table-scroll"><table class="rel-table"><thead><tr><th>From</th><th></th><th>Relation</th><th></th><th>To</th></tr></thead><tbody>';
    res.relations.forEach(r => {
      html += `<tr>
        <td><a href="#" onclick="showEntityDetail('${escHtml(r.from)}');return false">${escHtml(r.from)}</a></td>
        <td class="rel-arrow">→</td>
        <td>${escHtml(r.relationType)}</td>
        <td class="rel-arrow">→</td>
        <td><a href="#" onclick="showEntityDetail('${escHtml(r.to)}');return false">${escHtml(r.to)}</a></td>
      </tr>`;
    });
    html += '</tbody></table></div></div>';
  }
  el.innerHTML = html;
}

// Entity list
function renderEntityList() {
  const filter = (document.getElementById('entity-filter')?.value || '').toLowerCase();
  let entities = graphData.entities;
  if (filter) {
    entities = entities.filter(e =>
      e.name.toLowerCase().includes(filter) ||
      e.entityType.toLowerCase().includes(filter) ||
      e.observations.some(o => o.toLowerCase().includes(filter))
    );
  }
  const el = document.getElementById('entity-list-container');
  if (!entities.length) {
    el.innerHTML = '<div style="color:var(--text2); padding:16px;">No entities found.</div>';
    return;
  }
  el.innerHTML = '<div class="card"><ul class="entity-list">' +
    entities.map(e => `
      <li class="entity-item" onclick="showEntityDetail('${escHtml(e.name)}')">
        <span class="type-badge ${e.entityType.toLowerCase()}">${escHtml(e.entityType)}</span>
        <span class="name">${escHtml(e.name)}</span>
        <span class="obs-count">${e.observations.length} observations</span>
      </li>
    `).join('') +
    '</ul></div>';
}
function filterEntities() { renderEntityList(); }

// Entity detail
function showEntityDetail(name) {
  const entity = graphData.entities.find(e => e.name === name);
  if (!entity) { toast('Entity not found', true); return; }

  switchViewRaw('entity-detail');
  document.getElementById('entity-detail-title').textContent = entity.name;

  const related = graphData.relations.filter(r => r.from === name || r.to === name);

  const el = document.getElementById('entity-detail');
  el.innerHTML = `
    <div class="card">
      <div class="card-header"><h3>Properties</h3><button class="btn btn-sm" onclick="saveEntityProps('${escHtml(name)}')">Save Changes</button></div>
      <div class="card-body">
        <div class="field-group">
          <label>Name</label>
          <input type="text" id="edit-entity-name" value="${escHtml(entity.name)}">
        </div>
        <div class="field-group">
          <label>Type</label>
          <input type="text" id="edit-entity-type" value="${escHtml(entity.entityType)}">
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-header">
        <h3>Observations (${entity.observations.length})</h3>
        <button class="btn btn-sm btn-primary" onclick="showAddObservationModal('${escHtml(name)}')">+ Add</button>
      </div>
      <div class="card-body" style="padding:0;">
        <ul class="obs-list" style="padding:8px 16px;">
          ${entity.observations.map((obs, i) => `
            <li class="obs-item">
              <span class="obs-index">${i}</span>
              <span class="obs-text">${wrapSensitive(obs)}</span>
              <span class="obs-actions">
                <button class="btn btn-sm" onclick="editObservation('${escHtml(name)}', ${i})" title="Edit">Edit</button>
                <button class="btn btn-sm btn-danger" onclick="removeObservation('${escHtml(name)}', ${i})" title="Delete">Del</button>
              </span>
            </li>
          `).join('')}
        </ul>
      </div>
    </div>

    ${related.length ? `
    <div class="card">
      <div class="card-header"><h3>Relations (${related.length})</h3></div>
      <div class="card-body" style="padding:0;">
        <div class="table-scroll">
        <table class="rel-table">
          <thead><tr><th>From</th><th></th><th>Relation</th><th></th><th>To</th><th></th></tr></thead>
          <tbody>
            ${related.map(r => `
              <tr>
                <td><a href="#" onclick="showEntityDetail('${escHtml(r.from)}');return false" style="${r.from===name?'font-weight:600':''}">${escHtml(r.from)}</a></td>
                <td class="rel-arrow">→</td>
                <td>${escHtml(r.relationType)}</td>
                <td class="rel-arrow">→</td>
                <td><a href="#" onclick="showEntityDetail('${escHtml(r.to)}');return false" style="${r.to===name?'font-weight:600':''}">${escHtml(r.to)}</a></td>
                <td><button class="btn btn-sm btn-danger" onclick="deleteRelation('${escHtml(r.from)}','${escHtml(r.to)}','${escHtml(r.relationType)}')">Del</button></td>
              </tr>
            `).join('')}
          </tbody>
        </table>
        </div>
      </div>
    </div>` : ''}
  `;

  document.getElementById('delete-entity-btn').onclick = () => deleteEntity(name);
}

function switchViewRaw(view) {
  document.querySelectorAll('.view').forEach(v => v.style.display = 'none');
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  const el = document.getElementById('view-' + view);
  if (el) el.style.display = '';
}

async function saveEntityProps(oldName) {
  const newName = document.getElementById('edit-entity-name').value.trim();
  const newType = document.getElementById('edit-entity-type').value.trim();
  const res = await api('/api/graph/entity/update', {
    method: 'POST',
    body: { old_name: oldName, name: newName, entityType: newType }
  });
  if (res.ok) {
    toast('Entity updated');
    await loadGraph();
    showEntityDetail(newName);
  } else {
    toast(res.error || 'Failed', true);
  }
}

async function removeObservation(name, index) {
  if (!confirm(`Delete observation #${index}?`)) return;
  const res = await api('/api/graph/entity/remove-observation', {
    method: 'POST', body: { name, index }
  });
  if (res.ok) {
    toast('Observation removed');
    await loadGraph();
    showEntityDetail(name);
  } else {
    toast(res.error || 'Failed', true);
  }
}

function editObservation(name, index) {
  const entity = graphData.entities.find(e => e.name === name);
  if (!entity) return;
  const obs = entity.observations[index];
  showModal('Edit Observation', `
    <div class="form-group">
      <label>Observation text</label>
      <textarea id="modal-obs-text">${escHtml(obs)}</textarea>
    </div>
  `, async () => {
    const text = document.getElementById('modal-obs-text').value.trim();
    if (!text) return toast('Text required', true);
    const res = await api('/api/graph/entity/update-observation', {
      method: 'POST', body: { name, index, text }
    });
    if (res.ok) {
      toast('Observation updated');
      await loadGraph();
      showEntityDetail(name);
    } else {
      toast(res.error || 'Failed', true);
    }
  });
}

function showAddObservationModal(name) {
  showModal('Add Observation', `
    <div class="form-group">
      <label>Observation text</label>
      <textarea id="modal-obs-text" placeholder="Enter observation..."></textarea>
    </div>
  `, async () => {
    const text = document.getElementById('modal-obs-text').value.trim();
    if (!text) return toast('Text required', true);
    const res = await api('/api/graph/entity/add-observation', {
      method: 'POST', body: { name, observation: text }
    });
    if (res.ok) {
      toast('Observation added');
      await loadGraph();
      showEntityDetail(name);
    } else {
      toast(res.error || 'Failed', true);
    }
  });
}

async function deleteEntity(name) {
  if (!confirm(`Delete entity "${name}" and all its relations?`)) return;
  const res = await api('/api/graph/entity/delete', { method: 'POST', body: { name } });
  if (res.ok) {
    toast('Entity deleted');
    await loadGraph();
    switchView('entities');
  } else {
    toast(res.error || 'Failed', true);
  }
}

// Relation list
function renderRelationList() {
  const filter = (document.getElementById('relation-filter')?.value || '').toLowerCase();
  let relations = graphData.relations;
  if (filter) {
    relations = relations.filter(r =>
      r.from.toLowerCase().includes(filter) ||
      r.to.toLowerCase().includes(filter) ||
      r.relationType.toLowerCase().includes(filter)
    );
  }
  const el = document.getElementById('relation-list-container');
  if (!relations.length) {
    el.innerHTML = '<div style="color:var(--text2); padding:16px;">No relations found.</div>';
    return;
  }
  el.innerHTML = `<div class="card"><div class="table-scroll"><table class="rel-table">
    <thead><tr><th>From</th><th></th><th>Relation Type</th><th></th><th>To</th><th></th></tr></thead>
    <tbody>${relations.map(r => `
      <tr>
        <td><a href="#" onclick="showEntityDetail('${escHtml(r.from)}');return false">${escHtml(r.from)}</a></td>
        <td class="rel-arrow">→</td>
        <td>${escHtml(r.relationType)}</td>
        <td class="rel-arrow">→</td>
        <td><a href="#" onclick="showEntityDetail('${escHtml(r.to)}');return false">${escHtml(r.to)}</a></td>
        <td><button class="btn btn-sm btn-danger" onclick="deleteRelation('${escHtml(r.from)}','${escHtml(r.to)}','${escHtml(r.relationType)}')">Del</button></td>
      </tr>
    `).join('')}</tbody>
  </table></div></div>`;
}
function filterRelations() { renderRelationList(); }

async function deleteRelation(from, to, relationType) {
  if (!confirm(`Delete relation: ${from} → ${relationType} → ${to}?`)) return;
  const res = await api('/api/graph/relation/delete', {
    method: 'POST', body: { from, to, relationType }
  });
  if (res.ok) {
    toast('Relation deleted');
    await loadGraph();
    renderRelationList();
  } else {
    toast(res.error || 'Failed', true);
  }
}

// Create modals
function showCreateEntityModal() {
  showModal('Create Entity', `
    <div class="form-group">
      <label>Name</label>
      <input type="text" id="modal-entity-name" placeholder="Entity name...">
    </div>
    <div class="form-group">
      <label>Type</label>
      <input type="text" id="modal-entity-type" placeholder="e.g. person, device, project...">
    </div>
    <div class="form-group">
      <label>Initial observations (one per line)</label>
      <textarea id="modal-entity-obs" placeholder="First observation\nSecond observation..."></textarea>
    </div>
  `, async () => {
    const name = document.getElementById('modal-entity-name').value.trim();
    const entityType = document.getElementById('modal-entity-type').value.trim();
    const obsText = document.getElementById('modal-entity-obs').value.trim();
    const observations = obsText ? obsText.split('\n').map(s => s.trim()).filter(Boolean) : [];
    if (!name || !entityType) return toast('Name and type required', true);
    const res = await api('/api/graph/entity/create', {
      method: 'POST', body: { name, entityType, observations }
    });
    if (res.ok) {
      toast('Entity created');
      await loadGraph();
      renderEntityList();
    } else {
      toast(res.error || 'Failed', true);
    }
  });
}

function showCreateRelationModal() {
  const names = graphData.entities.map(e => e.name).sort();
  const opts = names.map(n => `<option value="${escHtml(n)}">${escHtml(n)}</option>`).join('');
  showModal('Create Relation', `
    <div class="form-group">
      <label>From</label>
      <select id="modal-rel-from" style="width:100%;background:var(--bg);border:1px solid var(--border);border-radius:var(--radius);padding:8px;color:var(--text);font-size:14px;">${opts}</select>
    </div>
    <div class="form-group">
      <label>Relation Type</label>
      <input type="text" id="modal-rel-type" placeholder="e.g. owns, uses, runs_on...">
    </div>
    <div class="form-group">
      <label>To</label>
      <select id="modal-rel-to" style="width:100%;background:var(--bg);border:1px solid var(--border);border-radius:var(--radius);padding:8px;color:var(--text);font-size:14px;">${opts}</select>
    </div>
  `, async () => {
    const from = document.getElementById('modal-rel-from').value;
    const to = document.getElementById('modal-rel-to').value;
    const relationType = document.getElementById('modal-rel-type').value.trim();
    if (!from || !to || !relationType) return toast('All fields required', true);
    const res = await api('/api/graph/relation/create', {
      method: 'POST', body: { from, to, relationType }
    });
    if (res.ok) {
      toast('Relation created');
      await loadGraph();
      renderRelationList();
    } else {
      toast(res.error || 'Failed', true);
    }
  });
}

// Modal
function showModal(title, bodyHtml, onSave) {
  const container = document.getElementById('modal-container');
  container.innerHTML = `
    <div class="modal-overlay" onclick="if(event.target===this)closeModal()">
      <div class="modal">
        <div class="modal-header"><h3>${title}</h3><button class="btn btn-sm" onclick="closeModal()">X</button></div>
        <div class="modal-body">${bodyHtml}</div>
        <div class="modal-footer">
          <button class="btn" onclick="closeModal()">Cancel</button>
          <button class="btn btn-primary" id="modal-save-btn">Save</button>
        </div>
      </div>
    </div>
  `;
  document.getElementById('modal-save-btn').onclick = async () => {
    await onSave();
    closeModal();
  };
}
function closeModal() { document.getElementById('modal-container').innerHTML = ''; }

// Graph visualization
const TYPE_COLORS = {
  person: '#3fb950', machine: '#58a6ff', device: '#58a6ff',
  tool: '#bc8cff', toolset: '#bc8cff', project: '#d29922',
  credential: '#f85149', account: '#f85149', network: '#79c0ff',
  configuration: '#8b949e', system: '#79c0ff', research: '#e3b341',
  strategy: '#f0883e', workspace: '#a5d6ff',
};

function renderGraph() {
  const container = document.getElementById('graph-container');
  const nodes = graphData.entities.map(e => ({
    id: e.name,
    label: e.name,
    title: `${e.entityType}: ${e.observations.length} observations`,
    color: {
      background: TYPE_COLORS[e.entityType.toLowerCase()] || '#8b949e',
      border: '#30363d',
      highlight: { background: '#fff', border: '#58a6ff' },
    },
    font: { color: '#e6edf3', size: 12 },
    shape: 'dot',
    size: Math.min(10 + e.observations.length * 1.5, 40),
  }));

  const edges = graphData.relations.map(r => ({
    from: r.from,
    to: r.to,
    label: r.relationType,
    font: { color: '#8b949e', size: 10, strokeWidth: 0 },
    color: { color: '#30363d', highlight: '#58a6ff' },
    arrows: 'to',
    smooth: { type: 'curvedCW', roundness: 0.2 },
  }));

  const data = { nodes: new vis.DataSet(nodes), edges: new vis.DataSet(edges) };
  const options = {
    physics: {
      enabled: true,
      solver: 'forceAtlas2Based',
      forceAtlas2Based: { gravitationalConstant: -80, springLength: 150, damping: 0.5 },
      stabilization: { iterations: 200 },
    },
    interaction: {
      hover: true,
      tooltipDelay: 100,
      zoomView: true,
      dragView: true,
    },
    layout: { improvedLayout: true },
  };

  visNetwork = new vis.Network(container, data, options);
  visNetwork.on('doubleClick', function(params) {
    if (params.nodes.length) showEntityDetail(params.nodes[0]);
  });
}

function resetGraph() { if (visNetwork) visNetwork.fit(); }
function togglePhysics() {
  physicsEnabled = !physicsEnabled;
  if (visNetwork) visNetwork.setOptions({ physics: { enabled: physicsEnabled } });
  toast('Physics ' + (physicsEnabled ? 'enabled' : 'disabled'));
}

// CLAUDE.md editor
async function loadClaudeMdFiles() {
  const files = await api('/api/claude-md/files');
  const sel = document.getElementById('claude-md-selector');
  sel.innerHTML = files.map(f => `<option value="${escHtml(f.path)}">${escHtml(f.label)}</option>`).join('');
  loadClaudeMd();
}

async function loadClaudeMd() {
  const path = document.getElementById('claude-md-selector').value;
  if (!path) return;
  const res = await api(`/api/claude-md/read?path=${encodeURIComponent(path)}`);
  if (res.content !== undefined) {
    document.getElementById('claude-md-editor').value = res.content;
  } else {
    toast(res.error || 'Failed to load file', true);
  }
}

async function saveClaudeMd() {
  const path = document.getElementById('claude-md-selector').value;
  const content = document.getElementById('claude-md-editor').value;
  if (!path) return toast('No file selected', true);
  if (!confirm(`Save changes to ${path}?`)) return;
  const res = await api('/api/claude-md/write', {
    method: 'POST', body: { path, content }
  });
  if (res.ok) {
    toast('Saved (backup: ' + res.backup.split('/').pop() + ')');
  } else {
    toast(res.error || 'Failed', true);
  }
}

// Dream Log
let dreamLogs = [];

async function loadDreamLogs() {
  dreamLogs = await api('/api/dream/logs');
  renderDreamBanner();
  renderDreamTimeline();
}

function issueColor(count) {
  if (count === 0) return 'green';
  if (count <= 3) return 'amber';
  return 'red';
}

function renderDreamBanner() {
  const el = document.getElementById('dream-latest-banner');
  if (!dreamLogs.length) {
    el.innerHTML = '<div style="color:var(--text2); padding:16px;">No dream logs found. Run <code>memory-dream --dry-run</code> to generate one.</div>';
    return;
  }
  const latest = dreamLogs[0];
  const color = issueColor(latest.total_issues);
  const dateStr = latest.timestamp
    ? new Date(latest.timestamp).toLocaleString()
    : latest.date;
  el.innerHTML = `
    <div class="dream-banner">
      <div class="issue-count ${color}">${latest.total_issues}</div>
      <div class="banner-info">
        <div class="banner-date">Latest run: ${escHtml(dateStr)}</div>
        <div class="banner-summary">${escHtml(latest.summary)}</div>
      </div>
    </div>
  `;
}

function renderDreamTimeline() {
  const el = document.getElementById('dream-timeline');
  if (!dreamLogs.length) { el.innerHTML = ''; return; }
  el.innerHTML = '<h3 style="margin-bottom:12px; font-size:15px;">Run History</h3>' +
    dreamLogs.map((log, i) => {
      const color = issueColor(log.total_issues);
      return `
        <div class="dream-timeline-item" id="dream-item-${i}">
          <div class="dream-timeline-header" onclick="toggleDreamDetail(${i}, '${escHtml(log.date)}')">
            <span class="dream-timeline-date">${escHtml(log.date)}</span>
            <span class="dream-issue-badge ${color}">${log.total_issues}</span>
            <span class="dream-timeline-summary">${escHtml(log.summary)}</span>
            <span class="dream-timeline-expand" id="dream-expand-${i}">&#9660;</span>
          </div>
          <div class="dream-detail" id="dream-detail-${i}"></div>
        </div>
      `;
    }).join('');
}

async function toggleDreamDetail(index, date) {
  const detail = document.getElementById('dream-detail-' + index);
  const expand = document.getElementById('dream-expand-' + index);
  if (detail.classList.contains('open')) {
    detail.classList.remove('open');
    expand.classList.remove('open');
    return;
  }
  // Close others
  document.querySelectorAll('.dream-detail.open').forEach(d => d.classList.remove('open'));
  document.querySelectorAll('.dream-timeline-expand.open').forEach(e => e.classList.remove('open'));

  // Fetch detail
  if (!detail.innerHTML) {
    detail.innerHTML = '<div style="padding:12px; color:var(--text2);">Loading...</div>';
    const data = await api('/api/dream/log?date=' + encodeURIComponent(date));
    if (data.error) {
      detail.innerHTML = `<div style="padding:12px; color:var(--danger);">${escHtml(data.error)}</div>`;
    } else if (data.raw_log) {
      detail.innerHTML = `<div class="dream-raw-log">${escHtml(data.raw_log)}</div>`;
    } else if (data.categories) {
      let html = '';
      for (const [catName, cat] of Object.entries(data.categories)) {
        const icon = cat.status === 'clean' ? '&#10003;' : '&#9888;';
        const iconStyle = cat.status === 'clean' ? 'color:var(--accent2)' : 'color:var(--warn)';
        const displayName = catName.replace(/^\d+\.\s*/, '');
        html += `<div class="dream-category">
          <span class="dream-cat-icon" style="${iconStyle}">${icon}</span>
          <span class="dream-cat-name">${escHtml(displayName)}</span>
          <span class="dream-cat-count">${cat.count} issue${cat.count !== 1 ? 's' : ''}</span>
          <div class="dream-cat-issues">
            ${cat.issues.map(issue => `<div class="dream-cat-issue">${escHtml(issue)}</div>`).join('')}
          </div>
        </div>`;
      }
      if (data.telegram_sent !== undefined) {
        html += `<div style="padding:8px 0; font-size:12px; color:var(--text2);">Telegram: ${data.telegram_sent ? 'sent' : 'not sent'}</div>`;
      }
      detail.innerHTML = html;
    }
  }
  detail.classList.add('open');
  expand.classList.add('open');
}

// Init
(async () => {
  await loadGraph();
  loadDashboard();
})();
</script>
</body>
</html>'''


if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    server = HTTPServer(('0.0.0.0', port), MemoryExplorerHandler)
    print(f"Memory Explorer running on http://0.0.0.0:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()
