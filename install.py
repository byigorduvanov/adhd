#!/usr/bin/env python3
"""Installer for adhd — the Claude Code multi-instance monitor.

Does two things, both idempotent (safe to re-run):

1. Drops an `adhd` command on your PATH that launches the dashboard.
2. Wires Claude Code's lifecycle hooks (in ~/.claude/settings.json) to this
   repo's hook.py, so every session reports its state.

Run it from wherever you cloned the repo:

    python3 install.py
"""
import json
import os
import shlex
import subprocess
import sys

IS_WIN = os.name == "nt"

REPO = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(REPO, "hook.py")
MONITOR = os.path.join(REPO, "monitor.py")
MENUBAR = os.path.join(REPO, "menubar.py")   # macOS menu bar (rumps)
TRAYBAR = os.path.join(REPO, "traybar.py")   # Windows tray (pystray)
SETTINGS = os.path.expanduser("~/.claude/settings.json")
LAUNCH_AGENT = os.path.expanduser(
    "~/Library/LaunchAgents/com.adhd.menubar.plist")
# launchd doesn't expand ~, so the plist gets this absolute path baked in. The
# menu-bar app's stdout/stderr land here — so if it ever crashes or vanishes,
# there's a trail instead of silence.
LOG = os.path.expanduser("~/Library/Logs/adhd-menubar.log")

# Events we hook and whether they take a "*" matcher.
EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
          "Notification", "Stop", "SessionEnd"]
WILDCARD = {"PreToolUse", "PostToolUse"}


def _gui_python():
    """pythonw.exe next to our interpreter (no console window), else python."""
    d, name = os.path.split(sys.executable)
    if not name.lower().startswith("pythonw"):
        cand = os.path.join(d, "pythonw.exe")
        if os.path.exists(cand):
            return cand
    return sys.executable


def _console_python():
    """python.exe even if the installer itself runs under pythonw.exe."""
    d, name = os.path.split(sys.executable)
    if name.lower().startswith("pythonw"):
        cand = os.path.join(d, "python.exe")
        if os.path.exists(cand):
            return cand
    return sys.executable


def pick_bin_dir():
    """First writable dir on (or destined for) PATH; create ~/.local/bin if needed.

    Windows: ~/.local/bin is where claude's own native installer drops
    claude.exe, so when it exists it's already on the user's PATH — piggyback
    on it. Otherwise fall back to %LOCALAPPDATA%\\adhd\\bin (main() prints how
    to add it to PATH when needed).
    """
    home = os.path.expanduser("~")
    if IS_WIN:
        local = os.path.join(home, ".local", "bin")
        if os.path.isdir(local):
            return local
        fallback = os.path.join(
            os.environ.get("LOCALAPPDATA", home), "adhd", "bin")
        os.makedirs(fallback, exist_ok=True)
        return fallback
    for d in ("/opt/homebrew/bin", "/usr/local/bin", os.path.join(home, ".local", "bin")):
        if os.path.isdir(d) and os.access(d, os.W_OK):
            return d
    fallback = os.path.join(home, ".local", "bin")
    os.makedirs(fallback, exist_ok=True)
    return fallback


def _write_launcher(bin_dir, name, script, gui=False):
    """Drop a tiny launcher `name` on PATH that runs `script`.

    POSIX gets a shell shim; Windows a .cmd one. `gui` launches through
    pythonw.exe via `start` so the tray app doesn't hold a console window open.
    """
    if IS_WIN:
        target = os.path.join(bin_dir, name + ".cmd")
        if gui:
            body = '@echo off\r\nstart "" "%s" "%s" %%*\r\n' % (_gui_python(), script)
        else:
            body = '@echo off\r\n"%s" "%s" %%*\r\n' % (_console_python(), script)
        # cmd.exe parses batch files in the console OEM codepage (e.g. cp866),
        # NOT the ANSI codepage Python would default to (e.g. cp1251) — the two
        # agree only on ASCII, so a Cyrillic username in the python path would
        # yield a mojibake launcher that "can't find the path". Write OEM;
        # if a path char doesn't exist there, fall back to UTF-8 behind chcp.
        try:
            with open(target, "w", newline="", encoding="oem") as f:
                f.write(body)
        except UnicodeEncodeError:
            with open(target, "w", newline="", encoding="utf-8") as f:
                f.write("@chcp 65001 >nul\r\n" + body)
        return target
    target = os.path.join(bin_dir, name)
    with open(target, "w") as f:
        f.write("#!/bin/sh\nexec %s %s \"$@\"\n"
                % (shlex.quote(sys.executable), shlex.quote(script)))
    os.chmod(target, 0o755)
    return target


def install_command():
    bin_dir = pick_bin_dir()
    target = _write_launcher(bin_dir, "adhd", MONITOR)
    menu = _write_launcher(bin_dir, "adhd-menu",
                           TRAYBAR if IS_WIN else MENUBAR, gui=IS_WIN)
    on_path = bin_dir in os.environ.get("PATH", "").split(os.pathsep)
    return target, menu, bin_dir, on_path


# What the platform's menu/tray app needs. windows-curses gives monitor.py its
# curses; psutil anchors sessions to their claude process (winplat.py).
DEPS_WIN = ["psutil", "pystray", "Pillow", "winotify", "windows-curses"]
DEPS_MAC = ["rumps"]


