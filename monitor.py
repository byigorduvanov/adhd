#!/usr/bin/env python3
"""Claude Code multi-instance monitor: live terminal dashboard.

Reads the per-session state files written by hook.py and renders a live,
grouped view: instances WAITING for your input float to the top, then the
ones actively WORKING, then the idle/done ones. Run it in a spare terminal
or tmux pane.

Keys:  ↑/↓ (or j/k) select   ⏎ focus a session / open a closed project
       r  resume a closed project's previous conversation (⇧⏎ works too on
          terminals that report modifier keys — not Terminal.app)
       c clear stale   q quit
"""
import glob
import json
import os
import shlex
import shutil
import subprocess
import sys
import time

try:
    import curses
except ImportError:  # Windows ships no curses; the installer adds the shim.
    # Don't exit here: traybar.py/menubar.py import this module purely for the
    # data layer (load/focus), which needs no curses — only the dashboard
    # entry point below actually requires it.
    curses = None

import winplat
from history import load_history, project_name, record_closed
from winplat import IS_WIN, NOWIN

CURSES_HINT = ("adhd needs the 'windows-curses' package on Windows:\n"
               "    pip install windows-curses\n"
               "(install.py installs it for you.)\n")

STATE_DIR = os.environ.get("ADHD_STATE_DIR") or os.path.join(
    os.path.expanduser("~"), ".adhd", "state")
ADHD_HOME = os.path.dirname(STATE_DIR)  # ~/.adhd
# Single-instance marker for the dashboard: written on launch, removed on exit.
# It lets the menu bar focus an already-open dashboard instead of spawning a
# second one. See write_dashboard_lock() / dashboard_session().
DASHBOARD_LOCK = os.path.join(ADHD_HOME, "dashboard.json")
REFRESH = 1.0          # seconds between redraws
STALE_AFTER = 6 * 3600  # entries older than this are pruneable with 'c'

# TERM_PROGRAM value -> macOS application name to `activate`.
APP_NAME = {
    "Apple_Terminal": "Terminal",
    "iTerm.app": "iTerm",
    "vscode": "Visual Studio Code",
    "WezTerm": "WezTerm",
    "Hyper": "Hyper",
    "ghostty": "Ghostty",
}

# Ordering / styling per state.
ORDER = {"waiting": 0, "limit": 1, "working": 2, "idle": 3, "done": 3}
LABEL = {"waiting": "WAITING", "limit": "LIMIT", "working": "WORKING",
         "idle": "IDLE", "done": "IDLE"}

# Escape-sequence bodies (everything after the ESC) that mean Shift+⏎. Terminals
# that report modifier keys send one of these; the two encodings cover xterm-style
# modifyOtherKeys (CSI 27;2;13~) and the kitty keyboard protocol (CSI 13;2u). Most
# terminals send a bare CR for Shift+⏎ and can't be told apart from ⏎ — `r` is the
# universal resume key for those. See _read_escape() / main().
SHIFT_ENTER = {"[27;2;13~", "[13;2u"}


def live_ttys():
    """TTYs that currently host a live `claude` process.

    Returns a set like {"ttys000", ...}, or None if we couldn't tell (in which
    case callers must not prune — better a stale row than dropping a live one).
    """
    try:
        out = subprocess.run(["ps", "-axo", "tty=,comm="],
                             stdout=subprocess.PIPE, text=True, **NOWIN).stdout
    except Exception:
        return None
    ttys = set()
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and os.path.basename(parts[1].strip()) == "claude":
            ttys.add(parts[0])
    return ttys


_BOOT_TIME = None


def boot_time():
    """Unix time of the last system boot (cached for the process), 0 if unknown.

    Used to reap sessions left over from before a reboot: their state files
    survive a power-off but the claude procs don't, and after a reboot tty
    numbers get reused — so the live-tty test alone could wrongly keep one (or
    let it mis-focus a different, unrelated window). Anything last updated before
    boot is provably from a past life, so we reap it straight to history.
    """
    global _BOOT_TIME
    if _BOOT_TIME is None:
        _BOOT_TIME = 0.0
        if IS_WIN:
            _BOOT_TIME = winplat.boot_time()
            return _BOOT_TIME
        try:
            # macOS: "{ sec = 1718000000, usec = 0 } Tue Jun ..."
            out = subprocess.check_output(
                ["sysctl", "-n", "kern.boottime"],
                stderr=subprocess.DEVNULL, text=True)
            _BOOT_TIME = float(out.split("sec =", 1)[1].split(",", 1)[0])
        except Exception:
            pass
    return _BOOT_TIME


