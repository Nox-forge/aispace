#!/usr/bin/env python3
"""
WebDash — Lightweight web dashboard for network & system status.
Serves a single-page dashboard that auto-refreshes.
Built by Claude (Opus 4.5).
"""

import asyncio
import json
import math
import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
from typing import Optional

PORT = 8088


# ─── Data collection (same logic as netdash.py) ──────────────────────────────

DEVICES = {
    "NAS (Synology)": {"ip": "192.168.53.73", "ports": {5001: "DSM", 445: "SMB", 22: "SSH"}},
    "Home Assistant": {"ip": "192.168.53.246", "ports": {8123: "HA Web UI"}},
    "Plex Server": {"ip": "192.168.56.231", "ports": {32400: "Plex", 13378: "Audiobookshelf"}},
    "PlexDownloader (*arr)": {"ip": "192.168.56.244", "ports": {7878: "Radarr", 8989: "Sonarr", 8787: "Readarr", 9696: "Prowlarr", 8096: "Jellyfin", 8080: "qBittorrent", 5000: "Ombi"}},
    "Alex-PcLinux": {"ip": "192.168.53.108", "ports": {3000: "Crypto Tracker", 5001: "Dockge"}},
    "UniFi Gateway": {"ip": "192.168.53.1", "ports": {443: "UniFi UI"}},
    "Canon Printer": {"ip": "192.168.53.58", "ports": {80: "Web UI", 631: "IPP"}},
    "Samsung TV (98in)": {"ip": "192.168.55.44", "ports": {8008: "Google Cast"}},
    "Minecraft Server": {"ip": "192.168.53.245", "ports": {}},
    "Netgear NAS (Yvette2)": {"ip": "192.168.53.240", "ports": {445: "SMB", 80: "Web UI"}},
}


async def check_port(ip, port, timeout=2.0):
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=timeout)
        writer.close()
        await writer.wait_closed()
        return True
    except:
        return False


async def check_ping(ip, timeout=2.0):
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-W", str(int(timeout)), ip,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout + 1)
        if proc.returncode == 0:
            for line in stdout.decode().split("\n"):
                if "time=" in line:
                    return float(line.split("time=")[1].split()[0])
        return None
    except:
        return None


async def check_device(name, config):
    ip = config["ip"]
    ports = config.get("ports", {})

    ping_ms = await check_ping(ip)
    reachable = ping_ms is not None

    services = {}
    for port, svc_name in ports.items():
        is_up = await check_port(ip, port)
        services[str(port)] = {"name": svc_name, "up": is_up}
        if is_up:
            reachable = True

    return {"name": name, "ip": ip, "ping_ms": ping_ms, "reachable": reachable, "services": services}


def get_system_info():
    info = {}
    try:
        load1, load5, load15 = [float(x) for x in open("/proc/loadavg").read().split()[:3]]
        ncpu = int(subprocess.check_output(["nproc"]).decode().strip())
        info["cpu"] = {"load1": load1, "load5": load5, "load15": load15, "cores": ncpu, "pct": round(load1/ncpu*100, 1)}
    except:
        info["cpu"] = None

    try:
        meminfo = {}
        for line in open("/proc/meminfo"):
            parts = line.split(":")
            if len(parts) == 2:
                meminfo[parts[0].strip()] = int(parts[1].strip().split()[0])
        total = meminfo.get("MemTotal", 0)
        avail = meminfo.get("MemAvailable", 0)
        used = total - avail
        info["memory"] = {"total_gb": round(total/1024/1024, 1), "used_gb": round(used/1024/1024, 1), "pct": round(used/total*100, 1) if total else 0}
    except:
        info["memory"] = None

    try:
        result = subprocess.check_output(["df", "-BG", "--output=size,used,avail,pcent", "/"], text=True).strip().split("\n")[1].split()
        info["disk"] = {"total": result[0], "used": result[1], "avail": result[2], "pct": float(result[3].replace("%",""))}
    except:
        info["disk"] = None

    try:
        secs = float(open("/proc/uptime").read().split()[0])
        info["uptime"] = f"{int(secs//86400)}d {int((secs%86400)//3600)}h {int((secs%3600)//60)}m"
    except:
        info["uptime"] = "unknown"

    return info


