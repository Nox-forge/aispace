#!/usr/bin/env python3
"""
NetSight — Live network dashboard powered by UniFi API.
Built by Nox (Claude Opus 4.5).

Usage:
  netsight              CLI summary
  netsight --web        Start web dashboard (default port 8089)
  netsight --json       Dump raw API data as JSON
"""

import asyncio
import json
import http.server
import ssl
import sys
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime
from http.cookies import SimpleCookie

# ─── Configuration ────────────────────────────────────────────────────────────

UNIFI_HOST = "192.168.53.1"
UNIFI_URL = f"https://{UNIFI_HOST}"
UNIFI_USER = "nox"
UNIFI_PASS = "ONgbEc5oVWhDq1vLOpXKn99"
WEB_PORT = 8090
CACHE_TTL = 15  # seconds

# ─── UniFi API Client ────────────────────────────────────────────────────────

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
            f"{UNIFI_URL}/api/auth/login",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, context=self._ctx)
        for header in resp.headers.get_all("Set-Cookie") or []:
            if "TOKEN=" in header:
                self._cookie = header.split(";")[0]
        csrf = resp.headers.get("X-Updated-Csrf-Token") or resp.headers.get("X-Csrf-Token")
        if csrf:
            self._csrf = csrf

    def _request(self, path: str, post_data: dict = None) -> dict:
        cache_key = path + (json.dumps(post_data, sort_keys=True) if post_data else "")
        now = time.time()
        if cache_key in self._cache and now - self._cache_time.get(cache_key, 0) < CACHE_TTL:
            return self._cache[cache_key]

        if not self._cookie:
            self._login()

        url = f"{UNIFI_URL}{path}"
        headers = {"Content-Type": "application/json"} if post_data else {}
        if self._cookie:
            headers["Cookie"] = self._cookie
        if self._csrf:
            headers["X-CSRF-Token"] = self._csrf

        body = json.dumps(post_data).encode() if post_data else None
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            resp = urllib.request.urlopen(req, context=self._ctx)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                self._cookie = None
                self._login()
                headers["Cookie"] = self._cookie
                if self._csrf:
                    headers["X-CSRF-Token"] = self._csrf
                req = urllib.request.Request(url, data=body, headers=headers)
                resp = urllib.request.urlopen(req, context=self._ctx)
            else:
                raise

        result = json.loads(resp.read().decode())
        self._cache[cache_key] = result
        self._cache_time[cache_key] = now
        return result

    def _get(self, path: str) -> dict:
        return self._request(path)

    def get_devices(self):
        return self._get("/proxy/network/api/s/default/stat/device").get("data", [])

    def get_clients(self):
        return self._get("/proxy/network/api/s/default/stat/sta").get("data", [])

    def get_networks(self):
        return self._get("/proxy/network/api/s/default/rest/networkconf").get("data", [])

    def get_health(self):
        return self._get("/proxy/network/api/s/default/stat/health").get("data", [])

    def get_hourly_traffic(self):
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - 86400 * 1000
        return self._request(
            "/proxy/network/api/s/default/stat/report/hourly.site",
            {"attrs": ["wan-tx_bytes", "wan-rx_bytes", "num_sta"], "start": start_ms, "end": now_ms},
        ).get("data", [])

    def get_port_forwards(self):
        return self._get("/proxy/network/api/s/default/rest/portforward").get("data", [])

    def get_anomalies(self):
        return self._get("/proxy/network/api/s/default/stat/anomalies").get("data", [])

    def get_known_clients(self):
        return self._get("/proxy/network/api/s/default/rest/user").get("data", [])

    def get_all(self):
        devices = self.get_devices()
        clients = self.get_clients()
        networks = self.get_networks()
        health = self.get_health()
        try:
            hourly = self.get_hourly_traffic()
        except Exception:
            hourly = []
        try:
            port_forwards = self.get_port_forwards()
        except Exception:
            port_forwards = []
        try:
            anomalies = self.get_anomalies()
        except Exception:
            anomalies = []
        try:
            known = self.get_known_clients()
        except Exception:
            known = []

        # Process devices
        dev_list = []
        for d in devices:
            uptime = d.get("uptime", 0)
            dev_list.append({
                "name": d.get("name", "Unknown"),
                "model": d.get("model", "?"),
                "type": d.get("type", "?"),
                "ip": d.get("ip", "?"),
                "mac": d.get("mac", "?"),
                "version": d.get("version", "?"),
                "uptime_days": round(uptime / 86400, 1),
                "state": d.get("state", 0),
                "num_sta": d.get("num_sta", 0),
                "tx_bytes": d.get("tx_bytes", 0),
                "rx_bytes": d.get("rx_bytes", 0),
                "ports": [
                    {
                        "idx": p.get("port_idx"),
                        "name": p.get("name", ""),
                        "speed": p.get("speed", 0),
                        "up": p.get("up", False),
                        "tx_bytes": p.get("tx_bytes", 0),
                        "rx_bytes": p.get("rx_bytes", 0),
                        "mac": (p.get("last_connection") or {}).get("mac", ""),
                    }
                    for p in d.get("port_table", [])
                ],
            })

        # Process clients
        client_list = []
        for c in clients:
            client_list.append({
                "name": c.get("name") or c.get("hostname") or c.get("mac", "?"),
                "hostname": c.get("hostname", ""),
                "ip": c.get("ip", "?"),
                "mac": c.get("mac", "?"),
                "network": c.get("network", "?"),
                "is_wired": c.get("is_wired", False),
                "tx_bytes": c.get("tx_bytes", 0),
                "rx_bytes": c.get("rx_bytes", 0),
                "uptime": c.get("uptime", 0),
                "signal": c.get("signal", None),
                "channel": c.get("channel", None),
                "radio": c.get("radio", None),
                "satisfaction": c.get("satisfaction", None),
                "sw_port": c.get("sw_port", None),
                "tx_rate": c.get("tx_rate", 0),
                "rx_rate": c.get("rx_rate", 0),
            })

        # Process networks
        net_list = []
        for n in networks:
            net_list.append({
                "name": n.get("name", "?"),
                "purpose": n.get("purpose", "?"),
                "subnet": n.get("ip_subnet", ""),
                "vlan": n.get("vlan_tag"),
                "enabled": n.get("enabled", True),
                "dhcp": n.get("dhcpd_enabled", False),
            })

        # Process health
        health_map = {}
        for h in health:
            sub = h.get("subsystem", "?")
            health_map[sub] = {
                "status": h.get("status", "?"),
                "num_sta": h.get("num_sta"),
                "num_ap": h.get("num_ap"),
                "num_sw": h.get("num_sw"),
                "wan_ip": h.get("wan_ip"),
                "isp": h.get("isp_name"),
                "latency": h.get("latency"),
                "xput_down": h.get("xput_down"),
                "xput_up": h.get("xput_up"),
            }

        # Process hourly traffic (entries are in chronological order, 24h)
        now_ts = time.time()
        traffic_list = []
        for i, e in enumerate(hourly):
            hour_ts = now_ts - (len(hourly) - 1 - i) * 3600
            traffic_list.append({
                "hour": datetime.fromtimestamp(hour_ts).strftime("%H:%M"),
                "rx_gb": round(e.get("wan-rx_bytes", 0) / (1024**3), 2),
                "tx_gb": round(e.get("wan-tx_bytes", 0) / (1024**3), 2),
                "clients": e.get("num_sta", 0),
            })

        # Process port forwards
        pf_list = []
        for p in port_forwards:
            pf_list.append({
                "name": p.get("name", "?"),
                "dst_port": p.get("dst_port", "?"),
                "fwd": p.get("fwd", "?"),
                "fwd_port": p.get("fwd_port", "?"),
                "proto": p.get("proto", "tcp_udp"),
                "enabled": p.get("enabled", True),
            })

        # Process anomalies
        anom_list = []
        for a in anomalies:
            ts_list = a.get("timestamps", [])
            last_ts = max(ts_list) / 1000 if ts_list else 0
            anom_list.append({
                "type": a.get("anomaly", "?"),
                "mac": a.get("mac", "?"),
                "last_seen": datetime.fromtimestamp(last_ts).isoformat() if last_ts else None,
            })

        total_rx_24h = sum(e.get("wan-rx_bytes", 0) for e in hourly)
        total_tx_24h = sum(e.get("wan-tx_bytes", 0) for e in hourly)

        return {
            "timestamp": datetime.now().isoformat(),
            "devices": dev_list,
            "clients": sorted(client_list, key=lambda x: x["ip"]),
            "networks": sorted(net_list, key=lambda x: x.get("vlan") or 0),
            "health": health_map,
            "traffic_hourly": traffic_list,
            "port_forwards": pf_list,
            "anomalies": anom_list,
            "summary": {
                "total_clients": len(client_list),
                "wired_clients": sum(1 for c in client_list if c["is_wired"]),
                "wifi_clients": sum(1 for c in client_list if not c["is_wired"]),
                "total_devices": len(dev_list),
                "networks": len([n for n in net_list if n["enabled"]]),
                "known_clients": len(known),
                "rx_24h_gb": round(total_rx_24h / (1024**3), 1),
                "tx_24h_gb": round(total_tx_24h / (1024**3), 1),
            },
        }


