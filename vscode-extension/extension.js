// adhd terminal focus — the missing half of per-session toast targeting.
//
// adhd's tray can raise the VS Code *window* that hosts a Claude session, but
// integrated terminals are xterm.js panes in one Electron window, not OS
// windows — so no external click-to-focus trick can pick a specific tab. Only
// VS Code itself can. This tiny extension closes that gap: it registers a
// `vscode://adhd.focus/...` URI handler, and when adhd fires
//
//     vscode://adhd.focus/term?shellPid=<pid>
//
// (on a toast click), it finds the integrated terminal whose shell process is
// <pid> and reveals it. <pid> is the terminal's own shell — the parent of the
// session's `claude` process, which is exactly what `Terminal.processId`
// reports — so the match is precise even with many Claude tabs in one window.
//
// The tray raises the correct window first, so in multi-window setups the URI
// (handled by the topmost window) lands on the right extension host.

const vscode = require("vscode");

function activate(context) {
  context.subscriptions.push(
    vscode.window.registerUriHandler({
      async handleUri(uri) {
        // uri: vscode://adhd.focus/term?shellPid=21316
        let want = 0;
        try {
          const q = new URLSearchParams(uri.query || "");
          want = parseInt(q.get("shellPid") || q.get("pid") || "", 10);
        } catch (e) {
          want = 0;
        }
        if (!want) return;

        // Terminal.processId is a Thenable<number> resolving to the shell pid.
        // Resolve them all, then reveal the one whose shell matches.
        const terms = vscode.window.terminals;
        const pids = await Promise.all(
          terms.map((t) => Promise.resolve(t.processId).catch(() => undefined))
        );
        for (let i = 0; i < terms.length; i++) {
          if (pids[i] === want) {
            terms[i].show(false); // reveal AND take focus (preserveFocus=false)
            return;
          }
        }
        // No match: the session probably restarted (new shell pid) or its tab
        // was closed. Do nothing — the tray already raised the window, which is
        // the best we can honestly offer without guessing at the wrong tab.
      },
    })
  );
}

function deactivate() {}

module.exports = { activate, deactivate };
