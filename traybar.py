#!/usr/bin/env python3
"""adhd tray app: the Windows counterpart of menubar.py.

Lives in the notification area (system tray) and shows a badge with the number
of sessions WAITING on a permission/approval prompt. Right-click the icon for a
live menu of every session; double-click opens the full curses dashboard.

While it runs it pops Windows toast notifications on the transitions that
matter — a session finishing its turn (working → idle), blocking on a
permission prompt, or hitting a usage/rate limit — with the same repeat
reminders and opt-in auto-resume as the macOS menu bar. Clicking a toast
focuses the session that needs you: toasts launch the `adhd:` URL protocol
(registered per-user in HKCU on startup, the Windows analogue of the macOS
notifier applet), which re-runs this script with --focus.

It reads the same per-session state files as the dashboard and reuses its
session-loading and window-focus logic, so the two always agree. The menu
toggles persist to ~/.adhd/config.json — same file, same keys as macOS.

Run it:   adhd-menu       (after install.py)
   or:    pythonw traybar.py

Requires pystray + Pillow (tray icon) and winotify (toasts); install.py
installs them for you.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

# Reuse the dashboard's data layer: loading + reaping + window focus all live in
# monitor.py. Importing it has no side effects (curses only runs under its own
# __main__), so the tray and the terminal dashboard never drift apart.
from monitor import (  # noqa: E402
    load_sessions, focus_session, fmt_age, dashboard_session)
from monitor import send_text_to_session  # noqa: E402
from winplat import NOWIN, _console_python, acquire_single_instance  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
MONITOR = os.path.join(HERE, "monitor.py")
ADHD_HOME = os.path.join(os.path.expanduser("~"), ".adhd")
CONFIG_PATH = os.path.join(ADHD_HOME, "config.json")
# Rendered once for toasts (winotify wants an absolute image path).
TOAST_ICON = os.path.join(ADHD_HOME, "adhd.png")
REFRESH = 1.0  # seconds between badge/menu refreshes
APP_ID = "adhd"
# Same environment knobs as menubar.py — see the README table.
NOTIFY_DEFAULT = os.environ.get("ADHD_NOTIFY", "1") != "0"
REPEAT_DEFAULT = os.environ.get("ADHD_NOTIFY_REPEAT", "1") != "0"
try:
    REPEAT_AFTER = max(1.0, float(os.environ.get("ADHD_NOTIFY_REPEAT_SECS", "600")))
except ValueError:
    REPEAT_AFTER = 600.0  # 10 minutes
RESUME_DEFAULT = os.environ.get("ADHD_AUTO_RESUME", "0") == "1"
RESUME_TEXT = os.environ.get("ADHD_RESUME_TEXT", "continue")
# Toast sound. winotify defaults every toast to SILENT unless an <audio>
# element is set, so skipping this would make notifications mute. "urgent"
# (the needs-you prompts) gets a more distinctive chime than the rest — the
# macOS app makes the same split with its "Funk" sound. ADHD_NOTIFY_SOUND=0
# keeps the toasts but drops the audio.
SOUND_ENABLED = os.environ.get("ADHD_NOTIFY_SOUND", "1") != "0"
try:
    RESUME_AFTER = max(10.0, float(os.environ.get("ADHD_AUTO_RESUME_SECS", "300")))
except ValueError:
    RESUME_AFTER = 300.0  # 5 minutes
NET_CACHE_SECS = 15.0  # how long a connectivity probe result is trusted
DOT = {"waiting": "🔴", "limit": "🟣", "working": "🟡", "idle": "🟢", "done": "🟢"}
LABEL = {"waiting": "waiting", "limit": "rate-limited", "working": "working",
         "idle": "idle", "done": "idle"}
# Badge color per most-urgent state (waiting > limit > working > idle > none).
COLOR = {"waiting": (214, 48, 49), "limit": (162, 89, 217),
         "working": (225, 177, 44), "idle": (57, 178, 100),
         "none": (120, 120, 120)}


def _clip(s, n):
    """Trim `s` to at most `n` chars, ending in an ellipsis when shortened."""
    s = str(s)
    return s if len(s) <= n else s[: max(0, n - 1)].rstrip() + "…"


def _repeat_label():
    """Menu label for the repeat toggle, showing the actual interval."""
    secs = int(REPEAT_AFTER)
    if secs % 60 == 0:
        return "Repeat reminders (%d min)" % (secs // 60)
    return "Repeat reminders (%ds)" % secs


def _gui_python():
    """pythonw.exe next to our interpreter (no console window), else python."""
    exe = sys.executable or "python"
    d, name = os.path.split(exe)
    if not name.lower().startswith("pythonw"):
        cand = os.path.join(d, "pythonw.exe")
        if os.path.exists(cand):
            return cand
    return exe


def ensure_protocol():
    """Register the per-user `adhd:` URL protocol so toast clicks focus adhd.

    A Windows toast can launch a protocol URL when clicked; pointing `adhd:` at
    `traybar.py --focus` makes every banner clickable without a packaged app —
    the same trick the macOS applet plays with banner ownership. HKCU only (no
    admin), idempotent, best-effort.
    """
    try:
        import winreg
        cmd = '"%s" "%s" --focus "%%1"' % (
            _gui_python(), os.path.abspath(__file__))
        base = winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\adhd")
        winreg.SetValueEx(base, None, 0, winreg.REG_SZ, "URL:adhd protocol")
        winreg.SetValueEx(base, "URL Protocol", 0, winreg.REG_SZ, "")
        cmdkey = winreg.CreateKey(base, r"shell\open\command")
        winreg.SetValueEx(cmdkey, None, 0, winreg.REG_SZ, cmd)
        winreg.CloseKey(cmdkey)
        winreg.CloseKey(base)
        return True
    except Exception:
        return False


def _urgency(counts):
    """The most urgent state present, for icon color (waiting > … > none)."""
    for st in ("waiting", "limit", "working", "idle"):
        if counts[st]:
            return st
    return "none"


def _draw_icon(counts):
    """The tray image: a rounded square colored by urgency, numbered when
    sessions are waiting on you (the macOS `◧ 2` badge, as pixels)."""
    from PIL import Image, ImageDraw, ImageFont
    state = _urgency(counts)
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 4, 60, 60), radius=14, fill=COLOR[state] + (255,))
    if counts["waiting"]:
        text = str(counts["waiting"]) if counts["waiting"] < 10 else "9+"
        font = None
        for name in ("seguisb.ttf", "segoeuib.ttf", "arialbd.ttf"):
            try:
                font = ImageFont.truetype(
                    os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                                 "Fonts", name), 40)
                break
            except Exception:
                continue
        if font is None:
            font = ImageFont.load_default()
        box = d.textbbox((0, 0), text, font=font)
        d.text(((64 - box[2] - box[0]) / 2, (64 - box[3] - box[1]) / 2 - 2),
               text, font=font, fill=(255, 255, 255, 255))
    return img


def ensure_toast_icon():
    """Write the toast PNG once so banners carry the adhd badge."""
    if os.path.exists(TOAST_ICON):
        return TOAST_ICON
    try:
        os.makedirs(ADHD_HOME, exist_ok=True)
        _draw_icon({"waiting": 0, "limit": 0, "working": 1, "idle": 0}
                   ).resize((128, 128)).save(TOAST_ICON)
        return TOAST_ICON
    except Exception:
        return ""


def _toast_safe(s):
    """Neutralize PowerShell/XML metacharacters in toast text.

    winotify interpolates title/msg into a double-quoted PowerShell here-string
    and a CDATA block, so a raw `$(...)` in a project folder name would EXECUTE
    when the toast fires, `$var`/backtick would garble it, and a literal `]]>`
    would kill the toast outright. Project names and Claude's notification
    messages are untrusted input here.
    """
    s = str(s)
    s = s.replace("]]>", "]] >")
    s = s.replace("`", "'").replace('"', "'")
    return s.replace("$", "＄")  # fullwidth: reads the same, inert in PS


def notify(title, message, sound="default", sid=""):
    """Show a Windows toast; clicking it focuses the session it came from.

    The toast's launch URL carries the session id (adhd:focus/<sid>), so the
    protocol handler can jump to that exact window — unlike macOS, where a
    banner can't address a specific session and the click lands on the
    neediest one. Without a sid the click falls back to neediest-first.
    `sound` is "default" or "urgent" (needs-you prompts). winotify posts
    through a hidden PowerShell; the toast lands in the Action Center too, so
    a banner you miss isn't gone. Failures are swallowed — a broken toast
    pipeline must never take the tray loop down with it.
    """
    try:
        from winotify import Notification, audio
        toast = Notification(
            app_id=APP_ID, title=_toast_safe(title), msg=_toast_safe(message),
            icon=ensure_toast_icon(),
            launch="adhd:focus" + ("/" + sid if sid else ""))
        if SOUND_ENABLED:
            toast.set_audio(audio.IM if sound == "urgent" else audio.Default,
                            loop=False)
        toast.show()
    except Exception:
        pass


def open_monitor():
    """Focus the dashboard if it's already open, else launch it. Single-instance.

    Same contract as macOS: the dashboard records a marker (pid + terminal)
    while it runs; raise that window when alive, otherwise open a fresh console
    running the monitor (via the `adhd` launcher when it's on PATH, so the
    window title and environment match a hand-started one).
    """
    existing = dashboard_session()
    if existing:
        focus_session(existing)
        return
    # Pre-quoted command string: list2cmdline leaves space-free args bare, and
    # an unquoted `&` in a path would split the command at the cmd.exe level.
    adhd_cmd = shutil.which("adhd")
    if adhd_cmd:
        cmdline = 'cmd /c start "adhd monitor" "%s"' % adhd_cmd
    else:
        cmdline = ('cmd /c start "adhd monitor" "%s" "%s"'
                   % (_console_python(), MONITOR))
    try:
        subprocess.Popen(cmdline, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, **NOWIN)
    except Exception:
        pass


_net_cache = {"t": 0.0, "ok": False}


def network_up():
    """Best-effort: is the API reachable right now? Cached for NET_CACHE_SECS."""
    now = time.time()
    if now - _net_cache["t"] < NET_CACHE_SECS:
        return _net_cache["ok"]
    ok = False
    try:
        socket.create_connection(("api.anthropic.com", 443), timeout=2).close()
        ok = True
    except OSError:
        ok = False
    _net_cache.update(t=now, ok=ok)
    return ok


def load_config():
    """Read persisted menu toggles. Missing/corrupt file -> {} (use defaults)."""
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    """Persist menu toggles atomically so they survive restarts/reboots."""
    try:
        os.makedirs(ADHD_HOME, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=ADHD_HOME, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f)
        os.replace(tmp, CONFIG_PATH)
    except OSError:
        pass


class AdhdTray:
    """The tray icon + the 1s refresh loop (menubar.py's AdhdMenuApp, in pystray)."""

    def __init__(self):
        cfg = load_config()
        self.notify_enabled = cfg.get("notify", NOTIFY_DEFAULT)
        self.repeat_enabled = cfg.get("repeat", REPEAT_DEFAULT)
        self.resume_enabled = cfg.get("auto_resume", RESUME_DEFAULT)
        # Last-seen state per session id, to spot transitions worth announcing.
        self.prev_state = {}
        # When we last toasted per session id, for the repeat-reminder clock.
        self.last_notified = {}
        # Per-session limit bookkeeping for auto-resume: sid -> {last_try, tries}.
        self.limited = {}
        self.sessions = []
        self.counts = {"waiting": 0, "limit": 0, "working": 0, "idle": 0}
        self._menu_sig = None
        self._icon_sig = None
        self._stop = threading.Event()
        import pystray
        self.icon = pystray.Icon(
            APP_ID, icon=_draw_icon(self.counts), title="adhd",
            menu=pystray.Menu(*self._build_menu()))

    # -- lifecycle ----------------------------------------------------------

    def run(self):
        self.icon.run(setup=self._setup)

    def _setup(self, icon):
        icon.visible = True
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def _quit(self, icon=None, item=None):
        self._stop.set()
        self.icon.stop()

    def _loop(self):
        while not self._stop.wait(REFRESH):
            try:
                self.refresh()
            except Exception:
                pass  # one bad tick must not kill the tray

    # -- refresh ------------------------------------------------------------

    def refresh(self):
        sessions = load_sessions()
        self._maybe_notify(sessions)
        self._maybe_resume(sessions)
        counts = {"waiting": 0, "limit": 0, "working": 0, "idle": 0}
        for s in sessions:
            st = s.get("state", "idle")
            st = "idle" if st == "done" else st
            counts[st] = counts.get(st, 0) + 1
        self.sessions, self.counts = sessions, counts

        # Reassign the icon only when its pixels would differ: pystray's icon
        # setter has no equality check and every assignment writes a temp .ico
        # and reloads GDI handles — churn worth skipping at 1 Hz.
        icon_sig = (_urgency(counts), min(counts["waiting"], 10))
        if icon_sig != self._icon_sig:
            self._icon_sig = icon_sig
            self.icon.icon = _draw_icon(counts)
        # Tooltip = the macOS headline: counts plus the most urgent session's
        # chat title (sessions arrive pre-sorted by urgency, then recency).
        summary = "%d waiting · %d limited · %d working · %d idle" % (
            counts["waiting"], counts["limit"], counts["working"], counts["idle"])
        lead = sessions[0] if sessions else None
        headline = (lead.get("title") or lead.get("project") or "") if lead else ""
        tip = "adhd — " + summary + (" — " + headline if headline else "")
        self.icon.title = _clip(tip, 127)  # Windows tooltips cap at 128 chars

        # Rebuild the native menu only when its contents meaningfully changed:
        # assigning .menu destroys the live HMENU from this thread, and doing
        # that while the user has the menu open (the message-loop thread is
        # blocked in TrackPopupMenuEx on it) kills the menu mid-click. That's
        # why the sig excludes `detail` — it churns on every tool event — and
        # buckets ages to the minute fmt_age displays, keeping rows honest
        # without a rebuild every tick. (No explicit update_menu(): the .menu
        # setter already triggers one; calling it again doubles the race.)
        now = time.time()
        sig = (tuple((s.get("session_id"), s.get("state"), s.get("title"),
                      int((now - s.get("updated", now)) // 60))
                     for s in sessions),
               self.notify_enabled, self.repeat_enabled, self.resume_enabled)
        if sig != self._menu_sig:
            self._menu_sig = sig
            import pystray
            self.icon.menu = pystray.Menu(*self._build_menu())

    def _maybe_notify(self, sessions):
        """Fire on state changes, and re-ping prompts you haven't dealt with.

        Same semantics as menubar.py: seed silently on first sight (no startup
        burst), toast only transitions, repeat-remind sessions that stay
        waiting, drop bookkeeping for sessions that went away.
        """
        seen = set()
        now = time.time()
        for s in sessions:
            sid = s.get("session_id")
            seen.add(sid)
            st = "idle" if s.get("state") == "done" else s.get("state", "idle")
            prev = self.prev_state.get(sid)
            self.prev_state[sid] = st
            if st != "waiting":
                self.last_notified.pop(sid, None)
            if prev is None or not self.notify_enabled:
                continue
            proj = s.get("project") or "?"
            detail = s.get("detail") or ""
            if st == "waiting" and prev != "waiting":
                notify("🔴 %s needs you" % proj, detail or "waiting for approval",
                       sound="urgent", sid=sid)
                self.last_notified[sid] = now
            elif st == "limit" and prev != "limit":
                notify("🟣 %s rate-limited" % proj,
                       detail or "waiting for usage limit to reset", sid=sid)
            elif st == "idle" and prev == "working":
                notify("✅ %s — done" % proj, detail or "turn finished", sid=sid)
            elif (st == "waiting" and self.repeat_enabled
                  and now - self.last_notified.get(sid, now) >= REPEAT_AFTER):
                notify("🔴 %s still needs you" % proj,
                       detail or "still waiting for approval",
                       sound="urgent", sid=sid)
                self.last_notified[sid] = now
        for sid in list(self.prev_state):
            if sid not in seen:
                del self.prev_state[sid]
                self.last_notified.pop(sid, None)

    def _maybe_resume(self, sessions):
        """Auto-proceed sessions stuck on a usage/rate limit, when it's safe.

        Identical policy to menubar.py, but the injection mechanism is
        WriteConsoleInput into the claude process's own console (see
        monitor.send_text_to_session) — process-targeted, so it can never type
        into the wrong window, and VS Code sessions are nudgeable too.
        """
        seen = set()
        now = time.time()
        for s in sessions:
            if s.get("state") != "limit":
                continue
            sid = s.get("session_id")
            seen.add(sid)
            rec = self.limited.get(sid)
            if rec is None:
                self.limited[sid] = {"last_try": now, "tries": 0}
                continue
            if not self.resume_enabled or now - rec["last_try"] < RESUME_AFTER:
                continue
            if not network_up():
                continue  # offline: hold until the connection is back
            rec["last_try"] = now  # re-arm whether or not the nudge lands
            if send_text_to_session(s, RESUME_TEXT):
                rec["tries"] += 1
                if rec["tries"] == 1:
                    notify("↩️ %s — resuming" % (s.get("project") or "?"),
                           "sent “%s” to clear the limit" % RESUME_TEXT,
                           sid=sid)
        for sid in list(self.limited):
            if sid not in seen:
                del self.limited[sid]

    # -- menu ---------------------------------------------------------------

    def _build_menu(self):
        import pystray
        item = pystray.MenuItem
        now = time.time()
        items = []

        summary = "%d waiting · %d limited · %d working · %d idle" % (
            self.counts["waiting"], self.counts["limit"],
            self.counts["working"], self.counts["idle"])
        items.append(item(summary, None, enabled=False))
        items.append(pystray.Menu.SEPARATOR)

        if self.sessions:
            for s in self.sessions:
                st = s.get("state", "idle")
                dot = DOT.get(st, "⚪")
                proj = s.get("project") or "?"
                detail = s.get("detail") or LABEL.get(st, st)
                age = fmt_age(now - s.get("updated", now))
                chat = s.get("title")
                name = "%s · %s" % (proj, _clip(chat, 36)) if chat else proj
                title = "%s  %s — %s (%s)" % (dot, name, detail, age)
                items.append(item(title, self._make_focus(s)))
        else:
            items.append(item("No Claude Code sessions reporting", None,
                              enabled=False))

        items.append(pystray.Menu.SEPARATOR)
        # default=True: double-clicking the tray icon opens the dashboard.
        items.append(item("Open adhd monitor…",
                          lambda icon, it: open_monitor(), default=True))
        items.append(item("Notifications", self._toggle_notify,
                          checked=lambda it: self.notify_enabled))
        items.append(item(_repeat_label(), self._toggle_repeat,
                          checked=lambda it: self.repeat_enabled))
        items.append(item("Auto-resume rate-limited", self._toggle_resume,
                          checked=lambda it: self.resume_enabled))
        items.append(pystray.Menu.SEPARATOR)
        items.append(item("Quit", self._quit))
        return items

    def _toggle_notify(self, icon, it):
        self.notify_enabled = not self.notify_enabled
        self._save_config()

    def _toggle_repeat(self, icon, it):
        self.repeat_enabled = not self.repeat_enabled
        self._save_config()

    def _toggle_resume(self, icon, it):
        self.resume_enabled = not self.resume_enabled
        self._save_config()

    def _save_config(self):
        save_config({"notify": self.notify_enabled,
                     "repeat": self.repeat_enabled,
                     "auto_resume": self.resume_enabled})

    def _make_focus(self, session):
        """Closure so each session row focuses its own window when clicked."""
        def cb(icon, it):
            focus_session(session)
        return cb


def _sid_from_url(url):
    """The session id carried by an adhd:focus/<sid> protocol URL, or ''.

    Tolerates `adhd://focus/<sid>` too — shell components sometimes normalize
    a scheme to the double-slash form before handing it to the handler.
    """
    rest = url.split(":", 1)[-1].lstrip("/")
    if rest.startswith("focus"):
        return rest[len("focus"):].strip("/")
    return ""


def focus_waiting(sid=""):
    """Focus the toast's own session (by sid), then exit.

    Invoked as `traybar.py --focus <adhd:focus/sid>` when a toast is clicked
    (via the adhd: protocol). If that session is gone — or the toast predates
    sid-carrying URLs — fall back to the neediest one: load_sessions() sorts
    waiting-first, then most-recent, so the first row is the best guess.
    """
    sessions = load_sessions()
    if not sessions:
        return
    target = next(
        (s for s in sessions if sid and s.get("session_id") == sid), None)
    focus_session(target or sessions[0])


def main():
    args = sys.argv[1:]
    if "--focus" in args:
        sid = next((_sid_from_url(a) for a in args
                    if a.startswith("adhd:")), "")
        focus_waiting(sid)
        return
    # One tray only: the Run key + a manual adhd-menu would otherwise run two,
    # each toasting every transition and double-injecting auto-resume text.
    # (--focus stays above this guard — toast clicks re-run this script.)
    if not acquire_single_instance():
        return
    ensure_protocol()
    ensure_toast_icon()
    AdhdTray().run()


if __name__ == "__main__":
    main()