def _reap(fp, record):
    """Remove a dead session's state file, recording its project to history first."""
    record_closed(record)
    try:
        os.remove(fp)
    except OSError:
        pass


def session_root(s):
    """The project root for a live session record (term.root, else cwd)."""
    return (s.get("term") or {}).get("root") or s.get("cwd") or ""


def load_sessions():
    # Crashed/force-closed sessions never send SessionEnd, so they'd linger
    # forever showing their last state (often a red WAITING). Drop any whose
    # terminal no longer has a live claude, or that predates the last reboot,
    # recording each to history on the way out. Skip the live prune if we
    # can't read the process list, or a record has no captured tty/pid.
    # macOS anchors a session by its tty; Windows by the claude process's pid
    # (+ creation time, so a recycled pid can't impersonate a dead session).
    # snap_t is taken BEFORE the process snapshot: a state file written after
    # it may belong to a claude the snapshot missed (psutil freezes its pid set
    # up front, and a cold first enumeration takes seconds), so such records
    # get a pass this tick and are re-judged against the next, warmer snapshot.
    live = None if IS_WIN else live_ttys()
    snap_t = time.time()
    live_pids = winplat.live_claude_procs() if IS_WIN else None
    boot = boot_time()
    out = []
    for fp in glob.glob(os.path.join(STATE_DIR, "*.json")):
        try:
            with open(fp) as f:
                r = json.load(f)
        except Exception:
            continue  # mid-write or garbage; skip this tick
        # Left over from before the last boot: its claude is gone and its tty may
        # now belong to a different session, so reap outright rather than trust it.
        if boot and r.get("updated", 0) < boot:
            _reap(fp, r)
            continue
        if IS_WIN:
            cpid = (r.get("term") or {}).get("claude_pid") or 0
            if live_pids is not None and cpid and r.get("updated", 0) < snap_t - 1.0:
                created = live_pids.get(cpid)
                recorded = (r.get("term") or {}).get("claude_create") or 0.0
                # Gone, or the pid now belongs to a younger process: reap.
                if created is None or (recorded and abs(created - recorded) > 2.0):
                    _reap(fp, r)
                    continue
            out.append(r)
            continue
        tty = (r.get("term", {}).get("tty") or "").replace("/dev/", "")
        if live and tty and tty not in live:
            _reap(fp, r)  # its terminal is gone
            continue
        out.append(r)
    out.sort(key=lambda r: (ORDER.get(r.get("state"), 3), -r.get("updated", 0)))
    return out


def fmt_age(secs):
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m{secs % 60:02d}s"
    return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"


def clear_stale():
    now = time.time()
    for fp in glob.glob(os.path.join(STATE_DIR, "*.json")):
        try:
            with open(fp) as f:
                r = json.load(f)
            if now - r.get("updated", 0) > STALE_AFTER:
                _reap(fp, r)  # to history, like any other close
        except Exception:
            pass


def _run(cmd):
    """Run a command silently; return True on exit 0."""
    try:
        return subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **NOWIN).returncode == 0
    except Exception:
        return False


def _osascript(script):
    return _run(["osascript", "-e", script])


def _osa_str(s):
    """Escape a Python string for embedding inside an AppleScript "..." literal."""
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def _session_exists(sid):
    """True if a transcript for `sid` is still on disk in claude's session store.

    Recorded history can outlive the conversation it points at — transcripts get
    cleared or rotated, and older entries predate session_id capture entirely. A
    `claude --resume <deadid>` just errors with "No conversation found" in a fresh
    window that then dies, which reads as "r does nothing". Checking first lets
    open_project fall back to --continue. Globs every project dir so we needn't
    reproduce claude's cwd->dir-name encoding.
    """
    if not sid:
        return False
    pattern = os.path.join(
        os.path.expanduser("~"), ".claude", "projects", "*", sid + ".jsonl")
    return bool(glob.glob(pattern))


