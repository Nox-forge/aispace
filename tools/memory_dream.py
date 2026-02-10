#!/usr/bin/env python3
"""
Memory Dream — Nightly coherence checker.
Cross-references CLAUDE.md, MCP memory graph, and live system state.
Reports incongruities via Telegram.

Built by Claude (Opus 4.5) for AiSpace.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# ─── Constants ────────────────────────────────────────────────────────────────

HOME = Path.home()
CLAUDE_MD = HOME / "CLAUDE.md"
MEMORY_JSON = HOME / ".claude" / "memory.json"
LOG_DIR = HOME / "aispace" / "logs"
BIN_DIR = HOME / "bin"
LOCAL_BIN = HOME / ".local" / "bin"
TIMEOUT = 5  # seconds for subprocess calls


# ─── Data Loaders ─────────────────────────────────────────────────────────────

def load_claude_md():
    """Parse CLAUDE.md into sections and extract structured data."""
    if not CLAUDE_MD.exists():
        print("FATAL: CLAUDE.md not found at", CLAUDE_MD, file=sys.stderr)
        sys.exit(1)
    text = CLAUDE_MD.read_text(encoding="utf-8")
    return text


def parse_services_table(text):
    """Extract services from the markdown table in CLAUDE.md."""
    services = []
    in_table = False
    for line in text.splitlines():
        if "| Service " in line and "| Type " in line:
            in_table = True
            continue
        if in_table and line.startswith("|---"):
            continue
        if in_table and line.startswith("|"):
            cols = [c.strip() for c in line.split("|")]
            # cols: ['', 'service', 'type', 'port', 'description', '']
            cols = [c for c in cols if c]
            if len(cols) >= 4:
                services.append({
                    "name": cols[0],
                    "type": cols[1],       # user, system, Docker
                    "port": cols[2],
                    "description": cols[3] if len(cols) > 3 else "",
                })
        elif in_table and not line.startswith("|"):
            break
    return services


def parse_bin_tools(text):
    """Extract tool names listed under ~/bin/ Tools section."""
    tools = {}
    # Parse lines like: - **Core scripts**: send-telegram-voice, send-email, ...
    # and: - **~/.local/bin/**: send-telegram, nano-pdf, ...
    pattern = re.compile(r"-\s+\*\*(.+?)\*\*:\s*(.+)")
    in_bin_section = False
    for line in text.splitlines():
        if "### ~/bin/ Tools" in line:
            in_bin_section = True
            continue
        if in_bin_section and line.startswith("###"):
            break
        if in_bin_section:
            m = pattern.match(line.strip())
            if m:
                category = m.group(1).strip()
                items = []
                for t in m.group(2).split(","):
                    # Strip parenthetical descriptions:
                    # "fintools (quote/hist/...)" → "fintools"
                    # "ollama-grid-search (model comparison UI)" → "ollama-grid-search"
                    name = re.sub(r'\s*\(.*\)\s*$', '', t).strip()
                    if name:
                        items.append(name)
                tools[category] = items
    return tools


def parse_cli_tools(text):
    """Extract CLI tool names from the CLI Tools section."""
    tools = []
    pattern = re.compile(r"-\s+\*\*(.+?)\*\*")
    in_section = False
    for line in text.splitlines():
        if "### CLI Tools" in line:
            in_section = True
            continue
        if in_section and line.startswith("###"):
            break
        if in_section:
            m = pattern.match(line.strip())
            if m:
                name = m.group(1).strip()
                # Handle entries like "fd" (`fdfind`) or "lynx" / "w3m"
                if "/" in name:
                    parts = [p.strip() for p in name.split("/")]
                    tools.extend(parts)
                else:
                    tools.append(name)
    return tools


def load_memory():
    """Load MCP memory graph from NDJSON file."""
    if not MEMORY_JSON.exists():
        return {"entities": [], "relations": []}
    entities = []
    relations = []
    try:
        with open(MEMORY_JSON, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "entity":
                    entities.append(obj)
                elif obj.get("type") == "relation":
                    relations.append(obj)
    except Exception as e:
        print(f"WARNING: Could not read memory.json: {e}", file=sys.stderr)
    return {"entities": entities, "relations": relations}


def run_cmd(cmd, timeout=TIMEOUT, shell=False):
    """Run a command with timeout, return (stdout, returncode)."""
    try:
        result = subprocess.run(
            cmd if shell else cmd,
            capture_output=True, text=True, timeout=timeout,
            shell=shell,
        )
        return result.stdout.strip(), result.returncode
    except subprocess.TimeoutExpired:
        return "", -1
    except FileNotFoundError:
        return "", -2
    except Exception as e:
        return str(e), -3


# ─── Check Category 1: Path Verification ─────────────────────────────────────

def check_paths(memory):
    """Verify every path mentioned in memory graph entities exists on disk."""
    issues = []
    # Only match ~ followed by / (not ~2Gbps, ~32B, etc.)
    path_re = re.compile(r"(?:/home/clawdbot/[/\w.\-]+|~/[/\w.\-]+)")
    for entity in memory["entities"]:
        for obs in entity.get("observations", []):
            for match in path_re.finditer(obs):
                raw = match.group(0)
                expanded = raw.replace("~", str(HOME))
                # Skip URLs
                start = obs.find(raw)
                if start > 0 and "http" in obs[max(0, start - 10):start]:
                    continue
                # Skip if it looks like an IP or version
                if re.match(r".*\d+\.\d+\.\d+\.\d+.*", expanded.split("/")[-1]):
                    continue
                p = Path(expanded)
                if not p.exists():
                    issues.append(f"[{entity['name']}] path does not exist: {raw}")
    return issues


# ─── Check Category 2: Service Status ────────────────────────────────────────

def check_services(claude_md_text):
    """Check CLAUDE.md service table vs actually running services."""
    issues = []
    services = parse_services_table(claude_md_text)

    # Check each listed service
    for svc in services:
        name = svc["name"]
        svc_type = svc["type"]

        if svc_type == "Docker":
            # Check docker container
            out, rc = run_cmd("sg docker -c 'docker ps --format {{.Names}}'", shell=True)
            container_match = name.lower().replace(".service", "").replace(".", "")
            # For changedetection.io, look for partial match
            docker_names = out.lower() if rc == 0 else ""
            search_term = name.lower().replace(".service", "").split(".")[0]
            if search_term not in docker_names and name.lower() not in docker_names:
                issues.append(f"Docker container '{name}' not running")
        elif svc_type == "user":
            out, rc = run_cmd(["systemctl", "--user", "is-active", name])
            if out != "active":
                issues.append(f"User service '{name}' not active (status: {out or 'unknown'})")
        elif svc_type == "system":
            out, rc = run_cmd(["systemctl", "is-active", name])
            if out != "active":
                issues.append(f"System service '{name}' not active (status: {out or 'unknown'})")

    # Check for running user services not listed in CLAUDE.md
    # Only flag services that look like custom/project services, not desktop infrastructure
    listed_names = {s["name"].lower() for s in services}

    # Prefixes for desktop/system infrastructure services to ignore
    desktop_prefixes = (
        "dbus", "dconf", "evolution-", "filter-chain", "gcr-", "gnome-",
        "gpg-agent", "gvfs-", "org.freedesktop.", "org.gnome.", "pipewire",
        "snap.", "speech-", "tracker-", "wireplumber", "xdg-", "at-spi",
        "ibus", "pulseaudio", "systemd-",
    )

    out, _ = run_cmd(["systemctl", "--user", "list-units", "--type=service",
                      "--state=running", "--no-pager", "--plain", "--no-legend"])
    for line in out.splitlines():
        parts = line.split()
        if parts:
            svc_name = parts[0]
            svc_lower = svc_name.lower()
            if svc_lower in listed_names:
                continue
            if any(svc_lower.startswith(p) for p in desktop_prefixes):
                continue
            issues.append(f"Unlisted user service running: {svc_name}")

    return issues


# ─── Check Category 3: Tool Existence ────────────────────────────────────────

def check_tools(claude_md_text):
    """Check CLAUDE.md tool lists vs what exists on disk."""
    issues = []
    bin_tools = parse_bin_tools(claude_md_text)
    cli_tools = parse_cli_tools(claude_md_text)

    # Map of tool name -> actual binary name for known aliases
    aliases = {
        "fd": "fdfind",
        "ripgrep": "rg",
        "lazygit": "lazygit",
        "yt-dlp": "yt-dlp",
        "pandoc": "pandoc",
        "tesseract": "tesseract",
        "jq": "jq",
        "lynx": "lynx",
        "w3m": "w3m",
        "sshfs": "sshfs",
        "rsync": "rsync",
        "nmap": "nmap",
        "ImageMagick": "convert",
    }

    # Check ~/bin/ tools
    for category, tools in bin_tools.items():
        if "Full list" in category:
            continue
        for raw_tool in tools:
            raw_tool = raw_tool.strip()
            if not raw_tool or "Full list" in raw_tool:
                continue
            # Handle entries like "uv/uvx" — split and check each
            sub_tools = [t.strip() for t in raw_tool.split("/") if t.strip()]
            for tool in sub_tools:
                if category == "~/.local/bin/":
                    check_path = LOCAL_BIN / tool
                elif category == "npm globals":
                    _, rc = run_cmd(["which", tool])
                    if rc != 0:
                        issues.append(f"npm global '{tool}' not found in PATH")
                    continue
                else:
                    check_path = BIN_DIR / tool
                if not check_path.exists():
                    # Try which as fallback
                    _, rc = run_cmd(["which", tool])
                    if rc != 0:
                        issues.append(f"Tool '{tool}' ({category}) not found at {check_path}")

    # Check CLI tools
    for tool in cli_tools:
        binary = aliases.get(tool, tool)
        _, rc = run_cmd(["which", binary])
        if rc != 0:
            issues.append(f"CLI tool '{tool}' (binary: {binary}) not in PATH")

    # Check for new tools in ~/bin/ not listed in CLAUDE.md
    all_listed = set()
    for tools in bin_tools.values():
        for t in tools:
            t = t.strip()
            if t and "Full list" not in t:
                # Handle compound entries like "uv/uvx"
                for sub in t.split("/"):
                    sub = sub.strip()
                    if sub:
                        all_listed.add(sub)

    # Also include tools mentioned elsewhere in CLAUDE.md (Development section, etc.)
    # Extract tool names mentioned with backticks: `tool_name`
    for m in re.finditer(r'`(\w[\w-]*)`', claude_md_text):
        all_listed.add(m.group(1))

    if BIN_DIR.exists():
        for f in sorted(BIN_DIR.iterdir()):
            if f.is_file() or f.is_symlink():
                if f.name not in all_listed:
                    issues.append(f"New tool in ~/bin/ not listed in CLAUDE.md: {f.name}")

    return issues


# ─── Check Category 4: Version Checks ────────────────────────────────────────

def check_versions(claude_md_text, memory):
    """Verify recorded versions match actual versions."""
    issues = []

    # CCC version
    ccc_out, rc = run_cmd([str(BIN_DIR / "ccc"), "version"])
    if rc == 0:
        ccc_version = ccc_out.strip()
        if "v2.0.0" in claude_md_text and "2.0.0" not in ccc_version:
            issues.append(f"CCC version mismatch: CLAUDE.md says v2.0.0, actual: {ccc_version}")
    else:
        issues.append(f"Could not check CCC version (rc={rc})")

    # Ollama local models
    ollama_out, rc = run_cmd(["ollama", "list"])
    if rc == 0:
        actual_models = set()
        for line in ollama_out.splitlines():
            parts = line.split()
            if not parts or parts[0] == "NAME":
                continue  # skip header
            actual_models.add(parts[0].strip())
        # CLAUDE.md says: qwen2.5:1.5b, qwen3:0.6b, gemma3:1b, qwen3:4b, qwen3:8b
        listed_local = {"qwen2.5:1.5b", "qwen3:0.6b", "gemma3:1b", "qwen3:4b", "qwen3:8b"}
        missing = listed_local - actual_models
        extra = actual_models - listed_local
        if missing:
            issues.append(f"Local Ollama models listed but missing: {', '.join(sorted(missing))}")
        if extra:
            issues.append(f"Local Ollama models present but unlisted: {', '.join(sorted(extra))}")
    else:
        issues.append("Could not list local Ollama models")

    # GitHub active account
    gh_out, rc = run_cmd(["gh", "auth", "status"])
    if rc != 0:
        # gh auth status outputs to stderr
        gh_out2, _ = run_cmd("gh auth status 2>&1", shell=True)
        gh_out = gh_out2
    if gh_out:
        if "Nox-forge" not in gh_out and "nox-forge" not in gh_out.lower():
            issues.append(f"GitHub active account may not be Nox-forge: {gh_out[:100]}")

    # Docker version
    docker_out, rc = run_cmd("sg docker -c 'docker --version'", shell=True)
    if rc == 0 and "v29.2.0" in claude_md_text:
        if "29.2.0" not in docker_out:
            issues.append(f"Docker version mismatch: CLAUDE.md says v29.2.0, actual: {docker_out}")

    # Ollama version
    ollama_ver, rc = run_cmd(["ollama", "--version"])
    if rc == 0 and "v0.15.2" in claude_md_text:
        if "0.15.2" not in ollama_ver:
            issues.append(f"Ollama version mismatch: CLAUDE.md says v0.15.2, actual: {ollama_ver}")

    return issues


# ─── Check Category 5: Cross-Source Coherence ────────────────────────────────

def check_cross_source(claude_md_text, memory):
    """Check facts that appear in both CLAUDE.md and memory graph agree."""
    issues = []

    # Build a lookup of memory observations by entity name
    mem_obs = {}
    for entity in memory["entities"]:
        name = entity.get("name", "")
        obs_list = entity.get("observations", [])
        mem_obs[name] = obs_list

    # CCC version: CLAUDE.md vs memory
    claude_ccc = "v2.0.0" if "v2.0.0" in claude_md_text else None
    mem_ccc = None
    for obs in mem_obs.get("ccc-tool", []) + mem_obs.get("ClawdbotVM", []):
        m = re.search(r"CCC v?([\d.]+)", obs)
        if m:
            mem_ccc = m.group(1)
            break
    if claude_ccc and mem_ccc and claude_ccc.lstrip("v") != mem_ccc.lstrip("v"):
        issues.append(f"CCC version: CLAUDE.md={claude_ccc}, memory={mem_ccc}")

    # Ollama version
    claude_ollama = None
    m = re.search(r"Ollama.*?v([\d.]+)", claude_md_text)
    if m:
        claude_ollama = m.group(1)
    mem_ollama = None
    for obs in mem_obs.get("ClawdbotVM", []):
        m2 = re.search(r"Ollama v?([\d.]+)", obs)
        if m2:
            mem_ollama = m2.group(1)
            break
    if claude_ollama and mem_ollama and claude_ollama != mem_ollama:
        issues.append(f"Ollama version: CLAUDE.md={claude_ollama}, memory={mem_ollama}")

    # GitHub account
    if "Nox-forge" in claude_md_text:
        gh_in_mem = any("Nox-forge" in o or "nox-forge" in o.lower()
                        for o in mem_obs.get("ClawdbotVM", []) + mem_obs.get("Krz-machine-setup", []))
        if not gh_in_mem and mem_obs:
            issues.append("GitHub account 'Nox-forge' in CLAUDE.md but not found in memory")

    # Docker version
    claude_docker = None
    m = re.search(r"Docker.*?v([\d.]+)", claude_md_text)
    if m:
        claude_docker = m.group(1)
    mem_docker = None
    for obs in mem_obs.get("ClawdbotVM", []):
        m2 = re.search(r"Docker v?([\d.]+)", obs)
        if m2:
            mem_docker = m2.group(1)
            break
    if claude_docker and mem_docker and claude_docker != mem_docker:
        issues.append(f"Docker version: CLAUDE.md={claude_docker}, memory={mem_docker}")

    # Local Ollama model count
    claude_model_count = None
    m = re.search(r"Local models:(.+)", claude_md_text)
    if m:
        claude_model_count = len([x.strip() for x in m.group(1).split(",") if x.strip()])
    mem_model_count = None
    for obs in mem_obs.get("ClawdbotVM", []):
        if "local Ollama models" in obs.lower() or "local ollama model" in obs.lower():
            m2 = re.search(r"(\d+)\s+local", obs, re.IGNORECASE)
            if m2:
                mem_model_count = int(m2.group(1))
                break
    if claude_model_count and mem_model_count and claude_model_count != mem_model_count:
        issues.append(f"Local Ollama model count: CLAUDE.md={claude_model_count}, memory={mem_model_count}")

    return issues


# ─── Check Category 6: Stale Entities ────────────────────────────────────────

def check_stale_entities(memory):
    """Flag memory entities referencing dead paths or stopped services."""
    issues = []
    # Entity types that are informational/notes — skip these
    skip_types = {"person", "configuration", "Network", "NetworkDevice", "Device"}

    # Only match ~ followed by / (not ~2Gbps, ~32B, etc.)
    path_re = re.compile(r"(?:/home/clawdbot/[/\w.\-]+|~/[/\w.\-]+)")

    for entity in memory["entities"]:
        etype = entity.get("entityType", "")
        ename = entity.get("name", "")
        if etype in skip_types:
            continue

        dead_paths = []
        for obs in entity.get("observations", []):
            for match in path_re.finditer(obs):
                raw = match.group(0)
                expanded = raw.replace("~", str(HOME))
                # Skip URLs
                start = obs.find(raw)
                if start > 0 and "http" in obs[max(0, start - 10):start]:
                    continue
                if re.match(r".*\d+\.\d+\.\d+\.\d+.*", expanded.split("/")[-1]):
                    continue
                p = Path(expanded)
                if not p.exists():
                    dead_paths.append(raw)

        # Check service references — match word-char+.service (not parens, etc.)
        service_re = re.compile(r"\b([\w-]+\.service)\b")
        for obs in entity.get("observations", []):
            for svc_match in service_re.finditer(obs):
                svc = svc_match.group(1)
                # Try both user and system
                out1, _ = run_cmd(["systemctl", "--user", "is-active", svc])
                out2, _ = run_cmd(["systemctl", "is-active", svc])
                if out1 != "active" and out2 != "active":
                    # Only flag if it looks like a claim the service is running
                    if "running" in obs.lower() or "enabled" in obs.lower():
                        issues.append(f"[{ename}] references '{svc}' as running, but it's not active")

        if dead_paths:
            unique = list(dict.fromkeys(dead_paths))[:5]
            issues.append(f"[{ename}] has {len(dead_paths)} dead path(s): {', '.join(unique)}")

    return issues


# ─── Report Generation ────────────────────────────────────────────────────────

def generate_report(results):
    """Generate full log report and short Telegram summary."""
    now = datetime.now()
    total_issues = sum(len(v) for v in results.values())

    lines = []
    lines.append(f"{'='*60}")
    lines.append(f"  Memory Dream — Coherence Report")
    lines.append(f"  {now.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"  Total issues: {total_issues}")
    lines.append(f"{'='*60}")
    lines.append("")

    for category, issues in results.items():
        status = "CLEAN" if not issues else f"{len(issues)} issue(s)"
        lines.append(f"── {category} [{status}] ──")
        if issues:
            for issue in issues:
                lines.append(f"  ⚠ {issue}")
        else:
            lines.append("  ✓ All checks passed")
        lines.append("")

    full_report = "\n".join(lines)

    # Short Telegram summary
    if total_issues == 0:
        telegram_msg = f"🌙 Memory Dream — All coherent. 0 issues."
    else:
        tg_lines = [f"🌙 Memory Dream — {total_issues} issue(s) found:"]
        shown = 0
        for category, issues in results.items():
            for issue in issues:
                if shown < 8:
                    tg_lines.append(f"• {issue}")
                    shown += 1
        if total_issues > 8:
            tg_lines.append(f"... and {total_issues - 8} more")
        log_date = now.strftime("%Y-%m-%d")
        tg_lines.append(f"Full report: ~/aispace/logs/dream-{log_date}.log")
        telegram_msg = "\n".join(tg_lines)

    return full_report, telegram_msg


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    dry_run = "--dry-run" in sys.argv

    print("Memory Dream starting...", flush=True)

    # Load data sources
    claude_md_text = load_claude_md()
    memory = load_memory()

    entity_count = len(memory["entities"])
    print(f"Loaded CLAUDE.md ({len(claude_md_text)} chars), memory ({entity_count} entities)")

    if not memory["entities"]:
        print("WARNING: Memory graph is empty — memory checks will be limited")

    # Run all six check categories
    results = {}

    print("  [1/6] Path Verification...", flush=True)
    results["1. Path Verification"] = check_paths(memory)

    print("  [2/6] Service Status...", flush=True)
    results["2. Service Status"] = check_services(claude_md_text)

    print("  [3/6] Tool Existence...", flush=True)
    results["3. Tool Existence"] = check_tools(claude_md_text)

    print("  [4/6] Version Checks...", flush=True)
    results["4. Version Checks"] = check_versions(claude_md_text, memory)

    print("  [5/6] Cross-Source Coherence...", flush=True)
    results["5. Cross-Source Coherence"] = check_cross_source(claude_md_text, memory)

    print("  [6/6] Stale Entities...", flush=True)
    results["6. Stale Entities"] = check_stale_entities(memory)

    # Generate report
    full_report, telegram_msg = generate_report(results)

    # Write log file
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    date_str = now.strftime('%Y-%m-%d')
    log_file = LOG_DIR / f"dream-{date_str}.log"
    log_file.write_text(full_report, encoding="utf-8")
    print(f"\nReport written to {log_file}")

    # Write structured JSON companion file
    total_issues = sum(len(v) for v in results.values())
    categories_json = {}
    for category, issues in results.items():
        categories_json[category] = {
            "status": "clean" if not issues else "issues",
            "count": len(issues),
            "issues": issues,
        }
    # Build human-readable summary
    parts = []
    for category, issues in results.items():
        if issues:
            short_name = category.split(". ", 1)[-1] if ". " in category else category
            parts.append(f"{len(issues)} {short_name.lower()}")
    summary = ", ".join(parts) if parts else "All coherent"

    json_file = LOG_DIR / f"dream-{date_str}.json"
    json_data = {
        "date": date_str,
        "timestamp": now.strftime('%Y-%m-%dT%H:%M:%S'),
        "total_issues": total_issues,
        "categories": categories_json,
        "summary": summary,
        "telegram_sent": not dry_run,
    }
    json_file.write_text(json.dumps(json_data, indent=2), encoding="utf-8")
    print(f"JSON written to {json_file}")

    # Print report to stdout
    print(full_report)

    # Send Telegram
    telegram_sent = False
    if dry_run:
        print("\n[DRY RUN] Would send to Telegram:")
        print(telegram_msg)
    else:
        print("\nSending Telegram notification...")
        out, rc = run_cmd(["send-telegram", telegram_msg])
        if rc == 0:
            print("Telegram sent OK")
            telegram_sent = True
        else:
            print(f"Telegram send failed (rc={rc}): {out}", file=sys.stderr)

    # Update JSON with actual telegram_sent status
    json_data["telegram_sent"] = telegram_sent
    json_file.write_text(json.dumps(json_data, indent=2), encoding="utf-8")

    total = sum(len(v) for v in results.values())
    print(f"\nDone. {total} issue(s) found.")
    return 0 if total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
