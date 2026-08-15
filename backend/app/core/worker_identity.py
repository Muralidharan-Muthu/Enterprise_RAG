"""Worker/code-version identity stamp for ingestion diagnosability.

Computed ONCE at module import time (not per-task) so repeated Celery task
invocations don't pay a subprocess-spawn cost per document. Import this
module (or its two constants) from the orchestrator to stamp ingestion_jobs
rows with "who touched this job" — see migration 015.

Motivating incident: a stray native Celery worker running stale/older code
competed with the Docker worker container on the same Redis queue, silently
producing corrupted results for ~50% of otherwise-identical re-ingestions
with zero error signal. worker_id + code_version let a future recurrence be
diagnosed from ingestion_jobs alone instead of requiring live repro.
"""
import logging
import os
import socket
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Repo root: backend/app/core/worker_identity.py -> parents[3] == repo root
# (parents[0]=app/core, [1]=app, [2]=backend, [3]=repo root).
_REPO_ROOT = Path(__file__).resolve().parents[3]

_PROCESS_START_TS = int(time.time())


def _compute_code_version() -> str:
    """Return the current git short commit hash, or 'unknown' on any failure.

    Never raises — a missing git binary, a non-repo checkout (e.g. a Docker
    image built from a source tarball), or any other failure must never
    block ingestion; it just means code_version degrades to 'unknown'.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            sha = result.stdout.strip()
            if sha:
                return sha
        logger.warning(
            "worker_identity: git rev-parse failed (returncode=%s, stderr=%s); "
            "code_version will be 'unknown'",
            result.returncode, result.stderr.strip() if result.stderr else "",
        )
    except Exception as exc:
        logger.warning(
            "worker_identity: git rev-parse unavailable (%s); code_version will be 'unknown'",
            exc,
        )
    return "unknown"


def _compute_worker_id() -> str:
    """Return a stable-for-this-process identifier: '<hostname>-<pid>-<start_ts>'.

    Distinguishes a native venv worker from a Docker container, and one
    container restart from the next, since both hostname (container id) and
    process-start timestamp change across restarts.
    """
    try:
        hostname = socket.gethostname()
    except Exception as exc:
        logger.warning("worker_identity: socket.gethostname() failed (%s); using 'unknown-host'", exc)
        hostname = "unknown-host"
    return f"{hostname}-{os.getpid()}-{_PROCESS_START_TS}"


# Computed once at import time — see module docstring.
CODE_VERSION: str = _compute_code_version()
WORKER_ID: str = _compute_worker_id()