def open_project(entry, resume=False):
    """Re-open a recently-closed project where it lived.

    `entry` is a history record (see history.py). The destination follows the
    editor the session ran in:
      - vscode -> open the project folder in VS Code (`code <root>`); the user
        resumes `claude` in the integrated terminal, which is where it lived.
      - iTerm  -> a fresh iTerm window
      - else   -> a fresh Terminal.app window
    Terminal/iTerm windows `cd` into the root and exec `claude`. With
    `resume=True` (`r` / Shift+⏎) they exec `claude --resume <id>` to bring back
    the *exact* previous conversation — or `claude --continue` (most recent in
    that dir) when that conversation is gone; plain ⏎ starts a fresh one.
    Returns a short status string for the footer.
    """
    root = entry.get("root") or entry.get("cwd") or ""
    proj = entry.get("project") or (project_name(root) if root else "?")
    if not root or not os.path.isdir(root):
        return "can't re-open %s — folder is gone" % proj

    # VS Code keeps its session in the integrated terminal, which we can't script
    # into from out here — so honor "open vscode" by raising/opening the folder
    # window and let the user resume claude there (its persisted state is intact).
    if entry.get("term_program") == "vscode":
        return focus_vscode_window(root, proj)

    if IS_WIN:
        return _open_project_win(entry, root, proj, resume)

    claude = shutil.which("claude") or "claude"
    sid = entry.get("session_id") or ""
    if resume and _session_exists(sid):
        cmd = shlex.quote(claude) + " --resume " + shlex.quote(sid)
        status = "resuming %s…" % proj
    elif resume:
        # The exact conversation is gone (cleared/rotated) or was never captured.
        # `--resume <deadid>` would error and leave a dead window, so fall back to
        # --continue: the most recent conversation in this project's directory.
        cmd = shlex.quote(claude) + " --continue"
        status = "resuming %s… (latest)" % proj
    else:
        cmd = shlex.quote(claude)
        status = "opening %s…" % proj
    run = "cd %s && exec %s" % (shlex.quote(root), cmd)
    if entry.get("term_program") == "iTerm.app":
        # iTerm runs `command` via execvp (no shell), so wrap it in a login shell.
        ok = _osascript(
            'tell application "iTerm"\n'
            '  activate\n'
            '  create window with default profile command "%s"\n'
            'end tell' % _osa_str("/bin/sh -lc %s" % shlex.quote(run)))
    else:
        # Terminal's `do script` runs the string in a fresh interactive shell.
        ok = _osascript(
            'tell application "Terminal"\n'
            '  activate\n'
            '  do script "%s"\n'
            'end tell' % _osa_str(run))
    return status if ok else "couldn't re-open %s" % proj


def _open_project_win(entry, root, proj, resume):
    """Windows re-open: a fresh console window in `root` running claude.

    Prefers Windows Terminal (`wt -d`) when installed, else a classic console
    via `cmd /c start`. The command is passed as separate argv items — never a
    nested quoted string — so paths with spaces survive cmd.exe's parsing. The
    window closes when claude exits, same as the macOS `exec claude` behavior.
    """
    claude = shutil.which("claude") or "claude"
    sid = entry.get("session_id") or ""
    if resume and _session_exists(sid):
        args, status = [claude, "--resume", sid], "resuming %s…" % proj
    elif resume:
        args, status = [claude, "--continue"], "resuming %s… (latest)" % proj
    else:
        args, status = [claude], "opening %s…" % proj
    wt = shutil.which("wt")
    if wt:
        # `;` is wt's subcommand separator and must be escaped inside args.
        ok = _run([wt, "-d", root.replace(";", "\\;")] + args)
    else:
        # Hand cmd.exe a pre-quoted command line rather than a list:
        # list2cmdline only quotes args containing whitespace, so a root like
        # C:\dev\R&D would otherwise reach cmd unquoted and split at the `&`.
        # (`"` is not legal in Windows paths, so plain wrapping is safe.)
        # `start` treats its first quoted argument as the window title, so the
        # title must be passed explicitly or a quoted claude path would eat it.
        ok = _run('cmd /c start "adhd — %s" /D "%s" %s'
                  % (proj.replace('"', ""), root,
                     " ".join('"%s"' % a for a in args)))
    return status if ok else "couldn't re-open %s" % proj


