#!/usr/bin/env python3
"""
NetDash - Network & System Status Dashboard
Built by Claude (Opus 4.5) for the Clawdbot machine.

Checks known devices, services, and system health on Krz's network.
"""

import asyncio
import json
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

# ─── Configuration: Known Network Devices & Services ─────────────────────────

DEVICES = {
    "NAS (Synology)": {
        "ip": "192.168.53.73",
        "ports": {5001: "DSM", 445: "SMB", 22: "SSH"},
    },
    "Home Assistant": {
        "ip": "192.168.53.246",
        "ports": {8123: "HA Web UI"},
    },
    "Plex Server": {
        "ip": "192.168.56.231",
        "ports": {32400: "Plex", 13378: "Audiobookshelf"},
    },
    "PlexDownloader (*arr)": {
        "ip": "192.168.56.244",
        "ports": {
            7878: "Radarr",
            8989: "Sonarr",
            8787: "Readarr",
            9696: "Prowlarr",
            8096: "Jellyfin",
            8080: "qBittorrent",
            5000: "Ombi",
        },
    },
    "Alex-PcLinux": {
        "ip": "192.168.53.108",
        "ports": {3000: "Crypto Tracker", 5001: "Dockge"},
    },
    "UniFi Gateway": {
        "ip": "192.168.53.1",
        "ports": {443: "UniFi UI"},
    },
    "Canon Printer": {
        "ip": "192.168.53.58",
        "ports": {80: "Web UI", 631: "IPP"},
    },
    "Samsung TV (98in)": {
        "ip": "192.168.55.44",
        "ports": {8008: "Google Cast"},
    },
    "Minecraft Server": {
        "ip": "192.168.53.245",
        "ports": {},
    },
    "Netgear NAS (Yvette2)": {
        "ip": "192.168.53.240",
        "ports": {445: "SMB", 80: "Web UI"},
    },
}


# ─── Styling ──────────────────────────────────────────────────────────────────

class C:
    """ANSI color codes."""
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"
    BG_DARK = "\033[48;5;235m"

    @staticmethod
    def ok(text):
        return f"{C.GREEN}{text}{C.RESET}"

    @staticmethod
    def warn(text):
        return f"{C.YELLOW}{text}{C.RESET}"

    @staticmethod
    def err(text):
        return f"{C.RED}{text}{C.RESET}"

    @staticmethod
    def info(text):
        return f"{C.CYAN}{text}{C.RESET}"

    @staticmethod
    def dim(text):
        return f"{C.DIM}{text}{C.RESET}"

    @staticmethod
    def bold(text):
        return f"{C.BOLD}{text}{C.RESET}"

    @staticmethod
    def header(text):
        return f"{C.BOLD}{C.BLUE}{text}{C.RESET}"


# ─── Network Checks ──────────────────────────────────────────────────────────

async def check_port(ip: str, port: int, timeout: float = 2.0) -> bool:
    """Check if a TCP port is open."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=timeout
        )
        writer.close()
        await writer.wait_closed()
        return True
    except (asyncio.TimeoutError, ConnectionRefusedError, OSError):
        return False


async def check_ping(ip: str, timeout: float = 2.0) -> Optional[float]:
    """Ping a host and return latency in ms, or None if unreachable."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-W", str(int(timeout)), ip,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout + 1)
        if proc.returncode == 0:
            output = stdout.decode()
            for line in output.split("\n"):
                if "time=" in line:
                    time_str = line.split("time=")[1].split()[0]
                    return float(time_str)
        return None
    except (asyncio.TimeoutError, Exception):
        return None


@dataclass
class DeviceStatus:
    name: str
    ip: str
    ping_ms: Optional[float] = None
    reachable: bool = False
    services: dict = field(default_factory=dict)  # port -> (name, is_up)


