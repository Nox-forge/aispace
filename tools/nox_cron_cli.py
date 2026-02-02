#!/usr/bin/env python3
"""
Nox-Cron CLI — Manage scheduled tasks for Claude sessions.
Interface for adding, listing, removing, and controlling Nox-Cron jobs.
Built by Claude (Opus 4.5).
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import pytz
from croniter import croniter

# ─── Configuration ────────────────────────────────────────────────────────────

CONFIG_DIR = Path.home() / ".nox-cron"
JOBS_FILE = CONFIG_DIR / "jobs.json"
HISTORY_FILE = CONFIG_DIR / "history.json"
PID_FILE = CONFIG_DIR / "daemon.pid"
DEFAULT_TIMEZONE = "America/Toronto"


# ─── Helpers ──────────────────────────────────────────────────────────────────

def load_jobs() -> list:
    """Load jobs from jobs.json."""
    try:
        if JOBS_FILE.exists() and JOBS_FILE.stat().st_size > 0:
            return json.loads(JOBS_FILE.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"Error loading jobs: {e}", file=sys.stderr)
    return []


def save_jobs(jobs: list):
    """Save jobs to jobs.json."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = JOBS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(jobs, indent=2, default=str) + "\n")
    tmp.replace(JOBS_FILE)


def load_history() -> list:
    """Load execution history."""
    try:
        if HISTORY_FILE.exists() and HISTORY_FILE.stat().st_size > 0:
            return json.loads(HISTORY_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        pass
    return []


def get_daemon_pid() -> int | None:
    """Get the daemon PID if running."""
    try:
        if PID_FILE.exists():
            pid = int(PID_FILE.read_text().strip())
            # Check if process is actually running
            os.kill(pid, 0)
            return pid
    except (ValueError, OSError):
        pass
    return None


def signal_daemon():
    """Send SIGHUP to daemon to reload jobs."""
    pid = get_daemon_pid()
    if pid:
        try:
            os.kill(pid, signal.SIGHUP)
            print(f"Signaled daemon (PID {pid}) to reload jobs")
        except OSError as e:
            print(f"Failed to signal daemon: {e}", file=sys.stderr)
    else:
        print("Daemon not running (changes saved, will take effect on next start)")


def find_job(jobs: list, job_id: str) -> dict | None:
    """Find a job by ID or ID prefix."""
    # Exact match first
    for job in jobs:
        if job.get("id") == job_id:
            return job
    # Prefix match
    matches = [j for j in jobs if j.get("id", "").startswith(job_id)]
    if len(matches) == 1:
        return matches[0]
    elif len(matches) > 1:
        print(f"Ambiguous ID prefix '{job_id}', matches:", file=sys.stderr)
        for m in matches:
            print(f"  {m['id'][:8]}  {m.get('name', '?')}", file=sys.stderr)
        return None
    return None


def parse_interval(spec: str) -> int:
    """Parse interval string like '5m', '1h', '30s', '2h30m' into seconds."""
    pattern = re.compile(r'(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$')
    match = pattern.match(spec.strip())
    if not match or not any(match.groups()):
        raise ValueError(f"Invalid interval format: '{spec}'. Use e.g. '5m', '1h', '30s', '2h30m'.")
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    total = hours * 3600 + minutes * 60 + seconds
    if total <= 0:
        raise ValueError(f"Interval must be positive, got: '{spec}'")
    return total


def validate_schedule(schedule_type: str, schedule: str):
    """Validate schedule format based on type."""
    if schedule_type == "cron":
        try:
            croniter(schedule)
        except (ValueError, KeyError) as e:
            print(f"Invalid cron expression: {e}", file=sys.stderr)
            sys.exit(1)
    elif schedule_type == "every":
        try:
            parse_interval(schedule)
        except ValueError as e:
            print(f"Invalid interval: {e}", file=sys.stderr)
            sys.exit(1)
    elif schedule_type == "at":
        try:
            datetime.fromisoformat(schedule)
        except ValueError:
            print(f"Invalid ISO timestamp: '{schedule}'. Use e.g. '2025-02-01T09:00:00'", file=sys.stderr)
            sys.exit(1)
    else:
        print(f"Unknown schedule type: '{schedule_type}'. Use cron, every, or at.", file=sys.stderr)
        sys.exit(1)


# ─── Execution (for manual 'run' command) ────────────────────────────────────

def tmux_session_exists(session: str) -> bool:
    """Check if a tmux session exists."""
    result = subprocess.run(
        ["tmux", "has-session", "-t", f"claude-{session}"],
        capture_output=True, timeout=5
    )
    return result.returncode == 0


def execute_inject(command: str, session: str) -> tuple[bool, str]:
    """Inject text into a tmux Claude session."""
    target = f"claude-{session}"
    if not tmux_session_exists(session):
        return False, f"tmux session '{target}' does not exist"
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", target, "-l", command],
            capture_output=True, timeout=10, check=True
        )
        subprocess.run(
            ["tmux", "send-keys", "-t", target, "Enter"],
            capture_output=True, timeout=10, check=True
        )
        return True, f"Injected into {target}"
    except subprocess.CalledProcessError as e:
        return False, f"tmux send-keys failed: {e}"
    except subprocess.TimeoutExpired:
        return False, "tmux send-keys timed out"


