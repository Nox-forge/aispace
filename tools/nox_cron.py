#!/usr/bin/env python3
"""
Nox-Cron — Task scheduler daemon for Claude sessions.
Supports cron expressions, intervals, and one-shot scheduling.
Can inject prompts into tmux sessions, run isolated Claude queries, or execute shell commands.
Built by Claude (Opus 4.5).
"""

import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytz
from croniter import croniter

# ─── Configuration ────────────────────────────────────────────────────────────

CONFIG_DIR = Path.home() / ".nox-cron"
JOBS_FILE = CONFIG_DIR / "jobs.json"
HISTORY_FILE = CONFIG_DIR / "history.json"
PID_FILE = CONFIG_DIR / "daemon.pid"
CHECK_INTERVAL = 10  # seconds between schedule checks
HISTORY_MAX = 100    # keep last N history entries
DEFAULT_TIMEZONE = "America/Toronto"


# ─── Logging ──────────────────────────────────────────────────────────────────

def log(message: str):
    """Log to stderr with timestamp."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {message}", file=sys.stderr, flush=True)


# ─── File I/O ─────────────────────────────────────────────────────────────────

def load_json(path: Path) -> list:
    """Load a JSON file, returning empty list on failure."""
    try:
        if path.exists() and path.stat().st_size > 0:
            return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log(f"Error loading {path}: {e}")
    return []


def save_json(path: Path, data: list):
    """Atomically save JSON data to file."""
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, default=str) + "\n")
        tmp.replace(path)
    except OSError as e:
        log(f"Error saving {path}: {e}")


def load_jobs() -> list:
    return load_json(JOBS_FILE)


def save_jobs(jobs: list):
    save_json(JOBS_FILE, jobs)


def load_history() -> list:
    return load_json(HISTORY_FILE)


def save_history(history: list):
    save_json(HISTORY_FILE, history)


# ─── Interval Parsing ────────────────────────────────────────────────────────

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


# ─── Timezone Helpers ─────────────────────────────────────────────────────────

def get_tz(timezone_name: str) -> pytz.BaseTzInfo:
    """Get a pytz timezone object, falling back to default."""
    try:
        return pytz.timezone(timezone_name)
    except pytz.UnknownTimeZoneError:
        log(f"Unknown timezone '{timezone_name}', using {DEFAULT_TIMEZONE}")
        return pytz.timezone(DEFAULT_TIMEZONE)


def now_in_tz(timezone_name: str) -> datetime:
    """Get current time in the given timezone."""
    tz = get_tz(timezone_name)
    return datetime.now(tz)


# ─── Schedule Evaluation ─────────────────────────────────────────────────────

def compute_next_run(job: dict) -> str | None:
    """Compute the next run time for a job. Returns ISO string or None."""
    schedule_type = job.get("schedule_type")
    schedule = job.get("schedule", "")
    tz_name = job.get("timezone", DEFAULT_TIMEZONE)
    tz = get_tz(tz_name)
    now = datetime.now(tz)

    if schedule_type == "cron":
        try:
            cron = croniter(schedule, now)
            next_dt = cron.get_next(datetime)
            return next_dt.isoformat()
        except (ValueError, KeyError) as e:
            log(f"Invalid cron expression for job '{job.get('name')}': {e}")
            return None

    elif schedule_type == "every":
        last_run = job.get("last_run")
        if last_run:
            try:
                interval = parse_interval(schedule)
                last_dt = datetime.fromisoformat(last_run)
                if last_dt.tzinfo is None:
                    last_dt = tz.localize(last_dt)
                next_dt = last_dt + timedelta(seconds=interval)
                return next_dt.isoformat()
            except (ValueError, TypeError) as e:
                log(f"Error computing next run for '{job.get('name')}': {e}")
                return None
        else:
            # Never run before, run immediately
            return now.isoformat()

    elif schedule_type == "at":
        return schedule  # The schedule IS the target time

    return None


def is_due(job: dict) -> bool:
    """Check if a job should fire now."""
    if not job.get("enabled", True):
        return False

    schedule_type = job.get("schedule_type")
    schedule = job.get("schedule", "")
    tz_name = job.get("timezone", DEFAULT_TIMEZONE)
    tz = get_tz(tz_name)
    now = datetime.now(tz)

    if schedule_type == "cron":
        last_run = job.get("last_run")
        try:
            if last_run:
                last_dt = datetime.fromisoformat(last_run)
                if last_dt.tzinfo is None:
                    last_dt = tz.localize(last_dt)
                cron = croniter(schedule, last_dt)
                next_dt = cron.get_next(datetime)
            else:
                # First run: check if we're past a cron tick
                base = now - timedelta(seconds=CHECK_INTERVAL + 1)
                cron = croniter(schedule, base)
                next_dt = cron.get_next(datetime)
            return now >= next_dt
        except (ValueError, KeyError):
            return False

    elif schedule_type == "every":
        last_run = job.get("last_run")
        if not last_run:
            return True  # Never run, fire immediately
        try:
            interval = parse_interval(schedule)
            last_dt = datetime.fromisoformat(last_run)
            if last_dt.tzinfo is None:
                last_dt = tz.localize(last_dt)
            return now >= last_dt + timedelta(seconds=interval)
        except (ValueError, TypeError):
            return False

    elif schedule_type == "at":
        try:
            target = datetime.fromisoformat(schedule)
            if target.tzinfo is None:
                target = tz.localize(target)
            return now >= target
        except (ValueError, TypeError):
            return False

    return False


# ─── Execution ────────────────────────────────────────────────────────────────

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
        # Send the command text
        subprocess.run(
            ["tmux", "send-keys", "-t", target, "-l", command],
            capture_output=True, timeout=10, check=True
        )
        # Press Enter
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
    """Execute a job. Returns (success, output, duration_seconds)."""
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


# ─── Delivery ─────────────────────────────────────────────────────────────────

def deliver_results(job: dict, success: bool, output: str, duration: float):
    """Send execution results via configured delivery channels."""
    delivery = job.get("delivery", {})
    status = "OK" if success else "ERROR"
    name = job.get("name", job.get("id", "unknown"))

    # Truncate output for notifications
    short_output = output[:500] if output else "(no output)"
    if len(output) > 500:
        short_output += "... (truncated)"

    message = f"[Nox-Cron] {name}\nStatus: {status} ({duration}s)\n{short_output}"

    if delivery.get("telegram", False):
        try:
            subprocess.run(
                ["send-telegram", message],
                timeout=10, capture_output=True
            )
        except Exception as e:
            log(f"Failed to send Telegram notification: {e}")

    if delivery.get("signal", False):
        try:
            subprocess.run(
                ["send-signal", message],
                timeout=10, capture_output=True
            )
        except Exception as e:
            log(f"Failed to send Signal notification: {e}")


# ─── History ──────────────────────────────────────────────────────────────────

def record_history(job: dict, success: bool, output: str, duration: float, error: str = ""):
    """Record an execution in history."""
    history = load_history()

    entry = {
        "job_id": job.get("id", "unknown"),
        "job_name": job.get("name", "unknown"),
        "timestamp": datetime.now().isoformat(),
        "status": "success" if success else "error",
        "duration_seconds": duration,
        "output": output[:500] if output else "",
        "error": error if not success else ""
    }

    history.append(entry)

    # Trim to max entries
    if len(history) > HISTORY_MAX:
        history = history[-HISTORY_MAX:]

    save_history(history)


# ─── Signal Handlers ─────────────────────────────────────────────────────────

_reload_flag = False


def handle_sighup(signum, frame):
    """Set flag to reload jobs on next loop iteration."""
    global _reload_flag
    _reload_flag = True
    log("SIGHUP received, will reload jobs")


def handle_sigterm(signum, frame):
    """Graceful shutdown."""
    log("Received shutdown signal, cleaning up...")
    cleanup()
    sys.exit(0)


# ─── PID File ─────────────────────────────────────────────────────────────────

def write_pid():
    """Write current PID to file."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()) + "\n")
    log(f"PID {os.getpid()} written to {PID_FILE}")


