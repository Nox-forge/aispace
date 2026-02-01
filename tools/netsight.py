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

    def _get(self, path: str) -> dict:
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
        return self._get("/proxy/network/api/s/default/stat/device").get("data", [])

    def get_clients(self):
        return self._get("/proxy/network/api/s/default/stat/sta").get("data", [])

    def get_networks(self):
        return self._get("/proxy/network/api/s/default/rest/networkconf").get("data", [])

    def get_health(self):
        return self._get("/proxy/network/api/s/default/stat/health").get("data", [])

    def get_all(self):
        devices = self.get_devices()
        clients = self.get_clients()
        networks = self.get_networks()
        health = self.get_health()

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

        return {
            "timestamp": datetime.now().isoformat(),
            "devices": dev_list,
            "clients": sorted(client_list, key=lambda x: x["ip"]),
            "networks": sorted(net_list, key=lambda x: x.get("vlan") or 0),
            "health": health_map,
            "summary": {
                "total_clients": len(client_list),
                "wired_clients": sum(1 for c in client_list if c["is_wired"]),
                "wifi_clients": sum(1 for c in client_list if not c["is_wired"]),
                "total_devices": len(dev_list),
                "networks": len([n for n in net_list if n["enabled"]]),
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

    # Clients
    print(f"  {C.header('── Clients ────────────────────────────────────────────')}")
    s = data["summary"]
    print(f"    {C.ok(s['total_clients'])} connected ({s['wired_clients']} wired, {s['wifi_clients']} WiFi)")
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
  a { color: #7aa2f7; text-decoration: none; }

  #app { display: flex; height: 100vh; }

  /* Left panel — topology */
  #topo-panel {
    flex: 1;
    position: relative;
    border-right: 1px solid #1a1e26;
  }
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

  #topo-header {
    position: absolute; top: 14px; left: 16px; z-index: 10; pointer-events: none;
  }
  #topo-header h1 { font-size: 15px; color: #7aa2f7; letter-spacing: 2px; }
  #topo-header .sub { font-size: 10px; color: #444; margin-top: 2px; }

  #tooltip {
    position: absolute; background: #151920; border: 1px solid #2a2f38;
    border-radius: 8px; padding: 12px 16px; font-size: 12px; line-height: 1.6;
    max-width: 360px; pointer-events: none; opacity: 0; transition: opacity 0.12s;
    z-index: 100; box-shadow: 0 8px 32px rgba(0,0,0,0.6);
  }
  #tooltip.visible { opacity: 1; }
  #tooltip h3 { color: #e8e8e8; margin-bottom: 2px; font-size: 13px; }
  #tooltip .ip { color: #7aa2f7; }
  #tooltip .detail { color: #777; font-size: 11px; margin-top: 4px; }

  /* Right panel — data */
  #data-panel {
    width: 480px;
    overflow-y: auto;
    padding: 16px;
    background: #0c1018;
  }
  #data-panel::-webkit-scrollbar { width: 6px; }
  #data-panel::-webkit-scrollbar-thumb { background: #1a1e26; border-radius: 3px; }

  .section { margin-bottom: 20px; }
  .section h2 {
    font-size: 12px; color: #7aa2f7; letter-spacing: 1.5px;
    text-transform: uppercase; margin-bottom: 10px;
    padding-bottom: 4px; border-bottom: 1px solid #1a1e26;
  }

  /* Health cards */
  .health-row { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
  .health-card {
    background: #111620; border: 1px solid #1a1e26; border-radius: 6px;
    padding: 8px 12px; flex: 1; min-width: 120px;
  }
  .health-card .label { font-size: 10px; color: #555; text-transform: uppercase; }
  .health-card .value { font-size: 16px; font-weight: 600; margin-top: 2px; }
  .health-card .sub { font-size: 10px; color: #444; margin-top: 2px; }
  .status-ok { color: #9ece6a; }
  .status-bad { color: #f7768e; }

  /* Device cards */
  .device-card {
    background: #111620; border: 1px solid #1a1e26; border-radius: 6px;
    padding: 10px 14px; margin-bottom: 6px;
  }
  .device-card .name { font-weight: 600; color: #e8e8e8; }
  .device-card .meta { font-size: 11px; color: #555; margin-top: 2px; }

  /* Client table */
  .client-table { width: 100%; border-collapse: collapse; font-size: 11px; }
  .client-table th {
    text-align: left; color: #555; font-weight: 500; padding: 4px 6px;
    border-bottom: 1px solid #1a1e26; font-size: 10px; text-transform: uppercase;
  }
  .client-table td { padding: 4px 6px; border-bottom: 1px solid #0f1318; }
  .client-table tr:hover { background: #111620; }
  .client-table .name-col { color: #c5c8c6; max-width: 140px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .client-table .ip-col { color: #7aa2f7; font-size: 10px; }
  .client-table .net-col { color: #555; font-size: 10px; }
  .client-table .traffic-col { color: #444; font-size: 10px; text-align: right; }
  .wired-badge { color: #9ece6a; font-size: 9px; }
  .wifi-badge { color: #e0af68; font-size: 9px; }

  /* Network list */
  .net-item {
    display: flex; justify-content: space-between; align-items: center;
    padding: 6px 0; border-bottom: 1px solid #0f1318;
  }
  .net-item .net-name { color: #c5c8c6; }
  .net-item .net-detail { font-size: 10px; color: #555; }
  .net-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 6px; }

  #refresh-indicator {
    position: fixed; bottom: 12px; right: 12px; font-size: 10px; color: #333; z-index: 50;
  }
</style>
</head>
<body>
<div id="app">
  <div id="topo-panel">
    <div id="topo-header">
      <h1>NETSIGHT</h1>
      <div class="sub" id="topo-sub">Loading...</div>
    </div>
    <div id="tooltip"></div>
  </div>
  <div id="data-panel">
    <div class="section" id="health-section">
      <h2>Health</h2>
      <div class="health-row" id="health-cards"></div>
    </div>
    <div class="section" id="devices-section">
      <h2>Infrastructure</h2>
      <div id="device-cards"></div>
    </div>
    <div class="section" id="clients-section">
      <h2>Clients</h2>
      <table class="client-table" id="client-table">
        <thead><tr><th></th><th>Name</th><th>IP</th><th>Network</th><th style="text-align:right">Traffic</th></tr></thead>
        <tbody id="client-tbody"></tbody>
      </table>
    </div>
    <div class="section" id="networks-section">
      <h2>Networks</h2>
      <div id="net-list"></div>
    </div>
  </div>
</div>
<div id="refresh-indicator">--</div>

<script src="https://d3js.org/d3.v7.min.js"></script>
<script>
// ── Color scheme ──────────────────────────────────────────
const NET_COLORS = {
  'Internet (No VPN)': '#7aa2f7',
  'Internet IOT': '#e0af68',
  'Internet VPN Canada': '#9ece6a',
  'Internet VPN USA': '#bb9af7',
  'Internet VPN UK': '#f7768e',
  'Internet TOR': '#ff9e64',
  'Internet VPN SCore Singapour': '#73daca',
};
const INFRA_COLOR = '#f7768e';
const DEFAULT_COLOR = '#555';

function netColor(name) { return NET_COLORS[name] || DEFAULT_COLOR; }

// ── Topology ──────────────────────────────────────────────
const topoPanel = document.getElementById('topo-panel');
const width = topoPanel.clientWidth;
const height = topoPanel.clientHeight;

const svg = d3.select('#topo-panel').append('svg').attr('viewBox', [0, 0, width, height]);
const defs = svg.append('defs');
const filter = defs.append('filter').attr('id', 'glow');
filter.append('feGaussianBlur').attr('stdDeviation', '3').attr('result', 'blur');
const fm = filter.append('feMerge');
fm.append('feMergeNode').attr('in', 'blur');
fm.append('feMergeNode').attr('in', 'SourceGraphic');

const g = svg.append('g');
const zoomBehavior = d3.zoom().scaleExtent([0.2, 5])
  .on('zoom', (e) => g.attr('transform', e.transform));
svg.call(zoomBehavior);
svg.call(zoomBehavior.transform, d3.zoomIdentity.translate(width*0.05, height*0.05).scale(0.9));

const hullG = g.append('g');
const linkG = g.append('g');
const nodeG = g.append('g');

const tooltip = d3.select('#tooltip');
let simulation = null;

function fmtBytes(b) {
  if (b > 1e12) return (b/1e12).toFixed(1) + ' TB';
  if (b > 1e9) return (b/1e9).toFixed(1) + ' GB';
  if (b > 1e6) return (b/1e6).toFixed(1) + ' MB';
  if (b > 1e3) return (b/1e3).toFixed(1) + ' KB';
  return b + ' B';
}

function buildTopology(data) {
  const nodes = [];
  const links = [];
  const nodeIds = new Set();

  // Add infrastructure devices
  data.devices.forEach(d => {
    const id = d.mac || d.ip;
    let r = 14;
    if (d.type === 'udm') r = 18;
    else if (d.type === 'uap') r = 13;
    else if (d.type === 'usw') r = 15;

    nodes.push({
      id, label: d.name, ip: d.ip, r, vlan: '_infra',
      detail: `${d.model} · fw ${d.version}\n${d.num_sta} clients · up ${d.uptime_days.toFixed(0)}d`,
      isInfra: true, deviceType: d.type, portData: d.ports,
    });
    nodeIds.add(id);
  });

  // Find gateway, switch, AP
  const gateway = nodes.find(n => n.deviceType === 'udm');
  const sw = nodes.find(n => n.deviceType === 'usw');
  const ap = nodes.find(n => n.deviceType === 'uap');

  if (gateway && sw) links.push({ source: gateway.id, target: sw.id, label: '10G', w: 3.5 });
  if (gateway && ap) links.push({ source: gateway.id, target: ap.id, label: '', w: 2 });

  // Add clients
  data.clients.forEach(c => {
    const id = c.mac || c.ip;
    if (nodeIds.has(id)) return;

    const tx = c.tx_bytes || 0;
    let r = 5;
    if (tx > 10e9) r = 12;
    else if (tx > 1e9) r = 9;
    else if (tx > 100e6) r = 7;

    const isMe = c.ip === '192.168.53.247';
    if (isMe) r = 14;

    nodes.push({
      id, label: c.name, ip: c.ip, r, vlan: c.network,
      detail: `${c.is_wired ? 'Wired' : 'WiFi'}${c.signal ? ' · ' + c.signal + 'dBm' : ''}\nrx: ${fmtBytes(c.rx_bytes)} · tx: ${fmtBytes(c.tx_bytes)}`,
      isInfra: false, isMe, isWired: c.is_wired, network: c.network,
    });
    nodeIds.add(id);

    // Link to infrastructure
    if (c.is_wired && sw) {
      links.push({ source: sw.id, target: id, w: 1 });
    } else if (!c.is_wired && ap) {
      links.push({ source: ap.id, target: id, w: 0.8 });
    } else if (gateway) {
      links.push({ source: gateway.id, target: id, w: 0.6 });
    }
  });

  return { nodes, links };
}

function renderTopology(topo) {
  linkG.selectAll('*').remove();
  nodeG.selectAll('*').remove();
  hullG.selectAll('*').remove();

  if (simulation) simulation.stop();

  const linkSel = linkG.selectAll('.link').data(topo.links).join('line')
    .attr('class', 'link')
    .attr('stroke', d => {
      const src = topo.nodes.find(n => n.id === (typeof d.source === 'string' ? d.source : d.source.id));
      if (src && src.isInfra) return INFRA_COLOR;
      if (src) return netColor(src.vlan);
      return '#333';
    })
    .attr('stroke-width', d => d.w || 1);

  const linkLabels = linkG.selectAll('.link-label')
    .data(topo.links.filter(d => d.label)).join('text')
    .attr('class', 'link-label').text(d => d.label);

  const nodeSel = nodeG.selectAll('.node-group').data(topo.nodes).join('g')
    .attr('class', 'node-group')
    .call(d3.drag()
      .on('start', (e, d) => { if (!e.active) simulation.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
      .on('drag', (e, d) => { d.fx = e.x; d.fy = e.y; })
      .on('end', (e, d) => { if (!e.active) simulation.alphaTarget(0); d.fx = null; d.fy = null; }));

  nodeSel.append('circle').attr('class', 'node-circle')
    .attr('r', d => d.r)
    .attr('fill', d => {
      if (d.isInfra) return INFRA_COLOR + 'cc';
      if (d.isMe) return netColor(d.vlan);
      return netColor(d.vlan) + '99';
    })
    .attr('stroke', d => {
      const c = d3.color(d.isInfra ? INFRA_COLOR : netColor(d.vlan));
      return c ? c.brighter(0.6) : '#777';
    })
    .attr('filter', d => d.isMe ? 'url(#glow)' : null);

  // Pulse for "me" node
  const me = nodeSel.filter(d => d.isMe);
  me.insert('circle', '.node-circle')
    .attr('r', 18).attr('fill', 'none')
    .attr('stroke', '#7aa2f7').attr('stroke-opacity', 0.4).attr('stroke-width', 1.5)
    .attr('class', 'pulse-ring');

  nodeSel.append('text').attr('class', 'node-label')
    .attr('dy', d => -d.r - 5).text(d => d.label);
  nodeSel.append('text').attr('class', 'node-sublabel')
    .attr('dy', d => d.r + 12).text(d => d.ip);

  nodeSel.on('mouseover', (e, d) => {
    tooltip.html(`<h3>${d.label}</h3><div class="ip">${d.ip}</div><div class="detail">${(d.detail||'').replace(/\n/g,'<br>')}</div>`)
      .classed('visible', true);
  }).on('mousemove', e => {
    tooltip.style('left', (e.pageX+16)+'px').style('top', (e.pageY-12)+'px');
  }).on('mouseout', () => tooltip.classed('visible', false));

  simulation = d3.forceSimulation(topo.nodes)
    .force('link', d3.forceLink(topo.links).id(d => d.id).distance(d => {
      const sr = typeof d.source === 'object' ? d.source.r : 10;
      const tr = typeof d.target === 'object' ? d.target.r : 10;
      return 50 + sr + tr + (d.w || 1) * 8;
    }).strength(0.4))
    .force('charge', d3.forceManyBody().strength(-250))
    .force('center', d3.forceCenter(width / 2, height / 2))
    .force('collision', d3.forceCollide().radius(d => d.r + 15))
    .force('x', d3.forceX(width / 2).strength(0.02))
    .force('y', d3.forceY(height / 2).strength(0.02))
    .on('tick', () => {
      linkSel.attr('x1', d => d.source.x).attr('y1', d => d.source.y)
        .attr('x2', d => d.target.x).attr('y2', d => d.target.y);
      linkLabels.attr('x', d => (d.source.x+d.target.x)/2).attr('y', d => (d.source.y+d.target.y)/2-5);
      nodeSel.attr('transform', d => `translate(${d.x},${d.y})`);

      // VLAN hulls
      hullG.selectAll('*').remove();
      const groups = {};
      topo.nodes.forEach(n => {
        const key = n.isInfra ? '_infra' : (n.vlan || '?');
        if (!groups[key]) groups[key] = [];
        groups[key].push(n);
      });
      Object.entries(groups).forEach(([vlan, members]) => {
        if (members.length < 3) return;
        const pts = members.map(m => [m.x, m.y]);
        const hull = d3.polygonHull(pts);
        if (!hull) return;
        const cx = d3.mean(hull, p => p[0]), cy = d3.mean(hull, p => p[1]);
        const expanded = hull.map(p => {
          const dx = p[0]-cx, dy = p[1]-cy;
          const dist = Math.sqrt(dx*dx+dy*dy);
          return [cx+dx*(dist+45)/dist, cy+dy*(dist+45)/dist];
        });
        const col = vlan === '_infra' ? INFRA_COLOR : netColor(vlan);
        hullG.append('path').attr('class','vlan-hull')
          .attr('d','M'+expanded.join('L')+'Z')
          .attr('fill',col).attr('stroke',col);
        hullG.append('text').attr('class','vlan-label')
          .attr('x',cx).attr('y',d3.min(expanded,p=>p[1])-6)
          .attr('fill',col).text(vlan === '_infra' ? 'Infrastructure' : vlan);
      });
    });

  // Pulse animation
  (function pulse() {
    d3.selectAll('.pulse-ring')
      .transition().duration(1800).ease(d3.easeQuadOut)
      .attr('r', 26).attr('stroke-opacity', 0)
      .transition().duration(0)
      .attr('r', 18).attr('stroke-opacity', 0.4)
      .on('end', pulse);
  })();
}

// ── Data panel rendering ──────────────────────────────────
function renderData(data) {
  // Health
  const hc = document.getElementById('health-cards');
  hc.innerHTML = '';
  const wan = data.health.wan || {};
  const cards = [
    { label: 'WAN', value: wan.status === 'ok' ? 'Online' : 'Down', cls: wan.status === 'ok' ? 'status-ok' : 'status-bad', sub: wan.isp || '' },
    { label: 'Clients', value: data.summary.total_clients, cls: 'status-ok', sub: `${data.summary.wired_clients}W ${data.summary.wifi_clients}Wi` },
    { label: 'Devices', value: data.summary.total_devices, cls: 'status-ok', sub: 'UniFi managed' },
    { label: 'Networks', value: data.summary.networks, cls: 'status-ok', sub: 'enabled' },
  ];
  cards.forEach(c => {
    const el = document.createElement('div');
    el.className = 'health-card';
    el.innerHTML = `<div class="label">${c.label}</div><div class="value ${c.cls}">${c.value}</div><div class="sub">${c.sub}</div>`;
    hc.appendChild(el);
  });

  // Devices
  const dc = document.getElementById('device-cards');
  dc.innerHTML = '';
  data.devices.forEach(d => {
    const el = document.createElement('div');
    el.className = 'device-card';
    el.innerHTML = `<div class="name">${d.name}</div><div class="meta">${d.model} · ${d.ip} · ${d.num_sta} clients · up ${d.uptime_days.toFixed(0)}d · fw ${d.version}</div>`;
    dc.appendChild(el);
  });

  // Clients
  const tbody = document.getElementById('client-tbody');
  tbody.innerHTML = '';
  data.clients.forEach(c => {
    const tr = document.createElement('tr');
    const badge = c.is_wired ? '<span class="wired-badge">W</span>' : '<span class="wifi-badge">~</span>';
    const traffic = fmtBytes(c.tx_bytes + c.rx_bytes);
    tr.innerHTML = `<td>${badge}</td><td class="name-col">${c.name}</td><td class="ip-col">${c.ip}</td><td class="net-col">${c.network}</td><td class="traffic-col">${traffic}</td>`;
    tbody.appendChild(tr);
  });

  // Networks
  const nl = document.getElementById('net-list');
  nl.innerHTML = '';
  data.networks.filter(n => n.enabled && n.subnet).forEach(n => {
    const el = document.createElement('div');
    el.className = 'net-item';
    const col = netColor(n.name);
    el.innerHTML = `<div><span class="net-dot" style="background:${col}"></span><span class="net-name">${n.name}</span></div><div class="net-detail">${n.subnet} · ${n.purpose}</div>`;
    nl.appendChild(el);
  });
}

// ── Data fetch loop ───────────────────────────────────────
let firstLoad = true;
async function refresh() {
  try {
    const resp = await fetch('/api/data');
    const data = await resp.json();

    document.getElementById('topo-sub').textContent =
      `${data.summary.total_clients} clients · ${data.summary.total_devices} devices · ${new Date(data.timestamp).toLocaleTimeString()}`;

    if (firstLoad) {
      const topo = buildTopology(data);
      renderTopology(topo);
      firstLoad = false;
    }
    renderData(data);
    document.getElementById('refresh-indicator').textContent = new Date().toLocaleTimeString();
  } catch (e) {
    document.getElementById('refresh-indicator').textContent = 'Error: ' + e.message;
  }
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
