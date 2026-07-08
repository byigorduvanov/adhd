# adhd terminal focus

A ~40-line companion extension for [adhd](../README.md) on Windows.

adhd's tray can raise the VS Code **window** that hosts a Claude session, but VS
Code integrated terminals are xterm.js panes inside one Electron window — not OS
windows — so nothing outside VS Code can select a specific terminal **tab**.
This extension is the only thing that can.

It registers a `vscode://adhd.focus/...` URI handler. When you click an adhd
toast, the tray fires:

```
vscode://adhd.focus/term?shellPid=<pid>
```

where `<pid>` is the shell process of the session's terminal (the parent of its
`claude` process — exactly what `Terminal.processId` reports). The handler finds
the matching terminal and reveals it, so the click lands on the exact tab that
needs you even when a dozen Claude sessions share one window.

## Install

`python install.py` copies this folder to
`~/.vscode/extensions/adhd.focus-<version>/` for you. After install (or after
editing it), run **Developer: Reload Window** (or restart VS Code) once so VS
Code loads the extension.

To install by hand instead:

```
cp -r vscode-extension ~/.vscode/extensions/adhd.focus-1.0.0
```

No marketplace, no build step — it's plain JavaScript against the stable API.