# ─── CLI Output ──────────────────────────────────────────────────────────────

class C:
    RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
    RED = "\033[31m"; GREEN = "\033[32m"; YELLOW = "\033[33m"
    BLUE = "\033[34m"; CYAN = "\033[36m"

    @staticmethod
    def ok(t): return f"{C.GREEN}{t}{C.RESET}"
    @staticmethod
    def err(t): return f"{C.RED}{t}{C.RESET}"
    @staticmethod
    def dim(t): return f"{C.DIM}{t}{C.RESET}"
    @staticmethod
    def bold(t): return f"{C.BOLD}{t}{C.RESET}"
    @staticmethod
    def info(t): return f"{C.CYAN}{t}{C.RESET}"
    @staticmethod
    def header(t): return f"{C.BOLD}{C.BLUE}{t}{C.RESET}"


def print_cli(data):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print()
    print(f"  {C.bold(C.info('NetSight'))} {C.dim('— Live Network Dashboard')}")
    print(f"  {C.dim(now)}")
    print()

    # Health
    print(f"  {C.header('── Health ─────────────────────────────────────────────')}")
    for sub, info in data["health"].items():
        status = info["status"]
        icon = C.ok("OK") if status == "ok" else C.err(status.upper())
        extras = []
        if info.get("wan_ip"):
            extras.append(f"IP:{info['wan_ip']}")
        if info.get("isp"):
            extras.append(info["isp"])
        if info.get("num_ap"):
            extras.append(f"{info['num_ap']} APs")
        if info.get("num_sw"):
            extras.append(f"{info['num_sw']} switches")
        ext = f"  {C.dim(' · '.join(extras))}" if extras else ""
        print(f"    {sub:<8s} [{icon}]{ext}")
    print()

    # Devices
    print(f"  {C.header('── Devices ────────────────────────────────────────────')}")
    for d in data["devices"]:
        state = C.ok("●") if d["state"] == 1 else C.err("●")
        print(f"    {state} {C.bold(d['name']):<28s} {d['model']:<12s} {d['ip']:<18s} {d['num_sta']} clients  up {d['uptime_days']:.0f}d  fw {d['version']}")
    print()

    # Bandwidth (24h)
    traffic = data.get("traffic_hourly", [])
    s = data["summary"]
    if traffic:
        print(f"  {C.header('── Bandwidth (24h) ────────────────────────────────────')}")
        rx_24h = s.get('rx_24h_gb', 0)
        tx_24h = s.get('tx_24h_gb', 0)
        print(f"    Total: {C.info(f'{rx_24h:.0f} GB')} rx  {C.info(f'{tx_24h:.0f} GB')} tx")
        # Sparkline
        bars = " ▁▂▃▄▅▆▇█"
        rx_vals = [t["rx_gb"] for t in traffic]
        max_rx = max(rx_vals) if rx_vals else 1
        spark = ""
        for v in rx_vals:
            idx = min(int(v / max_rx * 8), 8) if max_rx > 0 else 0
            spark += bars[idx]
        print(f"    rx: {C.info(spark)}")
        print(f"        {C.dim(traffic[0]['hour'])}{'':>{len(traffic)-8}}{C.dim(traffic[-1]['hour'])}")
        print()

    # Clients
    print(f"  {C.header('── Clients ────────────────────────────────────────────')}")
    known = s.get('known_clients', '?')
    print(f"    {C.ok(s['total_clients'])} connected ({s['wired_clients']} wired, {s['wifi_clients']} WiFi)  {C.dim(f'({known} known)')}")
    print()
    for c in data["clients"]:
        conn = "W" if c["is_wired"] else "~"
        name = c["name"][:28]
        rx = c["rx_bytes"] / (1024**3)
        tx = c["tx_bytes"] / (1024**3)
        sig = f" {c['signal']}dBm" if c.get("signal") else ""
        print(f"    {conn} {name:<28s} {c['ip']:<18s} {c['network']:<20s} rx={rx:.1f}G tx={tx:.1f}G{sig}")
    print()

    # Networks
    print(f"  {C.header('── Networks ───────────────────────────────────────────')}")
    for n in data["networks"]:
        if not n["enabled"]:
            continue
        vlan = f"VLAN {n['vlan']}" if n.get("vlan") else "untagged"
        print(f"    {n['name']:<28s} {str(n['subnet'] or 'n/a'):<22s} {vlan:<12s} {n['purpose']}")
    print()

    # Port forwards
    pf = data.get("port_forwards", [])
    if pf:
        print(f"  {C.header('── Port Forwards ──────────────────────────────────────')}")
        for p in pf:
            status = C.ok("ON") if p["enabled"] else C.dim("off")
            print(f"    [{status}] {p['name']:<20s} :{p['dst_port']} -> {p['fwd']}:{p['fwd_port']}")
        print()