def focus_vscode_window(root, project):
    """Focus the VS Code window that has `root` open, via the `code` CLI.

    `code <folder>` brings an already-open window for that folder to the front
    (and opens one if none exists) — no Accessibility permission needed. We pass
    the project root rather than the raw cwd so a session started in a deep
    subdir still resolves to the window the user actually has open.
    """
    if not root:
        return "no folder recorded for this session (restart it)"
    if IS_WIN:
        # `code` is a .cmd shim on Windows; run it through cmd.exe. Quote the
        # root ourselves — list2cmdline wouldn't, and an unquoted `&` in the
        # path would both open the wrong folder and run the rest as a command.
        if not shutil.which("code"):
            return "`code` CLI not found (install it from VS Code: Shell Command)"
        if _run('cmd /c code "%s"' % root):
            return "focused VS Code: %s" % (project or root)
        return "`code` CLI failed to run"
    code = shutil.which("code") or "/usr/local/bin/code"
    if not os.path.exists(code):
        return "`code` CLI not found (install it from VS Code: Shell Command)"
    if _run([code, root]):
        return "focused VS Code: %s" % (project or root)
    return "`code` CLI failed to run"


def focus_vscode_session(s):
    """Focus the exact VS Code terminal *tab* running session `s` (Windows).

    A Claude session is an integrated-terminal tab, not a window, so two moves:
      1. raise the VS Code window hosting the session's claude process — this
         also makes it the topmost window, which is the one VS Code routes the
         next step to in a multi-window setup; then
      2. hand VS Code `vscode://adhd.focus/term?shellPid=<pid>` so the companion
         extension reveals the tab whose shell is <pid>. That shell is the
         parent of the claude process — exactly what Terminal.processId reports.

    Degrades honestly: window-only when the extension is absent/not-yet-loaded,
    then folder-based focus when no pid was captured (restart the session).
    """
    term = s.get("term") or {}
    proj = s.get("project") or "session"
    cpid = term.get("claude_pid") or 0
    raised = bool(cpid) and winplat.focus_pid(cpid)
    shell = winplat.parent_pid(cpid) if cpid else 0
    if shell and winplat.vscode_focus_ext_installed():
        if winplat.open_uri("vscode://adhd.focus/term?shellPid=%d" % shell):
            return "focused %s terminal" % proj
    if raised:
        return ("raised the VS Code window for %s — install the adhd.focus "
                "extension and reload VS Code to land on the exact tab" % proj)
    root = term.get("root") or s.get("cwd") or ""
    return focus_vscode_window(root, s.get("project") or "")