async def check_device(name: str, config: dict) -> DeviceStatus:
    """Check a device's connectivity and services."""
    ip = config["ip"]
    ports = config.get("ports", {})

    status = DeviceStatus(name=name, ip=ip)

    # Run ping and port checks concurrently
    ping_task = check_ping(ip)
    port_tasks = {port: check_port(ip, port) for port, svc in ports.items()}

    # Gather all results
    ping_result = await ping_task
    status.ping_ms = ping_result
    status.reachable = ping_result is not None

    for port, svc_name in ports.items():
        is_up = await port_tasks[port]
        status.services[port] = (svc_name, is_up)
        if is_up:
            status.reachable = True  # Port open means reachable even without ping

    return status


# ─── System Info ──────────────────────────────────────────────────────────────

def get_system_info() -> dict:
    """Gather local system metrics."""
    info = {}

    # CPU usage
    try:
        load1, load5, load15 = [x / 100 for x in open("/proc/loadavg").read().split()[:3]] if False else [float(x) for x in open("/proc/loadavg").read().split()[:3]]
        ncpu = int(subprocess.check_output(["nproc"]).decode().strip())
        info["cpu"] = {"load1": load1, "load5": load5, "load15": load15, "cores": ncpu}
    except Exception:
        info["cpu"] = None

    # Memory
    try:
        meminfo = {}
        for line in open("/proc/meminfo"):
            parts = line.split(":")
            if len(parts) == 2:
                key = parts[0].strip()
                val = int(parts[1].strip().split()[0])  # kB
                meminfo[key] = val
        total = meminfo.get("MemTotal", 0)
        avail = meminfo.get("MemAvailable", 0)
        used = total - avail
        info["memory"] = {
            "total_gb": round(total / 1024 / 1024, 1),
            "used_gb": round(used / 1024 / 1024, 1),
            "avail_gb": round(avail / 1024 / 1024, 1),
            "pct": round(used / total * 100, 1) if total > 0 else 0,
        }
    except Exception:
        info["memory"] = None

    # Disk
    try:
        result = subprocess.check_output(
            ["df", "-BG", "--output=size,used,avail,pcent", "/"],
            text=True,
        ).strip().split("\n")[1].split()
        info["disk"] = {
            "total": result[0],
            "used": result[1],
            "avail": result[2],
            "pct": result[3],
        }
    except Exception:
        info["disk"] = None

    # Uptime
    try:
        uptime_secs = float(open("/proc/uptime").read().split()[0])
        days = int(uptime_secs // 86400)
        hours = int((uptime_secs % 86400) // 3600)
        mins = int((uptime_secs % 3600) // 60)
        info["uptime"] = f"{days}d {hours}h {mins}m"
    except Exception:
        info["uptime"] = "unknown"

    # Docker containers
    try:
        result = subprocess.check_output(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        info["docker"] = result.split("\n") if result else []
    except Exception:
        info["docker"] = []

    # Ollama models
    try:
        result = subprocess.check_output(
            ["ollama", "list"], text=True, stderr=subprocess.DEVNULL,
        ).strip()
        lines = result.split("\n")[1:]  # skip header
        info["ollama"] = [l.split()[0] for l in lines if l.strip()]
    except Exception:
        info["ollama"] = []

    return info


# ─── Display ──────────────────────────────────────────────────────────────────

def render_bar(pct: float, width: int = 20) -> str:
    """Render a percentage bar."""
    filled = int(pct / 100 * width)
    bar = "█" * filled + "░" * (width - filled)
    if pct > 85:
        return C.err(bar)
    elif pct > 60:
        return C.warn(bar)
    return C.ok(bar)


def print_header():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print()
    print(f"  {C.bold(C.info('╔══════════════════════════════════════════════════════════════╗'))}")
    print(f"  {C.bold(C.info('║'))}  {C.bold('NetDash')} {C.dim('— Network & System Status')}                          {C.bold(C.info('║'))}")
    print(f"  {C.bold(C.info('║'))}  {C.dim(now)}                                      {C.bold(C.info('║'))}")
    print(f"  {C.bold(C.info('╚══════════════════════════════════════════════════════════════╝'))}")
    print()


def print_system(info: dict):
    print(f"  {C.header('── System Health ──────────────────────────────────────────────')}")
    print()

    # CPU
    if info.get("cpu"):
        cpu = info["cpu"]
        load_pct = cpu["load1"] / cpu["cores"] * 100
        print(f"    CPU    {render_bar(load_pct)} {load_pct:5.1f}%  ({cpu['cores']} cores, load {cpu['load1']:.1f}/{cpu['load5']:.1f}/{cpu['load15']:.1f})")

    # Memory
    if info.get("memory"):
        mem = info["memory"]
        print(f"    Memory {render_bar(mem['pct'])} {mem['pct']:5.1f}%  ({mem['used_gb']:.1f}G / {mem['total_gb']:.1f}G)")

    # Disk
    if info.get("disk"):
        disk = info["disk"]
        pct_val = float(disk["pct"].replace("%", ""))
        print(f"    Disk   {render_bar(pct_val)} {disk['pct']:>5s}   ({disk['used']} / {disk['total']})")

    # Uptime
    print(f"    Uptime {C.dim(info.get('uptime', 'unknown'))}")

    # Docker
    if info.get("docker"):
        print(f"\n    {C.dim('Docker:')} {', '.join(info['docker'])}")
    else:
        print(f"\n    {C.dim('Docker:')} no running containers")

    # Ollama
    if info.get("ollama"):
        print(f"    {C.dim('Ollama:')} {', '.join(info['ollama'])}")
    else:
        print(f"    {C.dim('Ollama:')} no models loaded")

    print()


def print_devices(statuses: list[DeviceStatus]):
    print(f"  {C.header('── Network Devices ───────────────────────────────────────────')}")
    print()

    for dev in statuses:
        # Device header
        if dev.reachable:
            icon = C.ok("●")
            latency = f" {C.dim(f'{dev.ping_ms:.1f}ms')}" if dev.ping_ms else ""
        else:
            icon = C.err("●")
            latency = ""

        print(f"    {icon} {C.bold(dev.name):<30s} {C.dim(dev.ip)}{latency}")

        # Services
        if dev.services:
            svcs = []
            for port, (svc_name, is_up) in sorted(dev.services.items()):
                if is_up:
                    svcs.append(C.ok(f"  {svc_name}:{port}"))
                else:
                    svcs.append(C.err(f"  {svc_name}:{port}"))

            # Print services in rows of 4
            for i in range(0, len(svcs), 4):
                row = "  ".join(svcs[i : i + 4])
                print(f"      {row}")

    print()


def print_summary(statuses: list[DeviceStatus]):
    total = len(statuses)
    up = sum(1 for d in statuses if d.reachable)
    down = total - up
    total_services = sum(len(d.services) for d in statuses)
    services_up = sum(
        sum(1 for _, (_, is_up) in d.services.items() if is_up) for d in statuses
    )
    services_down = total_services - services_up

    print(f"  {C.header('── Summary ───────────────────────────────────────────────────')}")
    print()
    print(f"    Devices:  {C.ok(f'{up} up')}  {C.err(f'{down} down') if down else C.dim('0 down')}  ({total} total)")
    print(f"    Services: {C.ok(f'{services_up} up')}  {C.err(f'{services_down} down') if services_down else C.dim('0 down')}  ({total_services} total)")
    print()


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    json_mode = "--json" in sys.argv
    quiet_mode = "--quiet" in sys.argv or "-q" in sys.argv

    if not json_mode:
        print_header()

    # System info
    sys_info = get_system_info()
    if not json_mode:
        print_system(sys_info)

    # Check all devices concurrently
    tasks = [check_device(name, config) for name, config in DEVICES.items()]
    statuses = await asyncio.gather(*tasks)

    if json_mode:
        output = {
            "timestamp": datetime.now().isoformat(),
            "system": sys_info,
            "devices": [
                {
                    "name": s.name,
                    "ip": s.ip,
                    "reachable": s.reachable,
                    "ping_ms": s.ping_ms,
                    "services": {
                        str(port): {"name": name, "up": up}
                        for port, (name, up) in s.services.items()
                    },
                }
                for s in statuses
            ],
        }
        print(json.dumps(output, indent=2))
    else:
        print_devices(statuses)
        print_summary(statuses)


if __name__ == "__main__":
    asyncio.run(main())
