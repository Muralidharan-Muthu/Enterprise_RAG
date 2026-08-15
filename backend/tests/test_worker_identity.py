"""Tests for app.core.worker_identity — the worker/code-version stamp used
to diagnose which process touched a given ingestion_jobs row (migration 015).

Motivating incident: a stray native Celery worker running stale/older code
competed with the Docker worker container on the same Redis queue, silently
producing corrupted results with zero error signal in document_registry.
worker_id + code_version let that be diagnosed after the fact.

CODE_VERSION / WORKER_ID are computed once at module import time, so these
tests exercise the underlying _compute_* functions directly (re-invokable,
unlike the frozen module-level constants) via subprocess mocking.
"""
from __future__ import annotations

import re
import subprocess
from unittest.mock import MagicMock, patch

from app.core import worker_identity


def test_code_version_fetch_succeeds():
    """git rev-parse succeeds -> returns the trimmed short hash."""
    fake_result = MagicMock(returncode=0, stdout="abc1234\n", stderr="")
    with patch.object(worker_identity.subprocess, "run", return_value=fake_result) as mock_run:
        version = worker_identity._compute_code_version()

    assert version == "abc1234"
    mock_run.assert_called_once()
    _, kwargs = mock_run.call_args
    assert kwargs.get("timeout") == 2
    assert kwargs.get("cwd") == str(worker_identity._REPO_ROOT)


def test_code_version_fetch_fails_gracefully_returns_unknown_on_nonzero_exit():
    """git present but errors (e.g. not a git repo) -> 'unknown', never raises."""
    fake_result = MagicMock(returncode=128, stdout="", stderr="fatal: not a git repository")
    with patch.object(worker_identity.subprocess, "run", return_value=fake_result):
        version = worker_identity._compute_code_version()

    assert version == "unknown"


def test_code_version_fetch_fails_gracefully_when_git_missing():
    """git binary not on PATH (FileNotFoundError) -> 'unknown', never raises."""
    with patch.object(worker_identity.subprocess, "run", side_effect=FileNotFoundError("git not found")):
        version = worker_identity._compute_code_version()

    assert version == "unknown"


def test_code_version_fetch_fails_gracefully_on_timeout():
    """git hangs past the 2s timeout -> 'unknown', never raises."""
    with patch.object(
        worker_identity.subprocess, "run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=2),
    ):
        version = worker_identity._compute_code_version()

    assert version == "unknown"


def test_code_version_fetch_fails_gracefully_on_empty_stdout():
    """git succeeds but returns empty output (edge case) -> 'unknown', not ''."""
    fake_result = MagicMock(returncode=0, stdout="   \n", stderr="")
    with patch.object(worker_identity.subprocess, "run", return_value=fake_result):
        version = worker_identity._compute_code_version()

    assert version == "unknown"


def test_worker_id_format_is_stable_and_parseable():
    """worker_id = '<hostname>-<pid>-<start_ts>' — three dash-joined parts,
    with pid and start_ts each parseable as integers."""
    with patch.object(worker_identity.socket, "gethostname", return_value="myhost"), \
         patch.object(worker_identity.os, "getpid", return_value=4321), \
         patch.object(worker_identity, "_PROCESS_START_TS", 1700000000):
        worker_id = worker_identity._compute_worker_id()

    assert worker_id == "myhost-4321-1700000000"
    # Parseable: last two dash-segments are integers, everything before is host.
    match = re.match(r"^(.+)-(\d+)-(\d+)$", worker_id)
    assert match is not None
    hostname, pid, start_ts = match.groups()
    assert hostname == "myhost"
    assert int(pid) == 4321
    assert int(start_ts) == 1700000000


def test_worker_id_falls_back_gracefully_when_hostname_fails():
    """socket.gethostname() raising must not propagate — falls back to a
    placeholder hostname so the pid/timestamp portion still identifies the
    process."""
    with patch.object(worker_identity.socket, "gethostname", side_effect=OSError("no hostname")), \
         patch.object(worker_identity.os, "getpid", return_value=99), \
         patch.object(worker_identity, "_PROCESS_START_TS", 1700000001):
        worker_id = worker_identity._compute_worker_id()

    assert worker_id == "unknown-host-99-1700000001"


def test_module_level_constants_are_populated_strings():
    """CODE_VERSION and WORKER_ID are computed once at import time and are
    always non-empty strings (never None), regardless of git availability in
    the test environment."""
    assert isinstance(worker_identity.CODE_VERSION, str)
    assert len(worker_identity.CODE_VERSION) > 0
    assert isinstance(worker_identity.WORKER_ID, str)
    assert re.match(r"^.+-\d+-\d+$", worker_identity.WORKER_ID)