def cleanup():
    """Remove PID file on exit."""
    try:
        if PID_FILE.exists():
            PID_FILE.unlink()
            log("PID file removed")
    except OSError as e:
        log(f"Error removing PID file: {e}")


# ─── Main Loop ───────────────────────────────────────────────────────────────

def main():
    global _reload_flag

    # Set up signal handlers
    signal.signal(signal.SIGHUP, handle_sighup)
    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)

    # Write PID file
    write_pid()

    # Ensure config directory exists
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # Load initial jobs
    jobs = load_jobs()
    log(f"Nox-Cron daemon started with {len(jobs)} job(s)")

    # Compute initial next_run for all jobs
    for job in jobs:
        if job.get("enabled", True):
            job["next_run"] = compute_next_run(job)
    save_jobs(jobs)

    try:
        while True:
            # Check for reload
            if _reload_flag:
                _reload_flag = False
                jobs = load_jobs()
                log(f"Jobs reloaded: {len(jobs)} job(s)")
                for job in jobs:
                    if job.get("enabled", True):
                        job["next_run"] = compute_next_run(job)
                save_jobs(jobs)

            # Check each job
            modified = False
            for job in jobs:
                if not job.get("enabled", True):
                    continue

                if is_due(job):
                    name = job.get("name", job.get("id", "?"))
                    log(f"Firing job: {name} (mode={job.get('execution_mode')})")

                    success, output, duration = execute_job(job)

                    status_str = "OK" if success else "ERROR"
                    log(f"Job '{name}' completed: {status_str} in {duration}s")

                    # Update last_run
                    tz_name = job.get("timezone", DEFAULT_TIMEZONE)
                    tz = get_tz(tz_name)
                    job["last_run"] = datetime.now(tz).isoformat()

                    # Handle one-shot 'at' jobs
                    if job.get("schedule_type") == "at":
                        job["enabled"] = False
                        job["next_run"] = None
                        log(f"One-shot job '{name}' disabled after execution")
                    else:
                        job["next_run"] = compute_next_run(job)

                    modified = True

                    # Record history
                    error_msg = output if not success else ""
                    record_history(job, success, output, duration, error_msg)

                    # Deliver results
                    deliver_results(job, success, output, duration)

            if modified:
                save_jobs(jobs)

            time.sleep(CHECK_INTERVAL)

    except Exception as e:
        log(f"Fatal error: {e}")
        raise
    finally:
        cleanup()


if __name__ == "__main__":
    main()
