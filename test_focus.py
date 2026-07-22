#!/usr/bin/env python3
"""Unit tests for the Windows toast-click focus path.

Covers the two fixes:
  A) traybar.focus_waiting: a sid-carrying toast whose session has ended must
     NOT yank focus to an unrelated session (regression: it fell back to the
     neediest one).
  B) monitor.focus_vscode_session: emits a routing-free focus-request the
     bundled adhd.focus extension watches, and raises the owner window by title.

Run: python test_focus.py   (no framework needed; exits non-zero on failure)
"""
import json
import os
import tempfile

import monitor
import traybar

_fails = []


def check(cond, msg):
    print(("ok:  " if cond else "FAIL: ") + msg)
    if not cond:
        _fails.append(msg)


class _Rec:
    """Record what focus_session was called with (or that it wasn't)."""
    def __init__(self):
        self.calls = []

    def __call__(self, s):
        self.calls.append(s)


def test_focus_waiting():
    s0 = {"session_id": "a", "project": "neediest"}
    s1 = {"session_id": "b", "project": "clicked"}
    orig_load, orig_focus = traybar.load_sessions, traybar.focus_session
    try:
        rec = _Rec()
        traybar.focus_session = rec

        # sid resolves -> focus exactly that session
        traybar.load_sessions = lambda: [s0, s1]
        rec.calls = []
        traybar.focus_waiting("b")
        check(rec.calls == [s1], "sid present+resolved focuses its own session")

        # sid present but gone -> do nothing (no yank to the neediest)
        traybar.load_sessions = lambda: [s0, s1]
        rec.calls = []
        traybar.focus_waiting("ghost")
        check(rec.calls == [], "sid present+unresolved focuses NOTHING")

        # no sid (legacy toast) -> fall back to the neediest (first) session
        traybar.load_sessions = lambda: [s0, s1]
        rec.calls = []
        traybar.focus_waiting("")
        check(rec.calls == [s0], "no sid falls back to the neediest session")

        # empty session list -> no crash, no focus
        traybar.load_sessions = lambda: []
        rec.calls = []
        traybar.focus_waiting("b")
        check(rec.calls == [], "no sessions -> no focus, no crash")
    finally:
        traybar.load_sessions, traybar.focus_session = orig_load, orig_focus


def test_write_focus_request():
    with tempfile.TemporaryDirectory() as d:
        orig_home, orig_req = monitor.ADHD_HOME, monitor.FOCUS_REQUEST
        monitor.ADHD_HOME = d
        monitor.FOCUS_REQUEST = os.path.join(d, "focus-request.json")
        try:
            check(monitor._write_focus_request(4242), "_write_focus_request returns True")
            with open(monitor.FOCUS_REQUEST) as f:
                req1 = json.load(f)
            check(req1.get("shellPid") == 4242, "request records the shell pid")
            check(isinstance(req1.get("ts"), (int, float)), "request carries a ts")

            monitor._write_focus_request(99)
            with open(monitor.FOCUS_REQUEST) as f:
                req2 = json.load(f)
            check(req2["shellPid"] == 99 and req2["ts"] >= req1["ts"],
                  "a later request overwrites shellPid and never goes back in time")
        finally:
            monitor.ADHD_HOME, monitor.FOCUS_REQUEST = orig_home, orig_req


def test_focus_vscode_session():
    import winplat
    with tempfile.TemporaryDirectory() as d:
        saved = {
            "ADHD_HOME": monitor.ADHD_HOME, "FOCUS_REQUEST": monitor.FOCUS_REQUEST,
            "parent_pid": winplat.parent_pid,
            "ext": winplat.vscode_focus_ext_installed,
            "by_title": winplat.focus_vscode_window_by_title,
            "focus_pid": winplat.focus_pid,
        }
        monitor.ADHD_HOME = d
        monitor.FOCUS_REQUEST = os.path.join(d, "focus-request.json")
        winplat.parent_pid = lambda pid: 5555          # claude_pid -> shell
        winplat.vscode_focus_ext_installed = lambda: True
        winplat.focus_vscode_window_by_title = lambda name: True
        winplat.focus_pid = lambda pid: False          # must not be needed
        try:
            s = {"project": "myproj", "term": {"claude_pid": 1234}}
            msg = monitor.focus_vscode_session(s)
            check("focused" in msg and "myproj" in msg,
                  "returns a 'focused ... terminal' status")
            with open(monitor.FOCUS_REQUEST) as f:
                req = json.load(f)
            check(req["shellPid"] == 5555,
                  "focus_vscode_session broadcasts the session's shell pid")
        finally:
            monitor.ADHD_HOME = saved["ADHD_HOME"]
            monitor.FOCUS_REQUEST = saved["FOCUS_REQUEST"]
            winplat.parent_pid = saved["parent_pid"]
            winplat.vscode_focus_ext_installed = saved["ext"]
            winplat.focus_vscode_window_by_title = saved["by_title"]
            winplat.focus_pid = saved["focus_pid"]


if __name__ == "__main__":
    test_focus_waiting()
    test_write_focus_request()
    test_focus_vscode_session()
    print()
    if _fails:
        print("%d FAILURE(S)" % len(_fails))
        raise SystemExit(1)
    print("ALL PASS")