def focus_session(s):
    """Bring the terminal window/pane running session `s` to the front.

    Returns a short status string for the footer. Picks the best mechanism
    available from what the hook captured: tmux pane > iTerm session id >
    Terminal.app tty > VS Code window (`code` CLI) > plain app activation.
    """
    term = s.get("term") or {}
    prog = term.get("term_program", "")
    pane = term.get("tmux_pane", "")
    iterm = term.get("iterm_session_id", "")
    tty = term.get("tty", "")
    app = APP_NAME.get(prog)

    if IS_WIN:
        # VS Code: raise the window hosting this session's claude process, then
        # switch to its exact terminal tab via the adhd.focus extension. Beats
        # `code <folder>`, which only knows the folder — useless when many
        # sessions share one window (and it can even spawn a stray new window
        # for a terminal cd'd outside the workspace root).
        if prog == "vscode":
            return focus_vscode_session(s)
        # Everything else: raise the terminal window hosting the claude process
        # (classic console directly; Windows Terminal via its top-level window —
        # right window, though not necessarily the right tab).
        cpid = term.get("claude_pid") or 0
        if cpid and winplat.focus_pid(cpid):
            return "focused window of %s" % (s.get("project") or "session")
        if cpid:
            return "couldn't raise the window (session on pid %s)" % cpid
        return "no window info for this session (restart it to capture)"

    # 1) tmux: select the window+pane, then raise whatever app hosts tmux.
    if pane:
        ok = _run(["tmux", "select-window", "-t", pane])
        _run(["tmux", "select-pane", "-t", pane])
        if app:
            _osascript('tell application "%s" to activate' % app)
        return "focused tmux pane %s" % pane if ok else "tmux server not reachable"

    # 2) iTerm2: match the session by its unique id (suffix of ITERM_SESSION_ID).
    if prog == "iTerm.app" and iterm:
        guid = iterm.split(":", 1)[-1]
        script = (
            'tell application "iTerm"\n'
            '  activate\n'
            '  repeat with w in windows\n'
            '    repeat with t in tabs of w\n'
            '      repeat with se in sessions of t\n'
            '        if id of se is "%s" then\n'
            '          select w\n'
            '          tell t to select\n'
            '          select se\n'
            '          return\n'
            '        end if\n'
            '      end repeat\n'
            '    end repeat\n'
            '  end repeat\n'
            'end tell' % guid)
        return "focused iTerm session" if _osascript(script) else "iTerm session not found"

    # 3) Terminal.app: match the tab by its tty device.
    if prog == "Apple_Terminal" and tty:
        script = (
            'tell application "Terminal"\n'
            '  activate\n'
            '  repeat with w in windows\n'
            '    repeat with t in tabs of w\n'
            '      if tty of t is "%s" then\n'
            '        set selected of t to true\n'
            '        set frontmost of w to true\n'
            '        return\n'
            '      end if\n'
            '    end repeat\n'
            '  end repeat\n'
            'end tell' % tty)
        return "focused Terminal tab (%s)" % tty if _osascript(script) else "Terminal tab not found"

    # 4) VS Code: one window per session — focus it via the `code` CLI.
    if prog == "vscode":
        root = term.get("root") or s.get("cwd") or ""
        return focus_vscode_window(root, s.get("project") or "")

    # 5) Fallback: raise the app; we can't pinpoint the exact pane.
    if app:
        _osascript('tell application "%s" to activate' % app)
        return "raised %s; can't target exact pane%s" % (
            app, (" (tty %s)" % tty) if tty else "")

    return "no window info for this session (restart it to capture)"


def send_text_to_session(s, text):
    """Type `text` (plus a Return) into session `s`'s live terminal. True on success.

    Used by the menu bar's auto-resume: when a session is stuck on a usage limit,
    we submit a short prompt to nudge it forward once the limit clears. We only
    inject where we can target the exact pane/tab the session owns — tmux pane,
    iTerm session id, or Terminal tab-by-tty — so a stray keystroke can never land
    in some unrelated window. VS Code (no scriptable terminal) and the bare-app
    fallback return False; the caller skips them rather than guess.
    """
    term = s.get("term") or {}
    prog = term.get("term_program", "")
    pane = term.get("tmux_pane", "")
    iterm = term.get("iterm_session_id", "")
    tty = term.get("tty", "")

    # Windows: write into the claude process's own console input buffer
    # (WriteConsoleInput). This targets the process, not a window, so it can
    # never land in the wrong app — which also makes VS Code sessions safe to
    # nudge here, unlike on macOS where their terminal isn't scriptable.
    if IS_WIN:
        return winplat.send_to_pid(term.get("claude_pid") or 0, text)

    # tmux: send the literal text to the pane, then Enter to submit it.
    if pane:
        ok = _run(["tmux", "send-keys", "-t", pane, "-l", text])
        return _run(["tmux", "send-keys", "-t", pane, "Enter"]) and ok

    # iTerm2: `write text` appends a newline, so it submits as one keystroke run.
    if prog == "iTerm.app" and iterm:
        guid = iterm.split(":", 1)[-1]
        return _osascript(
            'tell application "iTerm"\n'
            '  repeat with w in windows\n'
            '    repeat with t in tabs of w\n'
            '      repeat with se in sessions of t\n'
            '        if id of se is "%s" then\n'
            '          tell se to write text "%s"\n'
            '          return\n'
            '        end if\n'
            '      end repeat\n'
            '    end repeat\n'
            '  end repeat\n'
            'end tell' % (guid, _osa_str(text)))

    # Terminal.app: `do script ... in <tab>` types into that tab's running program
    # (claude), not a new shell — matched by tty so it's the session's own tab.
    if prog == "Apple_Terminal" and tty:
        return _osascript(
            'tell application "Terminal"\n'
            '  repeat with w in windows\n'
            '    repeat with t in tabs of w\n'
            '      if tty of t is "%s" then\n'
            '        do script "%s" in t\n'
            '        return\n'
            '      end if\n'
            '    end repeat\n'
            '  end repeat\n'
            'end tell' % (tty, _osa_str(text)))

    return False  # vscode / unknown: no safe way to target the input