async def collect_data():
    sys_info = get_system_info()
    tasks = [check_device(name, config) for name, config in DEVICES.items()]
    devices = await asyncio.gather(*tasks)
    return {
        "timestamp": datetime.now().isoformat(),
        "system": sys_info,
        "devices": list(devices),
    }


# ─── HTML Template ────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NetDash</title>
<style>
  :root {
    --bg: #0d1117; --surface: #161b22; --border: #30363d;
    --text: #c9d1d9; --text-dim: #8b949e; --accent: #58a6ff;
    --green: #3fb950; --red: #f85149; --yellow: #d29922; --blue: #58a6ff;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'SF Mono', 'Cascadia Code', 'Fira Code', monospace; font-size: 14px; padding: 24px; }
  h1 { color: var(--accent); font-size: 22px; margin-bottom: 4px; }
  .subtitle { color: var(--text-dim); font-size: 12px; margin-bottom: 24px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
  .card h2 { font-size: 14px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 12px; }
  .metric { display: flex; align-items: center; gap: 12px; margin-bottom: 8px; }
  .metric-label { min-width: 60px; color: var(--text-dim); }
  .bar-bg { flex: 1; height: 8px; background: var(--border); border-radius: 4px; overflow: hidden; }
  .bar-fill { height: 100%; border-radius: 4px; transition: width 0.5s ease; }
  .bar-fill.ok { background: var(--green); }
  .bar-fill.warn { background: var(--yellow); }
  .bar-fill.crit { background: var(--red); }
  .metric-val { min-width: 50px; text-align: right; font-weight: bold; }
  .device { display: flex; align-items: flex-start; gap: 10px; padding: 10px; border-radius: 6px; margin-bottom: 8px; background: rgba(255,255,255,0.02); }
  .device:hover { background: rgba(255,255,255,0.05); }
  .dot { width: 10px; height: 10px; border-radius: 50%; margin-top: 4px; flex-shrink: 0; }
  .dot.up { background: var(--green); box-shadow: 0 0 6px var(--green); }
  .dot.down { background: var(--red); box-shadow: 0 0 6px var(--red); }
  .dev-info { flex: 1; }
  .dev-name { font-weight: bold; }
  .dev-ip { color: var(--text-dim); font-size: 12px; }
  .dev-ping { color: var(--text-dim); font-size: 12px; }
  .services { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; }
  .svc { font-size: 11px; padding: 2px 8px; border-radius: 4px; }
  .svc.up { background: rgba(63,185,80,0.15); color: var(--green); }
  .svc.down { background: rgba(248,81,73,0.15); color: var(--red); }
  .summary { display: flex; gap: 32px; padding: 16px; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; }
  .summary-item { text-align: center; }
  .summary-num { font-size: 28px; font-weight: bold; }
  .summary-label { color: var(--text-dim); font-size: 12px; }
  .refresh-note { text-align: center; color: var(--text-dim); font-size: 11px; margin-top: 16px; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.5; } }
  .loading { animation: pulse 1s infinite; }
</style>
</head>
<body>
  <h1>NetDash</h1>
  <div class="subtitle" id="timestamp">Loading...</div>

  <div class="grid">
    <div class="card">
      <h2>System</h2>
      <div id="system-metrics"></div>
    </div>
    <div class="card">
      <h2>Summary</h2>
      <div class="summary" id="summary"></div>
    </div>
  </div>

  <div class="card" style="margin-bottom: 16px;">
    <h2>Network Devices</h2>
    <div id="devices"></div>
  </div>

  <div class="refresh-note">Auto-refreshes every 30 seconds | Built by Claude (Opus 4.5)</div>

<script>
function barClass(pct) {
  if (pct > 85) return 'crit';
  if (pct > 60) return 'warn';
  return 'ok';
}

function renderMetric(label, pct, detail) {
  return `<div class="metric">
    <span class="metric-label">${label}</span>
    <div class="bar-bg"><div class="bar-fill ${barClass(pct)}" style="width:${pct}%"></div></div>
    <span class="metric-val">${pct.toFixed(1)}%</span>
    <span style="color:var(--text-dim);font-size:12px;min-width:100px">${detail}</span>
  </div>`;
}

function render(data) {
  document.getElementById('timestamp').textContent =
    new Date(data.timestamp).toLocaleString() + ' | Uptime: ' + (data.system.uptime || '?');

  // System
  let sysHtml = '';
  if (data.system.cpu) sysHtml += renderMetric('CPU', data.system.cpu.pct, `${data.system.cpu.cores} cores`);
  if (data.system.memory) sysHtml += renderMetric('Memory', data.system.memory.pct, `${data.system.memory.used_gb}G / ${data.system.memory.total_gb}G`);
  if (data.system.disk) sysHtml += renderMetric('Disk', data.system.disk.pct, `${data.system.disk.used} / ${data.system.disk.total}`);
  document.getElementById('system-metrics').innerHTML = sysHtml;

  // Summary
  const devUp = data.devices.filter(d => d.reachable).length;
  const devDown = data.devices.length - devUp;
  let svcUp = 0, svcTotal = 0;
  data.devices.forEach(d => {
    Object.values(d.services).forEach(s => { svcTotal++; if (s.up) svcUp++; });
  });
  document.getElementById('summary').innerHTML = `
    <div class="summary-item"><div class="summary-num" style="color:var(--green)">${devUp}</div><div class="summary-label">Devices Up</div></div>
    <div class="summary-item"><div class="summary-num" style="color:${devDown?'var(--red)':'var(--text-dim)'}">${devDown}</div><div class="summary-label">Devices Down</div></div>
    <div class="summary-item"><div class="summary-num" style="color:var(--green)">${svcUp}</div><div class="summary-label">Services Up</div></div>
    <div class="summary-item"><div class="summary-num" style="color:${(svcTotal-svcUp)?'var(--red)':'var(--text-dim)'}">${svcTotal-svcUp}</div><div class="summary-label">Services Down</div></div>
  `;

  // Devices
  let devHtml = '';
  data.devices.forEach(d => {
    const dotClass = d.reachable ? 'up' : 'down';
    const ping = d.ping_ms ? `${d.ping_ms.toFixed(1)}ms` : 'n/a';
    let svcs = '';
    Object.entries(d.services).sort((a,b) => parseInt(a[0]) - parseInt(b[0])).forEach(([port, s]) => {
      svcs += `<span class="svc ${s.up?'up':'down'}">${s.name}:${port}</span>`;
    });
    devHtml += `<div class="device">
      <div class="dot ${dotClass}"></div>
      <div class="dev-info">
        <span class="dev-name">${d.name}</span>
        <span class="dev-ip">${d.ip}</span>
        <span class="dev-ping">${ping}</span>
        ${svcs ? '<div class="services">' + svcs + '</div>' : ''}
      </div>
    </div>`;
  });
  document.getElementById('devices').innerHTML = devHtml;
}

async function refresh() {
  try {
    const resp = await fetch('/api/status');
    const data = await resp.json();
    render(data);
  } catch (e) {
    document.getElementById('timestamp').textContent = 'Error fetching data: ' + e.message;
  }
}

refresh();
setInterval(refresh, 30000);
</script>
</body>
</html>"""


# ─── HTTP Server ──────────────────────────────────────────────────────────────

class DashHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(HTML.encode())
        elif self.path == "/api/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            data = asyncio.run(collect_data())
            self.wfile.write(json.dumps(data).encode())
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        pass  # suppress request logs


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    server = HTTPServer(("0.0.0.0", port), DashHandler)
    print(f"NetDash web UI running on http://0.0.0.0:{port}")
    print(f"  Local:   http://localhost:{port}")
    print(f"  Network: http://192.168.53.247:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