# ─── Web Dashboard ───────────────────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NetSight — Nox</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: #0a0e14;
    color: #c5c8c6;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 13px;
  }

  #app { display: flex; height: 100vh; }

  /* Left: topology */
  #topo-panel { flex: 1; position: relative; border-right: 1px solid #1a1e26; }
  #topo-panel svg { width: 100%; height: 100%; }
  .link { stroke-opacity: 0.3; }
  .link-label { font-size: 8px; fill: #444; text-anchor: middle; pointer-events: none; }
  .node-group { cursor: pointer; }
  .node-circle { stroke-width: 2; transition: filter 0.15s; }
  .node-group:hover .node-circle { filter: brightness(1.4) drop-shadow(0 0 6px currentColor); }
  .node-label { font-size: 10px; fill: #c5c8c6; text-anchor: middle; pointer-events: none;
    text-shadow: 0 0 3px #0a0e14, 0 0 6px #0a0e14; }
  .node-sublabel { font-size: 8px; fill: #555; text-anchor: middle; pointer-events: none; }
  .vlan-hull { fill-opacity: 0.03; stroke-opacity: 0.12; stroke-width: 1.5; stroke-dasharray: 5 3; }
  .vlan-label { font-size: 10px; fill-opacity: 0.25; text-anchor: middle; pointer-events: none; }

  #topo-header { position: absolute; top: 14px; left: 16px; z-index: 10; pointer-events: none; }
  #topo-header h1 { font-size: 15px; color: #7aa2f7; letter-spacing: 2px; }
  #topo-header .sub { font-size: 10px; color: #444; margin-top: 2px; }

  #tooltip {
    position: absolute; background: #151920; border: 1px solid #2a2f38;
    border-radius: 8px; padding: 12px 16px; font-size: 12px; line-height: 1.6;
    max-width: 360px; pointer-events: none; opacity: 0; transition: opacity 0.12s;
    z-index: 100; box-shadow: 0 8px 32px rgba(0,0,0,0.6);
  }
  #tooltip.visible { opacity: 1; }
  #tooltip h3 { color: #e8e8e8; margin-bottom: 2px; }
  #tooltip .ip { color: #7aa2f7; }
  #tooltip .detail { color: #777; font-size: 11px; margin-top: 4px; }

  /* Right panel */
  #data-panel { width: 480px; overflow-y: auto; padding: 16px; background: #0c1018; }
  #data-panel::-webkit-scrollbar { width: 6px; }
  #data-panel::-webkit-scrollbar-thumb { background: #1a1e26; border-radius: 3px; }

  .section { margin-bottom: 18px; }
  .section h2 {
    font-size: 11px; color: #7aa2f7; letter-spacing: 1.5px;
    text-transform: uppercase; margin-bottom: 8px;
    padding-bottom: 4px; border-bottom: 1px solid #1a1e26;
  }

  .health-row { display: flex; gap: 8px; flex-wrap: wrap; }
  .health-card {
    background: #111620; border: 1px solid #1a1e26; border-radius: 6px;
    padding: 8px 12px; flex: 1; min-width: 100px;
  }
  .health-card .label { font-size: 10px; color: #555; text-transform: uppercase; }
  .health-card .value { font-size: 16px; font-weight: 600; margin-top: 2px; }
  .health-card .sub { font-size: 10px; color: #444; margin-top: 2px; }
  .status-ok { color: #9ece6a; }
  .status-bad { color: #f7768e; }

  /* Bandwidth chart */
  #bw-chart { background: #111620; border: 1px solid #1a1e26; border-radius: 6px; padding: 10px 14px; }
  #bw-chart .bw-header { display: flex; justify-content: space-between; margin-bottom: 6px; }
  #bw-chart .bw-total { font-size: 11px; color: #555; }
  #bw-chart .bw-total span { color: #7aa2f7; }
  #bw-chart svg { width: 100%; }

  .device-card {
    background: #111620; border: 1px solid #1a1e26; border-radius: 6px;
    padding: 8px 12px; margin-bottom: 5px;
  }
  .device-card .name { font-weight: 600; color: #e8e8e8; font-size: 12px; }
  .device-card .meta { font-size: 10px; color: #555; margin-top: 2px; }

  .client-table { width: 100%; border-collapse: collapse; font-size: 11px; }
  .client-table th {
    text-align: left; color: #555; font-weight: 500; padding: 3px 5px;
    border-bottom: 1px solid #1a1e26; font-size: 10px; text-transform: uppercase;
  }
  .client-table td { padding: 3px 5px; border-bottom: 1px solid #0f1318; }
  .client-table tr:hover { background: #111620; }
  .client-table .name-col { color: #c5c8c6; max-width: 130px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .client-table .ip-col { color: #7aa2f7; font-size: 10px; }
  .client-table .net-col { color: #555; font-size: 10px; }
  .client-table .traffic-col { color: #444; font-size: 10px; text-align: right; }
  .wired-badge { color: #9ece6a; font-size: 9px; }
  .wifi-badge { color: #e0af68; font-size: 9px; }

  .net-item { display: flex; justify-content: space-between; padding: 5px 0; border-bottom: 1px solid #0f1318; }
  .net-item .net-name { color: #c5c8c6; font-size: 12px; }
  .net-item .net-detail { font-size: 10px; color: #555; }
  .net-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 6px; }

  .pf-item { display: flex; justify-content: space-between; padding: 4px 0; border-bottom: 1px solid #0f1318; font-size: 11px; }
  .pf-name { color: #c5c8c6; }
  .pf-detail { color: #555; font-size: 10px; }
  .pf-on { color: #9ece6a; }
  .pf-off { color: #444; }

  #refresh-indicator { position: fixed; bottom: 10px; right: 10px; font-size: 10px; color: #333; z-index: 50; }
</style>
</head>
<body>
<div id="app">
  <div id="topo-panel">
    <div id="topo-header"><h1>NETSIGHT</h1><div class="sub" id="topo-sub">Loading...</div></div>
    <div id="tooltip"></div>
  </div>
  <div id="data-panel">
    <div class="section"><h2>Health</h2><div class="health-row" id="health-cards"></div></div>
    <div class="section"><h2>Bandwidth (24h)</h2><div id="bw-chart"></div></div>
    <div class="section"><h2>Infrastructure</h2><div id="device-cards"></div></div>
    <div class="section"><h2>Clients</h2>
      <table class="client-table"><thead><tr><th></th><th>Name</th><th>IP</th><th>Network</th><th style="text-align:right">Traffic</th></tr></thead>
      <tbody id="client-tbody"></tbody></table>
    </div>
    <div class="section"><h2>Networks</h2><div id="net-list"></div></div>
    <div class="section" id="pf-section" style="display:none"><h2>Port Forwards</h2><div id="pf-list"></div></div>
  </div>
</div>
<div id="refresh-indicator">--</div>

<script src="https://d3js.org/d3.v7.min.js"></script>
<script>
const NET_COLORS = {
  'Internet (No VPN)': '#7aa2f7', 'Internet IOT': '#e0af68',
  'Internet VPN Canada': '#9ece6a', 'Internet VPN USA': '#bb9af7',
  'Internet VPN UK': '#f7768e', 'Internet TOR': '#ff9e64',
  'Internet VPN SCore Singapour': '#73daca',
};
const INFRA_COLOR = '#f7768e';
function netColor(n) { return NET_COLORS[n] || '#555'; }
function fmtBytes(b) {
  if (b > 1e12) return (b/1e12).toFixed(1)+' TB';
  if (b > 1e9) return (b/1e9).toFixed(1)+' GB';
  if (b > 1e6) return (b/1e6).toFixed(1)+' MB';
  return (b/1e3).toFixed(0)+' KB';
}

// ── Topology ──────────────────────────────────────────────
const topoPanel = document.getElementById('topo-panel');
const W = topoPanel.clientWidth, H = topoPanel.clientHeight;
const svg = d3.select('#topo-panel').append('svg').attr('viewBox', [0,0,W,H]);
const defs = svg.append('defs');
const flt = defs.append('filter').attr('id','glow');
flt.append('feGaussianBlur').attr('stdDeviation','3').attr('result','blur');
const fm = flt.append('feMerge'); fm.append('feMergeNode').attr('in','blur'); fm.append('feMergeNode').attr('in','SourceGraphic');
const g = svg.append('g');
svg.call(d3.zoom().scaleExtent([0.2,5]).on('zoom', e => g.attr('transform', e.transform)))
   .call(d3.zoom().transform, d3.zoomIdentity.translate(W*0.05,H*0.05).scale(0.9));
const hullG=g.append('g'), linkG=g.append('g'), nodeG=g.append('g');
const tooltip = d3.select('#tooltip');
let sim = null;

function buildTopology(data) {
  const nodes=[], links=[], ids=new Set();
  data.devices.forEach(d => {
    const id=d.mac||d.ip;
    let r = d.type==='udm'?18:d.type==='usw'?15:13;
    nodes.push({id,label:d.name,ip:d.ip,r,vlan:'_infra',
      detail:`${d.model} · fw ${d.version}\n${d.num_sta} clients · up ${d.uptime_days.toFixed(0)}d`,
      isInfra:true,deviceType:d.type});
    ids.add(id);
  });
  const gw=nodes.find(n=>n.deviceType==='udm'), sw=nodes.find(n=>n.deviceType==='usw'), ap=nodes.find(n=>n.deviceType==='uap');
  if(gw&&sw) links.push({source:gw.id,target:sw.id,label:'10G',w:3.5});
  if(gw&&ap) links.push({source:gw.id,target:ap.id,w:2});

  data.clients.forEach(c => {
    const id=c.mac||c.ip;
    if(ids.has(id)) return;
    const tx=c.tx_bytes||0;
    let r = tx>10e9?12:tx>1e9?9:tx>100e6?7:5;
    const isMe = c.ip==='192.168.53.247';
    if(isMe) r=14;
    nodes.push({id,label:c.name,ip:c.ip,r,vlan:c.network,
      detail:`${c.is_wired?'Wired':'WiFi'}${c.signal?' · '+c.signal+'dBm':''}\nrx: ${fmtBytes(c.rx_bytes)} · tx: ${fmtBytes(c.tx_bytes)}`,
      isInfra:false,isMe,isWired:c.is_wired,network:c.network});
    ids.add(id);
    if(c.is_wired&&sw) links.push({source:sw.id,target:id,w:1});
    else if(!c.is_wired&&ap) links.push({source:ap.id,target:id,w:0.8});
    else if(gw) links.push({source:gw.id,target:id,w:0.6});
  });
  return {nodes,links};
}

function renderTopology(topo) {
  linkG.selectAll('*').remove(); nodeG.selectAll('*').remove(); hullG.selectAll('*').remove();
  if(sim) sim.stop();

  const lnk = linkG.selectAll('.link').data(topo.links).join('line').attr('class','link')
    .attr('stroke', d => { const s=topo.nodes.find(n=>n.id===(typeof d.source==='string'?d.source:d.source.id)); return s?.isInfra?INFRA_COLOR:netColor(s?.vlan); })
    .attr('stroke-width', d=>d.w||1);
  const ll = linkG.selectAll('.link-label').data(topo.links.filter(d=>d.label)).join('text').attr('class','link-label').text(d=>d.label);

  const nd = nodeG.selectAll('.node-group').data(topo.nodes).join('g').attr('class','node-group')
    .call(d3.drag().on('start',(e,d)=>{if(!e.active)sim.alphaTarget(0.3).restart();d.fx=d.x;d.fy=d.y;})
      .on('drag',(e,d)=>{d.fx=e.x;d.fy=e.y;}).on('end',(e,d)=>{if(!e.active)sim.alphaTarget(0);d.fx=null;d.fy=null;}));

  nd.append('circle').attr('class','node-circle').attr('r',d=>d.r)
    .attr('fill',d=>d.isInfra?INFRA_COLOR+'cc':d.isMe?netColor(d.vlan):netColor(d.vlan)+'99')
    .attr('stroke',d=>{const c=d3.color(d.isInfra?INFRA_COLOR:netColor(d.vlan));return c?c.brighter(0.6):'#777';})
    .attr('filter',d=>d.isMe?'url(#glow)':null);

  nd.filter(d=>d.isMe).insert('circle','.node-circle').attr('r',18).attr('fill','none')
    .attr('stroke','#7aa2f7').attr('stroke-opacity',0.4).attr('stroke-width',1.5).attr('class','pulse-ring');

  nd.append('text').attr('class','node-label').attr('dy',d=>-d.r-5).text(d=>d.label);
  nd.append('text').attr('class','node-sublabel').attr('dy',d=>d.r+12).text(d=>d.ip);

  nd.on('mouseover',(e,d)=>{tooltip.html(`<h3>${d.label}</h3><div class="ip">${d.ip}</div><div class="detail">${(d.detail||'').replace(/\n/g,'<br>')}</div>`).classed('visible',true);})
    .on('mousemove',e=>{tooltip.style('left',(e.pageX+16)+'px').style('top',(e.pageY-12)+'px');})
    .on('mouseout',()=>tooltip.classed('visible',false));

  sim = d3.forceSimulation(topo.nodes)
    .force('link',d3.forceLink(topo.links).id(d=>d.id).distance(d=>{
      const sr=typeof d.source==='object'?d.source.r:10,tr=typeof d.target==='object'?d.target.r:10;
      return 55+sr+tr+(d.w||1)*8;
    }).strength(0.4))
    .force('charge',d3.forceManyBody().strength(-280))
    .force('center',d3.forceCenter(W/2,H/2))
    .force('collision',d3.forceCollide().radius(d=>d.r+18))
    .force('x',d3.forceX(W/2).strength(0.02)).force('y',d3.forceY(H/2).strength(0.02))
    .on('tick',()=>{
      lnk.attr('x1',d=>d.source.x).attr('y1',d=>d.source.y).attr('x2',d=>d.target.x).attr('y2',d=>d.target.y);
      ll.attr('x',d=>(d.source.x+d.target.x)/2).attr('y',d=>(d.source.y+d.target.y)/2-5);
      nd.attr('transform',d=>`translate(${d.x},${d.y})`);
      hullG.selectAll('*').remove();
      const gr={};
      topo.nodes.forEach(n=>{const k=n.isInfra?'_infra':(n.vlan||'?');if(!gr[k])gr[k]=[];gr[k].push(n);});
      Object.entries(gr).forEach(([v,m])=>{
        if(m.length<3)return;
        const pts=m.map(n=>[n.x,n.y]),hull=d3.polygonHull(pts);if(!hull)return;
        const cx=d3.mean(hull,p=>p[0]),cy=d3.mean(hull,p=>p[1]);
        const exp=hull.map(p=>{const dx=p[0]-cx,dy=p[1]-cy,d=Math.sqrt(dx*dx+dy*dy);return[cx+dx*(d+50)/d,cy+dy*(d+50)/d];});
        const col=v==='_infra'?INFRA_COLOR:netColor(v);
        hullG.append('path').attr('class','vlan-hull').attr('d','M'+exp.join('L')+'Z').attr('fill',col).attr('stroke',col);
        hullG.append('text').attr('class','vlan-label').attr('x',cx).attr('y',d3.min(exp,p=>p[1])-6).attr('fill',col).text(v==='_infra'?'Infrastructure':v);
      });
    });

  (function pulse(){d3.selectAll('.pulse-ring').transition().duration(1800).ease(d3.easeQuadOut)
    .attr('r',26).attr('stroke-opacity',0).transition().duration(0).attr('r',18).attr('stroke-opacity',0.4).on('end',pulse);})();
}

// ── Bandwidth chart ───────────────────────────────────────
function renderBandwidth(traffic, summary) {
  const el = document.getElementById('bw-chart');
  if (!traffic || !traffic.length) { el.innerHTML = '<div style="color:#444">No traffic data</div>'; return; }

  const cw = 440, ch = 100, pad = {t:5, r:10, b:20, l:40};
  const iw = cw-pad.l-pad.r, ih = ch-pad.t-pad.b;

  let html = `<div class="bw-header"><div class="bw-total">24h: <span>${summary.rx_24h_gb} GB</span> rx / <span>${summary.tx_24h_gb} GB</span> tx</div></div>`;
  html += `<svg viewBox="0 0 ${cw} ${ch}">`;

  const maxRx = Math.max(...traffic.map(t=>t.rx_gb), 1);
  const barW = iw / traffic.length - 1;

  traffic.forEach((t, i) => {
    const x = pad.l + i * (iw / traffic.length);
    const rxH = (t.rx_gb / maxRx) * ih;
    const txH = (t.tx_gb / maxRx) * ih;
    html += `<rect x="${x}" y="${pad.t+ih-rxH}" width="${barW}" height="${rxH}" fill="#7aa2f7" opacity="0.6" rx="1"/>`;
    html += `<rect x="${x}" y="${pad.t+ih-txH}" width="${barW*0.4}" height="${txH}" fill="#9ece6a" opacity="0.8" rx="1"/>`;
    if (i % 6 === 0) {
      html += `<text x="${x+barW/2}" y="${ch-2}" fill="#444" font-size="8" text-anchor="middle">${t.hour}</text>`;
    }
  });

  // Y axis
  html += `<text x="${pad.l-4}" y="${pad.t+6}" fill="#444" font-size="8" text-anchor="end">${maxRx.toFixed(0)}G</text>`;
  html += `<text x="${pad.l-4}" y="${pad.t+ih}" fill="#444" font-size="8" text-anchor="end">0</text>`;
  html += `<line x1="${pad.l}" y1="${pad.t}" x2="${pad.l}" y2="${pad.t+ih}" stroke="#1a1e26"/>`;
  html += `<line x1="${pad.l}" y1="${pad.t+ih}" x2="${cw-pad.r}" y2="${pad.t+ih}" stroke="#1a1e26"/>`;

  // Legend
  html += `<rect x="${cw-80}" y="${pad.t}" width="8" height="8" fill="#7aa2f7" opacity="0.6" rx="1"/>`;
  html += `<text x="${cw-68}" y="${pad.t+7}" fill="#555" font-size="8">RX</text>`;
  html += `<rect x="${cw-48}" y="${pad.t}" width="8" height="8" fill="#9ece6a" opacity="0.8" rx="1"/>`;
  html += `<text x="${cw-36}" y="${pad.t+7}" fill="#555" font-size="8">TX</text>`;

  html += '</svg>';
  el.innerHTML = html;
}

// ── Data panel ────────────────────────────────────────────
function renderData(data) {
  const hc = document.getElementById('health-cards');
  hc.innerHTML = '';
  const wan = data.health.wan || {};
  const s = data.summary;
  [{label:'WAN',value:wan.status==='ok'?'Online':'Down',cls:wan.status==='ok'?'status-ok':'status-bad',sub:wan.isp||''},
   {label:'Clients',value:s.total_clients,cls:'status-ok',sub:`${s.wired_clients}W ${s.wifi_clients}Wi · ${s.known_clients||'?'} known`},
   {label:'Devices',value:s.total_devices,cls:'status-ok',sub:'UniFi managed'},
   {label:'Networks',value:s.networks,cls:'status-ok',sub:'enabled'}
  ].forEach(c => {
    const el=document.createElement('div');el.className='health-card';
    el.innerHTML=`<div class="label">${c.label}</div><div class="value ${c.cls}">${c.value}</div><div class="sub">${c.sub}</div>`;
    hc.appendChild(el);
  });

  renderBandwidth(data.traffic_hourly, data.summary);

  const dc=document.getElementById('device-cards');dc.innerHTML='';
  data.devices.forEach(d=>{const el=document.createElement('div');el.className='device-card';
    el.innerHTML=`<div class="name">${d.name}</div><div class="meta">${d.model} · ${d.ip} · ${d.num_sta} clients · up ${d.uptime_days.toFixed(0)}d · fw ${d.version}</div>`;
    dc.appendChild(el);});

  const tb=document.getElementById('client-tbody');tb.innerHTML='';
  data.clients.forEach(c=>{const tr=document.createElement('tr');
    tr.innerHTML=`<td>${c.is_wired?'<span class="wired-badge">W</span>':'<span class="wifi-badge">~</span>'}</td><td class="name-col">${c.name}</td><td class="ip-col">${c.ip}</td><td class="net-col">${c.network}</td><td class="traffic-col">${fmtBytes(c.tx_bytes+c.rx_bytes)}</td>`;
    tb.appendChild(tr);});

  const nl=document.getElementById('net-list');nl.innerHTML='';
  data.networks.filter(n=>n.enabled&&n.subnet).forEach(n=>{const el=document.createElement('div');el.className='net-item';
    el.innerHTML=`<div><span class="net-dot" style="background:${netColor(n.name)}"></span><span class="net-name">${n.name}</span></div><div class="net-detail">${n.subnet} · ${n.purpose}</div>`;
    nl.appendChild(el);});

  const pf=data.port_forwards||[];
  if(pf.length){document.getElementById('pf-section').style.display='';
    const pl=document.getElementById('pf-list');pl.innerHTML='';
    pf.forEach(p=>{const el=document.createElement('div');el.className='pf-item';
      el.innerHTML=`<div class="pf-name"><span class="${p.enabled?'pf-on':'pf-off'}">${p.enabled?'ON':'off'}</span> ${p.name}</div><div class="pf-detail">:${p.dst_port} → ${p.fwd}:${p.fwd_port}</div>`;
      pl.appendChild(el);});
  }
}

// ── Fetch loop ────────────────────────────────────────────
let firstLoad = true;
async function refresh() {
  try {
    const data = await (await fetch('/api/data')).json();
    document.getElementById('topo-sub').textContent =
      `${data.summary.total_clients} clients · ${data.summary.total_devices} devices · ${new Date(data.timestamp).toLocaleTimeString()}`;
    if (firstLoad) { renderTopology(buildTopology(data)); firstLoad = false; }
    renderData(data);
    document.getElementById('refresh-indicator').textContent = new Date().toLocaleTimeString();
  } catch(e) { document.getElementById('refresh-indicator').textContent = 'Error: '+e.message; }
}
refresh();
setInterval(refresh, 15000);
</script>
</body>
</html>"""


# ─── HTTP Server ──────────────────────────────────────────────────────────────

class NetSightHandler(http.server.BaseHTTPRequestHandler):
    client = None

    def log_message(self, format, *args):
        pass  # suppress default logging

    def do_GET(self):
        if self.path == "/api/data":
            try:
                data = self.client.get_all()
                body = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", len(body))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                body = json.dumps({"error": str(e)}).encode()
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
        elif self.path == "/" or self.path == "/index.html":
            body = DASHBOARD_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


def serve(port=WEB_PORT):
    client = UniFiClient()
    # Test connection
    print(f"Connecting to UniFi at {UNIFI_URL}...")
    try:
        data = client.get_all()
        print(f"Connected. {data['summary']['total_clients']} clients, {data['summary']['total_devices']} devices.")
    except Exception as e:
        print(f"Warning: initial connection failed: {e}")

    NetSightHandler.client = client

    class ReusableServer(http.server.HTTPServer):
        allow_reuse_address = True

    server = ReusableServer(("0.0.0.0", port), NetSightHandler)
    print(f"NetSight running on http://0.0.0.0:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nNetSight stopped.")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    if "--web" in sys.argv:
        port = WEB_PORT
        for i, a in enumerate(sys.argv):
            if a == "--port" and i + 1 < len(sys.argv):
                port = int(sys.argv[i + 1])
        serve(port)
    elif "--json" in sys.argv:
        client = UniFiClient()
        data = client.get_all()
        print(json.dumps(data, indent=2))
    else:
        client = UniFiClient()
        data = client.get_all()
        print_cli(data)


if __name__ == "__main__":
    main()
