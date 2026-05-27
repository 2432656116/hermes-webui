"""
Hermes WebUI — Cron Live Activity Tracker.

Tracks which cron jobs are currently running, their start time,
and last completion status. Exposed via /api/cron/live-status.

All state is in-memory (no persistence needed — cron jobs report
via stdout events, and status resets on webui restart).
"""

import threading
import time
import logging

logger = logging.getLogger(__name__)

# ── In-memory state ────────────────────────────────────────────────────────
_lock = threading.Lock()

# {job_id: {"name": str, "started_at": float, "phase": str}}
_active: dict[str, dict] = {}

# {job_id: {"name": str, "finished_at": float, "success": bool, "output_snippet": str}}
_last_runs: dict[str, dict] = {}

# Max entries to keep in history
MAX_LAST_RUNS = 50


def mark_started(job_id: str, name: str = ""):
    """Called when a cron job begins execution."""
    with _lock:
        _active[job_id] = {
            "name": name or job_id,
            "started_at": time.time(),
            "phase": "running",
        }
        logger.debug("Cron tracker: started %s (%s)", job_id, name)


def mark_phase(job_id: str, phase: str):
    """Update the phase of a running job (e.g. 'fetching', 'processing')."""
    with _lock:
        if job_id in _active:
            _active[job_id]["phase"] = phase


def mark_finished(job_id: str, success: bool = True, output_snippet: str = ""):
    """Called when a cron job finishes."""
    with _lock:
        entry = _active.pop(job_id, None)
        if entry:
            _last_runs[job_id] = {
                "name": entry.get("name", job_id),
                "finished_at": time.time(),
                "started_at": entry.get("started_at"),
                "duration_s": time.time() - entry.get("started_at", time.time()),
                "success": success,
                "output_snippet": output_snippet[:200],
            }
            # Prune history
            while len(_last_runs) > MAX_LAST_RUNS:
                oldest = next(iter(_last_runs))
                del _last_runs[oldest]
        logger.debug("Cron tracker: finished %s (success=%s)", job_id, success)


def get_snapshot() -> dict:
    """Return current state for the /api/cron/live-status endpoint."""
    with _lock:
        return {
            "active": dict(_active),
            "last_run": dict(_last_runs),
            "active_count": len(_active),
        }


# ── Integration hooks ──────────────────────────────────────────────────────

# These are called from the cron run pipeline in streaming.py or routes.py.
# If not available, the API endpoint still works (just returns empty).

def on_cron_event(event_type: str, job_id: str, **kwargs):
    """Handle a cron lifecycle event from the streaming pipeline."""
    if event_type == "started":
        mark_started(job_id, kwargs.get("name", ""))
    elif event_type == "phase":
        mark_phase(job_id, kwargs.get("phase", "running"))
    elif event_type == "finished":
        mark_finished(
            job_id,
            success=kwargs.get("success", True),
            output_snippet=kwargs.get("output_snippet", ""),
        )
