#!/usr/bin/env python3
"""Recently-closed project history for adhd.

When a session closes — cleanly (SessionEnd) or by crash/power-off (its terminal
no longer hosts a live `claude`) — we record its project here so you can re-open
it later from the dashboard. The store is a single JSON file on disk, so it
survives app restarts, updates, and power loss: whatever was open before a crash
lands in history the next time adhd reaps its stale state file.

Capped at the most recent 10 distinct projects, keyed by root path. Writes are
atomic (tmp + os.replace), so the hook processes and the dashboard can append
concurrently without ever leaving a half-written file behind — a racing writer
can at worst lose a single update, never corrupt the store.
"""
import json
import os
import tempfile
import time

ADHD_HOME = os.environ.get("ADHD_HOME") or os.path.join(
    os.path.expanduser("~"), ".adhd")
HISTORY_FILE = os.path.join(ADHD_HOME, "history.json")
MAX_HISTORY = 10

# Separators to trim off a root path before taking its basename. Backslash is
# only a separator on Windows — on macOS it's a legal filename character, so
# stripping it there would rename a (weird but real) project.
_SEPS = "/\\" if os.name == "nt" else "/"


def project_name(root):
    """Display name for a project root: its basename, tolerant of trailing
    separators. Shared by hook.py, monitor.py, and record_closed below."""
    return os.path.basename(root.rstrip(_SEPS)) or root


def load_history():
    """Return the recently-closed projects, newest first (empty list on any error)."""
    try:
        with open(HISTORY_FILE) as f:
            data = json.load(f)
    except Exception:
        return []
    items = data.get("projects") if isinstance(data, dict) else data
    return items if isinstance(items, list) else []


def record_closed(session):
    """Add `session`'s project to history, newest-first, deduped by root path.

    `session` is a state record (the dict hook.py writes per session). Idempotent
    per root: re-closing a project refreshes its entry instead of duplicating it.
    The entry's `closed` time is the session's last activity (`updated`), not the
    moment we noticed — so sessions reaped together after a reboot still sort by
    when they were really last alive. Best effort: never raises, so it's safe to
    call from a reaping loop or a one-shot hook.
    """
    term = session.get("term") or {}
    root = term.get("root") or session.get("cwd") or ""
    if not root:
        return
    entry = {
        "project": session.get("project") or project_name(root),
        "root": root,
        "cwd": session.get("cwd") or root,
        "term_program": term.get("term_program", ""),
        # The closing session's id, so re-opening can `claude --resume <id>` the
        # exact same conversation rather than starting a blank one. Deduping by
        # root means an entry always carries the most recent session for that
        # project. Empty for sessions closed before this field existed.
        "session_id": session.get("session_id", ""),
        "closed": session.get("updated") or time.time(),
    }
    try:
        items = [e for e in load_history() if e.get("root") != root]
        items.append(entry)
        items.sort(key=lambda e: e.get("closed", 0), reverse=True)
        del items[MAX_HISTORY:]
        os.makedirs(ADHD_HOME, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=ADHD_HOME, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"projects": items}, f)
            os.replace(tmp, HISTORY_FILE)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
    except Exception:
        pass
