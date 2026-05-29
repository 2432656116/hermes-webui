"""
Hermes WebUI — Hot update module.

Supports pull & apply updates from the git origin without leaving the browser.
Uses git CLI (must be available) for safe operations.

Endpoints:
  GET  /api/update/check   — check if updates available
  POST /api/update/pull    — git pull from origin
  POST /api/update/restart — signal for restart (writes sentinel file)
"""

import os
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RESTART_SENTINEL = Path(os.environ.get("HERMES_WEBUI_STATE_DIR", os.path.expanduser("~/.hermes/webui"))) / ".restart-requested"


def _run_git(args, timeout=15):
    """Run a git command in the repo root."""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout.strip(), result.stderr.strip(), result.returncode
    except subprocess.TimeoutExpired:
        return "", "git timed out", 1
    except FileNotFoundError:
        return "", "git not found", 1


def check_updates():
    """Check if new commits are available on origin/master."""
    # Fetch silently
    _run_git(["fetch", "origin", "--quiet"], timeout=15)

    local, _, _ = _run_git(["rev-parse", "HEAD"])
    remote, _, _ = _run_git(["rev-parse", "origin/master"])

    behind = False
    commits_behind = []
    if local and remote and local != remote:
        behind = True
        log, _, _ = _run_git(["log", f"{local}..{remote}", "--oneline", "-20"])
        commits_behind = log.split("\n") if log else []

    # Check if restart was requested
    restart_requested = RESTART_SENTINEL.exists()

    return {
        "behind": behind,
        "current": local[:8] if local else "unknown",
        "remote": remote[:8] if remote else "unknown",
        "commits_behind": commits_behind[:10],
        "restart_requested": restart_requested,
        "repo_root": str(REPO_ROOT),
    }


def pull_updates():
    """Pull latest from origin/master. Returns commit info."""
    local_before, _, _ = _run_git(["rev-parse", "HEAD"])
    stdout, stderr, rc = _run_git(["pull", "origin", "master", "--ff-only"], timeout=30)
    local_after, _, _ = _run_git(["rev-parse", "HEAD"])

    if rc != 0:
        return {"success": False, "error": stderr or "pull failed"}

    changes = []
    if local_before != local_after:
        log, _, _ = _run_git(["log", f"{local_before}..{local_after}", "--oneline", "--stat"])
        changes = [l.strip() for l in (log.split("\n") if log else []) if l.strip()]

    # Request restart
    RESTART_SENTINEL.parent.mkdir(parents=True, exist_ok=True)
    RESTART_SENTINEL.write_text(str(int(time.time())))

    return {
        "success": True,
        "before": local_before[:8] if local_before else "unknown",
        "after": local_after[:8] if local_after else "unknown",
        "changes": changes[:30],
        "restart_needed": True,
    }


def clear_restart_sentinel():
    """Clear the restart request flag."""
    if RESTART_SENTINEL.exists():
        RESTART_SENTINEL.unlink()
        return {"cleared": True}
    return {"cleared": False}