def draw(stdscr, colors, sessions, history, selected_key, status_msg):
    now = time.time()
    h, w = stdscr.getmaxyx()
    stdscr.erase()

    counts = {"waiting": 0, "limit": 0, "working": 0, "idle": 0}
    for s in sessions:
        st = s.get("state", "idle")
        counts["idle" if st == "done" else st] = counts.get("idle" if st == "done" else st, 0) + 1

    title = "  Claude Code Monitor"
    summary = (f"{len(sessions)} running   "
               f"{counts['waiting']} waiting   "
               f"{counts['limit']} limited   "
               f"{counts['working']} working   "
               f"{counts['idle']} idle")
    stdscr.addnstr(0, 0, title.ljust(w), w - 1, curses.A_BOLD)
    stdscr.addnstr(1, 0, "  " + summary, w - 1, curses.A_DIM)

    # Column header
    hdr = f"  {'STATE':<8} {'PROJECT':<22} {'DETAIL':<26} {'AGE':>7}  CWD"
    stdscr.addnstr(3, 0, hdr.ljust(w), w - 1, curses.A_UNDERLINE)

    row = 4
    for s in sessions:
        if row >= h - 1:
            break
        st = s.get("state", "idle")
        label = LABEL.get(st, st.upper())
        attr = colors.get(st, 0)
        if st == "waiting":
            attr |= curses.A_BOLD
        selected = ("s:" + (s.get("session_id") or "")) == selected_key
        if selected:
            attr |= curses.A_REVERSE
        marker = "▶ " if selected else "  "
        proj = (s.get("project") or "?")[:22]
        detail = (s.get("detail") or "")[:26]
        age = fmt_age(now - s.get("updated", now))
        cwd = s.get("cwd") or ""
        line = f"{marker}{label:<8} {proj:<22} {detail:<26} {age:>7}  {cwd}"
        try:
            stdscr.addnstr(row, 0, line.ljust(w), w - 1, attr)
        except curses.error:
            pass
        row += 1

    if not sessions:
        stdscr.addnstr(row + 1, 2, "No Claude Code sessions reporting yet.", w - 3, curses.A_DIM)
        stdscr.addnstr(row + 2, 2, "Start a session in any project (hooks must be installed).", w - 3, curses.A_DIM)
        row += 4

    # Recently-closed projects (newest first). Select one and ⏎ opens it fresh
    # (or raises its VS Code window); r resumes its previous conversation.
    # Survives restarts and power-offs — it's read from history.json on disk.
    if history and row < h - 1:
        row += 1  # spacer between the live sessions and the closed ones
        if row < h - 1:
            stdscr.addnstr(row, 0, "  recently closed".ljust(w), w - 1,
                           curses.A_DIM | curses.A_UNDERLINE)
            row += 1
        for e in history:
            if row >= h - 1:
                break
            selected = ("h:" + (e.get("root") or "")) == selected_key
            attr = curses.A_REVERSE if selected else curses.A_DIM
            marker = "▶ " if selected else "  "
            proj = (e.get("project") or "?")[:22]
            age = fmt_age(now - e.get("closed", now))
            root = e.get("root") or ""
            detail = ("closed %s ago" % age)[:26]
            line = f"{marker}{'CLOSED':<8} {proj:<22} {detail:<26} {'':>7}  {root}"
            try:
                stdscr.addnstr(row, 0, line.ljust(w), w - 1, attr)
            except curses.error:
                pass
            row += 1

    if status_msg:
        stdscr.addnstr(h - 2, 0, ("  " + status_msg).ljust(w), w - 1, curses.A_DIM)
    foot = "  ↑/↓ select   ⏎ open/focus   r resume   c clear stale   q quit"
    stdscr.addnstr(h - 1, 0, foot.ljust(w), w - 1, curses.A_BOLD)
    stdscr.refresh()


