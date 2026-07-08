#!/usr/bin/env python3
"""Claude Code multi-instance monitor: hook handler.

Invoked by Claude Code on lifecycle events (one process per event). Reads the
event JSON on stdin and writes/updates a per-session state file that the
dashboard (monitor.py) reads. Must be fast and must never fail the host
session, so everything is wrapped and we always exit 0.
"""
import json
import os
import subprocess
import sys
import time
import tempfile

from history import project_name, record_closed
from winplat import IS_WIN, NOWIN, find_claude_ancestor

STATE_DIR = os.environ.get("ADHD_STATE_DIR") or os.path.join(
    os.path.expanduser("~"), ".adhd", "state")


def detect_tty():
    """Best-effort controlling terminal device, e.g. /dev/ttys011.

    The hook's stdin/stdout are piped by Claude Code, so os.ttyname usually
    fails; fall back to asking ps for the tty of our parent (the claude proc).
    Windows has no ttys at all — sessions are anchored by the claude process's
    pid instead (see detect_terminal).
    """
    if IS_WIN:
        return ""
    for fd in (2, 1, 0):
        try:
            return os.ttyname(fd)
        except OSError:
            pass
    try:
        out = subprocess.check_output(
            ["ps", "-o", "tty=", "-p", str(os.getppid())],
            stderr=subprocess.DEVNULL, text=True).strip()
        if out and out not in ("?", "??"):
            return out if out.startswith("/dev/") else "/dev/" + out
    except Exception:
        pass
    return ""


def detect_root(cwd):
    """The project root, i.e. the folder VS Code most likely has open.

    The session cwd may be a deep subdir (e.g. crates/foo/src); the git
    top-level is a far better focus target and display name. Falls back to cwd.
    """
    try:
        out = subprocess.check_output(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL, text=True, **NOWIN).strip()
        if out:
            return out
    except Exception:
        pass
    return cwd


def detect_terminal(cwd):
    """Identifiers the dashboard uses to focus this session's window later.

    On Windows the anchor is the claude process itself: the hook is a
    descendant of the claude that spawned it, so the nearest claude ancestor's
    pid (+ creation time, guarding pid reuse) identifies this session for
    reaping, window focus, and auto-resume. TERM_PROGRAM still marks VS Code
    (it sets the var on every platform); WT_SESSION marks Windows Terminal.
    """
    term = {
        "term_program": os.environ.get("TERM_PROGRAM", ""),
        "tmux_pane": os.environ.get("TMUX_PANE", ""),
        "iterm_session_id": os.environ.get("ITERM_SESSION_ID", ""),
        "tty": detect_tty(),
        "root": detect_root(cwd),
    }
    if IS_WIN:
        anc = find_claude_ancestor()
        term["claude_pid"] = anc.get("pid", 0)
        term["claude_create"] = anc.get("create", 0.0)
        if not term["term_program"] and os.environ.get("WT_SESSION"):
            term["term_program"] = "WindowsTerminal"
    return term


def read_title(transcript_path, fallback=""):
    """The conversation's current Claude-generated title, or `fallback`.

    Claude writes an `ai-title` line into the transcript each time it refreshes
    the chat's title; the newest one is the live title. We scan only the tail of
    the file (titles are rewritten as the chat grows, so a recent one sits near
    the end) to stay cheap on every hook fire, and return `fallback` when the
    path is missing or the tail holds no title — so a long run of tool calls
    after the last title keeps showing the previous one instead of going blank.
    """
    if not transcript_path or not os.path.isfile(transcript_path):
        return fallback
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))  # last 64 KiB is plenty for a recent title
            tail = f.read().decode("utf-8", "replace")
    except Exception:
        return fallback
    title = fallback
    for line in tail.splitlines():
        # We may have seeked into the middle of a line; a partial first line just
        # fails to parse and is skipped. Cheap pre-filter before the json.loads.
        if '"ai-title"' not in line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") == "ai-title" and obj.get("aiTitle"):
            title = obj["aiTitle"]
    return title


