// adhd terminal focus — the missing half of per-session toast targeting.
//
// adhd's tray can raise the VS Code *window* that hosts a Claude session, but
// integrated terminals are xterm.js panes in one Electron window, not OS
// windows — so no external click-to-focus trick can pick a specific tab. Only
// VS Code itself can. This tiny extension closes that gap.
//
// PRIMARY mechanism — a file the tray writes on a toast click:
//
//     ~/.adhd/focus-request.json   ->   {"shellPid": <pid>, "ts": <seconds>}
//
// EVERY VS Code window watches this file; the one whose integrated terminal has
// shell process <pid> reveals that tab. This is routing-free, which is the
// whole point: a `vscode://` URI is delivered by VS Code to the *focused*
// window, which in a multi-window setup is usually NOT the window holding the
// session — so the URI silently no-ops and "nothing happens" on click. A
// watched file has no such routing: only the true owner reacts, wherever it is.
//
// <pid> is the terminal's own shell — the parent of the session's `claude`
// process, exactly what `Terminal.processId` reports — so the match is precise
// even with many Claude tabs across many windows.
//
// The legacy `vscode://adhd.focus/term?shellPid=<pid>` URI handler is kept for
// backward compatibility and single-window use, but the tray now prefers the
// file above.

const vscode = require("vscode");
const fs = require("fs");
const os = require("os");
const path = require("path");

const REQUEST = path.join(os.homedir(), ".adhd", "focus-request.json");
const LOG = path.join(os.homedir(), ".adhd", "focus-ext.log");

function flog(obj) {
  try {
    fs.appendFileSync(
      LOG,
      JSON.stringify(Object.assign({ t: new Date().toISOString() }, obj)) + "\n"
    );
  } catch (e) {}
}

// Reveal the integrated-terminal tab whose shell pid is `want`, if THIS window
// owns it. Returns true when a terminal matched.
async function revealTerminal(want) {
  if (!want) return false;
  const terms = vscode.window.terminals;
  const pids = await Promise.all(
    terms.map((t) => Promise.resolve(t.processId).catch(() => undefined))
  );
  for (let i = 0; i < terms.length; i++) {
    if (pids[i] === want) {
      terms[i].show(false); // reveal AND focus the tab (preserveFocus=false)
      return true;
    }
  }
  return false;
}

function activate(context) {
  // Ignore any request that predates our activation, so opening/reloading a
  // window doesn't replay the last toast click.
  let lastTs = 0;
  try {
    lastTs = (JSON.parse(fs.readFileSync(REQUEST, "utf8")).ts) || 0;
  } catch (e) {}

  async function onRequestChange() {
    let req;
    try {
      req = JSON.parse(fs.readFileSync(REQUEST, "utf8"));
    } catch (e) {
      return; // missing or mid-write; the atomic replace will fire us again
    }
    const ts = req && req.ts ? req.ts : 0;
    if (typeof req.shellPid !== "number" || ts <= lastTs) return;
    lastTs = ts;
    const matched = await revealTerminal(req.shellPid);
    flog({ ev: "request", want: req.shellPid, matched });
  }

  try {
    // Polling watcher (stat-based): reliable on Windows even across the tray's
    // atomic rename, and it works before the file exists (fires on creation).
    fs.watchFile(REQUEST, { interval: 200 }, onRequestChange);
    context.subscriptions.push({
      dispose: () => fs.unwatchFile(REQUEST, onRequestChange),
    });
  } catch (e) {}

  // Legacy URI handler (kept for compatibility; the tray prefers the file).
  context.subscriptions.push(
    vscode.window.registerUriHandler({
      async handleUri(uri) {
        let want = 0;
        try {
          const q = new URLSearchParams(uri.query || "");
          want = parseInt(q.get("shellPid") || q.get("pid") || "", 10);
        } catch (e) {
          want = 0;
        }
        const matched = await revealTerminal(want);
        flog({ ev: "uri", want, matched });
      },
    })
  );
}

function deactivate() {}

module.exports = { activate, deactivate };