def ensure_deps():
    """Best-effort: make sure the platform's dependencies are importable."""
    mods = (["psutil", "pystray", "PIL", "winotify", "curses"]
            if IS_WIN else ["rumps"])
    try:
        for m in mods:
            __import__(m)
        return True
    except ImportError:
        pass
    pkgs = DEPS_WIN if IS_WIN else DEPS_MAC
    # POSIX keeps the historical --user-first behavior (a bare install could
    # land in a shared interpreter's site-packages); the bare attempt remains
    # as the venv rescue, where --user is an error. Windows pythons are
    # per-user installs, so the bare attempt is the right default there.
    attempts = ([], ["--user"]) if IS_WIN else (["--user"], [])
    for extra in attempts:
        try:
            subprocess.run(
                [_console_python(), "-m", "pip", "install"] + extra + pkgs,
                check=True)
            return True
        except Exception:
            continue
    return False


def install_login_item():
    """Auto-start the menu/tray app at login.

    Windows: an HKCU Run value (per-user, no admin, delete the value to undo).
    macOS: a LaunchAgent plist.
    """
    if IS_WIN:
        import winreg
        cmd = '"%s" "%s"' % (_gui_python(), TRAYBAR)
        key = winreg.CreateKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run")
        winreg.SetValueEx(key, "adhd-menu", 0, winreg.REG_SZ, cmd)
        winreg.CloseKey(key)
        return r"HKCU\...\Run\adhd-menu"
    os.makedirs(os.path.dirname(LAUNCH_AGENT), exist_ok=True)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    plist = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        '  <key>Label</key><string>com.adhd.menubar</string>\n'
        '  <key>ProgramArguments</key><array>\n'
        '    <string>%s</string><string>%s</string>\n'
        '  </array>\n'
        '  <key>RunAtLoad</key><true/>\n'
        '  <key>KeepAlive</key><true/>\n'
        '  <key>StandardOutPath</key><string>%s</string>\n'
        '  <key>StandardErrorPath</key><string>%s</string>\n'
        '</dict></plist>\n' % (sys.executable, MENUBAR, LOG, LOG))
    with open(LAUNCH_AGENT, "w") as f:
        f.write(plist)
    subprocess.run(["launchctl", "unload", LAUNCH_AGENT],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["launchctl", "load", LAUNCH_AGENT],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return LAUNCH_AGENT


def _is_ours(group):
    """True if a hook group belongs to a prior adhd/cc-monitor install."""
    for h in group.get("hooks", []):
        c = h.get("command", "").rstrip('"')  # Windows commands end quoted
        if c.endswith("hook.py") and (
                HOOK in h.get("command", "") or "cc-monitor" in c
                or "/adhd/" in c or "\\adhd\\" in c):
            return True
    return False


def _hook_command():
    """The hook command line for settings.json, quoted for the platform shell.

    Windows runs hooks through cmd.exe, so paths take double quotes — and
    pythonw.exe rather than python3, so no console window can flash on the
    hook's account even if the host doesn't hide it.
    """
    if IS_WIN:
        return '"%s" "%s"' % (_gui_python(), HOOK)
    return "python3 %s" % shlex.quote(HOOK)


def install_hooks():
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    cfg = {}
    if os.path.exists(SETTINGS):
        with open(SETTINGS) as f:
            cfg = json.load(f)

    hooks = cfg.setdefault("hooks", {})
    cmd = _hook_command()
    for ev in EVENTS:
        # Keep the user's unrelated hooks; replace only our own.
        groups = [g for g in hooks.get(ev, []) if not _is_ours(g)]
        entry = {"hooks": [{"type": "command", "command": cmd, "timeout": 5}]}
        if ev in WILDCARD:
            entry["matcher"] = "*"
        groups.append(entry)
        hooks[ev] = groups

    with open(SETTINGS, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")


def main():
    want_login = "--login" in sys.argv[1:]
    target, menu, bin_dir, on_path = install_command()
    install_hooks()
    have_deps = ensure_deps()

    deps_hint = ("pip install " + " ".join(DEPS_WIN)) if IS_WIN \
        else "pip3 install --user rumps"
    print("adhd installed.")
    print("  command   : %s" % target)
    print("  %s: %s" % ("tray app  " if IS_WIN else "menu bar  ", menu))
    print("  hooks     : wired in %s" % SETTINGS)
    print("  monitor   : %s" % MONITOR)
    print("  deps      : %s" % ("ready" if have_deps
                                else "MISSING — run: " + deps_hint))

    if want_login:
        where = install_login_item()
        print("  login item: %s (adhd-menu starts at login)" % where)

    if on_path:
        print("\nRun the dashboard:  adhd")
        print("Run the %s:  adhd-menu" % ("tray app " if IS_WIN else "menu bar")
              + ("" if want_login else
                 "   (or re-run with --login to auto-start it)"))
    elif IS_WIN:
        print("\n%s is not on your PATH. Add it once (new terminals pick it up):"
              % bin_dir)
        print('  powershell -Command "[Environment]::SetEnvironmentVariable('
              "'Path', [Environment]::GetEnvironmentVariable('Path','User')"
              " + ';%s', 'User')\"" % bin_dir)
        print("\nThen run:  adhd   (dashboard)   or   adhd-menu   (tray app)")
    else:
        rc = "~/.zshrc" if os.environ.get("SHELL", "").endswith("zsh") else "~/.bashrc"
        print("\n%s is not on your PATH. Add it once:" % bin_dir)
        print('  echo \'export PATH="%s:$PATH"\' >> %s' % (bin_dir, rc))
        print("  source %s" % rc)
        print("\nThen run:  adhd   (dashboard)   or   adhd-menu   (menu bar)")
    print("\nAlready-open Claude sessions will report after their next event "
          "(or restart them).")


if __name__ == "__main__":
    main()