def classify(data):
    """Return (state, detail) for the incoming event.

    states: working | waiting | limit | idle | done
      working -> actively processing (prompt/tool in flight)
      waiting -> blocked on a permission/approval prompt (needs YOU)
      limit   -> blocked on a usage/rate limit, waiting for it to reset
      idle    -> finished a turn, sitting at the prompt for next input
      done    -> session ended (file is removed instead, see main)
    """
    event = data.get("hook_event_name", "")

    if event == "UserPromptSubmit":
        return "working", "thinking"
    if event == "PreToolUse":
        return "working", data.get("tool_name", "tool")
    if event == "PostToolUse":
        return "working", "ran " + data.get("tool_name", "tool")
    if event == "SubagentStop":
        return "working", "subagent done"
    if event == "SessionStart":
        return "idle", data.get("source", "started")
    if event == "Stop":
        return "idle", "done"
    if event == "Notification":
        ntype = str(data.get("notification_type", ""))
        msg = str(data.get("message", ""))
        low = (ntype + " " + msg).lower()
        # Usage/rate limit: blocked on the clock, not on a decision you can make
        # right now. Keep it out of the "waiting" (needs-you) bucket so the
        # dashboard doesn't flag it red like an approval prompt.
        if "limit" in low and ("usage" in low or "rate" in low
                               or "reset" in low or "reached" in low):
            return "limit", msg or "usage limit reached"
        if "permission" in low or "approve" in low or "allow" in low or "needs your" in low:
            return "waiting", msg or "needs approval"
        if "waiting for your input" in low or "idle" in low:
            return "idle", "waiting for input"
        return "waiting", msg or "needs attention"
    # Unknown event: don't change semantics, just note it.
    return "working", event or "active"


def main():
    raw = sys.stdin.read()
    data = json.loads(raw) if raw.strip() else {}

    sid = data.get("session_id") or "unknown"
    event = data.get("hook_event_name", "")
    path = os.path.join(STATE_DIR, sid + ".json")

    # Session ended cleanly: record it to history, then remove its state file so
    # it drops off the dashboard. (Crash/power-off skips SessionEnd, so the
    # dashboard's reaper records those closures instead — see monitor.load_sessions.)
    if event == "SessionEnd":
        try:
            with open(path) as f:
                record_closed(json.load(f))
        except Exception:
            pass
        try:
            os.remove(path)
        except OSError:
            pass
        return

    state, detail = classify(data)
    cwd = data.get("cwd") or os.getcwd()

    # Reuse the terminal block (and last-known title) captured on a prior event so
    # we only pay the tty/ps lookup once per session, not on every hook fire.
    prev = {}
    try:
        with open(path) as f:
            prev = json.load(f)
    except Exception:
        pass
    term = prev.get("term")
    if not term:
        term = detect_terminal(cwd)
    else:
        if not term.get("root"):
            term["root"] = detect_root(cwd)  # backfill for pre-existing sessions
        # Windows: re-anchor the claude pid when a NEW claude process reports
        # under an old sid (`--resume` reuses the session id, so the cached pid
        # points at the dead predecessor), and keep retrying a capture that
        # failed on the first event (e.g. psutil not importable yet) — a stale
        # or zero pid makes the session unfocusable and mis-reapable.
        if IS_WIN and (event == "SessionStart" or not term.get("claude_pid")):
            anc = find_claude_ancestor()
            if anc:
                term["claude_pid"] = anc["pid"]
                term["claude_create"] = anc.get("create", 0.0)

    root = term.get("root") or cwd
    record = {
        "session_id": sid,
        "cwd": cwd,
        "project": project_name(root),
        # The chat's Claude-generated title, so the dashboard and menu bar can
        # show WHAT a session is about, not just which folder it's in. Falls back
        # to the previous title when this event's transcript tail has none.
        "title": read_title(data.get("transcript_path", ""), prev.get("title", "")),
        "state": state,
        "detail": detail,
        "event": event,
        "model": data.get("model", ""),
        "term": term,
        "updated": time.time(),
    }

    os.makedirs(STATE_DIR, exist_ok=True)
    # Atomic write so the dashboard never reads a half-written file.
    fd, tmp = tempfile.mkstemp(dir=STATE_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(record, f)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # never break the host Claude Code session
    sys.exit(0)
