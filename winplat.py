#!/usr/bin/env python3
"""Windows platform layer for adhd — process identity, window focus, and
console input injection.

On macOS a session is anchored by its tty and focused with AppleScript. Windows
has neither, so this module provides the equivalents:

  - identity : the nearest `claude` ancestor process (pid + creation time,
               captured by hook.py). Creation time guards against pid reuse the
               same way the boot-time check guards recycled ttys on macOS.
  - reaping  : live_claude_procs() lists every running claude, so the dashboard
               can drop sessions whose process is gone (monitor.load_sessions).
  - focus    : focus_pid() brings the terminal window hosting a claude pid to
               the front. It attaches to that process's console and raises its
               window; for pseudoconsole hosts (Windows Terminal, VS Code) whose
               console window is hidden, it falls back to the nearest ancestor
               process that owns a visible top-level window.
  - typing   : send_to_pid() writes text (plus Enter) straight into a claude
               process's console *input buffer* via WriteConsoleInput. This is
               what auto-resume uses — it targets the process itself, not a
               window, so a keystroke can never land in the wrong app, focused
               or not. More precise than the macOS AppleScript route.

AttachConsole requires the caller to have no console of its own, and the
dashboard can't give up the console curses is drawing on — so focus/send run in
a short-lived hidden helper: `python winplat.py focus <pid>` /
`send <pid> <base64-text>`. focus_pid()/send_to_pid() spawn that helper; the
in-process implementations are the _*_impl functions at the bottom.

Everything is best-effort and import-safe on non-Windows (functions just return
falsy values), so shared code can import it unconditionally.
"""
import base64
import ctypes
import os
import subprocess
import sys
import time

IS_WIN = os.name == "nt"

# Hide child consoles: without this, every git/pip/helper spawn from a
# console-less process (pythonw tray, pythonw hook) flashes a black window.
CREATE_NO_WINDOW = 0x08000000
NOWIN = {"creationflags": CREATE_NO_WINDOW} if IS_WIN else {}

# Process names claude may run as: the native launcher installs claude.exe;
# an npm install runs the CLI under node.exe.
CLAUDE_NAMES = {"claude.exe", "claude", "node.exe", "node"}


def _psutil():
    """Import psutil lazily; None when unavailable (callers degrade gracefully)."""
    try:
        import psutil
        return psutil
    except Exception:
        return None


def find_claude_ancestor():
    """The nearest ancestor process that is claude, as {'pid', 'create'} — or {}.

    Called by hook.py once per session: the hook is spawned by claude (via a
    shell), so walking up the parent chain finds the exact claude process that
    owns this session. Its pid anchors reaping, focusing, and auto-resume.
    """
    ps = _psutil()
    if not ps:
        return {}
    try:
        for p in ps.Process(os.getpid()).parents():
            try:
                if p.name().lower() in CLAUDE_NAMES:
                    return {"pid": p.pid, "create": p.create_time()}
            except ps.Error:
                continue
    except Exception:
        pass
    return {}


def live_claude_procs():
    """{pid: create_time} for every running claude process, or None if unknown.

    None (psutil missing/failed) tells the caller not to reap — same contract
    as live_ttys() on macOS: better a stale row than dropping a live session.
    """
    ps = _psutil()
    if not ps:
        return None
    out = {}
    try:
        for p in ps.process_iter(["pid", "name", "create_time"]):
            try:
                if (p.info["name"] or "").lower() in CLAUDE_NAMES:
                    out[p.info["pid"]] = p.info["create_time"] or 0.0
            except ps.Error:
                continue
    except Exception:
        return None
    return out


def pid_alive(pid):
    """True if a process with this pid exists. Never signals/kills anything.

    NOTE: os.kill(pid, 0) is NOT a liveness probe on Windows — any signal other
    than CTRL_C/CTRL_BREAK unconditionally TerminateProcess()es the target. This
    exists so no caller ever reaches for that footgun.
    """
    ps = _psutil()
    if ps:
        return ps.pid_exists(pid)
    if not IS_WIN:
        return False
    # ctypes fallback: an openable handle (or an access-denied refusal) = alive.
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if h:
        k32.CloseHandle(h)
        return True
    ERROR_ACCESS_DENIED = 5
    return ctypes.GetLastError() == ERROR_ACCESS_DENIED


def boot_time():
    """Unix time of the last boot, 0.0 if unknown (mirrors monitor.boot_time)."""
    ps = _psutil()
    if ps:
        try:
            return float(ps.boot_time())
        except Exception:
            pass
    if IS_WIN:
        try:  # GetTickCount64 = ms since boot; no psutil needed
            k32 = ctypes.windll.kernel32
            k32.GetTickCount64.restype = ctypes.c_ulonglong
            return time.time() - k32.GetTickCount64() / 1000.0
        except Exception:
            pass
    return 0.0