def _activate(key, sessions, history, resume=False):
    """Act on the selected row: focus a live session, or re-open a closed project.

    Selection is tracked by a stable key ("s:<session_id>" or "h:<root>") so it
    survives the per-second resort. Plain ⏎ does the natural thing for whichever
    kind of row is selected — focus a live session, open a closed project fresh.
    `resume` (Shift+⏎ / `r`) only changes closed rows: it brings back that
    project's previous conversation. It's a no-op on a live row (already open).
    """
    if key.startswith("s:"):
        if resume:
            return ""  # live session: nothing to resume, it's already running
        sid = key[2:]
        s = next((x for x in sessions if (x.get("session_id") or "") == sid), None)
        return focus_session(s) if s else ""
    root = key[2:]
    e = next((x for x in history if (x.get("root") or "") == root), None)
    return open_project(e, resume=resume) if e else ""


def _pid_alive(pid):
    """True if a process with this pid exists (even if it isn't ours to signal).

    Windows must go through winplat: os.kill(pid, 0) is not a probe there —
    any signal besides CTRL_C/CTRL_BREAK unconditionally TERMINATES the target,
    which here would mean killing the running dashboard we're checking for.
    """
    if IS_WIN:
        return winplat.pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # alive, just owned by someone else
    except OSError:
        return False
    return True


def _is_monitor_proc(pid):
    """Guard against pid reuse: True unless ps says this pid is clearly not us.

    If ps can't tell us (rare), we assume it's the dashboard rather than risk
    spawning a duplicate — the worst case is focus_session() raising the wrong
    window, which is far less annoying than a pile of stray monitors.
    """
    if IS_WIN:
        out = winplat.proc_cmdline(pid)
        return "monitor.py" in out if out else True
    try:
        out = subprocess.check_output(
            ["ps", "-o", "command=", "-p", str(pid)],
            stderr=subprocess.DEVNULL, text=True)
    except Exception:
        return True
    return "monitor.py" in out


def _own_tty():
    """This dashboard's controlling terminal (e.g. /dev/ttys003), or ''.

    Unlike the hook — whose stdio Claude Code pipes — the dashboard owns a real
    terminal, so os.ttyname works directly. Windows has no ttys (and no
    os.ttyname); the lock's pid is the anchor there instead.
    """
    if IS_WIN:
        return ""
    for fd in (0, 1, 2):
        try:
            return os.ttyname(fd)
        except OSError:
            pass
    return ""


def write_dashboard_lock():
    """Record this dashboard's pid + terminal so the menu bar can find it.

    Captures the same identifiers focus_session() needs (term_program, tty,
    iterm/tmux ids). Best-effort: if this fails the only cost is the menu bar
    possibly opening a second window.
    """
    try:
        os.makedirs(ADHD_HOME, exist_ok=True)
        term = {
            "term_program": os.environ.get("TERM_PROGRAM", ""),
            "tmux_pane": os.environ.get("TMUX_PANE", ""),
            "iterm_session_id": os.environ.get("ITERM_SESSION_ID", ""),
            "tty": _own_tty(),
        }
        if IS_WIN:
            # The dashboard is itself a console process, so the same pid-based
            # focus path used for claude sessions can raise its window.
            term["claude_pid"] = os.getpid()
        with open(DASHBOARD_LOCK, "w") as f:
            json.dump({"pid": os.getpid(), "term": term}, f)
    except OSError:
        pass


def clear_dashboard_lock():
    """Remove our marker on exit — but only if it's still ours (pid match)."""
    try:
        with open(DASHBOARD_LOCK) as f:
            if json.load(f).get("pid") != os.getpid():
                return
    except (OSError, ValueError):
        return
    try:
        os.remove(DASHBOARD_LOCK)
    except OSError:
        pass


def dashboard_session():
    """A focus target for an already-running dashboard, or None.

    Reads the marker, confirms its process is still alive (and really a
    monitor.py, guarding pid reuse), and returns a session-shaped dict that
    focus_session() understands. A stale marker (process gone) reads as None, so
    the caller transparently falls back to launching a fresh window.
    """
    try:
        with open(DASHBOARD_LOCK) as f:
            info = json.load(f)
    except (OSError, ValueError):
        return None
    pid = info.get("pid")
    if not isinstance(pid, int) or not _pid_alive(pid) or not _is_monitor_proc(pid):
        return None
    return {"term": info.get("term") or {}, "project": "adhd monitor"}