def execute_run(prompt: str) -> tuple[bool, str]:
    """Run an isolated Claude query."""
    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--no-input"],
            capture_output=True, text=True, timeout=120
        )
        output = result.stdout.strip()
        if result.returncode == 0:
            return True, output
        else:
            error = result.stderr.strip() or f"Exit code {result.returncode}"
            return False, f"{error}\n{output}" if output else error
    except subprocess.TimeoutExpired:
        return False, "Claude query timed out (120s)"
    except FileNotFoundError:
        return False, "claude command not found"


def execute_shell(command: str) -> tuple[bool, str]:
    """Run an arbitrary shell command."""
    try:
        result = subprocess.run(
            command, shell=True,
            capture_output=True, text=True, timeout=300
        )
        output = result.stdout.strip()
        error = result.stderr.strip()
        combined = output
        if error:
            combined = f"{output}\n[stderr] {error}" if output else f"[stderr] {error}"
        if result.returncode == 0:
            return True, combined
        else:
            return False, combined or f"Exit code {result.returncode}"
    except subprocess.TimeoutExpired:
        return False, "Shell command timed out (300s)"


def execute_job(job: dict) -> tuple[bool, str, float]:
    """Execute a job inline. Returns (success, output, duration_seconds)."""
    mode = job.get("execution_mode", "shell")
    command = job.get("command", "")
    session = job.get("session", "general")

    start = time.monotonic()

    if mode == "inject":
        success, output = execute_inject(command, session)
    elif mode == "run":
        success, output = execute_run(command)
    elif mode == "shell":
        success, output = execute_shell(command)
    else:
        success, output = False, f"Unknown execution mode: {mode}"

    duration = round(time.monotonic() - start, 2)
    return success, output, duration


# ─── Commands ─────────────────────────────────────────────────────────────────

def cmd_add(args):
    """Add a new scheduled job."""
    validate_schedule(args.type, args.schedule)

    # Parse delivery
    delivery = {"telegram": False, "signal": False}
    if args.delivery:
        for channel in args.delivery.split(","):
            channel = channel.strip().lower()
            if channel in delivery:
                delivery[channel] = True

    job = {
        "id": str(uuid.uuid4()),
        "name": args.name,
        "schedule_type": args.type,
        "schedule": args.schedule,
        "execution_mode": args.mode,
        "command": args.command,
        "session": args.session or "general",
        "context_depth": args.context_depth or 0,
        "delivery": delivery,
        "timezone": args.timezone or DEFAULT_TIMEZONE,
        "enabled": True,
        "last_run": None,
        "next_run": None
    }

    jobs = load_jobs()
    jobs.append(job)
    save_jobs(jobs)

    print(f"Added job: {job['name']}")
    print(f"  ID:       {job['id'][:8]}...")
    print(f"  Schedule: {args.type} '{args.schedule}'")
    print(f"  Mode:     {args.mode}")
    print(f"  Command:  {args.command[:60]}{'...' if len(args.command) > 60 else ''}")

    signal_daemon()