def proc_cmdline(pid):
    """The command line of `pid` as one string, '' on any failure."""
    ps = _psutil()
    if not ps:
        return ""
    try:
        return " ".join(ps.Process(int(pid)).cmdline())
    except Exception:
        return ""


def _console_python():
    """Path to python.exe even when we run under pythonw.exe (for helpers)."""
    exe = sys.executable or "python"
    d, name = os.path.split(exe)
    if name.lower().startswith("pythonw"):
        cand = os.path.join(d, "python.exe")
        if os.path.exists(cand):
            return cand
    return exe


def _helper(*args, timeout=15):
    """Run this module as a hidden one-shot helper; True on exit 0.

    The helper process frees/attaches consoles at will — something neither the
    curses dashboard (would lose its screen) nor the tray app can safely do
    in-process.
    """
    try:
        return subprocess.run(
            [_console_python(), os.path.abspath(__file__)] + [str(a) for a in args],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout, **NOWIN).returncode == 0
    except Exception:
        return False


def focus_pid(pid):
    """Bring the terminal window hosting process `pid` to the front. Best-effort."""
    return IS_WIN and bool(pid) and _helper("focus", pid)


def send_to_pid(pid, text):
    """Type `text` + Enter into process `pid`'s console input. True on success."""
    if not (IS_WIN and pid):
        return False
    payload = base64.b64encode(str(text).encode("utf-8")).decode("ascii")
    return _helper("send", pid, payload)


# ---------------------------------------------------------------------------
# In-process implementations (run inside the hidden helper subprocess).
# ---------------------------------------------------------------------------

_user32 = ctypes.windll.user32 if IS_WIN else None
_kernel32 = ctypes.windll.kernel32 if IS_WIN else None

if IS_WIN:
    # Declare handle-bearing signatures explicitly: ctypes' default return
    # type is a 32-bit int, which truncates 64-bit HANDLEs/HWNDs and turns
    # INVALID_HANDLE_VALUE into -1 (breaking comparisons against
    # c_void_p(-1).value). With c_void_p, NULL comes back as None.
    _P = ctypes.c_void_p
    _kernel32.GetConsoleWindow.restype = _P
    _kernel32.CreateFileW.restype = _P
    _kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong, _P,
        ctypes.c_ulong, ctypes.c_ulong, _P]
    _kernel32.WriteConsoleInputW.argtypes = [_P, _P, ctypes.c_ulong, _P]
    _kernel32.CloseHandle.argtypes = [_P]
    _user32.SetForegroundWindow.argtypes = [_P]
    _user32.BringWindowToTop.argtypes = [_P]
    _user32.ShowWindow.argtypes = [_P, ctypes.c_int]
    _user32.IsIconic.argtypes = [_P]
    _user32.IsWindowVisible.argtypes = [_P]
    _user32.GetWindowTextLengthW.argtypes = [_P]
    _user32.GetWindowThreadProcessId.argtypes = [_P, _P]

SW_RESTORE = 9
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002

INVALID_HANDLE = ctypes.c_void_p(-1).value
ERROR_ALREADY_EXISTS = 183
_MUTEX = None  # keeps the single-instance mutex handle alive for our lifetime


def acquire_single_instance(name="Local\\adhd-traybar"):
    """Take a named mutex; False if another instance already holds it.

    Guards the tray app: the HKCU Run key plus a manual `adhd-menu` launch
    would otherwise run two trays that each toast every transition and each
    inject auto-resume text into the same limited session. Non-Windows: True.
    """
    global _MUTEX
    if not IS_WIN:
        return True
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        handle = k32.CreateMutexW(None, False, name)
        if not handle or ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            return False
        _MUTEX = handle  # referenced forever; released by process exit
        return True
    except Exception:
        return True  # can't tell — better a duplicate tray than none


def _force_foreground(hwnd):
    """SetForegroundWindow with the standard workarounds.

    Windows refuses foreground changes from background processes; the classic
    escape hatch is a synthetic no-op Alt press (grants our thread the
    foreground right), plus restoring the window if it's minimized.
    """
    if not hwnd:
        return False
    try:
        if _user32.IsIconic(hwnd):
            _user32.ShowWindow(hwnd, SW_RESTORE)
        _user32.keybd_event(VK_MENU, 0, 0, 0)
        ok = bool(_user32.SetForegroundWindow(hwnd))
        _user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        _user32.BringWindowToTop(hwnd)
        return ok
    except Exception:
        return False


def _attach(pid):
    """Detach from any console we have, then attach to `pid`'s. True on success."""
    try:
        _kernel32.FreeConsole()  # fails harmlessly when we have no console
    except Exception:
        pass
    return bool(_kernel32.AttachConsole(int(pid)))