def _read_escape(stdscr):
    """Collect the rest of an escape sequence after a bare ESC (27) was read.

    Returns the sequence body (everything after the ESC), so the caller can match
    keys curses doesn't decode itself — notably Shift+⏎, which terminals that
    report modifiers send as a CSI sequence. Reads non-blocking (the bytes are
    already buffered) and bounded, restoring the redraw timeout on the way out. A
    lone ESC press just yields ''.
    """
    seq = ""
    stdscr.timeout(0)  # non-blocking for the buffered tail of the sequence
    try:
        for _ in range(12):
            c = stdscr.getch()
            if c == -1 or c > 255:
                break
            seq += chr(c)
            if 0x40 <= c <= 0x7e and c != 0x5b:  # CSI final byte (not the '[')
                break
    finally:
        stdscr.timeout(int(REFRESH * 1000))  # restore the per-second redraw
    return seq


def main(stdscr):
    curses.curs_set(0)
    stdscr.keypad(True)  # decode arrow keys to curses.KEY_* codes
    stdscr.timeout(int(REFRESH * 1000))
    colors = {}
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_RED, -1)
        curses.init_pair(2, curses.COLOR_YELLOW, -1)
        curses.init_pair(3, curses.COLOR_GREEN, -1)
        curses.init_pair(4, curses.COLOR_MAGENTA, -1)
        colors = {
            "waiting": curses.color_pair(1),
            "limit": curses.color_pair(4),
            "working": curses.color_pair(2),
            "idle": curses.color_pair(3),
            "done": curses.color_pair(3),
        }

    # Track selection by a stable key so the per-second resort never moves it.
    selected_key = None
    status_msg = ""
    while True:
        sessions = load_sessions()
        active_roots = {session_root(s) for s in sessions}
        # Hide a project from "recently closed" while it's open again.
        history = [e for e in load_history()
                   if e.get("root") not in active_roots]
        keys = (["s:" + (s.get("session_id") or "") for s in sessions]
                + ["h:" + (e.get("root") or "") for e in history])
        if selected_key not in keys:
            selected_key = keys[0] if keys else None

        draw(stdscr, colors, sessions, history, selected_key, status_msg)
        try:
            ch = stdscr.getch()
        except KeyboardInterrupt:
            break

        if ch == -1:
            continue  # refresh tick, no key pressed
        if ch in (ord("q"), ord("Q")):
            break
        if ch in (ord("c"), ord("C")):
            clear_stale()
            continue

        idx = keys.index(selected_key) if selected_key in keys else 0
        if ch in (curses.KEY_DOWN, ord("j")) and keys:
            selected_key = keys[min(idx + 1, len(keys) - 1)]
            status_msg = ""
        elif ch in (curses.KEY_UP, ord("k")) and keys:
            selected_key = keys[max(idx - 1, 0)]
            status_msg = ""
        elif ch == 27:  # ESC: may begin a Shift+⏎ sequence on reporting terminals
            if _read_escape(stdscr) in SHIFT_ENTER and selected_key:
                status_msg = _activate(selected_key, sessions, history, resume=True)
        elif ch in (curses.KEY_ENTER, 10, 13) and selected_key:
            status_msg = _activate(selected_key, sessions, history)  # open / focus
        elif ch in (ord("r"), ord("R")) and selected_key:
            status_msg = _activate(selected_key, sessions, history, resume=True)


if __name__ == "__main__":
    if curses is None:
        # pythonw has no stderr; fall back so the write can't mask the message.
        (sys.stderr or sys.stdout or open(os.devnull, "w")).write(CURSES_HINT)
        raise SystemExit(1)
    # Single-instance backstop: if a dashboard is already running, raise its
    # window and bow out instead of stacking a second curses view. This holds no
    # matter how we're launched — the menu bar, the `adhd` command, or a bare
    # `python3 monitor.py`. Set ADHD_FORCE=1 to start a second one anyway.
    existing = dashboard_session()
    if existing and os.environ.get("ADHD_FORCE") != "1":
        focus_session(existing)
        raise SystemExit(0)
    write_dashboard_lock()  # so the menu bar focuses this window, not a new one
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
    finally:
        clear_dashboard_lock()