def cmd_list(args):
    """List all jobs in table format."""
    jobs = load_jobs()
    if not jobs:
        print("No jobs configured.")
        return

    # Table header
    fmt = "{:<10} {:<25} {:<15} {:<8} {:<8} {:<20}"
    print(fmt.format("ID", "NAME", "SCHEDULE", "MODE", "ENABLED", "NEXT RUN"))
    print("-" * 90)

    for job in jobs:
        job_id = job.get("id", "?")[:8]
        name = job.get("name", "?")[:25]
        stype = job.get("schedule_type", "?")
        schedule = job.get("schedule", "?")
        sched_str = f"{stype}:{schedule}"[:15]
        mode = job.get("execution_mode", "?")[:8]
        enabled = "yes" if job.get("enabled", True) else "no"
        next_run = job.get("next_run", "-")
        if next_run and next_run != "-":
            try:
                dt = datetime.fromisoformat(next_run)
                next_run = dt.strftime("%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                next_run = str(next_run)[:20]
        else:
            next_run = "-"
        print(fmt.format(job_id, name, sched_str, mode, enabled, next_run))


def cmd_remove(args):
    """Remove a job by ID."""
    jobs = load_jobs()
    job = find_job(jobs, args.job_id)
    if not job:
        print(f"Job not found: {args.job_id}", file=sys.stderr)
        sys.exit(1)

    name = job.get("name", "?")
    jobs = [j for j in jobs if j.get("id") != job["id"]]
    save_jobs(jobs)
    print(f"Removed job: {name} ({job['id'][:8]})")
    signal_daemon()


def cmd_run(args):
    """Manually trigger a job immediately."""
    jobs = load_jobs()
    job = find_job(jobs, args.job_id)
    if not job:
        print(f"Job not found: {args.job_id}", file=sys.stderr)
        sys.exit(1)

    name = job.get("name", "?")
    print(f"Running job: {name} (mode={job.get('execution_mode')})")

    success, output, duration = execute_job(job)
    status = "SUCCESS" if success else "ERROR"

    print(f"\nStatus: {status} ({duration}s)")
    if output:
        print(f"Output:\n{output}")


def cmd_status(args):
    """Show daemon status and upcoming jobs."""
    pid = get_daemon_pid()
    if pid:
        # Get process start time for uptime
        try:
            stat = Path(f"/proc/{pid}/stat").read_text().split()
            # Get system boot time and process start time for uptime
            uptime_secs = time.time() - os.path.getmtime(f"/proc/{pid}")
            hours = int(uptime_secs // 3600)
            minutes = int((uptime_secs % 3600) // 60)
            uptime_str = f"{hours}h{minutes}m"
        except (OSError, IndexError):
            uptime_str = "unknown"

        print(f"Daemon: RUNNING (PID {pid}, uptime {uptime_str})")
    else:
        print("Daemon: STOPPED")

    jobs = load_jobs()
    enabled = [j for j in jobs if j.get("enabled", True)]
    disabled = [j for j in jobs if not j.get("enabled", True)]
    print(f"Jobs: {len(enabled)} enabled, {len(disabled)} disabled, {len(jobs)} total")

    # Show next upcoming jobs
    upcoming = []
    for job in enabled:
        next_run = job.get("next_run")
        if next_run:
            try:
                dt = datetime.fromisoformat(next_run)
                upcoming.append((dt, job))
            except (ValueError, TypeError):
                pass

    upcoming.sort(key=lambda x: x[0])
    if upcoming:
        print("\nNext scheduled jobs:")
        for dt, job in upcoming[:5]:
            name = job.get("name", "?")
            time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
            print(f"  {time_str}  {name}")


def cmd_history(args):
    """Show execution history."""
    history = load_history()
    if not history:
        print("No execution history.")
        return

    # Filter by job_id if provided
    if args.job_id:
        history = [h for h in history if h.get("job_id", "").startswith(args.job_id)]
        if not history:
            print(f"No history for job: {args.job_id}")
            return

    # Show most recent first
    history.reverse()

    fmt = "{:<20} {:<25} {:<8} {:<8} {}"
    print(fmt.format("TIMESTAMP", "JOB", "STATUS", "DURATION", "OUTPUT"))
    print("-" * 100)

    for entry in history[:20]:
        ts = entry.get("timestamp", "?")
        try:
            dt = datetime.fromisoformat(ts)
            ts = dt.strftime("%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            ts = str(ts)[:20]

        name = entry.get("job_name", "?")[:25]
        status = entry.get("status", "?")[:8]
        duration = f"{entry.get('duration_seconds', 0)}s"[:8]
        output = entry.get("output", "")[:50].replace("\n", " ")

        print(fmt.format(ts, name, status, duration, output))


def cmd_enable(args):
    """Enable a job."""
    jobs = load_jobs()
    job = find_job(jobs, args.job_id)
    if not job:
        print(f"Job not found: {args.job_id}", file=sys.stderr)
        sys.exit(1)

    job["enabled"] = True
    save_jobs(jobs)
    print(f"Enabled job: {job.get('name', '?')} ({job['id'][:8]})")
    signal_daemon()


def cmd_disable(args):
    """Disable a job."""
    jobs = load_jobs()
    job = find_job(jobs, args.job_id)
    if not job:
        print(f"Job not found: {args.job_id}", file=sys.stderr)
        sys.exit(1)

    job["enabled"] = False
    save_jobs(jobs)
    print(f"Disabled job: {job.get('name', '?')} ({job['id'][:8]})")
    signal_daemon()


# ─── Argument Parsing ─────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nox-cron",
        description="Nox-Cron: Task scheduler for Claude sessions"
    )
    sub = parser.add_subparsers(dest="command", help="Command to execute")

    # add
    p_add = sub.add_parser("add", help="Add a new scheduled job")
    p_add.add_argument("--name", required=True, help="Human-readable job name")
    p_add.add_argument("--schedule", required=True, help="Schedule expression (cron, interval, or ISO timestamp)")
    p_add.add_argument("--type", required=True, choices=["cron", "every", "at"],
                        help="Schedule type")
    p_add.add_argument("--mode", required=True, choices=["inject", "run", "shell"],
                        help="Execution mode")
    p_add.add_argument("--command", required=True, help="Command or prompt text")
    p_add.add_argument("--session", default="general", help="Target tmux session name (default: general)")
    p_add.add_argument("--context-depth", type=int, default=0, help="Context depth (default: 0)")
    p_add.add_argument("--delivery", default="", help="Delivery channels, comma-separated (telegram,signal)")
    p_add.add_argument("--timezone", default=DEFAULT_TIMEZONE, help=f"Timezone (default: {DEFAULT_TIMEZONE})")
    p_add.set_defaults(func=cmd_add)

    # list
    p_list = sub.add_parser("list", help="List all scheduled jobs")
    p_list.set_defaults(func=cmd_list)

    # remove
    p_remove = sub.add_parser("remove", help="Remove a job by ID")
    p_remove.add_argument("job_id", help="Job ID (or prefix)")
    p_remove.set_defaults(func=cmd_remove)

    # run
    p_run = sub.add_parser("run", help="Manually trigger a job immediately")
    p_run.add_argument("job_id", help="Job ID (or prefix)")
    p_run.set_defaults(func=cmd_run)

    # status
    p_status = sub.add_parser("status", help="Show daemon status")
    p_status.set_defaults(func=cmd_status)

    # history
    p_history = sub.add_parser("history", help="Show execution history")
    p_history.add_argument("job_id", nargs="?", default=None, help="Filter by job ID (optional)")
    p_history.set_defaults(func=cmd_history)

    # enable
    p_enable = sub.add_parser("enable", help="Enable a job")
    p_enable.add_argument("job_id", help="Job ID (or prefix)")
    p_enable.set_defaults(func=cmd_enable)

    # disable
    p_disable = sub.add_parser("disable", help="Disable a job")
    p_disable.add_argument("job_id", help="Job ID (or prefix)")
    p_disable.set_defaults(func=cmd_disable)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