def _visible_window_for_pids(pids):
    """First visible, titled top-level window owned by any pid in `pids` (ordered).

    Used when a session's console window is a hidden pseudoconsole (Windows
    Terminal / VS Code): the window the user actually sees belongs to an
    ancestor process, closest-first.
    """
    found = {}
    EnumWindowsProc = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(hwnd, _):
        if not _user32.IsWindowVisible(hwnd):
            return True
        if _user32.GetWindowTextLengthW(hwnd) == 0:
            return True
        owner = ctypes.c_ulong(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        found.setdefault(owner.value, hwnd)
        return True

    _user32.EnumWindows(EnumWindowsProc(cb), 0)
    for pid in pids:
        if pid in found:
            return found[pid]
    return 0


def _focus_impl(pid):
    """Helper-side focus: console window first, ancestor window as fallback."""
    pid = int(pid)
    hwnd = 0
    if _attach(pid):
        hwnd = _kernel32.GetConsoleWindow()
        _kernel32.FreeConsole()
    # A classic conhost window is visible — focus it and we're done. A
    # pseudoconsole's window exists but is hidden; fall through to ancestors.
    if hwnd and _user32.IsWindowVisible(hwnd):
        return _force_foreground(hwnd)

    ps = _psutil()
    if not ps:
        return False
    try:
        proc = ps.Process(pid)
        chain = []
        for p in proc.parents():
            try:
                name = p.name().lower()
            except ps.Error:
                continue
            # explorer is everyone's ancestor; focusing a random Explorer
            # window would be worse than doing nothing.
            if name in ("explorer.exe", "svchost.exe"):
                continue
            chain.append(p.pid)
    except Exception:
        return False
    return _force_foreground(_visible_window_for_pids(chain))


class _CHAR_UNION(ctypes.Union):
    _fields_ = [("UnicodeChar", ctypes.c_wchar), ("AsciiChar", ctypes.c_char)]


class _KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("bKeyDown", ctypes.c_int),
                ("wRepeatCount", ctypes.c_ushort),
                ("wVirtualKeyCode", ctypes.c_ushort),
                ("wVirtualScanCode", ctypes.c_ushort),
                ("uChar", _CHAR_UNION),
                ("dwControlKeyState", ctypes.c_ulong)]


class _EVENT_UNION(ctypes.Union):
    _fields_ = [("KeyEvent", _KEY_EVENT_RECORD),
                ("_pad", ctypes.c_byte * 16)]


class _INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", ctypes.c_ushort), ("Event", _EVENT_UNION)]


KEY_EVENT = 0x0001
VK_RETURN = 0x0D


def _key_record(ch, vk, down):
    rec = _INPUT_RECORD()
    rec.EventType = KEY_EVENT
    ev = rec.Event.KeyEvent
    ev.bKeyDown = 1 if down else 0
    ev.wRepeatCount = 1
    ev.wVirtualKeyCode = vk
    ev.wVirtualScanCode = 0
    ev.uChar.UnicodeChar = ch
    ev.dwControlKeyState = 0
    return rec


def _send_impl(pid, b64text):
    """Helper-side send: put text + Enter into pid's console input buffer."""
    text = base64.b64decode(b64text).decode("utf-8")
    if not _attach(pid):
        return False
    try:
        GENERIC_RW = 0x80000000 | 0x40000000
        SHARE_RW = 0x00000001 | 0x00000002
        OPEN_EXISTING = 3
        conin = _kernel32.CreateFileW(
            "CONIN$", GENERIC_RW, SHARE_RW, None, OPEN_EXISTING, 0, None)
        if not conin or conin == INVALID_HANDLE:
            return False
        # One KEY_EVENT per UTF-16 code unit, not per code point: c_wchar is
        # 2 bytes on Windows, so an astral-plane char (emoji in the resume
        # text) must be delivered as its surrogate pair, one half per record.
        raw = text.encode("utf-16-le")
        units = [raw[i:i + 2].decode("utf-16-le", "surrogatepass")
                 for i in range(0, len(raw), 2)]
        records = [_key_record(u, 0, True) for u in units]
        records.append(_key_record("\r", VK_RETURN, True))
        records.append(_key_record("\r", VK_RETURN, False))
        arr = (_INPUT_RECORD * len(records))(*records)
        written = ctypes.c_ulong(0)
        ok = bool(_kernel32.WriteConsoleInputW(
            conin, ctypes.byref(arr), len(records), ctypes.byref(written)))
        _kernel32.CloseHandle(conin)
        return ok and written.value == len(records)
    finally:
        _kernel32.FreeConsole()


def main(argv):
    if len(argv) >= 2 and argv[0] == "focus":
        return 0 if _focus_impl(int(argv[1])) else 1
    if len(argv) >= 3 and argv[0] == "send":
        return 0 if _send_impl(int(argv[1]), argv[2]) else 1
    sys.stderr.write("usage: winplat.py focus <pid> | send <pid> <b64text>\n")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:
        sys.exit(1)
